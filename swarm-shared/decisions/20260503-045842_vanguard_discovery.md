# vanguard / discovery

## 1. Diagnosis
- Training script still resolves data via HF `datasets` API at runtime (streaming/list calls) → exposes surrogate-1 to 429 rate limits and non-reproducible shard order.
- No content-addressed manifest per date folder → epochs drift across runs and resumable training is unreliable.
- Missing CDN-only fetch path → every worker hits `/api/` endpoints instead of bypassing auth via `resolve/main/`.
- No deterministic file list embedded in training job → Lightning re-lists on every epoch/worker start, wasting quota and risking 429 mid-run.
- No lightweight orchestration wrapper for Mac → local runs risk accidental local GPU training instead of delegating to Lightning/Kaggle/Cerebras.

## 2. Proposed change
Create `/opt/axentx/vanguard/discovery/manifest.py` + `/opt/axentx/vanguard/discovery/train_cdn.py` (and a tiny CLI wrapper) that:
- Uses a single `list_repo_tree` call (per date folder) on the Mac to produce `manifest-{date}.json` with deterministic sorted file paths.
- Embeds that manifest in the Lightning training script so data loading uses only CDN URLs (`resolve/main/...`) with zero API calls during training.
- Adds a `VANGUARD_ENV` guard to prevent local GPU training on Mac.

## 3. Implementation

```bash
# /opt/axentx/vanguard/discovery/manifest.py
#!/usr/bin/env python3
"""
Generate a content-addressed manifest for a date folder in a HF dataset repo.
Usage:
  python manifest.py --repo huggingface/dataset --date 2026-05-03 --out manifest-2026-05-03.json
"""
import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    raise SystemExit("pip install huggingface_hub")

HF_TOKEN = os.getenv("HF_TOKEN", "")

def list_date_files(repo_id: str, date: str, api: HfApi):
    """
    List files for a single date folder (non-recursive per folder) to avoid
    recursive pagination and reduce API calls.
    """
    prefix = f"{date}/"
    entries = api.list_repo_tree(repo_id=repo_id, path=prefix, recursive=False, repo_type="dataset")
    files = []
    for e in entries:
        if not e.path.endswith("/"):
            files.append(e.path)

    # If nested per-date/filename, recurse one level only for subfolders
    extra = []
    for e in entries:
        if e.path.endswith("/"):
            sub = api.list_repo_tree(repo_id=repo_id, path=e.path, recursive=False, repo_type="dataset")
            for s in sub:
                if not s.path.endswith("/"):
                    extra.append(s.path)
    files.extend(extra)
    return sorted(set(files))

def build_manifest(repo_id: str, date: str, out_path: Path):
    api = HfApi(token=HF_TOKEN)
    files = list_date_files(repo_id, date, api)

    manifest = {
        "repo_id": repo_id,
        "date": date,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "files": files,
        "count": len(files),
        "cdn_prefix": f"https://huggingface.co/datasets/{repo_id}/resolve/main/",
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files -> {out_path}")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate HF dataset manifest for a date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo id, e.g. org/repo")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-03")
    parser.add_argument("--out", default="manifest.json", help="Output JSON path")
    args = parser.parse_args()

    # Rate-limit courtesy: small sleep before API call if token present (avoid burst)
    if HF_TOKEN:
        time.sleep(1.0)

    build_manifest(args.repo, args.date, Path(args.out))
```

```python
# /opt/axentx/vanguard/discovery/train_cdn.py
#!/usr/bin/env python3
"""
Lightning-compatible surrogate-1 training using CDN-only fetches.
Embed a pre-generated manifest to avoid HF API calls during training.
"""
import json
import os
import random
from pathlib import Path
from typing import Dict, List

import torch
from torch.utils.data import Dataset, DataLoader

try:
    import lightning as L
except ImportError:
    L = None

VANGUARD_ENV = os.getenv("VANGUARD_ENV", "local")
if VANGUARD_ENV == "local" and torch.cuda.is_available():
    raise RuntimeError(
        "VANGUARD_ENV=local should not run GPU training. "
        "Set VANGUARD_ENV=lightning/kaggle/remote or use Lightning Studio / Kaggle."
    )

def load_manifest(manifest_path: str) -> Dict:
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)

class CDNTextDataset(Dataset):
    """
    Lightweight dataset that reads files via CDN URLs (no HF API/auth).
    Projects each file to {prompt, response} at parse time.
    """
    def __init__(self, manifest_path: str, max_files: int = -1, seed: int = 42):
        self.manifest = load_manifest(manifest_path)
        self.files: List[str] = self.manifest["files"]
        if max_files > 0:
            rng = random.Random(seed)
            self.files = rng.sample(self.files, min(max_files, len(self.files)))
        self.cdn_prefix = self.manifest.get("cdn_prefix", "")

    def __len__(self):
        return len(self.files)

    def _fetch_via_cdn(self, rel_path: str) -> str:
        import urllib.request
        url = self.cdn_prefix + rel_path
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read().decode("utf-8")

    def _project_to_pair(self, text: str) -> Dict[str, str]:
        """
        Heuristic projection to {prompt, response}.
        Replace with your domain-specific parser.
        """
        parts = text.split("\n\n", 1)
        if len(parts) == 2:
            return {"prompt": parts[0].strip(), "response": parts[1].strip()}
        return {"prompt": "", "response": text.strip()}

    def __getitem__(self, idx):
        rel_path = self.files[idx]
        raw = self._fetch_via_cdn(rel_path)
        pair = self._project_to_pair(raw)
        return pair

class SimpleSurrogateModule(L.LightningModule if L else object):
    def __init__(self, manifest_path: str, batch_size: int = 8, max_files: int = -1):
        super().__init__()
        self.save_hyperparameters()
        self.manifest_path = manifest_path
        self.batch_size = batch_size
        self.max_files = max_files
        # tiny model for demo; replace with surrogate-1 architecture
        self.model = torch.nn.Linear(1024, 1024)

    def train_dataloader(self):
        dataset = CDNTextDataset(self.manifest_path, max_files=self.max_files)
        return DataLoader(dataset, batch_size=self.batch_size, shuffle=True, num_workers=0)

    def training_step(self, batch, batch_idx):
        # Minimal training step placeholder
        prompts = batch["prompt"]
        # tokenize -> model -> loss (replace with real logic)
        dummy = torch.randn(len(prompts), 1024)
        loss = torch.nn.functional.mse_loss(self.model(dummy), dummy)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-4)

if __name__ == "__main__":
    # Example local validation run (CPU only, small subset)
    manifest = "

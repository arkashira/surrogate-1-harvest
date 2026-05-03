# vanguard / backend

## Final Synthesized Implementation

**Core diagnosis (unified):**  
- No persisted `(repo, dateFolder) → file-list` manifest → repeated authenticated `list_repo_tree` → quota burn + 429 risk.  
- Training uses HF API/`load_dataset`/streaming on heterogeneous repos → schema/`pyarrow.CastError` risk.  
- No CDN-only data path → unnecessary authenticated calls during training.  
- No Lightning Studio reuse policy → quota waste.  
- No idle-stop resilience → training killed by timeout without restart.

**Resolution priorities:**  
1. Correctness: manifest-driven, CDN-only fetches, schema normalization at parse time.  
2. Actionability: minimal, deterministic scripts with clear run order and failure modes.  
3. Efficiency: reuse running Studio; idle-stop guard; L40S priority, free-tier fallback.

---

### Files to add/modify
- `/opt/axentx/vanguard/backend/generate_manifest.py` (new)  
- `/opt/axentx/vanguard/backend/train.py` (new)  
- `/opt/axentx/vanguard/backend/launch_studio.py` (new)  
- `/opt/axentx/vanguard/backend/requirements.txt` (new)  
- Update runbook/README with run order and env vars.

---

### 1) generate_manifest.py
```bash
mkdir -p /opt/axentx/vanguard/backend
```

```python
#!/usr/bin/env python3
"""
Generate and persist (repo, dateFolder) -> file-list manifest.
Run from any HF-authenticated env (e.g., after rate-limit window clears).
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_REPO")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
OUT_DIR = Path(__file__).parent
MANIFEST_PATH = OUT_DIR / "manifest.json"

def main() -> None:
    if not HF_REPO:
        print("Error: set HF_REPO env var (e.g. datasets/your-org/your-repo).", file=sys.stderr)
        sys.exit(1)

    api = HfApi()
    tree = api.list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_FOLDER,
        repo_type="dataset",
        recursive=False,
    )

    files = sorted(e.path for e in tree if e.type == "file")

    manifest = {}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text())

    key = f"{HF_REPO}::{DATE_FOLDER}"
    manifest[key] = {
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "files": files,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest updated: {MANIFEST_PATH}")
    print(f"Key: {key}, files: {len(files)}")

if __name__ == "__main__":
    main()
```

---

### 2) train.py
```python
#!/usr/bin/env python3
"""
Lightning-compatible training script using CDN-only fetches.
Embed manifest produced by generate_manifest.py to avoid HF API calls during training.
"""
import json
import os
import random
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List

import requests
import torch
from torch.utils.data import Dataset, DataLoader
from lightning import LightningModule, Trainer

MANIFEST_PATH = Path(__file__).parent / "manifest.json"
HF_DATASETS_BASE = "https://huggingface.co/datasets"

class CDNTextDataset(Dataset):
    def __init__(self, repo: str, files: List[str], max_samples: int = 50_000):
        self.repo = repo
        self.files = files
        self.max_samples = max_samples
        self.cache_dir = Path.home() / ".cache" / "vanguard_cdn"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _fetch_via_cdn(self, file_path: str) -> str:
        url = f"{HF_DATASETS_BASE}/{self.repo}/resolve/main/{file_path}"
        # deterministic local name
        safe_name = file_path.replace("/", "_")
        out_path = self.cache_dir / safe_name
        if out_path.exists():
            return out_path.read_text(encoding="utf-8")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        out_path.write_bytes(resp.content)
        return resp.text

    def _parse_to_pair(self, text: str) -> Dict[str, str]:
        # Best-effort normalization for heterogeneous files.
        # Customize per your schema.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return {"prompt": "", "response": ""}
        if len(lines) == 1:
            return {"prompt": lines[0], "response": ""}
        return {"prompt": lines[0], "response": " ".join(lines[1:])}

    def __len__(self) -> int:
        return min(len(self.files), self.max_samples)

    def __getitem__(self, idx: int) -> Dict[str, str]:
        file_path = self.files[idx % len(self.files)]
        raw = self._fetch_via_cdn(file_path)
        pair = self._parse_to_pair(raw)
        return {"prompt": pair["prompt"], "response": pair["response"]}

class SurrogateCollator:
    def __call__(self, batch):
        prompts = [item["prompt"] for item in batch]
        responses = [item["response"] for item in batch]
        return {"prompts": prompts, "responses": responses}

class SurrogateModel(LightningModule):
    def __init__(self, lr: float = 1e-4):
        super().__init__()
        self.lr = lr
        # Replace with your actual model
        self.model = torch.nn.Linear(10, 10)  # placeholder

    def training_step(self, batch, batch_idx):
        loss = torch.tensor(0.0, requires_grad=True)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(), lr=self.lr)

def load_manifest() -> Dict:
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Manifest not found: {MANIFEST_PATH}. Run generate_manifest.py first.")
    return json.loads(MANIFEST_PATH.read_text())

def pick_latest_manifest(manifest: Dict) -> Dict:
    latest_key = max(manifest.keys(), key=lambda k: manifest[k]["generated_at"])
    return manifest[latest_key]

def main() -> None:
    manifest = load_manifest()
    entry = pick_latest_manifest(manifest)

    repo = entry["repo"]
    files = entry["files"]
    if not files:
        print("No files in manifest; aborting.")
        return

    random.shuffle(files)
    split = int(0.95 * len(files))
    train_files, val_files = files[:split], files[split:]

    train_ds = CDNTextDataset(repo=repo, files=train_files)
    val_ds = CDNTextDataset(repo=repo, files=val_files)

    train_loader = DataLoader(
        train_ds,
        batch_size=8,
        shuffle=True,
        num_workers=0,
        collate_fn=SurrogateCollator(),
    )
    val_loader = DataLoader(
        val_ds,
        batch_size=8,
        shuffle=False,
        num_workers=0,
        collate_fn=SurrogateCollator(),
    )

    model = SurrogateModel()
    trainer = Trainer(
        max_epochs=1,
        accelerator="auto",
        devices="auto",
        enable_checkpointing=False,
        logger=False,
    )
    trainer.fit(model, train_loader, val_loader)

if __name__ == "__main__":
    main()
```

---

### 3) launch_studio.py
```python
#!/usr/bin/env python3
"""

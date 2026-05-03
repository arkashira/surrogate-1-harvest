# vanguard / quality

## Final Synthesized Implementation

### 1. Diagnosis (merged)
- **No file-list cache**: every training run re-authenticates and paginates `list_repo_tree`, burning HF API quota and risking 429s.
- **No CDN bypass**: training uses `load_dataset(streaming=True)` or per-file auth calls instead of CDN-only fetches.
- **Commit-cap risk**: all writes target one repo (128 writes/hour cap).
- **Studio reuse missing**: idle stop kills training and wastes quota.
- **Wrapper hygiene missing**: cron-launched helpers lack shebang/executable bit.

### 2. Chosen solution (merged + corrected)
Introduce a CDN-only training launcher that:
- Persists `(repo, date_folder) → file-list` JSON after a single authenticated list call.
- Embeds that list so Lightning workers fetch via `https://huggingface.co/datasets/.../resolve/main/...` (no auth, no API).
- Adds deterministic repo-selector by hash-slug to spread writes across 5 sibling repos.
- Adds Studio reuse + idle-restart guard with fallback to local LightningCLI.
- Adds cron-safe wrapper scripts with shebang, `chmod +x`, and idempotent directory creation.

**Corrections applied**:
- Use `datasets/.../resolve/main/...` for CDN (not `models/.../resolve/...`).
- Keep `hf_hub_download` fallback with token only when CDN fails.
- Fix Studio guard to avoid hard crash when Studio unavailable; fallback to local `LightningCLI`.
- Remove recursive tree listing by default; allow opt-in recursion with pagination guard.
- Add schema-safe parse hook placeholder and schema registry path.

---

### 3. Files

#### `/opt/axentx/vanguard/list_and_cache.py`
```python
#!/usr/bin/env python3
"""
Single-shot authenticated file-lister for (repo, date_folder).
Produces: file_list_{repo_slug}_{date_folder}.json
"""
import os, json, sys
from huggingface_hub import HfApi

HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

api = HfApi(token=HF_TOKEN)

def list_and_cache(repo_id: str, date_folder: str, out_dir: str = ".", recursive: bool = False):
    entries = api.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=recursive)
    files = sorted(e.path for e in entries if e.type == "file")
    slug = repo_id.replace("/", "_")
    out_path = os.path.join(out_dir, f"file_list_{slug}_{date_folder}.json")
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"repo_id": repo_id, "date_folder": date_folder, "files": files}, f, indent=2)
    print(out_path)
    return out_path

if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        print("Usage: list_and_cache.py <repo_id> <date_folder> [--recursive]")
        sys.exit(1)
    rec = "--recursive" in sys.argv
    list_and_cache(sys.argv[1], sys.argv[2], recursive=rec)
```

#### `/opt/axentx/vanguard/train_cdn.py`
```python
#!/usr/bin/env python3
"""
Lightning training using CDN-only file fetches.
Expects file_list_{repo}_{folder}.json produced by list_and_cache.py.
"""
import os, json, hashlib, sys
from pathlib import Path
from typing import Dict, Any

import torch
from torch.utils.data import IterableDataset, DataLoader
import lightning as L
from huggingface_hub import hf_hub_download
import requests

HF_TOKEN = os.getenv("HF_TOKEN", None)  # optional for CDN; kept for fallback

def pick_repo_by_slug(slug: str, siblings: int = 5) -> int:
    """Deterministic sibling index for commit-cap spreading."""
    h = hashlib.sha256(slug.encode()).hexdigest()
    return int(h, 16) % siblings

def parse_record(path: str, content: bytes) -> Dict[str, Any]:
    """
    Project raw file into {prompt,response}.
    Replace with real schema-aware parser or registry lookup.
    """
    # Placeholder: naive split on first newline for demo
    text = content.decode(errors="replace")
    parts = text.split("\n", 1)
    prompt = parts[0].strip()
    response = parts[1].strip() if len(parts) > 1 else ""
    return {"prompt": prompt, "response": response, "source_path": path}

class CDNTextDataset(IterableDataset):
    def __init__(self, file_list_path: str, repo_id: str, date_folder: str, max_files: int = None):
        with open(file_list_path) as f:
            manifest = json.load(f)
        self.files = manifest["files"]
        if max_files:
            self.files = self.files[:max_files]
        self.repo_id = repo_id
        self.date_folder = date_folder

    def _fetch(self, fname: str) -> bytes:
        # CDN bypass: no Authorization header
        url = f"https://huggingface.co/datasets/{self.repo_id}/resolve/main/{fname}"
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            return r.content
        except Exception:
            local = hf_hub_download(repo_id=self.repo_id, filename=fname, token=HF_TOKEN)
            with open(local, "rb") as f:
                return f.read()

    def __iter__(self):
        for fname in self.files:
            content = self._fetch(fname)
            yield parse_record(fname, content)

class SurrogateDataModule(L.LightningDataModule):
    def __init__(self, file_list_path: str, repo_id: str, date_folder: str, batch_size: int = 8):
        super().__init__()
        self.file_list_path = file_list_path
        self.repo_id = repo_id
        self.date_folder = date_folder
        self.batch_size = batch_size

    def train_dataloader(self):
        ds = CDNTextDataset(self.file_list_path, self.repo_id, self.date_folder)
        return DataLoader(ds, batch_size=self.batch_size, num_workers=0)

class SurrogateModel(L.LightningModule):
    def __init__(self, lr: float = 1e-4):
        super().__init__()
        self.save_hyperparameters()
        self.net = torch.nn.Linear(1024, 512)  # placeholder

    def training_step(self, batch, batch_idx):
        # Replace with real forward + loss
        loss = torch.tensor(0.0, requires_grad=True)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.hparams.lr)

def run_training(
    repo_id: str,
    date_folder: str,
    list_dir: str = ".",
    max_files: int = None,
    machine: str = "gpu-l40s",
):
    slug = repo_id.replace("/", "_")
    list_path = os.path.join(list_dir, f"file_list_{slug}_{date_folder}.json")
    if not os.path.exists(list_path):
        print(f"Missing {list_path}. Run list_and_cache.py first.", file=sys.stderr)
        sys.exit(1)

    # Studio reuse + idle-restart guard with safe fallback
    try:
        from lightning.pytorch.studio import Studio, Teamspace
        studios = Teamspace.studios()
        target = None
        for s in studios:
            if s.name == f"vanguard-{date_folder}" and s.status == "Running":
                target = s
                print(f"Reusing running studio: {s.id}")
                break
        if target is None:
            target = Studio(name=f"vanguard-{date_folder}", create_ok=True, machine=machine)
        if target.status != "Running":
            target.start(machine=machine)

        # Launch via Lightning CLI inside studio (simplified)
        cmd = [
            sys.executable, "-m", "lightning.pytorch.cli",
            "fit",
            "--model", "vanguard.train_cdn.SurrogateModel",
            "--data", "vanguard.train_cdn.SurrogateDataModule",
           

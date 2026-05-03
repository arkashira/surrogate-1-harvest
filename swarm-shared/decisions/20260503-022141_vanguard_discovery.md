# vanguard / discovery

## Final Synthesized Solution

**Core diagnosis (agreed across candidates):**  
- No `(repo, dateFolder) → file-list` manifest exists, so every training run triggers authenticated `list_repo_tree`, burning HF API quota and risking 429s.  
- Training likely uses authenticated per-file fetches or `load_dataset(streaming=True)` on heterogeneous schemas, causing `pyarrow.CastError` and wasted API calls.  
- No CDN-only data path: authenticated API calls during training are unnecessary when public CDN URLs bypass auth and rate limits.  
- No reuse guard for Lightning Studio: training loops likely recreate studios, burning quota.  
- No idle-stop resilience: Lightning idle timeout kills training; no pre-run status check or auto-restart.

**Chosen approach:**  
Add a discovery/manifest layer and a Lightning launcher that:  
- Creates a one-time Mac script to list a repo/date folder via `list_repo_tree`, saves `manifests/{repo}/{date}.json`, and exits (no training).  
- Provides a Lightning training script that reads the manifest and downloads via CDN URLs only (zero HF API calls during data load).  
- Provides Mac-side orchestration that reuses a running studio or starts one (L40S priority), then calls `train.py` inside it.  
- Provides a small utility to check studio status and restart if stopped (idle-timeout resilience).

Scope: new files only; no changes to existing code.

---

### Directory setup
```bash
mkdir -p /opt/axentx/vanguard/{discovery,training,orchestration,manifests}
```

---

### discovery/build_manifest.py
```python
#!/usr/bin/env python3
"""
Mac-side one-off: build (repo, dateFolder) -> file-list manifest.
Run: python3 build_manifest.py --repo datasets/myrepo --date 2026-05-03
"""
import argparse
import json
import os
import sys
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/myrepo)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    args = parser.parse_args()

    # Single non-recursive call per date folder (avoids 100x pagination)
    items = list_repo_tree(repo_id=args.repo, path=args.date, recursive=False)
    files = [it.rfilename for it in items if it.type == "file"]

    out_dir = Path(__file__).resolve().parent.parent / "manifests" / args.repo.replace("/", "_")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.date}.json"
    out_path.write_text(json.dumps({"repo": args.repo, "date": args.date, "files": files}, indent=2))
    print(f"Manifest written: {out_path} ({len(files)} files)")

if __name__ == "__main__":
    main()
```

---

### training/train.py
```python
#!/usr/bin/env python3
"""
Lightning training script: CDN-only data loading (zero HF API calls).
Expects manifest at manifests/{repo_slug}/{date}.json
"""
import argparse
import json
import sys
from pathlib import Path
from typing import List

try:
    import lightning as L
    import requests
    import torch
    from torch.utils.data import Dataset, DataLoader
except ImportError:
    print("Install: pip install lightning torch requests")
    sys.exit(1)

HF_CDN = "https://huggingface.co/datasets"

class CDNTextDataset(Dataset):
    def __init__(self, repo: str, files: List[str], max_files: int = None):
        self.repo = repo
        self.files = files[:max_files] if max_files else files

    def __len__(self) -> int:
        return len(self.files)

    def __getitem__(self, idx: int):
        url = f"{HF_CDN}/{self.repo}/resolve/main/{self.files[idx]}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        # Lightweight projection: treat raw text as prompt/response placeholder
        text = resp.text.strip()
        # Replace with real parser (e.g. JSONL -> {prompt,response}) as needed
        return {"text": text}

class SimpleModel(L.LightningModule):
    def __init__(self):
        super().__init__()
        self.layer = torch.nn.Linear(768, 768)

    def training_step(self, batch, batch_idx):
        # Placeholder training op
        x = torch.randn(len(batch["text"]), 768)
        loss = self.layer(x).sum() * 0.0
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-4)

def run(manifest_path: str, max_files: int = 64, batch_size: int = 8, max_epochs: int = 1):
    manifest = json.loads(Path(manifest_path).read_text())
    repo = manifest["repo"]
    files = manifest["files"]

    dataset = CDNTextDataset(repo=repo, files=files, max_files=max_files)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True)

    model = SimpleModel()
    trainer = L.Trainer(max_epochs=max_epochs, accelerator="gpu", devices=1)
    trainer.fit(model, loader)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--max-files", type=int, default=64)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--epochs", type=int, default=1)
    args = ap.parse_args()
    run(manifest_path=args.manifest, max_files=args.max_files, batch_size=args.batch_size, max_epochs=args.epochs)
```

---

### orchestration/launch_studio.py
```python
#!/usr/bin/env python3
"""
Mac orchestration: reuse or start a Lightning Studio, then run training.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

try:
    import lightning as L
    from lightning.pytorch.studio import Studio
except ImportError:
    print("Install: pip install lightning")
    sys.exit(1)

STUDIO_NAME = "vanguard-train-studio"

def launch_and_train(manifest: str, machine: str = "L40S"):
    # Reuse if running
    studio = None
    for s in Studio.list():
        if s.name == STUDIO_NAME and s.status == "running":
            print(f"Reusing running studio: {s.name}")
            studio = s
            break

    if studio is None:
        print(f"Creating studio: {STUDIO_NAME}")
        studio = Studio(
            name=STUDIO_NAME,
            create_ok=True,
            machine=machine,
        )

    # Build command to run inside studio
    cmd = [
        "python3", "train.py",
        "--manifest", manifest,
        "--max-files", "128",
        "--epochs", "1"
    ]

    print(f"Running in studio: {' '.join(cmd)}")
    job = studio.run(cmd)
    print(f"Job submitted: {job}")
    return job

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest file")
    parser.add_argument("--machine", default="L40S", help="Studio machine type")
    args = parser.parse_args()
    launch_and_train(manifest=args.manifest, machine=args.machine)

if __name__ == "__main__":
    main()
```

---

### orchestration/ensure_studio.py
```python
#!/usr/bin/env python3
"""
Utility to check studio status and restart if stopped (idle-timeout resilience).
"""
import sys
import time
from typing import Optional

try:
    from lightning.pytorch.studio import Studio
except ImportError:
    print("Install: pip install lightning")
    sys.exit(1)

STUDIO_NAME = "vanguard-train-studio"

def get

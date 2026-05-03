# airship / frontend

## Highest-value incremental improvement (<2h)

**Goal**: Eliminate HF API 429s during Surrogate training and prevent Lightning idle-stop training loss.

**Chosen deliverable**:  
Add a deterministic CDN file manifest generator + Lightning Studio lifecycle resilience to the Surrogate training pipeline.

- Single Mac-side script to list one date-folder via HF API once, emit `train_files.json`.
- Embed `train_files.json` in training so Lightning workers fetch via CDN only (zero API calls during data load).
- Reuse running Studio; restart automatically if stopped (prevents idle-stop loss).
- Small, safe, and immediately useful for the next training run.

---

## Implementation plan

1. **Add manifest generator** (`scripts/build_cdn_manifest.py`)
   - Uses `list_repo_tree(path, recursive=False)` for one date folder.
   - Emits `train_files.json` with CDN URLs and local basenames.
   - Deterministic sort for reproducibility.

2. **Update training script** (`surrogate/train.py` or equivalent)
   - Accept `--manifest train_files.json`.
   - Build a `IterableDataset` that streams files via `requests.get(cdn_url)` and yields `{prompt, response}`.
   - No `load_dataset` on heterogeneous repo; no HF API calls during training.

3. **Add Lightning launcher** (`scripts/run_lightning_studio.py`)
   - Reuse running Studio by name if exists and is running.
   - If stopped, restart with `L40S` (free-tier compatible).
   - Run training with `--manifest`.

4. **Smoke test**
   - Run manifest build locally.
   - Validate one CDN download works.
   - Launch studio and run one training step.

---

## Code snippets

### 1) Manifest generator (Mac orchestration)

```python
# scripts/build_cdn_manifest.py
#!/usr/bin/env python3
"""
Build deterministic CDN manifest for one date folder.
Usage:
  HF_REPO="datasets/yourorg/surrogate-mirror" \
  DATE_FOLDER="batches/mirror-merged/2026-05-03" \
  python scripts/build_cdn_manifest.py --out train_files.json
"""
import argparse
import json
import os
from typing import List, Dict

from huggingface_hub import HfApi

HF_API = HfApi()
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, folder: str) -> List[Dict[str, str]]:
    entries = HF_API.list_repo_tree(repo=repo, path=folder, recursive=False)
    files = [e for e in entries if e.type == "file"]
    items = []
    for f in sorted(files, key=lambda x: x.path):
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=f.path)
        items.append({
            "cdn_url": cdn_url,
            "path": f.path,
            "basename": os.path.basename(f.path),
        })
    return items

def main() -> None:
    parser = argparse.ArgumentParser(description="Build CDN manifest for Surrogate training.")
    parser.add_argument("--repo", default=os.getenv("HF_REPO"), help="HF dataset repo (e.g. datasets/yourorg/surrogate-mirror)")
    parser.add_argument("--folder", default=os.getenv("DATE_FOLDER"), help="Folder path inside repo (e.g. batches/mirror-merged/2026-05-03)")
    parser.add_argument("--out", default="train_files.json", help="Output JSON path")
    args = parser.parse_args()

    if not args.repo or not args.folder:
        parser.error("Provide --repo and --folder or set HF_REPO / DATE_FOLDER")

    manifest = build_manifest(args.repo, args.folder)
    with open(args.out, "w") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Wrote {len(manifest)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x scripts/build_cdn_manifest.py
```

---

### 2) Lightweight CDN dataset (used in training)

```python
# surrogate/data/cdn_dataset.py
import json
import requests
from torch.utils.data import IterableDataset

class CDNJsonlDataset(IterableDataset):
    """
    Stream JSONL files from CDN URLs listed in manifest.
    Each line must be JSON with at least {"prompt": "...", "response": "..."}.
    """
    def __init__(self, manifest_path: str):
        with open(manifest_path) as f:
            self.files = json.load(f)

    def __iter__(self):
        for item in self.files:
            cdn_url = item["cdn_url"]
            resp = requests.get(cdn_url, timeout=60)
            resp.raise_for_status()
            for line in resp.text.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                # Project to {prompt, response} only
                yield {
                    "prompt": obj["prompt"],
                    "response": obj["response"],
                }
```

In `train.py`:

```python
from surrogate.data.cdn_dataset import CDNJsonlDataset

def train(cfg):
    dataset = CDNJsonlDataset(cfg.manifest_path)
    # ... continue with dataloader / trainer
```

---

### 3) Lightning Studio launcher with reuse + idle-stop resilience

```python
# scripts/run_lightning_studio.py
#!/usr/bin/env python3
"""
Launch Surrogate training in Lightning Studio with reuse + restart resilience.
"""
import os
import time
from lightning import LightningWork, LightningFlow, LightningApp, Machine
from lightning.fabric.utilities.cloud_io import get_filesystem

# Minimal Work that runs training command
class SurrogateTrainWork(LightningWork):
    def __init__(self, manifest_path: str, **kwargs):
        super().__init__(**kwargs)
        self.manifest_path = manifest_path

    def run(self):
        import subprocess
        cmd = [
            "python", "surrogate/train.py",
            "--manifest", self.manifest_path,
        ]
        subprocess.run(cmd, check=True)

# Flow to manage lifecycle
class SurrogateFlow(LightningFlow):
    def __init__(self):
        super().__init__()
        self.train_work = SurrogateTrainWork(
            manifest_path=os.getenv("MANIFEST_PATH", "train_files.json"),
            cloud_compute=Machine("L40S"),  # free-tier friendly
        )

    def configure_layout(self):
        return [{"name": "train", "content": self.train_work}]

    def run(self):
        # Reuse if already running; if stopped, restart
        if not self.train_work.has_started:
            self.train_work.start()
        elif self.train_work.status == "stopped":
            # idle-stop killed training -> restart
            self.train_work.start(machine=Machine("L40S"))

if __name__ == "__main__":
    # Studio reuse: list existing and attach if running
    from lightning import Teamspace
    studio_name = "surrogate-training"
    existing = [s for s in Teamspace.studios if s.name == studio_name and s.status == "Running"]
    if existing:
        print(f"Reusing running studio: {studio_name}")
        # Attach logic depends on local run vs cloud; for CLI we just run the app
        app = LightningApp(SurrogateFlow())
    else:
        print(f"Starting new studio: {studio_name}")
        app = LightningApp(SurrogateFlow())
```

Make executable:

```bash
chmod +x scripts/run_lightning_studio.py
```

---

### 4) Quick smoke test

```bash
# 1) Build manifest (run once per date folder)
HF_REPO="datasets/yourorg/surrogate-mirror" \
DATE_FOLDER="batches/mirror-merged/2026-05-03" \
python scripts/build_cdn_manifest.py --out train_files.json

# 2) Validate one CDN fetch
head -1 train_files.json | jq -r .cdn_url | xargs curl -sI | head -5

# 3) Run training locally (or via studio)
MANIFEST_PATH=train_files.json python surrogate/train.py --epochs 1
```

---

## Expected outcome

- HF API used only once per date folder (no

# vanguard / discovery

## 1. Diagnosis
- No durable ingestion manifest: every training run re-lists HF repos via API, causing 429s and quota burn.
- Training uses `load_dataset`/`list_repo_files` instead of CDN bypass → guaranteed rate limits during data loading.
- No reuse guard for Lightning Studio: scripts create new studios instead of reusing running ones, wasting 80+ hrs/mo quota.
- Missing idle-stop resilience: Lightning idle timeout kills training; no pre-run status check or auto-restart.
- No single source of truth for file list per date folder: forces repeated API pagination on every run.

## 2. Proposed change
Add two small, high-leverage files under `/opt/axentx/vanguard/`:
- `scripts/build_manifest.py` — one-time Mac-side HF API call to list a date folder and emit `manifests/{date}.json` (CDN paths only).
- `training/train.py` — lightweight Lightning training stub that:
  - reuses a running Studio by name,
  - loads file list from the embedded manifest (CDN-only fetches),
  - checks studio status and auto-restarts if stopped,
  - streams data via `datasets` using `http://` CDN URLs (zero API calls during training).

Scope: ~120 LoC total; no changes to existing repo files.

## 3. Implementation

```bash
# /opt/axentx/vanguard/scripts/build_manifest.py
#!/usr/bin/env python3
"""
Usage (Mac, after rate-limit window):
  HF_REPO=datasets/your/repo python3 scripts/build_manifest.py --date 2026-05-02

Produces:
  manifests/2026-05-02.json  -> {"files":["https://huggingface.co/datasets/...", ...]}
"""
import argparse
import json
import os
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_REPO")
OUT_DIR = Path(__file__).parent.parent / "manifests"
OUT_DIR.mkdir(exist_ok=True)

def main(date: str):
    if not HF_REPO:
        raise RuntimeError("Set HF_REPO=datasets/owner/repo")
    api = HfApi()
    # Single non-recursive call per date folder (avoids 100x pagination)
    entries = api.list_repo_tree(repo_id=HF_REPO, path=date, recursive=False)
    files = []
    for e in entries:
        if e.path.endswith((".parquet", ".jsonl", ".json")):
            cdn = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{e.path}"
            files.append(cdn)
    out = OUT_DIR / f"{date}.json"
    out.write_text(json.dumps({"date": date, "files": files}, indent=2))
    print(f"Wrote {len(files)} CDN paths to {out}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True)
    args = p.parse_args()
    main(args.date)
```

```python
# /opt/axentx/vanguard/training/train.py
"""
Lightning Studio launcher + CDN-only dataset loader.
Run from Mac:
  python3 training/train.py --manifest manifests/2026-05-02.json --name surrogate-1-run
"""
import argparse
import json
from pathlib import Path

import torch
from datasets import load_dataset, DatasetDict
from lightning import Fabric
from lightning.fabric.plugins import LightningStudioPlugin
from lightning.fabric.utilities.studio import Studio, Teamspace

MANIFEST = Path(__file__).parent.parent / "manifests"

def load_cdn_dataset(manifest_path: str):
    meta = json.loads(Path(manifest_path).read_text())
    # Use data_files pointing to CDN URLs -> zero HF API calls during training
    ds = load_dataset("parquet", data_files={"train": meta["files"]}, streaming=True)
    return ds

def reuse_or_create_studio(name: str):
    # Reuse guard: saves quota
    for s in Teamspace().studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return Studio(
        name=name,
        machine="L40S",           # free-tier fallback; switch to H200 in lambda-prod if available
        create_ok=True,
        plugins=[LightningStudioPlugin()],
    )

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", required=True)
    p.add_argument("--name", default="surrogate-1-run")
    args = p.parse_args()

    studio = reuse_or_create_studio(args.name)

    # Idle-stop resilience
    if studio.status != "Running":
        print("Studio stopped; restarting...")
        studio.start(machine="L40S")

    fabric = Fabric(accelerator="cuda", devices=1, precision="bf16-mixed")
    fabric.launch()

    dataset = load_cdn_dataset(args.manifest)

    # Minimal training step placeholder
    model = torch.nn.Linear(1024, 1024)
    model, dataset = fabric.setup(model, dataset)

    for batch in dataset["train"]:
        # project to {prompt, response} at parse time; schema handled here
        x = torch.randn(1, 1024)  # replace with tokenizer(batch["prompt"])
        y = torch.randn(1, 1024)  # replace with tokenizer(batch["response"])
        loss = torch.nn.functional.mse_loss(model(x), y)
        fabric.backward(loss)
        fabric.optimizer_step()
        fabric.log_dict({"loss": loss.item()})

if __name__ == "__main__":
    main()
```

## 4. Verification
1. On Mac (or any dev box with HF token):
   ```bash
   export HF_REPO=datasets/your/repo
   python3 scripts/build_manifest.py --date 2026-05-02
   ```
   Confirm `manifests/2026-05-02.json` exists and contains only CDN `https://huggingface.co/...` URLs.

2. Dry-run dataset load (zero training):
   ```bash
   python3 -c "from training.train import load_cdn_dataset; ds=load_cdn_dataset('manifests/2026-05-02.json'); print(next(iter(ds['train'])))"
   ```
   Should stream a batch without any HF API request (check via network or by revoking token temporarily — CDN still works).

3. Launch training:
   ```bash
   python3 training/train.py --manifest manifests/2026-05-02.json --name surrogate-1-run
   ```
   - Confirm in Lightning UI that the studio is reused if already running.
   - Stop the studio manually, re-run the script: it should detect stopped status and restart automatically.
   - Monitor logs: no `load_dataset` HF API calls; only CDN downloads.

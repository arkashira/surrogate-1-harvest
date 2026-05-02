# vanguard / backend

## 1. Diagnosis
- No durable ingestion manifest: every training run re-lists HF repos via API, causing 429s and quota burn.
- Training data loader uses `load_dataset`/`list_repo_files` (HF API) instead of CDN bypass → guaranteed rate limits during data loading.
- No reuse guard for Lightning Studio: scripts create new studios instead of reusing running ones, wasting ~80hr/mo quota.
- No idle-stop resilience: Lightning idle timeout kills training; no pre-check/restart logic.
- Missing orchestration script shebang/permissions pattern: wrapper scripts risk cron/exec failures (seen in opus-pr-reviewer and active-learning patterns).

## 2. Proposed change
Create `/opt/axentx/vanguard/backend/orchestrate_train.py` (single orchestrator) that:
- Generates a durable `manifests/{date}/file_list.json` via one HF API call (post-rate-limit window).
- Embeds the file list so Lightning training uses CDN-only fetches (zero API calls during data load).
- Reuses a running Lightning Studio or starts one (L40S priority, fallback to public tier).
- Adds idle-stop resilience: checks studio status before `.run()` and restarts if stopped.
- Uses proper shebang, is executable, and is Bash-invoked for cron safety.

## 3. Implementation

```bash
# /opt/axentx/vanguard/backend/orchestrate_train.py
#!/usr/bin/env python3
"""
Orchestrate surrogate-1 training with HF CDN bypass and Lightning Studio reuse.
Run from cron (ensure SHELL=/bin/bash) or manually:
  bash /opt/axentx/vanguard/backend/orchestrate_train.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import lightning as L
from lightning.fabric.utilities.exceptions import FabricException

HF_REPO = os.getenv("HF_REPO", "datasets/axentx/surrogate-1")
MANIFEST_ROOT = Path(__file__).parent.parent / "manifests"
DATE_TAG = datetime.now(timezone.utc).strftime("%Y-%m-%d")
MANIFEST_PATH = MANIFEST_ROOT / DATE_TAG / "file_list.json"
TRAIN_SCRIPT = Path(__file__).parent / "train.py"
STUDIO_NAME = "vanguard-surrogate1-train"
MACHINE = L.Machine.L40S

# ---- HF helpers (lightweight; expect 429 retries) ----
def list_hf_folder(path: str = "", max_retries: int = 5, backoff: int = 360):
    """List one folder non-recursively; retry on 429."""
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("ERROR: huggingface_hub not installed.", file=sys.stderr)
        sys.exit(1)

    api = HfApi()
    for attempt in range(1, max_retries + 1):
        try:
            files = api.list_repo_tree(
                repo_id=HF_REPO,
                path=path,
                repo_type="dataset",
                recursive=False,
            )
            # normalize to relative paths
            out = []
            for f in files:
                if hasattr(f, "path"):
                    out.append(f.path)
                else:
                    out.append(str(f))
            return out
        except Exception as e:
            if "429" in str(e) and attempt < max_retries:
                wait = backoff if attempt == 1 else backoff * attempt
                print(f"HF 429, retry {attempt}/{max_retries} after {wait}s: {e}", file=sys.stderr)
                time.sleep(wait)
                continue
            raise

def build_manifest():
    """Single API call to snapshot file list for today's folder."""
    MANIFEST_ROOT.mkdir(parents=True, exist_ok=True)
    if MANIFEST_PATH.exists():
        print(f"Manifest exists: {MANIFEST_PATH}")
        with open(MANIFEST_PATH) as f:
            return json.load(f)

    # list top-level date folder only (non-recursive)
    items = list_hf_folder(DATE_TAG)
    # keep only parquet files under the date folder
    files = [p for p in items if p.endswith(".parquet") and p.startswith(DATE_TAG)]
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "hf_repo": HF_REPO,
        "date_tag": DATE_TAG,
        "files": sorted(files),
    }
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written: {MANIFEST_PATH} ({len(files)} files)")
    return manifest

# ---- Lightning Studio reuse + resilience ----
def get_or_start_studio():
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == STUDIO_NAME and s.status == "running":
            print(f"Reusing running studio: {s.name}")
            return s

    print(f"No running studio '{STUDIO_NAME}'; starting...")
    # Try preferred clouds in priority order (H200 only in paid tier)
    clouds = [
        L.Cloud.LightningLambdaProd,  # paid tier (H200)
        L.Cloud.LightningPublicProd,  # free tier (L40S max)
    ]
    studio = None
    for cloud in clouds:
        try:
            studio = L.Studio(
                name=STUDIO_NAME,
                cloud=cloud,
                machine=MACHINE,
                create_ok=True,
            )
            print(f"Started studio on {cloud}: {studio.name}")
            return studio
        except FabricException as e:
            print(f"Cloud {cloud} failed: {e}; trying next...")
            continue
    raise RuntimeError("Could not start studio on any cloud.")

def ensure_studio_running(studio):
    if studio.status != "running":
        print(f"Studio stopped (status={studio.status}); restarting...")
        studio.start(machine=MACHINE)
        # wait briefly for running
        for _ in range(10):
            if studio.status == "running":
                break
            time.sleep(6)
    return studio

# ---- Main ----
def main():
    # 1) Build manifest once (post-rate-limit window)
    manifest = build_manifest()
    if not manifest["files"]:
        print("ERROR: No parquet files found for today.", file=sys.stderr)
        sys.exit(1)

    # 2) Reuse or start Lightning Studio
    studio = get_or_start_studio()
    studio = ensure_studio_running(studio)

    # 3) Run training script inside studio (CDN-only data loading)
    # train.py must read MANIFEST_PATH and use direct CDN URLs:
    #   f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{p}"
    cmd = [
        sys.executable,
        str(TRAIN_SCRIPT),
        "--manifest",
        str(MANIFEST_PATH),
    ]
    print(f"Running in studio: {' '.join(cmd)}")
    run = studio.run(cmd, cwd=str(Path(__file__).parent))
    print(f"Run submitted: {run}")

if __name__ == "__main__":
    main()
```

```bash
# /opt/axentx/vanguard/backend/train.py  (minimal CDN loader stub)
#!/usr/bin/env python3
"""
Train surrogate-1 using CDN-only file list (zero HF API calls during data load).
Usage:
  python train.py --manifest manifests/2026-05-02/file_list.json
"""

import argparse
import json
from pathlib import Path

import torch
from torch.utils.data import Dataset, DataLoader

HF_REPO = "datasets/axentx/surrogate-1"
CDN_ROOT = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

class CDNParquetDataset(Dataset):
    def __init__(self, file_urls):
        self.file_urls = file_urls

    def __len__(self):
        return len(self.file_urls)

    def __getitem__(self, idx):
        # In real training, stream/parquet-read from URL (pyarrow + fsspec/http)
        # For now, return URL so training loop confirms CDN usage.
        return {"url": self.file_urls[idx]}

def main():
    parser = argparse.ArgumentParser()
   

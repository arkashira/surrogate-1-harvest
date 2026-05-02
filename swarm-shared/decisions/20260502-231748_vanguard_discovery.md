# vanguard / discovery

## 1. Diagnosis
- No durable ingestion manifest: every training run re-lists HF repos via API, causing 429s and quota burn.
- Training uses `load_dataset`/`list_repo_files` instead of CDN bypass → guaranteed rate limits during data loading.
- No reuse guard for Lightning Studio: scripts create new studios instead of reusing running ones, wasting 80+ hrs/mo quota.
- Missing pre-flight check for idle-stopped studios: training dies when idle timeout kills the studio.
- No single source-of-truth file list for a given date folder: forces repeated API walks across training iterations.

## 2. Proposed change
Create `/opt/axentx/vanguard/discovery/manifest.py` + `/opt/axentx/vanguard/discovery/train_launcher.py`  
- `manifest.py`: one API call to `list_repo_tree` for a date folder → writes `manifest-{date}.json` (CDN paths only).  
- `train_launcher.py`: reads manifest, reuses running Lightning Studio, starts if idle-stopped, launches training with CDN-only fetches.

## 3. Implementation

```bash
# Ensure directory exists
mkdir -p /opt/axentx/vanguard/discovery
```

`/opt/axentx/vanguard/discovery/manifest.py`
```python
#!/usr/bin/env python3
"""
Generate durable manifest for a date folder to avoid HF API rate limits.
Usage:
  python manifest.py --repo datasets/mycorp/surrogate-1 --date 2026-04-29 --out manifest-2026-04-29.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

CDN_BASE = "https://huggingface.co/datasets"

def build_manifest(repo: str, date_folder: str, output_path: str):
    api = HfApi()
    # Single API call: non-recursive listing for the date folder
    entries = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)

    files = []
    for e in entries:
        if e.type != "file":
            continue
        # Only include parquet (or extend as needed)
        if not e.path.lower().endswith(".parquet"):
            continue
        cdn_url = f"{CDN_BASE}/{repo}/resolve/main/{e.path}"
        files.append({
            "path": e.path,
            "cdn_url": cdn_url,
            "size": getattr(e, "size", None),
            "lfs": getattr(e, "lfs", None)
        })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "count": len(files),
        "files": files
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Manifest written: {output_path} ({len(files)} files)")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate CDN manifest for HF dataset date folder")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., datasets/mycorp/surrogate-1)")
    parser.add_argument("--date", required=True, help="Date folder (e.g., 2026-04-29)")
    parser.add_argument("--out", default="manifest.json", help="Output JSON path")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, args.out)
```

`/opt/axentx/vanguard/discovery/train_launcher.py`
```python
#!/usr/bin/env python3
"""
Lightning Studio launcher that reuses running studios and uses CDN-only data loading.
Usage:
  python train_launcher.py --manifest manifest-2026-04-29.json --script train.py --name surrogate-l40s-run
"""
import argparse
import json
import subprocess
import sys
import time

try:
    from lightning import Studio, Teamspace, Machine
except ImportError:
    print("Install: pip install lightning")
    sys.exit(1)

def find_running_studio(name: str):
    try:
        for s in Teamspace.studios:
            if s.name == name and s.status == "Running":
                return s
    except Exception:
        pass
    return None

def ensure_studio(name: str, machine="lightning-public-prod"):
    studio = find_running_studio(name)
    if studio:
        print(f"Reusing running studio: {name}")
        return studio

    print(f"Starting new studio: {name} on {machine}")
    # start with small public machine; switch to L40S/H200 if quota allows
    studio = Studio(
        name=name,
        machine=Machine(machine),
        create_ok=True
    )
    return studio

def wait_for_ready(studio, timeout=300, interval=15):
    elapsed = 0
    while elapsed < timeout:
        try:
            studio.refresh()
            if studio.status == "Running":
                print("Studio is running")
                return True
        except Exception:
            pass
        print(f"Waiting for studio... ({elapsed}s)")
        time.sleep(interval)
        elapsed += interval
    return False

def run_training(studio, manifest_path, train_script):
    with open(manifest_path) as f:
        manifest = json.load(f)

    # Pass manifest to training via env var or CLI arg
    cmd = [
        "python", train_script,
        "--manifest", manifest_path
    ]

    print(f"Running on studio: {' '.join(cmd)}")
    # Studio.run executes inside the studio environment
    job = studio.run(cmd, cwd="/workspace")
    return job

def main():
    parser = argparse.ArgumentParser(description="Launcher with studio reuse + CDN manifest")
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    parser.add_argument("--script", default="train.py", help="Training script")
    parser.add_argument("--name", default="vanguard-train", help="Studio name")
    parser.add_argument("--machine", default="lightning-public-prod", help="Machine type")
    args = parser.parse_args()

    if not os.path.isfile(args.manifest):
        print(f"Manifest not found: {args.manifest}")
        sys.exit(1)

    studio = ensure_studio(args.name, args.machine)
    if not wait_for_ready(studio):
        print("Studio failed to become ready")
        sys.exit(1)

    try:
        run_training(studio, args.manifest, args.script)
    except Exception as e:
        print(f"Training launch failed: {e}")
        # If studio stopped (idle timeout), try restart once
        studio.refresh()
        if studio.status != "Running":
            print("Studio stopped; restarting...")
            studio.start(machine=Machine(args.machine))
            if wait_for_ready(studio):
                run_training(studio, args.manifest, args.script)
            else:
                print("Restart failed")
        else:
            raise

if __name__ == "__main__":
    main()
```

`/opt/axentx/vanguard/discovery/train.py` (minimal CDN-only loader example)
```python
#!/usr/bin/env python3
"""
Example training script that loads from CDN using manifest (no HF API during training).
"""
import argparse
import json
import pyarrow.parquet as pq
import requests
from io import BytesIO

def load_parquet_from_cdn(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return pq.read_table(BytesIO(r.content))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()

    with open(args.manifest) as f:
        manifest = json.load(f)

    print(f"Loading {manifest['count']} files from CDN...")
    tables = []
    for fobj in manifest["files"]:
        tbl = load_parquet_from_cdn(fobj["cdn_url"])
        # Project to {prompt, response} here if needed
        tables.append(tbl)

    # Concat and continue training...
    print("

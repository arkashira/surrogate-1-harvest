# airship / discovery

## Highest-Value Incremental Improvement (<2h)
**Ship:** Deterministic CDN file manifest generator + Lightning Studio lifecycle resilience for Surrogate training.

**Why:**  
- Eliminates HF API 429s during Surrogate training by pre-listing once and using CDN-only fetches.  
- Prevents idle-stop training loss by checking studio status and auto-restarting before each run.  
- Fits within 2h: small, focused scripts + config changes; no schema refactor or new infra.

---

## Implementation Plan

1. **Add manifest generator** (`scripts/generate_cdn_manifest.py`)  
   - Uses HF `list_repo_tree` (non-recursive per date folder) on Mac.  
   - Outputs `manifests/surrogate_training_manifest_YYYYMMDD.json` with CDN URLs and local paths.  
   - Embeddable in training script; zero API calls during Lightning training.

2. **Add Lightning Studio lifecycle wrapper** (`scripts/run_surrogate_training.py`)  
   - Checks `Teamspace.studios` for existing running studio; reuses if found.  
   - If stopped, restarts with `target.start(machine=Machine.L40S)` (respects free-tier fallback).  
   - Runs training script with `--manifest` arg pointing to generated manifest.

3. **Update training script** (`surrogate/train.py`)  
   - Accepts `--manifest` and loads file list.  
   - Uses `hf_hub_download` or raw CDN URLs (no `load_dataset`).  
   - Projects to `{prompt, response}` only at parse time; no schema mutation.

4. **Add cron-safe invocation** (`scripts/cron_train_wrapper.sh`)  
   - Bash shebang, `chmod +x`, sets `SHELL=/bin/bash`.  
   - Invokes `run_surrogate_training.py` with proper args and logging.

5. **Smoke test**  
   - Generate manifest locally.  
   - Run training in Lightning Studio (reuse or start).  
   - Verify no HF API calls during data load (check logs for CDN fetches).

---

## Code Snippets

### 1. `scripts/generate_cdn_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate CDN-only manifest for Surrogate training.
Run on Mac (or any dev machine) after HF API rate-limit window clears.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from huggingface_hub import HfApi

REPO_ID = "axentx/surrogate-dataset"  # adjust if needed
DATE_FOLDER = datetime.utcnow().strftime("%Y%m%d")  # or pass as arg
OUTPUT_DIR = Path(__file__).parent.parent / "manifests"
OUTPUT_DIR.mkdir(exist_ok=True)

def main():
    api = HfApi()
    # Non-recursive per date folder to avoid pagination explosion
    try:
        items = api.list_repo_tree(
            repo_id=REPO_ID,
            path=DATE_FOLDER,
            recursive=False,
        )
    except Exception as e:
        print(f"HF API error: {e}", file=sys.stderr)
        sys.exit(1)

    files = [it.rfilename for it in items if it.rfilename]
    manifest = {
        "repo_id": REPO_ID,
        "date_folder": DATE_FOLDER,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "files": [],
    }

    for f in sorted(files):
        cdn_url = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{DATE_FOLDER}/{f}"
        manifest["files"].append({
            "path": f"{DATE_FOLDER}/{f}",
            "cdn_url": cdn_url,
            "local_name": os.path.basename(f),
        })

    out_path = OUTPUT_DIR / f"surrogate_training_manifest_{DATE_FOLDER}.json"
    with open(out_path, "w") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Manifest written to {out_path}")
    print(f"Total files: {len(files)}")

if __name__ == "__main__":
    main()
```

### 2. `scripts/run_surrogate_training.py`
```python
#!/usr/bin/env python3
"""
Lightning Studio lifecycle-aware training launcher.
Reuses running studios and restarts stopped ones before training.
"""
import argparse
import subprocess
import sys

from lightning_sdk import Studio, Machine, Teamspace

STUDIO_NAME = "surrogate-training-studio"
MACHINE = Machine.L40S  # free tier falls back to L40S/public-prod if H200 unavailable

def get_or_create_studio():
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == STUDIO_NAME:
            if s.status == "running":
                print(f"Reusing running studio: {STUDIO_NAME}")
                return s
            else:
                print(f"Studio {STUDIO_NAME} is {s.status}. Restarting...")
                # stop if in a stuck state, then start
                try:
                    s.stop()
                except Exception:
                    pass
                return teamspace.create_studio(
                    name=STUDIO_NAME,
                    machine=MACHINE,
                    create_ok=True,
                )
    # not found: create
    print(f"Creating studio {STUDIO_NAME} on {MACHINE}")
    return teamspace.create_studio(
        name=STUDIO_NAME,
        machine=MACHINE,
        create_ok=True,
    )

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to CDN manifest JSON")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--output-dir", default="surrogate/output")
    args = parser.parse_args()

    studio = get_or_create_studio()

    # Ensure running before each run (idle-stop kills training)
    if studio.status != "running":
        print(f"Studio status is {studio.status}. Starting...")
        studio.start(machine=MACHINE)

    # Run training inside studio (lightning run executes remotely)
    cmd = [
        "python", "surrogate/train.py",
        "--manifest", args.manifest,
        "--epochs", str(args.epochs),
        "--output-dir", args.output_dir,
    ]
    print(f"Running in studio: {' '.join(cmd)}")
    result = studio.run(
        command=" ".join(cmd),
        environment={
            "HF_DATASETS_OFFLINE": "1",
            "HF_HUB_OFFLINE": "0",  # allow CDN fetches
        },
    )
    print(f"Training finished with status: {result.status}")
    if result.status != "success":
        sys.exit(1)

if __name__ == "__main__":
    main
```

### 3. `surrogate/train.py` (minimal changes)
```python
import argparse
import json
import os
from pathlib import Path

import requests
from huggingface_hub import hf_hub_download

def load_manifest(manifest_path):
    with open(manifest_path) as f:
        return json.load(f)

def stream_cdn_file(cdn_url, local_cache_dir):
    local_cache_dir = Path(local_cache_dir)
    local_cache_dir.mkdir(parents=True, exist_ok=True)
    fname = cdn_url.split("/")[-1]
    local_path = local_cache_dir / fname
    if local_path.exists():
        return local_path
    # CDN fetch (no auth, high rate limit)
    resp = requests.get(cdn_url, stream=True, timeout=60)
    resp.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return local_path

def parse_file_to_example(path):
    # Project to {prompt, response} only at parse time
    # Implement per your file schema (parquet/jsonl/etc.)
    # Example stub:
    return {"prompt": "...", "response": "..."}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    os.makedirs(args.output_dir, exist_ok=True)

    examples = []
    for entry in manifest["

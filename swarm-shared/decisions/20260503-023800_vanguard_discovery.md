# vanguard / discovery

## 1. Diagnosis

- Frontend still likely triggers authenticated HF API calls (`list_repo_tree` / dataset endpoints) at runtime, burning quota and risking 429s on user machines.
- No static file manifest embedded in the bundle → every session re-enumerates the repo instead of using CDN-only fetches.
- Training script probably uses `load_dataset(streaming=True)` on heterogeneous repos, exposing mixed-schema pyarrow errors and redundant API calls during data loading.
- No reuse guard for Lightning Studio → each run may recreate instead of attaching to a running studio, wasting 80+ hours/month of quota.
- Missing CDN-bypass strategy: training should fetch via `https://huggingface.co/datasets/{repo}/resolve/main/...` with zero Authorization headers after a one-time file-list snapshot.

## 2. Proposed change

Create a discovery-time manifest generator and a Lightning launcher that:
- Snapshots one date folder via `list_repo_tree` (non-recursive) → `manifests/{date}/files.json`
- Embeds that manifest in the frontend bundle at build time
- Uses CDN-only URLs for training data fetches
- Reuses a running Lightning Studio instead of recreating

Scope:
- Add `/opt/axentx/vanguard/scripts/build_manifest.py`
- Add `/opt/axentx/vanguard/scripts/launch_studio.py`
- Add `/opt/axentx/vanguard/train.py` (or patch existing) to consume manifest and use CDN URLs
- Add frontend env/config to embed manifest path at build

## 3. Implementation

```bash
# Ensure scripts directory
mkdir -p /opt/axentx/vanguard/scripts /opt/axentx/vanguard/manifests
```

### scripts/build_manifest.py
```python
#!/usr/bin/env python3
"""
Generate a static file manifest for one date folder.
Run from Mac (or CI) after HF API rate-limit window clears.
"""
import json, os, sys
from datetime import datetime, timezone
from huggingface_hub import HfApi

REPO_ID = os.getenv("HF_DATASET_REPO", "axentx/vanguard-data")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
OUT_DIR = os.getenv("OUT_DIR", "manifests")
OUT_FILE = os.path.join(OUT_DIR, DATE_FOLDER, "files.json")

def main() -> None:
    api = HfApi()
    # Non-recursive: one API call, no pagination explosion
    entries = api.list_repo_tree(
        repo_id=REPO_ID,
        path=DATE_FOLDER,
        repo_type="dataset",
        recursive=False,
    )
    files = [e.path for e in entries if e.type == "file"]
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump({"repo": REPO_ID, "date": DATE_FOLDER, "files": files}, f, indent=2)
    print(f"Wrote {len(files)} files -> {OUT_FILE}")

if __name__ == "__main__":
    main()
```

### scripts/launch_studio.py
```python
#!/usr/bin/env python3
"""
Reuse a running Lightning Studio or start one.
Prevents quota waste from repeated creates.
"""
import os, sys
from lightning_sdk import Studio, Machine, Teamspace

STUDIO_NAME = os.getenv("STUDIO_NAME", "vanguard-train")
MACHINE = Machine.L40S  # falls back to public tier if not in paid account

def main() -> None:
    teamspace = Teamspace()
    running = None
    for s in teamspace.studios:
        if s.name == STUDIO_NAME and s.status == "running":
            running = s
            break

    if running:
        print(f"Reusing running studio: {running.id}")
        studio = running
    else:
        print(f"Creating studio: {STUDIO_NAME}")
        studio = Studio.create(name=STUDIO_NAME, machine=MACHINE, create_ok=True)

    # Ensure it's running before submitting work
    if studio.status != "running":
        print("Studio not running; starting...")
        studio.start(machine=MACHINE)

    # Submit training (uses train.py in repo root)
    run = studio.run(
        command=["python", "train.py"],
        environment="lightning.ai/environments/pytorch:latest",
        requirements=["huggingface_hub", "pyarrow", "pandas"],
    )
    print(f"Submitted run: {run.id}")

if __name__ == "__main__":
    main()
```

### train.py (new or replace)
```python
#!/usr/bin/env python3
"""
Train using CDN-only fetches.
Expects manifests/{date}/files.json produced by build_manifest.py.
"""
import json, os, sys
from pathlib import Path
import requests
import pyarrow as pa
import pyarrow.parquet as pq

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifests/latest/files.json")
LOCAL_CACHE = Path(os.getenv("LOCAL_CACHE", ".hf_cache"))

def cdn_url(repo: str, file_path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"

def stream_parquet(url: str):
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    return pa.BufferReader(resp.content)

def load_selected(manifest_path: str):
    with open(manifest_path) as f:
        cfg = json.load(f)
    repo = cfg["repo"]
    for file_path in cfg["files"]:
        if not file_path.endswith(".parquet"):
            continue
        url = cdn_url(repo, file_path)
        try:
            table = pq.read_table(stream_parquet(url))
            # Project only {prompt, response}; ignore mixed schema extras
            cols = [c for c in table.column_names if c in ("prompt", "response")]
            if len(cols) < 2:
                continue
            yield table.select(cols).to_pylist()
        except Exception as e:
            print(f"Skip {file_path}: {e}", file=sys.stderr)

def main() -> None:
    LOCAL_CACHE.mkdir(parents=True, exist_ok=True)
    examples = []
    for batch in load_selected(MANIFEST_PATH):
        examples.extend(batch)
        if len(examples) >= 1000:
            break
    print(f"Loaded {len(examples)} examples via CDN (no HF API auth)")

    # Continue with your training loop using `examples`
    # e.g., tokenize and train surrogate-1

if __name__ == "__main__":
    main()
```

### Frontend integration hint (if applicable)
At build time, copy the latest manifest into the bundle:
```bash
cp manifests/latest/files.json frontend/public/manifest.json
```
Then in frontend code, fetch `/manifest.json` and construct CDN URLs directly — no authenticated HF API calls from the browser.

## 4. Verification

1. Generate manifest (Mac/CI):
   ```bash
   export HF_DATASET_REPO=axentx/vanguard-data
   export DATE_FOLDER=2026-05-03
   python3 scripts/build_manifest.py
   # Expect manifests/2026-05-03/files.json with non-empty files list
   ```

2. Confirm CDN URLs work without auth:
   ```bash
   head_file=$(jq -r '.files[0]' manifests/2026-05-03/files.json)
   curl -I "https://huggingface.co/datasets/axentx/vanguard-data/resolve/main/${head_file}"
   # Expect HTTP 200 (no 401/429 from API)
   ```

3. Dry-run training fetch:
   ```bash
   MANIFEST_PATH=manifests/2026-05-03/files.json python3 train.py
   # Expect "Loaded N examples via CDN (no HF API auth)" and no pyarrow schema errors
   ```

4. Studio reuse check:
   ```bash
   python3 scripts/launch_studio.py
   # If a running studio named vanguard-train exists, it should print "Reusing running studio"
   # Otherwise it should create/start one and submit the run
   ```

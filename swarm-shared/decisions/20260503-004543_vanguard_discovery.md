# vanguard / discovery

## 1. Diagnosis

- No persisted `(repo, dateFolder)` file manifest exists → every training run re-enumerates via authenticated HF API, burning quota and risking 429.
- Recursive `list_repo_files` usage (or equivalent) likely paginates heavily and exposes mixed-schema files, causing `pyarrow.CastError` downstream.
- Training script probably uses `load_dataset(streaming=True)` on heterogeneous repo files instead of deterministic CDN fetches.
- Lightning Studio lifecycle is likely recreated per run instead of reused, wasting 80hr/mo quota.
- No CDN-only path strategy: training still depends on authenticated `/api/` endpoints during data loading.

## 2. Proposed change

Create `/opt/axentx/vanguard/scripts/build_manifest.py` and update `/opt/axentx/vanguard/train.py` (or equivalent) to:
- Accept a pre-built `manifest.json` listing exact CDN paths for one date folder.
- Use CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with zero authenticated API calls during training.
- Reuse a running Lightning Studio instead of recreating it.

Scope:
- New file: `scripts/build_manifest.py`
- Modify: `train.py` (or launcher) to accept `--manifest manifest.json` and switch to `datasets` loading via `load_from_disk`/custom CDN iterator.
- Add: lightweight orchestration script `scripts/run_training.sh` with proper Bash shebang and `SHELL=/bin/bash` note.

## 3. Implementation

```bash
# Create directory if missing
mkdir -p /opt/axentx/vanguard/scripts
```

`/opt/axentx/vanguard/scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Build a deterministic file manifest for one date folder in a HF dataset repo.
Usage:
    python build_manifest.py --repo <repo> --date-folder <yyyy-mm-dd> --out manifest.json
"""
import argparse
import json
import os
import time
from typing import List, Dict

from huggingface_hub import HfApi, hf_hub_download

API = HfApi()

def list_date_files(repo: str, date_folder: str) -> List[Dict]:
    """
    Non-recursive listing for one date folder.
    Returns list of dicts with CDN path and local cache hint.
    """
    # Use non-recursive tree to avoid heavy pagination
    tree = API.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = []
    for entry in tree:
        if entry.type != "file":
            continue
        # CDN path (no auth required)
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{entry.path}"
        files.append({
            "repo": repo,
            "path": entry.path,
            "cdn_url": cdn_url,
            "size": getattr(entry, "size", None)
        })
    return files

def build(repo: str, date_folder: str, out_path: str) -> None:
    print(f"Building manifest for {repo}/{date_folder} ...")
    files = list_date_files(repo, date_folder)
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
        "count": len(files)
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} entries to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build HF dataset manifest for one date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (user/repo)")
    parser.add_argument("--date-folder", required=True, help="Date folder inside repo (e.g. 2026-04-29)")
    parser.add_argument("--out", default="manifest.json", help="Output JSON path")
    args = parser.parse_args()
    build(args.repo, args.date_folder, args.out)
```

`/opt/axentx/vanguard/scripts/run_training.sh`
```bash
#!/usr/bin/env bash
# Wrapper to run Lightning training with manifest and Studio reuse.
# Ensure this is invoked via Bash and executable (chmod +x).
set -euo pipefail

MANIFEST="${1:-manifest.json}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

# Prefer reuse of existing running studio to save quota
python -m vanguard.train --manifest "$MANIFEST" --reuse-studio
```

`/opt/axentx/vanguard/train.py` (minimal diff sketch — adapt to existing file)
```diff
+ import argparse
+ import json
+ import requests
+ from pathlib import Path
+ from datasets import load_dataset, Dataset, DatasetDict
+ from huggingface_hub import HfApi

+ def load_manifest(manifest_path: str):
+     with open(manifest_path) as f:
+         return json.load(f)
+
+ def cdn_dataset_generator(manifest):
+     """Yield {prompt, response} rows from CDN files without authenticated API calls."""
+     for f in manifest["files"]:
+         url = f["cdn_url"]
+         # Stream download; project to {prompt, response} here
+         resp = requests.get(url, timeout=30)
+         resp.raise_for_status()
+         # Replace with actual parser for your file type (jsonl/parquet/etc.)
+         # Example assumes jsonl lines with 'prompt' and 'response'
+         for line in resp.text.splitlines():
+             line = line.strip()
+             if not line:
+                 continue
+             obj = json.loads(line)
+             yield {"prompt": obj["prompt"], "response": obj["response"]}
+
+ def make_dataset(manifest):
+     ds = Dataset.from_generator(
+         lambda: cdn_dataset_generator(manifest),
+         features={"prompt": "string", "response": "string"}
+     )
+     return ds

-def main():
-    # old: load_dataset(streaming=True, ...)
+ def main():
+     parser = argparse.ArgumentParser()
+     parser.add_argument("--manifest", required=True, help="Path to manifest.json")
+     parser.add_argument("--reuse-studio", action="store_true", help="Reuse running Lightning Studio")
+     args = parser.parse_args()
+
+     manifest = load_manifest(args.manifest)
+     dataset = make_dataset(manifest)
+
+     # Lightning Studio reuse logic (idempotent)
+     try:
+         from lightning import Studio, Teamspace, Machine
+         teamspace = Teamspace()
+         studio_name = "vanguard-train"
+         studio = None
+         for s in teamspace.studios:
+             if s.name == studio_name and s.status == "Running":
+                 studio = s
+                 print(f"Reusing running studio: {studio_name}")
+                 break
+         if studio is None:
+             print("Starting new studio (L40S)...")
+             studio = Studio(
+                 name=studio_name,
+                 machine=Machine.L40S,
+                 create_ok=True
+             )
+         # If studio stopped, restart it
+         if studio.status != "Running":
+             print("Studio not running; restarting...")
+             studio.start(machine=Machine.L40S)
+
+         # Run training step using dataset
+         studio.run(
+             run_fn=train_step,
+             dataset=dataset
+         )
+     except ImportError:
+         print("Lightning not available; running local training step.")
+         train_step(dataset=dataset)

-def train_step(dataset):
+ def train_step(dataset):
+     # Your existing training logic here, using `dataset`
     ...
```

Make scripts executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
chmod +x /opt/axentx/vanguard/scripts/run_training.sh
```

## 4. Verification

1. Build manifest (run once per date folder after rate-limit window is clear):
   ```bash
   cd /opt/axentx/vanguard
   python scripts/build_manifest.py --repo myorg/surrogate

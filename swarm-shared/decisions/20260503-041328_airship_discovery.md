# airship / discovery

## Incremental Improvement: CDN-only training + Lightning idle-resilience (<2h)

**Goal**: Eliminate HF API 429s during Surrogate training and make Lightning training resilient to idle timeouts using existing CDN paths and Lightning SDK reuse.

### Implementation Plan

1. **Generate CDN file manifest** (Mac orchestration, one-time or on schedule)
   - Use `list_repo_tree` per date folder → save `training_manifest.json`
   - Embed public CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`)

2. **Update Surrogate training script** (`surrogate/train.py`)
   - Accept `--manifest training_manifest.json`
   - Data loader uses `requests.get(cdn_url, stream=True)` + `pyarrow` projection to `{prompt, response}` only
   - Zero `datasets.load_dataset` or `hf_api` calls during training

3. **Lightning Studio reuse + idle-resilience wrapper** (`surrogate/run_lightning.py`)
   - List existing studios, reuse running one
   - Before `.run()`, check status; if stopped → restart with `target.start(machine=Machine.L40S)`
   - Set `SHELL=/bin/bash` in any cron wrappers

4. **Deploy**
   - Replace current training invocation with new wrapper
   - Keep HF API usage to Mac-side manifest generation only (outside rate-limited windows)

---

### Code Snippets

#### 1. Manifest generator (`surrogate/gen_manifest.py`)
```python
#!/usr/bin/env python3
"""
Generate CDN-only manifest for Surrogate training.
Run on Mac (or cron) after rate-limit window clears.
"""
import json
import os
from huggingface_hub import HfApi

api = HfApi()
REPO = "axentx/surrogate-data"
DATE_FOLDER = "batches/mirror-merged/2026-05-03"  # parameterized in prod

def build_manifest():
    entries = []
    # recursive=False per folder, then walk subfolders manually to avoid 429
    for root, dirs, files in api.list_repo_tree(REPO, path=DATE_FOLDER, recursive=False):
        for f in files:
            if f.path.endswith(".parquet"):
                cdn_url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{f.path}"
                entries.append({
                    "path": f.path,
                    "cdn_url": cdn_url,
                    "size": f.size
                })
        # one-level recursion only
        for d in dirs:
            subpath = os.path.join(root, d) if root else d
            for _, _, subfiles in api.list_repo_tree(REPO, path=subpath, recursive=False):
                for sf in subfiles:
                    if sf.path.endswith(".parquet"):
                        cdn_url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{sf.path}"
                        entries.append({
                            "path": sf.path,
                            "cdn_url": cdn_url,
                            "size": sf.size
                        })

    manifest = {
        "repo": REPO,
        "date_folder": DATE_FOLDER,
        "files": entries
    }
    out_path = "training_manifest.json"
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {out_path} ({len(entries)} files)")

if __name__ == "__main__":
    build_manifest()
```

#### 2. CDN-only data loader (`surrogate/data.py`)
```python
import json
import pyarrow.parquet as pq
import requests
from io import BytesIO
from typing import List, Dict

class CDNParquetLoader:
    def __init__(self, manifest_path: str):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = self.manifest["files"]

    def stream_records(self, columns=("prompt", "response")):
        for entry in self.files:
            resp = requests.get(entry["cdn_url"], stream=True, timeout=30)
            resp.raise_for_status()
            buf = BytesIO(resp.content)
            table = pq.read_table(buf, columns=columns)
            for batch in table.to_batches(max_chunksize=1024):
                df = batch.to_pandas()
                for _, row in df.iterrows():
                    yield {"prompt": row["prompt"], "response": row["response"]}
```

#### 3. Lightning idle-resilience wrapper (`surrogate/run_lightning.py`)
```bash
#!/usr/bin/env bash
# Run Surrogate training on Lightning Studio with idle-resilience and reuse.
# Ensure SHELL=/bin/bash in crontab if scheduled.
set -euo pipefail

export SHELL=/bin/bash
MANIFEST="${1:-training_manifest.json}"
SCRIPT="surrogate/train.py"
STUDIO_NAME="surrogate-train-l40s"
MACHINE="lightning-lambda-prod/L40S"

# Reuse or create studio
python3 - <<PY
from lightning import Studio, Machine, Teamspace
import sys

manifest = sys.argv[1]
studio_name = sys.argv[2]
machine = Machine(sys.argv[3])
script = sys.argv[4]

ts = Teamspace()
running = None
for s in ts.studios:
    if s.name == studio_name and s.status == "running":
        running = s
        print(f"Reusing running studio: {s.name}")
        break

if running is None:
    print(f"Creating studio: {studio_name}")
    running = Studio(
        name=studio_name,
        machine=machine,
        create_ok=True
    )

# Idle-resilience: ensure running before run
if running.status != "running":
    print(f"Studio stopped ({running.status}), restarting...")
    running.start(machine=machine)

# Run training (non-blocking or blocking depending on needs)
running.run(
    script=script,
    arguments=[f"--manifest={manifest}"],
    wait=False
)
print("Training job submitted.")
PY "$MANIFEST" "$STUDIO_NAME" "$MACHINE" "$SCRIPT"
```

#### 4. Update training script args (`surrogate/train.py` snippet)
```python
import argparse
from surrogate.data import CDNParquetLoader

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to training_manifest.json")
    parser.add_argument("--epochs", type=int, default=1)
    args = parser.parse_args()

    loader = CDNParquetLoader(args.manifest)
    for record in loader.stream_records():
        # train step using record["prompt"], record["response"]
        ...
```

---

### Deployment checklist
- [ ] `chmod +x surrogate/gen_manifest.py surrogate/run_lightning.py`
- [ ] Generate `training_manifest.json` on Mac (or cron) after HF rate-limit window
- [ ] Replace existing training invocation with `surrogate/run_lightning.py`
- [ ] Verify cron wrappers include `SHELL=/bin/bash`
- [ ] Confirm Lightning quota savings by reusing running studio

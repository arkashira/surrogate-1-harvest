# airship / frontend

## Highest-Value Incremental Improvement (<2h)

**Goal**: Eliminate HF API 429s during Surrogate training data ingestion and prevent Lightning Studio quota waste by implementing **CDN-first deterministic ingestion + Studio reuse**.

**Why this ships fast**:  
- Pure orchestration change (no model code)  
- Reuses existing HF/Lightning SDK calls  
- ~90min implementation + 30min validation  
- Directly fixes the two highest-cost failure modes (rate limits + idle restarts)

---

## Implementation Plan

### 1. Create CDN-first file lister (`scripts/list_hf_files.py`)
Single API call per date folder → JSON file embedded in training.

```python
#!/usr/bin/env python3
"""
List HF dataset files for a date folder (non-recursive) and emit JSON.
Run from Mac after rate-limit window clears. Embed output in train.py.
"""
import json
import os
import sys
from datetime import datetime, timezone
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-ingest")
DATE_FOLDER = os.getenv("INGEST_DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
OUTPUT = os.getenv("OUTPUT_FILE", f"file_list_{DATE_FOLDER}.json")

def list_files_cdn_first():
    api = HfApi()
    # Single API call, non-recursive
    entries = api.list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_FOLDER,
        repo_type="dataset",
        recursive=False
    )
    # Keep only files (not subdirs), use CDN URLs
    files = [
        {
            "path": e.path,
            "cdn_url": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{e.path}"
        }
        for e in entries
        if e.type == "file"
    ]
    payload = {
        "repo": HF_REPO,
        "date": DATE_FOLDER,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "count": len(files)
    }
    with open(OUTPUT, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(files)} files to {OUTPUT}")
    return payload

if __name__ == "__main__":
    list_files_cdn_first()
```

Make executable:
```bash
chmod +x scripts/list_hf_files.py
```

---

### 2. Update training loader to use CDN-only (`surrogate/train.py` snippet)

Replace `load_dataset(streaming=True)` with CDN fetches using the embedded file list.

```python
# surrogate/train.py  (excerpt)
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset

class CDNParquetDataset(IterableDataset):
    def __init__(self, file_list_path="file_list_latest.json", max_retries=3):
        list_path = Path(file_list_path)
        if not list_path.exists():
            raise FileNotFoundError(f"File list not found: {file_list_path}")
        with open(list_path) as f:
            manifest = json.load(f)
        self.files = [f["cdn_url"] for f in manifest["files"] if f["path"].endswith(".parquet")]
        self.max_retries = max_retries

    def __iter__(self):
        for url in self.files:
            for attempt in range(self.max_retries):
                try:
                    resp = requests.get(url, timeout=30)
                    resp.raise_for_status()
                    table = pq.read_table(pa.BufferReader(resp.content))
                    # Project to {prompt, response} only
                    batch = table.select(["prompt", "response"]).to_batches()[0]
                    for i in range(batch.num_rows):
                        yield {
                            "prompt": batch["prompt"][i].as_py(),
                            "response": batch["response"][i].as_py(),
                        }
                    break
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        print(f"Failed {url}: {e}")
                        raise
                    continue
```

---

### 3. Lightning Studio reuse + idle guard (`scripts/launch_training.py`)

```python
#!/usr/bin/env python3
"""
Launch Surrogate training with Studio reuse and idle-stop resilience.
"""
import os
import sys
from lightning_sdk import Studio, Teamspace, Machine

TEAMSPACE = os.getenv("LIGHNING_TEAMSPACE", "default")
STUDIO_NAME = os.getenv("STUDIO_NAME", "surrogate-train-l40s")
MACHINE = Machine.L40S  # Free tier fallback → L40S; H200 requires lambda-prod

def launch_or_reuse():
    team = Teamspace(TEAMSPACE)
    running = [s for s in team.studios if s.name == STUDIO_NAME and s.status == "Running"]

    if running:
        studio = running[0]
        print(f"Reusing running studio: {studio.id}")
    else:
        studio = team.studios.create(
            STUDIO_NAME,
            machine=MACHINE,
            create_ok=True
        )
        print(f"Created studio: {studio.id}")

    # Guard against idle stop
    if studio.status != "Running":
        studio.start(machine=MACHINE)
        print("Restarted idle studio")

    # Run training (non-blocking)
    run = studio.run(
        command=[
            "python", "train.py",
            "--file-list", "file_list_latest.json",
            "--epochs", "1"
        ],
        environment={
            "HF_DATASET_REPO": "axentx/surrogate-ingest",
            "PYTHONUNBUFFERED": "1"
        }
    )
    print(f"Training run submitted: {run.id}")
    return run

if __name__ == "__main__":
    launch_or_reuse()
```

Make executable:
```bash
chmod +x scripts/launch_training.py
```

---

### 4. Cron-safe wrapper for ingestion (`scripts/ingest_wrapper.sh`)

Follows the **bash/shebang/executable** pattern from lessons learned.

```bash
#!/usr/bin/env bash
# scripts/ingest_wrapper.sh
# Cron-safe wrapper for HF ingestion → file list generation

set -euo pipefail
export SHELL=/bin/bash

cd /opt/axentx/airship

# Respect HF rate limits: wait if recently hit 429
if [[ -f /tmp/hf_429_since ]]; then
    elapsed=$(( $(date +%s) - $(cat /tmp/hf_429_since) ))
    if (( elapsed < 360 )); then
        echo "HF 429 cooldown active (${elapsed}s elapsed). Skipping."
        exit 0
    fi
fi

# Single API call (non-recursive) for today
python3 scripts/list_hf_files.py

echo "Ingestion list generated OK"
```

Make executable + install cron:
```bash
chmod +x scripts/ingest_wrapper.sh
(crontab -l 2>/dev/null; echo "0 3 * * * /opt/axentx/airship/scripts/ingest_wrapper.sh >> /var/log/airship_ingest.log 2>&1") | crontab -
```

---

## Validation Steps (30 min)

```bash
# 1. Generate file list (simulate cron)
./scripts/ingest_wrapper.sh
ls -la file_list_*.json

# 2. Dry-run training loader
python3 -c "from surrogate.train import CDNParquetDataset; d=CDNParquetDataset('file_list_latest.json'); print(next(iter(d)))"

# 3. Launch studio (reuse path)
python3 scripts/launch_training.py
```

**Expected outcomes**:
- `file_list_YYYY-MM-DD.json` created with CDN URLs  
- No `load_dataset` calls → zero HF API calls during training  
- Running studio reused; idle restart handled  
- Cron job logs to `/var/log/airship_ingest.log`

---

## Tags
#cdn-first #lightning-ai #hf-rate-limit #studio-reuse #surrogate-training

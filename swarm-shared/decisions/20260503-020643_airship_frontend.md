# airship / frontend

**Final Synthesized Implementation Plan (≤2h)**

**Goal**: Eliminate HF API rate limits, recover Lightning quota, and harden Surrogate training against idle-stop death via CDN-first ingestion, Studio reuse, and resilient orchestration/UI.

---

### 1. CDN-first deterministic ingestion (single source of truth)
- **Action**: On the Mac orchestrator, export a deterministic CDN file-list once per dataset/date.
- **Why**: Bypasses `/api/` rate limits entirely; training reads only from public CDN URLs.
- **Artifact**: `scripts/export_cdn_filelist.py` → `cdn_filelist_YYYY-MM-DD.json`

```python
# scripts/export_cdn_filelist.py
import json, os
from datetime import datetime
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "org/surrogate-dataset")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
OUT_PATH = os.getenv("OUT_PATH", f"cdn_filelist_{DATE_FOLDER}.json")

api = HfApi()
tree = api.list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)

files = []
for item in tree:
    if item.path.endswith((".parquet", ".jsonl")):
        cdn_url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{item.path}"
        files.append({"path": item.path, "cdn_url": cdn_url})

with open(OUT_PATH, "w") as f:
    json.dump({"date": DATE_FOLDER, "repo": HF_REPO, "files": files}, f, indent=2)

print(f"Exported {len(files)} files to {OUT_PATH}")
```

Usage:
```bash
export HF_DATASET_REPO=org/surrogate-dataset
export DATE_FOLDER=2026-05-03
python scripts/export_cdn_filelist.py
```

---

### 2. Training: CDN-only dataset loader (no HF API during training)
- **Action**: Replace `load_dataset` calls with `CDNParquetDataset` that streams from CDN URLs listed in the embedded JSON.
- **Why**: Zero HF API calls during training; avoids 429s and mixed-schema issues.

```python
# surrogate/train.py (excerpt)
import json, pyarrow.parquet as pq, requests
from torch.utils.data import IterableDataset

class CDNParquetDataset(IterableDataset):
    def __init__(self, filelist_path):
        with open(filelist_path) as f:
            manifest = json.load(f)
        self.cdn_urls = [f["cdn_url"] for f in manifest["files"]]

    def __iter__(self):
        for url in self.cdn_urls:
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                tbl = pq.read_table(pq.ParquetFile(pq.ParquetReader(r.raw)))
                # Project only needed columns
                for batch in tbl.to_batches(max_chunksize=1024):
                    df = batch.slice(0, batch.num_rows).to_pandas()
                    for _, row in df.iterrows():
                        yield {"prompt": row["prompt"], "response": row["response"]}
```

Notes:
- Do not use `load_dataset(streaming=True)` on mixed-schema repos.
- Keep manifest path configurable (e.g., `--filelist cdn_filelist_2026-05-03.json`).

---

### 3. Lightning Studio guard with reuse (quota recovery)
- **Action**: Add `lightning_utils.py` with `get_or_create_studio`, `wait_for_studio_ready`, and `run_training_with_guard`.
- **Why**: Reuse running Studios; auto-restart if stopped; prevent quota burn from repeated creates.

```python
# surrogate/lightning_utils.py
import os, time
from lightning_sdk import Client, Teamspace, Studio, Machine

LIGHTNING_USERNAME = os.getenv("LIGHTNING_USERNAME")
TEAMSPACE_NAME = os.getenv("TEAMSPACE_NAME", "default")
STUDIO_NAME = os.getenv("STUDIO_NAME", "surrogate-trainer")
MACHINE = Machine.L40S  # fallback; prefer H200 in lightning-lambda-prod if quota

client = Client()
teamspace = Teamspace(client, name=TEAMSPACE_NAME)

def get_or_create_studio():
    for s in teamspace.studios:
        if s.name == STUDIO_NAME:
            if s.status == "running":
                print(f"Reusing running studio: {STUDIO_NAME}")
                return s
            elif s.status == "stopped":
                print(f"Restarting stopped studio: {STUDIO_NAME}")
                s.start(machine=MACHINE)
                return s
    print(f"Creating new studio: {STUDIO_NAME}")
    return Studio.create(
        client,
        name=STUDIO_NAME,
        teamspace=teamspace,
        machine=MACHINE,
        framework="pytorch",
    )

def wait_for_studio_ready(studio, timeout=300, interval=10):
    elapsed = 0
    while elapsed < timeout:
        studio.refresh()
        if studio.status == "running":
            print("Studio is running.")
            return True
        print(f"Studio status: {studio.status}; waiting...")
        time.sleep(interval)
        elapsed += interval
    raise TimeoutError("Studio failed to become running in time.")

def run_training_with_guard(train_script_path, args=None):
    studio = get_or_create_studio()
    wait_for_studio_ready(studio)

    # Guard against idle-stop
    if studio.status != "running":
        studio.start(machine=MACHINE)
        wait_for_studio_ready(studio)

    run = studio.run(
        filename=train_script_path,
        arguments=args or [],
        sync=False,
    )
    print(f"Started training run: {run.name}")
    return run
```

---

### 4. Idle-stop resilient launcher (cron-safe)
- **Action**: Add `scripts/launch_studio.sh` to ensure Studio is running and kick off training.
- **Why**: Survives idle-stop; safe for cron or manual rerun.

```bash
#!/usr/bin/env bash
# scripts/launch_studio.sh
set -euo pipefail
export SHELL=/bin/bash

cd /opt/axentx/airship/surrogate
python -m lightning_utils.wait_for_studio_ready || true

python -m lightning_utils.run_training_with_guard train.py --config configs/cdn_cdn.json
```

Crontab (optional):
```cron
SHELL=/bin/bash
0 2 * * * cd /opt/axentx/airship/surrogate && bash scripts/launch_studio.sh >> logs/studio_launch.log 2>&1
```

---

### 5. Frontend: resilient Surrogate API calls
- **Action**: Add retry + circuit-breaker to Surrogate API client in Arkship UI.
- **Why**: Keeps UI functional when training restarts or transiently fails.

```javascript
// arkship/frontend/src/api/surrogateApi.js
import axios from "axios";

const surrogate = axios.create({
  baseURL: process.env.VUE_APP_SURROGATE_URL || "http://localhost:8001",
  timeout: 30000,
});

const RETRY_DELAYS = [1000, 3000, 10000];

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function requestWithRetry(config, retries = RETRY_DELAYS.length) {
  let lastError;
  for (let i = 0; i <= retries; i++) {
    try {
      return await surrogate(config);
    } catch (err) {
      lastError = err;
      const isRetryable = !err.response || err.response.status >= 500 || err.code === "ECONNABORTED";
      if (i === retries || !isRetryable) break;
      await sleep(RETRY_DELAYS[i]);
    }
  }
  throw lastError;
}

export default {
  startTraining(payload) {
    return requestWithRetry({ method: "post", url: "/train", data: payload });
  },
  getStatus() {
    return requestWithRetry({ method: "get", url: "/status" });
  },
};
```

---

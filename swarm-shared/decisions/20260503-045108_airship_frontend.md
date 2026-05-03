# airship / frontend

# Final Implementation Plan (≤2h)

## Core Objective
Eliminate HF API 429s and Lightning idle-stop quota waste by:
1. **Pre-listing file paths once** and embedding them in training (CDN-only fetches during training).
2. **Resilient Lightning Studio lifecycle** (auto-restart on idle-stop).
3. **Deterministic write sharding** across sibling repos to avoid HF commit cap.

---

## 1) Backend: Pre-list file paths (Mac orchestration)
Create `scripts/pre_list_hf_files.py`.

```python
# scripts/pre_list_hf_files.py
import os, json, datetime
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "org/surrogate-dataset")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", datetime.date.today().strftime("%Y-%m-%d"))
OUT_FILE = os.getenv("OUT_FILE", "file_list.json")

api = HfApi()
# Non-recursive per folder to avoid pagination explosion
entries = api.list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)

files = []
for e in entries:
    if e.path.endswith((".parquet", ".jsonl")):
        files.append({
            "path": e.path,
            "cdn_url": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{e.path}"
        })

with open(OUT_FILE, "w") as f:
    json.dump({"date": DATE_FOLDER, "files": files, "repo": HF_REPO}, f, indent=2)

print(f"Wrote {len(files)} files to {OUT_FILE}")
```

**Why this wins**:  
- Single API call per date folder (avoids 429s).  
- Non-recursive listing prevents pagination/timeouts.  
- CDN URLs embedded so training never calls HF API for data.

---

## 2) Backend: Lightning training launcher with idle resilience
Create `scripts/launch_surrogate_training.py`.

```python
# scripts/launch_surrogate_training.py
import os, time, json
from lightning_sdk import Teamspace, Studio, Machine

TEAMSPACE = os.getenv("LIGHNING_TEAMSPACE", "default")
STUDIO_NAME = os.getenv("STUDIO_NAME", "surrogate-training")
MACHINE = Machine.L40S  # falls back to public tier if not available
FILE_LIST = os.getenv("FILE_LIST", "file_list.json")

with open(FILE_LIST) as f:
    file_index = json.load(f)

ts = Teamspace(TEAMSPACE)

# Reuse running studio to save quota
studio = None
for s in ts.studios:
    if s.name == STUDIO_NAME:
        studio = s
        break

if studio is None:
    studio = ts.create_studio(
        name=STUDIO_NAME,
        machine=MACHINE,
        image="pytorch/pytorch:latest",
        create_ok=True
    )

# If stopped, restart
if studio.status != "running":
    print(f"Studio {STUDIO_NAME} is {studio.status}. Restarting...")
    studio.start(machine=MACHINE)
    # Wait until running
    while studio.status != "running":
        time.sleep(10)
        studio.refresh()

# Run training script with embedded file list
run = studio.run(
    command=[
        "python", "train.py",
        "--file-index", FILE_LIST,
        "--output-repo", os.getenv("HF_OUTPUT_REPO", "org/surrogate-enriched")
    ],
    wait=False
)

print(f"Started run {run.id} in studio {STUDIO_NAME}")
```

**Why this wins**:  
- Reuses existing studio to avoid idle-stop quota waste.  
- Auto-restarts if idle-stop killed the session.  
- Embeds file list so training never needs HF API for data.

---

## 3) Training script: CDN-only data loader
Update `train.py` to use CDN URLs from the embedded file list (no HF API during training).

```python
# train.py (excerpt)
import argparse, json, io, pyarrow.parquet as pq
import requests
from torch.utils.data import Dataset, DataLoader

class CDNParquetDataset(Dataset):
    def __init__(self, file_index_path):
        with open(file_index_path) as f:
            index = json.load(f)
        self.cdn_urls = [f["cdn_url"] for f in index["files"]]

    def __len__(self):
        return len(self.cdn_urls)

    def __getitem__(self, idx):
        resp = requests.get(self.cdn_urls[idx], timeout=30)
        resp.raise_for_status()
        table = pq.read_table(io.BytesIO(resp.content))
        # Project to {prompt, response} only
        return {
            "prompt": table["prompt"].to_pylist(),
            "response": table["response"].to_pylist()
        }
```

**Why this wins**:  
- CDN-only fetches (no Authorization header) → zero HF API calls during training.  
- Avoids `load_dataset(streaming=True)` for repos with heterogeneous schemas.  
- Projects only needed columns to reduce memory.

---

## 4) Deterministic write sharding helper
Create `scripts/pick_shard_repo.py` to avoid HF commit cap.

```python
# scripts/pick_shard_repo.py
import hashlib, os

SLUG = os.getenv("SLUG")
SIBLINGS = int(os.getenv("HF_SIBLING_REPOS", "5"))

if not SLUG:
    raise ValueError("SLUG required")

digest = int(hashlib.sha256(SLUG.encode()).hexdigest(), 16)
shard_id = digest % SIBLINGS
repo = f"org/surrogate-enriched-shard-{shard_id}"
print(repo)
```

Use in training/upload step:
```bash
export SLUG="2026-05-03/my-batch"
HF_OUTPUT_REPO=$(python scripts/pick_shard_repo.py)
```

**Why this wins**:  
- Deterministic sharding spreads commit load across sibling repos.  
- Avoids HF commit cap per repo.

---

## 5) Frontend: Training orchestrator card (Arkship UI)
Add a minimal React component in `arkship/src/components/TrainingOrchestrator.tsx`.

```tsx
// arkship/src/components/TrainingOrchestrator.tsx
import { useState } from "react";
import axios from "axios";

export default function TrainingOrchestrator() {
  const [fileCount, setFileCount] = useState<number | null>(null);
  const [status, setStatus] = useState("idle");

  const preList = async () => {
    setStatus("pre-listing");
    // Calls Mac-side helper via proxy/API you expose
    const res = await axios.post("/api/pre-list-hf-files", {
      date: new Date().toISOString().split("T")[0]
    });
    setFileCount(res.data.files.length);
    setStatus("ready");
  };

  const startTraining = async () => {
    setStatus("starting");
    await axios.post("/api/launch-training", {
      fileList: "file_list.json",
      machine: "L40S"
    });
    setStatus("running");
  };

  return (
    <div className="p-4 border rounded">
      <h3 className="font-bold">Surrogate-1 Training</h3>
      <div className="mt-2 space-x-2">
        <button onClick={preList} disabled={status === "pre-listing"} className="btn">
          {status === "pre-listing" ? "Listing..." : "Pre-list HF Files"}
        </button>
        {fileCount !== null && (
          <span className="text-sm text-gray-600">{fileCount} files indexed</span>
        )}
        <button onClick={startTraining} disabled={!fileCount || status === "starting"} className="btn">
          {status === "starting" ? "Starting..." : "Start Training"}
        </button>
      </div>
      <div className="mt-2 text-xs text-gray-500">
        Uses CDN-only fetches • Auto-restarts on idle-stop • Sharded writes
      </div>
    </div>
  );
}
```

Add route + lightweight proxy endpoints in Arkship backend (e.g., `/api

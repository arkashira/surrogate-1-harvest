# airship / discovery

Candidate 3:
## Highest-Value Incremental Improvement (≤2h)
**Resilient Surrogate-1 Training Pipeline**  
Make training immune to HF API 429s and Lightning idle-stop quota loss by switching to deterministic CDN-only data loading + studio lifecycle resilience.

---

## Implementation Plan

### 1. Pre-list file paths once (Mac orchestration)
- Single `list_repo_tree` call for one date folder → save to `batches/mirror-merged/{date}/filelist.json`
- Embed this JSON in `train.py`; Lightning training uses CDN-only fetches (zero API calls during data load)

### 2. CDN-only data loader
- Replace `load_dataset(streaming=True)` with direct `hf_hub_download` (or raw CDN URL) per file
- Project to `{prompt, response}` only at parse time; heterogeneous schemas ignored
- Use `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth, CDN tier limits)

### 3. Lightning Studio lifecycle resilience
- Before each `.run()`, check studio status; if stopped, restart with `target.start(machine=Machine.L40S)`
- Reuse running studios (`Teamspace.studios` scan) to save quota

### 4. Schema hygiene for mirror writes
- Ensure dataset-mirror writes only `{prompt, response}` to `batches/mirror-merged/{date}/{slug}.parquet`
- Move attribution to filename pattern; do not add `source` or `ts` columns to avoid mixed-schema issues

### 5. Commit cap mitigation (optional, if writes needed)
- If training produces commits back to HF (e.g., model weights), hash slug → pick one of 5 sibling repos deterministically to spread writes (~640/hr aggregate)

---

## Code Snippets

### 1. Pre-list file paths (run once on Mac)
```bash
# scripts/list_train_files.py
import json
import os
from huggingface_hub import HfApi

REPO = "axentx/surrogate-mirror"
FOLDER = "batches/mirror-merged/2026-05-03"
OUT = "train_filelist.json"

api = HfApi()
tree = api.list_repo_tree(repo_id=REPO, path=FOLDER, recursive=False)

files = [
    {"repo": REPO, "path": item.path, "sha": item.commit_id}
    for item in tree
    if item.type == "file" and item.path.endswith(".parquet")
]

with open(OUT, "w") as f:
    json.dump(files, f, indent=2)

print(f"Wrote {len(files)} files to {OUT}")
```

### 2. CDN-only IterableDataset
```python
# surrogate/train/data.py
import json
import pyarrow.parquet as pq
import requests
import io
import torch
from torch.utils.data import IterableDataset

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

class CDNParquetDataset(IterableDataset):
    def __init__(self, filelist_path, start_idx=0):
        with open(filelist_path) as f:
            self.files = json.load(f)
        self.start_idx = start_idx

    def __iter__(self):
        for idx, item in enumerate(self.files):
            if idx < self.start_idx:
                continue
            url = CDN_TEMPLATE.format(repo=item["repo"], path=item["path"])
            for attempt in range(5):
                try:
                    resp = requests.get(url, timeout=30)
                    resp.raise_for_status()
                    break
                except Exception as e:
                    if attempt == 4:
                        raise
                    time.sleep(2 ** attempt)
            table = pq.read_table(io.BytesIO(resp.content))
            # Project to {prompt, response} only
            df = table.select(["prompt", "response"]).to_pandas()
            for _, row in df.iterrows():
                yield {"prompt": row["prompt"], "response": row["response"]}
```

### 3. Lightning Studio lifecycle resilience
```python
# surrogate/train/run.py
import time
from lightning import Lightning, Teamspace, Machine, Studio

LIGHTNING = Lightning()
TEAMSPACE = Teamspace("axentx")
STUDIO_NAME = "surrogate-train-l40s"

def get_or_create_studio():
    for s in TEAMSPACE.studios:
        if s.name == STUDIO_NAME:
            if s.status == "running":
                print(f"Reusing running studio: {s.name}")
                return s
            else:
                print(f"Studio {s.name} is {s.status}; restarting...")
                s.start(machine=Machine.L40S)
                wait_for_running(s)
                return s
    # create if not exists
    studio = Studio.create(
        name=STUDIO_NAME,
        machine=Machine.L40S,
        teamspace=TEAMSPACE,
        create_ok=True,
    )
    wait_for_running(studio)
    return studio

def wait_for_running(studio, timeout=300):
    for _ in range(timeout):
        studio.refresh()
        if studio.status == "running":
            return
        time.sleep(1)
    raise TimeoutError(f"Studio {studio.name} did not become running")

def main():
    studio = get_or_create_studio()
    # Run training script inside studio
    run = studio.run(
        "bash",
        "train.py --filelist train_filelist.json --ckpt_path last.ckpt",
        cwd="/workspace/surrogate",
    )
    # Monitor and auto-restart on idle-stop if needed
    while True:
        time.sleep(60)
        studio.refresh()
        if studio.status != "running":
            print("Studio stopped; restarting training...")
            studio.start(machine=Machine.L40S)
            wait_for_running(studio)
            studio.run(
                "bash",
                "train.py --filelist train_filelist.json --resume_from_checkpoint last.ckpt",
                cwd="/workspace/surrogate",
            )

if __name__ == "__main__":
    main()
```

### 4. Schema hygiene enforcement (mirror writer snippet)
```python
# surrogate/ingest/mirror_writer.py
def write_mirror_parquet(records, date, slug):
    table = pa.Table.from_pylist(
        [{"prompt": r["prompt"], "response": r["response"]} for r in records]
    )
    out_dir = f"batches/mirror-merged/{date}"
    os.makedirs(out_dir, exist_ok=True)
    pq.write_table(table, f"{out_dir}/{slug}.parquet")
    # No 'source' or 'ts' columns — attribution via filename pattern
```

---

## Acceptance Criteria
- Training completes without any HF API calls (verify via network logs).
- Studio auto-restarts after idle-stop and resumes from checkpoint.
- No mixed-schema parquet files in `batches/mirror-merged/`.

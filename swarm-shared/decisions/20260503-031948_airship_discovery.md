# airship / discovery

## Implementation Plan — airship/surrogate (≤2h)

**Highest-value incremental improvement:**  
Make Surrogate training HF-rate-limit-proof and Lightning-idle-resilient by:

1. Embedding a CDN-only file list (bypass `/api/` rate limits).
2. Adding Lightning Studio auto-recovery (reuse running studio, restart on idle stop).
3. Ensuring dataset projection to `{prompt, response}` only (no mixed-schema writes).

---

### Steps (concrete, ~90min)

| Time | Step |
|------|------|
| 0–15m | Add `scripts/build_filelist.py` — one-time Mac script that calls `list_repo_tree` per date folder and emits `data/filelist-{date}.json` (paths only). |
| 15–35m | Add `surrogate/data/cdn_stream.py` — Lightning-compatible IterableDataset that downloads via CDN URLs (`resolve/main/...`) with zero HF API calls during training. |
| 35–55m | Add `surrogate/training/train.py` — thin wrapper that uses `cdn_stream`, projects to `{prompt, response}`, and writes `batches/mirror-merged/{date}/{slug}.parquet` (no `source`/`ts` cols). |
| 55–75m | Add `scripts/lightning_launcher.py` — reuse running studio or start L40S (fallback to free-tier), with idle-check/auto-restart before each `.run()`. |
| 75–90m | Wire entrypoint + test run (local dry-run + one Lightning job). |

---

### Code snippets

#### 1) Pre-list CDN file manifest (run on Mac)

```python
# scripts/build_filelist.py
import os, json, datetime
from huggingface_hub import HfApi

API = HfApi()
REPO = "axentx/surrogate-data"
OUT_DIR = "data"
os.makedirs(OUT_DIR, exist_ok=True)

# One date folder at a time (reduces pagination)
date_folder = datetime.date.today().isoformat()  # e.g. 2026-05-03
tree = API.list_repo_tree(REPO, path=date_folder, recursive=False)

files = [
    item.path
    for item in tree
    if item.type == "file" and item.path.lower().endswith((".jsonl", ".json", ".parquet"))
]

out_path = os.path.join(OUT_DIR, f"filelist-{date_folder}.json")
with open(out_path, "w") as f:
    json.dump({"repo": REPO, "date": date_folder, "files": files}, f, indent=2)

print(f"Wrote {len(files)} files -> {out_path}")
```

Run once (after rate-limit window clears):

```bash
python scripts/build_filelist.py
```

---

#### 2) CDN-only stream dataset (zero API during training)

```python
# surrogate/data/cdn_stream.py
import json, os, pyarrow.parquet as pq, pyarrow as pa, io, requests
from torch.utils.data import IterableDataset
from typing import Iterator, Dict, Any

HF_CDN = "https://huggingface.co/datasets"

class CDNStreamDataset(IterableDataset):
    def __init__(self, filelist_path: str, repo: str, start_idx: int = 0, max_items: int = None):
        with open(filelist_path) as f:
            manifest = json.load(f)
        self.files = manifest["files"][start_idx:]
        if max_items:
            self.files = self.files[:max_items]
        self.repo = repo or manifest["repo"]

    def _download_cdn(self, path: str) -> bytes:
        url = f"{HF_CDN}/{self.repo}/resolve/main/{path}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content

    def _project_record(self, raw: Dict[str, Any]) -> Dict[str, str]:
        # Keep only {prompt, response}; drop extra cols
        return {
            "prompt": str(raw.get("prompt", raw.get("input", ""))),
            "response": str(raw.get("response", raw.get("output", ""))),
        }

    def __iter__(self) -> Iterator[Dict[str, str]]:
        for path in self.files:
            try:
                buf = io.BytesIO(self._download_cdn(path))
                table = pq.read_table(buf)
                for batch in table.to_batches(max_chunksize=100):
                    for row in batch.to_pylist():
                        yield self._project_record(row)
            except Exception as exc:
                # skip corrupt file; continue training
                print(f"Skipping {path}: {exc}")
                continue
```

---

#### 3) Training entry (project + write mirror-merged)

```python
# surrogate/training/train.py
import os, datetime, pyarrow as pa, pyarrow.parquet as pq
from surrogate.data.cdn_stream import CDNStreamDataset

def run_training(date: str, limit: int = None):
    filelist = f"data/filelist-{date}.json"
    ds = CDNStreamDataset(filelist, repo="axentx/surrogate-data", max_items=limit)

    out_dir = f"batches/mirror-merged/{date}"
    os.makedirs(out_dir, exist_ok=True)

    rows = []
    for i, rec in enumerate(ds):
        if rec["prompt"] and rec["response"]:
            rows.append(rec)
        if limit and i + 1 >= limit:
            break

    if not rows:
        print("No valid rows; skipping write.")
        return

    table = pa.Table.from_pylist(rows, schema=pa.schema([
        pa.field("prompt", pa.string()),
        pa.field("response", pa.string()),
    ]))

    slug = f"mirror-{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"
    out_path = os.path.join(out_dir, f"{slug}.parquet")
    pq.write_table(table, out_path)
    print(f"Wrote {len(rows)} rows -> {out_path}")

if __name__ == "__main__":
    today = datetime.date.today().isoformat()
    run_training(date=today, limit=50_000)
```

---

#### 4) Lightning launcher with reuse + idle recovery

```python
# scripts/lightning_launcher.py
import time
from lightning_sdk import Teamspace, Studio, Machine

TEAMSPACE = "default"
STUDIO_NAME = "surrogate-train-l40s"
SCRIPT = "surrogate/training/train.py"

def launch():
    ts = Teamspace(TEAMSPACE)

    # Reuse running studio if available
    running = None
    for s in ts.studios:
        if s.name == STUDIO_NAME and s.status == "Running":
            running = s
            break

    if running:
        print(f"Reusing running studio: {STUDIO_NAME}")
        studio = running
    else:
        print(f"Starting new studio: {STUDIO_NAME}")
        studio = Studio.create(
            name=STUDIO_NAME,
            teamspace=TEAMSPACE,
            machine=Machine.L40S,  # fallback to free-tier L40S if available
            idle_timeout=3600,
        )

    # Ensure studio is running before run()
    max_wait = 300
    waited = 0
    while studio.status != "Running" and waited < max_wait:
        time.sleep(10)
        waited += 10
        studio.refresh()

    if studio.status != "Running":
        raise RuntimeError("Studio failed to start")

    # Execute training script
    run = studio.run(SCRIPT)
    print(f"Run started: {run.id}")
    return run

if __name__ == "__main__":
    launch()
```

---

### Quick test (local, no Lightning)

```bash
# 1) Build file list (once)
python scripts/build_filelist.py

# 2) Dry-run training (local)
python surrogate/training/train.py
```

---

### Expected outcome
- Training no longer hits HF API `/api/` endpoints during data load → rate-limit-proof.
- Lightning Studio reused or auto-restarted → survives idle timeouts.
- Output follows `batches/mirror-merged/{date}/{slug}.parquet` with clean `{prompt, response}` schema.

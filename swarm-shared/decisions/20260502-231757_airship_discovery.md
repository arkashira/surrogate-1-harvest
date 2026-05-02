# airship / discovery

## Implementation Plan: `airship discover` — CDN-only, deterministic ingestion (<2h)

### Highest-value scope (≤2h)
- Make `discover` produce a **reproducible file manifest** once (HF API), then run **CDN-only ingestion** (zero API calls during training).
- Eliminate PyArrow schema errors by projecting `{prompt,response}` at parse time only.
- Deterministic shard selection via hash-slug → sibling repo to bypass 128 commit/hr cap.
- Reuse running Lightning Studio to save quota; fail fast if no GPU available.
- Output: `batches/mirror-merged/{date}/{slug}.parquet` (no extra metadata columns).

### Steps (concrete, ordered)

1. **Create manifest script** (`scripts/discover_make_manifest.py`)
   - Single API call: `list_repo_tree(repo, path=date_folder, recursive=False)`
   - Save JSON: `{"repo": repo, "date": date, "files": [{"path": ..., "cdn_url": ...}]}`
   - Exit; commit manifest to repo (counts toward 128/hr — do once per folder).

2. **Create CDN fetcher module** (`airship/discover/cdn_fetcher.py`)
   - Use `requests.get(cdn_url, timeout=30, stream=True)` (no auth header).
   - Retry 429/5xx with exponential backoff; respect `Retry-After`.
   - Stream to temp file; validate extension (`.jsonl`, `.parquet`, `.json`).

3. **Schema-safe parser** (`airship/discover/parser.py`)
   - If `.parquet`: read with `pyarrow.parquet.read_table(columns=["prompt","response"])` (projection avoids mixed-schema).
   - If `.jsonl`/`.json`: stream rows, keep only keys `prompt` and `response` (case-insensitive fuzzy match).
   - Yield dicts `{prompt: str, response: str}`; drop everything else.

4. **Shard writer** (`airship/discover/shard_writer.py`)
   - Hash slug → `repo_index = hash(slug) % N_SIBLINGS` (N=5).
   - Target repo: `{base}-{repo_index}`.
   - Batch to 128 rows/parquet; upload via HF API **only during manifest phase** (or use HF Space for heavier writes). During training, rely on CDN.

5. **Training launcher** (`scripts/launch_surrogate_train.py`)
   - Load manifest JSON.
   - Pass file list to Lightning `train.py` via CLI/env.
   - `train.py` uses `cdn_fetcher` + `parser` in DataLoader (no HF API).
   - Reuse running Studio: `Teamspace.studios` filter by name/status; else create with `Machine.L40S` (fallback to public tier).
   - Before `.run()`, check status; if stopped, `studio.start(machine=...)`.

6. **Cron/wrapper hardening**
   - Shebang `#!/usr/bin/env bash`, `chmod +x`.
   - Set `SHELL=/bin/bash` in crontab.
   - Invoke via `bash script.sh "$@"`.

7. **Smoke test**
   - Run manifest on one date folder.
   - Run ingestion locally (CDN-only) → 100 rows.
   - Launch Studio reuse + train step (dry-run 1 epoch).

---

## Code snippets

### `scripts/discover_make_manifest.py`
```python
#!/usr/bin/env python3
import json, os, sys
from datetime import datetime
from huggingface_hub import HfApi

REPO = os.getenv("HF_DATASET_REPO", "my-org/airship-mirror")
DATE_FOLDER = sys.argv[1] if len(sys.argv) > 1 else datetime.utcnow().strftime("%Y-%m-%d")
OUT = sys.argv[2] if len(sys.argv) > 2 else f"manifests/{DATE_FOLDER}.json"

api = HfApi()
entries = api.list_repo_tree(repo_id=REPO, path=DATE_FOLDER, recursive=False)

files = []
for e in entries:
    if e.type != "file":
        continue
    cdn_url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{DATE_FOLDER}/{e.path}"
    files.append({"path": e.path, "cdn_url": cdn_url})

manifest = {"repo": REPO, "date": DATE_FOLDER, "files": files}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(manifest, f, indent=2)

print(f"Manifest saved: {OUT} ({len(files)} files)")
```

---

### `airship/discover/cdn_fetcher.py`
```python
import requests, time, tempfile, os
from typing import BinaryIO

def stream_cdn_to_temp(cdn_url: str, max_retries: int = 5) -> str:
    retries = 0
    backoff = 1
    while retries < max_retries:
        try:
            with requests.get(cdn_url, stream=True, timeout=30) as r:
                r.raise_for_status()
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(cdn_url)[1])
                for chunk in r.iter_content(chunk_size=8192):
                    tmp.write(chunk)
                tmp.close()
                return tmp.name
        except requests.HTTPError as e:
            if e.response.status_code == 429:
                wait = int(e.response.headers.get("Retry-After", backoff))
                time.sleep(wait)
                retries += 1
                backoff = min(backoff * 2, 60)
                continue
            raise
    raise RuntimeError(f"Failed to fetch {cdn_url} after {max_retries} retries")
```

---

### `airship/discover/parser.py`
```python
import pyarrow.parquet as pq, json, os

def parse_file(local_path: str):
    ext = os.path.splitext(local_path)[1].lower()
    if ext == ".parquet":
        tbl = pq.read_table(local_path, columns=["prompt", "response"])
        df = tbl.to_pandas()
        for _, row in df.iterrows():
            yield {"prompt": str(row.get("prompt", "")), "response": str(row.get("response", ""))}
    else:
        with open(local_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
                response = obj.get("response") or obj.get("output") or obj.get("answer")
                if prompt is not None and response is not None:
                    yield {"prompt": str(prompt), "response": str(response)}
```

---

### `scripts/launch_surrogate_train.py`
```python
#!/usr/bin/env python3
import json, os, sys
from lightning import Lightning, Teamspace, Machine

MANIFEST = sys.argv[1]
with open(MANIFEST) as f:
    manifest = json.load(f)

# Reuse running studio
team = Teamspace()
studio = None
for s in team.studios:
    if s.name == "surrogate-train" and s.status == "Running":
        studio = s
        break

if studio is None:
    studio = team.create_studio(
        name="surrogate-train",
        machine=Machine.L40S,
        startup_command="sleep infinity"
    )
    studio.start()

# If stopped, restart
if studio.status != "Running":
    studio.start(machine=Machine.L40S)

# Run training (CDN-only)
run = studio.run(
    command=[
        "python", "train.py",
        "--manifest", MANIFEST,
        "--epochs", "1"
    ],
    cwd="/workspace/airship",
    wait=False
)
print(f"Started run: {run.id}")
```

---

### `train.py` (minimal CDN-only loader)
```python
import argparse, json
from torch.utils.data import IterableDataset, DataLoader
from airship.discover.cdn_fetcher import stream_cdn_to_temp
from airship.discover.parser import parse_file

class CDNTextDataset(IterableDataset):
    def __init

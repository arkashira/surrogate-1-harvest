# airship / discovery

## Highest-Value Incremental Improvement (<2h)
**Goal**: Harden `airship discover` into a deterministic, CDN-safe, zero-runtime orchestrator that eliminates HF API rate limits and PyArrow schema errors.

**Why this wins**:
- Removes `load_dataset(streaming=True)` and `list_repo_files` recursive calls → eliminates 429/128-hr HF API caps and mixed-schema PyArrow errors.
- Uses CDN-only fetches (`resolve/main/`) → bypasses auth/rate limits during training.
- Single `list_repo_tree` call per date folder → deterministic file list saved to JSON; Lightning training uses CDN-only fetches with zero API calls.
- Projects to `{prompt,response}` only at parse/upload time → no `source`/`ts` columns in enriched payloads.
- Reuses running Lightning Studio and respects idle-stop → saves quota and prevents lost training.

---

## Implementation Plan

### 1) Update discovery orchestrator (`airship/discover.py`)
- Replace HF dataset streaming with:
  1. `list_repo_tree(path, recursive=False)` for target date folder (once, after rate-limit window).
  2. Save file list to `batches/file-list-{date}.json`.
  3. Embed file list in training script; training fetches via CDN URLs only.
- Add schema-safe parser:
  - Download each file via `hf_hub_download` (or raw CDN) → parse only `{prompt,response}` fields.
  - Drop all other columns; move attribution to filename pattern `batches/mirror-merged/{date}/{slug}.parquet`.
- Add Lightning Studio reuse + idle handling:
  - List running studios; reuse if name/status match.
  - Before `.run()`, check status; if stopped, restart with `target.start(machine=Machine.L40S)`.

### 2) Add training data loader (`airship/train_loader.py`)
- Accept JSON file list.
- Fetch files via CDN: `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth).
- Parse each file into `{prompt, response}` records; yield per-example.
- Stream to parquet in `batches/mirror-merged/{date}/` with filename `{slug}.parquet`.

### 3) Update Lightning training launcher (`airship/train_launcher.py`)
- Use existing studio if running; else create with `Machine.L40S` (respect free-tier fallback).
- Pass CDN file list JSON to training script (no API calls during dataload).
- Wrap `.run()` with studio status check + restart on idle-stop.

### 4) Scripts hygiene
- Ensure all wrapper scripts have `#!/usr/bin/env bash`, are `chmod +x`, invoked via `bash <script> "$@"`.
- Set `SHELL=/bin/bash` in any crontab entries.

---

## Code Snippets

### `airship/discover.py` (excerpt)
```python
import json
import os
from datetime import datetime
from huggingface_hub import HfApi, hf_hub_download

HF_REPO = "axentx/surrogate-dataset"
DATE_FOLDER = datetime.utcnow().strftime("%Y-%m-%d")
OUT_DIR = f"batches/file-lists"
os.makedirs(OUT_DIR, exist_ok=True)

def list_date_files(date_folder: str) -> list[str]:
    api = HfApi()
    tree = api.list_repo_tree(repo_id=HF_REPO, path=date_folder, recursive=False)
    # tree items have .path; include only files
    files = [item.path for item in tree if item.type == "file"]
    return files

def save_file_list(date_folder: str, files: list[str]) -> str:
    out_path = os.path.join(OUT_DIR, f"file-list-{date_folder}.json")
    with open(out_path, "w") as f:
        json.dump(files, f, indent=2)
    return out_path

def build_cdn_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

if __name__ == "__main__":
    files = list_date_files(DATE_FOLDER)
    list_path = save_file_list(DATE_FOLDER, files)
    print(f"Saved file list ({len(files)} files) to {list_path}")
    # Print CDN URLs for embedding in training script
    for f in files[:3]:
        print(build_cdn_url(HF_REPO, f))
```

### `airship/train_loader.py` (excerpt)
```python
import json
import pyarrow.parquet as pq
import pyarrow as pa
import requests
from typing import Iterator, Dict, Any

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
HF_REPO = "axentx/surrogate-dataset"

def iter_cdn_files(file_list_json: str) -> Iterator[Dict[str, Any]]:
    with open(file_list_json) as f:
        files = json.load(f)
    for path in files:
        url = CDN_TEMPLATE.format(repo=HF_REPO, path=path)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        # Try parquet first; fallback to jsonl/csv if needed
        try:
            table = pq.read_table(pa.BufferReader(resp.content))
        except Exception:
            # fallback: parse as lines
            text = resp.text
            for line in text.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                prompt = obj.get("prompt") or obj.get("input") or obj.get("text")
                response = obj.get("response") or obj.get("output") or obj.get("completion")
                if prompt and response:
                    yield {"prompt": str(prompt), "response": str(response)}
            continue

        # Project only prompt/response; ignore other schema
        cols = set(table.column_names)
        prompt_col = next((c for c in ("prompt", "input", "text") if c in cols), None)
        response_col = next((c for c in ("response", "output", "completion") if c in cols), None)
        if prompt_col and response_col:
            for i in range(table.num_rows):
                row = table.slice(i, 1).to_pydict()
                yield {"prompt": str(row[prompt_col][0]), "response": str(row[response_col][0])}

def write_mirror_parquet(items: Iterator[Dict[str, Any]], out_dir: str, slug: str):
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{slug}.parquet")
    schema = pa.schema([
        pa.field("prompt", pa.string()),
        pa.field("response", pa.string()),
    ])
    arrays = {"prompt": [], "response": []}
    for item in items:
        arrays["prompt"].append(item["prompt"])
        arrays["response"].append(item["response"])
    table = pa.table(arrays, schema=schema)
    pq.write_table(table, out_path)
    print(f"Wrote {table.num_rows} examples to {out_path}")
    return out_path
```

### `airship/train_launcher.py` (excerpt — Lightning reuse + idle handling)
```python
from lightning_sdk import Teamspace, Studio, Machine
import time

TEAMSPACE = "airship-team"
STUDIO_NAME = "surrogate-train"
MACHINE = Machine.L40S

def get_or_create_studio():
    team = Teamspace(TEAMSPACE)
    running = [s for s in team.studios if s.name == STUDIO_NAME and s.status == "Running"]
    if running:
        print(f"Reusing running studio: {running[0].id}")
        return running[0]
    # create if not exists (or stopped)
    existing = [s for s in team.studios if s.name == STUDIO_NAME]
    if existing:
        s = existing[0]
        if s.status != "Running":
            print(f"Starting stopped studio {s.id}")
            s.start(machine=MACHINE)
            wait_for_running(s)
        return s
    print("Creating new studio")
    return team.studios.create(STUDIO_NAME, machine=MACHINE)

def wait_for_running(studio, timeout=300, interval=10):
    elapsed = 0
    while elapsed < timeout:
        studio.refresh()
        if studio.status == "Running":
            return
        time.sleep(

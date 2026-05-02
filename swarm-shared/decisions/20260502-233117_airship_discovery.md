# airship / discovery

## Final Synthesized Plan (Highest-Value, <2h)

**Goal** — Harden `airship discover` into a deterministic, **CDN-only** orchestrator that:

1. **Eliminates HF API rate limits** by fetching all data via  
   `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no Authorization header).
2. **Avoids PyArrow schema errors** by never using `load_dataset(streaming=True)` on heterogeneous repos; instead download individual files via CDN and project `{prompt, response}` at parse time.
3. **Produces reproducible file lists** via a single `list_repo_tree` snapshot saved as JSON and embedded in training scripts for Lightning CDN-only ingestion.

---

## Unified Implementation Plan (≤2h)

| Step | Action | Owner | Time |
|------|--------|-------|------|
| 1 | Inspect current `airship discover` entrypoint and CLI | me | 10m |
| 2 | Add `discover/cdn_file_list.py` — single API call to `list_repo_tree`, save `file_list.json` | me | 20m |
| 3 | Add `discover/cdn_download.py` — parallel CDN fetches using `aiohttp` + `asyncio` (no HF client) | me | 30m |
| 4 | Add `discover/project_parquet.py` — stream each file, extract `{prompt, response}`, write `batches/mirror-merged/{date}/{slug}.parquet` (no `source`/`ts` cols) | me | 30m |
| 5 | Add `train/embed_file_list.py` — embed `file_list.json` into `train.py` as constant; Lightning Studio uses CDN-only paths | me | 15m |
| 6 | Add `/opt/axentx/airship/scripts/discover-cdn-orchestrator.sh` — one-line cron wrapper that runs steps 2–4 sequentially with lockfile to prevent overlap | me | 10m |
| 7 | Update `docker-compose.microservices.yml` to mount `batches/` volume for Surrogate AI service | me | 10m |
| 8 | Smoke test: run discovery against a small HF dataset (e.g., `yahma/alpaca-cleaned` subset) and verify parquet output | me | 15m |

*Total: ~2h 10m (with buffer; core deliverable <2h)*

---

## Resolved Contradictions (Correctness + Actionability)

- **Orchestrator language**: Candidate 2 proposed a shell-only orchestrator; Candidate 1 used Python for data steps.  
  **Resolution**: Keep Python for data integrity and error handling (steps 2–5), and add a lightweight shell wrapper (step 6) for cron/lockfile simplicity. This gives deterministic runs without sacrificing robustness.

- **File naming for CDN downloads**: Candidate 1 flattened paths with `path.replace("/", "_")`; Candidate 2 implied preserving structure.  
  **Resolution**: Preserve directory structure under `RAW_DIR` (use `out_path = Path(OUT_DIR) / path`) to avoid collisions and ease debugging; create parents as needed.

- **Embedding file list**: Candidate 1 truncated the snippet; Candidate 2 did not include it.  
  **Resolution**: Provide a complete `embed_file_list.py` that reads `file_list.json`, generates a Python module constant, and writes it into the training package so `train.py` can import it without runtime file access.

- **Parquet schema**: Both agree on `{prompt, response}` only; Candidate 1 explicitly drops `source`/`ts`.  
  **Resolution**: Keep minimal schema; enforce via `pa.schema` to prevent accidental column creep.

---

## Final Code Snippets

### 1. `discover/cdn_file_list.py`
```python
#!/usr/bin/env python3
"""
Generate a deterministic file list for a HF dataset repo using a single tree API call.
Save as JSON to be embedded in training scripts.
"""
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "yahma/alpaca-cleaned")
OUT_DIR = os.getenv("DISCOVER_OUT_DIR", "batches/mirror-merged")
DATE_STR = datetime.now(timezone.utc).strftime("%Y%m%d")
OUTPUT = f"{OUT_DIR}/{DATE_STR}/file_list.json"

def main() -> None:
    api = HfApi()
    tree = api.list_repo_tree(repo_id=HF_REPO, recursive=True)
    files = [item.rfilename for item in tree if item.type == "file"]

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    payload = {
        "repo": HF_REPO,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": sorted(files),
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Saved {len(files)} files to {OUTPUT}")

if __name__ == "__main__":
    main()
```

### 2. `discover/cdn_download.py`
```python
#!/usr/bin/env python3
"""
Download dataset files via HF CDN (no auth, no API rate limits).
Uses aiohttp for parallel fetches.
"""
import asyncio
import json
import os
from pathlib import Path

import aiohttp
from tqdm.asyncio import tqdm_asyncio

HF_REPO = os.getenv("HF_DATASET_REPO", "yahma/alpaca-cleaned")
FILE_LIST = os.getenv("FILE_LIST", "batches/mirror-merged/latest/file_list.json")
OUT_DIR = os.getenv("DOWNLOAD_OUT_DIR", "batches/mirror-merged/latest/raw")
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

async def download_file(session: aiohttp.ClientSession, path: str, out_path: Path) -> None:
    url = CDN_TEMPLATE.format(repo=HF_REPO, path=path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    async with session.get(url) as resp:
        resp.raise_for_status()
        out_path.write_bytes(await resp.read())

async def main() -> None:
    with open(FILE_LIST) as f:
        data = json.load(f)
    files = data["files"]

    os.makedirs(OUT_DIR, exist_ok=True)
    async with aiohttp.ClientSession() as session:
        tasks = [
            download_file(session, path, Path(OUT_DIR) / path)
            for path in files
        ]
        await tqdm_asyncio.gather(*tasks, desc="CDN download")
    print("CDN download complete")

if __name__ == "__main__":
    asyncio.run(main())
```

### 3. `discover/project_parquet.py`
```python
#!/usr/bin/env python3
"""
Project raw downloaded files to {prompt, response} parquet.
Avoids load_dataset() and PyArrow schema issues by parsing per-file.
"""
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

RAW_DIR = os.getenv("RAW_DIR", "batches/mirror-merged/latest/raw")
OUT_DIR = os.getenv("PARQUET_OUT_DIR", "batches/mirror-merged/latest")
DATE_STR = os.getenv("DATE_STR", "latest")
SLUG = os.getenv("SLUG", "snapshot")

def infer_prompt_response(obj: dict) -> dict:
    prompt_keys = {"instruction", "prompt", "input", "question"}
    response_keys = {"output", "response", "answer", "completion"}
    prompt = None
    response = None
    for k, v in obj.items():
        if k in prompt_keys and isinstance(v, str):
            prompt = v
        if k in response_keys and isinstance(v, str):
            response = v
    if prompt is None:
        prompt = json.dumps(obj, ensure_ascii=False)
    if response is None:
        response = ""
    return {"prompt": prompt, "response": response}

def main() -> None:
    rows = []
    raw_path = Path(RAW_DIR)
    for file in raw_path.rglob("*"):
        if file.is_file():
            try:
                text = file.read_text(encoding="utf-8")
                for line in text.strip().splitlines():
                    if not line.strip():
                        continue
                    obj = json.loads(line)


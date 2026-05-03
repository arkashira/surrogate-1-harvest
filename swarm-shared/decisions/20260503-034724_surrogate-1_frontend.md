# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- On the orchestrator (Mac/CI): list target folder once via HF API (respect rate limits), save `file-list.json`, and embed it in the runner environment or upload as artifact.
- In each runner: read assigned shard of `file-list.json`, download files **only via CDN** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with no Authorization header (bypasses `/api/` rate limits).
- Parse each file into `{prompt, response}` (projection at parse time) — ignore extra schema fields; do not add `source`/`ts` columns.
- Dedup via central md5 store (`lib/dedup.py`) and emit `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` with deterministic filename (shard + timestamp).
- Commit to `axentx/surrogate-1-training-pairs` using HF Hub write (spread across shards to respect 128/hr/repo cap).

---

## 2. Concrete steps & code snippets

### 2.1 Create new worker script

`bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py \
    --date-folder 2026-05-03 \
    --file-list file-list.json \
    --out-dir batches/public-merged
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Tuple

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from datasets import load_dataset

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa

HF_DATASET = "axentx/surrogate-1-training-pairs"
CDN_BASE = "https://huggingface.co/datasets"
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", 0))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", 16))

# Rate-limit safety: backoff on 429
def _backoff(attempt: int) -> None:
    wait = 360 if attempt == 1 else 60 * min(attempt, 10)
    print(f"[shard-{SHARD_ID}] rate-limited or transient error; sleeping {wait}s", file=sys.stderr)
    time.sleep(wait)

def list_repo_folder(date_folder: str) -> list[str]:
    """
    One-time API call (run on orchestrator) to list files in a date folder.
    Returns list of paths relative to dataset root.
    """
    import huggingface_hub

    api = huggingface_hub.HfApi(token=HF_TOKEN)
    # Use recursive=False per folder to avoid huge pagination
    files = api.list_repo_tree(
        repo_id=HF_DATASET,
        path=date_folder,
        repo_type="dataset",
        recursive=False,
    )
    # files may be dict or FileInfo depending on version
    paths = []
    for f in files:
        if isinstance(f, dict):
            if f.get("type") == "file":
                paths.append(f["path"])
        else:
            if f.type == "file":
                paths.append(f.path)
    return sorted(paths)

def cdn_download(path: str, client: httpx.Client) -> bytes:
    url = f"{CDN_BASE}/{HF_DATASET}/resolve/main/{path}"
    resp = client.get(url, timeout=30.0)
    resp.raise_for_status()
    return resp.content

def hash_slug(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()

def assign_shard(path: str, total: int) -> int:
    h = hash_slug(path)
    return int(h, 16) % total

def project_to_pair(content: bytes, path: str) -> Iterator[Tuple[str, str]]:
    """
    Project heterogeneous file to (prompt, response) pairs.
    Avoids mixed-schema pyarrow CastError by not using load_dataset(streaming=True)
    on heterogeneous repo.
    """
    # Try parquet first
    if path.endswith(".parquet"):
        try:
            table = pq.read_table(pa.BufferReader(content))
            cols = set(table.column_names)
            # Heuristic projection
            prompt_col = next((c for c in ("prompt", "input", "question") if c in cols), None)
            response_col = next((c for c in ("response", "output", "answer") if c in cols), None)
            if prompt_col and response_col:
                prompts = table.column(prompt_col).to_pylist()
                responses = table.column(response_col).to_pylist()
                for p, r in zip(prompts, responses):
                    if isinstance(p, str) and isinstance(r, str):
                        yield p.strip(), r.strip()
                return
        except Exception:
            pass

    # Fallback: try JSON/JSONL
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer")
            if isinstance(prompt, str) and isinstance(response, str):
                yield prompt.strip(), response.strip()
        elif isinstance(obj, list) and len(obj) >= 2:
            p, r = obj[0], obj[1]
            if isinstance(p, str) and isinstance(r, str):
                yield p.strip(), r.strip()

def build_manifest(date_folder: str, out_path: Path) -> list[str]:
    paths = list_repo_folder(date_folder)
    out_path.write_text(json.dumps(paths, indent=2))
    print(f"Wrote manifest with {len(paths)} files to {out_path}")
    return paths

def run_shard(paths: list[str], date_folder: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    out_file = out_dir / f"shard{SHARD_ID}-{ts}.jsonl"
    dedup = DedupStore()

    assigned = [p for p in paths if assign_shard(p, SHARD_TOTAL) == SHARD_ID]
    print(f"[shard-{SHARD_ID}] assigned {len(assigned)} files out of {len(paths)}")

    client = httpx.Client(timeout=30.0)
    written = 0
    failed = 0

    for path in assigned:
        attempt = 0
        while attempt < 3:
            try:
                raw = cdn_download(path, client)
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    _backoff(attempt + 1)
                    attempt += 1
                    continue
                print(f"[shard-{SHARD_ID}] failed {path}: {e}", file=sys.stderr)
                failed += 1
                break
            except Exception as e:
                print(f"[shard-{SHARD_ID}] failed {path}: {e}", file=sys.stderr)
                failed += 1
                break
        else:
            failed += 1
            continue

        for prompt, response in project_to_pair(raw, path):
            # Central dedup by content hash
            md5 = hashlib.md5(f"{prompt}\n{response}".encode()).hexdigest()
            if dedup.exists(md5):
                continue
            dedup.add(md5)

            item = {"prompt": prompt, "response": response

# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single API call** from the runner to list one date folder via `list_repo_tree(path, recursive=False)` and saves the file list to `manifest.json`.
- Embeds the manifest in the runner; during execution each shard deterministically hashes `slug` → bucket `hash(slug) % SHARD_TOTAL` and only processes its slice.
- Downloads assigned files **via HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header to bypass `/api/` rate limits.
- Projects each file to `{prompt, response}` at parse time (avoids `load_dataset(streaming=True)` schema issues).
- Deduplicates via the existing `lib/dedup.py` central md5 store.
- Emits `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` with one JSON object per line.
- Exits with non-zero on unrecoverable errors; logs structured JSON for Actions.

### Steps (est. 90–110 min)

1. Create `bin/dataset-enrich.py` (main worker) — 40 min.
2. Add small util `bin/list_files.py` (optional: generate `manifest.json` for reuse) — 10 min.
3. Update `.github/workflows/ingest.yml` to use Python and pass matrix vars — 15 min.
4. Add `requirements-dev.txt` additions if needed (`requests`, `tqdm`, `pyarrow`) — 5 min.
5. Smoke test locally with mocked HF repo structure — 20–30 min.

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.

Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE_FOLDER=2026-05-03 python bin/dataset-enrich.py

Behavior:
- Lists files in axentx/surrogate-1-training-pairs:{DATE_FOLDER}/
- Shards by hash(slug) % SHARD_TOTAL
- Downloads via HF CDN (no auth) to bypass /api/ rate limits
- Projects to {prompt,response} per file
- Deduplicates via lib.dedup
- Outputs batches/public-merged/{DATE_FOLDER}/shard{N}-{TS}.jsonl
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

import requests
from tqdm import tqdm

# local
from lib.dedup import is_duplicate, record_hash  # type: ignore

REPO = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO}/resolve/main"
API_BASE = f"https://huggingface.co/api/datasets/{REPO}/tree"

OUT_ROOT = Path("batches/public-merged")
HF_TOKEN = os.getenv("HF_TOKEN", "")  # used only for list_repo_tree when no file-list provided

session = requests.Session()
# CDN requests: no auth header (bypass rate limits)
# API requests (rare): optionally use HF_TOKEN
if HF_TOKEN:
    session.headers.update({"Authorization": f"Bearer {HF_TOKEN}"})


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def list_files(date_folder: str) -> List[str]:
    """
    List files in {date_folder}/ (non-recursive).
    Prefer manifest.json if present; otherwise call HF API once.
    """
    manifest_path = Path("manifest.json")
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text())
            if isinstance(data, list):
                return [p for p in data if str(p).startswith(f"{date_folder}/")]
        except Exception:
            pass

    # single API call (non-recursive)
    resp = session.get(API_BASE, params={"path": date_folder, "recursive": False})
    if resp.status_code == 429:
        print(json.dumps({"event": "rate_limit", "retry_after": 360}))
        sys.exit(1)
    resp.raise_for_status()
    tree = resp.json()
    paths = [item["path"] for item in tree if item.get("type") == "file"]
    # optionally cache for reuse
    try:
        manifest_path.write_text(json.dumps(paths, indent=2))
    except Exception:
        pass
    return paths


def shard_for(path: str, shard_total: int, shard_id: int) -> bool:
    """Deterministic shard assignment by path hash."""
    h = int(_sha256_hex(path), 16)
    return (h % shard_total) == shard_id


def project_to_pair(raw: Dict[str, Any], path: str) -> Dict[str, str] | None:
    """
    Project heterogeneous file to {prompt, response}.
    Returns None if projection fails.
    """
    # Common patterns observed:
    # - JSON/JSONL with 'prompt'/'response' or 'input'/'output' or 'instruction'/'answer'
    # - Parquet rows projected by upstream; here we handle dict-like payloads.
    if not isinstance(raw, dict):
        return None

    prompt_keys = ("prompt", "instruction", "input", "question", "query")
    response_keys = ("response", "answer", "output", "completion", "result")

    prompt = None
    response = None

    for k in prompt_keys:
        if k in raw and isinstance(raw[k], str) and raw[k].strip():
            prompt = raw[k].strip()
            break
    for k in response_keys:
        if k in raw and isinstance(raw[k], str) and raw[k].strip():
            response = raw[k].strip()
            break

    if prompt is None or response is None:
        # fallback: try to pick first/second text-like fields
        text_fields = [v for v in raw.values() if isinstance(v, str) and v.strip()]
        if len(text_fields) >= 2:
            prompt, response = text_fields[0].strip(), text_fields[1].strip()
        else:
            return None

    return {"prompt": prompt, "response": response}


def download_cdn(path: str) -> bytes:
    url = f"{BASE_CDN}/{path}"
    r = session.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def parse_file(content: bytes, path: str) -> Iterable[Dict[str, str]]:
    """Parse file content into prompt/response pairs."""
    from io import BytesIO

    ext = Path(path).suffix.lower()

    if ext == ".jsonl":
        for line in content.decode().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            pair = project_to_pair(obj, path)
            if pair:
                yield pair
        return

    if ext == ".json":
        try:
            data = json.loads(content.decode())
        except Exception:
            return
        if isinstance(data, list):
            for item in data:
                pair = project_to_pair(item, path)
                if pair:
                    yield pair
        elif isinstance(data, dict):
            pair = project_to_pair(data, path)
            if pair:
                yield pair
        return

    # For parquet, rely on pyarrow only if available; otherwise skip.
    # Note: we avoid load_dataset(streaming=True) per training pattern.
    try:
        import pyarrow.parquet as pq
        table = pq.read_table(BytesIO(content))
        df = table.to_pandas()
        for _, row in df.iterrows():
            pair = project_to_pair(row.to_dict(), path)
            if pair:
                yield pair
        return
    except Exception:
        pass

    # Unknown format: skip
    return


def run() -> None:
   

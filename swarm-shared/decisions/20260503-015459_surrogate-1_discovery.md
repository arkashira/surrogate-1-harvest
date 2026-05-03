# surrogate-1 / discovery

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Uses a **single `list_repo_tree` snapshot** (JSON manifest) generated once per date on the Mac orchestrator and committed to the repo (or passed via `MANIFEST_URL`).  
- Each GitHub Actions shard (`SHARD_ID=0..15`) loads the manifest, keeps only its deterministic slice (`hash(slug) % 16 == SHARD_ID`), then downloads those files **directly via HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with **no HF API calls during ingestion** → bypasses 429 rate limits.  
- Projects each file to `{prompt, response}` at parse time (avoids pyarrow schema errors), produces `{prompt, response, source_file, shard, ts}` rows, and streams output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.  
- Keeps `lib/dedup.py` as-is (central md5 store) but calls it per row to skip duplicates before write.  
- Adds proper Bash shebang + `chmod +x` wrapper (`bin/run-worker.sh`) for cron/matrix invocation and sets `SHELL=/bin/bash` in workflow.  

### Steps (timed)

1. **Create `bin/dataset-enrich.py`** (core worker) — 45m  
2. **Create `bin/run-worker.sh`** (wrapper with shebang + exec) — 10m  
3. **Update `.github/workflows/ingest.yml`** to use wrapper + pass manifest — 20m  
4. **Add `MANIFEST_PATH`/`MANIFEST_URL` support + date folder logic** — 15m  
5. **Test locally + adjust dedup usage** — 20m  
6. **Commit & push** — 10m  

Total: ~2h.

---

## `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=3 bin/run-worker.sh 2025-12-01 manifest.json

Behavior:
- Loads manifest JSON listing files under the target date folder.
- Keeps files where hash(slug) % 16 == SHARD_ID.
- Downloads via HF CDN (no Authorization header) to bypass API rate limits.
- Projects each file to {prompt, response} at parse time.
- Dedups via lib.dedup and streams output to batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl
"""

import json
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import httpx  # streaming HTTP
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import is_duplicate, mark_seen  # type: ignore

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1-training-pairs")
CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main"
OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT", "batches/public-merged"))

# Optional proxy/timeouts for robustness
HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
MAX_RETRIES = 3
RETRY_BACKOFF = 5.0

def slug_hash(slug: str) -> int:
    """Deterministic 32-bit hash for sharding."""
    return int(hashlib.md5(slug.encode("utf-8")).hexdigest(), 16) & 0xFFFFFFFF

def shard_match(slug: str, shard_id: int, total_shards: int = 16) -> bool:
    return (slug_hash(slug) % total_shards) == shard_id

def load_manifest(manifest_path: Path) -> Dict[str, Any]:
    with manifest_path.open("r", encoding="utf-8") as f:
        return json.load(f)

def cdn_url(repo: str, path: str) -> str:
    return f"{CDN_BASE}/{path.lstrip('/')}"

def stream_download(url: str) -> Iterable[bytes]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with httpx.stream("GET", url, timeout=HTTP_TIMEOUT, follow_redirects=True) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes(chunk_size=8192):
                    yield chunk
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                # File disappeared — skip
                print(f"SKIP 404: {url}", file=sys.stderr)
                return
            if attempt == MAX_RETRIES:
                raise
        except (httpx.NetworkError, httpx.TimeoutException) as exc:
            if attempt == MAX_RETRIES:
                raise
        time.sleep(RETRY_BACKOFF * attempt)
    # Should not reach
    raise RuntimeError(f"Failed to download after retries: {url}")

def parse_file_to_rows(content: bytes, source_file: str) -> Iterable[Dict[str, Any]]:
    """
    Parse parquet/JSONL content and project to {prompt, response}.
    Handles mixed schemas by reading with pyarrow and extracting
    text-like fields heuristically.
    """
    ts = datetime.now(timezone.utc).isoformat()

    # Try parquet first
    try:
        table = pq.read_table(pa.BufferReader(content))
        cols = table.column_names
        # Heuristic: find prompt/response or text columns
        prompt_col = next((c for c in cols if "prompt" in c.lower()), None)
        response_col = next((c for c in cols if "response" in c.lower() or "completion" in c.lower()), None)

        # If not found, pick first two string-like columns
        if prompt_col is None or response_col is None:
            str_cols = [c for c in cols if pa.types.is_string(table.schema.field(c).type)]
            if len(str_cols) >= 2:
                prompt_col, response_col = str_cols[0], str_cols[1]
            elif len(str_cols) == 1:
                prompt_col, response_col = str_cols[0], str_cols[0]
            else:
                # fallback: first two columns cast to string
                prompt_col, response_col = cols[0], cols[1] if len(cols) > 1 else cols[0]

        prompts = table.column(prompt_col).to_pylist()
        responses = table.column(response_col).to_pylist()
        for p, r in zip(prompts, responses):
            if p is None or r is None:
                continue
            yield {
                "prompt": str(p),
                "response": str(r),
                "source_file": source_file,
                "shard": None,  # filled by worker
                "ts": ts,
            }
        return
    except Exception:
        pass

    # Try JSONL lines
    try:
        text = content.decode("utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            # Heuristic field selection
            prompt = obj.get("prompt") or obj.get("input") or obj.get("text") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer") or obj.get("completion")
            if prompt is None or response is None:
                # pick first two values if dict-like
                vals = list(obj.values())
                if len(vals) >= 2:
                    prompt, response = vals[0], vals[1]
                else:
                    continue
            yield {
                "prompt": str(prompt),
                "response": str(response),
                "source_file": source_file,
                "shard": None,
                "ts": ts,
            }
        return
    except Exception:
        pass

    # Fallback: skip
    print(f"SKIP unparsable: {source_file}", file=sys.stderr)
    return

def worker(date_folder: str, manifest_path: Path, shard

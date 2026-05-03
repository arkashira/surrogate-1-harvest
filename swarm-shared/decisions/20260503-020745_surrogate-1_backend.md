# surrogate-1 / backend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac-side HF API call) to deterministically assign 1/16 of repo files per shard (no recursive listing during ingestion).
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with zero Authorization header to avoid 429 rate limits.
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas).
- Deduplicates via `lib/dedup.py` central md5 store.
- Writes output as `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` with deterministic shard + timestamp to prevent collisions.
- Runs entirely on GitHub Actions (isolated 7 GB per shard) while training uses Lightning Studio with CDN-only fetches from the produced dataset.

---

### 1. Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1.
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
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
from typing import Any, Dict, List

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import numpy as np

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

HF_DATASETS_CDN = "https://huggingface.co/datasets"
RETRY_WAIT = 360  # seconds after 429
TIMEOUT = 60.0

def hash_slug(s: str) -> int:
    """Deterministic 32-bit hash for shard assignment."""
    return int(hashlib.md5(s.encode()).hexdigest(), 16) & 0xFFFFFFFF

def assign_to_shard(slug: str, shard_total: int) -> int:
    return hash_slug(slug) % shard_total

def load_file_list(path: Path) -> List[str]:
    with path.open() as f:
        data = json.load(f)
    if isinstance(data, dict) and "files" in data:
        return [f["path"] for f in data["files"]]
    return [str(p) for p in data]

def cdn_download_url(repo: str, file_path: str) -> str:
    return f"{HF_DATASETS_CDN}/{repo}/resolve/main/{file_path}"

def safe_request(client: httpx.Client, url: str) -> bytes:
    for attempt in range(3):
        try:
            resp = client.get(url, timeout=TIMEOUT, follow_redirects=True)
            if resp.status_code == 429:
                wait = RETRY_WAIT
                print(f"429 rate-limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                print(f"File not found: {url}", file=sys.stderr)
                raise
            raise
        except (httpx.RequestError, httpx.HTTPError) as exc:
            print(f"Request error (attempt {attempt+1}): {exc}", file=sys.stderr)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to download after retries: {url}")

def extract_pair(raw: bytes, file_path: str) -> Dict[str, str]:
    """
    Project heterogeneous file to {prompt, response} only.
    Supports:
      - Parquet with any schema: looks for prompt/response or text/completion.
      - JSON/JSONL with prompt/response fields.
    """
    suffix = Path(file_path).suffix.lower()
    try:
        if suffix == ".parquet":
            table = pq.read_table(pa.BufferReader(raw))
            cols = table.column_names
            # Try common mappings
            prompt_col = next((c for c in cols if "prompt" in c.lower()), None)
            response_col = next((c for c in cols if "response" in c.lower() or "completion" in c.lower()), None)
            if prompt_col is None or response_col is None:
                # Fallback: first two string columns
                str_cols = [c for c in cols if table.schema.field(c).type in (pa.string(), pa.large_string())]
                if len(str_cols) >= 2:
                    prompt_col, response_col = str_cols[0], str_cols[1]
                else:
                    raise ValueError(f"Cannot find prompt/response in {file_path}")
            prompt = table.column(prompt_col).to_pylist()[0]
            response = table.column(response_col).to_pylist()[0]
            return {"prompt": str(prompt), "response": str(response)}
        else:
            # Assume JSON/JSONL
            text = raw.decode("utf-8", errors="replace").strip()
            if "\n" in text:
                lines = [ln for ln in text.splitlines() if ln.strip()]
                obj = json.loads(lines[0])
            else:
                obj = json.loads(text)
            if "prompt" in obj and "response" in obj:
                return {"prompt": str(obj["prompt"]), "response": str(obj["response"])}
            if "text" in obj and "completion" in obj:
                return {"prompt": str(obj["text"]), "response": str(obj["completion"])}
            # Fallback: use first two string values
            vals = [v for v in obj.values() if isinstance(v, str)]
            if len(vals) >= 2:
                return {"prompt": vals[0], "response": vals[1]}
            raise ValueError(f"Cannot extract pair from {file_path}")
    except Exception as exc:
        raise ValueError(f"Failed to extract pair from {file_path}: {exc}")

def build_record(pair: Dict[str, str], file_path: str) -> Dict[str, Any]:
    content = f"{pair['prompt']}\n\n{pair['response']}"
    md5 = hashlib.md5(content.encode()).hexdigest()
    return {
        "prompt": pair["prompt"],
        "response": pair["response"],
        "md5": md5,
        "source_file": file_path,
    }

def main() -> None:
    parser = argparse.ArgumentParser(description="CDN-bypass dataset worker")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--file-list", type=Path, default=Path("file-list.json"))
    parser.add_argument("--out-dir", type=Path, default=Path("batches/public-merged"))
    args = parser.parse_args()

    shard_id = int(os.environ.get("SHARD_ID", 0))
    shard_total = int(os.environ.get("SHARD_TOTAL", 16))
    if not (0 <= shard_id < shard_total):
        print(f"Invalid SHARD_ID={shard_id} SHARD_TOTAL={shard_total}", file=sys.stderr)
        sys.exit(1)

    file_list = load_file_list(args.file_list)
    assigned = [p for p in file_list if assign_to_shard(p, shard_total) == shard_id]
    print(f"Shard {shard_id}/{shard_total}: processing {len(assigned)} files")

    dedup = DedupStore()
    out_dir = args.out_dir / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
    out_path = out_dir / f"shard{shard_id}-{timestamp}.jsonl"

    client = httpx.Client(
        headers={"User-Agent": "axentx-surrogate-ingest/1.0"},
        timeout=TIMEOUT,
        follow_redirects

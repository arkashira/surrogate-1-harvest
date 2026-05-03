# surrogate-1 / discovery

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac-side, one `list_repo_tree` call per date folder) to deterministically shard file paths by `hash(slug) % SHARD_TOTAL`.
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header → avoids 429 API limits during training data load.
- Projects each file to `{prompt, response}` only at parse time (avoids `pyarrow.CastError` from mixed schemas).
- Deduplicates via central `lib/dedup.py` md5 store (same as existing).
- Writes normalized pairs to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` (one object per line, no extra metadata columns).
- Keeps the shell wrapper (`dataset-enrich.sh`) as a thin executable shim that invokes `python3 bin/dataset-enrich.py "$@"` with proper shebang and `set -euo pipefail`.

### Steps (timed)

1. Create `bin/dataset-enrich.py` (≈45 min) — manifest loading, CDN fetch, schema projection, dedup, output.
2. Update `bin/dataset-enrich.sh` (≈5 min) — shebang, executable, forward args.
3. Add `requirements.txt` extras if needed (`requests`, `tqdm`) (≈5 min).
4. Quick smoke test locally (≈15 min) — single shard on a small file list.
5. Commit and verify GitHub Actions still triggers (matrix unchanged) (≈10 min).

---

## Code Snippets

### bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python3 bin/dataset-enrich.py \
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
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

# Local dedup module (existing)
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

HF_DATASETS_CDN = "https://huggingface.co/datasets"
DEFAULT_CHUNK_SIZE = 8192
RETRY_WAIT = 360  # seconds after 429
MAX_RETRIES = 3


def shard_for(slug: str, total: int) -> int:
    """Deterministic shard assignment."""
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()
    return int(digest, 16) % total


def cdn_download(url: str, dest: Path) -> bool:
    """Download via HF CDN (no auth). Returns True on success."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                if r.status_code == 429:
                    wait = RETRY_WAIT
                    print(f"Rate-limited 429. Waiting {wait}s (attempt {attempt})", file=sys.stderr)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=DEFAULT_CHUNK_SIZE):
                        f.write(chunk)
            return True
        except Exception as exc:
            if attempt == MAX_RETRIES:
                print(f"Failed to download {url}: {exc}", file=sys.stderr)
                return False
            time.sleep(5 * attempt)
    return False


def project_to_pair(obj: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous file record to {prompt, response}.
    Returns None if unusable.
    """
    # Common surrogate-1 conventions (adjust if needed)
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
    response = obj.get("response") or obj.get("output") or obj.get("answer")
    if prompt is None or response is None:
        return None
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}


def extract_from_parquet(path: Path) -> Iterable[Dict[str, str]]:
    """Stream rows from parquet and project to pairs."""
    try:
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=1024):
            table = pa.Table.from_batches([batch])
            for col in table.column_names:
                # coerce to utf8 to avoid CastError downstream
                if not pa.types.is_string(table.schema.field(col).type):
                    table = table.set_column(
                        table.schema.get_field_index(col),
                        col,
                        table.column(col).cast(pa.string()),
                    )
            df = table.to_pylist()
            for row in df:
                pair = project_to_pair(row)
                if pair:
                    yield pair
    except Exception as exc:
        print(f"Error reading parquet {path}: {exc}", file=sys.stderr)


def extract_from_jsonl(path: Path) -> Iterable[Dict[str, str]]:
    """Stream rows from jsonl and project to pairs."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pair = project_to_pair(row)
                if pair:
                    yield pair
    except Exception as exc:
        print(f"Error reading jsonl {path}: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="CDN-bypass surrogate-1 ingest worker")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Date folder in dataset (e.g. 2026-05-03)")
    parser.add_argument("--file-list", required=True, help="Path to file-list.json")
    parser.add_argument("--out-dir", default="batches/public-merged")
    parser.add_argument("--shard-id", type=int, default=int(os.environ.get("SHARD_ID", 0)))
    parser.add_argument("--shard-total", type=int, default=int(os.environ.get("SHARD_TOTAL", 16)))
    args = parser.parse_args()

    if args.shard_total <= 0 or args.shard_id < 0 or args.shard_id >= args.shard_total:
        print("Invalid shard settings", file=sys.stderr)
        return 1

    file_list_path = Path(args.file_list)
    if not file_list_path.is_file():
        print(f"file-list not found: {file_list_path}", file=sys.stderr)
        return 1

    with open(file_list_path, "r", encoding="utf-8") as f:
        file_list = json.load(f)

    if not isinstance(file_list, list):
        print("file-list.json must be a list of relative paths", file=sys.stderr)
        return 1

    # Deterministic shard assignment by slug (filename without extension)
    assigned = []
    for rel in file_list:
        slug = Path(rel).stem
        if shard_for(slug, args.shard_total) == args.shard_id:
            assigned.append(rel)

    print(f"Shard {args.shard_id}/{args.shard_total}: processing {len(assigned)} files", file=sys.stderr)

    dedup = DedupStore()
    out_dir = Path(args.out_dir) / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%H%M%S")
    out_file = out_dir / f"shard{args.shard_id}-{

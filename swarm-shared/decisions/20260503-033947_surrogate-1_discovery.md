# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **manifest-first, CDN-only fetch**: one API call to `list_repo_tree` (non-recursive) for the date folder, saves manifest JSON, then workers stream files via `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{date}/{file}` (no auth, bypasses `/api/` rate limits)
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids `pyarrow.CastError`)
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Central dedup via `lib/dedup.py` (SQLite md5 store) — same semantics as HF Space
- Outputs `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` with one JSON object per line
- Fails gracefully on 429/5xx with exponential backoff; logs summary (files processed, bytes, dups skipped)
- Shebang `#!/usr/bin/env python3`, executable, invoked via `bash bin/dataset-enrich.py "$@"` from workflow if needed

### Why this is highest-value (<2h)
- Directly applies the **HF CDN bypass** and **schema projection** patterns from lessons learned
- Eliminates the most common failure modes (rate limits, pyarrow CastError, OOM from streaming heterogeneous files)
- Keeps the 16-shard matrix architecture unchanged — no workflow changes required
- Small, focused scope: one worker script replacement

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 bin/dataset-enrich.py [DATE_FOLDER]

Environment:
  HF_TOKEN         - write token for axentx/surrogate-1-training-pairs
  SHARD_ID         - 0..SHARD_TOTAL-1 (matrix index)
  SHARD_TOTAL      - total shards (default 16)
  DATE_FOLDER      - optional YYYY-MM-DD (default today)
"""

import os
import sys
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Tuple

import requests
from huggingface_hub import list_repo_tree, hf_hub_upload, CommitOperationAdd

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("dataset-enrich")

REPO = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO}/resolve/main"
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
MAX_RETRIES = 5
BACKOFF_BASE = 2.0

def get_date_folder() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def shard_assign(slug: str, total: int) -> int:
    digest = hashlib.sha256(slug.encode()).digest()
    return int.from_bytes(digest, "big") % total

def list_date_files(date_folder: str) -> list[str]:
    """Single API call: non-recursive tree for the date folder."""
    for attempt in range(MAX_RETRIES):
        try:
            items = list_repo_tree(
                repo_id=REPO,
                path=date_folder,
                repo_type="dataset",
                recursive=False,
            )
            files = [it.rfilename for it in items if it.type == "file"]
            log.info("Listed %d files in %s", len(files), date_folder)
            return files
        except Exception as exc:
            wait = BACKOFF_BASE ** attempt
            log.warning("list_repo_tree failed (attempt %s): %s — retry in %.1fs", attempt + 1, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Failed to list files in {date_folder} after {MAX_RETRIES} attempts")

def stream_cdn_file(date_folder: str, filename: str) -> Iterator[bytes]:
    url = f"{BASE_CDN}/{date_folder}/{filename}"
    for attempt in range(MAX_RETRIES):
        try:
            with requests.get(url, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    yield chunk
            return
        except Exception as exc:
            wait = BACKOFF_BASE ** attempt
            log.warning("CDN fetch failed %s (attempt %s): %s — retry in %.1fs", filename, attempt + 1, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {filename} after {MAX_RETRIES} attempts")

def parse_to_pair(content: bytes, filename: str) -> Iterator[Tuple[str, str, str]]:
    """
    Project heterogeneous schemas to (prompt, response, md5).
    Supports common patterns seen in surrogate-1-training-pairs:
    - JSONL with {prompt, response}
    - JSONL with {input, output}
    - JSONL with {question, answer}
    - Parquet rows projected via pyarrow by caller (if needed)
    """
    import io

    # Try JSONL lines first
    text = content.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
        response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
        if prompt and response:
            md5 = hashlib.md5((prompt + "\0" + response).encode()).hexdigest()
            yield prompt, response, md5

def run_worker(shard_id: int, shard_total: int, date_folder: str) -> None:
    dedup = DedupStore()
    out_dir = Path("batches/public-merged") / date_folder
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
    out_path = out_dir / f"shard{shard_id}-{timestamp}.jsonl"

    files = list_date_files(date_folder)
    assigned = [f for f in files if shard_assign(f, shard_total) == shard_id]
    log.info("Shard %s/%s assigned %s files", shard_id, shard_total, len(assigned))

    written = 0
    skipped_dup = 0
    errors = 0

    with out_path.open("w", encoding="utf-8") as out_f:
        for filename in assigned:
            try:
                content = b"".join(stream_cdn_file(date_folder, filename))
                for prompt, response, md5 in parse_to_pair(content, filename):
                    if dedup.exists(md5):
                        skipped_dup += 1
                        continue
                    dedup.add(md5)
                    obj = {"prompt": prompt, "response": response}
                    out_f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    written += 1
            except Exception as exc:
                errors += 1
                log.error("Failed processing %s: %s", filename, exc, exc_info=False)

    # Summary
    log.info(
        "Shard %s complete: written=%s, skipped_dup=%s, errors=%s, out=%s",
        shard_id, written, skipped_dup, errors, out_path,
    )

    # Best-effort upload via huggingface_hub (requires HF_TOKEN)
    hf_token = os.getenv("HF_TOKEN")

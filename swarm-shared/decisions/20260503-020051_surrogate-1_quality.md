# surrogate-1 / quality

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from the GitHub Actions matrix.
- Loads a pre-generated `manifest-YYYYMMDD.json` (produced once per day by a Mac orchestrator after `list_repo_tree`) containing all file paths to ingest for that date.
- Assigns each file to a deterministic shard: `hash(slug) % SHARD_TOTAL`.
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header to avoid API rate limits.
- Projects each file to `{prompt, response}` only at parse time (avoids `pyarrow.CastError` on mixed schemas).
- Deduplicates via the existing `lib/dedup.py` central md5 store.
- Writes output as `batches/public-merged/YYYY-MM-DD/shardN-HHMMSS.jsonl`.
- Keeps the old shell wrapper (`bin/dataset-enrich.sh`) as a thin executable shim that invokes `python3 bin/dataset-enrich.py "$@"` with proper shebang and `set -euo pipefail`.

### Steps (est. 90–110 min)

1. Create `bin/dataset-enrich.py` (60–75 min)
   - Deterministic sharding, CDN fetches, schema projection, dedup, output.
   - Retry/backoff for CDN 429/5xx; respect HF CDN limits.
   - Stream files line-by-line to bound memory.
2. Replace `bin/dataset-enrich.sh` with a shim (5 min)
   - Shebang `#!/usr/bin/env bash`, `set -euo pipefail`, exec python.
3. Update workflow if needed (10 min)
   - Ensure matrix passes `SHARD_ID`/`SHARD_TOTAL`; optionally add manifest generation step in a separate workflow.
4. Smoke test (15–20 min)
   - Run locally with a small manifest subset; verify output and dedup.

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --manifest manifest-20260503.json \
    --out-dir batches/public-merged

Environment:
  HF_TOKEN         Optional — if absent, CDN downloads are unauthenticated
                   (recommended to avoid API rate limits).
  SHARD_ID         0-based shard index.
  SHARD_TOTAL      Total shards (default 16).
"""

import json
import os
import sys
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

import requests
from tqdm import tqdm

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import is_duplicate, record_hash  # type: ignore

HF_DATASETS_CDN = "https://huggingface.co/datasets"
RETRY_BACKOFF = [1, 2, 4, 8, 16]
MAX_RETRIES = len(RETRY_BACKOFF)


def shard_for_slug(slug: str, total: int) -> int:
    """Deterministic shard assignment."""
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()
    return int(digest, 16) % total


def cdn_download(url: str, headers: Optional[Dict[str, str]] = None) -> bytes:
    """Download via HF CDN with retries/backoff."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 429:
                wait = RETRY_BACKOFF[attempt]
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except (requests.RequestException, requests.Timeout) as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(RETRY_BACKOFF[attempt])
    raise RuntimeError(f"Failed to download {url}")


def normalize_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Adjust heuristics here per known dataset variants.
    """
    # Common field names seen across ingested sources
    prompt_keys = {"prompt", "instruction", "input", "question", "user"}
    response_keys = {"response", "output", "answer", "assistant", "completion"}

    prompt = None
    response = None

    for k, v in raw.items():
        if v is None:
            continue
        nk = k.strip().lower()
        if nk in prompt_keys and prompt is None:
            prompt = str(v).strip()
        elif nk in response_keys and response is None:
            response = str(v).strip()

    # Fallbacks
    if prompt is None:
        prompt = raw.get("text", raw.get("content", "")).strip()
    if response is None and "completion" in raw:
        response = str(raw["completion"]).strip()

    # If still ambiguous, prefer first/second string fields
    if not prompt or not response:
        str_vals = [str(v).strip() for v in raw.values() if isinstance(v, str) and v.strip()]
        if len(str_vals) >= 2 and not prompt:
            prompt = str_vals[0]
        if len(str_vals) >= 2 and not response:
            response = str_vals[1]
        elif len(str_vals) == 1:
            if not prompt:
                prompt = str_vals[0]
            if not response:
                response = ""

    return {
        "prompt": prompt or "",
        "response": response or "",
    }


def file_slug_from_path(repo: str, path: str) -> str:
    """Stable slug for dedup: repo + path."""
    return f"{repo}/{path}"


def hash_content(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


def process_file(
    repo: str,
    path: str,
    out_f,
    stats: Dict[str, int],
) -> None:
    """Download, parse, dedup, and write entries for one file."""
    slug = file_slug_from_repo_path(repo, path)
    url = f"{HF_DATASETS_CDN}/{repo}/resolve/main/{path}"

    # CDN bypass: no Authorization header by default
    headers = {}
    # If HF_TOKEN is set, some private datasets may require it; but prefer no auth for public CDN.
    # token = os.getenv("HF_TOKEN")
    # if token:
    #     headers["Authorization"] = f"Bearer {token}"

    try:
        raw_bytes = cdn_download(url, headers=headers)
    except Exception as exc:
        stats["download_errors"] += 1
        print(f"[WARN] failed to download {url}: {exc}", file=sys.stderr)
        return

    content_hash = hash_content(raw_bytes)
    if is_duplicate(content_hash):
        stats["deduped"] += 1
        return

    # Try parse as JSON lines first; fallback to single JSON; else skip.
    lines = raw_bytes.decode("utf-8", errors="replace").strip().splitlines()
    written = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            # If file is a single JSON object, try once at file level
            try:
                raw = json.loads(raw_bytes.decode("utf-8", errors="replace"))
            except Exception:
                stats["parse_errors"] += 1
                return
            else:
                # treat as single record
                raw = [raw]
                break
        else:
            raw = [raw]
            break

    for raw_item in raw if isinstance(raw, list) else [raw]:
        try:
            pair = normalize_pair(raw_item)
        except Exception:
            stats["parse_errors"] += 1
           

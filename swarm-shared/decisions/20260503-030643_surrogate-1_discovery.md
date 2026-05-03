# surrogate-1 / discovery

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env
- Uses **one** `list_repo_tree` call (per DATE folder) → saves JSON manifest locally
- Downloads only assigned shard’s files via **CDN bypass** (`resolve/main/...`) — zero API calls during data load
- Projects heterogeneous schemas to `{prompt, response}` at parse time (avoids pyarrow CastError)
- Dedups via centralized SQLite md5 store (existing `lib/dedup.py`)
- Writes `batches/public-merged/<DATE>/shard<N>-<HHMMSS>.jsonl` with deterministic naming
- Exits non-zero on unrecoverable errors; logs structured JSON for Actions

### Steps (est. 90 min)

1. Create `bin/dataset-enrich.py` (60 min)
2. Update `.github/workflows/ingest.yml` to invoke via `python bin/dataset-enrich.py` (10 min)
3. Smoke-test locally with mock env (20 min)

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx python bin/dataset-enrich.py
"""

import os
import sys
import json
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Iterable

import requests
from huggingface_hub import HfApi, hf_hub_download

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import is_duplicate, mark_seen  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("surrogate-ingest")

# ---- config ----
REPO_ID = os.getenv("HF_REPO_ID", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    log.error("HF_TOKEN required")
    sys.exit(1)

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

API = HfApi(token=HF_TOKEN)
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}
CDN_ROOT = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

OUT_DIR = Path("batches/public-merged") / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)
TS = datetime.now(timezone.utc).strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TS}.jsonl"

# ---- helpers ----
def list_date_files(date: str) -> list[str]:
    """Single API call: list files under date/ (non-recursive)."""
    try:
        items = API.list_repo_tree(repo_id=REPO_ID, path=date, recursive=False)
    except Exception as exc:
        log.error("list_tree_failed", exc_info=exc)
        raise
    # Expect files like 2026-04-29/slug.parquet or .jsonl
    files = [it.rfilename for it in items if it.type == "file"]
    log.info("listed_files", date=date, count=len(files))
    return sorted(files)

def shard_filter(files: list[str]) -> list[str]:
    """Deterministic shard assignment by slug hash."""
    assigned = []
    for f in files:
        slug = Path(f).stem  # e.g. abc123 from abc123.parquet
        h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
        if h % SHARD_TOTAL == SHARD_ID:
            assigned.append(f)
    log.info("shard_assigned", shard=SHARD_ID, total=len(assigned))
    return assigned

def download_cdn(path: str) -> bytes:
    """Download via CDN (no auth counted against API rate limit)."""
    url = f"{CDN_ROOT}/{path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Known schemas handled; unknown -> best-effort.
    """
    # Common keys seen in public datasets
    prompt_keys = {"prompt", "instruction", "input", "question", "user"}
    response_keys = {"response", "output", "answer", "assistant", "completion"}

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

    # Fallback: try to find any string fields
    if not prompt or not response:
        strs = [v for v in raw.values() if isinstance(v, str) and v.strip()]
        if len(strs) >= 2:
            prompt, response = strs[0], strs[1]

    if not prompt or not response:
        raise ValueError("Cannot project to prompt/response")

    return {"prompt": prompt, "response": response}

def parse_file(path: str) -> Iterable[Dict[str, str]]:
    """Download and parse one file; yield projected pairs."""
    data = download_cdn(path)
    suffix = Path(path).suffix.lower()

    if suffix == ".jsonl":
        import io
        for line in io.BytesIO(data).read().splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            yield project_to_pair(raw)
    elif suffix == ".parquet":
        import pyarrow.parquet as pq
        import pyarrow as pa
        table = pq.read_table(pa.BufferReader(data))
        # Project only needed columns if present; else convert to dicts
        cols = table.column_names
        dicts = table.to_pylist()
        for raw in dicts:
            yield project_to_pair(raw)
    else:
        log.warning("unsupported_suffix", path=path, suffix=suffix)

# ---- main ----
def main() -> None:
    log.info("ingest_start", shard=SHARD_ID, date=DATE)

    files = list_date_files(DATE)
    assigned = shard_filter(files)

    written = 0
    skipped_dup = 0
    errors = 0

    with OUT_FILE.open("w", encoding="utf-8") as fout:
        for path in assigned:
            try:
                for pair in parse_file(path):
                    # Dedup by content hash
                    payload = json.dumps(pair, sort_keys=True, separators=(",", ":"))
                    md5 = hashlib.md5(payload.encode()).hexdigest()
                    if is_duplicate(md5):
                        skipped_dup += 1
                        continue

                    fout.write(payload + "\n")
                    mark_seen(md5)
                    written += 1
            except Exception as exc:
                errors += 1
                log.error("file_failed", path=path, exc_info=exc)
                # Continue processing other files

    log.info(
        "ingest_done",
        shard=SHARD_ID,
        written=written,
        skipped_dup=skipped_dup,
        errors=errors,
        out=str(OUT_FILE),
    )

    if written == 0 and errors > 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
```

### Update `.github/workflows/ingest.yml` (excerpt)

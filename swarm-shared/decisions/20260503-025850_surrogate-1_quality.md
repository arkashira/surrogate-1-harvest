# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads only assigned files via **HF CDN bypass** (`resolve/main/...`, no Authorization header)
- Projects each file to `{prompt, response}` at parse time (no schema assumptions)
- Deduplicates via central `lib/dedup.py` md5 store
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Exits 0 on success; logs counts + skipped dupes

### Why this is the highest-value incremental improvement
- Fixes the **HF API 429/rate-limit** and **pyarrow CastError** patterns from the playbook
- Eliminates `load_dataset(streaming=True)` on heterogeneous repos
- Cuts API calls during training (manifest is pre-computed)
- Keeps ingestion parallel, deterministic, and OOM-safe
- Reuses existing dedup store and output convention

---

## Code Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Environment:
  SHARD_ID          - worker index [0..SHARD_TOTAL-1]
  SHARD_TOTAL       - total parallel workers (default 16)
  DATE              - folder in axentx/surrogate-1-training-pairs to ingest
  HF_TOKEN          - HF write token (for dedup store + upload)
  DRY_RUN           - if set, skip upload
"""
import os
import sys
import json
import hashlib
import datetime
import logging
from pathlib import Path
from typing import Dict, Any, List

import requests
import pyarrow.parquet as pq
from huggingface_hub import HfApi

# Project-local dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # type: ignore

REPO_ID = "axentx/surrogate-1-training-pairs"
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("dataset-enrich")

def shard_of(slug: str, total: int) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % total

def list_date_files(date: str) -> List[str]:
    """Single API call: list top-level files in DATE/ folder."""
    api = HfApi(token=os.getenv("HF_TOKEN"))
    items = api.list_repo_tree(repo_id=REPO_ID, path=date, recursive=False)
    files = [it.rfilename for it in items if it.type == "file"]
    log.info("listed %d files in %s/", len(files), date)
    return files

def cdn_download(repo_id: str, path_in_repo: str, out_path: str) -> None:
    """Download via HF CDN (no Authorization header)."""
    url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path_in_repo}"
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                f.write(chunk)

def project_to_pair(obj: Dict[str, Any]) -> Dict[str, str]:
    """
    Best-effort projection to {prompt, response}.
    Supports common field names seen in surrogate-1 sources.
    """
    prompt_keys = {"prompt", "instruction", "input", "question", "user"}
    response_keys = {"response", "completion", "output", "answer", "assistant"}

    prompt = None
    response = None

    for k, v in obj.items():
        nk = k.strip().lower()
        if nk in prompt_keys and prompt is None:
            prompt = str(v) if v is not None else ""
        elif nk in response_keys and response is None:
            response = str(v) if v is not None else ""

    # Fallback: if only one text column exists, treat as prompt and empty response
    text_cols = [k for k, v in obj.items() if isinstance(v, str) and v.strip()]
    if prompt is None and response is None and len(text_cols) == 1:
        prompt = str(obj[text_cols[0]])
        response = ""

    return {
        "prompt": prompt or "",
        "response": response or "",
    }

def process_file(
    date: str,
    filename: str,
    dedup: DedupStore,
    out_lines: List[str],
) -> None:
    """Download, parse, dedup, append to out_lines."""
    path_in_repo = f"{date}/{filename}"
    local_parquet = Path("/tmp") / f"tmp_{hashlib.md5(path_in_repo.encode()).hexdigest()}.parquet"

    try:
        cdn_download(REPO_ID, path_in_repo, str(local_parquet))
        pf = pq.read_table(str(local_parquet))
    except Exception as exc:
        log.warning("failed to read %s: %s", path_in_repo, exc)
        return
    finally:
        if local_parquet.exists():
            local_parquet.unlink(missing_ok=True)

    # Convert to list of dicts (row-wise)
    rows = pf.to_pylist()
    accepted = 0
    duped = 0

    for row in rows:
        pair = project_to_pair(row)
        prompt = (pair.get("prompt") or "").strip()
        response = (pair.get("response") or "").strip()
        if not prompt and not response:
            continue

        content = f"{prompt}\n\n{response}".strip()
        md5 = hashlib.md5(content.encode()).hexdigest()

        if dedup.seen(md5):
            duped += 1
            continue

        dedup.add(md5)
        out_lines.append(json.dumps({"prompt": prompt, "response": response}, ensure_ascii=False))
        accepted += 1

    log.info("%s: accepted=%d duped=%d", path_in_repo, accepted, duped)

def main() -> int:
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    date = os.getenv("DATE")
    hf_token = os.getenv("HF_TOKEN")
    dry_run = os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}

    if not date:
        log.error("DATE is required")
        return 1
    if not hf_token:
        log.error("HF_TOKEN is required")
        return 1

    os.environ["HF_TOKEN"] = hf_token  # for dedup store + api

    log.info("shard %d/%d | date=%s | dry_run=%s", shard_id, shard_total, date, dry_run)

    dedup = DedupStore()
    files = list_date_files(date)

    assigned_files = [
        f for f in files if shard_of(f, shard_total) == shard_id
    ]
    log.info("assigned %d files", len(assigned_files))

    out_lines: List[str] = []
    for filename in assigned_files:
        try:
            process_file(date, filename, dedup, out_lines)
        except Exception as exc:
            log.exception("unexpected error processing %s: %s", filename, exc)

    if not out_lines:
        log.info("no new pairs to upload")
        return 0

    ts = datetime.datetime.utcnow().strftime("%H%M%S

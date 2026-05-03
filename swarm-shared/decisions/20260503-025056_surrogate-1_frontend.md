# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic shard assignment by filename hash
- Saves file-list JSON to `manifest-{DATE}-shard{SHARD_ID}.json` (so Lightning training can do CDN-only fetches with zero HF API calls)
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Deduplicates via central md5 store (`lib/dedup.py`)
- Writes output to `batches/public-merged/{DATE}/shard{SHARD_ID}-{HHMMSS}.jsonl`
- Exits non-zero on unrecoverable errors; logs structured JSON for Actions

### Code changes

```bash
# bin/dataset-enrich.py
#!/usr/bin/env python3
"""
CDN-bypass shard worker for surrogate-1 public dataset ingestion.
Usage:
  HF_TOKEN=hf_xxx \
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  python bin/dataset-enrich.py
"""

import os
import sys
import json
import hashlib
import datetime
import logging
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download

# -- config --
REPO_ID = "axentx/surrogate-1-training-pairs"
API_BASE = f"https://huggingface.co/api/datasets/{REPO_ID}"
CDN_BASE = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.date.today().isoformat())
HF_TOKEN = os.getenv("HF_TOKEN", "")
# --

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("surrogate-ingest")

# -- dedup (central store on HF Space) --
try:
    from lib.dedup import DedupStore
    dedup = DedupStore()
except Exception as exc:
    log.warning("Dedup unavailable: %s", exc)
    dedup = None

def hf_api() -> HfApi:
    return HfApi(token=HF_TOKEN) if HF_TOKEN else HfApi()

def list_date_files(date_folder: str) -> List[str]:
    """Single API call: list files in date folder (non-recursive)."""
    try:
        tree = hf_api().list_repo_tree(
            repo_id=REPO_ID,
            path=date_folder,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as exc:
        log.error("list_repo_tree failed: %s", exc)
        raise
    files = [item.rfilename for item in tree if not item.rfilename.endswith("/")]
    log.info("listed %d files in %s", len(files), date_folder)
    return files

def shard_assign(filename: str) -> int:
    """Deterministic shard by filename hash."""
    digest = hashlib.md5(filename.encode()).hexdigest()
    return int(digest, 16) % SHARD_TOTAL

def cdn_url(path: str) -> str:
    return f"{CDN_BASE}/{path}"

def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Best-effort projection to {prompt, response}.
    Keep this minimal to avoid schema/pyarrow issues.
    """
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

def download_and_process(path: str) -> List[Dict[str, str]]:
    """CDN bypass: no Authorization header."""
    url = cdn_url(path)
    log.info("downloading %s", path)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    # Try parquet first, fallback to jsonl
    out = []
    if path.endswith(".parquet"):
        import pyarrow.parquet as pq
        import io
        table = pq.read_table(io.BytesIO(resp.content))
        for batch in table.to_batches(max_chunksize=1000):
            for row in batch.to_pylist():
                out.append(project_to_pair(row))
    elif path.endswith(".jsonl"):
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out.append(project_to_pair(row))
    else:
        log.warning("unsupported file %s, skipping", path)
    return out

def md5_hash(obj: Dict[str, str]) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.md5(payload).hexdigest()

def main() -> None:
    if SHARD_ID < 0 or SHARD_ID >= SHARD_TOTAL:
        log.error("invalid SHARD_ID=%d (SHARD_TOTAL=%d)", SHARD_ID, SHARD_TOTAL)
        sys.exit(1)

    # 1) list files once
    files = list_date_files(DATE)
    assigned = [f for f in files if shard_assign(f) == SHARD_ID]
    log.info("shard %d assigned %d files", SHARD_ID, len(assigned))

    # 2) save manifest for Lightning CDN-only training
    manifest_path = Path(f"manifest-{DATE}-shard{SHARD_ID}.json")
    manifest_path.write_text(json.dumps(assigned, indent=2))
    log.info("manifest saved to %s", manifest_path)

    # 3) process assigned files
    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    out_dir = Path(f"batches/public-merged/{DATE}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shard{SHARD_ID}-{ts}.jsonl"

    written = 0
    skipped_dup = 0
    with out_path.open("w", encoding="utf-8") as f:
        for path in assigned:
            try:
                pairs = download_and_process(path)
            except Exception as exc:
                log.error("failed %s: %s", path, exc)
                continue

            for pair in pairs:
                h = md5_hash(pair)
                if dedup and not dedup.add(h):
                    skipped_dup += 1
                    continue
                f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                written += 1

    log.info("shard %d done: written=%d skipped_dup=%d out=%s", SHARD_ID, written, skipped_dup, out_path)

    # 4) push to HF dataset repo (if token provided)
    if HF_TOKEN:
        try:
            hf_api().upload_file(
                path_or_fileobj=str(out_path),
                path_in_repo=str(out_path),
                repo_id=REPO_ID,
                repo_type="dataset",
            )
            log.info("uploaded %s to %s", out_path, REPO_ID)
        except Exception as exc:
            log.error("upload failed: %s", exc)
            sys.exit(1)
    else:
        log.warning("HF_TOKEN not set, skipping upload")

if __name__ == "__main__":
    main()
```

```bash
# Make executable
chmod +x bin/dataset-enrich.py
```

```yaml
# .github/workflows/ingest.yml  (minimal diff)
# Replace step run: bin/dataset-enrich.sh
# with: python bin/dataset-enrich.py
```

### Dependencies

Add to `requirements.txt` if missing:

```
requests
pyarrow
huggingface_hub
```


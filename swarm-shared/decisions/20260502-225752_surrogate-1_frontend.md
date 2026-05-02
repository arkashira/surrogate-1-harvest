# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes
1. Add `bin/list-date-files.py` — single Mac-side script that calls `list_repo_tree(path, recursive=False)` for one date folder, saves JSON of file paths. Embeds this list into training/shard scripts so Lightning training does CDN-only fetches with zero API calls during data load.
2. Update `bin/dataset-enrich.sh` to accept an optional file-list JSON; if provided, iterate only listed files (skip runtime `list_repo_files`). Fall back to current behavior if not provided.
3. Add small Python helper (`lib/cdn_download.py`) to fetch via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` with retries and backoff (no auth header).
4. Update README with usage and the HF CDN bypass note.

### Why this is highest value
- Eliminates HF API 429 risk during ingestion/training (per past lessons).
- Keeps shard workers deterministic and fast (no recursive listing, no auth on CDN downloads).
- Fits <2h: ~30 min for scripts, ~30 min for tests/README, buffer for polish.

---

## Files to add/modify

### 1) bin/list-date-files.py
```python
#!/usr/bin/env python3
"""
List files in a single date folder of axentx/surrogate-1-training-pairs.
Usage:
  python bin/list-date-files.py 2026-05-01 > file-list-2026-05-01.json
"""
import json
import sys
import os
from huggingface_hub import HfApi

REPO = "axentx/surrogate-1-training-pairs"

def main():
    if len(sys.argv) < 2:
        print("Usage: list-date-files.py <date-folder>", file=sys.stderr)
        sys.exit(1)
    date_folder = sys.argv[1].strip("/")
    api = HfApi()
    # Non-recursive per date folder to avoid 100x pagination and rate limits
    entries = api.list_repo_tree(repo_id=REPO, path=date_folder, recursive=False)
    files = [e.path for e in entries if e.type == "file"]
    payload = {
        "repo": REPO,
        "date_folder": date_folder,
        "files": sorted(files),
        "count": len(files),
    }
    json.dump(payload, sys.stdout, indent=2)

if __name__ == "__main__":
    main()
```

### 2) lib/cdn_download.py
```python
import requests
import time
import os
from pathlib import Path
from typing import Optional

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def cdn_download(repo: str, path: str, dest: Optional[Path] = None, retries: int = 5) -> bytes:
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == retries:
                raise
            sleep_sec = min(2 ** attempt, 60)
            time.sleep(sleep_sec)

def stream_cdn_to_file(repo: str, path: str, dest: Path, chunk_size: int = 8192, retries: int = 5) -> Path:
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=chunk_size):
                        f.write(chunk)
            return dest
        except Exception as exc:
            if attempt == retries:
                raise
            sleep_sec = min(2 ** attempt, 60)
            time.sleep(sleep_sec)
```

### 3) bin/dataset-enrich.sh (updated)
```bash
#!/usr/bin/env bash
# dataset-enrich.sh — worker for surrogate-1 ingestion
#
# Usage:
#   # normal (current behavior)
#   bash bin/dataset-enrich.sh
#
#   # deterministic mode using pre-listed files (recommended)
#   bash bin/dataset-enrich.sh --file-list file-list-2026-05-01.json
#
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE=$(date -u +"%Y-%m-%d")
SHARD_ID=${SHARD_ID:-0}
TOTAL_SHARDS=${TOTAL_SHARDS:-16}
OUTDIR="batches/public-merged/${DATE}"
mkdir -p "${OUTDIR}"
TIMESTAMP=$(date -u +"%H%M%S")
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

FILE_LIST="${1:-}"
USE_LIST=false
if [[ -n "${FILE_LIST}" && -f "${FILE_LIST}" ]]; then
  USE_LIST=true
  echo "Using deterministic file list: ${FILE_LIST}"
fi

# Dedup helper (existing)
DEDPY="lib/dedup.py"
if [[ ! -f "${DEDPY}" ]]; then
  echo "Missing ${DEDPY}" >&2
  exit 1
fi

# If using file list, parse JSON and iterate only listed files
if ${USE_LIST}; then
  # Requires jq; fallback to python if not present
  if command -v jq >/dev/null 2>&1; then
    mapfile -t FILES < <(jq -r '.files[]' "${FILE_LIST}")
  else
    mapfile -t FILES < <(python3 -c "import json,sys;print('\n'.join(json.load(sys.stdin)['files']))" < "${FILE_LIST}")
  fi
else
  # Existing behavior: list repo files (may hit rate limits)
  mapfile -t FILES < <(python3 -c "
from huggingface_hub import HfApi
api = HfApi()
entries = api.list_repo_files('${REPO}')
for e in entries:
    print(e)
")
fi

# Deterministic shard assignment by slug-hash
process_file() {
  local f="$1"
  # compute deterministic shard: hash slug mod TOTAL_SHARDS
  local slug_hash
  slug_hash=$(echo -n "${f}" | sha256sum | awk '{print $1}')
  local shard
  shard=$(( 0x${slug_hash:0:8} % TOTAL_SHARDS ))
  if [[ ${shard} -ne ${SHARD_ID} ]]; then
    return 0
  fi

  # Download via CDN (no auth) and normalize per-schema
  # Placeholder: call python helper to fetch and project to {prompt,response}
  python3 - <<PY
import sys, json, hashlib
from lib.cdn_download import stream_cdn_to_file
from pathlib import Path

repo = "${REPO}"
path = "${f}"
try:
    content = stream_cdn_to_file(repo, path).decode('utf-8', errors='replace')
    # Minimal projection: produce {prompt, response} record
    # Replace with real schema normalization per surrogate-1 rules
    record = {"prompt": path, "response": content[:2000]}
    record["_sha256"] = hashlib.sha256(content.encode('utf-8', errors='replace')).hexdigest()
    print(json.dumps(record, ensure_ascii=False))
except Exception as e:
    sys.stderr.write(f"Failed {path}: {e}\\n")
PY
}

export -f process_file
export SHARD_ID TOTAL_SHARDS

# Run processing and append deduped entries
TMP_OUT=$(mktemp)
trap 'rm -f ${TMP_OUT}' EXIT

printf "%s\n" "${FILES[@]}" | while IFS= read -r f; do
  process_file "${f}"
done | python3 "${DEDPY}" >> "${TMP_OUT}"

# Final output
if [[ -s "${TMP_OUT}" ]]; then
  cat "${TMP_OUT}" >> "${OUTFILE}"
  echo "Wrote $(wc -l < "${TMP_OUT")} records to

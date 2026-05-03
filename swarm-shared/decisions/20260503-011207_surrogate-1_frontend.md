# surrogate-1 / frontend

Below is the **single, consolidated plan** that merges the strongest, non-overlapping parts of both proposals, removes contradictions, and prioritizes **correctness + concrete actionability**.

---

## Final Plan: CDN-Bypass Ingestion with Pre-Computed Manifest

**Goal**  
Eliminate HF API rate-limit failures and HF Space OOM by replacing recursive `list_repo_files` and per-file API calls with **one per-folder `list_repo_tree` + CDN-only fetches**, and project to `{prompt,response}` at parse time. Preserve 16-shard parallelism and remove auth-checked API calls during data load.

**Total estimated effort**: ≤2h.

---

## Implementation Steps (ordered)

| Step | Owner | Time | Concrete deliverable |
|------|-------|------|----------------------|
| 1 | Engineer | 15m | Add `bin/build-manifest.py` (Mac-side) — uses `list_repo_tree` (non-recursive) for one date folder and emits `manifest.json` (paths + sizes). |
| 2 | Engineer | 20m | Update `bin/dataset-enrich.sh` to accept optional `MANIFEST`. If present, workers read file paths from manifest instead of calling `list_repo_files`. |
| 3 | Engineer | 30m | Replace `load_dataset(streaming=True)` with **direct CDN downloads** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) using `requests` with streaming and `pyarrow` projection to `{prompt,response}` only. |
| 4 | Engineer | 20m | Add retry/backoff for CDN downloads (exponential backoff + jitter) and respect HF CDN rate limits. |
| 5 | Engineer | 20m | Remove `source` and `ts` columns before upload; keep attribution in filename pattern `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`. |
| 6 | Engineer | 15m | Update README: run `python bin/build-manifest.py --date 2026-05-03 > manifest.json`, commit or pass to workflow via artifact. |

---

## Code Snippets (final, ready to use)

### 1) `bin/build-manifest.py` (Mac orchestration script)

```python
#!/usr/bin/env python3
"""
Build a manifest for a single date folder in surrogate-1-training-pairs.
Usage:
  python bin/build-manifest.py --date 2026-05-03 > manifest.json
"""

import argparse
import json
import os
import sys
from typing import List, Dict

from huggingface_hub import HfApi

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"

def list_date_folder(date: str) -> List[Dict]:
    api = HfApi()
    # Non-recursive per folder to avoid pagination and rate limits.
    tree = api.list_repo_tree(
        repo_id=HF_REPO,
        path=date,
        repo_type="dataset",
        recursive=False,
    )
    files = []
    for entry in tree:
        if entry.type == "file":
            files.append(
                {
                    "path": os.path.join(date, entry.path),
                    "size": getattr(entry, "size", None),
                }
            )
    return files

def main() -> None:
    parser = argparse.ArgumentParser(description="Build manifest for date folder.")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    args = parser.parse_args()

    try:
        manifest = {
            "repo": HF_REPO,
            "date": args.date,
            "files": list_date_folder(args.date),
        }
        json.dump(manifest, sys.stdout, indent=2)
    except Exception as e:
        sys.stderr.write(f"Error building manifest: {e}\n")
        sys.exit(1)

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/build-manifest.py
```

---

### 2) `bin/dataset-enrich.sh` (updated worker)

```bash
#!/usr/bin/env bash
# dataset-enrich.sh — worker for one shard
# Usage:
#   SHARD_ID=0 SHARD_TOTAL=16 MANIFEST=manifest.json ./bin/dataset-enrich.sh

set -euo pipefail
SHELL=/bin/bash

HF_REPO="datasets/axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
SHARD_TOTAL="${SHARD_TOTAL:-16}"
MANIFEST="${MANIFEST:-}"  # optional; if provided, use CDN-only mode
OUT_DIR="batches/public-merged/${DATE}"
TS="$(date +%H%M%S)"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"
HF_TOKEN="${HF_TOKEN:-}"

mkdir -p "$(dirname "${OUT_FILE}")"

# Dedup store (central SQLite) — reused from HF Space if available
DEDUP_DB="${DEDUP_DB:-/tmp/dedup.db}"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [shard:${SHARD_ID}] $*"
}

# If manifest provided, use CDN-only mode (bypass HF API during ingestion)
if [[ -n "${MANIFEST}" && -f "${MANIFEST}" ]]; then
  log "Using manifest: ${MANIFEST}"
  mapfile -t ALL_FILES < <(python3 -c "
import json, sys
m=json.load(open(sys.argv[1]))
for f in m['files']: print(f['path'])
" "${MANIFEST}")

  TOTAL_FILES="${#ALL_FILES[@]}"
  if (( TOTAL_FILES == 0 )); then
    log "No files in manifest; exiting."
    exit 0
  fi

  # Deterministic shard assignment by slug-hash
  mapfile -t MY_FILES < <(
    for f in "${ALL_FILES[@]}"; do
      slug=$(basename "$f" | sed 's/\.[^.]*$//')
      hash=$(echo -n "$slug" | md5sum | cut -c1-8)
      bucket=$(( 0x$hash % SHARD_TOTAL ))
      if (( bucket == SHARD_ID )); then
        echo "$f"
      fi
    done
  )

  process_cdn_file() {
    local relpath="$1"
    local url="https://huggingface.co/${HF_REPO}/resolve/main/${relpath}"
    python3 - "$url" "$relpath" <<'PYEOF'
import sys, json, requests, pyarrow.parquet as pq, pyarrow as pa, tempfile, os, time, random

url, relpath = sys.argv[1], sys.argv[2]
max_retries = 5
backoff = 1.0

for attempt in range(1, max_retries + 1):
    try:
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".parquet") as tmp:
                for chunk in r.iter_content(chunk_size=8192):
                    tmp.write(chunk)
                tmp_path = tmp.name
        # Project only prompt/response to avoid mixed-schema issues
        try:
            table = pq.read_table(tmp_path, columns=["prompt", "response"])
        except (pa.lib.ArrowInvalid, KeyError):
            os.unlink(tmp_path)
            raise
        # Emit JSONL lines
        df = table.to_pandas()
        for _, row in df.iterrows():
            print(json.dumps({"prompt": row["prompt"], "response": row["response"]}, ensure_ascii=False))
        os.unlink(tmp_path)
        break
    except Exception as e:
        if attempt == max_retries:
            sys.stderr.write(f"Failed to process {relpath}: {e}\n")
            raise
        sleep = backoff * (2 ** (attempt - 1)) + random.uniform(0, 0.1)
        time.sleep(sleep)
PYEOF
  }

  log "Processing ${#MY_FILES[@]} files for shard ${SHARD_ID}"
  > "${OUT_FILE}"
  for f in "${MY_FILES[@]}"; do
    process_cdn_file "$f" >> "${OUT_FILE}"
  done

else
  # Fallback: use HF API (

# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### What we’ll do
1. Add `bin/list-shards.sh` — one-time Mac/CI script that lists target folder tree (non-recursive per subfolder to respect rate limits), saves `shards.json`, and computes deterministic shard assignment.
2. Update `bin/dataset-enrich.sh` to accept an optional `SHARDS_JSON` path; if provided, use CDN URLs only (`huggingface.co/datasets/.../resolve/main/...`) and skip `load_dataset`/`list_repo_files` during worker runs.
3. Add lightweight Python helper (`lib/cdn_stream.py`) to stream parquet/jsonl from CDN URLs with schema projection to `{prompt,response}` and deterministic `shard_id` assignment (`hash(slug) % 16`).
4. Add GitHub Actions step to generate `shards.json` once per cron run (or reuse cached list) and pass it to each matrix shard.
5. Keep existing SQLite dedup logic unchanged (central store on HF Space remains source of truth).

Estimated effort: ~90 minutes.

---

### 1) `bin/list-shards.sh` (run on Mac/CI before workers start)

```bash
#!/usr/bin/env bash
# list-shards.sh
# Usage: HF_TOKEN=... ./list-shards.sh <owner>/<dataset> <date-folder> > shards.json
# Lists one date folder (non-recursive) and emits shard map for CDN-only ingestion.
set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE_FOLDER="${2:-$(date +%Y-%m-%d)}"
API_ROOT="https://huggingface.co/api/datasets/${REPO}/tree"

# Single API call: list top-level items in date folder (non-recursive)
# Avoids recursive list_repo_files which paginates 100x and hits 429.
echo "Listing ${REPO}/${DATE_FOLDER} (non-recursive)..." >&2

curl -sSfL \
  -H "Authorization: Bearer ${HF_TOKEN:-}" \
  "${API_ROOT}/${DATE_FOLDER}?recursive=false" \
  | jq -c '[ .[] | select(.type=="file") | .path ]' \
  | jq -c --arg repo "$REPO" --arg date "$DATE_FOLDER" '
    {
      repo: $repo,
      date: $date,
      files: .,
      generated_at: (now | todate),
      shards: (
        . as $files
        | [range(0;16)]
        | map({
            shard_id: .,
            files: ($files | map(select((. | @sh) | hash % 16 == .)) // [])
          })
      )
    }'
```

- Make executable: `chmod +x bin/list-shards.sh`
- Notes: Uses one API call per date folder. CDN downloads (`/resolve/main/...`) are not counted against API rate limits.

---

### 2) `lib/cdn_stream.py` (lightweight CDN fetcher + projection)

```python
# lib/cdn_stream.py
import json
import hashlib
import pyarrow.parquet as pq
import pyarrow as pa
import numpy as np
import requests
from io import BytesIO
from typing import Iterator, Dict, Any

CDN_ROOT = "https://huggingface.co/datasets"

def deterministic_shard_id(slug: str, n_shards: int = 16) -> int:
    return int(hashlib.md5(slug.encode("utf-8")).hexdigest(), 16) % n_shards

def cdn_url(repo: str, path: str) -> str:
    return f"{CDN_ROOT}/{repo}/resolve/main/{path}"

def project_to_pair(row: Dict[str, Any]) -> Dict[str, Any]:
    # Keep only prompt/response; ignore extra schema fields.
    return {
        "prompt": row.get("prompt") or row.get("input") or row.get("text") or "",
        "response": row.get("response") or row.get("output") or row.get("completion") or "",
    }

def stream_shard_files(file_paths: list[str], repo: str, shard_id: int) -> Iterator[Dict[str, Any]]:
    for rel_path in file_paths:
        url = cdn_url(repo, rel_path)
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            # Log and skip; don't kill whole shard.
            print(f"Skipping {rel_path}: {exc}")
            continue

        data = BytesIO(resp.content)
        try:
            # Try parquet first
            table = pq.read_table(data, columns=["prompt", "response"] if pa.schema_contains_names(pq.read_schema(data), ["prompt", "response"]) else None)
            if table.num_rows == 0:
                continue
            # Normalize to dicts
            cols = table.to_pydict()
            # If columns missing, project from all available
            if "prompt" not in cols or "response" not in cols:
                rows = [dict(zip(table.column_names, row)) for row in zip(*cols.values())]
                for row in rows:
                    pair = project_to_pair(row)
                    if pair["prompt"] and pair["response"]:
                        yield pair
            else:
                for i in range(table.num_rows):
                    pair = {"prompt": cols["prompt"][i], "response": cols["response"][i]}
                    if pair["prompt"] and pair["response"]:
                        yield pair
        except Exception:
            # Fallback: try line-delimited JSON
            try:
                data.seek(0)
                text = data.read().decode("utf-8", errors="ignore")
                for line in text.strip().splitlines():
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    pair = project_to_pair(row)
                    if pair["prompt"] and pair["response"]:
                        yield pair
            except Exception:
                print(f"Could not decode {rel_path} as parquet or JSONL")
                continue
```

---

### 3) Updated `bin/dataset-enrich.sh` (worker)

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Updated to support CDN-only mode via SHARDS_JSON.
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
SHARD_ID="${SHARD_ID:-0}"
SHARDS_JSON="${SHARDS_JSON:-}"
WORK_DIR="${WORK_DIR:-/tmp/enrich-${SHARD_ID}}"
DEDUP_DB="${DEDUP_DB:-/opt/axentx/surrogate-1/lib/dedup.db}"
OUT_DIR="${OUT_DIR:-batches/public-merged/$(date +%Y-%m-%d)}"
TIMESTAMP=$(date +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

mkdir -p "$WORK_DIR" "$OUT_DIR"

echo "Worker shard=${SHARD_ID} out=${OUT_FILE}"

if [ -n "$SHARDS_JSON" ] && [ -f "$SHARDS_JSON" ]; then
  echo "Using CDN-only mode from ${SHARDS_JSON}"
  FILES=$(jq -r --argjson sid "$SHARD_ID" '.shards[$sid].files[]' "$SHARDS_JSON")
  if [ -z "$FILES" ]; then
    echo "No files assigned to shard ${SHARD_ID}. Exiting."
    exit 0
  fi

  python3 - <<PY
import sys, json, os
sys.path.insert(0, os.path.join(os.getcwd(), "lib"))
from cdn_stream import stream_shard_files, deterministic_shard_id

repo = os.getenv("REPO", "axentx/surrogate-1-training-pairs")
shard_id = int(os.getenv("SHARD_ID", "0"))
shards_json = os.getenv("SHARDS_JSON")
out_file = os.getenv("OUT_FILE")
dedup_db = os.getenv("DEDUP_DB")

with open(shards_json) as f:
    manifest = json.load(f)
files = manifest["shards"][shard_id]["files"]

# Import dedup lazily
from lib.dedup import DedupStore
dedup = DedupStore(dedup_db)

count = 0
with open(out_file, "w", encoding="utf-8")

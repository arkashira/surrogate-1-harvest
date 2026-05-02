# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes
1. Add `bin/list-date-files.py` — single Mac-side script that calls `list_repo_tree` once per date folder and emits `file-list-<date>.json`. Embed this list in training scripts so Lightning workers do **zero API calls** during data loading (CDN-only).
2. Update `bin/dataset-enrich.sh` to accept an optional file-list JSON. If provided, workers iterate the list and stream via `hf_hub_download` (bypassing `load_dataset` for heterogeneous schemas). If not provided, keep current behavior.
3. Add lightweight schema projector in Python (inline in the worker) that reads each downloaded file and yields only `{prompt, response}` — no extra columns, no mixed-schema pyarrow errors.
4. Add retry/backoff for 429 with 360s sleep and respect per-folder pagination (no recursive `list_repo_files`).

### Why this is highest value
- Eliminates the most common training failure (HF API 429) by moving to CDN-only fetches.
- Keeps shard workers fast and memory-bounded (7 GB each) while removing dataset-library schema ambiguity.
- One-time list on the Mac side means Lightning training can reuse the same list across restarts without burning quota.

---

## Code snippets

### 1) bin/list-date-files.py
```python
#!/usr/bin/env python3
"""
Generate a deterministic file list for a date folder in
axentx/surrogate-1-training-pairs so workers can fetch via CDN
without HF API calls during training.

Usage:
  python bin/list-date-files.py --date 2026-04-29 --out file-list-2026-04-29.json
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from huggingface_hub import HfApi, Repository

API = HfApi()
REPO_ID = "axentx/surrogate-1-training-pairs"

def list_date_folder(date: str, retries: int = 3, backoff: int = 360) -> list[str]:
    """
    Non-recursive list of files under <date>/
    Returns paths relative to repo root.
    """
    prefix = f"{date}/"
    for attempt in range(1, retries + 1):
        try:
            items = API.list_repo_tree(
                repo_id=REPO_ID,
                path=prefix.rstrip("/"),
                recursive=False,
            )
            # items can be dict or object depending on hf_hub version
            paths = []
            for item in items:
                p = item.get("path") if isinstance(item, dict) else getattr(item, "path", None)
                if p and p.startswith(prefix):
                    paths.append(p)
            return sorted(paths)
        except Exception as exc:
            if hasattr(exc, "response") and getattr(exc.response, "status_code", None) == 429:
                if attempt == retries:
                    raise
                print(f"429 rate-limited, sleeping {backoff}s (attempt {attempt}/{retries})", file=sys.stderr)
                time.sleep(backoff)
                continue
            raise

def main() -> None:
    parser = argparse.ArgumentParser(description="List date folder for CDN-only ingestion")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-04-29")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    paths = list_date_folder(args.date)
    payload = {
        "repo": REPO_ID,
        "date": args.date,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": paths,
    }

    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(f"Wrote {len(paths)} files to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-date-files.py
```

---

### 2) bin/dataset-enrich.sh (updated worker section)
Add optional file-list mode and CDN streaming:

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Existing behavior preserved; new mode: --file-list <json>

set -euo pipefail
SHELL=/bin/bash

HF_REPO="axentx/surrogate-1-training-pairs"
HF_TOKEN="${HF_TOKEN:-}"
DATE=$(date -u +%Y-%m-%d)
TS=$(date -u +%H%M%S)
SHARD="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
WORKDIR=$(mktemp -d)
cd "$WORKDIR"

# Optional file-list mode (CDN-only, no HF API during streaming)
FILE_LIST="${FILE_LIST:-}"

function download_via_cdn() {
  local rel_path="$1"
  local out_path="$2"
  curl -fsSL "https://huggingface.co/datasets/${HF_REPO}/resolve/main/${rel_path}" \
    -o "${out_path}"
}

function project_record() {
  # Lightweight projection: keep only prompt/response, drop extra cols.
  # Handles JSON/JSONL/parquet via python one-liner.
  local src="$1"
  local tmp=$(mktemp)
  python3 -c "
import sys, json, pyarrow.parquet as pq, pyarrow as pa, os
src = sys.argv[1]
tmp = sys.argv[2]
if src.endswith('.parquet'):
    tbl = pq.read_table(src, columns=['prompt','response'] if set(['prompt','response']).issubset(pq.read_schema(src).names) else None)
    if tbl is None or tbl.num_rows == 0:
        sys.exit(0)
    # normalize to object
    df = tbl.to_pandas()
else:
    # assume line-delimited json
    rows = []
    with open(src) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            rows.append(obj)
    if not rows:
        sys.exit(0)
    df = pa.Table.from_pylist(rows).to_pandas()
# keep only prompt/response
df = df[['prompt','response']] if set(['prompt','response']).issubset(df.columns) else df
# write normalized jsonl
df.to_json(tmp, orient='records', lines=True)
" "$src" "$tmp" || true
  cat "$tmp"
  rm -f "$tmp"
}

function process_with_file_list() {
  local list_file="$1"
  local files=()
  mapfile -t files < <(python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
for f in data.get('files', []):
    print(f)
" "$list_file")

  local total=${#files[@]}
  local out_lines=()
  for idx in "${!files[@]}"; do
    f="${files[$idx]}"
    # deterministic shard assignment by slug-hash
    slug=$(basename "$f" | sed 's/\.[^.]*$//')
    bucket=$(( $(echo -n "$slug" | md5sum | cut -c1-8) % TOTAL_SHARDS ))
    if [[ "$bucket" != "$SHARD" ]]; then
      continue
    fi
    echo "Processing [$idx/$total] $f (shard=$SHARD)" >&2
    dl=$(mktemp)
    if download_via_cdn "$f" "$dl"; then
      while IFS= read -r line; do
        [[ -n "$line" ]] && out_lines+=("$line")
      done < <(project_record "$dl")
    else
      echo "Failed to download $f" >&2
    fi
    rm -f "$dl"
  done

  # upload shard output
  out_file="shard${SHARD}-${TS}.jsonl"
  printf "%s\n" "${out_lines[@]}" | jq -c '.' > "$out_file" 2>/dev/null || cat > "$out_file"
  # push to dataset repo (batches/public-merged/<date>/)
  if [[ -n "$HF_TOKEN" ]]; then
    git config --

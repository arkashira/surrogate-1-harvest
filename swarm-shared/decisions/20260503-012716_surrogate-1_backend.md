# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Goal**: Eliminate HF API rate-limit failures and recursive listing by switching to non-recursive `list_repo_tree` + CDN-only fetches + deterministic shard routing.

### Core Changes
1. **Replace recursive listing** with `list_repo_tree(path, recursive=False)` per date folder; emit stable file list JSON.
2. **Fetch via CDN URLs** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with no Authorization header.
3. **Deterministic shard routing**: hash `slug` → pick one of 5 sibling repos for writes (640/hr aggregate) and use deterministic output path.
4. **Schema projection**: keep only `{prompt, response}` at parse time; move attribution to filename pattern `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

### Why This Is Highest Value
- Removes recursive paginated calls (100× pages) and per-file auth calls that trigger 429.
- CDN downloads bypass `/api/` rate limits entirely.
- Deterministic sibling repo routing lifts HF commit cap (128/hr/repo) to 640/hr aggregate.
- Fits within <2h: only one script to change plus small Python helper.

---

## Code Changes

### 1) Helper to list files for a date folder (non-recursive)

`bin/list_files.py`
```python
#!/usr/bin/env python3
"""
List files in a single date folder (non-recursive) for surrogate-1-training-pairs.
Outputs JSON list of relative paths to stdout.
Usage:
  python3 bin/list_files.py --date 2026-04-29 --repo axentx/surrogate-1-training-pairs
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-04-29)")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--output", default=None, help="Optional output file (default: stdout)")
    args = parser.parse_args()

    api = HfApi()
    folder = args.date.strip("/")
    # non-recursive listing for the folder
    entries = api.list_repo_tree(repo=args.repo, path=folder, recursive=False)

    files = []
    for e in entries:
        # e.path is like "2026-04-29/file1.parquet"
        if not e.path.endswith("/"):  # skip subfolders (shouldn't exist with recursive=False)
            files.append(e.path)

    out = json.dumps({"date": args.date, "files": sorted(files)}, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
    else:
        sys.stdout.write(out)

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list_files.py
```

---

### 2) Updated worker script using CDN fetches and sibling routing

`bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
# surrogate-1 dataset-enrich worker (shard-aware, CDN-only fetches)
#
# Required env:
#   HF_TOKEN          - write token for sibling repos
#   SHARD_ID          - 0..15
#   DATE_FOLDER       - e.g. 2026-04-29
#   WORK_DIR          - working directory (default: /tmp/enrich-$SHARD_ID)
#
# Behavior:
#   1) List files for DATE_FOLDER via non-recursive API call (once per shard).
#   2) Download each file via CDN (no auth header) and normalize to {prompt,response}.
#   3) Dedup via central md5 store (lib/dedup.py).
#   4) Upload shard output to a deterministic sibling repo to avoid HF commit cap.

set -euo pipefail
SHELL=/bin/bash

HF_TOKEN="${HF_TOKEN:?HF_TOKEN required}"
SHARD_ID="${SHARD_ID:?SHARD_ID required (0-15)}"
DATE_FOLDER="${DATE_FOLDER:?DATE_FOLDER required (e.g. 2026-04-29)}"
WORK_DIR="${WORK_DIR:-/tmp/enrich-${SHARD_ID}}"
BASE_REPO="axentx/surrogate-1-training-pairs"
NUM_SIBLINGS=5

mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

# ---- 1) List files (non-recursive) for the date folder ----
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Listing files for ${DATE_FOLDER} (shard ${SHARD_ID})"
python3 "$(realpath "$(dirname "$0")")/list_files.py" \
  --date "$DATE_FOLDER" \
  --repo "$BASE_REPO" \
  --output filelist.json

mapfile -t FILES < <(jq -r '.files[]' filelist.json)
TOTAL=${#FILES[@]}
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Found ${TOTAL} files"

# ---- 2) Deterministic shard slicing ----
# Simple deterministic assignment: include file if hash(path) % 16 == SHARD_ID
mapfile -t MY_FILES < <(
  for f in "${FILES[@]}"; do
    # stable numeric hash (0-65535)
    h=$(python3 -c "import hashlib; print(int(hashlib.md5('$f'.encode()).hexdigest(), 16) % 65536)")
    if (( h % 16 == SHARD_ID )); then
      printf '%s\n' "$f"
    fi
  done
)
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Shard ${SHARD_ID} processing ${#MY_FILES[@]} files"

# ---- 3) Download via CDN (no auth) and normalize ----
# Central dedup helper
DEDUP_PY="$(realpath "$(dirname "$0")")/lib/dedup.py"

OUTPUT="shard-${SHARD_ID}-$(date -u +%H%M%S).jsonl"
> "$OUTPUT"

for rel in "${MY_FILES[@]}"; do
  # CDN URL (public, no Authorization header)
  URL="https://huggingface.co/datasets/${BASE_REPO}/resolve/main/${rel}"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Fetching ${rel}"

  # Download to temp file
  tmpf="$(mktemp)"
  if ! curl -fsSL --retry 3 --retry-delay 5 -o "$tmpf" "$URL"; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WARN: failed to fetch ${rel}" >&2
    rm -f "$tmpf"
    continue
  fi

  # Normalize: project to {prompt,response} only
  # Supports common patterns: json/jsonl/parquet with varying schemas.
  python3 - "$tmpf" "$rel" "$OUTPUT" "$DEDUP_PY" <<'PYEOF'
import json, sys, os, tempfile, uuid
from pathlib import Path

def normalize_file(path: str, output_path: str, dedup_py: str) -> None:
    path = Path(path)
    suffix = path.suffix.lower()

    def write_obj(obj):
        # minimal projection
        prompt = obj.get("prompt") or obj.get("input") or obj.get("text") or ""
        response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
        if not prompt and not response:
            return
        # central dedup via md5 store
        import subprocess, json
        proc = subprocess.run(
            [sys.executable, dedup_py, "check", prompt, response],
            capture_output=True, text=True
        )
        if proc.returncode != 0:
            return  # duplicate
        # store accepted
        with open(output_path, "a", encoding="utf-8") as f:
            json.dump({"prompt": prompt, "response": response}, f, ensure_ascii=False)
            f.write("\n")

    try:
        if suffix == ".jsonl":
            with open

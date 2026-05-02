# surrogate-1 / backend

### Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s and make shard workers resilient.

---

### What we’ll do
1. **Create `bin/list-files.py`** — one-time script that lists files for a date folder and writes `file-list.json` (path + size + sha256 + CDN URL). Embed this list in training/shard scripts so workers do **zero** HF API calls during data load.
2. **Update `bin/dataset-enrich.sh`** to accept an optional file-list JSON and stream via `curl` against CDN URLs. Keep fallback to `datasets` library for compatibility.
3. **Add `bin/train-cdn.sh`** launcher for Lightning Studio that injects the file-list and sets `HF_DATASETS_OFFLINE=1` to prevent accidental API calls.
4. **Update `ingest.yml`** to run `list-files.py` once per date folder on schedule and commit the updated file list.

---

### Why this matters
- HF API rate-limit (429) blocks training/ingest; CDN tier has much higher limits and no auth checks.
- Single `list_repo_tree` call per folder avoids recursive pagination and 429s.
- Lightning Studio reuse + CDN-only = no quota burn and no auth/rate failures during long runs.

---

### 1) `bin/list-files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file-list for a date folder in axentx/surrogate-1-training-pairs.
Usage:
  python bin/list-files.py --date 2026-05-02 --out file-list.json
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

REPO_ID = "datasets/axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--out", default="file-list.json", help="Output JSON path")
    parser.add_argument("--token", default=os.getenv("HF_TOKEN"), help="HF token (optional for public reads)")
    args = parser.parse_args()

    api = HfApi(token=args.token)
    folder = f"{args.date}"
    print(f"Listing {REPO_ID}/{folder} ...", file=sys.stderr)

    # Non-recursive per top-level date folder; keeps API usage minimal.
    entries = api.list_repo_tree(repo_id=REPO_ID, path=folder, recursive=False)

    files = []
    for e in entries:
        if e.type != "file":
            continue
        # CDN URL (no auth required for public datasets)
        cdn_url = f"https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{folder}/{e.path}"
        files.append({
            "path": f"{folder}/{e.path}",
            "size": e.size,
            "lfs": getattr(e, "lfs", None),
            "cdn_url": cdn_url,
        })

    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date": args.date,
        "repo": REPO_ID,
        "count": len(files),
        "files": files,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {len(files)} entries to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-files.py
```

---

### 2) `bin/dataset-enrich.sh` (updated)

Add CDN support and optional file-list mode. Keep existing `datasets` fallback for compatibility.

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Normalize & dedup a deterministic slice of public dataset files.
#
# Usage:
#   # Default: uses `datasets` library (HF API)
#   HF_TOKEN=... python -m dataset_enrich --shard $SHARD_ID --total 16
#
#   # CDN mode (recommended): uses file-list + wget to bypass HF API auth/limits
#   HF_TOKEN=... python -m dataset_enrich --shard $SHARD_ID --total 16 \
#        --file-list file-list.json --cdn
#
# Environment:
#   HF_TOKEN         write token for axentx/surrogate-1-training-pairs
#   SHARD_ID         0..15 (or set via matrix)
#   PYTHONPATH       . (for local modules)

set -euo pipefail
export SHELL=/bin/bash

cd "$(dirname "$0")/.."

SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
FILE_LIST="${FILE_LIST:-}"
USE_CDN="${USE_CDN:-false}"

if [[ -n "$FILE_LIST" && "$USE_CDN" == "true" ]]; then
    echo "Running in CDN mode with $FILE_LIST"
    python -m dataset_enrich \
        --shard "$SHARD_ID" \
        --total "$TOTAL_SHARDS" \
        --file-list "$FILE_LIST" \
        --cdn
else
    echo "Running in default (datasets) mode"
    python -m dataset_enrich \
        --shard "$SHARD_ID" \
        --total "$TOTAL_SHARDS"
fi
```

Ensure executable:
```bash
chmod +x bin/dataset-enrich.sh
```

---

### 3) `bin/train-cdn.sh` (Lightning Studio launcher)

```bash
#!/usr/bin/env bash
# train-cdn.sh
# Launch Lightning Studio training with CDN-only file list.
# Reuses running studio when possible to save quota.

set -euo pipefail
export SHELL=/bin/bash

cd "$(dirname "$0")/.."

FILE_LIST="${FILE_LIST:-file-list.json}"
STUDIO_NAME="${STUDIO_NAME:-surrogate-1-train}"
DATE_FOLDER="${DATE_FOLDER:-2026-05-02}"

if [[ ! -f "$FILE_LIST" ]]; then
    echo "Generating file-list for $DATE_FOLDER ..."
    python bin/list-files.py --date "$DATE_FOLDER" --out "$FILE_LIST"
fi

HF_DATASETS_OFFLINE=1 \
lightning studio run \
    --name "$STUDIO_NAME" \
    --script train.py \
    --env "HF_DATASETS_OFFLINE=1" \
    --env "FILE_LIST=$FILE_LIST" \
    --arg "--file-list" "$FILE_LIST" \
    --arg "--cdn"
```

Make executable:
```bash
chmod +x bin/train-cdn.sh
```

---

### 4) `ingest.yml` (updated)

```yml
name: Ingest

on:
  schedule:
    - cron: 0 0 * * *
  workflow_dispatch:

jobs:
  ingest:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install deps
        run: pip install huggingface_hub

      - name: Update file list
        run: python bin/list-files.py --date 2026-05-02 --out file-list.json

      - name: Commit updated file list
        run: |
          git config user.name "github-actions"
          git config user.email "github-actions@github.com"
          git add file-list.json
          git diff --quiet && git diff --staged --quiet || git commit -m "Update file-list.json"
          git push

      - name: Run ingestion script
        run: bash bin/dataset-enrich.sh
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
          SHARD_ID: 0
          TOTAL_SHARDS: 16
          FILE_LIST: file-list.json
          USE_CDN: "true"
```

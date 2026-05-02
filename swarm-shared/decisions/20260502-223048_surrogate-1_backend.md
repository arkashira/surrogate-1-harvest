# surrogate-1 / backend

## Implementation Plan (≤2h)

**Goal**: Eliminate HF API 429s during training/ingest by switching to CDN-only fetches with deterministic pre-flight file listing.

### Changes (3 files)

1. **`bin/list-files.py`** (new)  
   - Single API call from Mac/CI (after rate-limit window) to list one date folder via `list_repo_tree(recursive=False)`.  
   - Emits `file-list.json` with `{"date":"YYYY-MM-DD","files":["path1.parquet",...],"repo":"datasets/axentx/surrogate-1-training-pairs"}`.  
   - Embeddable in training scripts and shard workers.

2. **`bin/dataset-enrich.sh`** (modify)  
   - Accept optional `FILE_LIST_JSON` path. If provided, read file list from JSON instead of calling `list_repo_files`/`list_repo_tree` repeatedly.  
   - Use CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) for downloads — no Authorization header, bypasses API rate limits.  
   - Keep existing schema projection and dedup logic unchanged.

3. **`.github/workflows/ingest.yml`** (modify)  
   - Add optional `file_list` input and pass to workers via env.  
   - If not provided, workers fall back to current behavior (safe for cron).  
   - Document how Mac/CI should run `list-files.py` and commit `file-list.json` before training sweeps.

---

## 1) `bin/list-files.py` (new)

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in
axentx/surrogate-1-training-pairs.

Usage:
  python bin/list-files.py --date 2026-05-02 --out file-list.json

Output:
  {
    "repo": "datasets/axentx/surrogate-1-training-pairs",
    "date": "2026-05-02",
    "files": [
      "batches/public-merged/2026-05-02/part-0000.parquet",
      ...
    ],
    "generated_at": "2026-05-02T22:30:00Z"
  }
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

REPO = "datasets/axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser(description="List HF dataset files for a date folder (CDN-ready).")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--repo", default=REPO, help="HF dataset repo (default: %(default)s)")
    parser.add_argument("--out", default="file-list.json", help="Output JSON path")
    parser.add_argument("--token", default=os.getenv("HF_TOKEN"), help="HF token (optional for public repo tree)")
    args = parser.parse_args()

    api = HfApi(token=args.token)
    path = f"batches/public-merged/{args.date}"

    try:
        # Single API call; recursive=False keeps it small and fast
        entries = api.list_repo_tree(repo_id=args.repo, path=path, recursive=False, repo_type="dataset")
    except Exception as exc:
        print(f"ERROR: failed to list repo tree: {exc}", file=sys.stderr)
        sys.exit(1)

    files = sorted(e.rfilename for e in entries if e.rfilename.lower().endswith(".parquet"))
    if not files:
        print(f"WARN: no parquet files found under {path}", file=sys.stderr)

    out = {
        "repo": args.repo,
        "date": args.date,
        "path": path,
        "files": files,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list-files.py
```

---

## 2) `bin/dataset-enrich.sh` (modify)

Add near top (after shebang):

```bash
# Optional deterministic file list (JSON from bin/list-files.py).
# If provided, workers will use CDN URLs and skip HF API list/tree calls.
FILE_LIST_JSON="${FILE_LIST_JSON:-}"
```

Replace any `list_repo_files` / recursive listing logic with:

```bash
resolve_cdn_url() {
  local repo="$1"
  local path="$2"
  # Public CDN URL — no Authorization header required
  printf "https://huggingface.co/datasets/%s/resolve/main/%s" "$repo" "$path"
}

load_file_list() {
  if [ -n "$FILE_LIST_JSON" ] && [ -f "$FILE_LIST_JSON" ]; then
    # Use jq if available; fallback to python for portability
    if command -v jq >/dev/null 2>&1; then
      jq -r '.files[]' "$FILE_LIST_JSON"
    else
      python3 -c "import json,sys; [print(f) for f in json.load(open(sys.argv[1]))['files']]" "$FILE_LIST_JSON"
    fi
    return 0
  fi
  return 1
}

download_via_cdn() {
  local repo="$1"
  local rel_path="$2"
  local out="$3"
  local url
  url=$(resolve_cdn_url "$repo" "$rel_path")
  curl -L --retry 3 --retry-delay 5 -o "$out" "$url"
}
```

In the worker loop:

```bash
if load_file_list > /tmp/filelist.txt; then
  echo "Using deterministic file list from ${FILE_LIST_JSON}"
  while IFS= read -r rel_path; do
    [ -z "$rel_path" ] && continue
    out_pq="${WORKDIR}/$(basename "$rel_path")"
    if download_via_cdn "$DATASET_REPO" "$rel_path" "$out_pq"; then
      # existing schema projection / dedup logic unchanged
      python3 -m parquet_to_jsonl "$out_pq" >> "${WORKDIR}/shard-${SHARD_ID}.jsonl"
      rm -f "$out_pq"
    else
      echo "WARN: CDN download failed for $rel_path"
    fi
  done < /tmp/filelist.txt
else
  echo "WARN: no file list; falling back to HF API (may hit 429)"
  # existing HF API listing logic (keep as fallback)
fi
```

---

## 3) `.github/workflows/ingest.yml` (modify)

Add optional input and env passthrough:

```yaml
on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:
    inputs:
      file_list:
        description: "Optional file-list.json path (relative to repo root) to enable CDN-only ingestion"
        required: false
        default: ""

jobs:
  ingest:
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    runs-on: ubuntu-latest
    env:
      SHARD_ID: ${{ matrix.shard_id }}
      DATASET_REPO: axentx/surrogate-1-training-pairs
      HF_TOKEN: ${{ secrets.HF_TOKEN }}
      FILE_LIST_JSON: ${{ github.event.inputs.file_list }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: bash bin/dataset-enrich.sh
```

---

## Usage (docs)

**Mac/CI — generate file list once per date** (after rate-limit window clears):

```bash
python bin

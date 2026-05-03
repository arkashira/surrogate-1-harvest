# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value change**: Eliminate HF API rate-limit failures during ingestion by switching to CDN-bypass ingestion with a pre-computed manifest. This preserves 16-shard parallelism while removing per-file API calls during data loading.

### Steps (120 min total)

1. **Create manifest generator** (20 min)  
   - Single script run from Mac (or cron) that calls `list_repo_tree` once per date folder → saves `manifest-YYYYMMDD.json` to repo root.  
   - Manifest contains only `path` and `size`; no recursive listing.

2. **Update `bin/dataset-enrich.sh`** (30 min)  
   - Accept optional manifest file arg; if present, shard the manifest entries instead of calling `list_repo_files`.  
   - Replace `load_dataset(streaming=True, repo_type="dataset", ...)` with direct CDN downloads via `hf_hub_download` (or raw `requests` to `resolve/main/`).  
   - Project to `{prompt,response}` immediately after decode; drop all other columns.

3. **Update dedup/projection logic** (20 min)  
   - Ensure `lib/dedup.py` works with streamed JSONL lines from CDN files.  
   - Keep md5 hash store as source of truth; skip per-run state.

4. **Update GitHub Actions matrix** (10 min)  
   - Pass `MANIFEST_FILE` as env to each shard; compute deterministic shard assignment from manifest rows (by slug-hash).  
   - Keep 16-shard matrix unchanged.

5. **Add fallback & retry** (15 min)  
   - If CDN fetch fails (rare), retry with exponential backoff; after N failures, skip file and log.  
   - Respect HF CDN limits (generous) but avoid hammering.

6. **Test locally** (25 min)  
   - Run one shard against a small date folder; verify output lines and no HF API calls during data load.

---

## Code Snippets

### 1. Manifest generator (`bin/gen-manifest.py`)

```python
#!/usr/bin/env python3
"""
Generate manifest for a date folder in axentx/surrogate-1-training-pairs.
Run from Mac (or cron) when API rate-limit window is clear.
Usage:
  python bin/gen-manifest.py --date 2026-05-03 --out manifest-20260503.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

API = HfApi()
REPO_ID = "axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    # One non-recursive call per date folder
    items = API.list_repo_tree(
        repo_id=REPO_ID,
        path=args.date,
        recursive=False,
        repo_type="dataset",
    )

    manifest = []
    for item in items:
        if item.type != "file":
            continue
        # Only include parquet/jsonl we expect
        if not item.path.lower().endswith((".parquet", ".jsonl")):
            continue
        manifest.append({
            "path": item.path,          # e.g. "2026-05-03/file-001.parquet"
            "size": item.size or 0,
        })

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(manifest)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/gen-manifest.py
```

---

### 2. Updated worker snippet (inside `bin/dataset-enrich.sh`)

Replace dataset loading section with:

```bash
#!/usr/bin/env bash
set -euo pipefail

# --
# Config
# --
REPO="axentx/surrogate-1-training-pairs"
MANIFEST="${MANIFEST_FILE:-}"          # e.g. manifest-20260503.json
SHARD_ID="${SHARD_ID:-0}"              # 0..15
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
TMP_DIR=$(mktemp -d)
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

# --
# Determine which files this shard processes
# --
if [[ -n "$MANIFEST" && -f "$MANIFEST" ]]; then
  # Shard the manifest lines deterministically by path hash
  mapfile -t FILES < <(
    python3 -c "
import json, sys, hashlib
with open(sys.argv[1]) as f:
    rows = json.load(f)
for r in rows:
    h = int(hashlib.md5(r['path'].encode()).hexdigest(), 16)
    if h % ${TOTAL_SHARDS} == ${SHARD_ID}:
        print(r['path'])
" "$MANIFEST"
  )
else
  # Fallback: use API list (avoid in production)
  mapfile -t FILES < <(
    python3 -c "
from huggingface_hub import HfApi
api = HfApi()
items = api.list_repo_files('$REPO', repo_type='dataset')
for p in items:
    print(p)
" | shuf  # optional deterministic sort if needed
  )
fi

echo "Shard $SHARD_ID processing ${#FILES[@]} files"

# --
# Process each file via CDN (no HF API auth checks)
# --
process_file() {
  local rel_path="$1"
  local url="https://huggingface.co/datasets/${REPO}/resolve/main/${rel_path}"
  local dest="${TMP_DIR}/$(basename "$rel_path")"

  # CDN download (no Authorization header)
  curl -L --fail --retry 3 --retry-delay 5 -o "$dest" "$url"

  # Project to {prompt,response} and normalize
  python3 - "$dest" <<'PY'
import json, sys, hashlib, pyarrow.parquet as pq, pyarrow as pa, os, uuid, datetime

def hash_row(obj):
    return hashlib.md5(json.dumps(obj, sort_keys=True).encode()).hexdigest()

def extract_pairs(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".parquet":
        tbl = pq.read_table(path, columns=["prompt", "response"])
        for batch in tbl.to_batches(max_chunksize=1000):
            df = batch.to_pandas()
            for _, row in df.iterrows():
                if pd.isna(row.get("prompt")) or pd.isna(row.get("response")):
                    continue
                yield {
                    "prompt": str(row["prompt"]),
                    "response": str(row["response"]),
                }
    elif ext == ".jsonl":
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                prompt = obj.get("prompt") or obj.get("input") or ""
                response = obj.get("response") or obj.get("output") or ""
                if prompt and response:
                    yield {"prompt": str(prompt), "response": str(response)}
    else:
        return

import pandas as pd
for pair in extract_pairs(sys.argv[1]):
    pair["md5"] = hash_row(pair)
    print(json.dumps(pair, ensure_ascii=False))
PY
}

# --
# Stream process and append to shard output
# --
OUT_FILE="batches/public-merged/$(date +%Y-%m-%d)/shard${SHARD_ID}-$(date +%H%M%S).jsonl"
mkdir -p "$(dirname "$OUT_FILE")"
: > "$OUT_FILE"

for f in "${FILES[@]}"; do
  echo "Processing $f"
  process_file "$f" >> "$OUT_FILE" || echo "WARN: failed $f"
done

echo "Shard $SHARD_ID wrote $OUT_FILE"
```

---

### 3.

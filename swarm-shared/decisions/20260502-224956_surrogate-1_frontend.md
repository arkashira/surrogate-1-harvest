# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~120 lines total)

1. **`bin/list_files.py`** — single-call tree snapshot → JSON for embedding. Uses `list_repo_tree(path, recursive=False)` per date folder (avoids recursive 100× pagination). Saves `file_list.json` with `{"date": "...", "files": ["path1", ...], "snapshot_ts": ...}`. Exits 0 if empty; warns but succeeds.

2. **`bin/dataset-enrich.sh`** — embed file list, then CDN-only fetch. Accepts optional `FILE_LIST_JSON` (default: `file_list.json`). Worker computes its shard slice from `slug-hash % 16 == SHARD_ID`. Downloads via `curl -L "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/${file}"` (no auth header → bypasses API rate limit). Streams through Python normalizer (kept from current logic) and outputs to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

3. **`train.py`** (minimal add) — consume pre-listed files, CDN-only dataloader. Reads `file_list.json` embedded at launch. Uses `IterableDataset` that `curl | pyarrow` per file (no `load_dataset`). Projects to `{prompt, response}` only; drops extra cols. Deterministic order for reproducibility.

### Why this is highest value
- Eliminates HF API 429 during ingestion/training (CDN bypass).
- Avoids `list_repo_files` recursive pagination that triggers 100× rate-limit bursts.
- Keeps shard workers independent and deterministic (hash → shard).
- Fits <2h: ~60 lines Python + 30 lines Bash + 30 lines train loader.

---

## 1) `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
Create a deterministic snapshot of public dataset files for one date folder.
Usage:
  python list_files.py --repo axentx/surrogate-1-training-pairs \
                       --date 2026-05-02 \
                       --out file_list.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--out", default="file_list.json")
    args = parser.parse_args()

    api = HfApi()
    folder = args.date
    try:
        # Non-recursive to avoid 100× pagination bursts
        tree = api.list_repo_tree(repo_id=args.repo, path=folder, recursive=False)
    except Exception as exc:
        print(f"ERROR listing {args.repo}/{folder}: {exc}", file=sys.stderr)
        sys.exit(1)

    files = sorted([f.rfilename for f in tree if f.type == "file"])
    payload = {
        "date": folder,
        "snapshot_ts": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list_files.py
```

---

## 2) `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
# surrogate-1 shard worker: deterministic shard slice + CDN-only ingestion
#
# Required env:
#   SHARD_ID          (0-15)
#   HF_DATASET_REPO   (default: axentx/surrogate-1-training-pairs)
#   FILE_LIST_JSON    (default: file_list.json)
#   OUT_DIR           (default: batches/public-merged)
#
# Behavior:
#   - Reads FILE_LIST_JSON produced by bin/list_files.py
#   - Each file assigned to shard by slug-hash % 16
#   - Downloads via HF CDN (no auth header) to bypass API rate limits
#   - Streams through Python normalizer and writes shard-<N>-<ts>.jsonl

set -euo pipefail

HF_DATASET_REPO="${HF_DATASET_REPO:-axentx/surrogate-1-training-pairs}"
FILE_LIST_JSON="${FILE_LIST_JSON:-file_list.json}"
OUT_DIR="${OUT_DIR:-batches/public-merged}"
SHARD_ID="${SHARD_ID:?required}"
DATE_FOLDER="${DATE_FOLDER:-}"  # optional override; else taken from file_list.json

if [[ ! -f "$FILE_LIST_JSON" ]]; then
  echo "ERROR: $FILE_LIST_JSON not found. Run bin/list_files.py first." >&2
  exit 1
fi

# Python normalizer embedded here to avoid extra file churn
NORMALIZER_PY=$(cat <<'PY'
import sys
import json
import hashlib
import pyarrow.parquet as pq
import pyarrow as pa
import numpy as np
from io import BytesIO

def normalize_file(content_bytes, filename):
    """Project arbitrary HF files to {prompt,response} and yield dicts."""
    try:
        table = pq.read_table(pa.BufferReader(content_bytes))
    except Exception:
        # fallback: try line-delimited JSON
        for line in content_bytes.decode("utf-8", errors="replace").strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            prompt = obj.get("prompt") or obj.get("input") or obj.get("text") or ""
            response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
            if prompt and response:
                slug = filename.lower().replace("/", "_").replace(".", "_")
                h = hashlib.md5(f"{slug}:{prompt[:200]}:{response[:200]}".encode()).hexdigest()
                yield {"prompt": prompt, "response": response, "slug": slug, "md5": h}
        return

    cols = set(table.column_names)
    prompt_col = next((c for c in ("prompt", "input", "text") if c in cols), None)
    resp_col = next((c for c in ("response", "output", "completion") if c in cols), None)

    if prompt_col and resp_col:
        prompts = table.column(prompt_col).to_pylist()
        responses = table.column(resp_col).to_pylist()
        for p, r in zip(prompts, responses):
            if p and r:
                slug = filename.lower().replace("/", "_").replace(".", "_")
                h = hashlib.md5(f"{slug}:{str(p)[:200]}:{str(r)[:200]}".encode()).hexdigest()
                yield {"prompt": str(p), "response": str(r), "slug": slug, "md5": h}
    else:
        # best-effort: dump rows as-is with filename attribution
        for i in range(table.num_rows):
            row = {k: str(table.column(k)[i].as_py()) for k in cols}
            row["_filename"] = filename
            yield row
PY
)

# Determine date folder from file_list.json if not provided
if [[ -z "$DATE_FOLDER" ]]; then
  DATE_FOLDER=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('date',''))" "$FILE_LIST_JSON")
fi

if [[ -z "$DATE_FOLDER" ]]; then
  echo "ERROR: could not determine date folder from $FILE_LIST_JSON" >&2
  exit 1
fi

TIMESTAMP=$(date -u +"%H%M%S")
OUT_FILE="${OUT_DIR}/${DATE_FOLDER}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"
mkdir -p "$(dirname "$OUT_FILE")"

echo "Starting shard $SHARD_ID -> $OUT_FILE"

# Process each file: assign to shard by slug-hash % 16, then CDN fetch + normalize
python3 -c "
import

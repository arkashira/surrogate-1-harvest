# surrogate-1 / quality

## Final Synthesis (one answer)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers fully resilient.

### Concrete plan (≤2h)

1. **Pre-flight file listing**  
   - Add `bin/list_files.py` (Mac/Linux).  
   - Run once per date folder after rate-limit window clears.  
   - Output: `file-list-<date>.json` committed to repo root.  
   - Workers read this list and never call `list_repo_tree`/`list_repo_files` recursively during training or enrichment.

2. **Deterministic shard assignment**  
   - `bin/dataset-enrich.sh` accepts optional `FILE_LIST`.  
   - If provided: assign files to shards by `hash(slug) % 16 == SHARD_ID`.  
   - If not provided: fallback to recursive listing with a clear warning (avoid in production).

3. **CDN-only ingestion**  
   - Replace `load_dataset(streaming=True)` and recursive API calls with direct CDN downloads (`hf_hub_download` or raw `https://huggingface.co/datasets/.../resolve/main/...`).  
   - Project only `{prompt, response}` at read time; drop other columns.  
   - Write outputs as `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

4. **Resilience and idempotency**  
   - Lightweight retry/backoff for CDN downloads:  
     - 429 from CDN → wait 60s and retry.  
     - Network errors → exponential backoff up to 5 attempts.  
   - Keep `lib/dedup.py` central md5 store as source of truth; workers may produce duplicates but downstream dedup will catch them.

5. **Training loader (CDN-only)**  
   - Add `bin/train-cdn-only.py` skeleton that reads the embedded file list and streams via raw CDN URLs (no auth, no API rate limits).  
   - Integrate into Lightning `IterableDataset`.

6. **Validation**  
   - Dry-run one shard (`SHARD_ID=0`) on a small date folder before enabling the full 16-shard matrix.

---

### 1) `bin/list_files.py`
```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in surrogate-1-training-pairs.
Usage:
    python bin/list_files.py --date 2026-04-29 --out file-list-2026-04-29.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

REPO_ID = "datasets/axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-04-29")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    api = HfApi(token=os.getenv("HF_TOKEN"))
    try:
        tree = api.list_repo_tree(
            repo_id=REPO_ID,
            path=args.date,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as e:
        print(f"Failed to list {args.date}: {e}", file=sys.stderr)
        sys.exit(1)

    files = [entry.path for entry in tree if entry.type == "file"]
    files.sort()

    payload = {
        "date": args.date,
        "repo": REPO_ID,
        "files": files,
        "count": len(files),
    }

    with open(args.out, "w") as f:
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

### 2) `bin/dataset-enrich.sh` (key excerpts)
```bash
#!/usr/bin/env bash
set -euo pipefail

# Required env
: "${HF_TOKEN:?Need HF_TOKEN}"
: "${SHARD_ID:?Need SHARD_ID 0..15}"
: "${DATE:?Need DATE e.g. 2026-04-29}"

REPO="datasets/axentx/surrogate-1-training-pairs"
OUTDIR="batches/public-merged/${DATE}"
mkdir -p "${OUTDIR}"

# Optional file list for deterministic slicing
FILE_LIST="${FILE_LIST:-}"
declare -a TARGET_FILES=()

if [[ -n "${FILE_LIST}" && -f "${FILE_LIST}" ]]; then
  echo "Using file list: ${FILE_LIST}"
  mapfile -t TARGET_FILES < <(
    python3 -c "
import json, sys, hashlib
data = json.load(open(sys.argv[1]))
files = data['files']
shard = int(sys.argv[2])
for f in files:
    h = int(hashlib.sha256(f.encode()).hexdigest(), 16)
    if h % 16 == shard:
        print(f)
" "${FILE_LIST}" "${SHARD_ID}"
  )
else
  echo "WARNING: No FILE_LIST provided; falling back to recursive list (may hit API limits)"
  mapfile -t TARGET_FILES < <(
    python3 -c "
import os, sys
from huggingface_hub import HfApi
api = HfApi(token=os.getenv('HF_TOKEN'))
tree = api.list_repo_tree(
    repo_id='${REPO}',
    path='${DATE}',
    repo_type='dataset',
    recursive=True,
)
for entry in tree:
    if entry.type == 'file':
        print(entry.path)
"
  )
fi

if [[ ${#TARGET_FILES[@]} -eq 0 ]]; then
  echo "No files assigned to shard ${SHARD_ID}"
  exit 0
fi

echo "Shard ${SHARD_ID} processing ${#TARGET_FILES[@]} files"

# Process each file via CDN download + projection
for rel_path in "${TARGET_FILES[@]}"; do
  tmp=$(mktemp)
  python3 -c "
import os, sys, json, pyarrow.parquet as pq
from huggingface_hub import hf_hub_download
path = sys.argv[1]
out = sys.argv[2]
local = hf_hub_download(
    repo_id='${REPO}',
    filename=path,
    repo_type='dataset',
    token=os.getenv('HF_TOKEN'),
)
# Project only prompt/response; ignore other columns
try:
    tbl = pq.read_table(local, columns=['prompt', 'response'])
except Exception:
    # Fallback: try common aliases
    try:
        tbl = pq.read_table(local, columns=['instruction', 'output'])
        tbl = tbl.rename_columns(['prompt', 'response'])
    except Exception:
        print(f'Could not project {path}', file=sys.stderr)
        sys.exit(0)

with open(out, 'ab') as f:
    for batch in tbl.to_batches():
        for i in range(batch.num_rows):
            row = {
                'prompt': batch['prompt'][i].as_py(),
                'response': batch['response'][i].as_py(),
            }
            f.write(json.dumps(row, ensure_ascii=False).encode() + b'\n')
" "${rel_path}" "${tmp}" || continue

  # Append to shard output (one file per run for idempotency)
  ts=$(date -u +"%H%M%S")
  shard_out="${OUTDIR}/shard${SHARD_ID}-${ts}.jsonl"
  cat "${tmp}" >> "${shard_out}"
  rm -f "${tmp}"
done

echo "Shard ${SHARD_ID} finished -> ${shard_out}"
```
Make executable:
```bash
chmod +x bin/dataset-enrich.sh
```

---

### 3) `bin/train-cdn-only.py` (Lightning CDN-only IterableDataset)
```python
#!/usr/bin/env python3
import json
import os
from pathlib import Path
from typing import Iterator, Dict, Any

import requests
from torch.utils.data import IterableDataset

REPO = "datasets/axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO}/resolve/main"

class CDNPar

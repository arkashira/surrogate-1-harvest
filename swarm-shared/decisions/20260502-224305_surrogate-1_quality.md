# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~120 lines total)

1. **`bin/list_files.py`** — one-shot script to snapshot a date folder via `list_repo_tree`, emit `file-list.json` (path + size + sha256). Embeds into training scripts so Lightning workers do **zero API calls** during data load.
2. **`bin/dataset-enrich.sh`** — updated to accept optional `FILE_LIST` env var (path to JSON). If present, workers iterate the local list and fetch via CDN (bypasses `/api/` rate limits); falls back to existing streaming behavior for compatibility.
3. **`requirements.txt`** — add `requests` for reliable CDN downloads.

---

### 1) `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
Snapshot one date folder from axentx/surrogate-1-training-pairs.

Usage:
  HF_TOKEN=<token> python bin/list_files.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-02 \
    --out file-list.json

Outputs JSON list:
[
  {"path": "batches/public-merged/2026-05-02/shard0-123456.jsonl", "size": 12345, "sha256": "..."},
  ...
]
"""

import argparse
import json
import os
import sys
from typing import List, Dict

from huggingface_hub import HfApi

REPO_DEFAULT = "axentx/surrogate-1-training-pairs"

def list_date_folder(repo_id: str, date: str) -> List[Dict]:
    api = HfApi(token=os.getenv("HF_TOKEN"))
    # non-recursive per-folder to avoid heavy pagination
    tree = api.list_repo_tree(
        repo_id=repo_id,
        path=f"batches/public-merged/{date}",
        recursive=False,
    )
    out = []
    for entry in tree:
        if entry.type != "file":
            continue
        out.append({
            "path": f"batches/public-merged/{date}/{entry.path}",
            "size": getattr(entry, "size", None),
            "sha256": getattr(entry, "lfs", {}).get("oid", None) if hasattr(entry, "lfs") else None,
        })
    return out

def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot date folder file list.")
    parser.add_argument("--repo", default=REPO_DEFAULT, help="HF dataset repo id")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder under batches/public-merged/")
    parser.add_argument("--out", default="file-list.json", help="Output JSON path")
    args = parser.parse_args()

    if not os.getenv("HF_TOKEN"):
        print("ERROR: HF_TOKEN env var required", file=sys.stderr)
        sys.exit(1)

    try:
        items = list_date_folder(args.repo, args.date)
    except Exception as exc:
        print(f"ERROR: failed to list folder: {exc}", file=sys.stderr)
        sys.exit(1)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)

    print(f"Wrote {len(items)} entries to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list_files.py
```

---

### 2) `bin/dataset-enrich.sh` (patch)

Add near top (after `set -euo pipefail`):

```bash
# Optional pre-computed file list to avoid HF API list_repo_files/load_dataset(streaming=True)
# If provided, workers will iterate local list and fetch via CDN (no auth/API calls during data load).
FILE_LIST="${FILE_LIST:-}"
```

Replace the dataset-loading section (where `load_dataset` or repo file listing happens) with:

```bash
if [ -n "${FILE_LIST}" ] && [ -f "${FILE_LIST}" ]; then
  echo "Using pre-computed file list: ${FILE_LIST}"
  # Iterate local list; download each file via CDN (no Authorization header -> bypasses /api/ rate limits)
  python3 -c "
import json, os, sys, hashlib, tempfile
from pathlib import Path

try:
    import requests
except ImportError:
    print('ERROR: requests is required for CDN fetches. Add it to requirements.txt', file=sys.stderr)
    sys.exit(1)

file_list_path = sys.argv[1]
work_dir = Path(sys.argv[2])
file_list = json.load(open(file_list_path))

shard_id = int(os.environ.get('SHARD_ID', '0'))
shard_total = int(os.environ.get('SHARD_TOTAL', '16'))
dataset_repo = os.environ.get('DATASET_REPO', 'axentx/surrogate-1-training-pairs')
iter_ts = os.environ.get('ITER_TS', 'unknown')

def deterministic_bucket(path: str) -> int:
    # same deterministic hash used by runner matrix
    return hash(path) % shard_total

selected = [e for e in file_list if deterministic_bucket(e['path']) == shard_id]
print(f'Processing {len(selected)} files for shard {shard_id}/{shard_total}')

work_dir.mkdir(parents=True, exist_ok=True)
records = []
for entry in selected:
    url = f'https://huggingface.co/datasets/{dataset_repo}/resolve/main/{entry[\"path\"]}'
    try:
        # stream download via CDN (no auth)
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False, suffix='.tmp') as tmp:
                for chunk in r.iter_content(chunk_size=8192):
                    tmp.write(chunk)
                tmp_path = tmp.name

        # project to {prompt,response} per surrogate-1 schema rules
        try:
            import pyarrow.parquet as pq
            table = pq.read_table(tmp_path)
            schema_names = [f.name for f in table.schema]
            if 'prompt' in schema_names and 'response' in schema_names:
                df = table.select_columns(['prompt', 'response']).to_pandas()
            else:
                # fallback: include all columns and let downstream schema projection handle it
                df = table.to_pandas()
        except Exception:
            # fallback for non-parquet or malformed files
            import pandas as pd
            df = pd.read_json(tmp_path, lines=True) if tmp_path.endswith('.jsonl') else pd.DataFrame()

        os.unlink(tmp_path)

        for _, row in df.iterrows():
            records.append({
                'prompt': str(row.get('prompt', '')),
                'response': str(row.get('response', '')),
                '_source_file': entry['path'],
            })
    except Exception as e:
        print(f'Failed {url}: {e}', file=sys.stderr)

out_file = work_dir / f'shard{shard_id}-{iter_ts}.jsonl'
with open(out_file, 'w', encoding='utf-8') as f:
    for r in records:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')
print(f'Wrote {len(records)} records to {out_file}')
" "${FILE_LIST}" "./work"
else
  echo "FILE_LIST not provided or missing; falling back to streaming dataset (may hit HF API limits)"
  # existing streaming logic here (unchanged)
  python3 -c "
from datasets import load_dataset
import os, json
shard_id = int(os.environ.get('SHARD_ID','0'))
shard_total = int(os.environ.get('SHARD_TOTAL','16'))
def deterministic_bucket(path: str) -> int:
    return hash(path) % shard_total
ds = load_dataset('axentx/surrogate-1-training-pairs', split='train', streaming=True)
records = []
for x in ds:
    src = x.get('_source_file','') or x.get('path','')
    if deterministic_bucket(src) != shard_id:
        continue
    records.append({
        'prompt': str(x.get('prompt','')),
        'response': str(x.get

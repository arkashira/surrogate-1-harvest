# surrogate-1 / discovery

### Final Synthesis (Best Parts + Correctness + Actionability)

**Chosen fix**: **Pre-list files once → CDN-only ingestion**  
Combines Candidate 2’s CDN bypass and orchestration with Candidate 1’s pragmatic retry/robustness for the one-time list step. This eliminates 429s during training, costs nothing, and ships in <2h.

---

## Why this is highest-value (<2h ROI)
- **Directly unblocks surrogate-1 training**: removes HF API calls during data loading (the main 429 source).
- **Zero ongoing rate-limit risk**: downloads use public CDN URLs; no auth or HF client quotas.
- **Minimal change, maximum reliability**: one orchestrator-side `list_repo_tree` call per cron, then pure CDN ingestion.
- **Backward compatible**: keeps a safe fallback path for ad-hoc runs.

---

## Implementation (single coherent plan)

### 1. CDN utility + one-time lister (`lib/cdn.py`)
```python
# lib/cdn.py
import json
import time
import requests
from pathlib import Path
from typing import List, Dict
from huggingface_hub import list_repo_tree

HF_DATASETS_BASE = "https://huggingface.co/datasets"

def build_cdn_url(repo: str, filepath: str) -> str:
    return f"{HF_DATASETS_BASE}/{repo}/resolve/main/{filepath}"

def robust_list_repo_tree(repo: str, folder: str = "", recursive: bool = True, max_retries: int = 3) -> List[Dict]:
    """One-time listing with 429 retry (Candidate 1 robustness)."""
    for attempt in range(1, max_retries + 1):
        try:
            return list(list_repo_tree(repo, folder=folder, recursive=recursive))
        except Exception as e:
            if hasattr(e, "response") and getattr(e.response, "status_code", None) == 429 and attempt < max_retries:
                wait = 60 * attempt
                time.sleep(wait)
                continue
            raise

def generate_file_list(repo: str, output_path: str, folder: str = "", recursive: bool = True) -> List[Dict]:
    items = robust_list_repo_tree(repo, folder=folder, recursive=recursive)
    files = [
        {"path": item.path, "cdn_url": build_cdn_url(repo, item.path), "size": getattr(item, "size", None)}
        for item in items
        if item.type == "file" and item.path.endswith((".jsonl", ".parquet", ".json"))
    ]
    Path(output_path).write_text(json.dumps(files, indent=2))
    print(f"Saved {len(files)} files to {output_path}")
    return files

def load_file_list(path: str) -> List[Dict]:
    return json.loads(Path(path).read_text())
```

### 2. Updated dataset-enrich script (`bin/dataset-enrich.sh`)
- Accepts `FILE_LIST` JSON (CDN URLs) for zero-HF-API ingestion.
- Deterministic sharding preserved.
- Safe fallback to streaming if no file list provided.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
FILE_LIST="${FILE_LIST:-}"
DATE=$(date +%Y-%m-%d)
TIMESTAMP=$(date +%H%M%S)
OUTPUT="batches/public-merged/${DATE}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

echo "Starting shard ${SHARD_ID}/${TOTAL_SHARDS} | ${DATE} ${TIMESTAMP}"
mkdir -p "$(dirname "$OUTPUT")"

if [[ -n "$FILE_LIST" && -f "$FILE_LIST" ]]; then
    echo "Using pre-generated file list: $FILE_LIST"
    python - <<PY
import json, hashlib, sys
import requests
import pyarrow.parquet as pq
import pyarrow as pa

with open("$FILE_LIST") as f:
    files = json.load(f)

def shard_for(slug: str, total: int) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % total

shard_id = int("$SHARD_ID")
total_shards = int("$TOTAL_SHARDS")
output_path = "$OUTPUT"
processed = 0

for entry in files:
    slug = entry["path"].rsplit(".", 1)[0]
    if shard_for(slug, total_shards) != shard_id:
        continue
    try:
        resp = requests.get(entry["cdn_url"], timeout=30)
        resp.raise_for_status()
        if entry["path"].endswith(".parquet"):
            tbl = pq.read_table(pa.BufferReader(resp.content))
            df = tbl.to_pandas()
            prompt_col = next((c for c in df.columns if "prompt" in c.lower()), None)
            response_col = next((c for c in df.columns if "response" in c.lower()), None)
            if prompt_col and response_col:
                for _, row in df.iterrows():
                    print(json.dumps({"prompt": str(row[prompt_col]), "response": str(row[response_col])}))
                    processed += 1
        elif entry["path"].endswith(".jsonl"):
            for line in resp.text.strip().split('\n'):
                if line:
                    data = json.loads(line)
                    prompt = data.get("prompt") or data.get("input") or ""
                    response = data.get("response") or data.get("output") or ""
                    if prompt and response:
                        print(json.dumps({"prompt": str(prompt), "response": str(response)}))
                        processed += 1
    except Exception as e:
        print(f"Error processing {entry['path']}: {e}", file=sys.stderr)
        continue

print(f"Processed {processed} pairs for shard {shard_id}", file=sys.stderr)
PY
else
    echo "WARNING: No FILE_LIST provided, falling back to streaming (rate-limited)"
    python -c "
from datasets import load_dataset
import json, hashlib
ds = load_dataset('$REPO', split='train', streaming=True)
for item in ds:
    slug = item.get('source', 'unknown')
    if int(hashlib.md5(slug.encode()).hexdigest(), 16) % $TOTAL_SHARDS == $SHARD_ID:
        print(json.dumps({'prompt': item.get('prompt',''), 'response': item.get('response','')}))
" > "$OUTPUT"
fi

echo "Shard ${SHARD_ID} complete: ${OUTPUT}"
```

### 3. Updated GitHub Actions workflow (`.github/workflows/ingest.yml`)
- Runs `generate_file_list` once per cron.
- Passes the file list to each shard via artifact.
- Each shard uses CDN-only downloads.

```yaml
# .github/workflows/ingest.yml
name: Dataset Ingest (CDN Bypass)

on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:

jobs:
  prepare-filelist:
    runs-on: ubuntu-latest
    outputs:
      filelist-name: ${{ steps.set.outputs.filelist-name }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install huggingface_hub pyarrow requests
      - run: python -m lib.cdn generate_file_list --repo axentx/surrogate-1-training-pairs --out filelist.json
      - uses: actions/upload-artifact@v4
        with:
          name: filelist
          path: filelist.json
      - id: set
        run: echo "filelist-name=filelist.json" >> $GITHUB_OUTPUT

  ingest-shards:
    needs: prepare-filelist
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install pyarrow requests
      - uses: actions/download-artifact@v4
        with:
          name: filelist
      - run: |
          chmod

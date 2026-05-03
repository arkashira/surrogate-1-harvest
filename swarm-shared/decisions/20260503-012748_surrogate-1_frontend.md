# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value fix**  
Replace recursive HF API ingestion and per-file authenticated fetches with a single non-recursive `list_repo_tree` per date folder + deterministic shard routing + CDN-only fetches. This eliminates rate-limit pressure and removes per-file auth overhead during data loading.

### Steps (concrete, <2h)

1. **Update `bin/dataset-enrich.sh`**  
   - Accept `DATE_FOLDER` and `SHARD_ID`/`TOTAL_SHARDS` as env params (already passed from matrix).  
   - Use `list_repo_tree(path=date_folder, recursive=False)` from Mac/orchestrator once per date folder and emit a stable file list JSON.  
   - Deterministic shard assignment: `hash(slug) % TOTAL_SHARDS == SHARD_ID`.  
   - For assigned files, download via CDN URL (`https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}/{file}`) with no Authorization header.  
   - Stream-parse each file, project to `{prompt, response}`, compute md5, dedup against central store, append to shard output.

2. **Add lightweight Python helper (`bin/lib/cdn_ingest.py`)**  
   - Single function: given repo, date_folder, shard_id, total_shards → iterate assigned files via CDN, yield normalized pairs.  
   - Uses `requests` with streaming and `pyarrow`/`pandas` for parquet/JSONL projection.  
   - No `load_dataset` or `hf_hub_download` during streaming (avoid schema heterogeneity issues).  
   - Central dedup via `lib/dedup.py` (unchanged).

3. **Update GitHub Actions matrix (`ingest.yml`)**  
   - Pass `DATE_FOLDER` from workflow dispatch or compute as `YYYY-MM-DD` from run time.  
   - Keep 16-shard matrix; each job runs `dataset-enrich.sh` with its `SHARD_ID` and `TOTAL_SHARDS=16`.  
   - Ensure `HF_TOKEN` only used for repo list call and final push (not during CDN fetches).

4. **Output path**  
   - Write to `batches/public-merged/<date>/shard<SHARD_ID>-<HHMMSS>.jsonl`.  
   - Commit with deterministic filenames to avoid collisions.

5. **Validation & rollback**  
   - Dry-run locally with a small date folder to verify shard assignment and CDN fetch.  
   - If CDN fails, fallback to authenticated `hf_hub_download` for that file only (rare).  
   - On 429 from tree API, wait 360s and retry (per HF rate-limit pattern).

---

### Code snippets

#### `bin/dataset-enrich.sh` (updated)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE_FOLDER="${DATE_FOLDER:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
HF_TOKEN="${HF_TOKEN:-}"
OUTDIR="batches/public-merged/${DATE_FOLDER}"
TS=$(date +%H%M%S)
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "$(dirname "${OUTFILE}")"

echo "[$(date)] Shard ${SHARD_ID}/${TOTAL_SHARDS} | date=${DATE_FOLDER}"

# 1) Get file list via HF API (single non-recursive call)
# If token present, use it; public repos may not require it for list_repo_tree.
FILE_LIST=$(mktemp)
if [ -n "${HF_TOKEN}" ]; then
  curl -s -H "Authorization: Bearer ${HF_TOKEN}" \
    "https://huggingface.co/api/datasets/${REPO}/tree?path=${DATE_FOLDER}&recursive=false" \
    > "${FILE_LIST}"
else
  curl -s \
    "https://huggingface.co/api/datasets/${REPO}/tree?path=${DATE_FOLDER}&recursive=false" \
    > "${FILE_LIST}"
fi

# 2) Deterministic shard assignment + CDN fetch + normalize
python3 bin/lib/cdn_ingest.py \
  --repo "${REPO}" \
  --date-folder "${DATE_FOLDER}" \
  --shard-id "${SHARD_ID}" \
  --total-shards "${TOTAL_SHARDS}" \
  --file-list "${FILE_LIST}" \
  --output "${OUTFILE}" \
  --hf-token "${HF_TOKEN}"

# 3) Push output (only this shard's file)
if [ -n "${HF_TOKEN}" ]; then
  git config --global user.email "runner@axentx"
  git config --global user.name "surrogate-1-runner"
  git add "${OUTFILE}"
  git commit -m "shard${SHARD_ID} ${DATE_FOLDER} ${TS}" || true
  git push origin HEAD
fi

rm -f "${FILE_LIST}"
echo "[$(date)] Shard ${SHARD_ID} done -> ${OUTFILE}"
```

#### `bin/lib/cdn_ingest.py` (new)
```python
#!/usr/bin/env python3
import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests

from lib.dedup import DedupStore  # central md5 dedup store

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}/{file}"


def deterministic_shard(slug: str, total_shards: int) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % total_shards


def project_to_pair(obj) -> dict:
    # Heuristic projection: prefer known keys; tolerate schema heterogeneity
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
    response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}


def stream_cdn_file(url: str):
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        content = r.content

    # Try parquet first
    try:
        table = pq.read_table(pa.BufferReader(content))
        for batch in table.to_batches(max_chunksize=1000):
            for row in batch.to_pylist():
                yield row
        return
    except Exception:
        pass

    # Try JSON/JSONL
    text = content.decode("utf-8", errors="replace")
    # JSONL
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue

    # Single JSON
    try:
        single = json.loads(text)
        if isinstance(single, list):
            for item in single:
                yield item
        else:
            yield single
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date-folder", required=True)
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--total-shards", type=int, required=True)
    parser.add_argument("--file-list", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--hf-token", default="")
    args = parser.parse_args()

    with open(args.file_list, "r") as f:
        entries = json.load(f)

    dedup = DedupStore()
    os.makedirs(Path(args.output).parent, exist_ok=True)

    headers = {"Authorization": f"Bearer {args.hf_token}"} if args.hf_token else {}
    out_rows = []

    for entry in entries:
        path = entry.get("path")
        if not path:
            continue
        slug = f"{args.date_folder}/{path}"
        if deterministic_shard(slug, args.total_shards) != args.shard_id:
            continue

        url = CDN_TEMPLATE.format(repo=args.repo, date_folder=args.date_folder, file=path)
        try:
            for raw in stream_cdn_file(url):
                pair = project_to_pair(raw)
                if not pair["prompt"] and not pair["response"]:
                    continue
                md5 = hashlib.md5(json.dumps(pair, sort_keys=True

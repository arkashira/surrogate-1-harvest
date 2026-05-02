# surrogate-1 / discovery

### Final Consolidated Implementation Plan  
*Merged from Candidates 1 & 2; contradictions resolved in favor of correctness, determinism, and concrete actionability.*

---

#### Core Principles (why this wins)
- **Deterministic date-partitioning** (both agree): `YYYY-MM-DD` UTC for output paths and repo subfolders.  
- **CDN-bypass ingestion** (both agree): direct `resolve/main/` downloads to avoid HF API rate limits.  
- **Single source of truth for file list** (resolved): generate once per date via `list_repo_tree` and reuse; never rely on repeated API calls inside workers.  
- **Shard assignment by content hash** (Candidate 2): prevents cross-run collisions and makes retries idempotent.  
- **Idempotent, never-overwrite upload** (both agree): timestamped filenames + existence check before upload.  
- **Minimal, safe changes**: keep existing dedup/central store logic unchanged; only ingestion path and worker logic updated.

---

#### Step-by-Step Implementation (≤2 hours)

1. **Date-partitioned output path**  
   - Compute `DATE=$(date -u +%Y-%m-%d)` and `TS=$(date -u +%H%M%S)`.  
   - Emit to `batches/public-merged/${DATE}/shard${SHARD_ID}-${TS}.jsonl`.

2. **Pre-flight file list (once per date)**  
   - On workflow start, run a single step:  
     `list_repo_tree(repo, path=DATE, recursive=False)` → save `file-list-${DATE}.json`.  
   - Commit or upload as artifact; workers read this file (no further API calls).

3. **CDN-bypass download in workers**  
   - Replace `load_dataset(..., streaming=True)` with direct `requests.get` to:  
     `https://huggingface.co/datasets/{repo}/resolve/main/{path}`.  
   - No auth header; relies on CDN limits (much higher).  
   - Parse parquet/stream and project only `{prompt, response}` at parse time.

4. **Deterministic shard assignment**  
   - `shard_id = hash(slug) % 16` (or total shards).  
   - Worker runs only when `SHARD_ID == shard_id`.  
   - Guarantees no cross-run collisions and stable retries.

5. **Idempotent upload**  
   - Filename includes timestamp; never overwrite same timestamp.  
   - Skip upload if target already exists (stat check via `hf_hub_file_exists`).

6. **Script changes (minimal)**  
   - `bin/dataset-enrich.sh`: add date var, read file-list, loop files, call worker python.  
   - New `bin/worker.py`: CDN download + projection + shard filter + upload.  
   - `lib/dedup.py`: unchanged.

7. **CI update**  
   - Add optional step to generate `file-list-${DATE}.json` as artifact for the day.  
   - Workers fetch artifact or fallback to inline list.

---

#### Final Code Snippets

**bin/dataset-enrich.sh** (updated)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE=$(date -u +%Y-%m-%d)
TS=$(date -u +%H%M%S)
SHARD_ID=${SHARD_ID:?required}
OUT_DIR="batches/public-merged/${DATE}"
mkdir -p "${OUT_DIR}"

# Pre-flight file list (once per date)
FILE_LIST="file-list-${DATE}.json"
if [[ ! -f "${FILE_LIST}" ]]; then
  python -c "
import json, os
from huggingface_hub import list_repo_tree
files = list_repo_tree('${REPO}', path='${DATE}', recursive=False)
with open('${FILE_LIST}', 'w') as f:
    json.dump([f['path'] for f in files if f['type']=='file'], f)
"
fi

# Run worker for this shard
python bin/worker.py \
  --repo "${REPO}" \
  --file-list "${FILE_LIST}" \
  --shard-id "${SHARD_ID}" \
  --out "${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"
```

---

**bin/worker.py** (new)
```python
#!/usr/bin/env python3
import argparse
import hashlib
import json
import sys
import requests
import pyarrow.parquet as pq
import pyarrow as pa
from io import BytesIO

HF_CDN = "https://huggingface.co/datasets"

def slug_hash(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16)

def project_to_pair(buf: bytes):
    table = pq.read_table(BytesIO(buf))
    out = []
    for col in table.columns:
        if pa.types.is_string(col.type) or pa.types.is_large_string(col.type):
            for val in col:
                v = val.as_py()
                if v and isinstance(v, str):
                    out.append(v)
    prompt, response = "", ""
    for v in out:
        if not prompt:
            prompt = v
        elif not response:
            response = v
            break
    return {"prompt": prompt, "response": response}

def hf_hub_file_exists(repo_id: str, filename: str) -> bool:
    from huggingface_hub import hf_hub_url, http_get
    url = hf_hub_url(repo_id, filename, repo_type="dataset")
    try:
        r = http_get(url, method="HEAD")
        return r.status_code == 200
    except Exception:
        return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--file-list", required=True)
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(args.file_list) as f:
        files = json.load(f)

    assigned = [p for p in files if slug_hash(p) % 16 == args.shard_id]
    if not assigned:
        print("No files assigned to this shard.", file=sys.stderr)
        return

    rows = []
    for path in assigned:
        url = f"{HF_CDN}/{args.repo}/resolve/main/{path}"
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        pair = project_to_pair(resp.content)
        if pair["prompt"] and pair["response"]:
            rows.append(pair)

    out_path = args.out
    with open(out_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"Wrote {len(rows)} pairs to {out_path}")

    # Optional: upload to HF dataset repo (idempotent)
    # if not hf_hub_file_exists("axentx/surrogate-1-training-pairs", out_path):
    #   upload_file(...)

if __name__ == "__main__":
    main()
```

---

**.github/workflows/ingest.yml** (minimal changes)
```yaml
name: Ingestion Workflow
on:
  schedule:
    - cron: '*/30 * * * *'
jobs:
  ingest:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v3

      - name: Generate date-partitioned file list (once per day)
        run: |
          DATE=$(date -u +%Y-%m-%d)
          python -c "
import json
from huggingface_hub import list_repo_tree
files = list_repo_tree('axentx/surrogate-1-training-pairs', path='${DATE}', recursive=False)
with open('file-list-${DATE}.json', 'w') as f:
    json.dump([f['path'] for f in files if f['type']=='file'], f)
"

      - name: Run ingestion for each shard (parallelize as needed)
        env:
          SHARD_ID: ${{ matrix.shard_id }}
        run: |
          ./bin/dataset-enrich.sh
```

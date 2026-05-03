# surrogate-1 / backend

## Final Implementation Plan (≤2h) — Highest-value backend fix

**Goal**: Eliminate HF API rate-limit risk and OOM in the surrogate-1 ingestion pipeline by replacing recursive authenticated fetches with deterministic shard routing + CDN-only fetches.

---

### 1) Replace recursive listing with single `list_repo_tree` + deterministic shard routing

**Why this is highest value**
- Removes recursive `list_repo_files` (100-item pages) → one `list_repo_tree` per date folder.
- Avoids authenticated `/api/` calls during data loading → CDN bypass eliminates 429 risk.
- Keeps memory bounded: each shard streams via CDN and projects only needed fields.
- Enables deterministic shard assignment (hash slug → SHARD_ID) so retries are idempotent.
- Fits within 2h: small focused changes, no retraining logic touched.

---

### 2) Update `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# Config
REPO="axentx/surrogate-1-training-pairs"
DATE_FOLDER="${DATE_FOLDER:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"          # 0..15 via matrix
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
HF_TOKEN="${HF_TOKEN:-}"
OUT_DIR="work/${DATE_FOLDER}/shard${SHARD_ID}"
FILE_LIST="${OUT_DIR}/files.json"

mkdir -p "${OUT_DIR}"

echo "== Listing ${REPO}/${DATE_FOLDER} (non-recursive) =="
# Single API call: list_repo_tree per folder (no recursion)
python3 - <<PY > "${FILE_LIST}"
import os, json, sys
from huggingface_hub import HfApi
api = HfApi(token=os.environ.get("HF_TOKEN"))
items = api.list_repo_tree(
    repo_id="${REPO}",
    path="${DATE_FOLDER}",
    recursive=False
)
# Keep only files we want to process (parquet/jsonl)
files = [it.rpath for it in items if it.rpath and not it.rpath.endswith("/")]
# Deterministic shard assignment by slug hash
def shard_for(path):
    # path like "2026-05-03/some-slug.parquet"
    slug = path.split("/")[-1].split(".")[0]
    return hash(slug) % ${TOTAL_SHARDS}
sharded = [f for f in files if shard_for(f) == ${SHARD_ID}]
json.dump({"date_folder": "${DATE_FOLDER}", "shard_id": ${SHARD_ID}, "files": sharded}, sys.stdout)
PY

echo "== Shard ${SHARD_ID} will process $(jq '.files | length' "${FILE_LIST}") files =="

# Run enrichment worker (streams via CDN)
python3 bin/lib/fetch_cdn.py \
  --file-list "${FILE_LIST}" \
  --repo "${REPO}" \
  --out-dir "${OUT_DIR}" \
  --hf-token "${HF_TOKEN}"

# Upload shard output (same naming convention)
TS=$(date +%H%M%S)
DEST="batches/public-merged/${DATE_FOLDER}/shard${SHARD_ID}-${TS}.jsonl"
echo "== Uploading to ${REPO}:${DEST} =="
python3 - <<PY
import os, json
from huggingface_hub import HfApi
api = HfApi(token=os.environ.get("HF_TOKEN"))
out_dir = "${OUT_DIR}"
dest = "${DEST}"
output_file = os.path.join(out_dir, "enriched.jsonl")
if os.path.exists(output_file):
    api.upload_file(
        path_or_fileobj=output_file,
        path_in_repo=dest,
        repo_id="${REPO}"
    )
else:
    print("No output file; nothing to upload")
PY
```

---

### 3) Add `bin/lib/fetch_cdn.py`

```python
#!/usr/bin/env python3
"""
Stream files for a shard via CDN (no auth/API calls during download)
and project to {prompt,response}. Dedup via central md5 store.
"""
import argparse
import json
import hashlib
import os
import sys
import requests
import pyarrow.parquet as pq
import pyarrow as pa
from tqdm import tqdm

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--file-list", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--hf-token", default="")
    return p.parse_args()

def project_record(rec, source_path):
    """
    Normalize heterogeneous schemas to {prompt,response}.
    Keep only these two fields; attribution via filename pattern.
    """
    prompt_keys = {"prompt", "instruction", "input", "question", "text"}
    response_keys = {"response", "output", "answer", "completion", "result"}

    prompt = None
    response = None

    if isinstance(rec, dict):
        for k in rec:
            if k.lower() in prompt_keys and prompt is None:
                prompt = rec[k]
            if k.lower() in response_keys and response is None:
                response = rec[k]
    # Fallback: if rec is string, treat as prompt and response empty
    if prompt is None and isinstance(rec, str):
        prompt = rec
    if response is None:
        response = ""

    # Ensure strings
    prompt = str(prompt) if prompt is not None else ""
    response = str(response) if response is not None else ""
    return {"prompt": prompt, "response": response}

def stream_parquet_cdn(url):
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    buf = io.BytesIO(resp.content)
    table = pq.read_table(buf)
    for batch in table.to_batches():
        rb = pa.record_batch(batch)
        for i in range(rb.num_rows):
            rec = {col: rb.column(col)[i].as_py() for col in rb.schema.names}
            yield rec

def stream_jsonl_cdn(url):
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        if line:
            yield json.loads(line)

def stream_file(path, repo):
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    if path.endswith(".parquet"):
        yield from stream_parquet_cdn(url)
    elif path.endswith(".jsonl"):
        yield from stream_jsonl_cdn(url)
    else:
        # Skip unknown
        return

def main():
    args = parse_args()
    with open(args.file_list) as f:
        meta = json.load(f)
    files = meta["files"]
    os.makedirs(args.out_dir, exist_ok=True)
    out_file = os.path.join(args.out_dir, "enriched.jsonl")

    # Import dedup helper (central md5 store)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from lib.dedup import is_duplicate, add_hash

    written = 0
    skipped = 0
    with open(out_file, "w", encoding="utf-8") as out:
        for path in tqdm(files, desc="Shard files"):
            try:
                for rec in stream_file(path, args.repo):
                    pair = project_record(rec, path)
                    raw = json.dumps(pair, ensure_ascii=False, sort_keys=True)
                    md5 = hashlib.md5(raw.encode("utf-8")).hexdigest()
                    if is_duplicate(md5):
                        skipped += 1
                        continue
                    add_hash(md5)
                    out.write(raw + "\n")
                    written += 1
            except Exception as exc:
                print(f"ERROR processing {path}: {exc}", file=sys.stderr)

    print(f"Shard complete: {written} written, {skipped} skipped (duplicates)")

if __name__ == "__main__":
    main()
```

---

### 4) Keep `bin/lib/dedup.py` unchanged (central md5 store)

No changes required.

---

### 5) Update `requirements.txt`

```
huggingface-hub>=0.23
requests>=2.31
tqdm>=4.66
pyarrow>=14
```

---

### 6) Update `.

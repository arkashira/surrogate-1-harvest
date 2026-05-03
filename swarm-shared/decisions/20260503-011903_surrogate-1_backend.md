# surrogate-1 / backend

## Final synthesized plan (highest-value, <2h)

**Core fix**  
Replace recursive HF API ingestion and per-file authenticated fetches with:

1. One non-recursive `list_repo_tree(path=DATE, recursive=False)` per date folder.  
2. Deterministic shard assignment from the file list (hash-based).  
3. CDN-only downloads (`resolve/main/...`) with strict column projection (`prompt`, `response`).  
4. Lightweight dedup (in-process set + optional SQLite) and per-shard unique output files.

This removes 429 risk, eliminates per-file auth overhead, and keeps memory bounded.

---

## Concrete implementation plan (≤2h)

### 1) Update `bin/dataset-enrich.sh`
- Accept `DATE`, `SHARD_ID`, `SHARD_TOTAL`, `HF_REPO`, `HF_TOKEN` (already provided by matrix).  
- Run `list_repo_tree` once per runner (or pre-compute on Mac/CI and embed `file-list.json`).  
- Deterministic shard assignment by filename hash so workers never overlap.  
- Process only assigned files; stream with column projection; write `shard<SHARD_ID>-<HHMMSS>.jsonl`.  
- Upload each shard to `batches/public-merged/<DATE>/` with unique filename.

### 2) Worker logic (inline Python)
- Download via CDN:  
  `https://huggingface.co/datasets/<HF_REPO>/resolve/main/<path>` (no auth header).  
- Use `pyarrow.parquet.ParquetFile` with column projection to avoid mixed-schema `CastError`.  
- Normalize to `{prompt: str, response: str}`; skip empty rows.  
- Dedup by `md5(json.dumps(row, sort_keys=True))` (in-process set; optional SQLite for cross-runner dedup).  
- Write newline-delimited JSON.

### 3) Commit/upload strategy
- Each runner writes to a unique filename including `shard<SHARD_ID>-<TS>.jsonl`.  
- Keep only `{prompt, response}`; move attribution to filename/path (no extra columns).

### 4) Lightning Studio reuse (if used for downstream training)
- Before `.run()`, list `Teamspace.studios` and reuse a running studio to save quota.  
- If stopped, restart with `target.start(machine=Machine.L40S)` (prefer L40S on free tier; H200 only on paid clouds).

### 5) Validation (dry-run)
- Run on a single date folder and confirm:  
  - No recursive `list_repo_files`.  
  - No 429 during ingestion.  
  - Output schema is exactly `{prompt, response}`.

---

## Final code snippets

### `bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

: "${DATE:?Need DATE}"
: "${SHARD_ID:?Need SHARD_ID}"
: "${SHARD_TOTAL:?Need SHARD_TOTAL}"
: "${HF_REPO:?Need HF_REPO, e.g. axentx/surrogate-1-training-pairs}"
: "${HF_TOKEN:?Need HF_TOKEN}"

WORKDIR=$(mktemp -d)
cd "$WORKDIR"

# 1) Non-recursive tree listing for the date folder
python3 - <<PY
import os, json
from huggingface_hub import HfApi
api = HfApi(token=os.environ["HF_TOKEN"])
tree = api.list_repo_tree(
    repo_id=os.environ["HF_REPO"],
    path=f"public-merged/${DATE}",
    recursive=False
)
files = [f.rfilename for f in tree if f.rfilename.endswith(".parquet")]
with open("file-list.json", "w") as f:
    json.dump(files, f)
PY

# 2) Deterministic shard assignment by filename hash
mapfile -t ALL_FILES < <(jq -r '.[]' file-list.json)
if (( ${#ALL_FILES[@]} == 0 )); then
  echo "No parquet files for ${DATE}"
  exit 0
fi

declare -a MY_FILES
for f in "${ALL_FILES[@]}"; do
  h=$(echo -n "$f" | cksum | awk '{print $1}')
  b=$(( h % SHARD_TOTAL ))
  if (( b == SHARD_ID )); then
    MY_FILES+=("$f")
  fi
done

echo "Shard ${SHARD_ID}/${SHARD_TOTAL} processing ${#MY_FILES[@]}/${#ALL_FILES[@]} files"

# 3) Process via CDN + column projection
TS=$(date -u +%H%M%S)
OUT="shard${SHARD_ID}-${TS}.jsonl"

python3 - <<PY
import os, json, hashlib, sys
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

HF_REPO = os.environ["HF_REPO"]
OUT = sys.argv[1]
FILES = sys.argv[2:]

def download_parquet(path):
    url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content

def normalize_record(rec):
    prompt = str(rec.get("prompt", rec.get("input", "")))
    response = str(rec.get("response", rec.get("output", "")))
    return {"prompt": prompt, "response": response}

def hash_row(row):
    return hashlib.md5(json.dumps(row, sort_keys=True).encode()).hexdigest()

seen = set()
with open(OUT, "w", encoding="utf-8") as out_f:
    for rel in tqdm(FILES, desc="Files"):
        try:
            data = download_parquet(rel)
        except Exception as e:
            print(f"Failed {rel}: {e}", file=sys.stderr)
            continue
        try:
            pf = pq.ParquetFile(data)
            available = pf.schema.names
            proj = [c for c in ["prompt", "response"] if c in available]
            if not proj:
                # fallback: try common alternative names once
                alt = [c for c in ["input", "output", "text", "completion"] if c in available]
                if not alt:
                    continue
                proj = alt
            table = pf.read(columns=proj)
            df = table.to_pandas()
        except Exception as e:
            print(f"Parquet error {rel}: {e}", file=sys.stderr)
            continue

        for _, row in df.iterrows():
            d = row.to_dict()
            # map fallback names to canonical fields for normalize_record
            if "prompt" not in d and "input" in d:
                d["prompt"] = d["input"]
            if "response" not in d and "output" in d:
                d["response"] = d["output"]
            rec = normalize_record(d)
            if not rec["prompt"] or not rec["response"]:
                continue
            h = hash_row(rec)
            if h in seen:
                continue
            seen.add(h)
            out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY

"$OUT" "${MY_FILES[@]}"

# 4) Upload shard output
DEST="batches/public-merged/${DATE}/shard${SHARD_ID}-${TS}.jsonl"
python3 - <<PY
import sys
from huggingface_hub import HfApi
api = HfApi(token=os.environ["HF_TOKEN"])
api.upload_file(
    path_or_fileobj=sys.argv[1],
    path_in_repo=sys.argv[2],
    repo_id=os.environ["HF_REPO"]
)
PY

echo "Uploaded ${DEST}"
```

### `lib/dedup.py` (optional cross-runner dedup)
```python
import sqlite3
import hashlib
import json
from pathlib import Path

DB_PATH = Path("dedup_hashes.db")

def init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS hashes (md5 TEXT PRIMARY KEY)")

def add(md5: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        try:
            conn.execute("INSERT INTO hashes (md5) VALUES (?)", (md5,))
            return True
        except sqlite3.IntegrityError:
            return False

def hash_row(row):
    return hashlib.md5(json.dumps(row, sort_keys=True).encode

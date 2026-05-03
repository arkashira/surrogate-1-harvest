# surrogate-1 / quality

### Final Implementation Plan (≤2h)

**Goal**: Eliminate HF API rate-limit risk and OOM in the surrogate-1 ingestion pipeline by replacing recursive authenticated fetches with deterministic shard routing + CDN-only fetches.

---

#### 1) `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# Usage: dataset-enrich.sh <date> <shard_id> <total_shards>
# Example: dataset-enrich.sh 2026-05-03 0 16

DATE="${1:-$(date +%Y-%m-%d)}"
SHARD_ID="${2:-${SHARD_ID:-0}}"
TOTAL_SHARDS="${3:-${TOTAL_SHARDS:-16}}"
HF_REPO="${HF_REPO:-datasets/axentx/surrogate-1-training-pairs}"
HF_TOKEN="${HF_TOKEN:?HF_TOKEN required}"
OUT_DIR="batches/public-merged/${DATE}"
TIMESTAMP=$(date +%H%M%S)
OUTPUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

mkdir -p "$(dirname "${OUTPUT_FILE}")"

echo "[$(date)] Shard ${SHARD_ID}/${TOTAL_SHARDS} | Date ${DATE}"
echo "Listing ${HF_REPO} tree for ${DATE}..."

# Single non-recursive tree call per date folder (avoids recursive pagination)
TREE_JSON=$(python3 -c "
import json, os, sys
from huggingface_hub import HfApi
api = HfApi(token=os.environ['HF_TOKEN'])
items = api.list_repo_tree(
    repo_id=os.environ['HF_REPO'],
    path='${DATE}',
    repo_type='dataset',
    recursive=False
)
# Keep only files (ignore subfolders)
files = [i for i in items if i.type == 'file']
print(json.dumps([f.rfilename for f in files]))
" 2>/dev/null || python3 -c "
# Fallback: if HF SDK not available, use raw API (still one call)
import json, os, sys, urllib.request
token = os.environ['HF_TOKEN']
url = f'https://huggingface.co/api/datasets/{os.environ[\"HF_REPO\"]}/tree?path=${DATE}&recursive=false'
req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
with urllib.request.urlopen(req) as resp:
    items = json.load(resp)
files = [i['path'] for i in items if i['type'] == 'file' and i['path'].startswith('${DATE}/')]
print(json.dumps(files))
")

# Deterministic shard assignment by slug hash
FILTERED=$(python3 -c "
import json, hashlib, sys
files = json.loads(sys.stdin.read())
shard_id = int(${SHARD_ID})
total = int(${TOTAL_SHARDS})
selected = []
for f in files:
    # Use filename as slug; deterministic modulo shard
    slug = f.split('/')[-1]
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    if (h % total) == shard_id:
        selected.append(f)
print(json.dumps(selected))
" <<< "${TREE_JSON}")

echo "Selected $(echo "${FILTERED}" | python3 -c "import sys,json;print(len(json.load(sys.stdin)))") files for shard ${SHARD_ID}"

# Emit CDN-only URL list for downstream loader (zero API calls during fetch)
python3 -c "
import json, sys, os
files = json.loads(sys.stdin.read())
repo = os.environ['HF_REPO']
urls = [f'https://huggingface.co/datasets/{repo}/resolve/main/{f}' for f in files]
with open('${OUT_DIR}/shard${SHARD_ID}-urls.json', 'w') as f:
    json.dump(urls, f)
" <<< "${FILTERED}"

# Stream selected files via CDN and normalize to {prompt,response}
python3 bin/process_shard.py \
  --urls-file "${OUT_DIR}/shard${SHARD_ID}-urls.json" \
  --output "${OUTPUT_FILE}" \
  --dedup-db "lib/dedup.sqlite"

echo "Writing ${OUTPUT_FILE}"
echo "Uploading to HF dataset..."

git config user.name "github-actions"
git config user.email "actions@github.com"
git add "${OUTPUT_FILE}" "${OUT_DIR}/shard${SHARD_ID}-urls.json" || true
git commit -m "shard${SHARD_ID} ${DATE} ${TIMESTAMP}" || true
git push origin HEAD

echo "[$(date)] Shard ${SHARD_ID} done"
```

---

#### 2) `bin/process_shard.py`

```python
#!/usr/bin/env python3
"""
CDN-only shard processor.
- Reads list of CDN URLs (public dataset files).
- Downloads each file individually via CDN (no auth, no API rate-limit).
- Projects to {prompt,response} and normalizes per known schema.
- Dedups via central SQLite store.
- Outputs newline JSONL.
"""
import argparse
import json
import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

CDN_BASE = "https://huggingface.co/datasets"

def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def normalize_record(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Convert varied schema keys to {prompt, response}.
    Supports common variants:
      - prompt/response
      - instruction/input/output
      - question/answer
    """
    rk = {k.lower(): v for k, v in raw.items()}
    prompt = rk.get("prompt") or rk.get("instruction") or rk.get("question") or ""
    response = rk.get("response") or rk.get("output") or rk.get("answer") or ""
    # If nested, try to flatten simple cases
    if isinstance(prompt, (list, dict)):
        prompt = json.dumps(prompt, ensure_ascii=False)
    if isinstance(response, (list, dict)):
        response = json.dumps(response, ensure_ascii=False)
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def dedup_key(record: Dict[str, str]) -> str:
    return sha256_hex((record["prompt"] + "\x00" + record["response"]).encode())

def init_dedup(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY)")
    conn.commit()
    return conn

def is_seen(conn: sqlite3.Connection, key: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen WHERE key = ?", (key,))
    return cur.fetchone() is not None

def mark_seen(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("INSERT OR IGNORE INTO seen (key) VALUES (?)", (key,))

def download_cdn(url: str, timeout: int = 30) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def process_parquet(content: bytes, dedup_conn: sqlite3.Connection) -> Iterable[Dict[str, Any]]:
    table = pq.read_table(pa.BufferReader(content))
    df = table.to_pandas()
    for _, row in df.iterrows():
        rec = normalize_record(row.to_dict())
        if not rec["prompt"] or not rec["response"]:
            continue
        key = dedup_key(rec)
        if is_seen(dedup_conn, key):
            continue
        mark_seen(dedup_conn, key)
        yield {"id": key, **rec}

def process_shard(urls_file: Path, output_file: Path, dedup_db: Path, batch_size: int = 500) -> None:
    with urls_file.open() as f:
        urls = json.load(f)

    dedup_conn = init_dedup(dedup_db)
    output_file.parent.mkdir(parents=True, exist_ok=True)

   

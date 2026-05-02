# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~120 lines total)

1. **`bin/list_files.py`** — single API call (post-rate-limit window) to `list_repo_tree` for a date folder; emit `file_list.json` with `path`, `size`, `sha256` (if available). Embeddable by training and ingest scripts.

2. **`bin/dataset-enrich.sh`** — accept optional `FILE_LIST_JSON`; if provided, skip recursive `list_repo_files` and stream listed files via CDN (`resolve/main/...`) with retries. Keep existing 16-shard deterministic routing via `slug-hash % 16`.

3. **`train.py`** (new lightweight stub) — read `file_list.json`, stream via CDN with `requests` + `pyarrow` projection to `{prompt, response}` only; zero HF API calls during data load. Compatible with Lightning Studio reuse pattern.

---

## 1/ `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in
axentx/surrogate-1-training-pairs.

Usage:
  python bin/list_files.py 2026-05-03 > file_list.json

Embeddable by train.py and dataset-enrich.sh to avoid recursive
HF API list_repo_files and reduce 429 risk.
"""
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

REPO_ID = "datasets/axentx/surrogate-1-training-pairs"

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: list_files.py <date-folder>  # e.g. 2026-05-03", file=sys.stderr)
        sys.exit(1)

    date_folder = sys.argv[1]
    api = HfApi(token=os.getenv("HF_TOKEN"))

    # Single non-recursive call per folder (avoids 100x pagination)
    entries = api.list_repo_tree(
        repo_id=REPO_ID,
        path=date_folder,
        repo_type="dataset",
        recursive=False,
    )

    files = []
    for e in entries:
        if getattr(e, "type", None) == "file":
            files.append(
                {
                    "path": f"{date_folder}/{e.path.split('/')[-1]}",
                    "size": getattr(e, "size", None),
                    "sha256": getattr(e, "sha256", None),
                }
            )

    out = {
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "repo": REPO_ID,
        "folder": date_folder,
        "count": len(files),
        "files": files,
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list_files.py
```

---

## 2/ `bin/dataset-enrich.sh` (patch)

Add CDN fallback and deterministic routing when `FILE_LIST_JSON` is provided.

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Deterministic 1/16 shard worker with CDN bypass support.
#
# Usage:
#   FILE_LIST_JSON=file_list.json ./dataset-enrich.sh
#
# Environment:
#   SHARD_ID          0..15
#   HF_TOKEN          write token for uploads
#   FILE_LIST_JSON    optional; if set, use CDN-only ingestion

set -euo pipefail
export SHELL=/bin/bash

REPO="datasets/axentx/surrogate-1-training-pairs"
DATE=$(date -u +%Y-%m-%d)
TS=$(date -u +%H%M%S)
OUT="batches/public-merged/${DATE}/shard${SHARD_ID}-${TS}.jsonl"

# Central dedup store (same as HF Space)
DEDUP_DB="/tmp/dedup_cache.db"
python3 -c "
import sqlite3, os, sys
db = os.environ['DEDUP_DB']
os.makedirs(os.path.dirname(db), exist_ok=True)
con = sqlite3.connect(db)
con.execute('CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)')
con.commit()
con.close()
"

function slug_hash_mod16() {
  # Deterministic shard routing
  echo -n "$1" | sha256sum | tr -d ' -' | python3 -c "import sys; print(int(sys.stdin.read().strip(),16)%16)"
}

function stream_via_cdn() {
  local path="$1"
  local url="https://huggingface.co/${REPO}/resolve/main/${path}"
  curl -fsSL --retry 3 --retry-delay 2 "$url"
}

function process_file() {
  local path="$1"
  # Project to {prompt,response} only; normalize schema per surrogate-1 rules.
  python3 - "$path" <<'PY'
import json, sys, hashlib, pyarrow as pa, pyarrow.parquet as pq, io, os, requests
from typing import Any

path = sys.argv[1]
url = f"https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{path}"

def extract_pair(obj: Any) -> dict:
    # Surrogate-1 schema normalization
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
    response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

try:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    buf = io.BytesIO(r.content)
    table = pq.read_table(buf, columns=["prompt", "response"] if {"prompt","response"}.issubset(pq.read_metadata(buf).schema.names) else None)
    if table.num_rows == 0:
        # fallback: try json/jsonl lines
        for line in r.text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            pair = extract_pair(obj)
            md5 = hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()
            shard = int(hashlib.sha256(pair["prompt"].encode()).hexdigest(), 16) % 16
            if shard == int(os.environ["SHARD_ID"]):
                yield md5, json.dumps(pair, ensure_ascii=False)
        return

    for batch in table.to_batches():
        for i in range(batch.num_rows):
            row = {k: batch[k][i].as_py() for k in batch.schema.names}
            pair = extract_pair(row)
            md5 = hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()
            shard = int(hashlib.sha256(pair["prompt"].encode()).hexdigest(), 16) % 16
            if shard == int(os.environ["SHARD_ID"]):
                yield md5, json.dumps(pair, ensure_ascii=False)
except Exception as e:
    # non-fatal per file
    sys.stderr.write(f"skip {path}: {e}\n")

for md5, payload in process_file(path):
    print(f"{md5}\t{payload}")
PY
}

function dedup_and_append() {
  local tmpf=$(mktemp)
  cat >"$tmpf"
  python3 - "$tmpf" "$OUT" "$DEDUP_DB" <<'PY'
import sqlite3, sys, os
tmpf, out, db = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(os.path.dirname(out), exist_ok=True)
con = sqlite3.connect(db)
added = 0
with open(tmpf) as f:
    for line in f:
        line = line.rstrip("\n")
        if "\t" not in line:
            continue
        md5, payload = line.split("\t", 1)
        cur = con.execute("SELECT 1 FROM seen WHERE md5=?", (md5,))
        if cur.fetchone() is None:


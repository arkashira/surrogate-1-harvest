# surrogate-1 / backend

**Final Implementation Plan (≤2 h)**

**Highest-value improvement**  
Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training, make shard workers fully independent, and raise the effective commit cap with deterministic sibling-repo sharding.

**What we’ll ship (single coherent change set)**
1. `bin/list-snapshot.sh` — run once (or in cron) to produce `snapshot/<date>/file-list.json` for a date folder using non-recursive `list_repo_tree` calls to avoid pagination/429s.
2. Update `bin/dataset-enrich.sh` to accept optional `FILE_LIST`; when provided, workers iterate the local list and download via CDN (`resolve/main/...`) with zero HF API calls during streaming. Keep safe fallback to current behavior when no list is provided.
3. Deterministic sibling-repo write sharding (`md5(slug) % 5`) to pick one of `axentx/surrogate-1-training-pairs{,-sibling1,...,-sibling4}` so aggregate commit cap is ~640/hr.
4. Small Python helper to reuse Lightning Studio sessions and restart if idle-killed.

---

### 1) Snapshot creator (Mac/Linux)

```bash
#!/usr/bin/env bash
# bin/list-snapshot.sh
# Usage: HF_TOKEN=... ./bin/list-snapshot.sh axentx surrogate-1-training-pairs 2026-05-02
# Produces: snapshot/<date>/file-list.json

set -euo pipefail

REPO_OWNER="${1:-axentx}"
REPO_NAME="${2:-surrogate-1-training-pairs}"
DATE="${3:-$(date +%Y-%m-%d)}"
OUTDIR="snapshot/${DATE}"
OUTFILE="${OUTDIR}/file-list.json"

mkdir -p "${OUTDIR}"

echo "Listing ${REPO_OWNER}/${REPO_NAME} for ${DATE}..."
python3 - <<PY
import os, json, itertools
from huggingface_hub import HfApi

api = HfApi(token=os.environ["HF_TOKEN"])
owner = "${REPO_OWNER}"
repo = "${REPO_NAME}"
date = "${DATE}"

def list_folder(path):
    items = api.list_repo_tree(repo_id=f"{owner}/{repo}", path=path, recursive=False)
    files = [it.rfilename for it in items if it.type == "file"]
    folders = [it.rfilename for it in items if it.type == "folder"]
    return files, folders

base_files, subfolders = list_folder(date)
all_files = [f"{date}/{f}" for f in base_files]
for sub in subfolders:
    sub_files, _ = list_folder(sub)
    all_files.extend([f"{sub}/{f}" for f in sub_files])

allowed_ext = {".parquet", ".jsonl", ".json", ".csv", ".tsv"}
filtered = [f for f in all_files if any(f.endswith(e) for e in allowed_ext)]
filtered.sort()

os.makedirs("${OUTDIR}", exist_ok=True)
with open("${OUTFILE}", "w") as fp:
    json.dump({"date": date, "repo": f"{owner}/{repo}", "files": filtered}, fp, indent=2)

print(f"Wrote {len(filtered)} files to ${OUTFILE}")
PY

echo "Snapshot saved to ${OUTFILE}"
```

Make executable:
```bash
chmod +x bin/list-snapshot.sh
```

---

### 2) Updated `dataset-enrich.sh` (CDN-first, schema-safe, shard-aware)

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated: prefer CDN downloads when FILE_LIST is provided.

set -euo pipefail
export SHELL=/bin/bash

HF_TOKEN="${HF_TOKEN:?required}"
REPO_ID="${REPO_ID:-axentx/surrogate-1-training-pairs}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
FILE_LIST="${FILE_LIST:-}"   # optional: path to snapshot file-list.json
OUTDIR="batches/public-merged/${DATE}"
TIMESTAMP=$(date +%H%M%S)
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

mkdir -p "$(dirname "${OUTFILE}")"

python3 - <<PY
import os, json, hashlib, sys, subprocess, time
from pathlib import Path
import pyarrow.parquet as pq
import pyarrow as pa
import numpy as np

HF_TOKEN = os.environ["HF_TOKEN"]
REPO_ID = os.environ["REPO_ID"]
DATE = os.environ["DATE"]
SHARD_ID = int(os.environ["SHARD_ID"])
TOTAL_SHARDS = int(os.environ["TOTAL_SHARDS"])
FILE_LIST = os.environ.get("FILE_LIST", "")
OUTFILE = os.environ["OUTFILE"]

# Deterministic sibling-repo write sharding
def sibling_repo_for_slug(slug, n_siblings=5, base=REPO_ID):
    idx = int(hashlib.md5(slug.encode()).hexdigest(), 16) % n_siblings
    if idx == 0:
        return base
    owner, name = base.split("/", 1)
    return f"{owner}/{name}-sibling{idx}"

# Dedup (local sqlite; replace with lib/dedup.py if preferred)
DEDUP_DB = "dedup/md5_store.sqlite"
Path(DEDUP_DB).parent.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(DEDUP_DB)
conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY, ts INTEGER)")
conn.commit()

def already_seen(md5):
    cur = conn.execute("SELECT 1 FROM seen WHERE md5=?", (md5,))
    return cur.fetchone() is not None

def mark_seen(md5):
    try:
        conn.execute("INSERT INTO seen (md5, ts) VALUES (?, ?)", (md5, int(time.time())))
        conn.commit()
    except sqlite3.IntegrityError:
        pass

# Build file list
if FILE_LIST and Path(FILE_LIST).exists():
    with open(FILE_LIST) as f:
        manifest = json.load(f)
    files = manifest.get("files", [])
    print(f"Using pre-flight list with {len(files)} files")
else:
    # Fallback: list top-level date folder (non-recursive) via HF API
    from huggingface_hub import HfApi
    api = HfApi(token=HF_TOKEN)
    items = api.list_repo_tree(repo_id=REPO_ID, path=DATE, recursive=False)
    files = [DATE + "/" + it.rfilename for it in items if it.type == "file"]
    files = [f for f in files if any(f.endswith(e) for e in (".parquet", ".jsonl", ".json", ".csv", ".tsv"))]
    files.sort()

# Assign deterministic shard slice
def deterministic_shard(key, n):
    return int(hashlib.md5(key.encode()).hexdigest(), 16) % n

my_files = [f for f in files if deterministic_shard(f, TOTAL_SHARDS) == SHARD_ID]
print(f"Shard {SHARD_ID}/{TOTAL_SHARDS} processing {len(my_files)} files")

def cdn_download_url(repo_id, path):
    # Public CDN — no auth header required, bypasses API rate limits
    return f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"

def safe_parquet_to_rows(path):
    url = cdn_download_url(REPO_ID, path)
    local = Path("/tmp") / Path(path).name
    subprocess.run(["curl", "-L", "-f", "-s", "-o", str(local), url], check=True)
    try:
        table = pq.read_table(local, columns=["prompt", "response"] if pq.read_metadata(local).num_columns >= 2 else None)
    except Exception as e:
        print(f"Failed to read parquet {path}: {e}", file=sys.stderr)
        return []
    finally:
        try:
            local.unlink()
        except Exception:
            pass
    return table.to_pylist()

def safe_jsonl_to_rows(path):
    url = cdn_download_url(REPO_ID, path)
    local = Path("/tmp") / Path(path).name
    subprocess.run(["curl", "-L", "-f", "-s", "-o", str(local), url], check=True)
    rows = []
    try:
        with open

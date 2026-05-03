# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing — eliminating HF API rate limits during ingestion.

### Why this matters
- Current workflow calls `list_repo_files`/`load_dataset` during every shard run → risks 429 rate limits on 16 parallel runners
- Public dataset files on CDN (`resolve/main/...`) bypass auth/rate limits entirely
- Single deterministic file-list per date folder lets each shard stream only its slice with zero API calls during data loading

### Concrete changes (3 files)

#### 1. `bin/snapshot.sh` — generate file manifest
```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: HF_TOKEN=... ./bin/snapshot.sh <date> [repo]
# Emits: snapshots/<date>/files.json
set -euo pipefail

DATE="${1:-$(date +%Y-%m-%d)}"
REPO="${2:-axentx/surrogate-1-training-pairs}"
OUTDIR="snapshots/${DATE}"
OUTFILE="${OUTDIR}/files.json"

mkdir -p "${OUTDIR}"

echo "Listing ${REPO} for ${DATE}..."
# Use tree API (non-recursive per folder) to avoid pagination on full repo
# Walk date folder only; CDN URLs are constructed from paths
python3 - <<PY
import os, json, itertools
from huggingface_hub import list_repo_tree

token = os.environ.get("HF_TOKEN")
repo = os.environ.get("REPO", "${REPO}")
date = os.environ.get("DATE", "${DATE}")

# List top-level of date folder
files = list_repo_tree(
    repo=repo,
    path=date,
    recursive=True,
    token=token,
)

# Keep only files (not dirs), exclude hidden
paths = [f.rfilename for f in files if f.type == "file" and not f.rfilename.startswith(".")]

# Build CDN URLs (no auth, bypasses /api/ rate limits)
base = f"https://huggingface.co/datasets/{repo}/resolve/main"
entries = [
    {"path": p, "url": f"{base}/{p}", "size": None}
    for p in sorted(paths)
]

os.makedirs("${OUTDIR}", exist_ok=True)
with open("${OUTFILE}", "w") as f:
    json.dump({"date": date, "repo": repo, "files": entries}, f, indent=2)

print(f"Wrote {len(entries)} files to ${OUTFILE}")
PY

echo "Snapshot saved: ${OUTFILE}"
```

#### 2. `bin/dataset-enrich.sh` — use snapshot + CDN streaming
```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated to use snapshot + CDN-only downloads
set -euo pipefail

SHARD_ID="${SHARD_ID:-0}"
N_SHARDS="${N_SHARDS:-16}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SNAPSHOT="snapshots/${DATE}/files.json"

if [[ ! -f "${SNAPSHOT}" ]]; then
  echo "Snapshot not found: ${SNAPSHOT}. Run bin/snapshot.sh first."
  exit 1
fi

python3 - <<PY
import os, json, hashlib, pyarrow.parquet as pq, pyarrow as pa, io, sys, requests
from datetime import datetime

SHARD_ID = int(os.environ.get("SHARD_ID", "0"))
N_SHARDS = int(os.environ.get("N_SHARDS", "16"))
DATE = os.environ.get("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.environ.get("HF_TOKEN", "")
REPO = "axentx/surrogate-1-training-pairs"
OUT_REPO = REPO  # same repo, different path

# Load snapshot (file list + CDN URLs)
with open("snapshots/${DATE}/files.json") as f:
    manifest = json.load(f)

all_files = manifest["files"]
# Deterministic shard assignment by path hash
def shard_for(path):
    return int(hashlib.md5(path.encode()).hexdigest(), 16) % N_SHARDS

my_files = [f for f in all_files if shard_for(f["path"]) == SHARD_ID]
print(f"Shard {SHARD_ID}/{N_SHARDS}: processing {len(my_files)} files")

# Central dedup store (shared via mounted volume or HF Space SQLite)
DEDUP_DB = os.environ.get("DEDUP_DB", "dedup.db")

def is_duplicate(md5_hex):
    import sqlite3
    conn = sqlite3.connect(DEDUP_DB)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY, ts TEXT)")
    cur.execute("SELECT 1 FROM seen WHERE md5=?", (md5_hex,))
    exists = cur.fetchone() is not None
    if not exists:
        cur.execute("INSERT INTO seen (md5, ts) VALUES (?, ?)", (md5_hex, datetime.utcnow().isoformat()))
        conn.commit()
    conn.close()
    return exists

def normalize_record(obj):
    # Project to {prompt, response} only at parse time
    prompt = obj.get("prompt") or obj.get("input") or obj.get("text") or ""
    response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
    if not prompt or not response:
        return None
    return {"prompt": str(prompt), "response": str(response)}

def stream_parquet_cdn(url):
    # CDN download — no Authorization header, bypasses /api/ rate limits
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    buf = io.BytesIO(resp.content)
    try:
        table = pq.read_table(buf)
        return table.to_pylist()
    except Exception as e:
        print(f"Failed to read {url}: {e}", file=sys.stderr)
        return []

records = []
for f in my_files:
    url = f["url"]
    rows = stream_parquet_cdn(url)
    for row in rows:
        rec = normalize_record(row)
        if not rec:
            continue
        # Deterministic content hash for dedup
        content = (rec["prompt"] + "\0" + rec["response"]).encode()
        md5 = hashlib.md5(content).hexdigest()
        if is_duplicate(md5):
            continue
        rec["_md5"] = md5
        rec["_source_file"] = f["path"]
        records.append(rec)

print(f"Shard {SHARD_ID}: {len(records)} unique records")

if records:
    table = pa.Table.from_pylist(records)
    out_name = f"shard{SHARD_ID}-{datetime.utcnow().strftime('%H%M%S')}.jsonl"
    out_path = f"batches/public-merged/{DATE}/{out_name}"
    # Write local then push via huggingface_hub (or use pyarrow to write jsonl)
    import tempfile, subprocess, pathlib
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        for rec in records:
            tmp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tmp_path = tmp.name

    # Upload using huggingface_hub (requires HF_TOKEN)
    from huggingface_hub import upload_file
    upload_file(
        path_or_fileobj=tmp_path,
        path_in_repo=out_path,
        repo_id=OUT_REPO,
        token=HF_TOKEN,
    )
    pathlib.Path(tmp_path).unlink()
    print(f"Uploaded: {out_path}")
else:
    print("No records to upload")
PY
```

#### 3. `.github/workflows/ingest.yml` — add snapshot step
```yaml
# .github/workflows/ingest.yml
name: Ingest (16-shard)

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:
    inputs:
      date:
        description: "Date (YYYY-MM-DD)"
        required: false
        default: ""

env:
  HF_TOKEN: ${{ secrets.HF_TOKEN }}

jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      date: ${{ steps.date.outputs.value }}
    steps:
      - uses: actions

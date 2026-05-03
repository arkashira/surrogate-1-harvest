# surrogate-1 / frontend

## Final Implementation (merged + corrected)

**Core fix**: Replace recursive/per-file authenticated HF API calls with **one non-recursive `list_repo_tree` per date folder + deterministic shard routing + CDN-only fetches**. This eliminates the main cause of 429s, removes auth pressure during bulk reads, and keeps ingestion fast and deterministic.

### 1) One-time orchestrator step (run once per date)

Run this locally or in your orchestrator before the matrix starts. It produces a portable file manifest so every runner can do **CDN-only** fetches (no auth counted against API limits).

```bash
#!/usr/bin/env bash
# scripts/build-manifest.sh
# Usage: HF_TOKEN=... ./scripts/build-manifest.sh <date>
# Output: manifests/public-merged/<date>/filelist.json

set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="manifests/public-merged/${DATE}"
mkdir -p "${OUT}"

python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi

api = HfApi(token=os.environ["HF_TOKEN"])
items = api.list_repo_tree(
    repo_id="${REPO}",
    path="public-merged/${DATE}",
    repo_type="dataset",
    recursive=False
)
files = sorted(it.rfilename for it in items if it.type == "file")
manifest = {"date": "${DATE}", "root": "public-merged/${DATE}", "files": files}
with open("${OUT}/filelist.json", "w") as f:
    json.dump(manifest, f)
print(f"Wrote {len(files)} files to ${OUT}/filelist.json")
PY
```

### 2) Deterministic shard routing

Use a stable hash on the logical record key (filename without extension) so the same file always maps to the same shard across runs.

```python
import hashlib, os

def shard_for_file(path: str, total_shards: int) -> int:
    slug = os.path.splitext(os.path.basename(path))[0]
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % total_shards
```

### 3) Runner script (`bin/dataset-enrich.sh`)

Each matrix job runs this with `SHARD_ID`/`TOTAL_SHARDS`. It reads the manifest, selects its shard, fetches via CDN, projects to `{prompt,response}`, dedups, and writes output.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Usage: ./bin/dataset-enrich.sh <date> <shard_id> <total_shards>
#   date: YYYY-MM-DD folder under public-merged/
#   shard_id: 0..(total_shards-1)

set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
SHARD_ID="${2:-0}"
TOTAL_SHARDS="${3:-16}"
OUT_DIR="output/${DATE}"
MANIFEST="manifests/public-merged/${DATE}/filelist.json"
mkdir -p "${OUT_DIR}"

if [ ! -s "${MANIFEST}" ]; then
  echo "ERROR: manifest missing: ${MANIFEST}" >&2
  exit 1
fi

python3 - <<PY
import json, hashlib, os, sys, time, sqlite3, requests, io
from datetime import datetime

REPO = "${REPO}"
DATE = "${DATE}"
SHARD_ID = int("${SHARD_ID}")
TOTAL_SHARDS = int("${TOTAL_SHARDS}")
OUT_DIR = "${OUT_DIR}"
MANIFEST = "${MANIFEST}"

with open(MANIFEST) as f:
    manifest = json.load(f)

files = manifest["files"]

def shard_for_file(path: str) -> int:
    slug = os.path.splitext(os.path.basename(path))[0]
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % TOTAL_SHARDS

my_files = [f for f in files if shard_for_file(f) == SHARD_ID]
print(f"Shard {SHARD_ID}/{TOTAL_SHARDS-1}: processing {len(my_files)} files")

# Local dedup DB (per-date). Stores content hashes.
DB_PATH = os.path.join(OUT_DIR, "dedup.db")
conn = sqlite3.connect(DB_PATH)
conn.execute("CREATE TABLE IF NOT EXISTS seen (hash TEXT PRIMARY KEY)")
conn.commit()

def is_seen(h: str) -> bool:
    return conn.execute("SELECT 1 FROM seen WHERE hash=?", (h,)).fetchone() is not None

def mark_seen(h: str) -> None:
    conn.execute("INSERT OR IGNORE INTO seen (hash) VALUES (?)", (h,))
    conn.commit()

# CDN fetch (no auth counted against HF API rate limits)
def cdn_url(path: str) -> str:
    return f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"

def fetch_with_retry(url: str, max_retries: int = 5, backoff_base: int = 2) -> bytes:
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 429:
                wait = 360
                print(f"429 rate-limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(backoff_base ** attempt)
    raise RuntimeError("unreachable")

# Projection helpers
def _get_field(obj, keys):
    for k in keys:
        v = obj.get(k)
        if v is not None:
            return v
    return None

def parse_and_project(content: bytes, filename: str):
    name = filename.lower()
    rows = []

    if name.endswith(".jsonl"):
        for line in io.BytesIO(content).read().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            prompt = _get_field(obj, ("prompt", "input", "text", "question"))
            response = _get_field(obj, ("response", "output", "completion", "answer"))
            if prompt is not None and response is not None:
                rows.append({"prompt": str(prompt), "response": str(response)})
    elif name.endswith(".parquet"):
        try:
            import pyarrow.parquet as pq
            table = pq.read_table(io.BytesIO(content))
            df = table.to_pandas()
            for _, r in df.iterrows():
                prompt = _get_field(r.to_dict(), ("prompt", "input", "text", "question"))
                response = _get_field(r.to_dict(), ("response", "output", "completion", "answer"))
                if prompt is not None and response is not None:
                    rows.append({"prompt": str(prompt), "response": str(response)})
        except Exception:
            pass
    else:
        # Try generic JSON lines as fallback
        try:
            data = json.loads(content)
            if isinstance(data, list):
                for obj in data:
                    if isinstance(obj, dict):
                        prompt = _get_field(obj, ("prompt", "input", "text", "question"))
                        response = _get_field(obj, ("response", "output", "completion", "answer"))
                        if prompt is not None and response is not None:
                            rows.append({"prompt": str(prompt), "response": str(response)})
        except Exception:
            pass

    return rows

# Process assigned files
ts = datetime.utcnow().strftime("%H%M%S")
out_file = os.path.join(OUT_DIR, f"shard{SHARD_ID}-{ts}.jsonl")

total_rows = 0
for relpath in my_files:
    url = cdn_url(relpath)
    try:
        raw = fetch_with_retry(url)
    except Exception as e:
        print(f"Failed to fetch {relpath}: {e}", file=sys.stderr)
        continue

    # Content-level dedup
    content_hash = hashlib.md5(raw).hexdigest()
    if is_seen(content_hash):
        continue
    mark_seen(content_hash)

    rows = parse_and_project(raw, relpath)


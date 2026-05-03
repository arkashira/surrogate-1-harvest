# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing — eliminating HF API rate limits during ingestion.

### Why this matters
- Prevents 429 rate limits when 16 shards concurrently list/resolve files
- Enables CDN bypass (no auth, higher limits) during actual data loading
- Single `list_repo_tree` call from Mac orchestrator (after rate-limit window) → embeds manifest into shard jobs
- Keeps existing 16-shard parallelism and dedup behavior unchanged

---

### Changes (3 files, ~100 lines total)

#### 1. `bin/snapshot.sh` — new
Generates `snapshot.json` containing date-filtered file list for CDN-only ingestion.

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: HF_TOKEN=... ./bin/snapshot.sh <date> [repo]
#   date: YYYY-MM-DD folder to snapshot (default: today)
#   repo: HF dataset repo (default: axentx/surrogate-1-training-pairs)
# Output: snapshots/<date>/snapshot.json

set -euo pipefail

HF_TOKEN="${HF_TOKEN:-}"
DATE="${1:-$(date +%Y-%m-%d)}"
REPO="${2:-axentx/surrogate-1-training-pairs}"
OUTDIR="snapshots/${DATE}"
OUTFILE="${OUTDIR}/snapshot.json"

if [ -z "${HF_TOKEN}" ]; then
  echo "ERROR: HF_TOKEN is required" >&2
  exit 1
fi

mkdir -p "${OUTDIR}"

# Single API call: list top-level for the date folder (non-recursive)
# Then list each subfolder once (avoids recursive 100x pagination)
python3 - "${HF_TOKEN}" "${REPO}" "${DATE}" "${OUTFILE}" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

token, repo, date, outfile = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
api = HfApi(token=token)

# List date folder (non-recursive)
items = api.list_repo_tree(repo=repo, path=date, recursive=False)

files = []
for item in items:
    if item.type == "file":
        files.append(item.path)
    elif item.type == "directory":
        # One call per subfolder (bounded)
        sub_items = api.list_repo_tree(repo=repo, path=item.path, recursive=False)
        for si in sub_items:
            if si.type == "file":
                files.append(si.path)

# Keep only files likely to contain training pairs
candidates = [f for f in files if f.endswith((".jsonl", ".parquet", ".json"))]

snapshot = {
    "repo": repo,
    "date": date,
    "files": sorted(candidates),
    "cdn_base": f"https://huggingface.co/datasets/{repo}/resolve/main",
    "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z"
}

with open(outfile, "w") as f:
    json.dump(snapshot, f, indent=2)

print(f"Snapshot written: {outfile} ({len(candidates)} files)")
PY

echo "Snapshot complete: ${OUTFILE}"
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

#### 2. `bin/dataset-enrich.sh` — modified
Accept snapshot file path; use CDN URLs when available; fall back to HF API if no snapshot.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Existing behavior preserved; added CDN-first mode via snapshot.

set -euo pipefail

HF_TOKEN="${HF_TOKEN:-}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"  # optional: path to snapshot.json
REPO="${REPO:-axentx/surrogate-1-training-pairs}"
DATE="${DATE:-$(date +%Y-%m-%d)}"

# Dedup store (existing)
DEDUP_STORE="${DEDUP_STORE:-/tmp/dedup.db}"

export HF_TOKEN

# If snapshot provided, use CDN-only file list
if [ -n "${SNAPSHOT_FILE}" ] && [ -f "${SNAPSHOT_FILE}" ]; then
  echo "Using snapshot: ${SNAPSHOT_FILE}"
  FILES=$(python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
for f in data['files']:
    print(f)
" "${SNAPSHOT_FILE}")
  CDN_BASE=$(python3 -c "import json; print(json.load(open('${SNAPSHOT_FILE}'))['cdn_base'])")
else
  echo "No snapshot; falling back to HF API listing (may hit rate limits)"
  FILES=$(python3 -c "
from huggingface_hub import HfApi
api = HfApi()
items = api.list_repo_tree(repo='${REPO}', path='${DATE}', recursive=True)
for i in items:
    if i.type == 'file' and i.path.endswith(('.jsonl','.parquet','.json')):
        print(i.path)
")
  CDN_BASE=""
fi

# Deterministic shard assignment by slug hash
process_file() {
  local file="$1"
  local slug=$(basename "$file" | sed 's/\.[^.]*$//')
  local hash=$(echo -n "$slug" | md5sum | cut -c1-8)
  local bucket=$(( 0x${hash} % TOTAL_SHARDS ))
  if [ "$bucket" -ne "$SHARD_ID" ]; then
    return 0
  fi

  echo "Shard ${SHARD_ID}: processing ${file}"

  python3 - "${file}" "${CDN_BASE}" <<'PY'
import os, sys, hashlib, json, pyarrow.parquet as pq, pyarrow as pa
from pathlib import Path

file_path = sys.argv[1]
cdn_base = sys.argv[2] if len(sys.argv) > 2 else ""

# Try CDN download first if cdn_base provided
if cdn_base:
    import urllib.request, tempfile
    url = f"{cdn_base}/{file_path}"
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file_path).suffix) as tmp:
            urllib.request.urlretrieve(url, tmp.name)
            local_path = tmp.name
        use_cdn = True
    except Exception as e:
        print(f"CDN fetch failed for {url}: {e}; falling back to datasets")
        use_cdn = False
        local_path = None
else:
    use_cdn = False

def extract_pairs(path, use_cdn):
    pairs = []
    try:
        if path.endswith(".parquet"):
            tbl = pq.read_table(path)
            df = tbl.to_pandas()
            # Normalize to {prompt, response}
            for _, row in df.iterrows():
                prompt = row.get("prompt") or row.get("input") or row.get("text") or ""
                response = row.get("response") or row.get("output") or row.get("completion") or ""
                if prompt and response:
                    pairs.append({"prompt": str(prompt), "response": str(response)})
        elif path.endswith(".jsonl"):
            with open(path) as f:
                for line in f:
                    obj = json.loads(line)
                    prompt = obj.get("prompt") or obj.get("input") or obj.get("text") or ""
                    response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
                    if prompt and response:
                        pairs.append({"prompt": str(prompt), "response": str(response)})
        elif path.endswith(".json"):
            with open(path) as f:
                data = json.load(f)
                if isinstance(data, list):
                    for obj in data:
                        prompt = obj.get("prompt") or obj.get("input") or obj.get("text") or ""
                        response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
                        if prompt and response:
                            pairs.append({"prompt": str(prompt), "response": str(response)})
    except Exception as e:
        print(f"Parse error {path}: {e}")
    return pairs

if use_cdn:
    pairs = extract_pairs(local_path,

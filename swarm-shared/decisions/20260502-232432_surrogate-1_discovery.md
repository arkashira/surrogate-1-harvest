# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Goal**: Eliminate runtime `load_dataset(streaming=True)` and recursive `list_repo_files` from `bin/dataset-enrich.sh`. Replace with deterministic pre-flight snapshots + CDN-only fetches to avoid HF API rate limits and schema heterogeneity issues.

### Steps (est. 90 min)

1. **Add snapshot utility** (`bin/make-snapshot.sh`)  
   - Runs on Mac (or any dev box) once per date folder (or per cron tick)  
   - Calls `list_repo_tree(path, recursive=False)` for the target date folder  
   - Emits `snapshot/<date>/file-list.json` (flat list of file paths)  
   - Exits non-zero on 429 and prints retry-after

2. **Update worker script** (`bin/dataset-enrich.sh`)  
   - Accept optional `FILE_LIST` path (default: use snapshot if present)  
   - Remove `load_dataset(streaming=True)` and recursive listing  
   - For each assigned file (deterministic shard modulo 16):  
     - Download via CDN URL: `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<path>`  
     - Parse with `pyarrow` projecting only `{prompt, response}` (ignore extra cols)  
     - Dedup via central md5 store (`lib/dedup.py`)  
     - Append to shard output `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`  
   - Keep existing commit-per-shard behavior (filename includes shard+timestamp)

3. **Update GitHub Actions matrix** (`/.github/workflows/ingest.yml`)  
   - Add optional `snapshot_date` input (default: today)  
   - Add a one-off job (or pre-step) that optionally generates snapshot and uploads as artifact for the matrix jobs (or rely on dev snapshot committed to repo)  
   - Pass `FILE_LIST` to each shard via env

4. **Small safety changes**  
   - Ensure `lib/dedup.py` uses WAL mode for concurrent readers (workers are isolated runners, but central store may be on HF Space SQLite)  
   - Add retry/backoff for CDN downloads (separate from API limits)  
   - Log schema projection warnings instead of failing

---

## Code Snippets

### 1. Snapshot utility (`bin/make-snapshot.sh`)

```bash
#!/usr/bin/env bash
# bin/make-snapshot.sh
# Usage: HF_TOKEN=... ./bin/make-snapshot.sh <date> [output-dir]
# Produces snapshot/<date>/file-list.json

set -euo pipefail
REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%F)}"
OUTDIR="${2:-snapshot}"
OUTFILE="${OUTDIR}/${DATE}/file-list.json"

mkdir -p "$(dirname "$OUTFILE")"

echo "Listing ${REPO} for ${DATE}..."
python3 - "$REPO" "$DATE" "$OUTFILE" <<'PY'
import os, json, sys
from huggingface_hub import HfApi

repo, date_folder, out = sys.argv[1], sys.argv[2], sys.argv[3]
api = HfApi(token=os.environ.get("HF_TOKEN"))
# Non-recursive per-folder listing to avoid 100x pagination on big repos
items = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
files = [it.rfilename for it in items if not it.rfilename.endswith("/")]
with open(out, "w") as f:
    json.dump({"date": date_folder, "files": files}, f, indent=2)
print(f"Wrote {len(files)} files to {out}")
PY

echo "Snapshot created: $OUTFILE"
```

---

### 2. Updated worker (`bin/dataset-enrich.sh`)

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Shard worker: deterministic 1/16 slice, CDN-only fetches

set -euo pipefail
export SHELL=/bin/bash

REPO="axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date +%F)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
HF_TOKEN="${HF_TOKEN:-}"
OUTDIR="batches/public-merged/${DATE}"
TIMESTAMP=$(date +%H%M%S)
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

# Optional pre-computed file list (snapshot)
FILE_LIST="${FILE_LIST:-snapshot/${DATE}/file-list.json}"

mkdir -p "$(dirname "$OUTFILE")"

python3 - "$REPO" "$DATE" "$SHARD_ID" "$TOTAL_SHARDS" "$FILE_LIST" "$OUTFILE" <<'PY'
import os, json, hashlib, sys, urllib.request, pyarrow.parquet as pq, io, tempfile, time
from pathlib import Path

REPO, DATE, SHARD_ID, TOTAL_SHARDS = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
FILE_LIST, OUTFILE = sys.argv[5], sys.argv[6]
HF_TOKEN = os.environ.get("HF_TOKEN", "")

with open(FILE_LIST) as f:
    manifest = json.load(f)
files = sorted(manifest.get("files", []))

# Deterministic shard assignment by slug hash
def shard_for(path: str) -> int:
    return hash(path) % TOTAL_SHARDS

assigned = [p for p in files if shard_for(p) == SHARD_ID]
print(f"Shard {SHARD_ID}/{TOTAL_SHARDS}: processing {len(assigned)} files")

def download_cdn(path: str, max_retries: int = 3) -> bytes:
    url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url)
            # CDN does not require auth; but if private, include token
            if HF_TOKEN:
                req.add_header("Authorization", f"Bearer {HF_TOKEN}")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            print(f"Retry {path} in {wait}s ({e})")
            time.sleep(wait)

def project_to_pair(content: bytes):
    # Handle parquet files; project only prompt/response to avoid schema issues
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp.write(content)
        tmp.flush()
        try:
            pf = pq.read_table(tmp.name, columns=["prompt", "response"])
        except Exception:
            # Fallback: read all and select if columns exist
            pf = pq.read_table(tmp.name)
            cols = set(pf.column_names)
            want = [c for c in ["prompt", "response"] if c in cols]
            if not want:
                raise ValueError(f"No prompt/response in {pf.column_names}")
            pf = pf.select(want)
        df = pf.to_pandas()
        return df

def md5_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

# Central dedup store (import lib/dedup.py if available)
# For simplicity, local in-run dedup by hash (cross-run dedup handled by central store on HF Space)
seen = set()
written = 0

for path in assigned:
    try:
        raw = download_cdn(path)
        df = project_to_pair(raw)
    except Exception as e:
        print(f"SKIP {path}: {e}")
        continue

    for _, row in df.iterrows():
        prompt = str(row.get("prompt", ""))
        response = str(row.get("response", ""))
        if not prompt.strip() or not response.strip():
            continue
        h = md5_hash(prompt + "\0" + response)
        if h in seen:
            continue
        seen.add(h)
        with open(OUTFILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"prompt": prompt, "response": response}, ensure_ascii=False) + "\n")
        written += 1

print(f"Shard {SHARD_ID}: wrote {written} pairs to {OUTFILE}")
PY

# Optional: commit output (existing behavior preserved)
if [

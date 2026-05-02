# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Goal**: Eliminate runtime `load_dataset(streaming=True)` and recursive `list_repo_files` from `bin/dataset-enrich.sh`. Replace with deterministic pre-flight snapshot + CDN-only fetches to avoid HF API rate limits and schema heterogeneity issues.

### Steps (est. 90 min)

1. **Add snapshot utility** (`bin/make-snapshot.py`)  
   - Run once on Mac (or cron before the 16-shard matrix)  
   - Calls `list_repo_tree(path, recursive=False)` per date folder (non-recursive)  
   - Emits `snapshot-{date}.json` with `{file_path, size, sha, url}` for every parquet/jsonl  
   - Stores in repo under `snapshots/` (committed or artifact) so shards consume zero API calls during ingest

2. **Update `bin/dataset-enrich.sh`**  
   - Accept snapshot path as arg: `dataset-enrich.sh <snapshot.json> <shard_id> <nshards>`  
   - Remove `load_dataset` and `list_repo_files` calls  
   - Filter files by `shard_id = hash(slug) % nshards` using deterministic hash  
   - Download via CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) with `curl --retry 3 --retry-delay 5 -L -o`  
   - Stream-parse each file and project to `{prompt, response}` only; drop other columns  
   - Keep existing dedup via `lib/dedup.py` and output `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`

3. **Add lightweight Python fetcher** (`bin/fetch_cdn.py`)  
   - Reads snapshot, filters by shard, yields local temp paths  
   - Uses `requests` with streaming to avoid large memory peaks  
   - Emits line-delimited JSON records for shell pipeline

4. **Update GitHub Actions matrix**  
   - Add step before matrix to generate/restore snapshot artifact (or commit snapshots)  
   - Pass snapshot path to each shard via `with:` env  
   - Keep 16-shard parallelism unchanged

5. **Validation & rollback**  
   - Dry-run snapshot against a single shard locally  
   - Verify output row counts and schema match previous run  
   - Keep old codepath behind flag for one release (e.g., `USE_LEGACY_LOAD=0`)

---

## Code Snippets

### bin/make-snapshot.py
```python
#!/usr/bin/env python3
"""
Generate deterministic snapshot for a date folder.
Usage:
  python make-snapshot.py axentx/surrogate-1-training-pairs 2026-05-01 > snapshots/2026-05-01.json
"""
import json, hashlib, sys, os
from huggingface_hub import HfApi

API = HfApi()

def snapshot(repo_id: str, date_folder: str):
    # non-recursive per folder to avoid 100x pagination and 429
    tree = API.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
    files = []
    for f in tree:
        if f.path.endswith((".parquet", ".jsonl")):
            files.append({
                "path": f.path,
                "size": f.size,
                "sha": f.lfs.get("sha256", None) if f.lfs else None,
                # CDN URL — no Authorization header required
                "cdn": f"https://huggingface.co/datasets/{repo_id}/resolve/main/{f.path}"
            })
    # deterministic ordering
    files.sort(key=lambda x: x["path"])
    out = {
        "repo_id": repo_id,
        "date": date_folder,
        "generated_by": "make-snapshot.py",
        "files": files
    }
    return out

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: make-snapshot.py <repo_id> <date_folder>", file=sys.stderr)
        sys.exit(1)
    repo_id, date_folder = sys.argv[1], sys.argv[2]
    print(json.dumps(snapshot(repo_id, date_folder), indent=2))
```

### bin/fetch_cdn.py
```python
#!/usr/bin/env python3
"""
Yield records from snapshot for a single shard.
Usage:
  python fetch_cdn.py snapshot.json <shard_id> <nshards>
"""
import json, sys, hashlib, requests, tempfile, os

def shard_key(path: str, n: int) -> int:
    # deterministic across runs and platforms
    return int(hashlib.md5(path.encode()).hexdigest(), 16) % n

def stream_cdn(url: str):
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        # yield raw bytes; caller decodes per extension
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

def main():
    if len(sys.argv) != 4:
        print("Usage: fetch_cdn.py <snapshot.json> <shard_id> <nshards>", file=sys.stderr)
        sys.exit(1)
    snapshot_path = sys.argv[1]
    shard_id = int(sys.argv[2])
    nshards = int(sys.argv[3])

    with open(snapshot_path) as f:
        snap = json.load(f)

    for fmeta in snap["files"]:
        if shard_key(fmeta["path"], nshards) != shard_id:
            continue
        url = fmeta["cdn"]
        ext = os.path.splitext(fmeta["path"])[1].lower()
        # stream to temp file to avoid holding full file in memory
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            for chunk in stream_cdn(url):
                tmp.write(chunk)
            tmp_path = tmp.name
        # delegate parse to dataset-enrich.sh or a small python parser
        # emit temp path for downstream processing
        print(tmp_path)

if __name__ == "__main__":
    main()
```

### bin/dataset-enrich.sh (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

SNAPSHOT="${1:-}"
SHARD_ID="${2:-0}"
NSHARDS="${3:-16}"

if [[ -z "$SNAPSHOT" || ! -f "$SNAPSHOT" ]]; then
  echo "Usage: $0 <snapshot.json> <shard_id> <nshards>" >&2
  exit 1
fi

# Use CDN fetcher to materialize local files for this shard
mapfile -t LOCAL_FILES < <(python3 bin/fetch_cdn.py "$SNAPSHOT" "$SHARD_ID" "$NSHARDS")

OUTDIR="batches/public-merged/$(date +%F)"
mkdir -p "$OUTDIR"
OUTFILE="${OUTDIR}/shard${SHARD_ID}-$(date +%H%M%S).jsonl"

# Process each local file and project to {prompt,response}
for f in "${LOCAL_FILES[@]}"; do
  case "$f" in
    *.jsonl)
      # lightweight projection: keep only prompt/response fields
      jq -c '{prompt: .prompt // .input // .text, response: .response // .output // .completion}' "$f" >> "$OUTFILE" || true
      ;;
    *.parquet)
      # use pyarrow projection to avoid mixed-schema CastError
      python3 -c "
import pyarrow.parquet as pq, json, sys
tbl = pq.read_table('$f')
cols = tbl.column_names
prompt_col = next((c for c in ('prompt','input','text') if c in cols), None)
response_col = next((c for c in ('response','output','completion') if c in cols), None)
if prompt_col and response_col:
    for b in range(tbl.num_rows):
        row = { 'prompt': tbl[prompt_col][b].as_py(), 'response': tbl[response_col][b].as_py() }
        print(json.dumps(row, ensure_ascii=False))
" >> "$OUTFILE" || true
      ;;
  esac
  # optional: immediate dedup via lib/dedup.py
  # python3 lib/dedup.py --input "$OUTFILE" --output "$OUTFILE.dedup" && mv "$OUTFILE.dedup" "$OUTFILE"
done

echo "Shard $SHARD_ID

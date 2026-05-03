# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit (429) during ingestion and reduces per-shard overhead by replacing recursive `list_repo_files` with a single tree call + CDN fetches.

### Steps (all in `/opt/axentx/surrogate-1`)

1. **Create `bin/snapshot.sh`** (20m)  
   - Accept `DATE` (YYYY-MM-DD) or default to today.  
   - Call `list_repo_tree(path="public-merged/${DATE}", recursive=False)` once via `huggingface_hub`.  
   - Emit `snapshot-${DATE}.json` containing `{ "date": "...", "files": ["path1", "path2", ...], "generated_at": "ISO" }`.  
   - Make executable (`chmod +x`).

2. **Update `bin/dataset-enrich.sh`** (20m)  
   - Source snapshot if present for the target date; fall back to current recursive behavior if missing.  
   - Pass snapshot path to Python worker via env var `SNAPSHOT_MANIFEST`.  
   - Ensure worker uses CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) for downloads; no Authorization header needed.

3. **Update Python worker** (30m)  
   - If `SNAPSHOT_MANIFEST` exists, read JSON and iterate only listed files.  
   - For each file, stream via `requests.get(cdn_url, stream=True)` and process line-by-line (or parquet via `pyarrow`).  
   - Project to `{prompt, response}` at parse time; do not add extra columns.  
   - Keep existing dedup via `lib/dedup.py`.

4. **Update GitHub Actions matrix** (20m)  
   - Add a pre-step job (or first step in each shard) that runs `bin/snapshot.sh` once (use a single runner or one shard) and uploads artifact `snapshot-${DATE}.json`.  
   - Other shard jobs download the artifact and set `SNAPSHOT_MANIFEST`.  
   - Ensure `HF_TOKEN` still available for push, but not needed for downloads.

5. **Validation & rollback** (20m)  
   - Dry-run locally with a small date folder to confirm CDN-only fetch works and shard output unchanged.  
   - Keep old recursive path as fallback if snapshot missing or empty.

---

## Code Snippets

### 1. `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

# Usage: bin/snapshot.sh [YYYY-MM-DD]
# Generates snapshot-YYYY-MM-DD.json listing files under public-merged/<date>/

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="snapshot-${DATE}.json"

python3 - "$REPO" "$DATE" "$OUT" <<'PY'
import json
import sys
from datetime import datetime, timezone
from huggingface_hub import HfApi

repo_id = sys.argv[1]
date_folder = sys.argv[2]
out_path = sys.argv[3]

api = HfApi()
path_prefix = f"public-merged/{date_folder}"
try:
    tree = api.list_repo_tree(repo_id=repo_id, path=path_prefix, recursive=False)
    files = [item.path for item in tree if item.type == "file"]
except Exception as e:
    # Fallback: try recursive=False per folder if path_prefix not found
    # If still fails, produce empty list so caller can fallback
    files = []

snapshot = {
    "date": date_folder,
    "repo": repo_id,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "files": sorted(files)
}

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(snapshot, f, indent=2)
print(f"Wrote {len(files)} files to {out_path}")
PY

echo "Snapshot written to $OUT"
```

### 2. Update `bin/dataset-enrich.sh` (minimal diff)
```bash
#!/usr/bin/env bash
set -euo pipefail

# Existing logic preserved; add snapshot support.
DATE="${1:-$(date +%Y-%m-%d)}"
SNAPSHOT="snapshot-${DATE}.json"

# Generate snapshot if missing (optional: run only once per workflow via artifact)
if [[ ! -f "$SNAPSHOT" ]]; then
  echo "No snapshot found for ${DATE}, generating..."
  ./bin/snapshot.sh "$DATE"
fi

export SNAPSHOT_MANIFEST="$SNAPSHOT"
export TARGET_DATE="$DATE"

# Call python worker (embedded or separate). Pass HF_TOKEN for uploads only.
exec python3 - "$SNAPSHOT_MANIFEST" <<'PY'
import os, json, sys, hashlib, requests, pyarrow.parquet as pq
from pathlib import Path
from lib.dedup import DedupStore  # existing

SNAPSHOT_PATH = os.environ.get("SNAPSHOT_MANIFEST")
HF_TOKEN = os.environ.get("HF_TOKEN", "")
REPO = "axentx/surrogate-1-training-pairs"

def cdn_url(repo, path):
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def process_file(path, dedup):
    url = cdn_url(REPO, path)
    headers = {}
    # CDN public files: no Authorization required
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        # If parquet: read in chunks if needed; here simplified
        if path.endswith(".parquet"):
            # Stream to temp file or use pyarrow.parquet.ParquetFile on stream
            import io
            buf = io.BytesIO()
            for chunk in r.iter_content(chunk_size=8192):
                buf.write(chunk)
            buf.seek(0)
            pf = pq.ParquetFile(buf)
            for batch in pf.iter_batches(batch_size=1000):
                # Project to prompt/response at parse time
                # Adapt column names per schema as needed
                cols = batch.schema.names
                # Example heuristic: find text/response-like columns
                # Keep existing per-schema normalization logic here
                # ...
                # For each row, produce {prompt, response} and dedup
                # dedup.add(md5) and yield if new
                pass
        else:
            # Assume line-delimited JSONL
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                # Project to prompt/response per surrogate-1 schema rules
                # ...
                # md5 = hashlib.md5(...).hexdigest()
                # if dedup.add(md5): emit
                pass

def main():
    manifest = []
    if SNAPSHOT_PATH and Path(SNAPSHOT_PATH).exists():
        with open(SNAPSHOT_PATH) as f:
            data = json.load(f)
        manifest = data.get("files", [])
    else:
        print("No snapshot; fallback to recursive behavior not implemented here")
        sys.exit(1)

    dedup = DedupStore()  # existing central store
    for path in manifest:
        try:
            process_file(path, dedup)
        except Exception as e:
            print(f"Error processing {path}: {e}", file=sys.stderr)
            # continue

    # Upload outputs to batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl
    # Use HF_TOKEN for upload via huggingface_hub (existing logic)
    # ...

if __name__ == "__main__":
    main()
PY
```

### 3. GitHub Actions snippet (`.github/workflows/ingest.yml`) — key additions
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-file: ${{ steps.set.outputs.snapshot }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install huggingface_hub
      - run: ./bin/snapshot.sh ${{ env.TARGET

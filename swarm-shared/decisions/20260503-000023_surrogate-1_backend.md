# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit (429) and commit-cap (128/hr) pressure during ingestion by replacing per-shard `list_repo_files` calls with a single tree listing + CDN fetches.

### Steps (1h 45m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Accept `DATE` (YYYY-MM-DD) or default to yesterday.  
   - Use `huggingface_hub` to call `list_repo_tree(path="batches/public-merged/${DATE}", recursive=True)` and save to `snapshot/${DATE}/snapshot.json`.  
   - Include `sha256` of each file via `file.rfilename` + `file.size` + `file.last_modified` to detect changes.  
   - Exit 0 if no new files (skip downstream).  
   - On 429, wait 360s then retry once.

2. **Update `bin/dataset-enrich.sh`** (20m)  
   - Source snapshot: read `snapshot/${DATE}/snapshot.json`.  
   - Compute deterministic shard assignment: `hash(slug) % 16 == SHARD_ID`.  
   - For each assigned file, download via CDN URL:  
     `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/batches/public-merged/${DATE}/${file}`  
   - Stream-parse, normalize, dedup via `lib/dedup.py`, emit to `shard-<N>-<HHMMSS>.jsonl`.

3. **Update GitHub Actions matrix** (20m)  
   - Add a pre-job step that runs `bin/snapshot.sh` on a schedule or `workflow_dispatch`.  
   - Upload snapshot as artifact and pass to each shard via `matrix.snapshot_file`.  
   - Ensure 16 shards reuse the same snapshot (no per-shard API calls).

4. **Add training script support** (20m)  
   - Create `train.py` snippet that loads snapshot JSON and uses `datasets.load_dataset` with `data_files` pointing to CDN URLs (list of paths).  
   - Set `streaming=True` and use `map` to project fields; no `list_repo_files` during training.  
   - Add fallback: if snapshot missing, run snapshot generator on Mac (non-GPU) then start Lightning Studio job.

5. **Add dedup store reuse note** (10m)  
   - Document that central SQLite dedup store remains source of truth; snapshot only avoids API calls, not cross-run duplicates.

6. **Test locally** (15m)  
   - Run snapshot for a small date folder, verify JSON.  
   - Run one shard with snapshot, confirm CDN downloads and no HF API auth calls (check logs for 429).  
   - Verify output path format: `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

### Code Snippets

#### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
DATE="${1:-$(date -d yesterday +%F)}"
OUTDIR="${2:-snapshot/$DATE}"

mkdir -p "$OUTDIR"
OUTFILE="${OUTDIR}/snapshot.json"
PATH_PREFIX="batches/public-merged/${DATE}"

echo "Listing ${REPO} tree at ${PATH_PREFIX}..."
python3 - <<PY
import json, os, sys
from huggingface_hub import HfApi

repo = os.getenv("REPO")
path = os.getenv("PATH_PREFIX")
api = HfApi()
try:
    tree = api.list_repo_tree(repo=repo, path=path, recursive=True)
except Exception as e:
    # If 429, wait 360s then retry once
    import time
    if "429" in str(e):
        print("Rate limited, waiting 360s...")
        time.sleep(360)
        tree = api.list_repo_tree(repo=repo, path=path, recursive=True)
    else:
        raise

files = []
for item in tree:
    if hasattr(item, "rfilename"):
        files.append({
            "path": item.rfilename,
            "size": getattr(item, "size", 0),
            "last_modified": getattr(item, "last_modified", "").isoformat() if hasattr(item, "last_modified") else ""
        })
snapshot = {
    "date": os.getenv("DATE"),
    "repo": repo,
    "path_prefix": path,
    "snapshot_ts": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    "files": sorted(files, key=lambda x: x["path"])
}
with open(os.getenv("OUTFILE"), "w") as f:
    json.dump(snapshot, f, indent=2)
print(f"Wrote {len(files)} files to {os.getenv('OUTFILE')}")
PY

echo "Snapshot saved to ${OUTFILE}"
```

#### `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
SHARD_ID="${SHARD_ID:-0}"
DATE="${DATE:-$(date -d yesterday +%F)}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-snapshot/$DATE/snapshot.json}"

if [[ -n "$SNAPSHOT_FILE" && -f "$SNAPSHOT_FILE" ]]; then
  echo "Using snapshot: $SNAPSHOT_FILE"
  mapfile -t ALL_FILES < <(python3 -c "import json,sys; f=json.load(open(sys.argv[1])); print('\n'.join(item['path'] for item in f['files']))" "$SNAPSHOT_FILE")
else
  echo "No snapshot; listing repo (may hit rate limits)..."
  mapfile -t ALL_FILES < <(python3 -c "from huggingface_hub import HfApi; import os; api=HfApi(); tree=api.list_repo_tree(repo=os.getenv('REPO'), path='batches/public-merged/$(date +%F)', recursive=True); print('\n'.join(t.rfilename for t in tree if hasattr(t, 'rfilename')))")
fi

# Deterministic shard assignment
for f in "${ALL_FILES[@]}"; do
  slug=$(basename "$f" .jsonl)
  hash=$(echo -n "$slug" | md5sum | cut -c1-8)
  shard=$((0x$hash % 16))
  if [[ $shard -eq $SHARD_ID ]]; then
    # CDN download (bypass API auth)
    url="https://huggingface.co/datasets/${REPO}/resolve/main/${f}"
    echo "Processing shard $SHARD_ID: $url"
    # stream, normalize, dedup, upload...
  fi
done
```

#### `train.py` (excerpt)
```python
import json
from pathlib import Path
from datasets import load_dataset

def load_from_snapshot(snapshot_path, streaming=True):
    with open(snapshot_path) as f:
        snap = json.load(f)
    repo = snap["repo"]
    files = [f"https://huggingface.co/datasets/{repo}/resolve/main/{item['path']}" for item in snap["files"]]
    ds = load_dataset("json", data_files=files, streaming=streaming, split="train")
    # Project to {prompt, response} only
    def project(ex):
        return {"prompt": ex.get("prompt", ""), "response": ex.get("response", "")}
    return ds.map(project, remove_columns=ds.features.keys() if not streaming else None)

# Usage
# ds = load_from_snapshot("snapshot/2026-05-02/snapshot.json")
```

#### `.github/workflows/ingest.yml` (excerpt)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot_file: ${{ steps.upload.outputs.snapshot_file }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install huggingface_h

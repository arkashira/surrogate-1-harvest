# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit pressure during ingestion and aligns with the CDN bypass pattern.

### Steps (1h 30m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Accept `DATE` (YYYY-MM-DD) and optional `REPO` (default: `axentx/surrogate-1-training-pairs`)  
   - Use `huggingface_hub` CLI or Python one-liner to call `list_repo_tree(path=f"public-merged/{DATE}", recursive=True)`  
   - Filter to `.parquet`/`.jsonl` files, produce `snapshot-{DATE}.json` with `{"date":"...","files":["path1","path2",...],"generated_at":"ISO8601"}`  
   - Save to `snapshots/` and optionally upload as artifact or commit to repo (non-blocking)

2. **Update `bin/dataset-enrich.sh`** (20m)  
   - Add optional `SNAPSHOT_FILE` env var; if provided, skip `list_repo_tree` and read file list from snapshot  
   - Keep fallback to live API for backward compatibility  
   - Each shard uses the same snapshot → zero API calls during streaming

3. **Add `bin/build-manifest.py`** (15m)  
   - Small utility to convert snapshot into per-shard manifest slices (by `shard_id`)  
   - Output: `manifest-shard-<N>.json` with only the files assigned to that shard (deterministic by hash)  
   - Embed into GitHub Actions matrix so each job gets its own manifest

4. **Update GitHub Actions workflow** (20m)  
   - Add a pre-step job that runs `snapshot.sh` and uploads artifact  
   - Pass `SNAPSHOT_FILE` to each matrix job via `needs.snapshot.outputs.manifest-<shard>` or shared artifact download  
   - Ensure `HF_TOKEN` only used in snapshot step (CDN downloads need no token)

5. **Add training-side support** (15m)  
   - Create `scripts/train-cdn.py` that reads manifest JSON and downloads via `hf_hub_download` or raw CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`)  
   - Use `pyarrow` to stream only projected `{prompt,response}` fields  
   - No `load_dataset(streaming=True)` on heterogeneous schemas

6. **Validation & cleanup** (20m)  
   - Run snapshot locally for a recent date, verify file list matches live API  
   - Run one shard with manifest to confirm CDN-only fetch (check logs for no `/api/` calls)  
   - Remove any leftover `list_repo_tree` calls from worker scripts

---

## Code Snippets

### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="${2:-snapshots}"
OUTFILE="${OUTDIR}/snapshot-${DATE}.json"

mkdir -p "${OUTDIR}"

python3 - <<PY
import json, os, sys
from huggingface_hub import HfApi
from datetime import datetime, timezone

repo = os.environ["REPO"]
date = os.environ["DATE"]
path = f"public-merged/{date}"

api = HfApi()
try:
    tree = api.list_repo_tree(repo=repo, path=path, recursive=True)
    files = [f.rfilename for f in tree if f.rfilename.endswith(('.parquet', '.jsonl'))]
except Exception as e:
    # Fallback: try non-recursive on date folder then collect
    tree = api.list_repo_tree(repo=repo, path=path, recursive=False)
    files = []
    for entry in tree:
        if entry.rfilename.endswith('.parquet') or entry.rfilename.endswith('.jsonl'):
            files.append(entry.rfilename)

output = {
    "repo": repo,
    "date": date,
    "path": path,
    "files": sorted(files),
    "generated_at": datetime.now(timezone.utc).isoformat()
}

with open(os.environ["OUTFILE"], "w") as f:
    json.dump(output, f, indent=2)

print(f"Snapshot written: {os.environ['OUTFILE']} ({len(files)} files)")
PY
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

### `bin/build-manifest.py`
```python
#!/usr/bin/env python3
"""
Usage: python bin/build-manifest.py snapshots/snapshot-2026-05-02.json 16
Outputs manifest-shard-<i>.json for i in 0..15
"""
import json, hashlib, sys, os

def shard_for_file(filepath: str, n_shards: int) -> int:
    slug = os.path.splitext(os.path.basename(filepath))[0]
    h = hashlib.md5(slug.encode()).hexdigest()
    return int(h, 16) % n_shards

def main():
    snapshot_path = sys.argv[1]
    n_shards = int(sys.argv[2])

    with open(snapshot_path) as f:
        data = json.load(f)

    files = data["files"]
    out_dir = os.path.join(os.path.dirname(snapshot_path), "manifests")
    os.makedirs(out_dir, exist_ok=True)

    for i in range(n_shards):
        shard_files = [f for f in files if shard_for_file(f, n_shards) == i]
        manifest = {
            "shard_id": i,
            "n_shards": n_shards,
            "date": data["date"],
            "repo": data["repo"],
            "files": shard_files
        }
        out_path = os.path.join(out_dir, f"manifest-shard-{i}.json")
        with open(out_path, "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"Shard {i}: {len(shard_files)} files -> {out_path}")

if __name__ == "__main__":
    main()
```

---

### Updated `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
N_SHARDS="${N_SHARDS:-16}"
MANIFEST_FILE="${MANIFEST_FILE:-}"  # optional: path to manifest-shard-<id>.json

if [[ -n "$MANIFEST_FILE" && -f "$MANIFEST_FILE" ]]; then
    echo "Using manifest: $MANIFEST_FILE"
    mapfile -t FILES < <(python3 -c "import json,sys;print('\n'.join(json.load(open(sys.argv[1]))['files']))" "$MANIFEST_FILE")
else
    echo "No manifest provided; listing via API (rate-limited)"
    mapfile -t FILES < <(python3 - <<PY
from huggingface_hub import HfApi
api = HfApi()
tree = api.list_repo_tree(repo="$REPO", path="public-merged/$DATE", recursive=True)
for f in tree:
    if f.rfilename.endswith(('.parquet', '.jsonl')):
        print(f.rfilename)
PY
    )
fi

# Filter to this shard's files (if not using manifest)
if [[ -z "$MANIFEST_FILE" ]]; then
    TMP=()
    for f in "${FILES[@]}"; do
        slug=$(basename "$f" | cut -d. -f1)
        h=$(echo -n "$slug" | md5sum | cut -c1-32)
        shard=$(( 0x$h % N_SHARDS ))
        if [[ $shard -eq $SHARD_ID ]]; then
            TMP+=("$f")
        fi
    done
    FILES=("${TMP[@]}")
fi

echo "Processing ${#FILES[@]} files for shard $SHARD_ID"
# ... rest of worker logic (stream each file via CDN URL)
```

---


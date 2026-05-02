# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit (429) during ingestion and reduces per-shard overhead by ~30–60s each run.

### Steps (1h 45m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Uses `huggingface_hub` to call `list_repo_tree(path, recursive=False)` for each date folder under `public-merged/` (or root if no date folders).  
   - Emits `snapshot-<date>.json` with `{"repo": "...", "date": "ISO-8601", "generated_at": "ISO-8601", "files": [{"path": "...", "size": int, "sha": "...", "cdn_url": "..."}]}`.  
   - Shebang `#!/usr/bin/env bash`, `set -euo pipefail`.  
   - Exits non-zero if API fails (so workflow fails fast).  
   - Saves to `snapshots/` directory.

2. **Update `bin/dataset-enrich.sh`** (25m)  
   - Accept optional `SNAPSHOT_FILE` env var (default `snapshots/snapshot-*.json`).  
   - If present, reads shard assignment from snapshot instead of live `list_repo_files`.  
   - Falls back to live listing if snapshot missing (backwards compatibility).  
   - Uses deterministic `md5(path) % TOTAL_SHARDS` for shard assignment.  
   - Downloads via CDN URL with `curl -L -s -o` (no auth header).  
   - Keeps existing schema normalization and dedup via `lib/dedup.py`.

3. **Update GitHub Actions matrix** (15m)  
   - Add a pre-step job that runs `snapshot.sh` once and uploads artifact `snapshot-*.json`.  
   - Modify 16-shard matrix job to download snapshot artifact and pass `SNAPSHOT_PATH` to each runner.  
   - Ensure `HF_TOKEN` still available for push (upload) but not for listing during shards.

4. **Add training script integration** (15m)  
   - Create `bin/embed-snapshot.py` that reads snapshot JSON and generates `train_filelist.txt` with CDN URLs.  
   - Update `train.py` (or surrogate-1 training entrypoint) to read filelist and use direct CDN fetch for each file, projecting only `{prompt, response}` at parse time.  
   - Avoid `load_dataset(streaming=True)` entirely for heterogeneous repos.

5. **Validation & cleanup** (20m)  
   - Run snapshot locally against `axentx/surrogate-1-training-pairs` (dry-run).  
   - Run one shard with snapshot to verify CDN downloads and schema handling.  
   - Ensure no HF API calls during shard processing (check logs for 429 or `/api/` endpoints).  
   - Commit changes; update README with usage.

---

### Code Snippets

#### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
OUTDIR="${2:-snapshots}"
DATE_TAG=$(date +%Y%m%d)
OUTFILE="${OUTDIR}/snapshot-${DATE_TAG}.json"

mkdir -p "${OUTDIR}"

python3 - <<PY
import os, json, sys, datetime
from huggingface_hub import HfApi

repo = os.environ.get("REPO", "$REPO")
outfile = os.environ.get("OUTFILE", "$OUTFILE")
base_path = os.environ.get("BASE_PATH", "public-merged")

api = HfApi()
entries = []

try:
    # List top-level folders (date folders) then files within each
    tree = api.list_repo_tree(repo=repo, recursive=False, path=base_path)
    for item in tree:
        if item.type == "directory":
            subpath = item.path
            subfiles = api.list_repo_tree(repo=repo, recursive=False, path=subpath)
            for f in subfiles:
                if f.type == "file":
                    entries.append({
                        "repo": repo,
                        "path": f.path,
                        "size": f.size,
                        "sha": f.bin_sha if hasattr(f, "bin_sha") else None,
                        "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{f.path}"
                    })
        elif item.type == "file":
            entries.append({
                "repo": repo,
                "path": item.path,
                "size": item.size,
                "sha": item.bin_sha if hasattr(item, "bin_sha") else None,
                "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{item.path}"
            })
except Exception as e:
    print(f"Error listing repo: {e}", file=sys.stderr)
    sys.exit(1)

output = {
    "repo": repo,
    "date": datetime.date.today().isoformat(),
    "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    "files": entries
}

with open(outfile, "w") as f:
    json.dump(output, f, indent=2)
print(outfile)
PY
```

#### `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
SNAPSHOT="${SNAPSHOT_FILE:-}"

cd "$(dirname "$0")/.."

if [[ -n "$SNAPSHOT" && -f "$SNAPSHOT" ]]; then
    echo "Using snapshot: $SNAPSHOT"
    mapfile -t FILES < <(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for item in data.get('files', []):
    print(item['path'])
" "$SNAPSHOT")
else
    echo "No snapshot; falling back to HF API listing (may hit rate limits)"
    mapfile -t FILES < <(python3 -c "
from huggingface_hub import HfApi
api = HfApi()
tree = api.list_repo_tree(repo='axentx/surrogate-1-training-pairs', recursive=True, path='')
for item in tree:
    if item.type == 'file':
        print(item.path)
")
fi

# Deterministic shard assignment by path hash
process_file() {
    local path="$1"
    local hash
    hash=$(echo -n "$path" | md5sum | cut -c1-8)
    local bucket=$(( 0x$hash % TOTAL_SHARDS ))
    if [[ $bucket -eq $SHARD_ID ]]; then
        echo "Processing: $path"
        # Download via CDN (no auth)
        url="https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/${path}"
        curl -L -s -o "/tmp/$(basename "$path")" "$url"
        # Normalize and dedup (existing logic)
        python3 bin/normalize.py "/tmp/$(basename "$path")"
    fi
}

export -f process_file
export SHARD_ID TOTAL_SHARDS

printf "%s\n" "${FILES[@]}" | xargs -P 4 -I {} bash -c 'process_file "$@"' _ {}
```

#### `.github/workflows/ingest.yml` (excerpt)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-path: ${{ steps.set.outputs.path }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install huggingface_hub
      - run: |
          mkdir -p snapshots
          python3 bin/snapshot.py axentx/surrogate-1-training-pairs snapshots
      - name: Upload snapshot
        uses: actions/upload-artifact@v4
        with:
          name: snapshot-${{ github.run_id }}
          path: snapshots/snapshot-*.json
      - id: set
        run: |
          SNAP=$(ls snapshots/snapshot-*.json | head -1)


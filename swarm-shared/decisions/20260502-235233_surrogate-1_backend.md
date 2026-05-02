# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit (429) during ingestion and removes `list_repo_files` recursive overhead.

### Steps (1h 30m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Uses `huggingface_hub` to call `list_repo_tree(path, recursive=False)` per date folder  
   - Outputs `snapshot.json` with CDN URLs (`resolve/main/...`) and file metadata  
   - Single API call per folder → stays under 1000 req/5min limit  
   - Saves to `snapshot/<date>/files.json`

2. **Update `bin/dataset-enrich.sh`** (20m)  
   - Accept optional snapshot path; if provided, reads file list from JSON instead of calling HF API  
   - Falls back to current behavior if snapshot missing (backward compat)  
   - Uses CDN URLs directly via `curl`/`wget` or `hf_hub_download` with `repo_type=dataset`

3. **Add `bin/generate-file-list.py`** (20m)  
   - Python helper that `list_repo_tree` for given repo+path, flattens, filters by extension (jsonl/parquet)  
   - Emits `{"repo": "...", "path": "...", "cdn_url": "...", "size": ...}`  
   - Writes to stdout or file

4. **Update GitHub Actions matrix** (10m)  
   - Add step before 16-shard job: `Generate snapshot` using `snapshot.sh`  
   - Upload snapshot as artifact, download in each shard job  
   - Pass `SNAPSHOT_FILE` env var to `dataset-enrich.sh`

5. **Update training script integration** (20m)  
   - Add flag `--file-list snapshot.json` to training launcher  
   - Data loader reads manifest, uses CDN URLs with `wget` or `datasets.load_dataset` with `data_files` pointing to local paths after download  
   - Zero API calls during training

6. **Add dedup cache warm-up** (10m)  
   - Snapshot step also pulls latest central md5 store from HF Space (if available) to reduce cross-run duplicates  
   - Optional: embed cache hash in snapshot filename for traceability

7. **Test locally** (20m)  
   - Run snapshot against `axentx/surrogate-1-training-pairs`  
   - Run one shard with snapshot, verify CDN downloads and no 429  
   - Validate output schema unchanged

---

## Code Snippets

### `bin/generate-file-list.py`
```python
#!/usr/bin/env python3
"""
Generate a file list manifest for a HuggingFace dataset repo.
Uses list_repo_tree (non-recursive per folder) to avoid 429 rate limits.
Outputs JSON lines: {"repo":"...","path":"...","cdn_url":"...","size":...}
"""
import json
import os
import sys
from pathlib import Path
from huggingface_hub import HfApi, list_repo_tree

def main():
    repo = os.getenv("HF_REPO", "datasets/axentx/surrogate-1-training-pairs")
    root = os.getenv("HF_PATH", "")
    out = Path(os.getenv("OUT", "snapshot/files.json"))
    token = os.getenv("HF_TOKEN")

    api = HfApi(token=token)
    out.parent.mkdir(parents=True, exist_ok=True)

    # If root is empty, list top-level folders (assume date folders)
    entries = list_repo_tree(repo=repo, path=root, repo_type="dataset", token=token)
    folders = [e for e in entries if e.type == "directory"]
    if not folders:
        folders = [type('', (), {'path': root})]  # single root

    results = []
    for folder in folders:
        folder_path = folder.path
        items = list_repo_tree(repo=repo, path=folder_path, repo_type="dataset", token=token)
        for item in items:
            if item.type != "file":
                continue
            if not item.path.endswith((".jsonl", ".parquet", ".json")):
                continue
            cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{item.path}"
            results.append({
                "repo": repo,
                "path": item.path,
                "cdn_url": cdn_url,
                "size": getattr(item, "size", None),
                "folder": folder_path,
            })

    out.write_text(json.dumps(results, indent=2))
    print(f"Wrote {len(results)} files to {out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# Generate a snapshot of dataset files for CDN-only ingestion.
# Usage: HF_TOKEN=... HF_REPO=... ./snapshot.sh [output-dir]
set -euo pipefail

REPO="${HF_REPO:-datasets/axentx/surrogate-1-training-pairs}"
OUTDIR="${1:-snapshot}"
DATE_PART=$(date +%Y-%m-%d)
OUTFILE="${OUTDIR}/${DATE_PART}/files.json"

mkdir -p "$(dirname "$OUTFILE")"

echo "Generating snapshot for ${REPO}..."
HF_REPO="${REPO}" OUT="${OUTFILE}" python3 bin/generate-file-list.py

# Produce a compact manifest for training (just paths + cdn urls)
jq -c '.[] | {path, cdn_url}' "${OUTFILE}" > "${OUTDIR}/${DATE_PART}/manifest.ndjson"

echo "Snapshot saved to ${OUTFILE}"
echo "Manifest saved to ${OUTDIR}/${DATE_PART}/manifest.ndjson"
```

### Updated `bin/dataset-enrich.sh` (partial diff)
```bash
# Add near top, after shebang
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"

download_with_cdn() {
  local url="$1"
  local out="$2"
  # Use CDN URL directly; retry on transient failures
  curl -fsSL --retry 3 --retry-delay 1 -o "$out" "$url"
}

process_file() {
  local rel_path="$1"
  local out_dir="$2"
  if [[ -n "$SNAPSHOT_FILE" && -f "$SNAPSHOT_FILE" ]]; then
    # Use CDN URL from snapshot
    local cdn_url
    cdn_url=$(jq -r --arg p "$rel_path" '.[] | select(.path==$p) | .cdn_url' "$SNAPSHOT_FILE")
    if [[ -n "$cdn_url" && "$cdn_url" != "null" ]]; then
      download_with_cdn "$cdn_url" "${out_dir}/$(basename "$rel_path")"
      return 0
    fi
  fi
  # Fallback to hf_hub_download
  huggingface_hub download "$REPO" "$rel_path" --repo-type dataset -o "$out_dir/"
}
```

### GitHub Actions snippet (`.github/workflows/ingest.yml` partial)
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
      - run: pip install -r requirements.txt huggingface_hub
      - run: mkdir -p snapshot
      - run: HF_TOKEN=${{ secrets.HF_TOKEN }} HF_REPO=datasets/axentx/surrogate-1-training-pairs ./bin/snapshot.sh snapshot
      - uses: actions/upload-artifact@v4
        with:
          name: snapshot
          path: snapshot/
      - id: set
        run: echo "path=snapshot/$(date +%Y-%m-%d)/files.json" >> $GITHUB_OUTPUT

  ingest:
    needs: snapshot
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: snapshot
          path: snapshot/
      - run: |


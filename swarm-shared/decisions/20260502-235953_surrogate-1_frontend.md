# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit (429) during ingestion and reduces per-shard overhead by replacing recursive `list_repo_files` with a single tree call + CDN fetches.

### Concrete steps (1h 45m total)

1. **Create `bin/lib/snapshot.py`** (20m)  
   - Uses `huggingface_hub.HfApi.list_repo_tree(path, recursive=False)` for a date folder (e.g. `public-merged/2026-05-02/`).  
   - Saves JSON manifest: `{"repo":"...","date_folder":"...","snapshot_ts":"...","files":["file1.parquet",...],"count":N}` to `snapshot/<date>.json` and `snapshot/latest.json`.  
   - Exits non-zero on API errors; logs file count.

2. **Create `bin/snapshot.sh`** (15m)  
   - Thin wrapper that calls `snapshot.py` with `HF_DATASET_REPO` and `SNAPSHOT_DATE` (defaults to UTC today).  
   - Creates `snapshot/` directory and outputs both dated and latest manifest.  
   - Fails fast if tree call fails.

3. **Update `bin/dataset-enrich.sh`** (35m)  
   - Accept optional `SNAPSHOT_FILE` env var. If provided and valid, reads file list from snapshot instead of calling `list_repo_tree` recursively.  
   - Deterministic shard assignment unchanged: `hash(slug) % TOTAL_SHARDS == SHARD_ID` (using MD5 of basename).  
   - Downloads via CDN URL: `https://huggingface.co/datasets/<repo>/resolve/main/<rel_path>` (no auth header).  
   - Keep existing schema projection + dedup logic.  
   - If no snapshot, fall back to non-recursive tree call (single call, not per-file).

4. **Update GitHub Actions matrix** (20m)  
   - Add a pre-job `snapshot` that runs `bin/snapshot.sh` and uploads artifact `snapshot-<date>.json`.  
   - Pass snapshot path to each shard via `env.SNAPSHOT_FILE`.  
   - Keep 16-shard matrix; all shards share the same snapshot file.

5. **Validation & docs** (15m)  
   - Add `README.md` note describing CDN bypass, snapshot usage, and fallback behavior.  
   - Quick smoke test: run snapshot locally, verify manifest, run one shard with snapshot.

---

## Code snippets

### `bin/lib/snapshot.py`
```python
#!/usr/bin/env python3
"""
Generate snapshot of dataset files for a date folder.
Usage: python bin/lib/snapshot.py <repo> <date_folder> [output.json]
"""
import json
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

def generate_snapshot(repo: str, date_folder: str):
    api = HfApi()
    # list_repo_tree handles folder path; recursive=False lists immediate children
    tree = api.list_repo_tree(repo=repo, path=date_folder.rstrip("/"), recursive=False)
    files = [
        item.path
        for item in tree
        if item.type == "file" and item.path.lower().endswith((".parquet", ".jsonl"))
    ]
    snapshot = {
        "repo": repo,
        "date_folder": date_folder.rstrip("/"),
        "snapshot_ts": datetime.now(timezone.utc).isoformat(),
        "files": sorted(files),
        "count": len(files),
    }
    return snapshot

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: snapshot.py <repo> <date_folder> [output.json]")
        sys.exit(1)
    repo = sys.argv[1]
    date_folder = sys.argv[2]
    out_path = sys.argv[3] if len(sys.argv) > 3 else "-"
    snapshot = generate_snapshot(repo, date_folder)
    if out_path == "-":
        json.dump(snapshot, sys.stdout, indent=2)
    else:
        with open(out_path, "w") as f:
            json.dump(snapshot, f, indent=2)
```

### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# Generate snapshot of dataset files for a date folder.
# Usage: SNAPSHOT_DATE=2026-05-02 ./bin/snapshot.sh
# Outputs: snapshot/latest.json and snapshot/<date>.json
set -euo pipefail

REPO="${HF_DATASET_REPO:-axentx/surrogate-1-training-pairs}"
DATE_FOLDER="${SNAPSHOT_DATE:-$(date -u +%Y-%m-%d)}"
OUT_DIR="snapshot"
LATEST="${OUT_DIR}/latest.json"
DATED="${OUT_DIR}/${DATE_FOLDER}.json"

mkdir -p "${OUT_DIR}"

echo "Generating snapshot for ${REPO} -> public-merged/${DATE_FOLDER}"
python3 bin/lib/snapshot.py "${REPO}" "public-merged/${DATE_FOLDER}" "${DATED}"

# Also keep latest
cp "${DATED}" "${LATEST}"

echo "Snapshot saved: ${DATED}"
echo "Files: $(jq '.count' "${DATED}")"
```

### Updated `bin/dataset-enrich.sh` (key excerpt)
```bash
#!/usr/bin/env bash
# ... existing header ...
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE_FOLDER="${DATE_FOLDER:-$(date -u +%Y-%m-%d)}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"

# Determine file list
if [[ -n "${SNAPSHOT_FILE}" && -f "${SNAPSHOT_FILE}" ]]; then
  echo "Using snapshot: ${SNAPSHOT_FILE}"
  mapfile -t ALL_FILES < <(jq -r '.files[]' "${SNAPSHOT_FILE}")
else
  echo "No snapshot provided; listing repo tree (non-recursive) for public-merged/${DATE_FOLDER}"
  mapfile -t ALL_FILES < <(
    python3 -c "
import sys
from huggingface_hub import HfApi
api = HfApi()
tree = api.list_repo_tree('${REPO}', path='public-merged/${DATE_FOLDER}', recursive=False)
for item in tree:
    if item.type == 'file' and item.path.lower().endswith(('.parquet','.jsonl')):
        print(item.path)
"
  )
fi

# Shard assignment (unchanged)
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"

process_file() {
  local rel_path="$1"
  local cdn_url="https://huggingface.co/datasets/${REPO}/resolve/main/${rel_path}"
  # Download via CDN (no auth header) and process
  # ... existing normalization + dedup logic ...
}

for f in "${ALL_FILES[@]}"; do
  # Deterministic shard selection
  slug=$(basename "$f" | sed 's/\.[^.]*$//')
  bucket=$(( $(echo -n "$slug" | md5sum | cut -c1-8) % TOTAL_SHARDS ))
  if [[ "$bucket" -eq "$SHARD_ID" ]]; then
    process_file "$f"
  fi
done
```

### GitHub Actions snippet (`.github/workflows/ingest.yml` excerpt)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-path: ${{ steps.set.outputs.snapshot }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: ./bin/snapshot.sh
        env:
          HF_DATASET_REPO: axentx/surrogate-1-training-pairs
          SNAPSHOT_DATE: ${{ vars.SNAPSHOT_DATE || '' }}
      - name: Upload snapshot
        uses: actions/upload-artifact@v4
        with:
          name: snapshot-${{ steps.date.outputs.date }}
          path: snapshot/latest.json
      - id: set
        run: echo "snapshot=snapshot/latest.json" >> $GITHUB_OUTPUT

  ingest:
    needs: snapshot
    strategy:
      matrix:
        shard: [

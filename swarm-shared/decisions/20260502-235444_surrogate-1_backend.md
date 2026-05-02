# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate limits (429) during ingestion, removes recursive `list_repo_tree` overhead, and ensures deterministic file selection across all 16 shards.

### Steps (1h 45m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Uses `huggingface_hub` to call `list_repo_tree(path, recursive=False)` per date folder.  
   - Outputs `snapshot/<date>/files.json` containing `{repo, path, size, sha, url}` for every parquet/jsonl file.  
   - Shebang `#!/usr/bin/env bash`, `chmod +x`.

2. **Add `bin/filter-manifest.py`** (20m)  
   - Lightweight helper to shard manifest deterministically by `hash(path) % TOTAL_SHARDS`.  
   - Accepts `--manifest`, `--shard-id`, `--total-shards`; outputs filtered CDN URLs.

3. **Update `bin/dataset-enrich.sh`** (25m)  
   - Accepts optional `SNAPSHOT_FILE`. If provided, uses `filter-manifest.py` to get exact slice; otherwise falls back to API (rate-limited).  
   - Downloads via CDN URL with `curl -L --retry 3`.  
   - Processes each file with `lib/dedup.py`.

4. **Add lightweight Python helper `lib/snapshot.py`** (20m)  
   - `generate_snapshot(repo_id, date_folder, out_path)` uses `HfApi.list_repo_tree` with `recursive=False`.  
   - Validates extensions (`.parquet`, `.jsonl`, `.json`).  
   - Emits stable JSON sorted by path for deterministic shard assignment.

5. **Update GitHub Actions `ingest.yml`** (25m)  
   - Add pre-step job `snapshot` that runs once per workflow.  
   - Produces `snapshot-<date>.json` as artifact; passes to all 16 matrix shards via `env.SNAPSHOT_FILE`.  
   - Each shard downloads artifact and uses it as input to `dataset-enrich.sh`.

6. **Add training script integration `train.py`** (15m)  
   - Reads `snapshot/<date>/files.json` and builds a `DataLoader` that fetches only CDN URLs (zero HF API calls during training).  
   - Includes retry/backoff for CDN 429 (separate limit).

7. **Validation & cleanup** (20m)  
   - Run `bin/snapshot.sh` locally (dry-run) to verify JSON shape.  
   - Simulate one shard with `SHARD_ID=0` and `TOTAL_SHARDS=16` to confirm deterministic assignment.  
   - Ensure no `load_dataset(streaming=True)` on heterogeneous schemas; use `hf_hub_download` per file from CDN URLs.

---

## Code Snippets

### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ID="${1:-axentx/surrogate-1-training-pairs}"
DATE_FOLDER="${2:-$(date +%Y-%m-%d)}"
OUT_DIR="${3:-snapshot}"
OUT_FILE="${OUT_DIR}/${DATE_FOLDER}/files.json"

mkdir -p "$(dirname "${OUT_FILE}")"

python3 -m axentx.surrogate1.lib.snapshot \
  --repo-id "${REPO_ID}" \
  --date-folder "${DATE_FOLDER}" \
  --out "${OUT_FILE}"

echo "Snapshot written to ${OUT_FILE}"
```

---

### `lib/snapshot.py`
```python
import json
import argparse
from pathlib import Path
from huggingface_hub import HfApi

HF_API = HfApi()

def generate_snapshot(repo_id: str, date_folder: str, out_path: Path):
    # Non-recursive to avoid pagination explosion
    tree = HF_API.list_repo_tree(repo_id, path=date_folder, recursive=False)
    files = []
    for item in tree:
        if not item.type == "file":
            continue
        if not item.path.lower().endswith((".parquet", ".jsonl", ".json")):
            continue
        files.append({
            "repo": repo_id,
            "path": item.path,
            "size": item.size,
            "sha": item.lfs.get("sha256", None) if item.lfs else None,
            "url": f"https://huggingface.co/datasets/{repo_id}/resolve/main/{item.path}"
        })

    # Deterministic ordering
    files.sort(key=lambda f: f["path"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(files, f, indent=2, sort_keys=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate CDN snapshot for dataset repo.")
    parser.add_argument("--repo-id", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date-folder", required=True, help="e.g. 2026-04-29")
    parser.add_argument("--out", required=True, type=Path, help="Output JSON path")
    args = parser.parse_args()
    generate_snapshot(args.repo_id, args.date_folder, args.out)
```

---

### `bin/filter-manifest.py`
```python
import json
import argparse
import hashlib
import sys

def shard_filter(manifest_path: str, shard_id: int, total_shards: int):
    with open(manifest_path) as f:
        files = json.load(f)
    for fobj in files:
        h = int(hashlib.sha256(fobj["path"].encode()).hexdigest(), 16)
        if h % total_shards == shard_id:
            print(fobj["url"])

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Filter manifest to shard URLs.")
    parser.add_argument("--manifest", required=True, help="Path to snapshot JSON")
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--total-shards", type=int, default=16)
    args = parser.parse_args()
    shard_filter(args.manifest, args.shard_id, args.total_shards)
```

---

### Updated `bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ID="axentx/surrogate-1-training-pairs"
DATE_FOLDER="${DATE_FOLDER:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"

if [[ -n "${SNAPSHOT_FILE}" && -f "${SNAPSHOT_FILE}" ]]; then
  echo "Using snapshot: ${SNAPSHOT_FILE}"
  mapfile -t FILES < <(
    python3 bin/filter-manifest.py \
      --manifest "${SNAPSHOT_FILE}" \
      --shard-id "${SHARD_ID}" \
      --total-shards "${TOTAL_SHARDS}"
  )
else
  echo "No snapshot provided; falling back to repo listing (rate-limited)."
  mapfile -t FILES < <(
    python3 -c "
from huggingface_hub import HfApi
api = HfApi()
tree = api.list_repo_tree('${REPO_ID}', path='${DATE_FOLDER}', recursive=False)
for item in tree:
    if item.type == 'file' and item.path.lower().endswith(('.parquet','.jsonl','.json')):
        print(f'https://huggingface.co/datasets/${REPO_ID}/resolve/main/{item.path}')
"
  )
fi

for url in "${FILES[@]}"; do
  outfile="enriched/$(basename "${url}")"
  echo "Downloading ${url} -> ${outfile}"
  curl -L --retry 3 --retry-delay 5 -o "${outfile}" "${url}"
  python3 lib/dedup.py "${outfile}"
done
```

---

### `.github/workflows/ingest.yml` (excerpt)
```yaml
name: ingest

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:

jobs

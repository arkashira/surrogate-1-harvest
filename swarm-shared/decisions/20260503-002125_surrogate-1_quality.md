# surrogate-1 / quality

## Final Implementation Plan  
**Goal:** Eliminate HF API rate-limits during training, avoid PyArrow CastError, keep ingestion deterministic, and require ≤2h to ship.

---

### 1) Highest-value improvement (≤2h)
Add `bin/snapshot.sh` that produces a **deterministic per-date manifest** and update ingestion/training to use **CDN-only fetches** when a manifest is present.  
- Removes recursive `list_repo_files`/`list_repo_tree` calls during training (eliminates 429s).  
- Avoids `load_dataset(streaming=True)` on heterogeneous repos (prevents CastError).  
- Keeps Mac as orchestrator-only; heavy data movement runs on GitHub Actions/HF Space/Lightning.  
- Enables reproducible training runs (same manifest → same data/sharding).

---

### 2) Concrete changes

#### A. Add `bin/snapshot.sh`
- Inputs: `REPO`, `DATE` (e.g. `2026-04-29`), optional `OUT_DIR`.  
- Single API call: `list_repo_tree(path=DATE, recursive=false)` (non-recursive to avoid pagination/429).  
- Output: `snapshots/{REPO_SLUG}/{DATE}/manifest.json`  
  ```json
  {
    "repo": "owner/repo",
    "date": "2026-04-29",
    "snapshot_ts": "2026-04-29T14:03:00Z",
    "files": ["2026-04-29/file1.parquet", "2026-04-29/file2.parquet"],
    "count": 2
  }
  ```
- Deterministic lexicographic ordering for reproducible sharding.  
- Commit snapshots to repo (or push to dataset repo via sibling-repo hashing respecting 128/hr HF API cap).

#### B. Update `bin/dataset-enrich.sh`
- Add optional `--snapshot snapshots/.../manifest.json`.  
- If provided, skip recursive listing; drive per-file ingestion via manifest.  
- Project to `{prompt,response}` at parse time; write to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

#### C. Add training-side support (`lib/manifest_loader.py`)
- Accept `--manifest snapshots/.../manifest.json`.  
- When manifest present, construct CDN URLs:  
  `https://huggingface.co/datasets/{repo}/resolve/main/{file_path}`  
- Stream via `requests` + `pyarrow.parquet` (or download-then-read) with `streaming=True`-like behavior per file.  
- No `load_dataset(..., streaming=True)` on repo root; download individual parquet files via CDN then parse.

#### D. GitHub Actions (optional polish)
- Add one-off `snapshot.yml` that runs `bin/snapshot.sh` for the latest date folder and commits snapshot to repo (or dataset repo).  
- Keep existing 16-shard matrix workflow unchanged; it can consume snapshots to avoid per-run listing.

---

### 3) Code snippets

#### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: HF_TOKEN=... bin/snapshot.sh <repo> <date> [out_dir]
# Example: HF_TOKEN=... bin/snapshot.sh axentx/surrogate-1-training-pairs 2026-04-29 snapshots
set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE="${2:-$(date +%Y-%m-%d)}"
OUT_DIR="${3:-snapshots}"
HF_TOKEN="${HF_TOKEN:-}"
API_ROOT="https://huggingface.co/api"

mkdir -p "${OUT_DIR}/${REPO}/${DATE}"

# Single non-recursive tree call to avoid pagination/429
echo "Listing ${REPO} tree for ${DATE} (non-recursive)..."
TREE_JSON=$(curl -sSf \
  -H "Authorization: Bearer ${HF_TOKEN}" \
  "${API_ROOT}/datasets/${REPO}/tree?path=${DATE}&recursive=false")

# Extract file paths (type "file") and sort deterministically
FILES=$(echo "$TREE_JSON" | python3 -c "
import sys, json
tree = json.load(sys.stdin)
paths = [item['path'] for item in tree if item.get('type') == 'file']
for p in sorted(paths):
    print(p)
")

if [ -z "$FILES" ]; then
  echo "No files found for ${DATE} in ${REPO}"
  exit 1
fi

SNAP_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
MANIFEST="${OUT_DIR}/${REPO}/${DATE}/manifest.json"

python3 -c "
import json, os
repo = os.environ['REPO']
date = os.environ['DATE']
files = os.environ['FILES'].splitlines()
manifest = {
    'repo': repo,
    'date': date,
    'snapshot_ts': os.environ['SNAP_TS'],
    'files': files,
    'count': len(files)
}
with open(os.environ['MANIFEST'], 'w') as f:
    json.dump(manifest, f, indent=2)
print(f'Wrote {len(files)} entries to {os.environ[\"MANIFEST\"]}')
" -- \
  REPO="$REPO" DATE="$DATE" FILES="$FILES" SNAP_TS="$SNAP_TS" MANIFEST="$MANIFEST"

echo "Snapshot created: ${MANIFEST}"
```

#### `lib/manifest_loader.py`
```python
# lib/manifest_loader.py
import json
import pyarrow.parquet as pq
import requests
from pathlib import Path
from typing import Iterator, Dict, Any

CDN_ROOT = "https://huggingface.co/datasets"

def load_manifest(manifest_path: str) -> Dict[str, Any]:
    with open(manifest_path) as f:
        return json.load(f)

def cdn_url(repo: str, file_path: str) -> str:
    return f"{CDN_ROOT}/{repo}/resolve/main/{file_path}"

def stream_from_manifest(
    manifest_path: str,
    columns=("prompt", "response"),
    tmp_dir: str = "/tmp"
) -> Iterator[Dict[str, Any]]:
    manifest = load_manifest(manifest_path)
    repo = manifest["repo"]
    tmp_path = Path(tmp_dir)
    tmp_path.mkdir(parents=True, exist_ok=True)

    for file_path in manifest["files"]:
        url = cdn_url(repo, file_path)
        local_path = tmp_path / Path(file_path).name
        if not local_path.exists():
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
        try:
            table = pq.read_table(local_path, columns=columns)
            for batch in table.to_batches(max_chunksize=1024):
                for row in zip(*[batch.column(col).to_pylist() for col in columns]):
                    yield dict(zip(columns, row))
        finally:
            if local_path.exists():
                local_path.unlink()
```

#### `lib/process_with_manifest.py` (thin wrapper for `dataset-enrich.sh`)
```python
# lib/process_with_manifest.py
import argparse
import json
from pathlib import Path
from lib.manifest_loader import stream_from_manifest

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--shard", type=int, default=0)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"shard{args.shard}.jsonl"

    with open(out_file, "w") as f:
        for record in stream_from_manifest(args.manifest):
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"Wrote {out_file}")

if __name__ == "__main__":
    main()
```

#### Update to `bin/dataset-enrich.sh` (excerpt)
```bash
MANIFEST=""
if [[ "${1:-}" == "--snapshot" ]];

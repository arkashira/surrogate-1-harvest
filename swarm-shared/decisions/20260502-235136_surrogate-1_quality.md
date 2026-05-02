# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit (429) during ingestion and reduces per-shard latency by avoiding recursive `list_repo_files` calls.

### Concrete Steps (1h 50m total)

1. **Create `bin/snapshot.sh`** (20 min)  
   - Uses `huggingface_hub` to call `list_repo_tree(path, recursive=False)` per date folder  
   - Outputs `snapshot-<date>.json` with deterministic ordering and CDN URLs (`resolve/main/...`)  
   - Validates JSON and exits non-zero on API failure  
   - Includes `ts` (ISO timestamp) and `repo` fields for traceability

2. **Update `bin/dataset-enrich.sh`** (30 min)  
   - Accepts snapshot file as optional argument; falls back to legacy behavior  
   - Each shard reads only its 1/16 slice of the snapshot (by `SHARD_ID`) using **deterministic hash-based assignment**: `hash(path) % 16 == SHARD_ID`  
   - Downloads via CDN URLs with `curl`/`wget` → zero API calls during stream  
   - Uses `lib/cdn_loader.py` for projection and validation

3. **Add `lib/cdn_loader.py`** (20 min)  
   - Lightweight parquet reader that takes CDN URLs directly  
   - Projects to `{prompt, response}` only; drops extra schema columns  
   - Emits normalized JSONL lines for dedup  
   - Includes robust error handling and logging

4. **Add `bin/embed-snapshot.py`** (20 min)  
   - Injects snapshot into training as a frozen constant  
   - Generates `train_filelist.py` with `FILELIST = [...]`  
   - Used by Lightning training to avoid any API calls during training

5. **Update GitHub Actions matrix** (10 min)  
   - Add step to generate snapshot before matrix expansion  
   - Pass snapshot artifact to all 16 shards via `needs`  
   - Ensure deterministic, disjoint file sets per shard

6. **Validation** (20 min)  
   - Dry-run snapshot on `axentx/surrogate-1-training-pairs`  
   - Verify CDN downloads succeed without `HF_TOKEN`  
   - Confirm shard assignment is deterministic and disjoint  
   - Ensure no `load_dataset(streaming=True)` or `list_repo_files` calls remain in hot path

---

## Code Snippets

### 1. `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Generate CDN snapshot for surrogate-1 dataset ingestion.
# Usage: HF_TOKEN=... ./bin/snapshot.sh <date> [output.json]

set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="${2:-snapshot-${DATE}.json}"

python3 - "$REPO" "$DATE" "$OUT" <<'PY'
import json, os, sys
from datetime import datetime, timezone
from huggingface_hub import HfApi

repo, date, out = sys.argv[1], sys.argv[2], sys.argv[3]
api = HfApi(token=os.getenv("HF_TOKEN"))

# List top-level date folders (non-recursive)
tree = api.list_repo_tree(repo, path="", recursive=False)
folders = [t for t in tree if t.type == "directory" and t.path.startswith(date)]

if not folders:
    print(f"No folders found for date {date}", file=sys.stderr)
    sys.exit(1)

entries = []
for f in folders:
    files = api.list_repo_tree(repo, path=f.path, recursive=False)
    for file in files:
        if file.type == "file" and file.path.endswith((".parquet", ".jsonl")):
            cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file.path}"
            entries.append({
                "path": file.path,
                "cdn_url": cdn_url,
                "size": getattr(file, "size", None)
            })

# Deterministic ordering
entries.sort(key=lambda x: x["path"])

snapshot = {
    "repo": repo,
    "date": date,
    "ts": datetime.now(timezone.utc).isoformat(),
    "files": entries
}

with open(out, "w") as fp:
    json.dump(snapshot, fp, indent=2)

print(f"Snapshot written to {out} ({len(entries)} files)")
PY
```

### 2. `lib/cdn_loader.py`
```python
# lib/cdn_loader.py
import pyarrow.parquet as pq
import pyarrow as pa
import json
import sys
import hashlib
from typing import Iterator, Dict

def project_to_pair(batch: pa.Table) -> Iterator[Dict[str, str]]:
    """Project batch to {prompt, response} only."""
    prompts = batch.column("prompt").to_pylist() if "prompt" in batch.column_names else [""] * len(batch)
    responses = batch.column("response").to_pylist() if "response" in batch.column_names else [""] * len(batch)
    for p, r in zip(prompts, responses):
        if p and r:
            yield {"prompt": str(p).strip(), "response": str(r).strip()}

def stream_cdn_parquet(cdn_url: str) -> Iterator[Dict[str, str]]:
    """Stream parquet from CDN URL and yield normalized pairs."""
    try:
        pf = pq.ParquetFile(cdn_url)
        for batch in pf.iter_batches(batch_size=1024):
            yield from project_to_pair(batch)
    except Exception as e:
        print(f"CDN load failed {cdn_url}: {e}", file=sys.stderr)

def deterministic_shard(path: str, total_shards: int) -> int:
    """Deterministic shard assignment using hash."""
    return int(hashlib.md5(path.encode()).hexdigest(), 16) % total_shards

if __name__ == "__main__":
    for url in sys.stdin:
        url = url.strip()
        if url:
            for pair in stream_cdn_parquet(url):
                print(json.dumps(pair, ensure_ascii=False))
```

### 3. Updated `bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Enhanced to accept snapshot for CDN-only ingestion.

set -euo pipefail

SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
SNAPSHOT="${1:-snapshot-$(date +%Y-%m-%d).json}"

if [[ ! -f "$SNAPSHOT" ]]; then
  echo "Snapshot $SNAPSHOT not found. Run bin/snapshot.sh first." >&2
  exit 1
fi

# Deterministic shard assignment using hash
mapfile -t FILES < <(
  python3 -c "
import json, sys, hashlib

def deterministic_shard(path, total_shards):
    return int(hashlib.md5(path.encode()).hexdigest(), 16) % total_shards

with open(sys.argv[1]) as f:
    data = json.load(f)

shard_id = int(sys.argv[2])
total_shards = int(sys.argv[3])

for entry in data['files']:
    if deterministic_shard(entry['path'], total_shards) == shard_id:
        print(entry['cdn_url'])
" "$SNAPSHOT" "$SHARD_ID" "$TOTAL_SHARDS"
)

echo "Shard $SHARD_ID processing ${#FILES[@]} files via CDN"

for url in "${FILES[@]}"; do
  python3 lib/cdn_loader.py <<<"$url" | while read -r line; do
    # Existing dedup/upload logic unchanged
    echo "$line"
  done
done
```

### 4. `bin/embed-snapshot.py`
```python
#!/usr/bin/env python3
# bin/embed-snapshot.py
# Inject snapshot into training as a frozen constant.
# Usage: ./bin/embed-snapshot.py snapshot-<date>.json > train_filelist.py

import json
import sys

def main():
    if len(sys.argv) != 2:
        print("Usage: ./bin/embed-snapshot.py <snapshot.json>",

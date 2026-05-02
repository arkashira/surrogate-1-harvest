# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit pressure during ingestion and ensures deterministic file lists across all 16 shards.

### Why this now
- Current runners call `list_repo_files`/`load_dataset` per shard → 16× API pressure → 429 risk.
- Public CDN URLs bypass auth/rate limits entirely.
- A single deterministic snapshot lets every shard know exactly which files to process without coordination.

### Concrete steps (≤2h)

1. **Create `bin/snapshot.sh`** (15 min)  
   - Uses `huggingface_hub` to `list_repo_tree` (non-recursive per date folder) once.  
   - Outputs `snapshot/<date>/files.json` with `{ "path": "...", "sha": "...", "size": ... }`.  
   - Exits non-zero if API fails (so cron/GHA fails fast).

2. **Update `bin/dataset-enrich.sh`** (30 min)  
   - Accept optional `SNAPSHOT_FILE` env var.  
   - If present, read file list from JSON instead of listing repo.  
   - Compute deterministic shard assignment: `hash(slug) % 16 == SHARD_ID`.  
   - Download via CDN: `curl -L "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/${path}"`.  
   - Keep existing schema normalization + dedup via `lib/dedup.py`.

3. **Update `.github/workflows/ingest.yml`** (15 min)  
   - Add a pre-step that runs `bin/snapshot.sh` once (not in matrix).  
   - Pass snapshot path to each matrix job via `env.SNAPSHOT_FILE`.  
   - Keep 16-shard matrix unchanged.

4. **Add lightweight Python helper `lib/snapshot.py`** (20 min)  
   - `generate_snapshot(repo, date_folder) -> list[dict]`.  
   - `filter_by_shard(files, shard_id, total_shards=16) -> list[dict]`.  
   - Used by both snapshot generator and enrich script.

5. **Validation & hardening** (20 min)  
   - Ensure shebangs `#!/usr/bin/env bash`, `chmod +x` on new scripts.  
   - Add `set -euo pipefail`.  
   - Retry CDN downloads with exponential backoff (max 3 tries).  
   - If CDN fetch fails, fall back to authenticated `hf_hub_download` (but log warning).

6. **Quick test** (20 min)  
   - Run snapshot locally, verify JSON.  
   - Run one shard manually with snapshot, confirm CDN downloads and output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

---

### Code snippets

#### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT_DIR="snapshot/${DATE}"
OUT_FILE="${OUT_DIR}/files.json"

mkdir -p "${OUT_DIR}"

python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi

repo = os.environ.get("REPO", "$REPO")
date = os.environ.get("DATE", "$DATE")
out = os.environ.get("OUT_FILE", "$OUT_FILE")

api = HfApi()
# List top-level date folder only (non-recursive)
tree = api.list_repo_tree(repo=repo, path=date, recursive=False)

files = []
for item in tree:
    if item.type == "file":
        files.append({
            "path": f"{date}/{item.path}",
            "sha": getattr(item, "sha", None),
            "size": getattr(item, "size", None),
        })

with open(out, "w") as f:
    json.dump(files, f, indent=2)

print(f"Snapshot written: {out} ({len(files)} files)")
PY
```

#### `lib/snapshot.py`
```python
import json
import hashlib
from typing import List, Dict

def load_snapshot(path: str) -> List[Dict]:
    with open(path) as f:
        return json.load(f)

def shard_for_file(file_entry: Dict, total_shards: int = 16) -> int:
    slug = file_entry["path"]
    h = hashlib.sha256(slug.encode()).hexdigest()
    return int(h, 16) % total_shards

def filter_by_shard(files: List[Dict], shard_id: int, total_shards: int = 16) -> List[Dict]:
    return [f for f in files if shard_for_file(f, total_shards) == shard_id]
```

#### Updated `bin/dataset-enrich.sh` (key excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

SHARD_ID="${SHARD_ID:?required}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"
WORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${WORK_DIR}"

if [[ -n "${SNAPSHOT_FILE}" && -f "${SNAPSHOT_FILE}" ]]; then
  echo "Using snapshot: ${SNAPSHOT_FILE}"
  mapfile -t FILES < <(python3 -c "
import sys, json
from lib.snapshot import load_snapshot, filter_by_shard
files = filter_by_shard(load_snapshot('${SNAPSHOT_FILE}'), ${SHARD_ID})
for f in files:
    print(f['path'])
")
else
  echo "WARNING: No snapshot provided; falling back to repo listing (may hit API limits)"
  # fallback to existing listing logic (kept for compatibility)
  # ...
fi

for rel_path in "${FILES[@]}"; do
  url="https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/${rel_path}"
  outfile=$(mktemp)
  if curl -L --fail --retry 3 --retry-delay 2 -o "${outfile}" "${url}"; then
    # existing schema normalization + dedup
    python3 -c "
import sys, json, pyarrow as pa, pyarrow.parquet as pq
from lib.dedup import is_duplicate, store_hash
outfile = sys.argv[1]
# ... project to {prompt, response}, compute md5, dedup, emit jsonl
" "${outfile}"
    rm -f "${outfile}"
  else
    echo "CDN download failed: ${url}" >&2
    rm -f "${outfile}"
  fi
done
```

#### `.github/workflows/ingest.yml` (key excerpt)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-path: ${{ steps.set.outputs.snapshot_path }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: bin/snapshot.sh
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
      - id: set
        run: |
          echo "snapshot_path=snapshot/$(date +%Y-%m-%d)/files.json" >> $GITHUB_OUTPUT

  ingest:
    needs: snapshot
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: bin/dataset-enrich.sh
        env:
          SHARD_ID: ${{ matrix.shard_id }}
          SNAPSHOT_FILE: ${{ needs.snapshot.outputs.snapshot-path }}
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
```

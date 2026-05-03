# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing — eliminates HF API rate limits during ingestion.

### Concrete steps

1. **Create `bin/snapshot.sh`**  
   - Uses `huggingface_hub` to call `list_repo_tree(path, recursive=False)` for a single date folder (e.g., `public-merged/2026-05-02/`)  
   - Outputs `snapshot-<date>.json` with `{"date":"...","files":["path1","path2",...],"generated_at":"ISO8601"}`  
   - Exits non-zero on API failure so CI can retry after 360s backoff

2. **Update `bin/dataset-enrich.sh`**  
   - Accept optional snapshot file path (`SNAPSHOT_PATH`)  
   - If provided, read file list from snapshot instead of calling `list_repo_files` recursively  
   - Keep deterministic shard assignment: `hash(slug) % 16 == SHARD_ID`

3. **Update GitHub Actions matrix (`ingest.yml`)**  
   - Add a pre-job step that runs `bin/snapshot.sh` once, uploads artifact `snapshot-<date>.json`  
   - Pass snapshot path to each shard via `env.SNAPSHOT_PATH`  
   - Retain existing 16-shard parallelism; each shard now uses CDN-only fetches

4. **Add small Python helper (`lib/snapshot.py`)**  
   - Thin wrapper around `list_repo_tree` + JSON serialization  
   - Used by both `snapshot.sh` and `dataset-enrich.sh` for consistency

5. **Validation**  
   - Dry-run locally with a test repo to confirm zero API calls during shard processing  
   - Confirm shard outputs remain identical (only source changes)

---

### Code snippets

#### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_DATASET_REPO:-axentx/surrogate-1-training-pairs}"
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="${2:-./snapshots}"
OUTFILE="${OUTDIR}/snapshot-${DATE}.json"

mkdir -p "${OUTDIR}"

python3 - "${REPO}" "${DATE}" "${OUTFILE}" <<'PY'
import json, sys, os
from huggingface_hub import list_repo_tree

repo = sys.argv[1]
date = sys.argv[2]
outfile = sys.argv[3]

path = f"public-merged/{date}"
try:
    tree = list_repo_tree(repo=repo, path=path, recursive=False)
    files = [f.rfilename for f in tree if f.type == "file"]
except Exception as e:
    # HF API 429: caller should backoff 360s
    sys.stderr.write(f"HF API error: {e}\n")
    sys.exit(1)

payload = {
    "repo": repo,
    "date": date,
    "path": path,
    "files": sorted(files),
    "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z"
}

with open(outfile, "w") as f:
    json.dump(payload, f, indent=2)
print(f"Snapshot written: {outfile} ({len(files)} files)")
PY

echo "Snapshot ready: ${OUTFILE}"
```

#### `lib/snapshot.py` (optional thin wrapper)
```python
#!/usr/bin/env python3
import json, sys
from huggingface_hub import list_repo_tree

def snapshot(repo: str, date: str, outfile: str):
    tree = list_repo_tree(repo=repo, path=f"public-merged/{date}", recursive=False)
    files = [f.rfilename for f in tree if f.type == "file"]
    payload = {
        "repo": repo,
        "date": date,
        "files": sorted(files),
        "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }
    with open(outfile, "w") as f:
        json.dump(payload, f, indent=2)
    return payload

if __name__ == "__main__":
    repo, date, outfile = sys.argv[1], sys.argv[2], sys.argv[3]
    snapshot(repo, date, outfile)
```

#### Updated `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
SNAPSHOT_PATH="${SNAPSHOT_PATH:-}"

select_files() {
    local date="$1"
    if [[ -n "${SNAPSHOT_PATH}" && -f "${SNAPSHOT_PATH}" ]]; then
        # CDN-only mode: use snapshot file list
        python3 -c "
import json, sys, hashlib
with open(sys.argv[1]) as f:
    data = json.load(f)
for fn in data['files']:
    slug = fn.rsplit('/', 1)[-1].rsplit('.', 1)[0]
    if hash(slug) % ${TOTAL_SHARDS} == ${SHARD_ID}:
        print(fn)
" "${SNAPSHOT_PATH}"
    else
        # fallback: recursive list (rate-limited)
        python3 -c "
from huggingface_hub import list_repo_files
import sys, hashlib
for f in list_repo_files(sys.argv[1], repo_type='dataset'):
    if f.startswith('public-merged/${date}/'):
        slug = f.rsplit('/', 1)[-1].rsplit('.', 1)[0]
        if hash(slug) % ${TOTAL_SHARDS} == ${TOTAL_SHARDS}:
            print(f)
" "${REPO}"
    fi
}

# downstream usage:
# for file in $(select_files "2026-05-02"); do
#   curl -L "https://huggingface.co/datasets/${REPO}/resolve/main/${file}" -o "${file##*/}"
#   ... process ...
# done
```

#### `.github/workflows/ingest.yml` (excerpt additions)
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
          DATE=$(date +%Y-%m-%d)
          python bin/snapshot.py axentx/surrogate-1-training-pairs "$DATE" snapshots/snapshot-"$DATE".json
          echo "path=snapshots/snapshot-$DATE.json" >> "$GITHUB_OUTPUT"
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}

  ingest:
    needs: snapshot
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: snapshot-artifact
          path: snapshots/
      - run: bin/dataset-enrich.sh
        env:
          SHARD_ID: ${{ matrix.shard }}
          TOTAL_SHARDS: 16
          SNAPSHOT_PATH: ${{ needs.snapshot.outputs.snapshot-path }}
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
```

---

### Notes & trade-offs
- **Zero API during shard processing**: each shard uses `curl -L` on CDN URLs (`/resolve/main/...`) — no Authorization header, bypasses `/api/` rate limits.
- **Single API call per run**: `snapshot.sh` runs once per cron/workflow; if 429 occurs, workflow can retry after 360s.
- **Deterministic sharding preserved**: `hash(slug) % 16` unchanged; snapshot only changes file enumeration source.
- **Backward compatible**: if snapshot missing/unavailable, falls back to recursive `list_repo_files` (existing

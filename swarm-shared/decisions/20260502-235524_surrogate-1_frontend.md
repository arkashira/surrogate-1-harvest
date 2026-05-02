# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a deterministic pre-flight snapshot generator (`bin/snapshot.sh`) that lists dataset files once per date folder, emits a CDN-ready manifest, and wires it into the 16-shard runner so every shard uses CDN-only fetches (zero HF API calls during processing). This eliminates rate-limit risk and removes the 429/1000-per-5min ceiling for parallel workers.

### Steps (concrete)

1. **Add `bin/snapshot.sh`**
   - Accepts optional `DATE` (YYYY-MM-DD) or defaults to today.
   - Uses `huggingface_hub` to call `list_repo_tree(path="public-merged/<DATE>", recursive=True)` once.
   - Filters to `.jsonl`/`.parquet` files.
   - Emits `snapshots/<DATE>.json` with CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) and local paths.
   - Exits non-zero on API errors (so cron fails fast).

2. **Update `bin/dataset-enrich.sh`**
   - Accept `SNAPSHOT_FILE` env var (path to the JSON manifest).
   - If provided, read CDN URLs from the file and stream them directly (bypass `load_dataset`/`list_repo_files`).
   - If not provided, keep current behavior (backwards-compatible).
   - Deterministic shard assignment: `hash(slug) % 16 == SHARD_ID` (same logic as before) for consistent routing.
   - Downloads via CDN URLs with `curl` (no Authorization header needed).

3. **Update GitHub Actions workflow (`ingest.yml`)**
   - Add a pre-step job that runs `bin/snapshot.sh` once and uploads the manifest as an artifact.
   - All 16 shard jobs download the artifact and set `SNAPSHOT_FILE`.
   - Keeps the 30-minute cadence unchanged.

4. **Minor hardening**
   - Ensure `SHELL=/bin/bash` in any cron/crontab context.
   - Make scripts executable (`chmod +x bin/*.sh`).

---

## Code Snippets

### 1) `bin/snapshot.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# Usage: bin/snapshot.sh [YYYY-MM-DD]
# Requires: huggingface_hub (pip), HF_TOKEN (read-only is fine for public repos)
# Emits: snapshots/<DATE>.json

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="snapshots"
OUTFILE="${OUTDIR}/${DATE}.json"

mkdir -p "${OUTDIR}"

python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi

repo = os.environ.get("REPO", "${REPO}")
date = "${DATE}"
path = f"public-merged/{date}"
api = HfApi()

try:
    # Single API call: recursive tree listing for the date folder
    tree = api.list_repo_tree(repo=repo, path=path, recursive=True)
except Exception as e:
    print(f"ERROR listing repo tree: {e}", file=sys.stderr)
    sys.exit(1)

files = []
for item in tree:
    if item.type != "file":
        continue
    # Only include .jsonl and .parquet files
    if not (item.path.endswith(".jsonl") or item.path.endswith(".parquet")):
        continue
    # CDN URL (no Authorization header required)
    cdn = f"https://huggingface.co/datasets/{repo}/resolve/main/{item.path}"
    files.append({
        "path": item.path,
        "cdn": cdn,
        "size": getattr(item, "size", None)
    })

out = os.path.join(os.environ.get("OUTDIR", "${OUTDIR}"), "${DATE}.json")
with open(out, "w") as f:
    json.dump({"date": date, "root": path, "files": files}, f, indent=2)

print(f"Wrote {len(files)} files to {out}")
PY

echo "Snapshot created: ${OUTFILE}"
```

---

### 2) `bin/dataset-enrich.sh` (excerpt — integrate CDN mode)

```bash
#!/usr/bin/env bash
set -euo pipefail

# Existing behavior preserved when SNAPSHOT_FILE is unset.
# When SNAPSHOT_FILE is set, stream CDN URLs directly (zero HF API calls).

SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"

stream_from_cdn() {
  local snapshot="$1"
  python3 - <<PY
import json, sys, os

shard_id = int(os.environ.get("SHARD_ID", "0"))
total_shards = int(os.environ.get("TOTAL_SHARDS", "16"))

with open("$snapshot") as f:
    data = json.load(f)

files = data.get("files", [])
# Deterministic shard assignment by slug hash (consistent with existing logic)
assigned = []
for f in files:
    slug = os.path.basename(f["path"])
    if hash(slug) % total_shards == shard_id:
        assigned.append(f)

for f in assigned:
    print(f["cdn"])
PY
}

process_one_cdn_url() {
  local url="$1"
  # Download via CDN (no auth) and parse to {prompt,response}
  # Keep existing per-schema normalization logic here.
  curl -fsSL "$url" -o /tmp/record.$$.jsonl
  # ... existing normalization + dedup via lib/dedup.py ...
  # Output normalized JSONL lines to stdout
  # rm /tmp/record.$$.jsonl
}

main() {
  if [[ -n "${SNAPSHOT_FILE}" && -f "${SNAPSHOT_FILE}" ]]; then
    echo "Using CDN snapshot: ${SNAPSHOT_FILE}"
    while IFS= read -r url; do
      [[ -z "$url" ]] && continue
      process_one_cdn_url "$url"
    done < <(stream_from_cdn "${SNAPSHOT_FILE}")
  else
    echo "No snapshot provided — using legacy HF API path (may hit rate limits)"
    # ... existing dataset-enrich logic (streaming load_dataset etc) ...
  fi
}

main "$@"
```

---

### 3) `.github/workflows/ingest.yml` (minimal diff)

```yaml
# Add before the 16-shard matrix or as a separate job that produces artifact
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      date: ${{ steps.date.outputs.date }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install huggingface_hub
      - run: mkdir -p snapshots
      - run: bin/snapshot.sh ${{ github.event.inputs.date || '' }}
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
      - name: Upload snapshot
        uses: actions/upload-artifact@v4
        with:
          name: snapshot-manifest
          path: snapshots/

  ingest-shard:
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
      - uses: actions/download-artifact@v4
        with:
          name: snapshot-manifest
          path: snapshots/
      - name: Run shard
        env:
          SHARD_ID: ${{ matrix.shard_id }}
          TOTAL_SHARDS: 16
          SNAPSHOT_FILE: snapshots/${{ needs.snapshot.outputs.date }}.json
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: bin/dataset-enrich.sh
```

---

## Acceptance Criteria

- `bin/snapshot.sh` runs in <30s and produces valid `snapshots/<DATE>.json`.
- 16 parallel shards process only CDN URLs (no `list_repo_files`

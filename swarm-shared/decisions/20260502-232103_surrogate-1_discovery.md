# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Goal**: Eliminate runtime `load_dataset(streaming=True)` and recursive `list_repo_files` from `bin/dataset-enrich.sh`. Replace with a deterministic, pre-flight snapshot (JSON) and CDN-only fetches.  
Scope: single date folder per run; zero Hugging Face API calls during data loading; stable shard assignment; backward-compatible fallback.

### Chosen approach (merged + resolved)
- Use **non-recursive** `list_repo_tree(..., recursive=False)` to avoid pagination/rate-limit.
- Produce **deterministic, sorted JSON snapshots** with CDN URLs and sizes.
- **Commit snapshots into repo** (`snapshots/`) so Actions runners consume them without tokens or API calls.
- Keep a **lightweight fallback** to legacy streaming for compatibility.
- Add **Studio reuse snippet** to avoid wasting 80h/mo quota.

---

## Steps (est. 1h45m)

1. Add snapshot generator (`bin/make-snapshot.sh` + `tools/snapshot.py`)  
   - Runs on Mac or any machine with `huggingface_hub` + token.  
   - Non-recursive tree listing per date folder.  
   - Emits `snapshots/file-list-<date>-<HHMMSS>.json` with `{path, cdn_url, size, sha}`.  
   - Deterministic sort by path.  
   - Exits non-zero if rate-limited; prints `Retry-After`.

2. Update `bin/dataset-enrich.sh`  
   - Accept optional `SNAPSHOT_FILE` (default: latest in `snapshots/`).  
   - If snapshot provided:  
     - Parse JSON (prefer `jq`; fallback to Python).  
     - Deterministic shard assignment: `hash(slug) % 16 == SHARD_ID`.  
     - Download via `curl --retry 3 --retry-delay 5 --fail`.  
     - Parse with `pyarrow`, project `["prompt","response"]` only.  
     - Stream rows into `lib/dedup.py` unchanged.  
   - If no snapshot: fall back to legacy `load_dataset(streaming=True)` path.

3. Add `tools/snapshot.py` helper  
   - `snapshot(date_folder) -> list[dict]` with keys `path`, `cdn_url`, `size`, `sha`.  
   - Deterministic sort by path for stable shard assignment.

4. Update GitHub Actions (`ingest.yml`)  
   - Separate workflow or manual step to generate and commit snapshot (requires HF token once).  
   - Ingestion matrix jobs fetch snapshot from repo (no token/API needed).  
   - Optionally cache snapshot as artifact for faster runs.

5. Add Studio reuse snippet (non-blocking)  
   - Small Python utility to list running Spaces and reuse to avoid quota waste.

6. Validation  
   - Dry-run `bin/make-snapshot.sh` for a recent date folder.  
   - Run one shard locally with snapshot; confirm CDN-only, zero `datasets` API calls.  
   - Confirm output schema `{prompt, response}` and dedup behavior unchanged.

---

## Code Snippets

### 1. `tools/snapshot.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file-list snapshot for a date folder in
datasets/axentx/surrogate-1-training-pairs.
Uses non-recursive tree listing to avoid pagination/rate-limit.
"""
import json
import os
import sys
from pathlib import Path

from huggingface_hub import HfApi

REPO = "datasets/axentx/surrogate-1-training-pairs"


def snapshot(date_folder: str, api: HfApi | None = None) -> list[dict]:
    api = api or HfApi()
    entries = api.list_repo_tree(repo_id=REPO, path=date_folder, recursive=False)
    files = []
    for e in entries:
        if e.type != "file":
            continue
        path = f"{date_folder}/{e.path}"
        files.append(
            {
                "path": path,
                "cdn_url": f"https://huggingface.co/{REPO}/resolve/main/{path}",
                "size": e.size or 0,
                "sha": getattr(e, "oid", None) or getattr(e, "sha", None) or "",
            }
        )
    files.sort(key=lambda x: x["path"])
    return files


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: snapshot.py <date_folder> <output.json>")
        sys.exit(1)
    date_folder, out_path = sys.argv[1], Path(sys.argv[2])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    files = snapshot(date_folder)
    out_path.write_text(
        json.dumps({"date_folder": date_folder, "files": files}, indent=2)
    )
    print(f"Wrote {len(files)} files to {out_path}")


if __name__ == "__main__":
    main()
```

---

### 2. `bin/make-snapshot.sh`

```bash
#!/usr/bin/env bash
# Generate deterministic snapshot for a date folder.
# Usage:
#   HF_TOKEN=<token> ./make-snapshot.sh 2026-05-01 ./snapshots/file-list-2026-05-01-$(date +%H%M%S).json

set -euo pipefail

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "ERROR: HF_TOKEN is required" >&2
  exit 1
fi

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <date_folder> <output.json>" >&2
  exit 1
fi

DATE_FOLDER=$1
OUT=$2

export HUGGING_FACE_HUB_TOKEN="$HF_TOKEN"

mkdir -p "$(dirname "$OUT")"

python3 tools/snapshot.py "$DATE_FOLDER" "$OUT"
```

```bash
chmod +x bin/make-snapshot.sh
```

---

### 3. Updated `bin/dataset-enrich.sh` (key changes)

```bash
#!/usr/bin/env bash
# ... existing header ...

set -euo pipefail

SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"
# If empty, try to use latest snapshot in snapshots/
if [[ -z "$SNAPSHOT_FILE" && -d snapshots ]]; then
  latest=$(ls -1 snapshots/file-list-*.json 2>/dev/null | sort -V | tail -n1 || true)
  if [[ -n "$latest" ]]; then
    SNAPSHOT_FILE="$latest"
  fi
fi

# ... existing dedup/env setup ...

process_with_snapshot() {
  local snapshot_file=$1
  local shard_id=$2
  local out_dir=$3

  if [[ ! -f "$snapshot_file" ]]; then
    echo "ERROR: snapshot file not found: $snapshot_file" >&2
    exit 1
  fi

  # Read CDN URLs from snapshot
  local files
  if command -v jq >/dev/null 2>&1; then
    mapfile -t files < <(jq -r '.files[].cdn_url' "$snapshot_file")
  else
    mapfile -t files < <(
      python3 -c "import json,sys;print('\n'.join([f['cdn_url'] for f in json.load(sys.stdin)['files']]))" < "$snapshot_file"
    )
  fi

  local total=${#files[@]}
  local idx=0
  for url in "${files[@]}"; do
    idx=$((idx + 1))
    local fname
    fname=$(basename "$url")
    # Deterministic shard by filename slug
    local slug_hash
    slug_hash=$(echo -n "$fname" | md5sum | awk '{print $1}')
    local file_shard=$((0x${slug_hash:0:8} % 16))
    if [[ $file_shard -ne $shard_id ]]; then
      continue
    fi

    echo "[$idx/$total] Shard $shard_id processing $fname"
    local tmpfile
    tmpfile=$(mktemp)
    curl --retry 3 --retry-delay 5 --fail --silent --show-error --location --output "$tmpfile" "$url"

    # Project to {prompt,response} only; stream into dedup
    python3 - "$tmpfile" "$out_dir" <<'PY'
import sys, pyarrow.parquet as pq
from pathlib import Path

tmpfile, out_dir =

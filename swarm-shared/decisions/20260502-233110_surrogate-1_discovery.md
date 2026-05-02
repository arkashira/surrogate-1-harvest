# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value change**: Replace runtime `load_dataset(streaming=True)` + recursive `list_repo_tree` in `bin/dataset-enrich.sh` with a **deterministic pre-flight snapshot + CDN-only fetches**. This eliminates HF API rate limits (429), prevents `pyarrow` `CastError` on mixed schemas, and reduces per-shard memory pressure while preserving 16-shard parallelism.

---

### Steps (1h 45m total)

1. **Add snapshot generator** (`bin/make-snapshot.py`) — run once per date folder from Mac (or a single orchestrator job). Uses `list_repo_tree(recursive=False)` per folder → single API call → writes `snapshot-{date}.json` with `{path, size, sha}`. (20m)  
2. **Update `bin/dataset-enrich.sh`** — accept snapshot file as optional arg (`SNAPSHOT_FILE`). If provided, read paths from snapshot and download via CDN (`/resolve/main/...`). If not, keep current behavior (fallback). (30m)  
3. **Deterministic shard assignment** — `hash(slug) % 16 == SHARD_ID`. Snapshot ensures all runners see identical file list and avoid duplicates across shards within the same run. (10m)  
4. **Keep dedup guard** — leave `lib/dedup.py` unchanged (central md5 store). Snapshot reduces wasted uploads but doesn’t replace cross-run dedup. (10m)  
5. **Update workflow** — add optional step before matrix to generate snapshot (only on `workflow_dispatch` or a single lightweight job) and pass it to each matrix job via `env.SNAPSHOT_FILE`. (20m)  
6. **Test locally** — run one shard against a small date folder using snapshot + CDN; verify schema projection `{prompt, response}` and no HF API calls during data load. (15m)

---

### Code Snippets

#### 1) Snapshot generator (`bin/make-snapshot.py`)

```python
#!/usr/bin/env python3
"""
Generate a deterministic snapshot for a date folder in
axentx/surrogate-1-training-pairs.

Usage:
  HF_TOKEN=<token> python bin/make-snapshot.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-02 \
    --out snapshot-2026-05-02.json
"""

import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

HF_API = HfApi(token=os.getenv("HF_TOKEN"))

def list_date_folder(repo: str, date: str):
    """
    Single API call: list top-level folder contents non-recursively.
    Assumes layout: <date>/<slug>.parquet  (or other extensions).
    """
    prefix = f"{date}/"
    entries = HF_API.list_repo_tree(
        repo=repo,
        path=prefix,
        recursive=False,
    )
    # entries may include nested folders if any; filter to files only
    files = [e for e in entries if e.type == "file"]
    return files

def build_snapshot(repo: str, date: str):
    files = list_date_folder(repo, date)
    snapshot = {
        "repo": repo,
        "date": date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": [
            {
                "path": f.rfilename,  # relative path from repo root
                "size": f.size,
                "sha": getattr(f, "sha", None),
            }
            for f in files
        ],
    }
    return snapshot

def main():
    parser = argparse.ArgumentParser(description="Create CDN snapshot for date folder")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    if not os.getenv("HF_TOKEN"):
        print("error: HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    snapshot = build_snapshot(args.repo, args.date)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2)
    print(f"wrote {len(snapshot['files'])} files -> {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/make-snapshot.py
```

---

#### 2) Updated `bin/dataset-enrich.sh` (excerpt)

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Updated: prefer snapshot + CDN-only fetches to avoid HF API rate limits.

set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
HF_TOKEN="${HF_TOKEN:-}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"
WORK_DIR="$(mktemp -d)"
OUT_DIR="batches/public-merged/$(date +%F)"
mkdir -p "${OUT_DIR}"

cleanup() {
  rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

# ---- helpers ----

slug_hash() {
  # deterministic 0..65535 from slug
  local slug="$1"
  python3 -c "import hashlib; print(int(hashlib.sha256('$slug'.encode()).hexdigest(), 16) % 65536)"
}

assign_shard() {
  local slug="$1"
  local h
  h="$(slug_hash "$slug")"
  echo $(( h % TOTAL_SHARDS ))
}

project_pair() {
  local src="$1"
  # Project heterogeneous files to {prompt,response} only.
  # Keep existing per-format logic; do NOT add source/ts columns.
  python3 -c "
import sys, json, pyarrow.parquet as pq, os
try:
    tbl = pq.read_table('$src')
except Exception:
    sys.exit(0)
cols = set(tbl.column_names)
if 'prompt' in cols and 'response' in cols:
    for b in tbl.to_batches():
        d = b.to_pydict()
        for i in range(len(d['prompt'])):
            print(json.dumps({'prompt': d['prompt'][i], 'response': d['response'][i]}))
"
}

# ---- path resolution ----

resolve_paths() {
  if [[ -n "$SNAPSHOT_FILE" && -f "$SNAPSHOT_FILE" ]]; then
    # Use snapshot (CDN-only mode)
    python3 -c "
import json, sys
with open('$SNAPSHOT_FILE') as f:
    snap = json.load(f)
for fobj in snap['files']:
    print(fobj['path'])
"
  else
    # Fallback: list repo tree once (non-recursive per top-level)
    # Caller should avoid recursive list_repo_files on big repos.
    if [[ -z "$HF_TOKEN" ]]; then
      echo "error: HF_TOKEN required for fallback mode" >&2
      exit 1
    fi
    python3 -c "
from huggingface_hub import HfApi
import os
api = HfApi(token=os.getenv('HF_TOKEN'))
entries = api.list_repo_tree(repo='$REPO', path='.', recursive=False)
for e in entries:
    if e.type == 'file':
        print(e.rfilename)
"
  fi
}

# ---- main ----

resolve_paths | while IFS= read -r path; do
  [[ -z "$path" ]] && continue

  # Determine slug from filename (basename without extension)
  slug="$(basename "${path%.*}")"
  shard="$(assign_shard "$slug")"
  if [[ "$shard" != "$SHARD_ID" ]]; then
    continue
  fi

  # Download via CDN (no Authorization header -> bypasses API rate limits)
  url="https://huggingface.co/datasets/${REPO}/resolve/main/${path}"
  dest="${WORK_DIR}/$(basename "$path")"
  if curl -fsSL --retry 3 --retry-delay 5 -o "$dest" "$url"; then
    project_pair "$dest" >> "${OUT_DIR}/shard

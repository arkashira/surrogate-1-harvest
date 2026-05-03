# surrogate-1 / discovery

## Final Implementation Plan — Pre-flight snapshot generator for surrogate-1

**Single highest-value improvement**: Add a deterministic pre-flight manifest (`bin/snapshot.sh`) that lists dataset files **once per date folder** and emits a CDN-only file manifest so training can fetch shards with **zero HF API calls**, eliminating 429 risk and enabling reproducible shard selection.

**Why this now**
- Removes `list_repo_tree`/`load_dataset` API hammer during training.
- Fits ≤2h: one new script + small, backward-compatible changes to `dataset-enrich.sh`.
- Reuses existing cron/workflow model; no infra changes.

---

### Changes (merged + reconciled)

1. **Add `bin/hf_list_tree.py`** (canonical, testable helper)  
   Thin wrapper around `huggingface_hub.list_repo_tree` used by `snapshot.sh`. Keeps Bash simple and enables unit tests.

2. **Add `bin/snapshot.sh`**  
   - Inputs: `REPO_OWNER`, `REPO_NAME`, `DATE` (e.g. `2026-05-03`), optional `OUT_JSON`.  
   - Single API call per date folder (`recursive=False`).  
   - Emits deterministic JSON manifest:
     - `repo`, `date`, `generated_at_utc`, `count`, `files[]` sorted by path, `sha256_manifest`.  
   - Validation: non-empty, sorted, no duplicate paths, CDN URLs reachable (optional curl check).  
   - Exits non-zero on API failure so CI can retry with backoff.

3. **Update `bin/dataset-enrich.sh`** (backward-compatible)  
   - Accept optional `MANIFEST_FILE`.  
   - If present and valid, workers read CDN URLs from manifest instead of calling HF API.  
   - Fallback to current listing behavior when absent.

4. **(Optional) Training script guidance**  
   - Read `MANIFEST_FILE` from env.  
   - Stream from CDN URLs directly (e.g. `requests`/`aiohttp` or `hf_hub_download` with `repo_type="dataset"` and `repo_id` + `filename`).  
   - No `load_dataset` or `list_repo_files` during training.

---

### Implementation details

#### `bin/hf_list_tree.py`
```python
#!/usr/bin/env python3
"""
List top-level files in a repo path (non-recursive) using huggingface_hub.
Usage:
  HF_TOKEN=... ./bin/hf_list_tree.py <owner> <repo> <path>

Outputs JSON list of objects:
  [{"path": "...", "size": ...}, ...]
"""
import json
import os
import sys
from huggingface_hub import HfApi

def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: hf_list_tree.py <owner> <repo> <path>", file=sys.stderr)
        sys.exit(1)
    owner, repo, path = sys.argv[1], sys.argv[2], sys.argv[3]
    api = HfApi(token=os.environ.get("HF_TOKEN"))
    entries = api.list_repo_tree(
        repo=f"{owner}/{repo}",
        path=path,
        repo_type="dataset",
        recursive=False,
    )
    files = []
    seen = set()
    for e in entries:
        if e.type == "file":
            if e.path in seen:
                print(f"Duplicate path in listing: {e.path}", file=sys.stderr)
                sys.exit(1)
            seen.add(e.path)
            files.append({"path": e.path, "size": getattr(e, "size", None)})
    # Deterministic ordering
    files.sort(key=lambda x: x["path"])
    print(json.dumps(files, separators=(",", ":")))

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/hf_list_tree.py
```

---

#### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: HF_TOKEN=... ./bin/snapshot.sh <owner> <repo> <date> [out_json]
#
# Produces manifest: batches/public-merged/<date>/file-manifest-<YYYYMMDD>.json
# Format:
# {
#   "repo": "owner/repo",
#   "date": "YYYY-MM-DD",
#   "generated_at_utc": "...",
#   "count": N,
#   "sha256_manifest": "...",
#   "files": [
#     {"path": "...", "cdn_url": "...", "size": ...},
#     ...
#   ]
# }

set -euo pipefail

REPO_OWNER="${1:-axentx}"
REPO_NAME="${2:-surrogate-1-training-pairs}"
DATE="${3:-$(date +%F)}"
OUTDIR="batches/public-merged/${DATE}"
OUTFILE="${4:-${OUTDIR}/file-manifest-$(echo "$DATE" | tr -d '-').json}"

mkdir -p "$OUTDIR"

echo "[$(date -u +%FT%T%z)] snapshot: listing ${REPO_OWNER}/${REPO_NAME} path='${DATE}/' recursive=False"

FILES_JSON=$(python3 ./bin/hf_list_tree.py "$REPO_OWNER" "$REPO_NAME" "$DATE")
if [ -z "$FILES_JSON" ]; then
  echo "[$(date -u +%FT%T%z)] snapshot: no files listed" >&2
  exit 1
fi

# Build manifest with CDN URLs and deterministic ordering
MANIFEST=$(python3 - "$DATE" "$REPO_OWNER" "$REPO_NAME" "$FILES_JSON" <<'PY'
import hashlib, json, sys, urllib.parse
from datetime import datetime, timezone

date, owner, repo, files_json = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
files = json.loads(files_json)

base = f"https://huggingface.co/datasets/{owner}/{repo}/resolve/main"
out_files = []
for f in sorted(files, key=lambda x: x["path"]):
    out_files.append({
        "path": f["path"],
        "cdn_url": f"{base}/{urllib.parse.quote(f['path'])}",
        "size": f.get("size")
    })

manifest_obj = {
    "repo": f"{owner}/{repo}",
    "date": date,
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "count": len(out_files),
    "files": out_files
}
manifest_bytes = json.dumps(manifest_obj, indent=2, sort_keys=True).encode()
manifest_obj["sha256_manifest"] = hashlib.sha256(manifest_bytes).hexdigest()
print(json.dumps(manifest_obj, indent=2, sort_keys=True))
PY
)

echo "$MANIFEST" > "$OUTFILE"
echo "[$(date -u +%FT%T%z)] snapshot: wrote $OUTFILE"
echo "[$(date -u +%FT%T%z)] snapshot: ${#FILES_JSON} files listed"

# Lightweight validation
COUNT=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))['files']))" "$OUTFILE")
if [ "$COUNT" -le 0 ]; then
  echo "[$(date -u +%FT%T%z)] snapshot: validation failed — empty file list" >&2
  exit 1
fi

# Optional CDN reachability check (fast, non-blocking)
if command -v curl >/dev/null 2>&1; then
  SAMPLE_URL=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['files'][0]['cdn_url'])" "$OUTFILE")
  if curl -fs --max-time 5 -I "$SAMPLE_URL" >/dev/null 2>&1; then
    echo "[$(date -u +%FT%T%z)] snapshot: CDN reachable (sampled)"
  else
    echo "[$(date -u +%FT%T%z)] snapshot: warning — CDN sample unreachable: $SAMPLE_URL" >&2
  fi
fi
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

#### Update `bin/dataset-enrich.sh` (minimal, backward-compatible)
Add near top after `set -euo pipefail`:

```bash
MANIFEST_FILE="${MANIFEST_FILE:-}"

if [

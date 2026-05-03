# surrogate-1 / quality

## Chosen improvement (highest-value, <2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion pipeline** that eliminates HF API rate limits during training and removes recursive listing/streaming pitfalls.

- **Why**: Avoids 429s, sidesteps `load_dataset(streaming=True)` schema errors, and enables deterministic shard reuse.
- **Scope**: Single-file refactor + small util additions; no infra changes.
- **Time**: ~90–120 min (tested locally, then pushed).

---

## Implementation plan

1. Add `bin/list-manifest.py`  
   - Runs on Mac (or cron) once per date folder.  
   - Uses `list_repo_tree(path, recursive=False)` per folder (non-recursive) to avoid 100× pagination.  
   - Emits `manifest/<date>/file-list.json` with `{repo, path, sha, size}` entries.  
   - Exits 0 with empty list if rate-limited; caller may retry after window.

2. Refactor `bin/dataset-enrich.sh` → `bin/dataset-enrich.sh` (new)  
   - Accepts `MANIFEST_FILE` env var (fallback to legacy behavior).  
   - If manifest provided, iterates entries and downloads via CDN URL:  
     `https://huggingface.co/datasets/$REPO/resolve/main/$PATH` (no auth header).  
   - Keeps existing per-record schema projection to `{prompt, response}` only.  
   - Writes output to `batches/public-merged/<date>/shard${SHARD_ID}-<HHMMSS>.jsonl`.  
   - Uses `lib/dedup.py` for central md5 dedup (unchanged).

3. Add `bin/util-cdn-download.py` (optional but cleaner)  
   - Downloads with retries, timeout, and streaming decompression.  
   - Validates `sha256` if present in manifest entry.

4. Update workflow `ingest.yml`  
   - Optional: add a prior job `prepare-manifest` that runs on PR/dispatch and uploads artifact `file-list-<date>.json`.  
   - Matrix runners consume artifact via `download-artifact` to avoid any per-shard API calls.

5. Validate locally  
   - Run manifest generation for a small date folder.  
   - Run single shard with manifest and confirm:  
     - No `datasets` streaming usage.  
     - Output lines contain only `prompt`/`response`.  
     - Dedup store receives entries.

---

## Code snippets

### bin/list-manifest.py
```python
#!/usr/bin/env python3
"""
Generate a CDN-friendly manifest for one date folder in a dataset repo.
Usage:
  HF_TOKEN=<token> python bin/list-manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-04-29 \
    --out manifest/2026-04-29/file-list.json
"""
import argparse
import json
import os
import sys
from typing import List, Dict

from huggingface_hub import HfApi, login

def build_manifest(repo_id: str, date: str) -> List[Dict]:
    api = HfApi()
    # Non-recursive per folder to avoid massive pagination
    prefix = f"{date}/"
    items = api.list_repo_tree(repo_id=repo_id, path=prefix, recursive=False)

    manifest = []
    for item in items:
        if item.type != "file":
            continue
        # CDN path is repo/file; no auth required for public datasets
        manifest.append(
            {
                "repo": repo_id,
                "path": item.path,
                "sha": getattr(item, "sha", None),
                "size": getattr(item, "size", None),
                "cdn_url": f"https://huggingface.co/datasets/{repo_id}/resolve/main/{item.path}",
            }
        )
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN manifest for date folder.")
    parser.add_argument("--repo", required=True, help="Dataset repo id")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    token = os.getenv("HF_TOKEN")
    if token:
        login(token=token, add_to_git_credential=False)

    try:
        manifest = build_manifest(args.repo, args.date)
    except Exception as exc:
        # Graceful exit for rate-limit or transient errors
        print(f"Failed to list repo tree: {exc}", file=sys.stderr)
        sys.exit(0)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(manifest)} entries to {args.out}")

if __name__ == "__main__":
    main()
```

### bin/dataset-enrich.sh (skeleton)
```bash
#!/usr/bin/env bash
#
# Refactored worker: manifest-driven, CDN-bypass ingestion.
#
# Required env:
#   SHARD_ID          (0-15)
#   DATE              (YYYY-MM-DD)
#   HF_TOKEN          (write token)
# Optional env:
#   MANIFEST_FILE     (if unset, falls back to legacy behavior)
#   REPO_ID           (default: axentx/surrogate-1-training-pairs)
#   OUT_DIR           (default: batches/public-merged)

set -euo pipefail
export SHELL=/bin/bash

REPO_ID="${REPO_ID:-axentx/surrogate-1-training-pairs}"
DATE="${DATE:?required}"
SHARD_ID="${SHARD_ID:?required}"
HF_TOKEN="${HF_TOKEN:?required}"
OUT_DIR="${OUT_DIR:-batches/public-merged}"
MANIFEST_FILE="${MANIFEST_FILE:-}"

TS=$(date -u +"%H%M%S")
OUT_FILE="${OUT_DIR}/${DATE}/shard${SHARD_ID}-${TS}.jsonl"
mkdir -p "$(dirname "${OUT_FILE}")"

# Central dedup store (shared across runners via mounted volume or HF Space state)
DEDUP_PY="lib/dedup.py"

log() {
  echo "[$(date -u --iso-8601=seconds)] $*"
}

process_file_cdn() {
  local path="$1"
  local url="https://huggingface.co/datasets/${REPO_ID}/resolve/main/${path}"
  # Stream and project to {prompt,response} only
  python3 -c "
import json, sys, urllib.request, gzip, io
from pathlib import Path

url = sys.argv[1]
out_path = sys.argv[2]
dedup = sys.argv[3]

# Accept gzip or plain
req = urllib.request.Request(url, headers={'Accept-Encoding': 'gzip'})
with urllib.request.urlopen(req, timeout=60) as resp:
    if resp.info().get('Content-Encoding') == 'gzip':
        buf = io.BytesIO(resp.read())
        fh = gzip.GzipFile(fileobj=buf, mode='rb')
    else:
        fh = io.TextIOWrapper(resp, encoding='utf-8')

    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        prompt = obj.get('prompt') or obj.get('input') or ''
        response = obj.get('response') or obj.get('output') or ''
        if not prompt or not response:
            continue
        record = {'prompt': prompt, 'response': response}
        # Dedup via central store
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
            json.dump(record, tmp)
            tmp_path = tmp.name
        subprocess.run([sys.executable, dedup, tmp_path, out_path], check=False)
        Path(tmp_path).unlink(missing_ok=True)
" "${url}" "${OUT_FILE}" "${DEDUP_PY}"
}

if [[ -n "${MANIFEST_FILE}" && -f "${MANIFEST_FILE}" ]]; then
  log "Using manifest ${MANIFEST_FILE}"
  python3 -c "
import json, sys,

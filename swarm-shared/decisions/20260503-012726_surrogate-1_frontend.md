# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value incremental improvement**: Replace per-file authenticated HF Hub API calls with **one non-recursive `list_repo_tree` per date folder + CDN-only fetches** to eliminate rate-limit pressure and recursive listing. This is the single change that unlocks reliable, high-throughput ingestion without quota exhaustion.

### Steps (1h 30m total)

1. **Add file-list utility** (`bin/list-date-folder.py`)  
   - Single non-recursive `list_repo_tree(path, recursive=False)` per date folder.  
   - Save flat list of file paths to JSON (e.g., `filelist-YYYY-MM-DD.json`).  
   - Exit 0 if folder empty or 404 (safe for cron).

2. **Update `bin/dataset-enrich.sh`**  
   - Accept optional `FILELIST_JSON` env var; if provided, read paths from JSON instead of calling `list_repo_files` recursively.  
   - Keep deterministic shard selection (`slug-hash % 16 == SHARD_ID`).  
   - Download each file via **CDN URL** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header.  
   - Retain schema projection to `{prompt,response}` and dedup via central `lib/dedup.py`.

3. **Update GitHub Actions matrix** (`/.github/workflows/ingest.yml`)  
   - Add a lightweight "indexer" job that runs **once per workflow** before the 16 shards.  
   - Indexer calls `list-date-folder.py` for today’s folder (and optionally yesterday’s if catching up) and uploads the JSON as an artifact.  
   - Shard jobs download the artifact and set `FILELIST_JSON`.  
   - Keep matrix `strategy: matrix: shard: [0..15]`.

4. **Hardening & observability**  
   - Respect HF 429: if indexer hits 429, wait 360s and retry (max 2 retries).  
   - Log CDN fetch counts and bytes to stdout for Actions metrics.  
   - Ensure `lib/dedup.py` uses WAL mode SQLite to avoid cross-shard lock contention when multiple shards run on the same HF Space (if ever co-located).

5. **Validation (local)**  
   - Run indexer locally with a test date folder; verify JSON contains expected parquet/jsonl paths.  
   - Run one shard locally with `FILELIST_JSON` pointing to that file; confirm downloads via CDN and correct shard selection.

---

## Code Snippets

### 1. `bin/list-date-folder.py`

```python
#!/usr/bin/env python3
"""
List files in a single date folder (non-recursive) for surrogate-1 dataset.
Usage:
  HF_TOKEN=<token> python list-date-folder.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out filelist-2026-05-03.json
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from huggingface_hub import HfApi, RepositoryError

HF_API = HfApi()
RETRY_WAIT = 360
MAX_RETRIES = 2

def list_date_folder(repo_id: str, date: str, out_path: Path) -> None:
    folder_path = f"batches/public-merged/{date}"
    attempt = 0

    while attempt <= MAX_RETRIES:
        try:
            items = HF_API.list_repo_tree(
                repo_id=repo_id,
                path=folder_path,
                repo_type="dataset",
                recursive=False,
            )
            # items can be dict or object depending on hf_hub version; normalize
            files = []
            for it in items:
                p = it["path"] if isinstance(it, dict) else getattr(it, "path", str(it))
                # Keep only files in the target folder (exclude subfolders)
                if not p.endswith("/"):
                    files.append(p)

            result = {
                "repo_id": repo_id,
                "folder": folder_path,
                "date": date,
                "files": sorted(files),
            }

            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(result, indent=2))
            print(json.dumps({"event": "filelist_written", "date": date, "count": len(files), "path": str(out_path)}))
            return

        except RepositoryError as e:
            if "404" in str(e) or "not found" in str(e).lower():
                # Folder doesn't exist yet — not an error
                empty = {"repo_id": repo_id, "folder": folder_path, "date": date, "files": []}
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(empty, indent=2))
                print(json.dumps({"event": "folder_not_found", "date": date, "path": str(out_path)}))
                return

            if "429" in str(e):
                attempt += 1
                if attempt > MAX_RETRIES:
                    print(json.dumps({"event": "rate_limit_giveup", "date": date, "error": str(e)}), file=sys.stderr)
                    sys.exit(1)
                wait = RETRY_WAIT
                print(json.dumps({"event": "rate_limit_retry", "date": date, "attempt": attempt, "wait": wait}), file=sys.stderr)
                time.sleep(wait)
                continue

            print(json.dumps({"event": "error", "date": date, "error": str(e)}), file=sys.stderr)
            sys.exit(1)

    print(json.dumps({"event": "unexpected_exit", "date": date}), file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List files in a date folder (non-recursive).")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs", help="HF dataset repo")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if token:
        HF_API = HfApi(token=token)

    list_date_folder(args.repo, args.date, Path(args.out))
```

Make executable:

```bash
chmod +x bin/list-date-folder.py
```

---

### 2. Update `bin/dataset-enrich.sh` (key excerpts)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ID="${REPO_ID:-axentx/surrogate-1-training-pairs}"
SHARD_ID="${SHARD_ID:-0}"
FILELIST_JSON="${FILELIST_JSON:-}"
WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$WORKDIR"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

# If FILELIST_JSON is provided, use it; otherwise fall back to recursive listing (legacy)
if [[ -n "$FILELIST_JSON" && -f "$FILELIST_JSON" ]]; then
  log "Using filelist: $FILELIST_JSON"
  mapfile -t FILES < <(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print('\n'.join(d.get('files',[])))" "$FILELIST_JSON")
else
  log "FILELIST_JSON not provided or missing — falling back to recursive list (may hit rate limits)"
  # Legacy behavior (avoid in production)
  mapfile -t FILES < <(python3 -c "
from huggingface_hub import HfApi
import os
api = HfApi(token=os.environ.get('HF_TOKEN'))
items = api.list_repo_files('$REPO_ID', repo_type='dataset')
print('\n'.join(items))
")
fi

# Deterministic shard selection
declare -a MY_FILES=()
for f in "${FILES[@]}"; do
  # Use slug-hash bucket: hash(path) % 16
  bucket=$(echo -n "$f" | python3 -c "import sys,hashlib; print(int(hashlib.sha256(sys.stdin.buffer.read()).hexdigest(),16) % 16)")
  if [[ "$bucket" == "$SHARD_ID" ]]; then

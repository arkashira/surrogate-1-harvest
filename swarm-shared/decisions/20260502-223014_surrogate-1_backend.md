# surrogate-1 / backend

**Final Implementation Plan (≤2h)**

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient, with deterministic sibling-repo sharding for write throughput.

---

### What we’ll ship
1. `bin/list-snapshot.py` — run once (or cron) to capture a deterministic file list for a date folder and emit `file-list.json`.
2. Update `bin/dataset-enrich.sh` to accept an optional `FILE_LIST` path; when provided, workers use CDN-only downloads (no `load_dataset`/API calls) and deterministic sibling-repo routing for uploads.
3. Minimal `lib/cdn.py` helper for zero-auth CDN fetch with retry/back-off.
4. `lib/sibling_shard.py` — hash-slug → sibling repo picker (5 siblings = 640 commits/hr aggregate).

Total estimate: ~90 minutes (30m code, 30m integration test, 30m verification).

---

### File changes

#### 1) `bin/list-snapshot.py`
```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in the public dataset repo.
Run from Mac (or cron) after rate-limit window clears.

Usage:
  python bin/list-snapshot.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-02 \
    --out file-list.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder under datasets/")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi()
    # Single non-recursive call per folder (avoids 100x pagination)
    folder = f"batches/public-merged/{args.date}"
    try:
        tree = api.list_repo_tree(repo_id=args.repo, path=folder, recursive=False)
    except Exception as e:
        print(f"HF API error: {e}", file=sys.stderr)
        sys.exit(1)

    files = sorted(
        {
            f.rfilename
            for f in tree
            if f.rfilename.endswith((".jsonl", ".parquet"))
        }
    )

    snapshot = {
        "repo": args.repo,
        "date": args.date,
        "folder": folder,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "count": len(files),
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

    print(f"Wrote {len(files)} files -> {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-snapshot.py
```

---

#### 2) `lib/cdn.py`
```python
import time
import requests
from typing import Optional

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def cdn_download(repo: str, path: str, timeout: int = 30, retries: int = 3) -> bytes:
    """
    Download public dataset file via CDN (no Authorization header).
    CDN tier has much higher rate limits than /api/.
    """
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, headers={})
            resp.raise_for_status()
            return resp.content
        except requests.HTTPError as e:
            if resp.status_code == 429:
                wait = 360 if attempt == retries else (2 ** attempt) * 5
                print(f"CDN 429, waiting {wait}s (attempt {attempt}/{retries})")
                time.sleep(wait)
                continue
            raise
        except requests.RequestException:
            if attempt == retries:
                raise
            time.sleep((2 ** attempt) * 2)
    raise RuntimeError(f"Failed to download {url} after {retries} attempts")
```

---

#### 3) `lib/sibling_shard.py`
```python
import hashlib
from typing import List

SIBLINGS = [
    "axentx/surrogate-1-training-pairs",
    "axentx/surrogate-1-shard-1",
    "axentx/surrogate-1-shard-2",
    "axentx/surrogate-1-shard-3",
    "axentx/surrogate-1-shard-4",
]

def repo_for_slug(slug: str, siblings: List[str] = SIBLINGS) -> str:
    """
    Deterministic shard assignment: hash slug -> sibling repo.
    5 siblings = 640 commits/hr aggregate.
    """
    digest = hashlib.sha256(slug.encode()).hexdigest()
    idx = int(digest, 16) % len(siblings)
    return siblings[idx]
```

---

#### 4) Update `bin/dataset-enrich.sh`
Key changes:
- Accept `FILE_LIST` env var pointing to `file-list.json`.
- If `FILE_LIST` provided, iterate files and use `lib/cdn.py` to stream content (bypass `load_dataset`).
- Use `lib/sibling_shard.py` to pick destination repo per slug.
- Keep existing HF token write path but route to sibling repo deterministically.

Patch (conceptual — integrate into existing script):
```bash
#!/usr/bin/env bash
set -euo pipefail

# Existing env
HF_TOKEN="${HF_TOKEN:?required}"
export HF_TOKEN

# New: optional pre-computed file list
FILE_LIST="${FILE_LIST:-}"
WORK_DATE="${WORK_DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"

# Python helpers
CDN_HELPER="$(dirname "$0")/../lib/cdn.py"
SIBLING_HELPER="$(dirname "$0")/../lib/sibling_shard.py"

function pick_repo_for_slug() {
  python3 -c "import sys; sys.path.insert(0, '$(dirname "$0")/..'); from lib.sibling_shard import repo_for_slug; print(repo_for_slug(sys.argv[1]))" "$1"
}

function process_file_cdn() {
  local repo="$1"
  local path="$2"
  python3 -c "
import sys, json, hashlib
sys.path.insert(0, '$(dirname "$0")/..')
from lib.cdn import cdn_download
content = cdn_download('$repo', '$path')
# Project to {prompt,response} here per existing schema rules
# Emit JSONL lines to stdout
"
}

if [ -n "$FILE_LIST" ]; then
  echo "Using CDN-only ingestion from $FILE_LIST"
  # Deterministic shard filtering
  python3 -c "
import json, hashlib, sys
with open('$FILE_LIST') as f:
    files = json.load(f)['files']
shard_id = $SHARD_ID
total = $TOTAL_SHARDS
for p in files:
    h = int(hashlib.sha256(p.encode()).hexdigest(), 16)
    if h % total == shard_id:
        print(p)
" | while read -r relpath; do
    repo=$(pick_repo_for_slug "axentx/surrogate-1-training-pairs")
    process_file_cdn "$repo" "$relpath"
    # Append to local shard output (existing logic)
  done
else
  echo "FILE_LIST not set — falling back to streaming (may hit API limits)"
  # Existing load_dataset(streaming=True) path
fi
```

---

#### 5) Workflow tweak (`.github/workflows/ingest.yml`)
Add optional `file_list_artifact` input and pass `FILE_LIST` to jobs.

```yaml
# Minimal addition to existing matrix job
    - name: Download file-list artifact (if present)
      uses: actions/download-artifact@v4
      with:
        name: file-list-${{ github.run_id }}
        path: .


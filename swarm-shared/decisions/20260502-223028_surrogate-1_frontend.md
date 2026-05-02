# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### What we’ll do
1. Add `bin/list-public-files.py` — single Mac-side script that calls `list_repo_tree` once per date folder, saves `public-files-<date>.json` with CDN URLs and metadata. Embed this list in training and worker scripts so Lightning training and GitHub Actions shards do **zero** `list_repo_files`/`list_repo_tree` API calls during data load.
2. Update `bin/dataset-enrich.sh` to accept an optional file-list JSON. If provided, workers iterate the local list and fetch via CDN (`/resolve/main/...`). If not provided, fall back to current behavior (but log warning).
3. Add deterministic sibling-repo write sharding: `shard_repo = f"datasets/axentx/surrogate-1-training-pairs-{hash(slug) % 6}"` (5 siblings + primary = 6 repos) to raise aggregate commit cap from 128/hr to 768/hr.
4. Add small util `lib/cdn.py` with `download_via_cdn()` and `get_repo_file_list()` helpers.
5. Update README with usage and the HF CDN bypass note.

Estimated time: ~90 minutes (45m code, 30m integration/test, 15m docs).

---

### 1) New helper: `lib/cdn.py`

```python
# lib/cdn.py
import json
import os
import hashlib
import requests
from typing import List, Dict, Optional
from huggingface_hub import HfApi, hf_hub_download

HF_API = HfApi()

def deterministic_repo_for_slug(slug: str, primary: str = "datasets/axentx/surrogate-1-training-pairs", n_siblings: int = 5) -> str:
    """
    Deterministic repo assignment by slug hash.
    Returns one of: primary + n_siblings repos.
    """
    digest = hashlib.sha256(slug.encode("utf-8")).digest()
    idx = int.from_bytes(digest[:4], "little") % (n_siblings + 1)
    if idx == 0:
        return primary
    return f"{primary}-sib{idx}"

def get_repo_file_list(repo_id: str, date: str, cache_dir: str = ".cache") -> List[Dict]:
    """
    Return cached file list for repo/date; generate if missing.
    Output: [{"path": str, "size": int, "cdn_url": str}, ...]
    """
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"public-files-{date}.json")

    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    # Generate once via HF API (run this on Mac outside of rate-limited envs)
    entries = []
    try:
        items = HF_API.list_repo_tree(repo_id, path=date, recursive=False)
        for item in items:
            if item.type == "file":
                entries.append({
                    "path": f"{date}/{item.path}",
                    "size": item.size or 0,
                    "cdn_url": f"https://huggingface.co/{repo_id}/resolve/main/{date}/{item.path}"
                })
    except Exception as e:
        raise RuntimeError(f"Failed to list {repo_id}/{date}: {e}")

    with open(cache_path, "w") as f:
        json.dump(entries, f, indent=2)
    return entries

def download_via_cdn(cdn_url: str, out_path: str, timeout: int = 30) -> bool:
    """Download via CDN (no auth header) to bypass API rate limits."""
    try:
        resp = requests.get(cdn_url, timeout=timeout, stream=True)
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"CDN download failed ({cdn_url}): {e}")
        return False
```

---

### 2) Mac-side: generate file list once (non-recursive per folder)

`bin/list-public-files.py`
```python
#!/usr/bin/env python3
"""
Usage: ./bin/list-public-files.py <date> [--repo REPO] [--out FILE]
Generate public-files-<date>.json with CDN URLs for the date folder.
Run this once (outside rate-limited CI) and commit or host the JSON.
"""
import argparse
import json
import sys
from lib.cdn import get_repo_file_list

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN file list for a date.")
    parser.add_argument("date", help="Date folder, e.g. 2026-04-29")
    parser.add_argument("--repo", default="datasets/axentx/surrogate-1-training-pairs", help="HF repo id")
    parser.add_argument("--out", help="Output JSON path (default: public-files-<date>.json)")
    args = parser.parse_args()

    out_path = args.out or f"public-files-{args.date}.json"
    try:
        entries = get_repo_file_list(args.repo, args.date, cache_dir=".")
        with open(out_path, "w") as f:
            json.dump(entries, f, indent=2)
        print(f"Wrote {len(entries)} files to {out_path}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-public-files.py
```

Run once (after rate-limit window clears):
```bash
./bin/list-public-files.py 2026-04-29 --out file-list-2026-04-29.json
```

Commit or host `file-list-*.json` so runners can fetch it without API auth.

---

### 3) Updated worker: CDN-only fetch + sibling upload

`bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
# Worker entrypoint for GitHub Actions matrix shard.
# Uses pre-computed file-list.json and CDN URLs to avoid HF API 429.
set -euo pipefail

# Inputs
HF_TOKEN="${HF_TOKEN:-}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"          # 0..15
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
FILE_LIST="${FILE_LIST:-file-list.json}"
OUTPUT_DIR="output"
SIBLING_OVERRIDE="${SIBLING_OVERRIDE:-}"  # optional: force one repo

mkdir -p "$OUTPUT_DIR"

# Resolve sibling repo (python helper)
resolve_repo() {
  python3 -c "
import sys, hashlib
primary='datasets/axentx/surrogate-1-training-pairs'
if '${SIBLING_OVERRIDE}':
    print('${SIBLING_OVERRIDE}')
else:
    slug = sys.argv[1]
    idx = int.from_bytes(hashlib.sha256(slug.encode()).digest()[:4], 'little') % 6  # 0..5
    if idx == 0:
        print(primary)
    else:
        print(f'{primary}-sib{idx}')
" "$1"
}

# Download file-list if not present (fallback to raw CDN if repo-hosted)
if [[ ! -f "$FILE_LIST" ]]; then
  echo "Fetching file-list from CDN..."
  curl -fsSL "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/file-list-${DATE}.json" -o "$FILE_LIST" || \
    curl -fsSL "https://huggingface.co/datasets/axentx/surrogate-1-runner/resolve/main/file-list-${DATE}.json" -o "$FILE_LIST"
fi

# Parse file list (jq preferred; fallback to python)
mapfile -t FILES < <(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for item in data:
    print(item['path']

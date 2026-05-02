# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### What we’ll do
1. Add `bin/list-files.py` — one-time Mac/CI script that calls `list_repo_tree` once per date folder, saves `file-list.json`, and embeds it in the repo for reproducible training runs.
2. Add `lib/cdn_stream.py` — CDN-only downloader with exponential backoff that bypasses HF API entirely for public files.
3. Update `bin/dataset-enrich.sh` to accept `FILE_LIST` env var; when set, workers stream from CDN URLs only and skip `load_dataset` for heterogeneous repos.

Total diff: ~120 lines across 3 files; <2h including tests.

---

### 1) `bin/list-files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a HuggingFace dataset repo.
Usage:
  HF_TOKEN=<token> python bin/list-files.py \
    --repo axentx/surrogate-1-training-pairs \
    --path raw/2026-05-02 \
    --out file-list.json

Notes:
- Uses list_repo_tree(path, recursive=False) per folder to avoid 429.
- CDN download URLs are NOT rate-limited by /api/ endpoints.
- Output is stable (sorted) so training scripts can embed it.
"""
import argparse
import json
import os
import sys
import time
from typing import List, Dict

from huggingface_hub import HfApi, RepositoryTreeEntry

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_folder(api: HfApi, repo: str, folder: str) -> List[RepositoryTreeEntry]:
    """Single non-recursive call per folder."""
    target = folder if folder else "."
    try:
        entries = api.list_repo_tree(repo=repo, path=target, recursive=False)
    except Exception as exc:
        print(f"ERROR listing {repo}/{target}: {exc}", file=sys.stderr)
        raise
    return entries

def build_file_list(repo: str, root: str, api: HfApi) -> List[Dict]:
    """
    Walk root non-recursively by known subfolders (avoids recursive list_repo_files).
    If root contains nested folders, we expand one level only.
    """
    entries = list_folder(api, repo, root)
    files = []

    for entry in entries:
        if entry.type == "file":
            files.append(
                {
                    "path": entry.path,
                    "size": getattr(entry, "size", None),
                    "lfs": getattr(entry, "lfs", None),
                    "cdn_url": CDN_TEMPLATE.format(repo=repo, path=entry.path),
                }
            )
        elif entry.type == "folder":
            # One-level expansion to avoid heavy recursive calls
            sub_entries = list_folder(api, repo, entry.path)
            for sub in sub_entries:
                if sub.type == "file":
                    files.append(
                        {
                            "path": sub.path,
                            "size": getattr(sub, "size", None),
                            "lfs": getattr(sub, "lfs", None),
                            "cdn_url": CDN_TEMPLATE.format(repo=repo, path=sub.path),
                        }
                    )

    # Deterministic ordering
    files.sort(key=lambda x: x["path"])
    return files

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN file list for HF dataset repo.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (user/repo)")
    parser.add_argument("--path", default="", help="Root folder inside repo")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--retry-wait", type=int, default=360, help="Wait seconds on 429")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    api = HfApi(token=token)

    for attempt in range(3):
        try:
            files = build_file_list(args.repo, args.path, api)
            break
        except Exception as exc:
            if attempt == 2:
                print(f"FAILED after retries: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"Retry {attempt+1}/3 after {args.retry_wait}s: {exc}", file=sys.stderr)
            time.sleep(args.retry_wait)

    out = {
        "repo": args.repo,
        "root": args.path,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True)

    print(f"Wrote {len(files)} files -> {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-files.py
```

---

### 2) `lib/cdn_stream.py`

```python
"""
CDN-only streaming download helpers to avoid HF API rate limits.
Usage:
  from lib.cdn_stream import cdn_lines
  for line in cdn_lines(url, max_retries=5):
      process(line)
"""
import time
import requests
from typing import Iterator, Optional

DEFAULT_TIMEOUT = 30

def _backoff(attempt: int) -> float:
    return min(60.0, (2 ** attempt) + (attempt * 0.5))

def cdn_get(url: str, headers: Optional[dict] = None, max_retries: int = 5) -> bytes:
    """
    Download file from HF CDN (no Authorization header required for public repos).
    Retries with exponential backoff on 429/5xx.
    """
    if headers is None:
        headers = {}
    # Do NOT send Authorization for public CDN endpoints
    safe_headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}

    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=safe_headers, timeout=DEFAULT_TIMEOUT, stream=True)
            if resp.status_code == 429:
                wait = _backoff(attempt)
                print(f"CDN 429 on {url}, retry {attempt+1}/{max_retries} in {wait:.1f}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            # Stream content into memory (for small/medium parquet/jsonl this is fine)
            # For very large files, consider streaming to temp file.
            return resp.content
        except (requests.RequestException, IOError) as exc:
            if attempt == max_retries - 1:
                raise
            wait = _backoff(attempt)
            print(f"CDN error {exc} on {url}, retry in {wait:.1f}s")
            time.sleep(wait)

    raise RuntimeError(f"Failed to fetch {url} after {max_retries} retries")

def cdn_lines(url: str, max_retries: int = 5, encoding: str = "utf-8") -> Iterator[str]:
    """
    Yield lines from a CDN text file (e.g., .jsonl).
    """
    data = cdn_get(url, max_retries=max_retries)
    for line in data.decode(encoding).splitlines():
        line = line.strip()
        if line:
            yield line
```

---

### 3) Update `bin/dataset-enrich.sh`

Add CDN mode and optional file-list input. Preserve existing behavior when `FILE_LIST` is unset.

```bash
#!/usr/bin/env bash
#
# dataset-enrich.sh
# Normalize public datasets into {prompt,response} pairs.
#
# New behavior:
#   FILE_LIST=/path/to/file-list.json  -> use CDN-only fetches (no HF API streaming)
#   FILE_LIST=                         -> fallback to datasets.load_dataset

set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
FILE_LIST="${FILE_LIST:-}"
OUT_DIR="${OUT_DIR:-enriched}"
MAX_WORKERS="${MAX_WORKERS:-4

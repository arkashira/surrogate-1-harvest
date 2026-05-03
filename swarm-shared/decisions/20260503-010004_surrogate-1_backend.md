# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Goal**: Eliminate HF API 429s during ingestion by switching to **pre-list + CDN-only fetches** and add robust retry/backoff for the single list step.

### 1) Pre-list file paths once and embed in worker (Mac orchestrator)
- Run a one-time `list_repo_tree` (non-recursive per date folder) from the Mac runner.
- Save `{date: [paths]}` to `file-list.json`.
- Commit `file-list.json` into `surrogate-1-runner` so each GitHub Actions shard can consume it without API calls.

### 2) Update `bin/dataset-enrich.sh` to use CDN URLs
- Replace `load_dataset(streaming=True, repo, split=..., files=...)` with direct `curl`/`wget` (or Python `requests`) to:
  ```
  https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<path>
  ```
- Parse only `{prompt, response}` at read time; drop all other fields.
- Write normalized rows to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

### 3) Deterministic shard assignment unchanged
- Keep `SHARD_ID`/`TOTAL_SHARDS` matrix strategy.
- Hash slug → `hash(slug) % TOTAL_SHARDS` to assign rows to shards.

### 4) Central dedup remains SQLite on HF Space
- Workers call `lib/dedup.py` (HTTP or shared store) to check/insert md5.
- Accept occasional cross-run duplicates (wastes bandwidth, not correctness).

### 5) Retry/backoff for the one-time list step
- If 429 on `list_repo_tree`, wait 360s and retry (max 3 tries).
- Use `list_repo_tree(path, recursive=False)` per folder (not recursive on entire repo).

### 6) Lightning training script changes (optional follow-up)
- Embed the same `file-list.json` in training repo.
- Use CDN-only `requests`/`aiohttp` stream + pyarrow projection to `{prompt, response}`.
- No `load_dataset` during training.

---

## Code Snippets

### `scripts/pre-list-files.py` (run from Mac orchestrator)
```python
#!/usr/bin/env python3
"""
Pre-list public dataset files into file-list.json to avoid HF API calls during ingestion.
Run manually or via cron when new date folders appear.
"""
import json
import os
import sys
import time
from pathlib import Path

from huggingface_hub import HfApi, Repository

REPO_ID = "axentx/surrogate-1-training-pairs"
OUTPUT = Path(__file__).parent.parent / "file-list.json"
FOLDER_PREFIX = ""  # e.g. "raw/2026-05-03" or "" for all

def list_with_retry(api, path, retries=3, wait=360):
    for attempt in range(1, retries + 1):
        try:
            # recursive=False to avoid paginating 100x; we'll walk subfolders manually if needed
            tree = api.list_repo_tree(repo_id=REPO_ID, path=path, recursive=False)
            files = [item.path for item in tree if item.type == "file"]
            subdirs = [item.path for item in tree if item.type == "dir"]
            return files, subdirs
        except Exception as exc:
            if attempt == retries:
                raise
            print(f"[{attempt}/{retries}] list_repo_tree failed ({exc}), waiting {wait}s", file=sys.stderr)
            time.sleep(wait)

def main():
    api = HfApi()
    result = {}

    # If you know date folders, list them explicitly to minimize API calls
    root_files, root_dirs = list_with_retry(api, path=FOLDER_PREFIX or "/")
    # Include root files
    if root_files:
        result["."] = root_files

    # For each immediate subfolder (e.g. date-named), list files non-recursive
    for d in root_dirs:
        try:
            files, _ = list_with_retry(api, path=d)
            if files:
                result[d] = files
        except Exception as exc:
            print(f"Skipping {d}: {exc}", file=sys.stderr)

    OUTPUT.write_text(json.dumps(result, indent=2))
    print(f"Wrote {len(result)} folder entries to {OUTPUT}")

if __name__ == "__main__":
    main()
```

### `bin/dataset-enrich.sh` (updated worker)
```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Runs in GitHub Actions shard (SHARD_ID=0..15, TOTAL_SHARDS=16)
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
BASE_URL="https://huggingface.co/datasets/${REPO}/resolve/main"
WORKDIR=$(cd "$(dirname "$0")/.." && pwd)
FILE_LIST="${WORKDIR}/file-list.json"

# Deterministic shard assignment
SHARD_ID=${SHARD_ID:-0}
TOTAL_SHARDS=${TOTAL_SHARDS:-16}
DATE=$(date +%Y-%m-%d)
OUTDIR="${WORKDIR}/batches/public-merged/${DATE}"
TS=$(date +%H%M%S)
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "${OUTDIR}"

# Python helper to stream + project + dedup
python3 "${WORKDIR}/lib/worker.py" \
  --file-list "${FILE_LIST}" \
  --shard-id "${SHARD_ID}" \
  --total-shards "${TOTAL_SHARDS}" \
  --base-url "${BASE_URL}" \
  --outfile "${OUTFILE}"

# Push to HF dataset (use huggingface_hub upload via ghaction secrets)
if [[ -n "${HF_TOKEN:-}" ]]; then
  huggingface-cli upload --repo-type dataset "${REPO}" "${OUTFILE}" "batches/public-merged/${DATE}/$(basename "${OUTFILE}")" --token "${HF_TOKEN}"
else
  echo "HF_TOKEN not set; skipping upload"
fi
```

### `lib/worker.py` (stream + project + dedup)
```python
#!/usr/bin/env python3
import argparse
import hashlib
import json
import sys
from pathlib import Path

import requests

def parse_file_cdn(url: str):
    """Stream a parquet/jsonl file from CDN and yield {prompt, response} rows."""
    # For simplicity, assume JSONL; adapt for parquet via pyarrow if needed.
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            prompt = obj.get("prompt") or obj.get("input") or obj.get("text")
            response = obj.get("response") or obj.get("output")
            if prompt and response:
                yield {"prompt": str(prompt), "response": str(response)}

def slug_hash(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-list", required=True)
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--total-shards", type=int, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--outfile", required=True)
    args = parser.parse_args()

    file_list = json.loads(Path(args.file_list).read_text())
    # flatten
    all_files = []
    for folder, files in file_list.items():
        for f in files:
            all_files.append(f if folder == "." else f"{folder}/{f}")

    Path(args.outfile).parent.mkdir(parents=True, exist_ok=True)

    with open(args.outfile, "w", encoding="utf-8") as out:
        for path in all_files:
            # Deterministic shard by path (so same file always same shard)
            if slug_hash(path) % args.total_shards != args.shard_id:
                continue

            url = f"{args.base_url}/{path}"
            try:
                for row in parse_file_cdn(url):
                    # Optional: call central dedup here (lib/dedup.py) and skip if

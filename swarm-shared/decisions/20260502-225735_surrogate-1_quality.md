# surrogate-1 / quality

## Final Implementation (merged + hardened)

**Chosen highest-value improvement**:  
Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make all 16 shard workers fully resilient to rate limits.

**Summary of changes** (3 files, ~140 lines total):

1. `bin/list_files.py` — single deterministic run (post-rate-limit window) that walks one date folder via `list_repo_tree(recursive=False)` and emits `filelist.json`.  
2. `bin/dataset-enrich.sh` — accepts optional `FILELIST_JSON`; if present, workers slice deterministically and stream via CDN (zero HF API calls). Falls back to legacy `load_dataset(streaming=True)` when absent.  
3. `requirements.txt` — add `requests` (lightweight) for robust CDN streaming with retries.

---

### 1) `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a HuggingFace dataset repo.

Usage:
  HF_TOKEN=<token> python bin/list_files.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-04-29 \
    --out filelist.json

Output schema:
  [{"repo": "...", "path": "...", "cdn_url": "..."}, ...]
"""
import argparse
import json
import os
import sys
import time
from typing import Dict, List

from huggingface_hub import HfApi, login, utils

CDN_BASE = "https://huggingface.co/datasets"

def is_retryable(exc: Exception) -> bool:
    return isinstance(exc, utils.RepositoryNotFoundError) is False and (
        "429" in str(exc) or "502" in str(exc) or "503" in str(exc) or "504" in str(exc)
    )

def list_date_files(repo: str, date: str, api: HfApi) -> List[Dict[str, str]]:
    """
    Walk repo/date/* using non-recursive tree calls to minimize 429 risk.
    Returns deterministic sorted list of dicts with repo+path+cdn_url.
    """
    root = date.strip("/")
    out: List[Dict[str, str]] = []

    try:
        top = api.list_repo_tree(repo=repo, path=root, recursive=False)
    except Exception as e:
        print(f"Cannot list {repo}/{root}: {e}", file=sys.stderr)
        return out

    for entry in top:
        if entry.type != "directory":
            continue
        subpath = f"{root}/{entry.path}"
        try:
            files = api.list_repo_tree(repo=repo, path=subpath, recursive=False)
        except Exception as e:
            print(f"Cannot list {repo}/{subpath}: {e}", file=sys.stderr)
            continue

        for f in files:
            if f.type != "file":
                continue
            full = f"{subpath}/{f.path}"
            out.append({
                "repo": repo,
                "path": full,
                "cdn_url": f"{CDN_BASE}/{repo}/resolve/main/{full}"
            })

    out.sort(key=lambda x: x["path"])
    return out

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN file list for dataset repo.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (user/name)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", default="filelist.json", help="Output JSON path")
    parser.add_argument("--retry-wait", type=int, default=360, help="Wait on 429/5xx (s)")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retries on transient errors")
    args = parser.parse_args()

    token = os.getenv("HF_TOKEN")
    if token:
        login(token=token, add_to_git_credential=False)

    api = HfApi()

    for attempt in range(1, args.max_retries + 1):
        try:
            items = list_date_files(args.repo, args.date, api)
            break
        except Exception as e:
            if is_retryable(e) and attempt < args.max_retries:
                print(f"Retryable error, waiting {args.retry_wait}s (attempt {attempt}/{args.max_retries}): {e}", file=sys.stderr)
                time.sleep(args.retry_wait)
                continue
            print(f"Failed to list files: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        sys.exit("Failed after retries.")

    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(items, fp, indent=2, ensure_ascii=False)

    print(f"Wrote {len(items)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list_files.py
```

---

### 2) `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
# surrogate-1 dataset-enrich worker (GitHub Actions shard)
#
# Optional env:
#   FILELIST_JSON   path to JSON from bin/list_files.py
#                   If set, workers stream via CDN (zero HF API calls).
#   SHARD_ID        0..15 (required by workflow)
#   HF_TOKEN        write token for uploads
#
# Behavior:
# - If FILELIST_JSON present and valid: deterministic shard slice -> CDN streaming.
# - Otherwise: fallback to legacy load_dataset(streaming=True) path.

set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE=$(date +%Y-%m-%d)
TS=$(date +%H%M%S)
OUT="batches/public-merged/${DATE}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "$(dirname "$OUT")"

if [[ -n "${FILELIST_JSON:-}" && -f "$FILELIST_JSON" ]]; then
  echo "[$SHARD_ID] CDN mode: reading file list from $FILELIST_JSON"

  # Deterministic shard assignment by path hash
  mapfile -t ALL_PATHS < <(jq -r '.[].path' "$FILELIST_JSON")
  TOTAL=${#ALL_PATHS[@]}
  if (( TOTAL == 0 )); then
    echo "[$SHARD_ID] No files found."
    exit 0
  fi

  SHARD_FILES=()
  for path in "${ALL_PATHS[@]}"; do
    # Stable numeric hash from path -> bucket 0..15
    HASH=$(echo -n "$path" | md5sum | tr -d ' -' | tr '[:alpha:]' '[:digit:]')
    BUCKET=$(( 0x${HASH: -4} % 16 ))
    if (( BUCKET == SHARD_ID )); then
      SHARD_FILES+=("$path")
    fi
  done

  echo "[$SHARD_ID] Selected ${#SHARD_FILES[@]}/$TOTAL files for shard."

  # Stream via CDN (no Authorization header) and project {prompt,response}
  python3 - "$OUT" "${SHARD_FILES[@]}" <<'PY'
import json
import sys
import urllib.request
from pathlib import Path

OUT = Path(sys.argv[1])
paths = sys.argv[2:]

def normalize_record(raw: dict) -> dict | None:
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question")
    response = raw.get("response") or raw.get("output") or raw.get("answer")
    if not prompt or not response:
        return None
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

written = 0
with OUT.open("w", encoding="utf-8") as fout:
    for p in paths:
        url = f"https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{p}"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                if p.endswith(".jsonl"):
                    for line in resp:
                        rec = json.loads(line.decode())
                        norm = normalize_record(rec)
                        if norm:
                            fout.write(json.dumps(norm, ensure_

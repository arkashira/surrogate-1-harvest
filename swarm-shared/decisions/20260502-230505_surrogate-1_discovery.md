# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Goal**: Eliminate HF API 429s during training and make shard workers resilient by switching to deterministic pre-flight file listing + CDN-only ingestion.

### Steps

1. Add `bin/list_files.py` — single Mac-side script that calls `list_repo_tree` once per date folder, saves `file-list.json` to repo root (committed or artifact). Uses HF token but only one API call per run.
2. Update `bin/dataset-enrich.sh` to accept an optional file-list path. If provided, workers read URLs from that list instead of calling `list_repo_files`/`list_repo_tree`. Each worker still processes its deterministic shard (by slug-hash) but fetches via CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header.
3. Add fallback: if file-list missing or a CDN fetch fails (404/403), worker falls back to HF API for that single file (keeps resilience).
4. Update training script (`train.py` or equivalent) to embed the same file-list (or accept it as argument) and use CDN-only `hf_hub_download`/`wget`/`requests` for data loading — zero API calls during training.
5. Add small retry/backoff for CDN downloads (separate from API rate limits).
6. Add note to README about workflow: run `list_files.py` after rate-limit window clears, commit artifact, then trigger workers/training.

Estimated time: ~90 minutes (30m script, 30m shell + training integration, 30m test/README).

---

## Code Snippets

### 1) bin/list_files.py
```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in axentx/surrogate-1-training-pairs.
Usage:
  HF_TOKEN=... python bin/list_files.py --repo axentx/surrogate-1-training-pairs --path batches/public-merged/2026-05-02 --out file-list.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi, hf_hub_url

def main() -> None:
    parser = argparse.ArgumentParser(description="List repo tree for a folder.")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--path", default="batches/public-merged", help="Folder path in repo")
    parser.add_argument("--out", default="file-list.json", help="Output JSON file")
    parser.add_argument("--base-cdn", default="https://huggingface.co/datasets", help="CDN base")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        print("ERROR: HF_TOKEN environment variable required for listing.", file=sys.stderr)
        sys.exit(1)

    api = HfApi(token=token)
    try:
        # recursive=False keeps API calls minimal; we only need top-level files in this folder
        entries = api.list_repo_tree(repo_id=args.repo, path=args.path, recursive=False)
    except Exception as exc:
        print(f"ERROR: failed to list repo tree: {exc}", file=sys.stderr)
        sys.exit(1)

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        # CDN URL bypasses API auth/rate limits during training/ingest
        cdn_url = f"{args.base_cdn}/{args.repo}/resolve/main/{entry.path}"
        files.append({
            "path": entry.path,
            "cdn_url": cdn_url,
            "size": getattr(entry, "size", None),
        })

    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"folder": args.path, "files": files}, f, indent=2)
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list_files.py
```

---

### 2) bin/dataset-enrich.sh (excerpt — integrate into existing worker)
Add near top:
```bash
#!/usr/bin/env bash
set -euo pipefail
# Optional file list to avoid HF API calls during ingestion
FILE_LIST="${FILE_LIST:-}"          # e.g. file-list.json
SHARD_ID="${SHARD_ID:-0}"
N_SHARDS="${N_SHARDS:-16}"
```

Replace any `list_repo_files`/`list_repo_tree` loop with:
```bash
resolve_files() {
  if [[ -n "$FILE_LIST" && -f "$FILE_LIST" ]]; then
    # Use CDN list; filter to this shard by deterministic hash of filename
    jq -r '.files[].path' "$FILE_LIST" | while read -r f; do
      # deterministic shard assignment by filename
      h=$(echo -n "$f" | md5sum | cut -c1-8)
      shard=$(( 0x$h % N_SHARDS ))
      if (( shard == SHARD_ID )); then
        echo "$f"
      fi
    done
  else
    # Fallback: use HF API (may hit rate limits)
    python3 -c "
import os, sys
from huggingface_hub import HfApi
api = HfApi(token=os.environ.get('HF_TOKEN'))
entries = api.list_repo_tree(repo_id='axentx/surrogate-1-training-pairs', path='batches/public-merged', recursive=False)
for e in entries:
    if e.type == 'file':
        print(e.path)
" | while read -r f; do
      h=$(echo -n "$f" | md5sum | cut -c1-8)
      shard=$(( 0x$h % N_SHARDS ))
      if (( shard == SHARD_ID )); then
        echo "$f"
      fi
    done
  fi
}
```

Download helper (CDN-first):
```bash
download_cdn_first() {
  local relpath="$1"
  local out="$2"
  local cdn_url="https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/${relpath}"
  if curl -fsSL --retry 3 --retry-delay 2 -o "$out" "$cdn_url"; then
    return 0
  fi
  # CDN failed — fallback to HF API download (counts against rate limits)
  if [[ -n "${HF_TOKEN:-}" ]]; then
    python3 -c "
import sys
from huggingface_hub import hf_hub_download
hf_hub_download(repo_id='axentx/surrogate-1-training-pairs', filename='$relpath', local_dir='$out', local_dir_use_symlinks=False)
" && return 0
  fi
  echo "ERROR: failed to download $relpath" >&2
  return 1
}
```

Use in worker loop:
```bash
while read -r f; do
  tmp=$(mktemp)
  if download_cdn_first "$f" "$tmp"; then
    # process $tmp -> normalize -> dedup -> output
    # ...
    rm -f "$tmp"
  else
    echo "WARN: skipped $f"
  fi
done < <(resolve_files)
```

---

### 3) Training script integration (example train.py excerpt)
```python
import json
import os
import subprocess
from pathlib import Path

def load_file_list(list_path: str):
    if not os.path.isfile(list_path):
        return []
    with open(list_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [item["cdn_url"] for item in data.get("files", [])]

def stream_cdn_files(file_urls, shard_id=0, n_shards=1):
    for i, url in enumerate(file_urls):
        if i % n_shards != shard_id:
            continue
        # CDN fetch without HF API auth
        out = Path("/tmp") / f"shard{shard_id}_{i}.parquet"
        subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", str(out), url], check=False)
        if out.exists():
            yield out
        else:
            # optional fallback to hf_hub_download for this file
            pass
```

In your data loader, call `stream_cdn_files(file_urls, shard_id, n_shards)` and parse parquet → project `{prompt, response}` only at parse time (per earlier pattern).

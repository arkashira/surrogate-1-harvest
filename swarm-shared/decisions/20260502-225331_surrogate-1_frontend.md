# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### Changes (3 files, ~110 lines total)

1. **`bin/list_files.py`** — single API call to `list_repo_tree` per date folder, emit JSON of `{repo, date, generated_at, files[]}` with `{path, size}` for CDN fetches. Embeddable in training scripts and shard workers.

2. **`bin/dataset-enrich.sh`** — accept optional `FILE_LIST_JSON`; if provided, stream listed paths via CDN (`/resolve/main/...`) with `curl` + `python3 -c` normalization, bypassing `datasets` API calls entirely. Fallback to `datasets`/`hf_hub_download` only when file list unavailable.

3. **`.github/workflows/ingest.yml`** — add a pre-step that runs `list_files.py` for today’s date folder, passes JSON to each matrix shard via `matrix.file_list`. Keeps API usage to one call total per workflow run.

---

### 1) `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
List files in a date folder of axentx/surrogate-1-training-pairs
and emit a JSON payload for CDN-only ingestion.

Usage:
  python bin/list_files.py --date 2026-05-02 --out file-list.json
"""
import argparse
import json
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

REPO = "axentx/surrogate-1-training-pairs"

def list_date_folder(date_str: str, out_path: str) -> None:
    api = HfApi()
    prefix = f"batches/public-merged/{date_str}/"
    items = api.list_repo_tree(repo_id=REPO, path=prefix, recursive=False)

    files = []
    for item in items:
        if item.type == "file":
            files.append({"path": item.path, "size": item.size})

    files.sort(key=lambda x: x["path"])

    payload = {
        "repo": REPO,
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)

    print(f"Wrote {len(files)} files to {out_path}", file=sys.stderr)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List files for a date folder.")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", default="file-list.json")
    args = parser.parse_args()

    try:
        list_date_folder(args.date, args.out)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
```

Make executable:
```bash
chmod +x bin/list_files.py
```

---

### 2) `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Normalize + dedup public dataset shards.
#
# Usage:
#   FILE_LIST_JSON=file-list.json ./bin/dataset-enrich.sh <shard_id> <total_shards>
#
# If FILE_LIST_JSON is provided, uses CDN URLs (/resolve/main/...) to bypass HF API auth.
# Otherwise falls back to datasets library (streaming) for compatibility.

set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE=$(date +%Y-%m-%d)
TS=$(date +%H%M%S)
SHARD_ID=${1:-0}
TOTAL_SHARDS=${2:-16}
OUT_DIR="batches/public-merged/${DATE}"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"
HF_TOKEN=${HF_TOKEN:-}

mkdir -p "$(dirname "$OUT_FILE")"

# Deterministic shard assignment by slug hash
shard_for() {
  local slug=$1
  echo $(( $(echo -n "$slug" | md5sum | cut -c1-8) % TOTAL_SHARDS ))
}

# Dedup via central store (existing lib/dedup.py)
is_duplicate() {
  local md5=$1
  python3 lib/dedup.py check "$md5"
}

mark_seen() {
  local md5=$1
  python3 lib/dedup.py add "$md5"
}

normalize_pair() {
  local file=$1
  python3 -c "
import json, sys, hashlib
with open('$file', 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        prompt = obj.get('prompt') or obj.get('input') or obj.get('text') or ''
        response = obj.get('response') or obj.get('output') or ''
        if not prompt or not response:
            continue
        md5 = hashlib.md5((prompt + response).encode('utf-8')).hexdigest()
        print(json.dumps({'prompt': prompt, 'response': response, '_md5': md5}, ensure_ascii=False))
"
}

process_file_cdn() {
  local path=$1
  local url="https://huggingface.co/datasets/${REPO}/resolve/main/${path}"
  local tmp=$(mktemp)
  if curl -fsSL --retry 3 --retry-delay 5 -o "$tmp" "$url"; then
    normalize_pair "$tmp" | while read -r line; do
      local md5
      md5=$(echo "$line" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['_md5'])")
      if ! is_duplicate "$md5"; then
        echo "$line"
        mark_seen "$md5"
      fi
    done
  else
    echo "WARN: CDN download failed for $path" >&2
  fi
  rm -f "$tmp"
}

process_file_datasets() {
  local path=$1
  local tmp=$(mktemp)
  python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(repo_id='${REPO}', filename='$path', local_dir='.', local_dir_use_symlinks=False)
" 2>/dev/null || {
    echo "WARN: hf_hub_download failed for $path" >&2
    rm -f "$tmp"
    return
  }
  local fname=$(basename "$path")
  if [[ -f "$fname" ]]; then
    normalize_pair "$fname" | while read -r line; do
      local md5
      md5=$(echo "$line" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['_md5'])")
      if ! is_duplicate "$md5"; then
        echo "$line"
        mark_seen "$md5"
      fi
    done
    rm -f "$fname"
  fi
  rm -f "$tmp"
}

# Main
if [[ -n "${FILE_LIST_JSON:-}" && -f "$FILE_LIST_JSON" ]]; then
  echo "Using CDN mode with file list: $FILE_LIST_JSON"
  mapfile -t paths < <(python3 -c "
import json, sys
with open('$FILE_LIST_JSON', 'r') as f:
    data = json.load(f)
for item in data.get('files', []):
    print(item['path'])
")
  total=${#paths[@]}
  for i in "${!paths[@]}"; do
    path="${paths[$i]}"
    if [[ $(shard_for "$path") -eq "$SHARD_ID" ]]; then
      echo "Processing [$((i+1))/$total] (shard $SHARD_ID): $path"
      process_file_cdn "$path"
    fi
  done
else
  echo "No FILE_LIST_JSON provided; falling back to datasets library (may hit API limits)."
  python3 -c "
from huggingface_hub import list_repo_files
import sys
files = list(list_repo_files(repo_id='${REPO}'))
for f in files:
    print(f.path)
" | while read -r path;

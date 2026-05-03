# surrogate-1 / quality

## Highest-value improvement
Replace recursive/per-file authenticated HF API calls with **one non-recursive `list_repo_tree` per date folder + CDN-only fetches** during training.  
This removes 429 rate-limit risk and cuts API usage to a single Mac-side call per date, enabling Lightning Studio to train with zero HF API calls.

## Implementation plan (<2h)
1. Add `bin/list_folder_files.py` — list one date folder non-recursively and emit JSON.
2. Update `bin/dataset-enrich.sh` to use non-recursive listing and avoid `load_dataset(streaming=True)` on heterogeneous repos; fall back to per-file `hf_hub_download` + projection.
3. Add `training/train.py` stub that reads the folder list JSON and downloads via CDN (`resolve/main/...`) with zero authenticated API calls.
4. Ensure scripts are executable and have Bash shebangs (avoid cron/wrapper issues).
5. Quick smoke test: run listing locally and verify CDN download for one file.

---

## 1) bin/list_folder_files.py
```python
#!/usr/bin/env python3
"""
List files in a single date folder (non-recursive) for axentx/surrogate-1-training-pairs.
Usage:
  python list_folder_files.py --date 2026-04-29 --output files.json
"""
import argparse
import json
import os
import sys

from huggingface_hub import HfApi

REPO_ID = "axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser(description="List files in a date folder (non-recursive).")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-04-29")
    parser.add_argument("--output", default="files.json", help="Output JSON path")
    parser.add_argument("--repo", default=REPO_ID, help="HF dataset repo")
    args = parser.parse_args()

    api = HfApi()
    folder_path = f"batches/public-merged/{args.date}"
    try:
        items = api.list_repo_tree(repo_id=args.repo, path=folder_path, recursive=False)
    except Exception as exc:
        print(f"Error listing {folder_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    files = [it.rfilename for it in items if it.type == "file"]
    out = {
        "repo": args.repo,
        "date": args.date,
        "folder": folder_path,
        "files": sorted(files),
        "count": len(files),
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print(f"Wrote {len(files)} files to {args.output}")

if __name__ == "__main__":
    main()
```

---

## 2) bin/dataset-enrich.sh (excerpt)
Ensure Bash shebang, executable, and avoid `load_dataset(streaming=True)` on heterogeneous repos.

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Per-shard worker: normalize heterogeneous files and upload shard output.
# Uses non-recursive folder listing + per-file hf_hub_download to avoid
# recursive list_repo_files and mixed-schema streaming issues.

set -euo pipefail
export SHELL=/bin/bash

REPO="axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
HF_TOKEN="${HF_TOKEN:-}"

WORKDIR=$(mktemp -d)
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

# 1) Non-recursive file list for this date (single API call)
python3 "$(dirname "$0")/list_folder_files.py" \
  --date "$DATE" \
  --output "$WORKDIR/files.json" \
  --repo "$REPO"

mapfile -t FILES < <(jq -r '.files[]' "$WORKDIR/files.json")
TOTAL_FILES=${#FILES[@]}
if (( TOTAL_FILES == 0 )); then
  echo "No files for $DATE. Exiting."
  exit 0
fi

# 2) Deterministic shard assignment by slug-hash
shard_files=()
for f in "${FILES[@]}"; do
  slug=$(basename "$f" | sed 's/\.[^.]*$//')
  # simple deterministic hash -> bucket
  h=$(echo -n "$slug" | md5sum | cut -c1-8)
  b=$(( 0x$h % TOTAL_SHARDS ))
  if (( b == SHARD_ID )); then
    shard_files+=("$f")
  fi
done

echo "Shard $SHARD_ID processing ${#shard_files[@]}/${TOTAL_FILES} files for $DATE"

# 3) Process each assigned file: download individually and project to {prompt,response}
OUT="$WORKDIR/shard-${SHARD_ID}-$(date +%H%M%S).jsonl"
> "$OUT"

for rel in "${shard_files[@]}"; do
  echo "Processing $rel"
  tmp=$(mktemp "$WORKDIR/file.XXXXXX")
  # Use hf_hub_download (authenticated) or CDN fallback for public files
  if [[ -n "$HF_TOKEN" ]]; then
    huggingface-cli download "$REPO" "$rel" --cache-dir "$WORKDIR/cache" --local-dir-use-symlinks False --local-dir "$WORKDIR" 2>/dev/null || true
    src="$WORKDIR/$rel"
  else
    src="$WORKDIR/cache/$rel"
  fi

  # If download failed, try CDN (public) as fallback
  if [[ ! -f "$src" ]]; then
    mkdir -p "$(dirname "$src")"
    curl -fsSL "https://huggingface.co/datasets/${REPO}/resolve/main/${rel}" -o "$src" || {
      echo "Failed to fetch $rel"; continue
    }
  fi

  # Project heterogeneous formats to {prompt,response} here.
  # Minimal example: assume line-delimited JSON with possible fields.
  # Replace with your schema-specific projection logic.
  python3 - "$src" "$OUT" <<'PY'
import json, sys
src, out = sys.argv[1], sys.argv[2]
try:
    with open(src, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            # Projection: pick or construct prompt/response
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
            response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
            if prompt and response:
                with open(out, "a", encoding="utf-8") as o:
                    json.dump({"prompt": prompt, "response": response}, o, ensure_ascii=False)
                    o.write("\n")
except Exception:
    pass
PY

  rm -f "$src"
done

# 4) Upload shard output (deterministic filename prevents collisions)
if [[ -s "$OUT" ]]; then
  echo "Uploading shard output"
  huggingface-cli upload "$REPO" "$OUT" "batches/public-merged/$DATE/$(basename "$OUT")" \
    --repo-type dataset \
    --token "$HF_TOKEN" || {
      echo "Upload failed"; exit 1
    }
else
  echo "No output for shard $SHARD_ID"
fi
```

Make executable:
```bash
chmod +x bin/dataset-enrich.sh bin/list_folder_files.py
```

---

## 3) training/train.py (CDN-only, zero API during data load)
```python
#!/usr/bin/env python3
"""
Lightning training script that uses CDN-only fetches.
Expects a folder list JSON produced by list_folder_files.py.
"""
import json
import os
import sys
from pathlib import Path

import torch
from torch.utils.data import Dataset, DataLoader
from transformers import AutoTokenizer, AutoModelForCausalLM, Trainer, TrainingArguments

HF_REPO = "axentx/surrogate-1-training-pairs"

class CDNTextDataset(Dataset):
    def __init__(self, folder_json: str, max_files: int = -1):
        with open(folder_json, "r")

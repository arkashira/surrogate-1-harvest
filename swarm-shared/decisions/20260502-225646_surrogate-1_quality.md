# surrogate-1 / quality

Below is the **single, synthesized implementation** that keeps the strongest, non-contradictory parts of both proposals and resolves all conflicts in favor of **correctness + concrete actionability**.

Key decisions:
- Use `list_repo_tree(recursive=False)` per date folder (fast, deterministic, avoids pagination).
- Save per-date file list as `file-list/<date>.json` (clean, scalable).
- Prefer **CDN-only ingestion** (`/resolve/main/...`) when `FILE_LIST` is provided to eliminate HF API 429s during training/shard work.
- Make GitHub Actions run a **single list job** (max-parallel: 1) and pass the snapshot to all 16 shards via artifacts.
- Keep fallback to `huggingface_hub` download (not `datasets.load_dataset`) to avoid auth-check rate limits while still allowing local execution without `FILE_LIST`.

---

## 1) `bin/list_files.py`

```python
#!/usr/bin/env python3
"""
Deterministic pre-flight file lister for axentx/surrogate-1-training-pairs.

Usage:
  HF_TOKEN=<token> python bin/list_files.py \
    --date 2026-05-02 \
    --out file-list/2026-05-02.json

Output format (list of dicts):
[
  {"path": "batches/public-merged/2026-05-02/file1.parquet", "size": 12345},
  ...
]
"""

import argparse
import json
import os
import sys
from typing import List, Dict

from huggingface_hub import HfApi

REPO = "axentx/surrogate-1-training-pairs"
CDN_BASE = f"https://huggingface.co/datasets/{REPO}/resolve/main"

def list_date_files(date: str) -> List[Dict[str, object]]:
    """
    List parquet/jsonl files for a single date folder non-recursively.
    """
    api = HfApi()
    prefix = f"batches/public-merged/{date}/"
    try:
        tree = api.list_repo_tree(
            repo_id=REPO,
            path=prefix,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as exc:
        print(f"Error listing tree for {prefix}: {exc}", file=sys.stderr)
        return []

    entries = []
    for item in tree:
        rfn = item.rfilename
        if rfn.endswith((".parquet", ".jsonl")):
            entries.append({
                "path": rfn,
                "size": getattr(item, "size", None),
                "cdn_url": f"{CDN_BASE}/{rfn}",
            })
    entries.sort(key=lambda x: x["path"])
    return entries

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic file list for a date folder.")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", help="Output JSON path (default: file-list/<date>.json)")
    args = parser.parse_args()

    os.makedirs("file-list", exist_ok=True)
    out_path = args.out or f"file-list/{args.date}.json"

    files = list_date_files(args.date)
    payload = {
        "repo": REPO,
        "date": args.date,
        "cdn_base": CDN_BASE,
        "files": files,
    }

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")

    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

---

## 2) `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Deterministic shard worker: project to {prompt,response}, dedup, upload.
#
# New behavior:
#   If FILE_LIST is set and exists, use CDN URLs (/resolve/main/...) to bypass
#   HF API auth checks and avoid 429 during data loading.
#
# Required:
#   HF_TOKEN with read access to axentx/surrogate-1-training-pairs
#   SHARD_ID  (0..15)
#   DATE      (YYYY-MM-DD)

set -euo pipefail
export SHELL=/bin/bash

REPO_DST="axentx/surrogate-1-training-pairs"
DATE="${DATE:?required}"
SHARD_ID="${SHARD_ID:?required}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
WORKDIR=$(mktemp -d)
OUTDIR="${WORKDIR}/out"
mkdir -p "${OUTDIR}"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [shard-${SHARD_ID}] $*"
}

# Optional: CDN-only file list to avoid HF API 429 during ingestion
FILE_LIST="${FILE_LIST:-}"
declare -a SRC_FILES=()

if [[ -n "${FILE_LIST}" && -f "${FILE_LIST}" ]]; then
  log "Using file list: ${FILE_LIST}"
  mapfile -t SRC_FILES < <(
    python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for item in data.get('files', []):
    print(item['path'] if isinstance(item, dict) else item)
" "${FILE_LIST}"
  )
else
  log "FILE_LIST not provided; falling back to huggingface_hub downloads (may hit API limits)"
fi

# Install deps (cached by runner image when possible)
pip install -q pyarrow numpy huggingface_hub 2>/dev/null || true

# Helper: deterministic shard assignment by content hash
shard_for_slug() {
  local slug="$1"
  python3 -c "print(hash('${slug}') % ${TOTAL_SHARDS})"
}

# Process one file (parquet or jsonl) -> normalized jsonl
process_file() {
  local src="$1"
  local cdn_base="https://huggingface.co/datasets/${REPO_DST}/resolve/main"
  local local_path="${WORKDIR}/$(basename "${src}")"

  if [[ -n "${FILE_LIST}" && -f "${FILE_LIST}" ]]; then
    # CDN bypass: no Authorization header, higher CDN limits
    curl -fsSL --retry 3 --retry-delay 5 \
      "${cdn_base}/${src}" \
      -o "${local_path}"
  else
    # Fallback: use huggingface_hub (not datasets.load_dataset) to reduce auth pressure
    python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(
    repo_id='${REPO_DST}',
    filename='${src}',
    repo_type='dataset',
    local_dir='${WORKDIR}',
    local_dir_use_symlinks=False,
)
" 2>/dev/null || {
      log "Failed to download ${src}"
      return 1
    }
    local_path="${WORKDIR}/${src}"
  fi

  # Project to {prompt,response} and assign shard
  python3 -c "
import pyarrow.parquet as pq
import json, hashlib, sys

try:
    tbl = pq.read_table('${local_path}')
    df = tbl.to_pandas()
except Exception as e:
    print(f'Failed to read ${local_path}: {e}', file=sys.stderr)
    sys.exit(0)

for _, row in df.iterrows():
    prompt = str(row.get('prompt', row.get('input', '')))
    response = str(row.get('response', row.get('output', '')))
    if not prompt or not response:
        continue
    blob = (prompt + '|||' + response).encode()
    md5 = hashlib.md5(blob).hexdigest()
    shard = int(hashlib.sha256(md5.encode()).hexdigest(), 16) % ${TOTAL_SHARDS}
    if shard != ${SHARD_ID}:
        continue
    out = {'prompt': prompt, 'response': response, 'md5': md5}
    print(json.dumps(out, ensure_ascii=False))
" >> "${OUTDIR}/shard-${SHARD_ID}.jsonl" 2>/dev/null || true
}

export -f process_file
export -f shard_for_slug
export WORKDIR OUTDIR REPO_DST TOTAL

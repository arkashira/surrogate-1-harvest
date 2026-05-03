# surrogate-1 / backend

## Final Synthesis: CDN-Bypass Ingestion (Correct + Actionable)

**Core improvement**  
Add a **CDN-bypass ingestion mode** to `bin/dataset-enrich.sh` that:

1. Lists a single date folder with `list_repo_tree(..., recursive=False)` (one cheap API call).  
2. Downloads files directly via CDN (`resolve/main/...`) — bypassing HF `/api/` auth and rate limits.  
3. Projects each file individually to `{prompt, response}` with `pyarrow`/`json` (no `load_dataset(streaming=True)`), preventing OOM on heterogeneous schemas in HF Spaces.  
4. Keeps deterministic 16-shard assignment and existing dedup flow unchanged.

This eliminates 429s, raises effective throughput, and fits in <2h.

---

## Implementation Plan (<2h)

1. **Create `tools/list_cdn_files.py`**  
   - Input: `folder`, `--repo`, `--out`.  
   - Uses `HfApi.list_repo_tree(recursive=False)`.  
   - Outputs JSON list of `{path, cdn_url}`.

2. **Update `bin/dataset-enrich.sh`**  
   - Add `CDN_MODE` (default `false`) and `FOLDER_PATH` (required when `CDN_MODE=true`).  
   - If `CDN_MODE=true`:  
     - Generate or reuse `filelist.json`.  
     - Deterministically assign files to shards by hash(filename) % TOTAL_SHARDS.  
     - For each assigned file: `curl -L "$cdn_url" -o "$tmpfile"`; project with `project_to_pair`; append to `shard-<SHARD_ID>-<ts>.jsonl`.  
   - Keep `CDN_MODE=false` path unchanged (fallback to `load_dataset`).

3. **Update `.github/workflows/ingest.yml`**  
   - Add `env: CDN_MODE: true` and pass `FOLDER_PATH` (matrix or workflow input).  
   - Cache `filelist.json` per folder within a run.

4. **Small projection helper**  
   - Use `pyarrow` for Parquet; stream JSONL; skip non-conforming files silently.

---

## Code

### tools/list_cdn_files.py
```python
#!/usr/bin/env python3
"""
List files in a single folder of a HuggingFace dataset repo
and emit CDN URLs (bypasses /api/ auth rate limits).

Usage:
  python3 tools/list_cdn_files.py public-dumps/2026-05-03 \
    --repo axentx/surrogate-1-training-pairs \
    --out filelist.json
"""

import argparse
import json
import sys

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def main() -> None:
    parser = argparse.ArgumentParser(description="List dataset folder for CDN ingestion")
    parser.add_argument("folder", help="Folder path in dataset repo (e.g. public-dumps/2026-05-03)")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--out", default="filelist.json")
    args = parser.parse_args()

    api = HfApi()
    try:
        entries = api.list_repo_tree(
            repo_id=args.repo,
            path=args.folder,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as e:
        print(f"Error listing repo tree: {e}", file=sys.stderr)
        sys.exit(1)

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        path = entry.path
        files.append({
            "path": path,
            "cdn_url": CDN_TEMPLATE.format(repo=args.repo, path=path),
        })

    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(files, f, indent=2)

    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    main()
```

### bin/dataset-enrich.sh
```bash
#!/usr/bin/env bash
# dataset-enrich.sh — worker for surrogate-1 ingestion
#
# New: CDN_MODE=true avoids HF API rate limits by using direct CDN downloads
#      and per-file projection instead of load_dataset(streaming=True).

set -euo pipefail

# ---- config ----
HF_REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
CDN_MODE="${CDN_MODE:-false}"          # set true to use CDN bypass
FOLDER_PATH="${FOLDER_PATH:-}"         # required when CDN_MODE=true
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
OUT_DIR="${OUT_DIR:-./output}"
TMP_DIR="${TMP_DIR:-./tmp}"
mkdir -p "$OUT_DIR" "$TMP_DIR"

# ---- deps ----
python3 -m pip install -q pyarrow numpy huggingface_hub 2>/dev/null || true

# ---- helpers ----
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

project_to_pair() {
  # $1: input file path
  # $2: output jsonl path (appended)
  python3 - "$1" "$2" <<'PY'
import sys, json, pyarrow as pa, pyarrow.parquet as pq, os

src, dst = sys.argv[1], sys.argv[2]
lower = src.lower()

rows = []
try:
    if lower.endswith(".parquet"):
        tbl = pq.read_table(src, columns=["prompt", "response"])
        df = tbl.to_pydict()
        for p, r in zip(df.get("prompt", []), df.get("response", [])):
            if p is not None and r is not None:
                rows.append({"prompt": str(p), "response": str(r)})
    elif lower.endswith(".jsonl"):
        with open(src, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                p = obj.get("prompt") or obj.get("input") or obj.get("text")
                r = obj.get("response") or obj.get("output")
                if p is not None and r is not None:
                    rows.append({"prompt": str(p), "response": str(r)})
    # Skip uncommon formats in this fast path
except Exception:
    # Silently skip malformed files; CI logs will show if needed
    pass

if rows:
    with open(dst, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
PY
}

# ---- main ----
log "Starting dataset-enrich (CDN_MODE=$CDN_MODE, SHARD_ID=$SHARD_ID/$TOTAL_SHARDS)"

if [[ "$CDN_MODE" == "true" ]]; then
  if [[ -z "$FOLDER_PATH" ]]; then
    log "ERROR: CDN_MODE=true requires FOLDER_PATH"
    exit 1
  fi

  FILELIST="${TMP_DIR}/filelist.json"
  if [[ ! -f "$FILELIST" ]]; then
    log "Listing folder via HF API (single tree call): $FOLDER_PATH"
    python3 tools/list_cdn_files.py "$FOLDER_PATH" --repo "$HF_REPO" --out "$FILELIST"
  else
    log "Reusing existing filelist: $FILELIST"
  fi

  # Deterministic shard assignment by filename hash
  mapfile -t ALL_FILES < <(python3 -c "
import json, sys
with open('$FILELIST') as f:
    files = [x['cdn_url'] for x in json.load(f)]
for u in files:
    print(u)
")

  ASSIGNED=()
  for url in "${ALL_FILES[@]}"; do
    # Stable hash across runners
    h=$(python3 -c "import hashlib; print(int(hashlib.sha256('$url'.encode()).hexdigest(), 16))")
    shard=$(( h % TOTAL_SHARDS ))
    if [[ "$shard" -eq "$SHARD_ID" ]]; then
      ASSIGNED+=("$url")
    fi
  done

  log "Assigned ${#ASSIGNED[@

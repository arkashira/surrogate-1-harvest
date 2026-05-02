# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### What we’ll do
1. Add `bin/list-files.py` — one-time Mac/CI script that calls `list_repo_tree` once per date folder and writes `file-list.json` (path + size + sha256). Embed this list in training scripts so Lightning workers do **zero** HF API calls during data loading.
2. Update `bin/dataset-enrich.sh` to accept an optional `FILE_LIST` env var; if present, iterate the local list and fetch via CDN (`/resolve/main/...`) instead of `load_dataset`/`list_repo_files`.
3. Add `bin/train-cdn.sh` launcher that injects the file-list into training and a `bin/train-cdn.py` skeleton showing how to consume `file-list.json` in Lightning training with `requests`/`urllib` + `pyarrow` (streaming decode) — no `datasets` library during epoch reads.

Total diff: ~180 lines across 4 files. All changes are additive and backward-compatible.

---

### 1) `bin/list-files.py`

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in axentx/surrogate-1-training-pairs.
Run from Mac/CI after rate-limit window clears. Produces file-list.json for workers.

Usage:
  HF_TOKEN=hf_xxx python bin/list-files.py --date 2026-05-02 --out file-list.json
"""

import argparse
import hashlib
import json
import os
import sys
from typing import List, Dict

from huggingface_hub import HfApi

REPO_ID = "axentx/surrogate-1-training-pairs"

def list_date_folder(date: str, api: HfApi) -> List[Dict]:
    """
    Use list_repo_tree per folder (non-recursive) to avoid 100x pagination.
    Returns list of dicts: {"path": "...", "size": int, "sha256": str|None}
    """
    prefix = f"batches/public-merged/{date}/"
    entries = api.list_repo_tree(
        repo_id=REPO_ID,
        path=prefix.rstrip("/"),
        repo_type="dataset",
        recursive=False,
    )

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        try:
            meta = api.get_paths_info(
                repo_id=REPO_ID,
                paths=[entry.path],
                repo_type="dataset",
            )
            info = meta[0] if meta else None
            size = getattr(info, "size", None)
        except Exception:
            size = None

        files.append({
            "path": entry.path,
            "size": size,
            "sha256": None,
        })

    files.sort(key=lambda x: x["path"])
    return files

def main() -> None:
    parser = argparse.ArgumentParser(description="List files for date folder (CDN ingest).")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--out", default="file-list.json", help="Output JSON path")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        print("ERROR: HF_TOKEN env var required", file=sys.stderr)
        sys.exit(1)

    api = HfApi(token=token)

    try:
        files = list_date_folder(args.date, api)
    except Exception as exc:
        print(f"ERROR listing folder: {exc}", file=sys.stderr)
        sys.exit(1)

    payload = {
        "repo": REPO_ID,
        "date": args.date,
        "generated_by": "bin/list-files.py",
        "files": files,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} entries to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-files.py
```

---

### 2) Update `bin/dataset-enrich.sh` (CDN-aware mode)

Add a lightweight CDN fetch mode. If `FILE_LIST` is set, workers read the local JSON and download via raw CDN URLs (no `datasets`/API calls). Keeps existing behavior when `FILE_LIST` is unset.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated: CDN-only mode to avoid HF API 429s.

set -euo pipefail

# Existing env defaults
HF_TOKEN="${HF_TOKEN:-}"
REPO_ID="axentx/surrogate-1-training-pairs"
OUTPUT_DIR="${OUTPUT_DIR:-./enriched}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
FILE_LIST="${FILE_LIST:-}"   # NEW: path to file-list.json (optional)

mkdir -p "$OUTPUT_DIR"

# Central dedup store (existing)
DEDUP_STORE="${DEDUP_STORE:-/tmp/dedup.db}"
python3 -c "import lib.dedup; lib.dedup.init_db('$DEDUP_STORE')" 2>/dev/null || true

# ---- NEW: CDN mode ----
if [[ -n "$FILE_LIST" && -f "$FILE_LIST" ]]; then
  echo "INFO: CDN mode enabled via FILE_LIST=$FILE_LIST"
  python3 - "$FILE_LIST" "$SHARD_ID" "$TOTAL_SHARDS" "$OUTPUT_DIR" "$DEDUP_STORE" <<'PYTHON_CDN'
import json
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except Exception as e:
    print("ERROR: pyarrow required for CDN mode", file=sys.stderr)
    sys.exit(1)

def deterministic_shard(path: str, total: int) -> int:
    h = hashlib.md5(path.encode()).hexdigest()
    return int(h, 16) % total

def download_cdn(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "axentx-surrogate/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

def extract_pairs_from_parquet(data: bytes):
    table = pq.read_table(pa.BufferReader(data))
    cols = {}
    for name in ("prompt", "response"):
        if name in table.column_names:
            cols[name] = table[name].to_pylist()
        else:
            aliases = {
                "prompt": ("instruction", "question", "input"),
                "response": ("completion", "answer", "output"),
            }
            for cand in aliases[name]:
                if cand in table.column_names:
                    cols[name] = table[cand].to_pylist()
                    break
            else:
                cols[name] = [None] * len(table)
    for p, r in zip(cols["prompt"], cols["response"]):
        if p and r and isinstance(p, str) and isinstance(r, str):
            yield p.strip(), r.strip()

def main():
    file_list_path, shard_id_str, total_shards_str, out_dir, dedup_db = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
    shard_id = int(shard_id_str)
    total_shards = int(total_shards_str)

    with open(file_list_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    files = payload.get("files", [])
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"shard{shard_id}.jsonl")

    # Deterministic shard assignment
    my_files = [f for f in files if deterministic_shard(f["path"], total_shards) == shard_id]
    print(f"Shard {shard_id}/{total_shards}: processing {len(my_files)} files")

    seen = set()
    written = 0
    for meta in my_files:
        path = meta["path"]
        cdn_url = f"https://hug

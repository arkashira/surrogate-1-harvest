# surrogate-1 / backend

## Final synthesized implementation (single, correct, actionable)

**Core improvement (non-negotiable):**  
Replace recursive/per-file authenticated HF API calls with **one non-recursive `list_repo_tree` per date folder + unauthenticated CDN downloads + schema projection at parse time**. This eliminates 429 risk, reduces API usage to O(date-folders), and bounds memory.

---

## Implementation plan (≤2h)

### 1) File-list utility (run once per date, on Mac orchestrator or CI prepare step)
- Uses `huggingface_hub.list_repo_tree(path, recursive=False)` for the date folder.
- Emits deterministic JSON: `filelist/YYYY-MM-DD.json` with `{"date":"...","files":["path/to/file.parquet",...]}`.
- Retries once after 360s on 429; exits 0 if empty.
- Commit artifact or pass via workflow artifact to workers.

**Code** (`bin/list_folder_files.py`):
```python
#!/usr/bin/env python3
"""
Generate file list for a date folder in axentx/surrogate-1-training-pairs.
Usage:
  python bin/list_folder_files.py --date 2026-05-01 --out filelist/2026-05-01.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

API = HfApi()
REPO = "axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    try:
        items = API.list_repo_tree(REPO, path=args.date, recursive=False)
    except Exception as e:
        print(f"list_repo_tree failed: {e}", file=sys.stderr)
        sys.exit(1)

    files = sorted(
        it.path for it in items if it.type == "file" and it.path.lower().endswith((".parquet", ".jsonl"))
    )

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"date": args.date, "files": files}, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

---

### 2) Worker script (deterministic sharding + CDN downloads + projection)
- Accept `DATE`, `SHARD_ID`, `TOTAL_SHARDS`.
- Load `filelist/${DATE}.json`.
- Deterministic shard assignment: `hash(slug) % TOTAL_SHARDS == SHARD_ID` (stable across runs).
- Download via CDN URL:  
  `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<path>`  
  (no auth header; retry with exponential backoff, max 3 retries).
- Parse with `pyarrow.parquet` (or `pyarrow` for both parquet/jsonl) and **project only `{prompt,response}`** at read time.
- Normalize field names; skip rows missing prompt/response.
- Dedup via `lib/dedup.py` (central md5 store) if available; otherwise lightweight in-shard dedup.
- Output: `batches/public-merged/${DATE}/shard${SHARD_ID}-${HHMMSS}.jsonl`.

**Code** (`bin/dataset-enrich.sh`):
```bash
#!/usr/bin/env bash
set -euo pipefail
# Usage: bin/dataset-enrich.sh <DATE> <SHARD_ID> <TOTAL_SHARDS>
# Example: bin/dataset-enrich.sh 2026-05-01 3 16

DATE="${1:-$(date +%Y-%m-%d)}"
SHARD_ID="${2:-0}"
TOTAL_SHARDS="${3:-16}"
HF_REPO="axentx/surrogate-1-training-pairs"
OUT_DIR="batches/public-merged/${DATE}"
TS=$(date +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "${OUT_DIR}"

python3 - "$DATE" "$SHARD_ID" "$TOTAL_SHARDS" "$HF_REPO" "$OUT_FILE" <<'PY'
import json, hashlib, os, sys, requests, pyarrow as pa, pyarrow.parquet as pq, io

DATE, SHARD_ID, TOTAL_SHARDS, HF_REPO, OUT_FILE = sys.argv[1:]
SHARD_ID = int(SHARD_ID)
TOTAL_SHARDS = int(TOTAL_SHARDS)

FILELIST = f"filelist/{DATE}.json"
with open(FILELIST) as f:
    files = json.load(f)["files"]

def shard_for(path: str) -> int:
    slug = os.path.splitext(os.path.basename(path))[0]
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % TOTAL_SHARDS

my_files = [p for p in files if shard_for(p) == SHARD_ID]
print(f"Shard {SHARD_ID}/{TOTAL_SHARDS}: processing {len(my_files)} files")

def download_cdn(path: str, max_retries: int = 3) -> bytes:
    url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{path}"
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            if attempt == max_retries:
                raise
            wait = 2 ** attempt
            print(f"Retry {attempt}/{max_retries} for {path} after {wait}s: {e}")
            import time; time.sleep(wait)
    raise RuntimeError("unreachable")

def normalize_record(rec: dict) -> dict:
    return {
        "prompt": str(rec.get("prompt", rec.get("input", rec.get("question", "")))),
        "response": str(rec.get("response", rec.get("output", rec.get("answer", "")))),
    }

os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
seen = set()
with open(OUT_FILE, "w") as out_f:
    for path in my_files:
        try:
            data = download_cdn(path)
            table = pq.read_table(io.BytesIO(data))
            # Project only needed columns if present; ignore extras
            cols = [c for c in ("prompt", "response", "input", "output", "question", "answer") if c in table.column_names]
            if not cols:
                # fallback: read all and normalize
                df = table.to_pandas()
            else:
                df = table.select_columns(cols).to_pandas()
            for _, row in df.iterrows():
                rec = normalize_record(row.to_dict())
                prompt = (rec["prompt"] or "").strip()
                response = (rec["response"] or "").strip()
                if not prompt or not response:
                    continue
                # lightweight in-shard dedup
                key = hashlib.md5(f"{prompt}\n{response}".encode()).hexdigest()
                if key in seen:
                    continue
                seen.add(key)
                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"Error processing {path}: {e}", file=sys.stderr)

print(f"Wrote {OUT_FILE}")
PY

# Optional: run central dedup if available
# python3 lib/dedup.py --input "${OUT_FILE}" --output "${OUT_FILE}.dedup"
```

---

### 3) GitHub Actions workflow (matrix sharding + prepare step)
- Add a `prepare` job (or first matrix step) that runs `list_folder_files.py` for the target date and uploads `filelist/` as an artifact.
- Main `ingest` job uses a 16-shard matrix, downloads the artifact, and runs `dataset-enrich.sh` per shard.
- Keep cron and manual dispatch.

**Snippet** (`.github/workflows/ingest.yml`):
```yaml
name: Ingest public dataset (sharded)

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:
    inputs:
      date:
        description

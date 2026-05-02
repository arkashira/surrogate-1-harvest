# surrogate-1 / discovery

## Final Synthesis (Correctness + Actionability)

**Core diagnosis (merged, de-duplicated):**
- No CDN bypass: workers use `datasets`/`load_dataset` streaming → authenticated HF API calls → 429 risk and commit-cap pressure.
- No deterministic file manifest: each shard re-enumerates repo files on every run → paginated API calls, retry fragility, prevents zero-API CDN-only Lightning training.
- No reuse of running compute: workflow spins 16 fresh runners every 30 min even when prior shards are still active → wasted minutes, overlapping runs, and duplicate uploads.
- No cross-run dedup visibility: each job starts with empty local cache → duplicate uploads across runs and wasted write quota against HF dataset commit cap.
- Schema heterogeneity per date folder can still cause per-shard `pyarrow.CastError` if shard-to-date mapping is not pinned.

**Single proposed change (merged, prioritized):**
Add a “pre-list + CDN-only + sticky-shards” ingestion path:
- Add `bin/list-paths.py` (run once per date folder) → produces deterministic `file-list-<date>.json`.
- Update `bin/dataset-enrich.sh` to accept optional `FILE_LIST`; if provided, skip `list_repo_files` and stream files via raw CDN URLs with schema-safe projection and local dedup.
- Update `.github/workflows/ingest.yml` to:
  - generate/upload `file-list-<date>.json` as an artifact,
  - pass the date and file-list to each shard,
  - pin each shard to a deterministic date-folder list,
  - use sticky concurrency + `concurrency.cancel-in-progress=false` to reuse running shards and avoid overlapping 30-min spins,
  - persist a small local Bloom/HashSet per workflow run to reduce cross-run duplicates within the same run.
- Keep the central dedup store (HF Space SQLite) as source-of-truth; local cache is best-effort per-run only.

---

## Implementation

### 1. Deterministic path listing (single tree call per date)
`bin/list-paths.py`
```python
#!/usr/bin/env python3
"""
list-paths.py
Usage:
  HF_TOKEN=... python bin/list-paths.py <date> > file-list-<date>.json
"""
import json
import os
import sys
from huggingface_hub import HfApi

def main():
    if len(sys.argv) < 2:
        print("Usage: list-paths.py <date>", file=sys.stderr)
        sys.exit(1)

    repo_id = "datasets/axentx/surrogate-1-training-pairs"
    date_folder = sys.argv[1]
    api = HfApi(token=os.environ.get("HF_TOKEN"))

    # Single non-recursive tree call per date folder
    tree = api.list_repo_tree(repo_id, path=date_folder, recursive=False)
    files = sorted(f.rfilename for f in tree if f.type == "file")

    result = {"date": date_folder, "files": files}
    json.dump(result, sys.stdout, separators=(",", ":"))

if __name__ == "__main__":
    main()
```
Make executable:
```bash
chmod +x bin/list-paths.py
```

---

### 2. Worker script with CDN-only ingestion, schema-safe projection, and local dedup
`bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# If FILE_LIST is set, use CDN-only fetches (no datasets streaming API).
# Otherwise fall back to legacy behavior.

set -euo pipefail

REPO="datasets/axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date +%Y-%m-%d)}"
OUTPUT_DIR="batches/public-merged/${DATE}"
SHARD_ID="${SHARD_ID:-0}"
N_SHARDS="${N_SHARDS:-16}"
FILE_LIST="${FILE_LIST:-}"
TIMESTAMP="${TIMESTAMP:-$(date +%s)}"

mkdir -p "$OUTPUT_DIR"

python3 - "$REPO" "$DATE" "$SHARD_ID" "$N_SHARDS" "$FILE_LIST" "$OUTPUT_DIR" "$TIMESTAMP" <<'PY'
import json, os, sys, hashlib, pathlib, requests, io
from typing import List, Dict, Any

REPO = sys.argv[1]
DATE = sys.argv[2]
SHARD_ID = int(sys.argv[3])
N_SHARDS = int(sys.argv[4])
FILE_LIST = sys.argv[5] or None
OUT_DIR = sys.argv[6]
TIMESTAMP = sys.argv[7]

HF_TOKEN = os.environ.get("HF_TOKEN")
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

def deterministic_shard(key: str, n: int) -> int:
    return int(hashlib.md5(key.encode()).hexdigest(), 16) % n

def cdn_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/main/{path}"

def load_file_list(date: str) -> List[str]:
    if FILE_LIST and pathlib.Path(FILE_LIST).exists():
        with open(FILE_LIST) as f:
            data = json.load(f)
        return [p for p in data.get("files", []) if p.startswith(date + "/")]
    # Fallback: single non-recursive tree call
    from huggingface_hub import HfApi
    api = HfApi(token=os.environ.get("HF_TOKEN"))
    tree = api.list_repo_tree(REPO, path=date, recursive=False)
    return sorted(f.rfilename for f in tree if f.type == "file")

def project_to_pair(raw_bytes: bytes, filename: str) -> List[Dict[str, str]]:
    pairs = []
    # 1) Try parquet
    try:
        import pyarrow.parquet as pq
        table = pq.read_table(io.BytesIO(raw_bytes))
        cols = table.column_names
        prompt_col = next((c for c in ("prompt", "instruction", "input") if c in cols), None)
        response_col = next((c for c in ("response", "output", "completion") if c in cols), None)
        if prompt_col and response_col:
            for batch in table.to_batches():
                for i in range(batch.num_rows):
                    prompt = str(batch[prompt_col][i].as_py())
                    response = str(batch[response_col][i].as_py())
                    if isinstance(prompt, str) and isinstance(response, str) and prompt.strip() and response.strip():
                        pairs.append({"prompt": prompt.strip(), "response": response.strip()})
            return pairs
    except Exception:
        pass

    # 2) Try JSON/JSONL
    try:
        text = raw_bytes.decode("utf-8")
        for line in text.strip().splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            prompt = obj.get("prompt") or obj.get("instruction") or obj.get("input")
            response = obj.get("response") or obj.get("output") or obj.get("completion")
            if isinstance(prompt, str) and isinstance(response, str) and prompt.strip() and response.strip():
                pairs.append({"prompt": prompt.strip(), "response": response.strip()})
        if pairs:
            return pairs
    except Exception:
        pass

    # 3) Coarse raw-text fallback (avoid unless necessary)
    try:
        text = raw_bytes.decode("utf-8")
        parts = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
        if len(parts) >= 2:
            pairs.append({"prompt": parts[0], "response": parts[1]})
    except Exception:
        pass

    return pairs

def main():
    files = load_file_list(DATE)
    if not files:
        print("No files found for date:", DATE)
        return

    out_path = pathlib.Path(OUT_DIR) / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Local run-level dedup (best-effort to reduce cross-run duplicates within this workflow)
    seen_hashes = set()

    written = 0
    for fpath in sorted(files):
        if deterministic_shard(fpath, N_SHARDS) != SHARD_ID:
            continue
        url = cdn_url(REPO, fpath)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            pairs

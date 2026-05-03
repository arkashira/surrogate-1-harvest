# surrogate-1 / frontend

## Final Synthesis (Best of Both Candidates)

**Chosen improvement**: Replace recursive `list_repo_files` and per-file API calls with **one per-folder `list_repo_tree` + CDN-only fetches**, and project to `{prompt,response}` at parse time.

**Why this is highest value (≤2h)**:
- Eliminates recursive pagination (100× API calls) that causes 429s.
- CDN `/resolve/main/...` bypasses `/api/` auth and has much higher rate limits.
- Avoids `load_dataset(streaming=True)` schema/cast errors on heterogeneous parquet.
- Single manifest enables zero-API ingestion and stable Lightning training.
- Keeps 16-shard parallelism intact while making ingestion robust.

---

## Implementation Plan (≤2h)

1. **Add manifest generator** (`bin/build-manifest.py`)  
   - Run on Mac (or cron/Actions) after HF API rate-limit window clears.  
   - Calls `list_repo_tree(path, recursive=False)` per date folder.  
   - Emits `manifests/{date_slug}/files.json` with `{ "path": "...", "size": ... }`.

2. **Update `bin/dataset-enrich.sh`**  
   - Accept optional manifest file arg.  
   - If manifest exists, read file list from it; else fallback to current behavior.  
   - Replace `load_dataset(..., streaming=True)` with direct CDN download + parquet projection to `{prompt,response}`.

3. **Add lightweight parquet projector** (`lib/project_parquet.py`)  
   - Reads parquet from bytes.  
   - Selects only `prompt`/`response` (best-effort field mapping per schema).  
   - Emits normalized JSONL lines with deterministic hash for stable sharding.

4. **Update worker to use CDN URLs**  
   - Build URLs: `https://huggingface.co/datasets/{repo}/resolve/main/{path}`.  
   - No `Authorization` header (bypasses API auth limits).  
   - Retry with exponential backoff on 429/5xx (CDN tier rarely rate-limits).

5. **Validation & smoke test**  
   - Run one shard locally with a small manifest.  
   - Confirm zero `datasets` library usage during fetch and no schema errors.

---

## Code Snippets

### 1) Manifest builder (`bin/build-manifest.py`)

```python
#!/usr/bin/env python3
"""
Build per-folder manifest for surrogate-1 dataset using list_repo_tree.
Run from Mac/cron after HF API rate-limit window clears.
"""
import json, os, hashlib
from pathlib import Path
from huggingface_hub import HfApi

API = HfApi()
REPO = "datasets/axentx/surrogate-1-training-pairs"
OUT_DIR = Path(__file__).parent.parent / "manifests"

def build_manifest(date_folder: str):
    # date_folder like "public-merged/2026-04-30"
    entries = API.list_repo_tree(REPO, path=date_folder, recursive=False)
    files = []
    for e in entries:
        if e.type != "file":
            continue
        files.append({
            "path": f"{date_folder}/{e.path.split('/')[-1]}",
            "size": getattr(e, "size", None),
        })
    out_path = OUT_DIR / date_folder.replace("/", "_") / "files.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(files, indent=2))
    print(f"Wrote {len(files)} entries to {out_path}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: build-manifest.py <date-folder>")
        sys.exit(1)
    build_manifest(sys.argv[1])
```

### 2) Lightweight parquet projector (`lib/project_parquet.py`)

```python
import pyarrow.parquet as pq
import pyarrow as pa
import io
import hashlib
import json

CANDIDATE_PROMPT = {"prompt", "instruction", "input", "question"}
CANDIDATE_RESPONSE = {"response", "output", "answer", "completion"}

def normalize_record(rec: dict) -> dict | None:
    # Best-effort pick prompt/response
    prompt_keys = [k for k in rec if k in CANDIDATE_PROMPT]
    response_keys = [k for k in rec if k in CANDIDATE_RESPONSE]

    prompt = rec[prompt_keys[0]] if prompt_keys else rec.get("prompt", "")
    response = rec[response_keys[0]] if response_keys else rec.get("response", "")

    if not prompt or not response:
        return None
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def project_parquet_bytes(data: bytes):
    table = pq.read_table(io.BytesIO(data))
    for batch in table.to_batches():
        cols = batch.columns
        col_map = {name: batch.schema.get_field_index(name) for name in table.column_names if name in table.column_names}
        n = batch.num_rows
        for i in range(n):
            rec = {}
            for name, idx in col_map.items():
                try:
                    val = cols[idx][i].as_py()
                    rec[name] = val
                except Exception:
                    rec[name] = None
            nr = normalize_record(rec)
            if nr:
                nr["_hash"] = hashlib.md5(json.dumps(nr, sort_keys=True).encode()).hexdigest()
                yield nr
```

### 3) Update worker snippet inside `bin/dataset-enrich.sh` (core loop)

```bash
#!/usr/bin/env bash
set -euo pipefail

MANIFEST="${1:-}"
REPO="datasets/axentx/surrogate-1-training-pairs"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
OUT_DIR="batches/public-merged/$(date +%F)"
mkdir -p "$OUT_DIR"
TIMESTAMP=$(date +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

# If manifest provided, use it; else fallback (legacy)
if [[ -f "$MANIFEST" ]]; then
  mapfile -t FILES < <(jq -r '.[].path' "$MANIFEST" | awk -v s="$SHARD_ID" -v t="$TOTAL_SHARDS" 'NR % t == s')
else
  # Legacy: list via API (avoid in production)
  mapfile -t FILES < <(huggingface_hub list_repo_files "$REPO" --recursive | awk -v s="$SHARD_ID" -v t="$TOTAL_SHARDS" 'NR % t == s')
fi

python3 - <<PY
import os, sys, json, requests, hashlib
from lib.project_parquet import project_parquet_bytes

REPO = "datasets/axentx/surrogate-1-training-pairs"
OUT_FILE = "${OUT_FILE}"
TOTAL_SHARDS = int("${TOTAL_SHARDS}")
SHARD_ID = int("${SHARD_ID}")

def cdn_fetch(path):
    url = f"https://huggingface.co/{REPO}/resolve/main/{path}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.content

results = []
for i, path in enumerate("""${FILES[@]}""".split()):
    path = path.strip()
    if not path:
        continue
    try:
        data = cdn_fetch(path)
        for rec in project_parquet_bytes(data):
            # deterministic shard assignment by hash
            h = int(rec["_hash"], 16)
            if h % TOTAL_SHARDS != SHARD_ID:
                continue
            results.append(rec)
    except Exception as e:
        print(f"Skipping {path}: {e}", file=sys.stderr)

with open(OUT_FILE, "w") as f:
    for r in results:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

print(f"Wrote {len(results)} records to {OUT_FILE}")
PY
```

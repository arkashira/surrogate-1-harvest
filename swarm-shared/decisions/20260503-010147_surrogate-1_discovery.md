# surrogate-1 / discovery

## Highest-value incremental improvement (<2h)

**Goal**: Eliminate HF API rate-limit failures and HF Space OOM by replacing recursive `list_repo_files` and per-file API calls with **one per-folder `list_repo_tree` + CDN-only fetches**, and project to `{prompt,response}` at parse time.

**Why this is highest value**:
- Removes the 429/1000 req/5min API limit during ingestion (CDN bypass).
- Prevents OOM by avoiding `load_dataset(streaming=True)` on heterogeneous schemas.
- Fits in <2h: modify `bin/dataset-enrich.sh` and add a small Python fetcher; no infra changes.

---

## Implementation plan

1. **Pre-list once per run** (Mac/orchestrator or in the Action)  
   - Use `huggingface_hub.list_repo_tree(repo_id, path="public-raw", recursive=False)` per date folder.
   - Save `{"date": "...", "files": [...]}` to `filelist.json`.
   - Embed `filelist.json` in the runner image or pass via artifact.

2. **CDN-only fetches in workers**  
   - Replace `load_dataset(..., streaming=True)` with direct HTTP GET to:  
     `https://huggingface.co/datasets/{repo}/resolve/main/{path}`
   - No Authorization header required for public datasets → bypasses `/api/` rate limits.

3. **Schema-safe parsing**  
   - Download each file individually via CDN.
   - Parse with `pyarrow` only the columns we need; project to `{prompt, response}` at parse time.
   - Skip rows missing either field.

4. **Shard-local dedup + upload**  
   - Keep existing `lib/dedup.py` behavior (SQLite md5 store) but operate on the projected pairs.
   - Output `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` unchanged.

5. **Action + runner changes**  
   - Add step to generate `filelist.json` before matrix starts (or generate per-shard slice in each job).
   - Update `bin/dataset-enrich.sh` to use the new fetcher script.

---

## Code snippets

### 1. Folder lister (run once before matrix) — `bin/list-folders.py`

```python
#!/usr/bin/env python3
"""
Generate filelist.json for public-raw/<date> folders.
Run before the matrix so each shard uses the same snapshot.
"""
import json
import os
from datetime import datetime, timezone

from huggingface_hub import list_repo_tree

REPO = "axentx/surrogate-1-training-pairs"
OUT = "filelist.json"

def main() -> None:
    # List top-level date folders (non-recursive)
    entries = list_repo_tree(repo_id=REPO, path="public-raw", recursive=False)
    date_folders = [e for e in entries if e.type == "directory"]

    snapshot = []
    for folder in date_folders:
        date_path = folder.path  # e.g. public-raw/2026-05-03
        files = list_repo_tree(repo_id=REPO, path=date_path, recursive=True)
        file_paths = [f.path for f in files if f.type == "file"]
        snapshot.append({"date": os.path.basename(date_path), "folder": date_path, "files": file_paths})

    out_path = os.path.join(os.path.dirname(__file__), "..", OUT)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "repo": REPO,
                "snapshot": snapshot,
            },
            fh,
            indent=2,
        )
    print(f"Wrote {len(snapshot)} date folders to {out_path}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/list-folders.py
```

---

### 2. CDN fetcher + schema-safe parser — `bin/fetch_cdn.py`

```python
#!/usr/bin/env python3
"""
Download a single file via CDN and yield {prompt, response} rows.
Kept lightweight to avoid OOM and HF API calls.
"""
import json
import pyarrow as pa
import pyarrow.parquet as pq
import requests
import sys
from typing import Generator, Dict, Any

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
REPO = "axentx/surrogate-1-training-pairs"

def cdn_fetch(path: str) -> bytes:
    url = CDN_TEMPLATE.format(repo=REPO, path=path)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def extract_pairs_from_parquet(data: bytes) -> Generator[Dict[str, str], None, None]:
    table = pq.read_table(pa.BufferReader(data))
    # Best-effort projection: accept common column names
    prompt_col = next((c for c in table.column_names if "prompt" in c.lower()), None)
    response_col = next((c for c in table.column_names if "response" in c.lower()), None)

    if not prompt_col or not response_col:
        # If schema is unexpected, skip file
        return

    prompts = table[prompt_col].to_pylist()
    responses = table[response_col].to_pylist()
    for p, r in zip(prompts, responses):
        if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
            yield {"prompt": p.strip(), "response": r.strip()}

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: fetch_cdn.py <rel_path_1> [rel_path_2 ...]")
        sys.exit(1)

    for rel_path in sys.argv[1:]:
        try:
            data = cdn_fetch(rel_path)
            for pair in extract_pairs_from_parquet(data):
                print(json.dumps(pair, ensure_ascii=False))
        except Exception as exc:
            # Log to stderr but don't crash the whole shard
            print(f"SKIP {rel_path}: {exc}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/fetch_cdn.py
```

---

### 3. Updated worker script — `bin/dataset-enrich.sh` (excerpt)

Replace the `load_dataset` streaming section with:

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Inputs
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
FILELIST="${FILELIST:-filelist.json}"
OUT_DIR="batches/public-merged/$(date +%F)"
mkdir -p "$OUT_DIR"

TIMESTAMP=$(date +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

echo "Shard ${SHARD_ID}/${TOTAL_SHARDS} -> ${OUT_FILE}"

# Select deterministic slice of all files across dates
mapfile -t ALL_FILES < <(
  python3 -c "
import json, sys, itertools
with open(sys.argv[1]) as fh:
    data = json.load(fh)
files = []
for bucket in data['snapshot']:
    files.extend(bucket['files'])
for f in sorted(files):
    print(f)
" "$FILELIST" \
  | awk "NR % ${TOTAL_SHARDS} == ${SHARD_ID}"
)

echo "Processing ${#ALL_FILES[@]} files for this shard"

# Stream-parse via CDN, dedup, write
python3 bin/fetch_cdn.py "${ALL_FILES[@]}" \
  | python3 -m lib.dedup \
  > "$OUT_FILE"

echo "Wrote $(wc -l < "$OUT_FILE") pairs to ${OUT_FILE}"
```

---

### 4. Workflow change — `.github/workflows/ingest.yml` (excerpt)

Add a pre-step to generate and upload `filelist.json` as an artifact so each matrix job uses the same snapshot:

```yaml
jobs:
  list:
    runs-on: ubuntu

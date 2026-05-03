# surrogate-1 / backend

## Highest-value incremental improvement (<2h)

**What to ship:** Replace recursive `list_repo_files` + per-file API ingestion in `bin/dataset-enrich.sh` with a **single-tree + CDN-only fetch** strategy that:

1. Lists one date folder via `list_repo_tree(path, recursive=False)` (single API call).
2. Persists the file list to `file-list.json` (committed or embedded in the runner).
3. Downloads only needed files via **HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header during training/streaming.
4. Projects to `{prompt,response}` at parse time and writes `batches/public-merged/{date}/shard<N>-{HHMMSS}.jsonl` (no extra metadata columns).

This directly fixes:
- HF API 429 (rate-limit) by removing recursive listing and per-file auth calls.
- Space OOM by avoiding `load_dataset(streaming=True)` on heterogeneous schemas.
- Schema CastError by projecting only at parse time.

---

## Concrete implementation plan

### 1) Update `bin/dataset-enrich.sh` (or create thin orchestrator)

- Keep existing 16-shard matrix behavior.
- Add pre-step: if `file-list.json` missing/old, run a one-time Mac/CI script to produce it (see below).
- During worker run: read `file-list.json`, filter by `shard_id = hash(slug) % 16 == SHARD_ID`.
- Download assigned files via CDN URLs using `curl`/`wget` (no HF token required).
- Stream-parse each file (parquet/jsonl) and project `{prompt,response}`; emit to stdout or temp NDJSON.
- Final upload: use `huggingface_hub` upload only the produced `shard-N-*.jsonl` (small payload, one commit per shard).

### 2) Add Mac/CI list-builder script (`scripts/build-file-list.py`)

- Runs on Mac or CI after rate-limit window.
- Uses `huggingface_hub.list_repo_tree(repo_id, path="public-merged/2026-05-03", recursive=False)`.
- Walks one level to collect file paths; optionally recurses one more level if layout is `.../2026-05-03/*.parquet`.
- Outputs `file-list.json`:
  ```json
  {
    "date": "2026-05-03",
    "root": "public-merged/2026-05-03",
    "files": [
      "public-merged/2026-05-03/file-001.parquet",
      "public-merged/2026-05-03/file-002.parquet"
    ]
  }
  ```
- Commit this file into `surrogate-1-runner` (or store as workflow artifact) so workers never call `list_repo_files` recursively.

### 3) Update worker runtime behavior

- Remove any `load_dataset(streaming=True, repo_type="dataset", ...)` on heterogeneous repo.
- Use CDN download + local pyarrow read:
  ```python
  import pyarrow.parquet as pq
  import requests

  def stream_cdn_parquet(path):
      url = f"https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{path}"
      # stream download to temp file or memory
      r = requests.get(url, stream=True, timeout=60)
      r.raise_for_status()
      with open("/tmp/temp.parquet", "wb") as f:
          for chunk in r.iter_content(chunk_size=8192):
              f.write(chunk)
      table = pq.read_table("/tmp/temp.parquet", columns=["prompt", "response"])
      for batch in table.to_batches(max_chunksize=8192):
          for row in zip(batch["prompt"].to_pylist(), batch["response"].to_pylist()):
              yield {"prompt": row[0], "response": row[1]}
  ```
- If schema varies, catch `ArrowInvalid` and fallback to per-row projection or skip malformed rows.

### 4) Upload side (unchanged pattern)

- Each shard produces `shard-<N>-<HHMMSS>.jsonl`.
- Use `huggingface_hub.upload_file` to:
  ```
  batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl
  ```
- No `source`, no `ts` columns (attribution via filename pattern).

### 5) Cron / workflow hygiene

- Ensure GitHub Actions runners use `bash` explicitly where needed (shebang + executable).
- Keep `SHELL=/bin/bash` in any crontab entries (if you mirror this to cron later).

---

## Code snippets

### `scripts/build-file-list.py`

```python
#!/usr/bin/env python3
"""
Run on Mac/CI after rate-limit window.
Produces file-list.json for CDN-only workers.
"""
import json
import os
from huggingface_hub import list_repo_tree

REPO_ID = "axentx/surrogate-1-training-pairs"
DATE_ROOT = "public-merged/2026-05-03"  # parameterize per run if desired
OUTPUT = "file-list.json"

def main():
    entries = list_repo_tree(REPO_ID, path=DATE_ROOT, recursive=False)
    files = [e.rfilename for e in entries if e.type == "file"]
    payload = {
        "date": DATE_ROOT.split("/")[-1],
        "root": DATE_ROOT,
        "files": files,
    }
    with open(OUTPUT, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(files)} files to {OUTPUT}")

if __name__ == "__main__":
    main()
```

### Worker fragment (bash orchestrator excerpt)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="2026-05-03"
SHARD=$SHARD_ID  # from matrix
OUTDIR="batches/public-merged/${DATE}"
mkdir -p "$OUTDIR"
TIMESTAMP=$(date -u +"%H%M%S")
OUTFILE="${OUTDIR}/shard-${SHARD}-${TIMESTAMP}.jsonl"

# Use precomputed file list
jq -r '.files[]' file-list.json | while read -r f; do
  # deterministic shard assignment
  HASH=$(echo -n "$f" | md5sum | awk '{print strtonum("0x" substr($1,1,8))}')
  if (( HASH % 16 != SHARD )); then
    continue
  fi
  echo "Processing shard $SHARD: $f" >&2
  python -c "
import sys, json
from worker import stream_cdn_parquet
for rec in stream_cdn_parquet(sys.argv[1]):
    print(json.dumps(rec), flush=True)
" "$f"
done > "$OUTFILE"

# Upload single shard file (small, one commit)
python -c "
from huggingface_hub import upload_file
upload_file(
    path_or_fileobj='${OUTFILE}',
    path_in_repo='${OUTFILE}',
    repo_id='${REPO}',
    repo_type='dataset',
    token=os.environ['HF_TOKEN'],
)
"
```

### Minimal `worker.py` (CDN + projection)

```python
import tempfile
import requests
import pyarrow.parquet as pq

def stream_cdn_parquet(path):
    url = f"https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{path}"
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        r = requests.get(url, stream=True, timeout=60)
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=65536):
            tmp.write(chunk)
        tmp.flush()
        try:
            table = pq.read_table(tmp.name, columns=["prompt", "response"])
        except Exception:
            # fallback: try reading all and project
            table = pq.read_table(tmp.name)
            if "prompt" not in table.column_names or "response" not in table.column_names:
                return
        for batch in table.to_batches(max_chunksize=4096):
            prompts = batch["prompt"].

# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Goal**: Eliminate HF API 429s during training and make shard workers fully resilient by deterministic pre-flight file listing + CDN-only ingestion.

**Scope**:
- Add a one-time Mac/CLI step that snapshots `list_repo_tree` for a date folder → `snapshot-<date>.json`.
- Update `bin/dataset-enrich.sh` to read the snapshot and fetch every file via CDN (`resolve/main/...`) with zero `datasets`/`list_repo_files` calls during worker runs.
- Add `training/train.py` helper to load the same snapshot and use CDN-only downloads in the Lightning data pipeline.
- Keep existing 16-shard deterministic hash routing and dedup unchanged.
- Add retry/backoff for CDN downloads, per-file timeout, and schema-agnostic `{prompt,response}` projection.

---

### 1) `bin/list-snapshot.sh` (snapshot once, run on Mac)

```bash
#!/usr/bin/env bash
# bin/list-snapshot.sh
# Usage: HF_TOKEN=... ./bin/list-snapshot.sh axentx/surrogate-1-training-pairs 2026-05-02 > snapshot-2026-05-02.json
set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE="${2:-$(date +%F)}"
FOLDER="public-raw/${DATE}"

python3 - "$REPO" "$FOLDER" <<'PY'
import os, json, sys
from huggingface_hub import HfApi

repo_id = sys.argv[1]
folder = sys.argv[2].rstrip("/")
api = HfApi(token=os.environ.get("HF_TOKEN"))

# Single API call: non-recursive per folder to avoid pagination explosion.
entries = api.list_repo_tree(repo_id, path=folder, recursive=False)

files = []
for e in entries:
    if not e.path.endswith((".jsonl", ".parquet", ".json")):
        continue
    files.append({
        "path": e.path,
        "cdn_url": f"https://huggingface.co/datasets/{repo_id}/resolve/main/{e.path}"
    })

sys.stdout.write(json.dumps({"repo": repo_id, "folder": folder, "files": files}, indent=2))
PY
```

Make executable:
```bash
chmod +x bin/list-snapshot.sh
```

---

### 2) `lib/cdn_reader.py` (CDN-only, schema-agnostic projection)

```python
# lib/cdn_reader.py
import json
import pyarrow.parquet as pq
import requests
import io
import os
import time
import logging
from typing import Iterator, Dict, Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = int(os.getenv("CDN_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("CDN_RETRIES", "3"))
BACKOFF_FACTOR = float(os.getenv("CDN_BACKOFF", "1.5"))

HF_TOKEN = os.getenv("HF_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}


def _fetch_cdn(url: str) -> bytes:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=DEFAULT_TIMEOUT, stream=True)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            wait = BACKOFF_FACTOR ** attempt
            logger.warning("CDN fetch failed (attempt %s/%s) %s: %s", attempt, MAX_RETRIES, url, exc)
            if attempt == MAX_RETRIES:
                raise
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url}")


def read_jsonl(content: bytes) -> Iterator[Dict[str, Any]]:
    for line in io.BytesIO(content).read().splitlines():
        if not line:
            continue
        try:
            yield json.loads(line.decode("utf-8"))
        except Exception:
            continue


def read_parquet(content: bytes) -> Iterator[Dict[str, Any]]:
    try:
        table = pq.read_table(io.BytesIO(content))
        for batch in table.to_batches(max_chunksize=1000):
            cols = batch.column_names
            # Try common prompt/response names; fallback to first two text cols.
            prompt_col = next((c for c in ("prompt", "instruction", "input", "question") if c in cols), None)
            response_col = next((c for c in ("response", "output", "answer", "completion") if c in cols), None)

            if prompt_col and response_col:
                prompts = batch.column(cols.index(prompt_col)).to_pylist()
                responses = batch.column(cols.index(response_col)).to_pylist()
                for p, r in zip(prompts, responses):
                    if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                        yield {"prompt": p.strip(), "response": r.strip()}
            else:
                # fallback: project first two string columns
                projected = []
                for col in cols:
                    if len(projected) >= 2:
                        break
                    col_vals = batch.column(cols.index(col)).to_pylist()
                    if col_vals and isinstance(col_vals[0], str):
                        projected.append(col_vals)
                if len(projected) == 2:
                    for a, b in zip(projected[0], projected[1]):
                        if isinstance(a, str) and isinstance(b, str) and a.strip() and b.strip():
                            yield {"prompt": a.strip(), "response": b.strip()}
    except Exception as exc:
        logger.warning("Parquet decode failed: %s", exc)


def stream_records_from_cdn(file_entry: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    url = file_entry["cdn_url"]
    content = _fetch_cdn(url)
    path = file_entry["path"]
    if path.endswith(".parquet"):
        yield from read_parquet(content)
    else:
        yield from read_jsonl(content)
```

---

### 3) Updated `bin/dataset-enrich.sh` (CDN-only ingestion)

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated: CDN-only ingestion using pre-built snapshot
# Usage:
#   HF_TOKEN=... ./bin/dataset-enrich.sh --snapshot snapshot-2026-05-02.json --shard 0 --shards 16 --out shard-0.jsonl
set -euo pipefail

SNAPSHOT=""
SHARD="${SHARD_ID:-0}"
SHARDS="${SHARDS_TOTAL:-16}"
OUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot) SNAPSHOT="$2"; shift 2 ;;
    --shard) SHARD="$2"; shift 2 ;;
    --shards) SHARDS="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done

if [[ -z "$SNAPSHOT" || ! -f "$SNAPSHOT" ]]; then
  echo "ERROR: --snapshot <snapshot-*.json> is required" >&2
  exit 1
fi

if [[ -z "$OUT" ]]; then
  echo "ERROR: --out <output.jsonl> is required" >&2
  exit 1
fi

export PYTHONUNBUFFERED=1
TMP_DIR=$(mktemp -d)
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

python3 - "$SNAPSHOT" "$SHARD" "$SHARDS" "$OUT" <<'PY'
import json, sys, hashlib, os, itertools, logging
from lib.cdn_reader import stream_records_from_cdn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def deterministic_shard(key: str, shards: int) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % shards

def main():
    snapshot_path, shard_id, shards_total, out_path = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    with open(snapshot_path) as f:
        snapshot = json.load(f)

    files = snapshot.get("files", [])
    if not files:
        logging.error("No files in snapshot")
        sys.exit(1)


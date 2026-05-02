# surrogate-1 / discovery

## Final Synthesis — Highest-Value, Correct, Actionable Plan

Ship **deterministic date-partitioned ingestion with CDN-only fetches and a pre-flight file list** to eliminate redundant HF API calls, prevent overwrite races, and stabilize downstream training inputs.

---

### Why this is the correct, highest-value change
- **Correctness**: Date-partitioned outputs (`YYYY/MM/DD`) give stable history and prevent accidental overwrites across runs.
- **Actionability**: CDN-only fetches bypass HF API rate limits during bulk ingestion; pre-flight file list collapses many API calls into one.
- **Stability**: Deterministic shard assignment (`slug-hash % 16`) + idempotent upload prevents races and duplicate work.
- **Low risk, ~90–110 min**: One dev, minimal code changes, backward-compatible.

---

### Concrete Implementation Plan

#### 1) Add deterministic date-partitioned output path
- Output pattern:  
  `batches/public-merged/YYYY/MM/DD/shard{N}-{runId}.jsonl`  
  where `YYYY/MM/DD` is UTC date (from `RUN_DATE` or today) and `runId` is `HHMMSS` or short workflow id.
- Prevents overwrite races and stabilizes training snapshots.

#### 2) Generate a pre-flight file list once per date
- One API call per date folder (`public-raw/YYYY-MM-DD`) to list files.
- Embed this file list (JSON) and distribute to workers.
- Workers use only CDN URLs thereafter; no further `list_repo_tree` or per-file API metadata calls.

#### 3) Switch workers to CDN-only fetches
- Use `https://huggingface.co/datasets/{repo}/resolve/main/{path}` for downloads.
- Add lightweight retry/backoff (exponential) and timeouts.
- Skip API entirely during bulk ingestion.

#### 4) Keep shard-determinism and central dedup
- Shard assignment: `hash(slug) % 16` (stable across runs).
- Central dedup store used by workers to drop duplicates before write.

#### 5) Idempotent upload with existence check
- Before upload, check if blob exists; skip if present.
- Prevents redundant commits and reduces rate-limit pressure.

---

### Final Files (Minimal, Correct, Ready to Run)

#### `bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Usage:
#   export HF_TOKEN=...
#   export SHARD_ID=0
#   export RUN_DATE=2026-05-02   # optional; defaults to today UTC
#   ./bin/dataset-enrich.sh
set -euo pipefail

REPO_DATASET="${REPO_DATASET:-axentx/surrogate-1-training-pairs}"
HF_TOKEN="${HF_TOKEN:-}"
SHARD_ID="${SHARD_ID:-0}"
RUN_DATE="${RUN_DATE:-$(date -u +%Y-%m-%d)}"
PARTITION=$(echo "$RUN_DATE" | sed 's/-/\//g')   # 2026/05/02
RUN_ID=$(date -u +%H%M%S)
OUTDIR="batches/public-merged/${PARTITION}"
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${RUN_ID}.jsonl"
TMPDIR=$(mktemp -d)
cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT

mkdir -p "$OUTDIR"

# ---- 1) Pre-flight: list files in date folder once ----
DATE_RAW_PATH="public-raw/${RUN_DATE}"
FILE_LIST="${TMPDIR}/file-list.json"

echo "[$(date -u -Iseconds)] Listing ${DATE_RAW_PATH}..."
python3 - "$REPO_DATASET" "$DATE_RAW_PATH" "$FILE_LIST" "$HF_TOKEN" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

repo = sys.argv[1]
path = sys.argv[2]
out = sys.argv[3]
token = sys.argv[4] or None

api = HfApi(token=token)
items = api.list_repo_tree(repo=repo, path=path, recursive=False)
files = [it.rfilename for it in items if it.type == "file"]
with open(out, "w") as f:
    json.dump(files, f)
print(f"Wrote {len(files)} files to {out}")
PY

# ---- 2) Worker: process assigned files via CDN ----
python3 bin/worker.py \
  --repo "$REPO_DATASET" \
  --file-list "$FILE_LIST" \
  --shard-id "$SHARD_ID" \
  --num-shards 16 \
  --outfile "$OUTFILE" \
  --tmpdir "$TMPDIR"

# ---- 3) Upload result (idempotent: skip if exists) ----
if [ -s "$OUTFILE" ]; then
  echo "[$(date -u -Iseconds)] Uploading $OUTFILE..."
  python3 - "$REPO_DATASET" "$OUTFILE" "$HF_TOKEN" <<'PY'
import os, sys
from huggingface_hub import HfApi

repo = sys.argv[1]
path = sys.argv[2]
token = sys.argv[3] or None

api = HfApi(token=token)
try:
    api.get_metadata(repo_id=repo, path=path)
    print(f"Blob exists, skipping upload: {path}")
except Exception:
    api.upload_file(
        path_or_fileobj=path,
        path_in_repo=path,
        repo_id=repo,
        commit_message=f"shard-upload: {path}"
    )
    print(f"Uploaded: {path}")
PY
else
  echo "[$(date -u -Iseconds)] No output for shard ${SHARD_ID}; skipping upload."
fi
```

---

#### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
bin/worker.py
Shard worker: reads assigned files via CDN, projects to {prompt,response},
dedups via central store, and writes JSONL output.
"""
import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

# Local dedup helper (same interface used by HF Space)
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def hash_slug(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest(), 16)

def belongs_to_shard(slug: str, shard_id: int, num_shards: int) -> bool:
    return (hash_slug(slug) % num_shards) == shard_id

def download_via_cdn(repo: str, path: str, dest: Path, max_retries: int = 5) -> Path:
    url = HF_CDN_TEMPLATE.format(repo=repo, path=path)
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=30, stream=True)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return dest
        except Exception as exc:
            wait = 2 ** attempt
            print(f"[cdn] attempt {attempt}/{max_retries} failed for {path}: {exc}; retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Failed to download {path} via CDN after {max_retries} attempts")

def project_to_pair(batch_rows) -> list[dict]:
    """
    Normalize heterogeneous schemas to {prompt, response}.
    Keep minimal fields; drop source/ts to avoid schema creep.
    """
    out = []
    for row in batch_rows:
        d = dict(row)
        prompt = d.get("prompt") or d.get("input") or d.get("question") or d.get("text") or ""
        response = d.get("response") or d.get("output") or d.get("answer") or d.get("completion") or ""
        if not prompt or not response:
            continue
        out.append({"prompt": str(prompt).strip(), "response": str(response).strip()})
    return out

def run_shard(repo: str, file_list: list[str], shard_id: int, num_sh

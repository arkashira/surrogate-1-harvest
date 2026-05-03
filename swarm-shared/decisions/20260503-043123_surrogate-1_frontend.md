# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/worker.py`** — single-file worker that:
   - Accepts `SHARD_ID` and `TOTAL_SHARDS` from the matrix
   - Uses one HF API call (after rate-limit window) to list target folder via `list_repo_tree(path, recursive=False)` and saves manifest JSON
   - Downloads only assigned shard’s files via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no auth header
   - Projects each file to `{prompt, response}` at parse time (avoids mixed-schema CastError)
   - Deduplicates via existing `lib/dedup.py` md5 store
   - Writes `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`

2. **Update `bin/dataset-enrich.sh`** — thin wrapper that:
   - Sets `PYTHONUNBUFFERED=1`, `SHELL=/bin/bash`
   - Validates `HF_TOKEN` is present
   - Invokes `python3 bin/worker.py` with matrix params

3. **Update `.github/workflows/ingest.yml`** — ensure:
   - Matrix `shard_id: [0..15]`
   - Each job uses `python3 -m pip install -r requirements.txt`
   - No recursive `list_repo_files`; rely on worker’s single tree call per shard

4. **Add `requirements-dev.txt`** (optional) — pin `requests`, `pyarrow`, `tqdm` for local testing

---

### Code Snippets

#### `bin/worker.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.

Usage:
  SHARD_ID=0 TOTAL_SHARDS=16 python3 bin/worker.py

Environment:
  HF_TOKEN         - write token for axentx/surrogate-1-training-pairs
  SOURCE_REPO      - default: datasets/axentx/surrogate-1-training-pairs
  TARGET_DATE      - default: today YYYY-MM-DD
"""

import os
import sys
import json
import hashlib
import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download

# ── config --
HF_TOKEN = os.getenv("HF_TOKEN")
SOURCE_REPO = os.getenv("SOURCE_REPO", "datasets/axentx/surrogate-1-training-pairs")
TARGET_DATE = os.getenv("TARGET_DATE", datetime.date.today().isoformat())
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))

API = HfApi(token=HF_TOKEN)
BASE_CDN = f"https://huggingface.co/{SOURCE_REPO}/resolve/main"

# ── paths --
REPO_ROOT = Path(__file__).parent.parent.parent
DEDUP_PY = REPO_ROOT / "lib" / "dedup.py"
sys.path.insert(0, str(DEDUP_PY.parent))
from dedup import is_duplicate, mark_seen  # type: ignore

OUT_DIR = Path("batches") / "public-merged" / TARGET_DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{datetime.datetime.utcnow().strftime('%H%M%S')}.jsonl"

# ── helpers --
def list_folder_tree(path: str = "") -> List[str]:
    """Single API call: non-recursive tree for one folder."""
    items = API.list_repo_tree(repo_id=SOURCE_REPO, path=path, repo_type="dataset")
    # Keep only files (ignore subfolders for now); adjust if nested folders used
    files = [item.rfilename for item in items if item.type == "file"]
    return files

def assign_shard(key: str, n: int) -> int:
    """Deterministic shard assignment by hash."""
    digest = hashlib.md5(key.encode()).hexdigest()
    return int(digest, 16) % n

def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous file to {prompt, response}.
    Adjust per your actual schema conventions.
    """
    # Common patterns seen in surrogate-1 training pairs
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or ""
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def download_via_cdn(file_path: str) -> bytes:
    """CDN bypass: no auth header, separate rate limits."""
    url = f"{BASE_CDN}/{file_path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

# ── main --
def run_shard() -> None:
    print(f"[shard {SHARD_ID}/{TOTAL_SHARDS}] starting for {TARGET_DATE}")

    # 1) list once per shard (lightweight; can cache across runs if desired)
    all_files = list_folder_tree()
    eligible = [f for f in all_files if assign_shard(f, TOTAL_SHARDS) == SHARD_ID]
    print(f"[shard {SHARD_ID}] processing {len(eligible)} files")

    written = 0
    skipped_dup = 0
    schema_errors = 0

    with OUT_FILE.open("w", encoding="utf-8") as fout:
        for file_path in eligible:
            try:
                data = download_via_cdn(file_path)
                # Assume parquet for now; extend for jsonl if needed
                import pyarrow.parquet as pq
                import io

                table = pq.read_table(io.BytesIO(data))
                df = table.to_pandas()
            except Exception as exc:
                # If mixed schemas cause CastError, skip file-level corruption
                print(f"[warn] failed to read {file_path}: {exc}")
                schema_errors += 1
                continue

            for _, row in df.iterrows():
                raw = row.to_dict()
                pair = project_to_pair(raw)
                if not pair["prompt"] or not pair["response"]:
                    continue

                # dedup by content hash
                blob = json.dumps(pair, sort_keys=True, ensure_ascii=False)
                if is_duplicate(blob):
                    skipped_dup += 1
                    continue

                mark_seen(blob)
                fout.write(blob + "\n")
                written += 1

    print(
        f"[shard {SHARD_ID}] done. written={written} dup_skipped={skipped_dup} schema_errors={schema_errors} out={OUT_FILE}"
    )

if __name__ == "__main__":
    run_shard()
```

#### `bin/dataset-enrich.sh` (updated)

```bash
#!/usr/bin/env bash
set -euo pipefail
export SHELL=/bin/bash
export PYTHONUNBUFFERED=1

cd "$(dirname "$0")/.."

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "HF_TOKEN is required" >&2
  exit 1
fi

python3 bin/worker.py
```

#### `.github/workflows/ingest.yml` (matrix excerpt)

```yaml
jobs:
  ingest:
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: |
          echo "SHARD_ID=${{ matrix.shard_id }}" >> "$GITHUB_ENV"
          echo "TOTAL_SHARDS=16" >> "$GITHUB_ENV"
      - run: bash bin/dataset-enrich.sh
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
```

---

### Notes & Trade-offs

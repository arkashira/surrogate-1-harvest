# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/worker.py`** — single-file, deterministic shard worker:
   - Accepts `SHARD_ID` (0–15) and `TOTAL_SHARDS` (16) via env.
   - On start: one HF API call to `list_repo_tree` for today’s folder (or uses cached `manifest.json` if provided).
   - Writes `manifest-shard{N}.json` with the filtered file list (CDN paths only).
   - Streams each file via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no auth header.
   - Projects to `{prompt, response}` only at parse time; drops all other columns to avoid `pyarrow.CastError` on mixed schemas.
   - Deduplicates via `lib/dedup.py` (central md5 store) and outputs `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

2. **Update `bin/dataset-enrich.sh`** — thin wrapper:
   - Sets `SHELL=/bin/bash`, `#!/usr/bin/env bash`, `set -euo pipefail`.
   - Exports `PYTHONUNBUFFERED=1`, `HF_HUB_ENABLE_HF_TRANSFER=1` (faster downloads).
   - Invokes `python3 bin/worker.py` with proper args; no inline Python in shell.

3. **Update `.github/workflows/ingest.yml`** — matrix job:
   - Keeps 16-shard matrix but removes inline run steps that used `datasets` streaming.
   - Each job runs `bash bin/dataset-enrich.sh`.
   - Adds `timeout-minutes: 30` and `retry-on-429` backoff (wait 360s) via a small retry wrapper.

4. **Add `requirements.txt` entries** (if missing):
   ```
   huggingface_hub>=0.22
   pyarrow>=14
   numpy
   requests
   ```

5. **Remove anti-patterns**:
   - No `load_dataset(streaming=True)` on heterogeneous repos.
   - No recursive `list_repo_files`; use per-folder `list_repo_tree(recursive=False)`.
   - No `source`/`ts` columns in output; attribution via filename pattern.

---

### Code Snippets

#### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass shard worker for surrogate-1 public dataset ingestion.

Usage:
  SHARD_ID=0 TOTAL_SHARDS=16 python3 bin/worker.py

Outputs:
  batches/public-merged/YYYY-MM-DD/shard{N}-{HHMMSS}.jsonl
"""
import os, sys, json, hashlib, datetime, time, pathlib, subprocess
from typing import List, Dict, Any

import requests
from huggingface_hub import list_repo_tree, hf_hub_download
from tqdm import tqdm

# ── config --
HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
DATE_STR = datetime.date.today().isoformat()
OUT_DIR = pathlib.Path(f"batches/public-merged/{DATE_STR}")
OUT_DIR.mkdir(parents=True, exist_ok=True)

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))
MANIFEST_PATH = pathlib.Path(f"manifest-shard{SHARD_ID}.json")

# ── dedup --
def load_dedup_db() -> set:
    # delegate to central store; fallback to local set if unavailable
    try:
        from lib.dedup import DedupStore
        store = DedupStore()
        return store  # implements __contains__ / add
    except Exception:
        return set()

DEDUP = load_dedup_db()

# ── file listing --
def build_manifest() -> List[str]:
    """Single API call: list today's folder (non-recursive)."""
    items = list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_STR,
        recursive=False,
        token=True  # use HF token for listing; falls back to anonymous if public
    )
    files = [it.rfilename for it in items if it.type == "file"]
    # deterministic shard assignment by slug hash
    assigned = []
    for f in sorted(files):
        slug = pathlib.Path(f).stem
        h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
        if h % TOTAL_SHARDS == SHARD_ID:
            assigned.append(f"{DATE_STR}/{f}")
    return assigned

def get_files() -> List[str]:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    files = build_manifest()
    MANIFEST_PATH.write_text(json.dumps(files, indent=2))
    return files

# ── CDN download + parse --
def cdn_url(path: str) -> str:
    return f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{path}"

def parse_parquet_to_pairs(local_path: pathlib.Path) -> List[Dict[str, str]]:
    import pyarrow.parquet as pq
    tbl = pq.read_table(local_path, columns=["prompt", "response"])
    df = tbl.to_pandas()
    pairs = []
    for _, row in df.iterrows():
        prompt = str(row.get("prompt", ""))
        response = str(row.get("response", ""))
        if prompt and response:
            pairs.append({"prompt": prompt.strip(), "response": response.strip()})
    return pairs

def download_and_process(path: str, out_f) -> int:
    url = cdn_url(path)
    local = pathlib.Path("_tmp") / pathlib.Path(path).name
    local.parent.mkdir(parents=True, exist_ok=True)

    # stream download via CDN (no auth header)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(local, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

    try:
        pairs = parse_parquet_to_pairs(local)
        written = 0
        for p in pairs:
            payload = json.dumps(p, ensure_ascii=False)
            h = hashlib.md5(payload.encode()).hexdigest()
            if h in DEDUP:
                continue
            # best-effort add; central store may handle cross-process dedup
            if hasattr(DEDUP, "add"):
                DEDUP.add(h)
            else:
                DEDUP.add(h)  # local set
            out_f.write(payload + "\n")
            written += 1
        return written
    finally:
        try:
            local.unlink()
        except Exception:
            pass

# ── main --
def main() -> None:
    files = get_files()
    if not files:
        print("No files assigned to this shard.")
        return

    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    out_path = OUT_DIR / f"shard{SHARD_ID}-{ts}.jsonl"

    total = 0
    with out_path.open("w", buffering=1) as out_f:
        for f in tqdm(files, desc=f"Shard {SHARD_ID}"):
            try:
                written = download_and_process(f, out_f)
                total += written
            except Exception as e:
                print(f"Error processing {f}: {e}", file=sys.stderr)
                continue

    print(f"Shard {SHARD_ID}: wrote {total} pairs to {out_path}")

if __name__ == "__main__":
    main()
```

#### `bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Surrogate-1 shard worker (thin wrapper around worker.py)
set -euo pipefail

export SHELL=/bin/bash
export PYTHONUNBUFFERED=1
export HF_HUB_ENABLE_HF_TRANSFER=1

exec python3 "$(dirname "$0")/worker.py" "$@"
```

#### `.github/workflows/ingest.yml` (excerpt — matrix section)
```yaml
jobs:
  ingest-shard:
   

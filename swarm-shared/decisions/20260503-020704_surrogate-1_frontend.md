# surrogate-1 / frontend

## Final Implementation Plan (≤2 h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from the GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac-side `list_repo_tree` snapshot) to deterministically shard file paths **without any HF API calls during worker execution**.
- Downloads assigned files via **HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with **no Authorization header** (bypasses `/api/` rate limits).
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow `CastError` on mixed schemas).
- Deduplicates via the existing central `lib/dedup.py` md5 store; **HF Space is the single source of truth for cross-run dedup**.
- Writes output to:
  ```
  batches/public-merged/<YYYY-MM-DD>/shard<N>-<HHMMSS>.jsonl
  ```
- Keeps a thin shell wrapper for cron/workflow compatibility but moves heavy lifting to Python with proper shebang and `chmod +x`.

---

### Steps (timed)

1. **Create `bin/file-list.json` template** (5 min) — commit a small example; CI will overwrite with fresh list from Mac before cron runs.
2. **Write `bin/dataset-enrich.py`** (60–75 min) — CDN fetches, schema projection, dedup, JSONL output.
3. **Update `bin/dataset-enrich.sh`** (10 min) — thin wrapper that sets `SHELL=/bin/bash`, exports vars, and invokes the Python script.
4. **Update `.github/workflows/ingest.yml`** (10 min) — add step to generate/push `file-list.json` once per workflow run (single API call) and pass matrix indices.
5. **Smoke test** (10–15 min) — run locally with a small file list and verify output shape/dedup.

---

## Code Snippets

### 1. `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 \
  HF_DATASET="axentx/surrogate-1-training-pairs" \
  FILE_LIST=file-list.json \
  python bin/dataset-enrich.py
"""

import json
import os
import sys
import hashlib
import datetime
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import requests
import pyarrow as pa
import pyarrow.parquet as pq

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

HF_DATASET = os.getenv("HF_DATASET", "axentx/surrogate-1-training-pairs")
FILE_LIST = os.getenv("FILE_LIST", "file-list.json")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
OUT_ROOT = Path(os.getenv("OUT_ROOT", "batches/public-merged"))

CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/main"

HEADERS = {
    # No Authorization header -> bypass /api/ rate limits
    "User-Agent": "axentx-surrogate-ingest/1.0"
}

TIMEOUT = (10, 30)  # connect, read
RETRY_BACKOFF = [1, 2, 4]

def load_file_list(path: str) -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)

def shard_paths(paths: Iterable[str], shard_id: int, shard_total: int) -> list[str]:
    paths = sorted(set(p.strip() for p in paths if p.strip()))
    return [p for i, p in enumerate(paths) if i % shard_total == shard_id]

def safe_get(url: str) -> Optional[bytes]:
    for wait in RETRY_BACKOFF:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.content
            if r.status_code == 429:
                time.sleep(wait)
                continue
            # 404/403 -> skip
            return None
        except Exception:
            time.sleep(wait)
    return None

def project_to_pair(raw: bytes, ext: str) -> Optional[Dict[str, str]]:
    """
    Project arbitrary file to {prompt, response}.
    Supports: .jsonl, .json, .parquet
    """
    ext = ext.lower()
    try:
        if ext == ".parquet":
            tbl = pq.read_table(pa.BufferReader(raw))
            cols = tbl.column_names
            prompt_col = next((c for c in cols if "prompt" in c.lower()), cols[0] if cols else None)
            response_col = next((c for c in cols if "response" in c.lower()), cols[1] if len(cols) > 1 else None)
            if prompt_col is None or response_col is None:
                return None
            df = tbl.select([prompt_col, response_col]).to_pandas()
            return {"prompt": str(df.iloc[0][0]), "response": str(df.iloc[0][1])}

        if ext == ".jsonl":
            # streaming first line
            line = raw.split(b"\n", 1)[0].strip()
            obj = json.loads(line)
        else:  # .json
            obj = json.loads(raw)

        # Common shapes
        if isinstance(obj, dict):
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer")
            if prompt is not None and response is not None:
                return {"prompt": str(prompt), "response": str(response)}
        return None
    except Exception:
        return None

def md5_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()

def main() -> None:
    file_list = load_file_list(FILE_LIST)
    # Accept either list of strings or dict with "files" key
    if isinstance(file_list, dict):
        raw_paths = file_list.get("files", [])
        if not raw_paths:
            raw_paths = [v for v in file_list.values() if isinstance(v, list)]
            if raw_paths and isinstance(raw_paths[0], list):
                raw_paths = raw_paths[0]
    else:
        raw_paths = file_list  # type: ignore

    paths = shard_paths(raw_paths, SHARD_ID, SHARD_TOTAL)
    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} -> {len(paths)} files", file=sys.stderr)

    dedup = DedupStore()
    today = datetime.date.today().isoformat()
    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    out_dir = OUT_ROOT / today
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shard{SHARD_ID}-{ts}.jsonl"

    written = 0
    skipped = 0
    duped = 0

    with out_path.open("w", encoding="utf-8") as fout:
        for p in paths:
            url = f"{CDN_BASE}/{p}"
            raw = safe_get(url)
            if raw is None:
                skipped += 1
                continue

            pair = project_to_pair(raw, Path(p).suffix)
            if not pair:
                skipped += 1
                continue

            # Dedup by content hash
            h = md5_bytes((pair["prompt"] + "\n" + pair["response"]).encode("utf-8"))
            if dedup.exists(h):
                duped += 1
                continue

            dedup.add(h)
            fout.write(json.dumps(pair, ensure_ascii=False) + "\n")
            written += 1

    print(f"Done. written={written} duped={duped} skipped={skipped} out={out_path}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

---


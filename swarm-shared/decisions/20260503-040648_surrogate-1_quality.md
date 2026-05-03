# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data load.

### Steps (1h 45m total)

1. **Create `bin/dataset-enrich.py`** (60m)  
   - Deterministic sharding: `hash(slug) % SHARD_TOTAL == SHARD_ID`  
   - Single `list_repo_tree` call per date folder → save `manifest.json`  
   - CDN-only downloads via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth, bypass API rate limits)  
   - Schema projection to `{prompt, response}` only at parse time (avoids pyarrow CastError)  
   - Central md5 dedup via existing `lib/dedup.py`  
   - Output: `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`

2. **Update `bin/dataset-enrich.sh`** → thin wrapper (15m)  
   - Shebang `#!/usr/bin/env bash`, `set -euo pipefail`  
   - Invoke `python3 bin/dataset-enrich.py "$@"`  
   - Preserve env vars: `SHARD_ID`, `SHARD_TOTAL`, `HF_TOKEN`, `DATE_FOLDER`

3. **Update `.github/workflows/ingest.yml`** (15m)  
   - Ensure matrix `shard_id: [0..15]` passed as `SHARD_ID`  
   - Add `python-version: '3.11'` and cache `pip`  
   - Keep `HF_TOKEN` secret

4. **Smoke test** (15m)  
   - Run locally with `SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py`  
   - Verify manifest creation, CDN fetch, dedup, output JSONL

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py [--date-folder YYYY-MM-DD]

Environment:
  HF_TOKEN         - HuggingFace write token (for dedup store push + final upload)
  SHARD_ID         - This worker's shard index (0..SHARD_TOTAL-1)
  SHARD_TOTAL      - Total shards (default 16)
  DATE_FOLDER      - Dataset subfolder date (default today)
"""

import json
import os
import sys
import hashlib
import datetime
import time
from pathlib import Path
from typing import List, Dict, Any

import requests
import pyarrow.parquet as pq
import pyarrow as pa
from huggingface_hub import HfApi, hf_hub_download

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

# ---- config ----
REPO = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO}/resolve/main"
API = HfApi(token=os.getenv("HF_TOKEN"))

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.date.today().isoformat())

OUT_DIR = Path("batches/public-merged") / DATE_FOLDER
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

MANIFEST_PATH = Path("manifest") / DATE_FOLDER / "files.json"
MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---- helpers ----
def deterministic_shard(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def list_date_files(date_folder: str) -> List[str]:
    """Single API call to list files in date folder (non-recursive per folder)."""
    items = API.list_repo_tree(repo_id=REPO, path=date_folder, recursive=False)
    files = []
    for item in items:
        if item.get("type") == "file":
            files.append(f"{date_folder}/{item['path']}")
        elif item.get("type") == "directory":
            subitems = API.list_repo_tree(repo_id=REPO, path=item["path"], recursive=False)
            for sub in subitems:
                if sub.get("type") == "file":
                    files.append(f"{sub['path']}")
    return files

def download_via_cdn(repo_path: str, local_path: Path) -> None:
    """CDN bypass: no Authorization header -> avoids /api/ rate limits."""
    url = f"{BASE_CDN}/{repo_path}"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

def project_to_pair(raw_bytes: bytes, file_ext: str) -> Dict[str, str]:
    """Project to {prompt, response} only at parse time; ignore extra schema cols."""
    ext = file_ext.lower()
    if ext == ".parquet":
        tbl = pq.read_table(pa.BufferReader(raw_bytes))
        # Keep only prompt/response if present; tolerate schema variations
        cols = set(tbl.column_names)
        prompt_col = next((c for c in ("prompt", "instruction", "input") if c in cols), None)
        response_col = next((c for c in ("response", "output", "completion") if c in cols), None)
        if prompt_col is None or response_col is None:
            # fallback: first two string/text cols
            text_cols = [c for c in tbl.column_names if pa.types.is_string(tbl.schema.field(c).type)]
            if len(text_cols) >= 2:
                prompt_col, response_col = text_cols[0], text_cols[1]
            else:
                raise ValueError(f"Cannot find prompt/response in {tbl.column_names}")
        df = tbl.select([prompt_col, response_col]).to_pandas()
        return {"prompt": str(df.iloc[0][0]), "response": str(df.iloc[0][1])}
    elif ext == ".jsonl":
        # simple line-oriented; take first line
        line = raw_bytes.decode("utf-8").strip().splitlines()[0]
        obj = json.loads(line)
        prompt = obj.get("prompt") or obj.get("instruction") or obj.get("input")
        response = obj.get("response") or obj.get("output") or obj.get("completion")
        if prompt is None or response is None:
            # fallback: first two string values
            str_vals = [v for v in obj.values() if isinstance(v, str)]
            if len(str_vals) >= 2:
                prompt, response = str_vals[0], str_vals[1]
            else:
                raise ValueError("Cannot find prompt/response in JSON")
        return {"prompt": str(prompt), "response": str(response)}
    else:
        raise ValueError(f"Unsupported file type: {ext}")

# ---- main ----
def main() -> None:
    print(f"[shard {SHARD_ID}/{SHARD_TOTAL}] date={DATE_FOLDER}")

    # 1) manifest: single API call
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH) as f:
            all_files = json.load(f)
        print(f"Loaded manifest with {len(all_files)} files")
    else:
        all_files = list_date_files(DATE_FOLDER)
        MANIFEST_PATH.write_text(json.dumps(all_files, indent=2))
        print(f"Saved manifest with {len(all_files)} files")

    # 2) shard assignment
    my_files = [f for f in all_files if deterministic_shard(f) == SHARD_ID]
    print(f"Shard {SHARD_ID} processing {len(my_files)} files")

    # 3) dedup store
    dedup = DedupStore()

    # 4) process


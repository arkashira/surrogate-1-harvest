# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic shard via `hash(slug) % SHARD_TOTAL`
- **CDN-only fetches**: `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{DATE}/{file}` (no Authorization header; bypasses API rate limits)
- Projects heterogeneous files to `{prompt, response}` at parse time (no `load_dataset(streaming=True)` on mixed schemas)
- Dedup via central `lib/dedup.py` md5 store (same interface as existing)
- Outputs: `batches/public-merged/{DATE}/shard{SHARD_ID}-{HHMMSS}.jsonl`
- Idempotent: re-runs on same date+shard overwrite only their shard file; no cross-shard collisions

### Steps (1h 30m)

1. **Create `bin/dataset-enrich.py`** (50m)
   - Manifest fetch + deterministic shard assignment
   - CDN streaming download with `requests` (`stream=True`) for JSONL; `hf_hub_download` (CDN-backed) for Parquet to avoid Arrow cast issues
   - Per-file schema detection → project to `{prompt, response}`
   - Batch insert to dedup store + write JSONL
2. **Update `bin/dataset-enrich.sh`** to thin wrapper that calls `python3 bin/dataset-enrich.py` with env (10m)
3. **Add `requests` to `requirements-dev.txt`** if not present (5m)
4. **Smoke test** locally with mock HF_TOKEN and a small date folder (15m)

---

## Code

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage (via env):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 HF_TOKEN=hf_xxx python3 bin/dataset-enrich.py
"""

import os
import sys
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Iterator

import requests
from huggingface_hub import HfApi, hf_hub_download

# Local dedup module (shared with HF Space)
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # type: ignore

# ----------
# Constants
# ----------
REPO_ID = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"
OUTPUT_ROOT = Path("batches/public-merged")

# ----------
# Helpers
# ----------
def _hash_slug(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def _project_to_pair(obj: Dict[str, Any], filename: str) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous file content to {prompt, response}.
    Supports common patterns seen in surrogate-1-training-pairs.
    """
    if not obj:
        return None

    # If already correct shape
    if "prompt" in obj and "response" in obj:
        prompt = str(obj["prompt"]).strip()
        response = str(obj["response"]).strip()
        if prompt and response:
            return {"prompt": prompt, "response": response}
        return None

    # Common alternates
    if "instruction" in obj and "output" in obj:
        prompt = str(obj["instruction"]).strip()
        response = str(obj["output"]).strip()
        if prompt and response:
            return {"prompt": prompt, "response": response}
        return None
    if "input" in obj and "output" in obj:
        prompt = str(obj["input"]).strip()
        response = str(obj["output"]).strip()
        if prompt and response:
            return {"prompt": prompt, "response": response}
        return None
    if "question" in obj and "answer" in obj:
        prompt = str(obj["question"]).strip()
        response = str(obj["answer"]).strip()
        if prompt and response:
            return {"prompt": prompt, "response": response}
        return None

    # Fallback: try to find any two text-like fields
    text_keys = [k for k, v in obj.items() if isinstance(v, str) and len(v) > 10]
    if len(text_keys) >= 2:
        prompt = str(obj[text_keys[0]]).strip()
        response = str(obj[text_keys[1]]).strip()
        if prompt and response:
            return {"prompt": prompt, "response": response}
        return None

    # Last resort: serialize into prompt/response
    return {
        "prompt": filename,
        "response": json.dumps(obj, ensure_ascii=False),
    }

def _load_jsonl_cdn(url: str) -> Iterator[Dict[str, Any]]:
    """Stream JSONL from CDN (newline-delimited JSON)."""
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue

def _load_parquet_cdn(rel_path: str) -> Iterator[Dict[str, str]]:
    """
    Download via hf_hub_download (CDN-backed) and read minimal columns.
    Falls back to projection for non-standard schemas.
    """
    local_path = hf_hub_download(
        repo_id=REPO_ID,
        filename=rel_path,
        use_auth_token=False,  # public file
        local_dir="/tmp/surrogate_cache",
        local_dir_use_symlinks=False,
    )
    try:
        import pyarrow.parquet as pq
        table = pq.read_table(local_path, columns=["prompt", "response"])
        for batch in table.to_batches(max_chunksize=500):
            for i in range(batch.num_rows):
                prompt = batch.column("prompt")[i].as_py()
                response = batch.column("response")[i].as_py()
                if prompt is None or response is None:
                    continue
                yield {"prompt": str(prompt).strip(), "response": str(response).strip()}
    except Exception:
        # Fallback: read all columns and project
        import pyarrow.parquet as pq
        table = pq.read_table(local_path)
        for i in range(table.num_rows):
            raw = table.slice(i, 1).to_pylist()[0]
            pair = _project_to_pair(raw, rel_path)
            if pair:
                yield pair

# ----------
# Main worker
# ----------
def run_shard(
    shard_id: int,
    shard_total: int,
    date_folder: str,
    hf_token: str,
    api: HfApi,
    dedup: DedupStore,
) -> int:
    """
    Process one shard for a given date folder.
    Returns number of new pairs written.
    """
    # 1) Manifest: single non-recursive tree call
    try:
        tree = api.list_repo_tree(repo_id=REPO_ID, path=date_folder, recursive=False)
    except Exception as e:
        print(f"Failed to list repo tree for {date_folder}: {e}", file=sys.stderr)
        return 0

    files = [item.rfilename for item in tree if item.type == "file"]
    if not files:
        print(f"No files found in {date_folder}")
        return 0

    # Deterministic shard assignment
    my_files = [f for f in files if _hash_slug(f) % shard_total == shard_id]
    print(f"Shard {shard_id}/{shard_total} processing {len(my_files)} files from {len(files)} total")

    # 2) Process files via CDN
    new_count = 0
    timestamp = datetime.utcnow().strftime("%H%M%S")
    out_dir = OUTPUT_ROOT / date_folder
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir

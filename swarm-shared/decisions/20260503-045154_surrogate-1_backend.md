# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **`bin/dataset-enrich.sh`** → **`bin/dataset-enrich.py`**
   - Add `#!/usr/bin/env bash` shebang to any remaining wrapper scripts (per pattern).
   - Python worker:
     - Accept `SHARD_ID` and `SHARD_TOTAL` from matrix.
     - Single API call: `list_repo_tree(..., recursive=False)` for today’s folder (or latest folder). Save list to `manifest.json`.
     - Embed manifest in worker; iterate files and download via **CDN bypass** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with `requests` (no auth header).
     - Stream-parse each file, project to `{prompt, response}` only, compute md5 hash.
     - Central dedup via existing `lib/dedup.py` (SQLite).
     - Output: `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` (one JSONL per shard, newline-delimited).
   - Remove `load_dataset(streaming=True)` for heterogeneous repos; use `hf_hub_download` per file when needed, but prefer CDN for speed and rate-limit avoidance.

2. **`.github/workflows/ingest.yml`**
   - Keep 16-shard matrix.
   - Set `SHELL=/bin/bash` in job defaults (per wrapper script pattern).
   - Step: install Python deps from `requirements.txt`.
   - Run `python bin/dataset-enrich.py` with `SHARD_ID`/`SHARD_TOTAL` env.

3. **`requirements.txt`**
   - Add: `requests`, keep `datasets`, `huggingface_hub`, `pyarrow`, `numpy`.

4. **`lib/dedup.py`** (no change)
   - Ensure it exposes a simple `is_duplicate(md5: str) -> bool` and `add(md5: str)` interface.

### Code snippets

#### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingest worker for surrogate-1-training-pairs.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py
"""
import os
import json
import hashlib
import requests
import datetime as dt
from pathlib import Path
from typing import List, Tuple

from huggingface_hub import HfApi, hf_hub_download

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
BASE_DIR = Path(__file__).parent.parent

# Local imports
DEDUP = None  # will be initialized lazily

def init_dedup():
    global DEDUP
    if DEDUP is None:
        # lib/dedup.py exposes is_duplicate/add
        from lib.dedup import is_duplicate, add
        DEDUP = (is_duplicate, add)
    return DEDUP

def list_today_files(api: HfApi) -> List[str]:
    """Single API call: list top-level folder for today (YYYY-MM-DD)."""
    today = dt.datetime.utcnow().strftime("%Y-%m-%d")
    items = api.list_repo_tree(repo_id=HF_REPO, path=today, recursive=False)
    # items may be dicts or objects; normalize to path strings
    paths = []
    for it in items:
        p = it.get("path", None) if isinstance(it, dict) else getattr(it, "path", None)
        if p:
            paths.append(f"{today}/{p}")
    return paths

def download_via_cdn(repo: str, path: str) -> bytes:
    """CDN bypass: no Authorization header required for public datasets."""
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def parse_file_to_pairs(content: bytes, filename: str) -> List[Tuple[str, str]]:
    """
    Schema-agnostic parser:
    - If parquet: read with pyarrow, keep only prompt/response cols if present.
    - If json/jsonl: stream-parse and extract prompt/response.
    - Otherwise skip.
    """
    import pyarrow.parquet as pq
    import pyarrow as pa
    import io
    import json as jsonlib

    name = filename.lower()
    pairs = []

    try:
        if name.endswith(".parquet"):
            table = pq.read_table(io.BytesIO(content))
            # Project only prompt/response to avoid mixed-schema CastError
            cols = [c for c in table.column_names if c in ("prompt", "response")]
            if len(cols) == 2:
                df = table.select(cols).to_pandas()
                for _, row in df.iterrows():
                    prompt = str(row.get("prompt") or "")
                    response = str(row.get("response") or "")
                    if prompt and response:
                        pairs.append((prompt, response))
        elif name.endswith(".jsonl"):
            for line in content.decode("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = jsonlib.loads(line)
                prompt = str(obj.get("prompt") or obj.get("input") or "")
                response = str(obj.get("response") or obj.get("output") or "")
                if prompt and response:
                    pairs.append((prompt, response))
        elif name.endswith(".json"):
            obj = jsonlib.loads(content.decode("utf-8"))
            if isinstance(obj, list):
                items = obj
            else:
                items = [obj]
            for item in items:
                prompt = str(item.get("prompt") or item.get("input") or "")
                response = str(item.get("response") or item.get("output") or "")
                if prompt and response:
                    pairs.append((prompt, response))
    except Exception as exc:
        # Graceful degradation: skip malformed files
        print(f"Skipping {filename}: {exc}")
    return pairs

def md5_for_pair(prompt: str, response: str) -> str:
    return hashlib.md5(f"{prompt}\0{response}".encode("utf-8")).hexdigest()

def write_shard_output(pairs: List[Tuple[str, str]]):
    date_str = dt.datetime.utcnow().strftime("%Y-%m-%d")
    ts_str = dt.datetime.utcnow().strftime("%H%M%S")
    out_dir = BASE_DIR / "batches" / "public-merged" / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shard{SHARD_ID}-{ts_str}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for prompt, response in pairs:
            f.write(json.dumps({"prompt": prompt, "response": response}) + "\n")
    print(f"Wrote {len(pairs)} pairs to {out_path}")

def main():
    is_dup, add_dup = init_dedup()
    api = HfApi(token=HF_TOKEN)

    # 1) List files once (single API call)
    all_files = list_today_files(api)
    if not all_files:
        print("No files found for today; exiting.")
        return

    # Save manifest for reproducibility
    manifest_path = BASE_DIR / "manifest.json"
    with manifest_path.open("w") as f:
        json.dump({"date": dt.datetime.utcnow().strftime("%Y-%m-%d"), "files": all_files}, f)

    # 2) Deterministic shard assignment
    assigned_files = [f for i, f in enumerate(all_files) if hash(f) % SHARD_TOTAL == SHARD_ID]
    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} processing {len(assigned_files)} files")

    accepted = 0
    skipped_dup = 0
    for path in assigned_files:
        try:
            content = download_via_cdn(HF_REPO, path)
            pairs = parse_file_to_pairs(content, path)

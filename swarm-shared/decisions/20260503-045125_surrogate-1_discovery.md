# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/worker.py`** — deterministic shard worker that:
   - Reads a pre-computed `manifest.json` (date → file list) produced once per day by a lightweight Mac orchestrator (HF API call outside rate-limit window).
   - Downloads only its 1/16 slice via raw CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) — zero API calls during ingest.
   - Projects each file to `{prompt, response}` at parse time (avoids `load_dataset` mixed-schema CastError).
   - Computes per-row md5, checks central dedup store, streams output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

2. **Update `bin/dataset-enrich.sh`** → thin wrapper that:
   - Accepts `SHARD_ID`/`N_SHARDS` env vars (from matrix).
   - Invokes `python bin/worker.py` with proper error handling.
   - Ensures `#!/usr/bin/env bash`, `set -euo pipefail`, and executable bit.

3. **Add `requirements.txt`** extras if needed (`requests`, `tqdm`, `pyarrow`, `datasets`, `huggingface_hub` already present).

4. **Keep `.github/workflows/ingest.yml`** unchanged (16-shard matrix) — only the worker implementation changes.

### Why this wins
- **Eliminates HF API rate limits during training**: Lightning `train.py` will embed the same manifest and use CDN-only fetches (per prior pattern).
- **Prevents CastError**: no `load_dataset(streaming=True)` on heterogeneous repo; projection happens per-file.
- **Deterministic, reproducible, testable**: Python unit tests can mock CDN downloads; manifest is versioned.
- **Fits <2h**: ~30 lines of worker logic + 10 lines of shell wrapper.

---

## Code Snippets

### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass shard worker for surrogate-1 public-dataset ingest.

Environment:
  SHARD_ID      0..15
  N_SHARDS      16
  HF_TOKEN      write token for axentx/surrogate-1-training-pairs
  MANIFEST_URL  optional: direct URL to manifest.json (date -> file list)
"""
import json
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

HF_DATASET = "axentx/surrogate-1-training-pairs"
CDN_ROOT = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/main"

# Central dedup store (SQLite) lives on HF Space; here we use a local
# lightweight check to avoid obvious duplicates within the same shard run.
# The HF Space will perform authoritative cross-source dedup.
def md5_of_str(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def load_manifest(manifest_path: Path) -> Dict[str, List[str]]:
    with open(manifest_path) as f:
        return json.load(f)  # {"2026-05-03": ["folder/file1.parquet", ...]}

def pick_shard_files(files: List[str], shard_id: int, n_shards: int) -> List[str]:
    return [f for i, f in enumerate(sorted(files)) if i % n_shards == shard_id]

def safe_project_to_pair(local_path: Path):
    """
    Project heterogeneous file to {prompt, response} pair.
    Supports:
      - JSONL with 'prompt'/'response' or 'instruction'/'output'
      - Parquet with any schema: pick first string column as prompt,
        second string column as response; if missing, synthesize.
    """
    suffix = local_path.suffix.lower()
    if suffix == ".jsonl":
        with open(local_path) as f:
            for line in f:
                obj = json.loads(line)
                prompt = obj.get("prompt") or obj.get("instruction") or ""
                response = obj.get("response") or obj.get("output") or ""
                if prompt and response:
                    yield {"prompt": prompt, "response": response}
    elif suffix == ".parquet":
        try:
            table = pq.read_table(local_path, columns=None)
        except Exception:
            # Fallback: read all and project
            table = pq.read_table(local_path)
        cols = table.column_names
        # Heuristic: pick first two string-like cols
        str_cols = [c for c in cols if pa.types.is_string(table.schema.field(c).type)]
        if len(str_cols) >= 2:
            prompts = table.column(str_cols[0]).to_pylist()
            responses = table.column(str_cols[1]).to_pylist()
        elif len(str_cols) == 1:
            prompts = table.column(str_cols[0]).to_pylist()
            # Try to find any other column for response
            other = [c for c in cols if c != str_cols[0]][0] if len(cols) > 1 else None
            responses = table.column(other).to_pylist() if other else [""] * len(prompts)
        else:
            # No string cols: synthesize placeholder
            n = table.num_rows
            prompts = [""] * n
            responses = [""] * n
        for p, r in zip(prompts, responses):
            if p and r:
                yield {"prompt": str(p), "response": str(r)}
    else:
        # Unknown: skip
        return

def download_cdn(url: str, dest: Path, hf_token: str = None):
    headers = {}
    # CDN public files do not require Authorization, but including token
    # does not hurt and may help for gated repos if ever used.
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"
    r = requests.get(url, headers=headers, stream=True, timeout=60)
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)

def upload_chunk(output_dir: Path, rows: List[Dict], shard_id: int):
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    out_file = output_dir / f"shard{shard_id}-{ts}.jsonl"
    with open(out_file, "w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return out_file

def main():
    shard_id = int(os.getenv("SHARD_ID", "0"))
    n_shards = int(os.getenv("N_SHARDS", "16"))
    hf_token = os.getenv("HF_TOKEN", "")
    manifest_path = Path(os.getenv("MANIFEST_PATH", "manifest.json"))
    date_str = datetime.utcnow().strftime("%Y-%m-%d")

    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = load_manifest(manifest_path)
    all_files = []
    for files in manifest.values():
        all_files.extend(files)
    shard_files = pick_shard_files(all_files, shard_id, n_shards)

    print(f"Shard {shard_id}/{n_shards}: processing {len(shard_files)} files")

    output_dir = Path("batches") / "public-merged" / date_str
    buffer = []
    buffer_limit = 5000
    seen_md5s = set()

    tmp_root = Path("tmp") / f"shard{shard_id}"

    for rel_path in tqdm(shard_files, desc=f"Shard {shard_id}"):
        cdn_url = f"{CDN_ROOT}/{rel_path}"
        local_file = tmp_root / rel_path
        try:
            download_cdn(cdn_url, local_file, hf_token)
            for pair in safe_project_to_pair(local

# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` from the runner → produces deterministic file manifest
- Worker deterministically hashes each filename → assigns to shard; only processes assigned files
- Downloads via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header → avoids HF API 429 during data load
- Projects each file to `{prompt, response}` at parse time (no schema assumptions; handles mixed schemas without pyarrow CastError)
- Dedup via central `lib/dedup.py` md5 store
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with newline JSON
- Exits 0 on success, non-zero on fatal error (GitHub Actions will retry)

---

## File: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 dataset-enrich worker (CDN-bypass, manifest-driven).

Usage:
  HF_TOKEN=hf_xxx \
  SHARD_ID=0 SHARD_TOTAL=16 \
  DATE=2026-04-29 \
  python bin/dataset-enrich.py

Environment:
  HF_TOKEN         - HuggingFace write token (for dedup store + upload)
  SHARD_ID         - integer 0..(SHARD_TOTAL-1)
  SHARD_TOTAL      - default 16
  DATE             - YYYY-MM-DD folder under dataset repo
  MANIFEST_URL     - optional: URL to precomputed manifest.json
  DATASET_REPO     - default "axentx/surrogate-1-training-pairs"
  OUTPUT_BATCH_DIR - default "batches/public-merged"
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download

# ---- config ----
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("ERROR: HF_TOKEN is required", file=sys.stderr)
    sys.exit(1)

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE")
if not DATE:
    print("ERROR: DATE (YYYY-MM-DD) is required", file=sys.stderr)
    sys.exit(1)

OUTPUT_BATCH_DIR = os.getenv("OUTPUT_BATCH_DIR", "batches/public-merged")
MANIFEST_URL = os.getenv("MANIFEST_URL")  # optional precomputed manifest

# ---- dedup ----
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa

dedup = DedupStore()
api = HfApi(token=HF_TOKEN)

# ---- helpers ----
def hf_repo_tree(date_folder: str) -> List[str]:
    """List top-level files in repo/date_folder (non-recursive)."""
    entries = api.list_repo_tree(repo_id=DATASET_REPO, path=date_folder, recursive=False)
    # entries may be dict or object; normalize to path strings
    paths = []
    for e in entries:
        p = e if isinstance(e, str) else getattr(e, "path", None) or e.get("path")
        if p:
            paths.append(p)
    return paths

def build_manifest(date_folder: str) -> List[str]:
    """Return list of file paths for date_folder."""
    return sorted(hf_repo_tree(date_folder))

def assign_to_shard(path: str, total: int) -> int:
    """Deterministic shard assignment by path hash."""
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return int(digest, 16) % total

def cdn_download_url(repo: str, path: str) -> str:
    """CDN bypass URL (no auth)."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def safe_get(url: str, timeout: int = 30) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def parse_to_pair(raw: bytes, path: str) -> Dict[str, str]:
    """
    Project arbitrary file to {prompt, response}.
    Heuristics:
    - .json / .jsonl: try to extract prompt/response fields.
    - .parquet: read via pyarrow and project.
    - .txt / .md: treat whole content as prompt, response empty.
    """
    import io

    suffix = Path(path).suffix.lower()

    # JSON / JSONL
    if suffix in (".json", ".jsonl"):
        try:
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, list):
                # take first object if list
                data = data[0] if data else {}
            if isinstance(data, dict):
                prompt = data.get("prompt") or data.get("input") or data.get("question") or ""
                response = data.get("response") or data.get("output") or data.get("answer") or ""
                return {"prompt": str(prompt), "response": str(response)}
        except Exception:
            pass

    # Parquet
    if suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
            table = pq.read_table(io.BytesIO(raw))
            cols = table.column_names
            prompt_col = next((c for c in ("prompt", "input", "question") if c in cols), None)
            response_col = next((c for c in ("response", "output", "answer") if c in cols), None)
            if prompt_col or response_col:
                prompt = str(table[prompt_col][0].as_py()) if prompt_col else ""
                response = str(table[response_col][0].as_py()) if response_col else ""
                return {"prompt": prompt, "response": response}
        except Exception:
            pass

    # Fallback: text-like
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return {"prompt": text.strip(), "response": ""}

def main() -> None:
    # Build / load manifest
    if MANIFEST_URL:
        print(f"Loading manifest from {MANIFEST_URL}")
        manifest_raw = requests.get(MANIFEST_URL, timeout=30).content
        all_files = json.loads(manifest_raw.decode("utf-8"))
    else:
        print(f"Building manifest for {DATE} via list_repo_tree")
        all_files = build_manifest(DATE)

    my_files = [p for p in all_files if assign_to_shard(p, SHARD_TOTAL) == SHARD_ID]
    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} assigned {len(my_files)} files")

    if not my_files:
        print("No files assigned; exiting.")
        sys.exit(0)

    # Prepare output
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(OUTPUT_BATCH_DIR) / DATE
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shard{SHARD_ID}-{ts}.jsonl"

    written = 0
    skipped_dup = 0
    errors = 0

    with out_path.open("w", encoding="utf-8") as fout:
        for idx, path in enumerate(my_files, 1):
            try:
                url = cdn_download_url(DATASET_REPO, path)
                raw = safe_get(url)
                pair = parse_to_pair(raw, path)

                # Dedup by content hash
                content_hash = hashlib.md5(raw).hexdigest()
                if dedup.is_duplicate(content_hash):
                    skipped_dup += 1
                    continue

                record = {
                    "prompt":

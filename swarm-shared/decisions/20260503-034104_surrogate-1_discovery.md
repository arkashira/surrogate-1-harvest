# surrogate-1 / discovery

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **manifest-driven, CDN-bypass ingestion**: single API call to list one date folder → save `file-list.json`; workers stream files via raw CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) to avoid HF API rate limits during training
- Projects every file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Deduplicates via central `lib/dedup.py` md5 store
- Uploads shard output to `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`
- Adds shebang, `chmod +x`, and Bash-safe invocation for cron compatibility

---

## File: bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py

Env:
  SHARD_ID          - worker index (0..SHARD_TOTAL-1)
  SHARD_TOTAL       - total parallel workers (default 16)
  DATE_FOLDER       - dataset subfolder date (default today YYYY-MM-DD)
  HF_TOKEN          - write token for axentx/surrogate-1-training-pairs
  HF_REPO           - dataset repo (default axentx/surrogate-1-training-pairs)
  MANIFEST_PATH     - optional pre-saved file-list.json (if present, skips list_repo_tree)
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
from huggingface_hub import HfApi, hf_hub_download, list_repo_tree

# ---- config ----
HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("ERROR: HF_TOKEN is required", file=sys.stderr)
    sys.exit(1)

SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))

API = HfApi(token=HF_TOKEN)

# paths
WORKDIR = Path(__file__).parent.parent
MANIFEST_PATH = Path(os.getenv("MANIFEST_PATH", WORKDIR / "file-list.json"))
DEDUP_PY = WORKDIR / "lib" / "dedup.py"
OUTPUT_DIR = WORKDIR / "batches" / "public-merged" / DATE_FOLDER
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---- helpers ----
def slug_hash(s: str) -> int:
    """Deterministic 0..2^32-1 hash for shard assignment."""
    return int(hashlib.md5(s.encode()).hexdigest(), 16) % (2**32)

def belongs_to_shard(slug: str) -> bool:
    return (slug_hash(slug) % SHARD_TOTAL) == SHARD_ID

def list_date_files() -> List[str]:
    """Single API call: list files in DATE_FOLDER (non-recursive)."""
    print(f"Listing repo tree: {HF_REPO} @ {DATE_FOLDER}")
    try:
        tree = list_repo_tree(
            repo_id=HF_REPO,
            path=DATE_FOLDER,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as e:
        print(f"ERROR listing repo tree: {e}", file=sys.stderr)
        sys.exit(1)
    files = [item.rfilename for item in tree if item.type == "file"]
    print(f"Found {len(files)} files in {DATE_FOLDER}")
    return files

def save_manifest(files: List[str]) -> None:
    MANIFEST_PATH.write_text(json.dumps(files, indent=2))
    print(f"Saved manifest to {MANIFEST_PATH}")

def load_manifest() -> List[str]:
    if not MANIFEST_PATH.exists():
        return []
    return json.loads(MANIFEST_PATH.read_text())

def is_duplicate(md5_b64: str) -> bool:
    """Delegate to central dedup store (lib/dedup.py)."""
    # dedup.py exposes a small CLI: python lib/dedup.py check <md5>
    import subprocess
    try:
        out = subprocess.check_output(
            [sys.executable, str(DEDUP_PY), "check", md5_b64],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out == "1"
    except Exception:
        # If dedup unavailable, treat as not duplicate (safe fallback).
        return False

def mark_duplicate(md5_b64: str) -> None:
    import subprocess
    try:
        subprocess.run(
            [sys.executable, str(DEDUP_PY), "add", md5_b64],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

def parse_file_to_pair(cdn_url: str, file_path: str) -> List[Dict[str, str]]:
    """
    Download via CDN (no auth) and project to {prompt,response}.
    Supports common formats: jsonl, json, parquet, csv, txt.
    Returns list of {prompt, response} dicts.
    """
    import io

    resp = requests.get(cdn_url, timeout=30)
    resp.raise_for_status()
    data = resp.content
    suffix = Path(file_path).suffix.lower()

    pairs = []

    try:
        if suffix == ".parquet":
            import pyarrow.parquet as pq
            table = pq.read_table(io.BytesIO(data))
            df = table.to_pandas()
            # heuristic: find prompt/response columns (case-insensitive)
            cols = {c.lower(): c for c in df.columns}
            prompt_col = cols.get("prompt") or cols.get("question") or cols.get("input")
            response_col = cols.get("response") or cols.get("answer") or cols.get("output")
            if prompt_col and response_col:
                for _, row in df.iterrows():
                    pairs.append({"prompt": str(row[prompt_col]), "response": str(row[response_col])})
            else:
                # fallback: first two text columns
                text_cols = [c for c in df.columns if pd.api.types.is_string_dtype(df[c].dtype)]
                if len(text_cols) >= 2:
                    for _, row in df.iterrows():
                        pairs.append({"prompt": str(row[text_cols[0]]), "response": str(row[text_cols[1]])})

        elif suffix == ".jsonl":
            import json as jsonlib
            for line in io.BytesIO(data).read().decode("utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = jsonlib.loads(line)
                prompt = obj.get("prompt") or obj.get("question") or obj.get("input")
                response = obj.get("response") or obj.get("answer") or obj.get("output")
                if prompt is not None and response is not None:
                    pairs.append({"prompt": str(prompt), "response": str(response)})

        elif suffix == ".json":
            import json as jsonlib
            obj = jsonlib.loads(io.BytesIO(data).read().decode("utf-8", errors="ignore"))
            if isinstance(obj, list):
                for item in obj:
                    prompt = item.get("prompt") or item.get("question") or item.get("input")
                    response = item.get("response") or item.get("answer") or item.get("output")
                    if prompt is not None and response is not None:
                        pairs.append({"prompt": str(prompt), "response": str(response)})
            elif isinstance(obj, dict):
                prompt = obj.get("prompt") or obj.get("question") or obj.get("input")
                response = obj.get("response") or obj.get("answer") or obj.get("output")
                if prompt is

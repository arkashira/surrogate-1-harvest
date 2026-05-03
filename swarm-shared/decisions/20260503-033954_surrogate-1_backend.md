# surrogate-1 / backend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **manifest-first strategy**: single `list_repo_tree` call (per date folder) → saves `manifest.json`; workers deterministically shard by `hash(slug) % SHARD_TOTAL`
- Downloads via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero API/auth calls during data load, avoids 429 rate limits
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Dedups via central `lib/dedup.py` md5 store (same as Space)
- Outputs: `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`
- Reuses existing `requirements.txt` (datasets, huggingface_hub, pyarrow, numpy)

Time budget:
- 0–20 min: scaffold + manifest logic
- 20–50 min: CDN download + schema projection + dedup integration
- 50–80 min: output writer + idempotency + tests
- 80–120 min: polish, shebang, executable, workflow var update

---

## Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GH Actions matrix):
  SHARD_ID=3 SHARD_TOTAL=16 python bin/dataset-enrich.py

Env:
  SHARD_ID            - required; 0..SHARD_TOTAL-1
  SHARD_TOTAL         - optional; default 16
  DATE_FOLDER         - optional; default today YYYY-MM-DD
  HF_TOKEN            - write token for axentx/surrogate-1-training-pairs
  HF_REPO             - optional; default "axentx/surrogate-1-training-pairs"
  MANIFEST_PATH       - optional; path to cached manifest.json
"""

import os
import sys
import json
import hashlib
import datetime
import subprocess
import tempfile
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download

# ── config --
HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("ERROR: HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "-1"))
if not (0 <= SHARD_ID < SHARD_TOTAL):
    print(f"ERROR: SHARD_ID must be in [0, {SHARD_TOTAL - 1}]", file=sys.stderr)
    sys.exit(1)

DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.date.today().isoformat())
API = HfApi(token=HF_TOKEN)

# ── paths --
WORKDIR = Path(__file__).parent.parent
DEDUP_PY = WORKDIR / "lib" / "dedup.py"
OUTPUT_DIR = WORKDIR / "batches" / "public-merged" / DATE_FOLDER
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MANIFEST_PATH = Path(os.getenv("MANIFEST_PATH", OUTPUT_DIR / "manifest.json"))

# ── helpers --
def slug_hash_bucket(slug: str, n: int) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % n

def list_date_files(date_folder: str) -> List[str]:
    """Single API call: list top-level files in date folder (non-recursive)."""
    items = API.list_repo_tree(repo_id=HF_REPO, path=date_folder, recursive=False)
    # Keep only files (skip subfolders). Expect raw filenames or dicts.
    files = []
    for item in items:
        if isinstance(item, dict):
            if item.get("type") == "file":
                files.append(item["path"])
        elif isinstance(item, str):
            files.append(item)
    return sorted(files)

def cdn_url(repo: str, filepath: str) -> str:
    """CDN bypass URL (no auth, no API rate-limit)."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{filepath}"

def safe_download(url: str, dest: Path) -> bool:
    """Download via CDN. Returns True on success."""
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return True
    except Exception as e:
        print(f"Download failed {url}: {e}", file=sys.stderr)
        return False

def project_to_pair(raw_path: Path) -> List[Dict[str, str]]:
    """
    Project file to {prompt, response} pairs.
    Supports: .jsonl, .parquet, .json
    """
    import pyarrow.parquet as pq
    import numpy as np

    pairs = []
    suffix = raw_path.suffix.lower()

    try:
        if suffix == ".parquet":
            tbl = pq.read_table(raw_path, columns=["prompt", "response"])
            df = tbl.to_pandas()
            for _, row in df.iterrows():
                p = str(row.get("prompt") or "")
                r = str(row.get("response") or "")
                if p and r:
                    pairs.append({"prompt": p, "response": r})

        elif suffix == ".jsonl":
            for line in raw_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                p = str(obj.get("prompt") or obj.get("input") or "")
                r = str(obj.get("response") or obj.get("output") or "")
                if p and r:
                    pairs.append({"prompt": p, "response": r})

        elif suffix == ".json":
            data = json.loads(raw_path.read_text())
            if isinstance(data, list):
                items = data
            else:
                items = [data]
            for obj in items:
                p = str(obj.get("prompt") or obj.get("input") or "")
                r = str(obj.get("response") or obj.get("output") or "")
                if p and r:
                    pairs.append({"prompt": p, "response": r})
        else:
            print(f"Unsupported file type {suffix}, skipping", file=sys.stderr)
    except Exception as e:
        print(f"Projection failed {raw_path}: {e}", file=sys.stderr)

    return pairs

def dedup_pairs(pairs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Call central dedup store; returns non-duplicate pairs."""
    if not DEDUP_PY.exists():
        print("WARNING: dedup.py not found, skipping dedup", file=sys.stderr)
        return pairs

    # dedup.py expected interface: read_hashes(), add_hashes(hashes), is_dup(hash)
    # We'll exec in subprocess for isolation.
    import tempfile
    import json as _json

    payload = {"pairs": pairs}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(_json.dumps(payload))
        payload_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, str(DEDUP_PY), payload_path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"Dedup failed: {result.stderr}", file=sys.stderr)
            return pairs
        out = _json.loads(result.stdout)
        return out.get("unique_pairs", pairs)
    except Exception as e:
        print(f"Dedup error: {e}", file=sys.stderr)
        return pairs
    finally:
        Path(payload_path).unlink(missing_ok=True)

# ── main --
def main() -> None:
    # 1) manifest
    if MANIFEST_PATH

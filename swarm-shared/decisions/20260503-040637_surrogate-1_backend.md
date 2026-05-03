# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data load and adds robust retry/checksum validation.

### Concrete steps (1h 45m total)

1. **Create `bin/dataset-enrich.py`** (60m)  
   - Deterministic sharding: `hash(slug) % SHARD_TOTAL == SHARD_ID`  
   - Single `list_repo_tree` per date folder → save `manifest.json`  
   - Download via **CDN URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) with `requests` (no auth, bypass API rate limits)  
   - Add retries, timeout, and SHA-256 checksum validation (from manifest)  
   - Stream-parse each file → project to `{prompt, response}` only  
   - Central md5 dedup via existing `lib/dedup.py`  
   - Output: `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`  

2. **Update `bin/dataset-enrich.sh`** (15m)  
   - Thin wrapper that invokes `python bin/dataset-enrich.py "$@"`  
   - Keep backward compatibility for cron/workflow  

3. **Update `.github/workflows/ingest.yml`** (15m)  
   - Set `SHARD_TOTAL: 16` in matrix  
   - Pass `DATE_FOLDER` (optional) via `workflow_dispatch` input  
   - Ensure `HF_TOKEN` present for final push  

4. **Test locally** (15m)  
   - Dry-run with `SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py`  
   - Verify manifest creation, CDN downloads, checksum validation, dedup, output JSONL  

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py [DATE_FOLDER]

DATE_FOLDER defaults to today (YYYY-MM-DD).
"""
import os
import sys
import json
import hashlib
import datetime
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from huggingface_hub import HfApi

REPO = "axentx/surrogate-1-training-pairs"
API = HfApi()
CDN_BASE = f"https://huggingface.co/datasets/{REPO}/resolve/main"
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa: E402

def slug_hash(slug: str) -> int:
    """Deterministic hash for sharding."""
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def belongs_to_shard(slug: str) -> bool:
    return slug_hash(slug) % SHARD_TOTAL == SHARD_ID

def list_date_folder(date_folder: str) -> List[str]:
    """Single API call to list files in date folder (non-recursive)."""
    try:
        tree = API.list_repo_tree(REPO, path=date_folder, recursive=False)
        return [item.path for item in tree if item.type == "file"]
    except Exception as e:
        print(f"Error listing {date_folder}: {e}", file=sys.stderr)
        return []

def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def save_manifest(date_folder: str, files: List[str], base_path: Path) -> Path:
    """Save manifest with optional checksums (best-effort)."""
    manifest_path = base_path / "manifest" / date_folder / f"shard{SHARD_ID}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    entries = []
    for f in files:
        entry = {"path": f}
        # If checksum file exists in repo, try to fetch it (best-effort, non-blocking)
        checksum_path = f"{f}.sha256"
        try:
            checksum_url = f"{CDN_BASE}/{checksum_path}"
            r = requests.get(checksum_url, timeout=10)
            if r.status_code == 200:
                entry["sha256"] = r.text.strip().split()[0]  # handle `hash  filename`
        except Exception:
            pass
        entries.append(entry)

    manifest = {"date_folder": date_folder, "shard_id": SHARD_ID, "files": entries}
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest_path

def download_via_cdn(file_path: str, dest: Path, expected_sha256: Optional[str] = None) -> Path:
    """Download via CDN (no auth) with retries and optional SHA-256 validation."""
    url = f"{CDN_BASE}/{file_path}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(3):
        try:
            with requests.get(url, stream=True, timeout=(10, 60)) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
            # Validate checksum if provided
            if expected_sha256:
                actual = compute_sha256(dest)
                if actual != expected_sha256:
                    raise ValueError(f"Checksum mismatch: expected {expected_sha256}, got {actual}")
            return dest
        except Exception as e:
            if attempt == 2:
                raise
            print(f"Download attempt {attempt+1} failed for {file_path}: {e}; retrying...", file=sys.stderr)
            if dest.exists():
                dest.unlink(missing_ok=True)
    raise RuntimeError("Unreachable")

def parse_file_to_pairs(file_path: Path) -> List[Dict[str, str]]:
    """
    Parse a downloaded file and project to {prompt, response}.
    Supports common formats: jsonl, parquet (via pyarrow), json.
    """
    pairs = []
    try:
        if file_path.suffix == ".parquet":
            import pyarrow.parquet as pq
            table = pq.read_table(file_path)
            df = table.to_pandas()
            # Heuristic: look for prompt/response columns (case-insensitive)
            prompt_col = next((c for c in df.columns if "prompt" in c.lower()), None)
            response_col = next((c for c in df.columns if "response" in c.lower()), None)
            if prompt_col and response_col:
                for _, row in df.iterrows():
                    pairs.append({"prompt": str(row[prompt_col]), "response": str(row[response_col])})
            else:
                # Fallback: first two text columns
                text_cols = [c for c in df.columns if df[c].dtype == "object"]
                if len(text_cols) >= 2:
                    for _, row in df.iterrows():
                        pairs.append({"prompt": str(row[text_cols[0]]), "response": str(row[text_cols[1]])})
        else:
            # Try jsonl / json
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().strip()
            if file_path.suffix == ".jsonl" or "\n{" in content:
                for line in content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
                    response = obj.get("response") or obj.get("output") or obj.get("answer")
                    if prompt and response:
                        pairs.append({"prompt": str(prompt), "response

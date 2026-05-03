# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Pre-lists target folder once via `list_repo_tree(path, recursive=False)` → saves `manifest-{DATE}.json`
- Uses **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) for zero-API data streaming
- Projects heterogeneous files to `{prompt, response}` only at parse time (avoids PyArrow CastError)
- Deduplicates via central `lib/dedup.py` md5 store
- Writes output to `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Exits 0 on success, non-zero on fatal error (GitHub Actions-friendly)

---

## Code Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Environment:
  SHARD_ID          int (0..SHARD_TOTAL-1)
  SHARD_TOTAL       int (default 16)
  DATE              YYYY-MM-DD (default today)
  HF_TOKEN          HuggingFace write token
  REPO_ID           dataset repo (default axentx/surrogate-1-training-pairs)
  MANIFEST_PATH     optional path to pre-saved manifest JSON
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional

import requests
from huggingface_hub import HfApi

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent / "lib"))
from dedup import DedupStore  # type: ignore

HF_API = HfApi(token=os.getenv("HF_TOKEN"))
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
MANIFEST_PATH = os.getenv("MANIFEST_PATH", f"manifest-{DATE}.json")
OUT_DIR = Path(f"batches/public-merged/{DATE}")
OUT_DIR.mkdir(parents=True, exist_ok=True)

def slug_hash(slug: str) -> int:
    """Deterministic 0..2^32-1 hash for shard assignment."""
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def shard_for(slug: str) -> int:
    return slug_hash(slug) % SHARD_TOTAL

def list_date_files() -> List[str]:
    """List top-level files under DATE/ (non-recursive) via HF API."""
    try:
        tree = HF_API.list_repo_tree(
            repo_id=REPO_ID,
            path=DATE,
            recursive=False,
        )
        items = list(tree) if not isinstance(tree, list) else tree
        files = [item.rfilename for item in items if not item.rfilename.endswith("/")]
        return files
    except Exception as e:
        print(f"HF list_repo_tree failed: {e}", file=sys.stderr)
        raise

def save_manifest(files: List[str]) -> None:
    with open(MANIFEST_PATH, "w") as f:
        json.dump({"date": DATE, "files": files}, f)

def load_manifest() -> Optional[List[str]]:
    if not os.path.exists(MANIFEST_PATH):
        return None
    with open(MANIFEST_PATH) as f:
        data = json.load(f)
    return data.get("files")

def cdn_download_url(repo: str, path: str) -> str:
    """CDN URL that bypasses HF API auth/rate limits."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Heuristic: look for common field names; fallback to first/second text cols.
    """
    prompt_keys = {"prompt", "instruction", "input", "question", "user"}
    response_keys = {"response", "output", "answer", "assistant", "completion"}

    prompt = None
    response = None

    for k, v in raw.items():
        if k in prompt_keys and isinstance(v, str) and v.strip():
            prompt = v.strip()
        if k in response_keys and isinstance(v, str) and v.strip():
            response = v.strip()

    if prompt is None or response is None:
        str_fields = [v for v in raw.values() if isinstance(v, str) and v.strip()]
        if len(str_fields) >= 2:
            prompt, response = str_fields[0].strip(), str_fields[1].strip()
        elif len(str_fields) == 1:
            prompt, response = str_fields[0].strip(), ""
        else:
            prompt, response = "", ""

    return {"prompt": prompt, "response": response}

def stream_and_process(file_path: str, dedup: DedupStore) -> List[Dict[str, str]]:
    """Download via CDN, parse line-by-line (JSONL), dedup, project."""
    url = cdn_download_url(REPO_ID, file_path)
    pairs = []

    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue

                content = json.dumps(raw, sort_keys=True, separators=(",", ":"))
                md5 = hashlib.md5(content.encode()).hexdigest()

                if dedup.exists(md5):
                    continue

                pair = project_to_pair(raw)
                if pair["prompt"] or pair["response"]:
                    pairs.append(pair)
                    dedup.add(md5)
    except Exception as e:
        print(f"Failed to process {file_path}: {e}", file=sys.stderr)

    return pairs

def upload_shard_output(pairs: List[Dict[str, str]], shard_id: int) -> str:
    """Write shard output and return filename."""
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    out_file = OUT_DIR / f"shard{shard_id}-{ts}.jsonl"
    with open(out_file, "w") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    return str(out_file)

def push_to_hf(local_path: str, repo: str, path_in_repo: str) -> None:
    """Upload file to dataset repo using HF API (counted toward commit cap)."""
    try:
        HF_API.upload_file(
            path_or_fileobj=local_path,
            path_in_repo=path_in_repo,
            repo_id=repo,
            repo_type="dataset",
        )
    except Exception as e:
        print(f"HF upload failed for {path_in_repo}: {e}", file=sys.stderr)
        raise

def main() -> int:
    if SHARD_ID < 0 or SHARD_ID >= SHARD_TOTAL:
        print(f"Invalid SHARD_ID={SHARD_ID} for SHARD_TOTAL={SHARD_TOTAL}", file=sys.stderr)
        return 1

    dedup = DedupStore()

    # 1) Manifest: load or create
    files = load_manifest()
    if files is None:
        print(f"Creating manifest for {DATE}...")
        files = list_date_files()
        save_manifest(files)
    print(f"Manifest loaded: {len(files)} files")

    # 2) Shard assignment
    my_files = [f for f in files if shard_for(f) == SHARD_ID]
    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} assigned {len(my_files)} files")


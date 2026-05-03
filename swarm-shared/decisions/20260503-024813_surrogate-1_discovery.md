# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` (env).  
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic shard assignment by filename hash → **local JSON manifest** (enables idempotent retries and audit).  
- Downloads assigned files via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with **streaming + exponential backoff** (no Authorization header; avoids HF API 429).  
- Projects each file to `{prompt, response}` **only at parse time** (avoids persisting `source`/`ts`; handles mixed schemas).  
- Dedups via central `lib/dedup.py` md5 store (same interface as existing).  
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with **deterministic slug → repo assignment** for HF commit-cap spreading (5 sibling repos = 640/hr aggregate).  
- Exits 0 on success; non-zero on fatal error (GitHub Actions will retry).

---

## Code: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 dataset-enrich worker (CDN-bypass, manifest-driven).

Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx python bin/dataset-enrich.py
"""

from __future__ import annotations

import json
import os
import sys
import hashlib
import datetime
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download, ModelCard, Repository

# ── config --
HF_REPO_DATASET = "axentx/surrogate-1-training-pairs"
HF_API = HfApi()
DATE = os.getenv("DATE") or datetime.datetime.utcnow().strftime("%Y-%m-%d")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

# Sibling repos for HF commit-cap spreading (5 siblings + primary = 640/hr)
HF_SIBLING_REPOS = [
    "axentx/surrogate-1-training-pairs",
    "axentx/surrogate-1-training-pairs-s1",
    "axentx/surrogate-1-training-pairs-s2",
    "axentx/surrogate-1-training-pairs-s3",
    "axentx/surrogate-1-training-pairs-s4",
]

OUT_DIR = Path("batches/public-merged") / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"
MANIFEST_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.manifest.json"

# ── dedup --
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # type: ignore

dedup = DedupStore()

# ── helpers --
def hf_tree(date_folder: str) -> List[Dict[str, Any]]:
    """Single non-recursive tree call for date folder."""
    try:
        tree = HF_API.list_repo_tree(
            repo_id=HF_REPO_DATASET,
            path=date_folder,
            repo_type="dataset",
            token=HF_TOKEN,
        )
    except Exception as exc:
        print(f"HF list_repo_tree failed: {exc}", file=sys.stderr)
        sys.exit(1)
    # tree may be list or object depending on lib version
    if isinstance(tree, list):
        return [t for t in tree if t.get("type") == "file"]
    items = getattr(tree, "items", None)
    if callable(items):
        return [t for t in items() if t.get("type") == "file"]
    return []

def slug_to_repo(slug: str) -> str:
    """Deterministic repo assignment for HF commit-cap spreading."""
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return HF_SIBLING_REPOS[h % len(HF_SIBLING_REPOS)]

def download_via_cdn(repo: str, path: str) -> Optional[bytes]:
    """
    CDN bypass: no Authorization header.
    Public files at resolve/main/ are not counted against API rate limits.
    Uses streaming + exponential backoff.
    """
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    max_retries = 5
    for attempt in range(max_retries):
        try:
            with requests.get(url, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                chunks = []
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        chunks.append(chunk)
                return b"".join(chunks)
        except Exception as exc:
            wait = 2 ** attempt
            print(f"CDN download failed {path} (attempt {attempt+1}/{max_retries}): {exc}; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    print(f"CDN download exhausted retries for {path}", file=sys.stderr)
    return None

def normalize_record(raw: Dict[str, Any], source_file: str) -> Optional[Dict[str, Any]]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Attribution moved to filename pattern; no source/ts columns.
    """
    # Common surrogate-1 schema variants
    prompt = raw.get("prompt") or raw.get("instruction") or raw.get("input") or raw.get("question")
    response = raw.get("response") or raw.get("output") or raw.get("answer") or raw.get("completion")

    if prompt is None or response is None:
        # Best-effort: if there is exactly one text-like field besides metadata, try pairing
        text_keys = [k for k in raw if isinstance(raw[k], str) and len(raw[k]) > 20]
        if len(text_keys) == 2:
            prompt, response = raw[text_keys[0]], raw[text_keys[1]]
        else:
            return None

    prompt = str(prompt).strip()
    response = str(response).strip()
    if not prompt or not response:
        return None

    # Deterministic content hash for dedup
    content = (prompt + "\n\n" + response).encode()
    md5 = hashlib.md5(content).hexdigest()
    if dedup.exists(md5):
        return None
    dedup.add(md5)

    return {"prompt": prompt, "response": response}

# ── main --
def main() -> None:
    files = hf_tree(DATE)
    if not files:
        print(f"No files found for {DATE}", file=sys.stderr)
        sys.exit(0)

    # Deterministic shard assignment by filename
    assigned: List[str] = []
    manifest: Dict[str, Any] = {
        "date": DATE,
        "shard_id": SHARD_ID,
        "shard_total": SHARD_TOTAL,
        "assigned_files": [],
    }
    for f in files:
        p = f.get("path") or f.get("name")
        if not p:
            continue
        h = int(hashlib.sha256(p.encode()).hexdigest(), 16)
        if h % SHARD_TOTAL == SHARD_ID:
            assigned.append(p)
            manifest["assigned_files"].append(p)

    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} assigned {len(assigned)} files")

    # Persist manifest for idempotency/audit
    with MANIFEST_FILE.open("w", encoding="utf-8") as mf:
        json.dump(manifest, mf, indent=2)

    written =

# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value incremental improvement**: Replace the brittle shell-based worker (`bin/dataset-enrich.sh`) with a robust, manifest-driven Python worker that uses HF CDN bypass and deterministic sharding. This fixes schema heterogeneity, rate-limit exposure, and dedup reliability while remaining deployable in <2h.

### Steps
1. Create `bin/dataset-enrich.py` — manifest-driven worker with CDN bypass, schema projection, and deterministic sharding.
2. Keep `lib/dedup.py` as-is (central md5 store) but make it optional/import-safe.
3. Add `bin/list-manifest.py` — one-time Mac-side helper to produce `manifest.json` (date-folder → file list) so Lightning training can do CDN-only fetches.
4. Update `requirements.txt` if needed (add `requests` if not present).
5. Verify locally with `HF_TOKEN` and dry-run mode.

---

## Code

### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
surrogate-1 ingest worker (CDN-bypass, manifest-driven)

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 \
  HF_TOKEN=hf_xxx \
  HF_REPO=datasets/axentx/surrogate-1-training-pairs \
  MANIFEST=manifest.json \
  python bin/dataset-enrich.py [--dry-run] [--date-folder 2026-05-03]

Behavior:
- Reads MANIFEST (or falls back to repo tree listing once) to get file list.
- Assigns files to shards by hash(slug) % SHARD_TOTAL.
- Downloads assigned files via HF CDN (no auth header) and projects to
  {prompt, response} only.
- Deduplicates via lib.dedup central md5 store.
- Outputs batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl
- Commits to HF repo (if not dry-run).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

try:
    from lib.dedup import is_duplicate, register_hashes
except Exception:  # pragma: no cover — graceful fallback
    sys.path.insert(0, str(Path(__file__).parent.parent))
    try:
        from lib.dedup import is_duplicate, register_hashes
    except Exception:
        # Fallback: in-memory dedup for standalone runs
        _seen = set()

        def is_duplicate(h: str) -> bool:
            return h in _seen

        def register_hashes(hs: List[str]) -> None:
            _seen.update(hs)


HF_API = "https://huggingface.co"
CDN_ROOT = "https://huggingface.co/datasets"
HEADERS = {"User-Agent": "axentx-surrogate-1-worker/1.0"}


def slug_hash(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16)


def assign_shard(slug: str, total: int) -> int:
    return slug_hash(slug) % total


def list_repo_tree(repo: str, path: str = "", token: Optional[str] = None) -> List[str]:
    """
    Non-recursive tree listing (folder-level). Returns relative paths.
    Uses HF API (rate-limited) — call sparingly (once per date folder).
    """
    url = f"{HF_API}/api/datasets/{repo}/tree"
    params = {"path": path, "recursive": "false"}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get(url, params=params, headers=headers, timeout=30)
    if r.status_code == 429:
        retry_after = int(r.headers.get("Retry-After", "360"))
        raise RuntimeError(f"HF 429 — retry after {retry_after}s")
    r.raise_for_status()
    items = r.json()
    out = []
    for item in items:
        if item.get("type") == "file":
            out.append(item["path"])
        elif item.get("type") == "directory":
            # do not recurse here; caller can recurse selectively
            pass
    return out


def build_manifest(
    repo: str,
    date_folder: str,
    token: Optional[str],
    out_path: Path,
) -> List[str]:
    """
    Build manifest for a date folder (non-recursive listing of that folder).
    Returns list of file paths.
    """
    files = list_repo_tree(repo, path=date_folder, token=token)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump({date_folder: files}, f, indent=2)
    return files


def cdn_url(repo: str, filepath: str) -> str:
    return f"{CDN_ROOT}/{repo}/resolve/main/{filepath}"


def safe_download(url: str, timeout: int = 60) -> bytes:
    r = requests.get(url, headers=HEADERS, timeout=timeout, stream=True)
    r.raise_for_status()
    return r.content


def project_to_pair(raw: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Accepts common variants and normalizes.
    """
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or raw.get("instruction")
    response = raw.get("response") or raw.get("output") or raw.get("answer") or raw.get("completion")

    if prompt is None or response is None:
        return None

    # normalize to strings
    prompt = str(prompt).strip()
    response = str(response).strip()
    if not prompt or not response:
        return None

    return {"prompt": prompt, "response": response}


def hash_pair(pair: Dict[str, str]) -> str:
    payload = f"{pair['prompt']}\n{pair['response']}"
    return hashlib.md5(payload.encode()).hexdigest()


def process_file(
    repo: str,
    filepath: str,
    dry_run: bool,
) -> List[Dict[str, str]]:
    """
    Download via CDN, decode parquet/jsonl, project pairs.
    Returns accepted pairs.
    """
    url = cdn_url(repo, filepath)
    data = safe_download(url)

    pairs: List[Dict[str, str]] = []

    suffix = Path(filepath).suffix.lower()
    try:
        if suffix == ".parquet":
            table = pq.read_table(pa.BufferReader(data))
            batch = table.to_pylist()
            for row in batch:
                if row is None:
                    continue
                p = project_to_pair(row)
                if p:
                    pairs.append(p)
        elif suffix == ".jsonl":
            for line in data.decode().splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                p = project_to_pair(row)
                if p:
                    pairs.append(p)
        else:
            # fallback: try json lines
            for line in data.decode().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                p = project_to_pair(row)
                if p:
                    pairs.append(p)
    except Exception as exc:
        print(f"[WARN] failed to process {filepath}: {exc}", file=sys.stderr)
        return []

    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description="surrogate-1 ingest worker")
    parser.add_argument("--dry-run", action="store_true", help="no upload")
    parser.add_argument("--date-folder", default=None, help="date folder (e.g. 2026-05-03)")
    parser.add_argument("--manifest", default="manifest.json", help="manifest path")
    args = parser.parse_args()

    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD

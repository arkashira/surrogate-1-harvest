# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Uses a **single API call** from the runner (after rate-limit window) to list one date folder via `list_repo_tree(recursive=False)` → saves `manifest.json`
- Worker loads manifest, deterministically hashes each file path → assigns to shards, processes only its shard
- Downloads via **CDN bypass** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with no Authorization header → avoids `/api/` 429s
- Projects to `{prompt, response}` at parse time (avoids pyarrow CastError on mixed schemas)
- Dedups via central md5 store (`lib/dedup.py`)
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Returns exit code 0 on success, non-zero on failure (GitHub Actions matrix handles retries)

### Changes

1. `bin/dataset-enrich.sh` → rewrite as `bin/dataset-enrich.py` (Bash wrapper kept for backward compat if needed)
2. Add `bin/manifest.py` helper to list+save manifest (optional: can live in `dataset-enrich.py`)
3. Update `.github/workflows/ingest.yml` to pass `DATE` and use matrix `shard_id`

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py --repo axentx/surrogate-1-training-pairs
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
from huggingface_hub import HfApi

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

HF_API_BASE = "https://huggingface.co"
CDN_BASE = "https://huggingface.co/datasets"

HEADERS_API: Dict[str, str] = {}
HEADERS_CDN: Dict[str, str] = {}

# Rate-limit safety: single list call per worker is fine; backoff on 429
def hf_get(path: str, headers: Optional[Dict[str, str]] = None, retries: int = 3) -> Dict:
    url = f"{HF_API_BASE}/api{path}"
    h = headers or {}
    for attempt in range(retries):
        resp = requests.get(url, headers=h, timeout=30)
        if resp.status_code == 429:
            wait = 360
            print(f"[WARN] HF API 429, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Failed to GET {url}")

def list_date_folder(repo_id: str, date: str, token: Optional[str]) -> List[str]:
    """
    List files in datasets/{repo_id}/tree/main/{date} (non-recursive).
    Returns list of relative paths under repo root.
    """
    api = HfApi(token=token)
    try:
        tree = api.list_repo_tree(
            repo_id=repo_id,
            repo_type="dataset",
            path=date,
            recursive=False,
        )
    except Exception:
        # Fallback: raw API call
        if token:
            HEADERS_API["Authorization"] = f"Bearer {token}"
        raw = hf_get(f"/datasets/{repo_id}/tree?path={date}&recursive=false", headers=HEADERS_API)
        tree = [item for item in raw if item.get("type") == "file"]

    paths = []
    for item in tree:
        if hasattr(item, "path"):
            paths.append(item.path)
        elif isinstance(item, dict):
            paths.append(item["path"])
    return sorted(paths)

def build_manifest_if_missing(repo_id: str, date: str, token: Optional[str], out_path: Path) -> List[str]:
    if out_path.exists():
        with open(out_path) as f:
            return json.load(f)
    paths = list_date_folder(repo_id, date, token)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(paths, f)
    print(f"[INFO] Saved manifest with {len(paths)} files to {out_path}")
    return paths

def shard_assign(path: str, shard_id: int, shard_total: int) -> bool:
    """Deterministic shard assignment by path hash."""
    h = int(hashlib.sha256(path.encode()).hexdigest(), 16)
    return (h % shard_total) == shard_id

def download_cdn(repo_id: str, path: str) -> bytes:
    url = f"{CDN_BASE}/{repo_id}/resolve/main/{path}"
    resp = requests.get(url, headers=HEADERS_CDN, timeout=60)
    resp.raise_for_status()
    return resp.content

def parse_parquet_to_pairs(content: bytes):
    """Project mixed-schema parquet to {prompt,response} pairs."""
    import pyarrow.parquet as pq
    import io
    table = pq.read_table(io.BytesIO(content))
    df = table.to_pandas()
    pairs = []
    for _, row in df.iterrows():
        prompt = row.get("prompt") or row.get("input") or row.get("question") or ""
        response = row.get("response") or row.get("output") or row.get("answer") or ""
        if prompt and response:
            pairs.append({"prompt": str(prompt), "response": str(response)})
    return pairs

def parse_jsonl_to_pairs(content: bytes):
    import io
    pairs = []
    for line in io.BytesIO(content).read().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
        response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
        if prompt and response:
            pairs.append({"prompt": str(prompt), "response": str(response)})
    return pairs

def main():
    parser = argparse.ArgumentParser(description="CDN-bypass ingestion worker")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--shard-id", type=int, default=lambda: int(os.getenv("SHARD_ID", "0")))
    parser.add_argument("--shard-total", type=int, default=lambda: int(os.getenv("SHARD_TOTAL", "16")))
    parser.add_argument("--date", default=lambda: os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    parser.add_argument("--token", default=lambda: os.getenv("HF_TOKEN"))
    args = parser.parse_args()

    token = args.token
    if token:
        HEADERS_API["Authorization"] = f"Bearer {token}"

    manifest_path = Path("manifest") / args.date / "files.json"
    paths = build_manifest_if_missing(args.repo, args.date, token, manifest_path)

    my_paths = [p for p in paths if shard_assign(p, args.shard_id, args.shard_total)]
    print(f"[INFO] Shard {args.shard_id}/{args.shard_total} processing {len(my_paths)} files")

    dedup = DedupStore()
    out_dir = Path("batches") / "public-merged" / args.date
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    out_file = out_dir / f"shard{args.shard_id}-{ts}.jsonl"
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped_dup = 0
    for path in my_paths:
        try

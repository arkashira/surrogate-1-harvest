# surrogate-1 / quality

Below is the **single, consolidated implementation** that keeps every strong idea from both candidates while removing duplication and locking in correctness + concrete actionability.

---

## 1. One-shot implementation plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list (rate-limit safe)
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with **no Authorization header** during streaming
- Projects heterogeneous schemas → `{prompt, response}` only at parse time
- Central md5 dedup via existing `lib/dedup.py`
- Outputs: `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Reuses existing `requirements.txt` deps (`datasets`, `huggingface_hub`, `pyarrow`, `numpy`)
- Updates GitHub Actions matrix to use the new Python worker

---

## 2. Worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  HF_TOKEN=hf_xxx \
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  python bin/dataset-enrich.py
"""

from __future__ import annotations

import json
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import requests
from huggingface_hub import HfApi

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
API = HfApi()

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def get_file_list(date_folder: str) -> List[str]:
    """Single API call: list top-level files in date folder."""
    tree = API.list_repo_tree(
        repo_id=HF_REPO,
        path=date_folder,
        repo_type="dataset",
        recursive=False,
    )
    files = [entry.path for entry in tree if entry.type == "file"]
    return sorted(files)

def slug_from_path(path: str) -> str:
    return Path(path).stem

def shard_for(slug: str, total: int) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % total

def cdn_download(repo: str, path: str) -> bytes:
    """
    Download via HF CDN (no Authorization header).
    Public files at resolve/main/ bypass API rate limits.
    """
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return resp.content

def parse_parquet_to_pairs(content: bytes) -> List[Dict[str, str]]:
    import pyarrow.parquet as pq
    import io

    table = pq.read_table(io.BytesIO(content))
    df = table.to_pandas()

    pairs: List[Dict[str, str]] = []
    for _, row in df.iterrows():
        prompt = row.get("prompt") or row.get("input") or row.get("question") or ""
        response = row.get("response") or row.get("output") or row.get("answer") or ""
        if prompt and response:
            pairs.append(
                {
                    "prompt": str(prompt).strip(),
                    "response": str(response).strip(),
                }
            )
    return pairs

def parse_jsonl_to_pairs(content: bytes) -> List[Dict[str, str]]:
    pairs: List[Dict[str, str]] = []
    for line in content.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
        response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
        if prompt and response:
            pairs.append(
                {
                    "prompt": str(prompt).strip(),
                    "response": str(response).strip(),
                }
            )
    return pairs

def parse_file(content: bytes, path: str) -> List[Dict[str, str]]:
    if path.endswith(".parquet"):
        return parse_parquet_to_pairs(content)
    elif path.endswith(".jsonl"):
        return parse_jsonl_to_pairs(content)
    else:
        # Fallback: try jsonl-like lines
        return parse_jsonl_to_pairs(content)

def upload_shard(date_folder: str, shard_id: int, pairs: List[Dict[str, str]], token: str) -> None:
    """Upload shard JSONL to HF dataset repo."""
    timestamp = datetime.now(timezone.utc).strftime("%H%M%S")
    filename = f"shard{shard_id}-{timestamp}.jsonl"
    out_dir = Path("batches/public-merged") / date_folder
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename

    with open(out_path, "w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    API.upload_file(
        path_or_fileobj=str(out_path),
        path_in_repo=f"batches/public-merged/{date_folder}/{filename}",
        repo_id=HF_REPO,
        repo_type="dataset",
        token=token,
        commit_message=f"shard {shard_id} @ {timestamp}",
    )
    print(f"[{utcnow_iso()}] Uploaded {filename} ({len(pairs)} pairs)")

def main() -> None:
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    date = os.getenv("DATE")
    token = os.getenv("HF_TOKEN")

    if not date:
        date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    if not token:
        print("HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    print(f"[{utcnow_iso()}] Worker shard={shard_id}/{shard_total} date={date}")

    dedup = DedupStore()
    all_pairs: List[Dict[str, str]] = []

    try:
        files = get_file_list(date)
    except Exception as exc:
        print(f"[{utcnow_iso()}] Failed to list repo tree for {date}: {exc}", file=sys.stderr)
        files = []

    if not files:
        print(f"[{utcnow_iso()}] No files found for {date}")
        sys.exit(0)

    print(f"[{utcnow_iso()}] Found {len(files)} files, processing shard {shard_id}")

    for fpath in files:
        slug = slug_from_path(fpath)
        if shard_for(slug, shard_total) != shard_id:
            continue

        try:
            content = cdn_download(HF_REPO, fpath)
            pairs = parse_file(content, fpath)
        except Exception as exc:
            print(f"[{utcnow_iso()}] Failed to process {fpath}: {exc}", file=sys.stderr)
            continue

        for p in pairs:
            blob = f"{p['prompt']}\n{p['response']}".encode("utf-8")
            md5 = hashlib.md5(blob).hexdigest()
            if dedup.exists(md5):
                continue
            dedup.add(md5)
            all_pairs.append(p)

    print(f"[{utcnow_iso()}] Shard {shard_id} collected {len(all_pairs)} unique pairs")

    if all_pairs:
        upload_shard(date,

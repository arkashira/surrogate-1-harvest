# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` (fallback to env)
- Uses a **single** `list_repo_tree` call per date folder and **caches** the result to `manifest.json` (enables parallel shard runs without repeated API calls)
- Deterministic shard assignment via `hash(slug) % SHARD_TOTAL`
- Downloads via HF CDN (`resolve/main/...`) — **zero auth/API calls during data fetch** (bypasses 429)
- Projects each file to `{prompt, response}` **only at parse time** (avoids mixed-schema pyarrow errors)
- Deduplicates via central `lib/dedup.py` SQLite md5 store
- Writes output to `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Includes retry/backoff for CDN flakiness and cleans local cache after each file to bound disk usage

### Steps (≤2h)

1. Create `bin/dataset-enrich.py` (≈120–150 LoC)
2. Keep `lib/dedup.py` unchanged (central SQLite md5 store)
3. Update `.github/workflows/ingest.yml` to run the Python script with a matrix strategy (`SHARD_ID: [0..15]`)
4. Add/update `requirements.txt` with `requests tqdm huggingface-hub datasets`

---

## Code

### bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx python bin/dataset_enrich.py

Behavior:
- list_repo_tree(date_folder) once and cache to manifest.json
- deterministic shard assignment by hash(slug) % SHARD_TOTAL
- download via CDN resolve/main/ (no auth/API during fetch)
- project to {prompt,response} at parse time
- dedup via lib.dedup (SQLite md5 store)
- write shard-N output JSONL
"""

from __future__ import annotations

import json
import os
import sys
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

import requests
from huggingface_hub import HfApi
from datasets import load_dataset

# Local
from lib.dedup import is_duplicate, mark_seen  # type: ignore

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
DATE_FMT = "%Y-%m-%d"
CDN_BASE = f"https://huggingface.co/{HF_REPO}/resolve/main"

MAX_RETRIES = 3
BACKOFF = 5  # seconds

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

def _hash_slug(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16)

def list_date_files(date_str: str, manifest_path: Path) -> List[str]:
    """
    Single API call to list files in date folder, cached to manifest_path.
    """
    if manifest_path.exists():
        try:
            with open(manifest_path, encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, list) and all(isinstance(x, str) for x in cached):
                return cached
        except Exception:
            pass

    api = HfApi(token=os.getenv("HF_TOKEN"))
    tree = api.list_repo_tree(repo_id=HF_REPO, path=date_str, repo_type="dataset")
    files = sorted(item.rfilename for item in tree if item.type == "file")

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(files, f)

    return files

def download_via_cdn(repo_path: str, dest: Path) -> bool:
    url = f"{CDN_BASE}/{repo_path}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=30, stream=True)
            resp.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception as exc:
            if attempt == MAX_RETRIES:
                print(f"CDN download failed after {MAX_RETRIES} tries: {url} -> {exc}", file=sys.stderr)
                return False
            time.sleep(BACKOFF * attempt)
    return False

def project_to_pair(local_path: Path) -> Iterable[Dict[str, str]]:
    """
    Project file to {prompt,response} only at parse time.
    Supports common HF dataset file types: jsonl, parquet, json.
    """
    suffix = local_path.suffix.lower()
    try:
        if suffix == ".parquet":
            ds = load_dataset("parquet", data_files=str(local_path), split="train", streaming=True)
        elif suffix == ".jsonl":
            ds = load_dataset("json", data_files=str(local_path), split="train", streaming=True)
        elif suffix == ".json":
            ds = load_dataset("json", data_files=str(local_path), split="train", streaming=True)
        else:
            print(f"Unsupported file type {suffix}, skipping", file=sys.stderr)
            return

        for row in ds:
            prompt = row.get("prompt") or row.get("input") or row.get("question") or ""
            response = row.get("response") or row.get("output") or row.get("answer") or ""
            if isinstance(prompt, str) and isinstance(response, str):
                yield {"prompt": prompt.strip(), "response": response.strip()}
    except Exception as exc:
        print(f"Failed to project {local_path}: {exc}", file=sys.stderr)

def process_shard(files: List[str], shard_id: int, shard_total: int, date_str: str) -> List[Dict[str, Any]]:
    assigned = [f for f in files if _hash_slug(f) % shard_total == shard_id]
    print(f"Shard {shard_id}/{shard_total} assigned {len(assigned)} files")

    results: List[Dict[str, Any]] = []
    cache_dir = Path(".cache/cdn")
    for repo_path in assigned:
        slug = repo_path.replace("/", "_")
        local_path = cache_dir / slug

        if not download_via_cdn(repo_path, local_path):
            continue

        for pair in project_to_pair(local_path):
            content = json.dumps(pair, sort_keys=True)
            md5 = hashlib.md5(content.encode()).hexdigest()
            if is_duplicate(md5):
                continue
            mark_seen(md5)
            pair["_meta"] = {
                "source_file": repo_path,
                "shard": shard_id,
                "ingest_ts": _now(),
            }
            results.append(pair)

        try:
            local_path.unlink(missing_ok=True)
        except Exception:
            pass

    return results

def write_output(pairs: List[Dict[str, Any]], date_str: str, shard_id: int) -> Path:
    out_dir = Path(f"batches/public-merged/{date_str}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"shard{shard_id}-{_now()}.jsonl"
    with open(out_file, "w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    return out_file

def main() -> None:
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    date_str = os.getenv("DATE", datetime.utcnow().strftime(DATE_FMT))
    hf_token = os.getenv("HF_TOKEN")
    if not hf_token

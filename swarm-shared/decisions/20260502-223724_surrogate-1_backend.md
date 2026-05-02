# surrogate-1 / backend

Below is the single, merged implementation that keeps every concrete, high-leverage idea from both proposals and removes contradictions.  
The plan is ordered so you can execute it in ≤ 2 h and immediately stop HF API 429s during training.

---

## Final plan (≤2 h)

**Highest-value improvement**  
Add deterministic pre-flight file listing + CDN-only ingestion so shard workers and training jobs make **zero** `/api/` calls and avoid 429s and mixed-schema pyarrow errors.

1. `bin/list-files.py` – one-time script that calls `list_repo_tree` once per folder and writes `file-list.json` (path, size, sha256, cdn_url).  
2. `lib/cdn_stream.py` – robust CDN iterator that downloads via raw CDN URLs with retries/backoff and yields only `{prompt, response}` (no schema coercion).  
3. `bin/cdn-fetch.py` – standalone downloader used by `dataset-enrich.sh` for parquet→jsonl projection.  
4. Update `bin/dataset-enrich.sh` to accept `FILE_LIST` and use CDN-first workflow while preserving existing upload path.  
5. Add minimal `bin/train-cdn.py` snippet showing Lightning + CDN-only training.

---

## 1) `bin/list-files.py`

Deterministic, one-call-per-folder file listing.

```python
#!/usr/bin/env python3
"""
Usage:
  HF_TOKEN=... python bin/list-files.py \
    --repo axentx/surrogate-1-training-pairs \
    --out file-list.json \
    [--folder batches/public-merged/2026-05-02]

Writes:
{
  "repo": "...",
  "folder": "...",
  "generated_at_utc": "...",
  "files": [
    {"path": "...", "size": 123, "sha256": "...", "cdn_url": "..."},
    ...
  ],
  "count": N
}
"""

import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi, RepositoryTreeEntry

CDN_BASE = "https://huggingface.co/datasets"

def list_folder(api: HfApi, repo: str, folder: str) -> list[dict]:
    entries = api.list_repo_tree(repo=repo, path=folder.rstrip("/"), recursive=False)
    out = []
    for e in entries:
        if isinstance(e, RepositoryTreeEntry) and e.type == "file":
            out.append({
                "path": e.path,
                "size": e.size or 0,
                "lfs": getattr(e, "lfs", None) is not None,
                "sha256": getattr(e, "sha256", None),
                "cdn_url": f"{CDN_BASE}/{repo}/resolve/main/{e.path}"
            })
    out.sort(key=lambda x: x["path"])
    return out

def main() -> None:
    parser = argparse.ArgumentParser(description="List dataset files for CDN ingestion")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--out", default="file-list.json")
    parser.add_argument("--folder", default="batches/public-merged")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        print("ERROR: HF_TOKEN env var required", file=sys.stderr)
        sys.exit(1)

    api = HfApi(token=token)
    folder = args.folder.rstrip("/")

    try:
        files = list_folder(api, args.repo, folder)
    except Exception as exc:
        print(f"ERROR listing repo tree: {exc}", file=sys.stderr)
        sys.exit(1)

    payload = {
        "repo": args.repo,
        "folder": folder,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "files": files,
        "count": len(files),
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

```bash
chmod +x bin/list-files.py
```

---

## 2) `lib/cdn_stream.py`

CDN-only iterator that yields `{prompt, response}` without schema coercion.

```python
"""
lib/cdn_stream.py

Usage:
  from lib.cdn_stream import iter_cdn_parquet
  for item in iter_cdn_parquet(file_list, max_retries=5):
      ...
"""

import json
import time
from pathlib import Path
from typing import Iterable, Dict, Any

import pyarrow.parquet as pq
import requests

CDN_TIMEOUT = 30

def _download(url: str, out_path: Path, max_retries: int = 5) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, max_retries + 1):
        try:
            with requests.get(url, stream=True, timeout=CDN_TIMEOUT) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return
        except Exception as exc:
            wait = 2 ** attempt
            print(f"CDN download attempt {attempt}/{max_retries} failed: {exc}. Retrying in {wait}s...")
            time.sleep(wait)
    raise RuntimeError(f"Failed to download {url} after {max_retries} attempts")

def iter_cdn_parquet(
    file_list_path: str,
    work_dir: str = ".cdn_cache",
    max_retries: int = 5,
    fields=("prompt", "response"),
) -> Iterable[Dict[str, Any]]:
    """
    Given file-list.json, download each parquet via CDN and yield projected rows.
    """
    with open(file_list_path, encoding="utf-8") as f:
        manifest = json.load(f)

    work_dir = Path(work_dir)
    for item in manifest["files"]:
        if not item["path"].lower().endswith(".parquet"):
            continue
        local_path = work_dir / item["path"]
        if not local_path.exists():
            _download(item["cdn_url"], local_path, max_retries=max_retries)

        table = pq.read_table(local_path, columns=fields)
        for batch in table.to_batches():
            cols = {name: batch.column(name).to_pylist() for name in fields}
            for i in range(batch.num_rows):
                yield {k: cols[k][i] for k in fields}
```

---

## 3) `bin/cdn-fetch.py`

Standalone CDN downloader for shell workflows.

```python
#!/usr/bin/env python3
"""
Download via HuggingFace CDN with retries.

Usage:
  python bin/cdn-fetch.py --url <cdn_url> --out ./local.parquet [--max-retries 5]
"""

import argparse
import shutil
import sys
import time
from pathlib import Path

import requests

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 5

def cdn_download(url: str, out_path: Path, max_retries: int = MAX_RETRIES) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, max_retries + 1):
        try:
            with requests.get(url, stream=True, timeout=DEFAULT_TIMEOUT) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    shutil.copyfileobj(r.raw, f)
            return
        except Exception as exc:
            wait = 2 ** attempt
            print(f"Attempt {attempt}/{max_retries} failed: {exc}. Retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Failed to download {url} after {max_retries} attempts")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES)
    args = parser.parse_args()

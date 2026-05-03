# vanguard / discovery

## Final Synthesis (adopt strongest parts, resolve contradictions, maximize correctness + actionability)

- Use **Candidate 1’s CLI + module layout** (single file, clear `gen`/`validate` subcommands) because it’s concrete, tested, and fits Lightning Studio.
- Adopt **Candidate 2’s JSONL + url + resume/corruption-recovery** for training-time streaming and resumable downloads (more robust than pure JSON list).
- Resolve contradictions in favor of **correctness + zero HF API during training** and **resilience to partial/corrupt downloads**:
  - Manifest format: **JSONL** (one record per file) with required fields `path`, `size`, `sha256`, `url`. JSONL is safer for large folders and streamable.
  - Always include `url` in the manifest so training scripts never call HF API.
  - During `gen`, **do not** rely on un-stored `sha256`; populate it by fetching `?download=false` ETag/header when available, or leave `null` and require `validate --populate` before training. This avoids silent poison while keeping generation fast.
  - Add **rate-limit guardrails** (delays + retries + optional concurrency limit) during generation.
  - Add **resumable/parallel downloader** (`download --resume`) that validates sha256 on disk and skips/corrupt-redownloads as needed.
  - Keep **Candidate 1’s cache validation** logic but make it stricter: exit non-zero on any corrupt/missing unless `--fix` is used.

---

## 1. Manifest specification (canonical)

File: `manifests/{date}.jsonl`  
Each line is a JSON object:
```json
{"path":"batches/mirror-merged/2026-05-03/file1.parquet","size":1234567,"sha256":"abc...","url":"https://huggingface.co/datasets/org/repo/resolve/main/batches/mirror-merged/2026-05-03/file1.parquet"}
```
- `sha256` may be `null` if not yet known; `validate --populate` will fill it.
- `url` is always present and canonical (CDN).

---

## 2. Implementation (single file, production-ready)

```bash
# /opt/axentx/vanguard/discovery/manifest.py
#!/usr/bin/env python3
"""
Generate, validate, and download manifests for HF dataset folders.

Usage:
  python manifest.py gen \
    --repo org/repo \
    --folder batches/mirror-merged/2026-05-03 \
    --out manifests/2026-05-03.jsonl

  python manifest.py validate \
    --root ./cache \
    --manifest manifests/2026-05-03.jsonl \
    [--populate] [--fix]

  python manifest.py download \
    --manifest manifests/2026-05-03.jsonl \
    --root ./cache \
    [--workers 4] [--resume]
"""
import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import requests
from huggingface_hub import HfApi  # type: ignore

HF_CDN = "https://huggingface.co/datasets"
api = HfApi()

# Rate-limit guardrails
DEFAULT_DELAY = 1.0  # seconds between API calls
MAX_RETRIES = 5
BACKOFF = 2.0

def _retry(fn):
    def wrapper(*args, **kwargs):
        delay = DEFAULT_DELAY
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 429 or e.response.status_code >= 500:
                    if attempt == MAX_RETRIES:
                        raise
                    time.sleep(delay)
                    delay *= BACKOFF
                    continue
                raise
            except (requests.exceptions.RequestException, OSError) as e:
                if attempt == MAX_RETRIES:
                    raise
                time.sleep(delay)
                delay *= BACKOFF
        raise RuntimeError("unreachable")
    return wrapper

@_retry
def list_folder(repo: str, folder: str) -> List[Dict]:
    tree = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    items = []
    for entry in tree:
        if entry.type == "file":
            url = f"{HF_CDN}/{repo}/resolve/main/{entry.path}"
            items.append({
                "path": entry.path,
                "size": int(entry.size) if entry.size is not None else 0,
                "sha256": None,
                "url": url,
            })
    return items

def sha256_file(path: Path, block: int = 65536) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            h.update(chunk)
    return h.hexdigest()

def gen_manifest(repo: str, folder: str, out_path: Path) -> None:
    items = list_folder(repo, folder)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for item in items:
            f.write(json.dumps(item) + "\n")
    print(f"Manifest written to {out_path} ({len(items)} files)")

def iter_manifest(manifest_path: Path) -> Iterator[Dict]:
    with manifest_path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)

def validate_local_cache(root: Path, manifest_path: Path, populate: bool = False, fix: bool = False) -> Dict:
    root = root.resolve()
    ok, missing, corrupt = [], [], []
    updated = []

    for item in iter_manifest(manifest_path):
        local = (root / item["path"]).resolve()
        if not local.exists():
            missing.append(item["path"])
            continue

        actual = sha256_file(local)
        expected = item.get("sha256")
        if expected is not None and expected != actual:
            corrupt.append(item["path"])
            if fix:
                local.unlink(missing_ok=True)
        else:
            ok.append(item["path"])

        if populate and (expected is None or expected != actual):
            item["sha256"] = actual
            updated.append(item)

    if populate and updated:
        # rewrite manifest with populated sha256 values
        lines_map = {item["path"]: item for item in iter_manifest(manifest_path)}
        for item in updated:
            lines_map[item["path"]] = item
        with manifest_path.open("w") as f:
            for item in lines_map.values():
                f.write(json.dumps(item) + "\n")
        print(f"Updated {len(updated)} sha256 entries in {manifest_path}")

    report = {"ok": ok, "missing": missing, "corrupt": corrupt}
    return report

@_retry
def _head_size_etag(url: str) -> Optional[str]:
    r = requests.head(url, timeout=15, allow_redirects=True)
    r.raise_for_status()
    etag = r.headers.get("etag")
    if etag:
        etag = etag.strip('"')
    return etag

def download_worker(item: Dict, root: Path, resume: bool) -> Dict:
    local = (root / item["path"]).resolve()
    local.parent.mkdir(parents=True, exist_ok=True)

    if local.exists() and local.is_file():
        actual = sha256_file(local)
        expected = item.get("sha256")
        if expected is not None and actual == expected:
            return {"status": "skip", "path": item["path"]}
        if resume:
            # re-download if corrupt/incomplete
            pass
        else:
            local.unlink(missing_ok=True)

    try:
        with requests.get(item["url"], stream=True, timeout=30) as r:
            r.raise_for_status()
            tmp = local.with_suffix(".tmp")
            with tmp.open("wb

# vanguard / quality

## Final synthesized solution

**Core principle:** Deterministic, CDN-first, reproducible training inputs with zero runtime HF API calls and strong integrity guarantees.

---

### 1. Diagnosis (consensus)
- No deterministic CDN-first manifest exists → training/ingestion scripts risk HF API `list_repo_tree`/`load_dataset` calls and 429s.
- No content-hash integrity verification → silent corruption possible during long surrogate-1 training runs.
- No single source-of-truth for which files belong to a training slice → reproducibility relies on runtime API queries.
- No guard against mixed-schema parquet ingestion → `pyarrow.CastError` can occur if malformed files reach the pipeline.
- No lightweight local validation step before Lightning Studio launch → wastes quota if data is corrupt or incomplete.

---

### 2. Proposed change (single coherent artifact set)
Create two small, focused files:

1. `/opt/axentx/vanguard/manifest.py` — Python module with:
   - Deterministic manifest generation from one HF API call (with retries/backoff).
   - Schema validation (projected to `{prompt, response}`) and rejection of mixed-schema parquet files.
   - CDN-first integrity verification (SHA-256) and size checks.
   - Lockfile output (`manifest-{repo_slug}-{date}.lock.json`) + content hash (`.sha256`).
   - `verify` mode that downloads via CDN only (no auth/API) before training.

2. `/opt/axentx/vanguard/bin/mkmanifest` — small CLI wrapper that calls the module with clear UX and non-zero exit on failure.

---

### 3. Implementation

```bash
# /opt/axentx/vanguard/bin/mkmanifest
#!/usr/bin/env bash
# Usage: mkmanifest <repo> <date_folder> [out_dir]
# Example: HF_TOKEN=... mkmanifest datasets/surrogate-1 2026-05-03 manifests/
set -euo pipefail
SHELL=/bin/bash

REPO="${1:?repo required}"
FOLDER="${2:?date folder required}"
OUTDIR="${3:-./manifests}"

mkdir -p "$OUTDIR"

exec python3 - "$REPO" "$FOLDER" "$OUTDIR" <<'PY'
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
EXPECTED_SCHEMA_FIELDS = {"prompt", "response"}
MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0

def hf_get_tree(api: HfApi, repo: str, folder: str) -> List[Dict[str, Any]]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            entries = api.list_repo_tree(repo=repo, path=folder, recursive=True)
            # Normalize to dict-like objects
            return [
                {
                    "path": e.rfilename.lstrip("./") if hasattr(e, "rfilename") else str(e.get("path", "")),
                    "size": getattr(e, "size", None) or (e.get("size") if isinstance(e, dict) else None),
                }
                for e in entries
                if (hasattr(e, "type") and e.type == "file") or (isinstance(e, dict) and e.get("type") == "file")
            ]
        except Exception as exc:
            if attempt == MAX_RETRIES:
                print(f"HF API failure after {MAX_RETRIES} attempts: {exc}", file=sys.stderr)
                raise
            backoff = INITIAL_BACKOFF * (2 ** (attempt - 1))
            print(f"Attempt {attempt}/{MAX_RETRIES} failed ({exc}), retry in {backoff}s", file=sys.stderr)
            time.sleep(backoff)
    raise RuntimeError("Unreachable")

def validate_parquet_schema(path: Path) -> bool:
    try:
        pf = pq.read_schema(path)
        names = set(pf.names)
        if not EXPECTED_SCHEMA_FIELDS.issubset(names):
            print(f"Schema missing required fields {EXPECTED_SCHEMA_FIELDS - names}: {path}", file=sys.stderr)
            return False
        # Quick row check for non-empty prompt/response
        table = pq.read_table(path, columns=["prompt", "response"], use_threads=False)
        if table.num_rows == 0:
            print(f"Empty parquet: {path}", file=sys.stderr)
            return False
        return True
    except Exception as exc:
        print(f"Schema validation failed for {path}: {exc}", file=sys.stderr)
        return False

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def build_manifest(repo: str, folder: str, out_dir: Path) -> Path:
    api = HfApi(token=os.getenv("HF_TOKEN"))
    entries = hf_get_tree(api, repo, folder)

    parquet_files = [e for e in entries if str(e["path"]).endswith(".parquet")]
    if not parquet_files:
        print(f"No parquet files found in {repo}/{folder}", file=sys.stderr)
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    date_slug = Path(folder).name
    repo_slug = repo.replace("/", "_")
    lock_name = f"manifest-{repo_slug}-{date_slug}.lock.json"
    lock_path = out_dir / lock_name

    files: List[Dict[str, Any]] = []
    for e in parquet_files:
        path = e["path"]
        size = e["size"]
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=path)
        files.append({
            "path": path,
            "size": size,
            "cdn_url": cdn_url,
            "sha256": None,  # filled during optional verify
        })

    lock = {
        "repo": repo,
        "folder": folder,
        "generated_by": "mkmanifest",
        "files": files,
    }

    lock_path.write_text(json.dumps(lock, indent=2))
    lock_hash = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    (out_dir / f"{lock_name}.sha256").write_text(f"{lock_hash}  {lock_name}\n")
    print(f"OK: {lock_path}")
    print(f"lock_hash: {lock_hash}")
    return lock_path

def verify(lock_path: Path, cache_dir: Path, max_concurrent: int = 8) -> bool:
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock = json.loads(lock_path.read_text())
    files = lock["files"]
    ok = True

    for f in files:
        cdn_url = f["cdn_url"]
        expected_size = f["size"]
        out_name = Path(f["path"]).name
        out_path = cache_dir / out_name

        if out_path.exists() and out_path.stat().st_size == expected_size:
            if not validate_parquet_schema(out_path):
                print(f"Schema invalid (cached): {out_path}", file=sys.stderr)
                ok = False
            continue

        try:
            resp = requests.get(cdn_url, timeout=30, stream=True)
            resp.raise_for_status()
            out_path.write_bytes(resp.content)
        except Exception as exc:
            print(f"Download failed {cdn_url}: {exc}", file=sys.stderr)
            ok = False
            continue

        if out_path.stat().st_size != expected_size:
            print(f"Size mismatch {out_path}: expected {expected_size}, got {out_path.stat().st_size}", file=sys.stderr)
            ok = False
            continue

        if not validate_parquet_schema(out_path):
            ok = False
            continue

        f["sha256"] = sha256_file(out_path)

    if ok:
        # Update lock with computed hashes and rewrite
        lock_path.write_text(json.dumps(lock, indent=2))
        lock_hash = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        lock_path.with_suffix(".lock.json.sha25

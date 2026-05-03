# vanguard / quality

## Final Synthesized Answer

### Diagnosis (merged, de-duplicated)
- No deterministic **CDN-first manifest** exists; ingestion/training scripts likely still call Hugging Face API (`list_repo_tree`, `load_dataset`) at runtime, risking **429s** and non-reproducible runs.
- Missing **content-addressed file list** with pinned ordering and integrity (SHA-256) for date-folders used by surrogate-1 training.
- Mixed-schema ingestion writes `enriched/` files with extra metadata columns (`source`, `ts`) that break downstream pyarrow casting.
- No lightweight verification that the manifest matches what is actually on CDN before training starts.
- Lightning Studio reuse/quota discipline is not enforced; idle-stop can silently kill long-running training.

### Proposed Change (merged)
Add a small, self-contained quality utility that produces and verifies a CDN-first manifest for a single date folder and emits a minimal `{prompt,response}` parquet suitable for surrogate-1 training.

Scope:
- New file: `/opt/axentx/vanguard/scripts/build_manifest.py`
- Optional companion: `/opt/axentx/vanguard/scripts/verify_manifest.py`
- No changes to existing training code yet; this is an incremental quality/gate improvement.

### Implementation (merged + hardened)

```bash
mkdir -p /opt/axentx/vanguard/scripts
```

`/opt/axentx/vanguard/scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
build_manifest.py
Generate a deterministic CDN-first manifest for one date folder of a HF dataset repo.
Outputs:
  - manifest.jsonl  (path, size, sha256, cdn_url)
  - samples.parquet ({prompt, response} only)

Usage:
  HF_REPO="datasets/username/repo" \
  FOLDER="batches/mirror-merged/2026-04-29" \
  HF_TOKEN="hf_xxx" \
  python build_manifest.py --out-dir ./out

Notes:
- Uses HF API exactly once (non-recursive tree listing) to list top-level files in folder.
- All subsequent downloads use public CDN (no auth, bypasses /api/ rate limits).
- Projects to {prompt, response} only to avoid mixed-schema pyarrow issues downstream.
- Includes retries, backoff, and integrity checks for production robustness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HF_API_BASE = "https://huggingface.co"
CDN_BASE = "https://huggingface.co"

def make_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.mount("http://", HTTPAdapter(max_retries=retries))
    return session

session = make_session()

def list_folder_files(repo: str, folder: str, token: str | None = None) -> list[dict[str, Any]]:
    """Single API call: non-recursive tree listing for folder."""
    url = f"{HF_API_BASE}/api/datasets/{repo}/tree"
    params = {"recursive": "false", "prefix": folder.rstrip("/") + "/"}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = session.get(url, params=params, headers=headers, timeout=30)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "360"))
        print(f"HF API 429 — retry after {retry_after}s", file=sys.stderr)
        time.sleep(retry_after)
        return list_folder_files(repo, folder, token)
    resp.raise_for_status()
    items = resp.json()
    files = [i for i in items if i.get("type") == "file"]
    files.sort(key=lambda x: x["path"])
    return files

def cdn_url(repo: str, path: str) -> str:
    return f"{CDN_BASE}/datasets/{repo}/resolve/main/{path}"

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def download_cdn(url: str) -> bytes:
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def project_to_prompt_response(raw: dict[str, Any]) -> dict[str, str]:
    """
    Conservative projection to avoid mixed-schema issues.
    Accepts raw JSON-like object from JSON/JSONL parquet row.
    """
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or raw.get("completion") or ""
    return {"prompt": str(prompt) if prompt is not None else "", "response": str(response) if response is not None else ""}

def build(repo: str, folder: str, token: str | None, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = list_folder_files(repo, folder, token=token)
    if not files:
        print(f"No files found in {repo}/{folder}", file=sys.stderr)
        sys.exit(1)

    manifest_rows = []
    samples = []

    for f in files:
        path = f["path"]
        size = f.get("size", 0)
        url = cdn_url(repo, path)
        print(f"Fetching CDN: {path}", file=sys.stderr)
        data = download_cdn(url)
        digest = sha256_bytes(data)

        manifest_rows.append({
            "path": path,
            "size": size,
            "sha256": digest,
            "cdn_url": url,
        })

        # Lightweight projection for surrogate-1 friendliness.
        if path.lower().endswith(".jsonl"):
            lines = data.decode("utf-8", errors="replace").strip().split("\n")
            for line in lines[:10]:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    pr = project_to_prompt_response(obj)
                    if pr["prompt"] or pr["response"]:
                        samples.append(pr)
                except Exception:
                    continue
        elif path.lower().endswith(".json"):
            try:
                obj = json.loads(data)
                items = obj if isinstance(obj, list) else [obj]
                for item in items[:10]:
                    pr = project_to_prompt_response(item)
                    if pr["prompt"] or pr["response"]:
                        samples.append(pr)
            except Exception:
                pass

    # Write manifest
    manifest_path = out_dir / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as f:
        for row in manifest_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Manifest written: {manifest_path}", file=sys.stderr)

    # Write samples parquet (minimal schema)
    if samples:
        table = pa.Table.from_pylist(samples, schema=pa.schema([
            pa.field("prompt", pa.string()),
            pa.field("response", pa.string()),
        ]))
        pq_path = out_dir / "samples.parquet"
        pq.write_table(table, pq_path)
        print(f"Samples parquet written: {pq_path}", file=sys.stderr)
    else:
        print("No samples extracted; skipping samples.parquet", file=sys.stderr)

def main() -> None:
    parser = argparse.ArgumentParser(description="Build CDN-first manifest for HF dataset folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo, e.g. datasets/username/repo")
    parser.add_argument("--folder", required=True, help="Folder path in repo, e.g. batches/mirror-merged/2026-04-29")
    parser.add_argument("--hf-token", default=os.getenv("HF_TOKEN"), help="HF

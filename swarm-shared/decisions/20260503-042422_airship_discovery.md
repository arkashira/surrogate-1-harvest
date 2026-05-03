# airship / discovery

## Final Integrated Plan  
*Best parts + resolved contradictions + concrete, correct, actionable*

---

## Goal (unchanged)
Eliminate HF API 429s during Surrogate training and prevent Lightning Studio idle-stop training loss.  
**Target delivery:** <2h, single PR, no infra changes.

---

## Resolved Contradictions
- **Manifest scope:** Candidate 1 used a date folder at dataset root; Candidate 2 used `batches/mirror-merged/YYYY-MM-DD/`.  
  **Resolution:** Use Candidate 2 path (`batches/mirror-merged/YYYY-MM-DD/`) — it matches the actual mirror layout and avoids confusion. Keep manifest generation flexible via CLI so it works for either layout if needed.

- **Manifest storage:** Candidate 1 wrote to `manifests/{date}.json` locally; Candidate 2 suggested committing or uploading to a config bucket.  
  **Resolution:** Write locally by default and provide an option to commit (via git) or upload to a small config bucket. Default keeps the change minimal and safe under HF commit caps.

- **Studio reuse vs restart logic:** Candidate 1 reused running Studio and restarted only if stopped; Candidate 2 emphasized checking status and auto-restarting before each run.  
  **Resolution:** Combine — reuse if running; if stopped (idle-timeout), restart automatically on the same machine type before running the training script.

- **HTTP client in training:** Candidate 1 used `aiohttp` + async streaming; Candidate 2 omitted transport details.  
  **Resolution:** Keep Candidate 1’s async CDN fetch (fast, non-blocking) but make it optional/simple — provide both sync (`requests`) and async (`aiohttp`) helpers so `train.py` can choose.

---

## Implementation Plan (≤2h)

| Step | Owner | Time | Description |
|------|-------|------|-------------|
| 1 | me | 15m | Add `scripts/build_cdn_manifest.py` — one-shot script that lists `batches/mirror-merged/{date}/` (non-recursive) and emits `manifests/cdn-manifest-{date}.json` with CDN URLs and sizes. |
| 2 | me | 20m | Add `surrogate/training/cdn_loader.py` — helpers to read manifest and fetch parquet files via CDN (sync + async). Update `train.py` to accept `--manifest` and load via CDN only (zero HF API during dataload). |
| 3 | me | 20m | Add `surrogate/training/studio_lifecycle.py` — get or create Studio, reuse if running, restart if idle-stopped, with prioritized machine fallback (L40S → A100 → H200 on `lightning-lambda-prod`). |
| 4 | me | 30m | Tests: dry-run manifest on a small date; verify CDN fetch of 100 rows; verify lifecycle detects stopped Studio and restarts; run `run_training.sh` end-to-end without HF 429. |
| 5 | me | 15m | Update docs: add one-line manifest generation and `scripts/run_training.sh {date}` usage to README “Training (Surrogate)”. |

---

## Code Snippets (integrated + corrected)

### 1) `scripts/build_cdn_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate a deterministic CDN manifest for a date folder in the mirror repo.

Usage:
    python scripts/build_cdn_manifest.py --repo surrogate-data \
        --date 2026-05-03 \
        --out manifests/cdn-manifest-2026-05-03.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date: str, out_path: Path, token: str | None = None) -> None:
    api = HfApi(token=token)

    # Prefer mirror layout; keep generic fallback to plain date folder
    for prefix in [f"batches/mirror-merged/{date}", date]:
        try:
            entries = api.list_repo_tree(repo_id=repo, path=prefix, recursive=False)
            break
        except Exception:
            continue
    else:
        raise FileNotFoundError(f"No folder found for date {date} in {repo}")

    files = []
    for entry in entries:
        path = getattr(entry, "path", str(entry))
        if path.endswith(".parquet"):
            files.append(
                {
                    "path": path,
                    "cdn_url": CDN_TEMPLATE.format(repo=repo, path=path),
                    "size": getattr(entry, "size", None),
                }
            )

    files.sort(key=lambda f: f["path"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"repo": repo, "date": date, "prefix": prefix, "files": files}
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {len(files)} files to {out_path}")

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN manifest for a date folder.")
    parser.add_argument("--repo", default="surrogate-data", help="HF dataset repo")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-05-03)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--token", default=os.getenv("HF_TOKEN", None), help="HF token (optional for public repos)")
    args = parser.parse_args()

    build_manifest(repo=args.repo, date=args.date, out_path=Path(args.out), token=args.token)

if __name__ == "__main__":
    main()
```

---

### 2) `surrogate/training/cdn_loader.py`

```python
import json
from pathlib import Path
from typing import List, Dict, Any
import pyarrow.parquet as pq
from io import BytesIO

try:
    import aiohttp
    ASYNC_AVAILABLE = True
except Exception:
    ASYNC_AVAILABLE = False

import requests

def load_manifest(manifest_path: str) -> Dict[str, Any]:
    with open(manifest_path) as f:
        return json.load(f)

def fetch_via_cdn_sync(url: str, timeout: int = 30) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

async def fetch_via_cdn_async(session: "aiohttp.ClientSession", url: str) -> bytes:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()

def load_rows_from_manifest_sync(manifest_path: str, limit: int = 10_000):
    import pandas as pd
    manifest = load_manifest(manifest_path)
    rows = []
    for meta in manifest["files"]:
        data = fetch_via_cdn_sync(meta["cdn_url"])
        table = pq.read_table(BytesIO(data))
        df = table.select(["prompt", "response"]).to_pandas()
        rows.append(df)
        if sum(len(x) for x in rows) >= limit:
            break
    return pd.concat(rows, ignore_index=True).head(limit)

async def load_rows_from_manifest_async(manifest_path: str, limit: int = 10_000):
    if not ASYNC_AVAILABLE:
        return load_rows_from_manifest_sync(manifest_path, limit)

    import pandas as pd
    import aiohttp
    manifest = load_manifest(manifest_path)
    rows = []
    async with aiohttp.ClientSession() as session:
        for meta in manifest["files"]:
            data = await fetch_via_cdn_async(session, meta["cdn_url"])
            table = pq.read_table(BytesIO(data))
            df = table.select(["prompt", "response"]).to_pandas()
            rows.append(df)
            if sum(len(x) for x in rows) >= limit:
                break
    return pd.concat(rows, ignore_index=True).head(limit)
```

---

### 3) `surrogate/training/studio_lifecycle.py`

```python
from lightning import Lightning, Teamspace, Studio, Machine

LIGHTNING_ACCOUNT = "lightning-lambda-prod"  # paid; fallback to public-prod for L40S
STUDIO_NAME = "surrogate-train-st

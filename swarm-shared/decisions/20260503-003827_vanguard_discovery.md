# vanguard / discovery

# Final Synthesis (Corrected + Actionable)

## 1. Diagnosis (merged, corrected)
- **No persisted manifest**: every training run re-enumerates via authenticated HF API → quota burn and 429 risk.
- **Recursive/heterogeneous enumeration**: likely recursive `list_repo_files` or `load_dataset(streaming=True)` on mixed repos → schema/casting errors and more API calls.
- **Studio churn**: training script recreates Lightning Studio instead of reusing running instances → wastes quota and adds cold-start latency.
- **No CDN-only fetches**: training performs authenticated API calls instead of using public CDN URLs → unnecessary auth exposure and quota use.
- **Remote enumeration on GPU node**: file listing on Lightning Studio (GPU node) wastes expensive GPU time and quota.

## 2. Strategy (merged)
- **Mac-only orchestration**: produce a `(repo, dateFolder)` manifest with a single authenticated HF API call.
- **Training uses manifest + CDN-only fetches**: zero authenticated API calls during training.
- **Studio reuse guard**: detect and reuse a running Studio by name; create only if absent.
- **Keep enumeration off GPU nodes**: manifest creation and data listing run on orchestrator (Mac), not on Lightning Studio.

## 3. Implementation (merged, hardened)

### 3.1 Manifest creator (orchestrator)
`/opt/axentx/vanguard/discovery/make_manifest.py`

```python
#!/usr/bin/env python3
"""
Mac-only orchestration: produce (repo, dateFolder) manifest via a single HF API call.
Run after rate-limit window clears. Embed output manifest in training.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

def build_manifest(repo: str, date_folder: str, out_dir: str) -> str:
    api = HfApi()
    # Single non-recursive call per date folder (fast, low quota)
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [
        {"path": f.path, "size": getattr(f, "size", None)}
        for f in (tree.files or [])
        if f.path.lower().endswith((".parquet", ".jsonl", ".json"))
    ]

    manifest = {
        "repo": repo,
        "date": date_folder,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "note": "Use CDN-only downloads during training: https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    }

    os.makedirs(out_dir, exist_ok=True)
    safe_repo = repo.replace("/", "_")
    out_path = os.path.join(out_dir, f"manifest-{safe_repo}-{date_folder}.json")
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    return out_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create HF (repo,date) manifest for CDN-only training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., 'axentx/surrogate-1')")
    parser.add_argument("--date", required=True, help="Date folder in repo (e.g., '2026-04-29')")
    parser.add_argument("--out-dir", default="manifests", help="Output directory for manifest JSON")
    args = parser.parse_args()

    try:
        out = build_manifest(args.repo, args.date, args.out_dir)
        print(f"Manifest written: {out}")
    except Exception as exc:
        print(f"Failed to build manifest: {exc}", file=sys.stderr)
        sys.exit(1)
```

### 3.2 CDN-only loader + training stub
`/opt/axentx/vanguard/discovery/train.py`

```python
import argparse
import json
import os
from pathlib import Path
from typing import Iterator, Dict

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

def cdn_url(repo: str, filepath: str) -> str:
    # Public CDN; no Authorization header required
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{filepath}"

def stream_parquet_from_cdn(repo: str, filepath: str, columns=("prompt", "response")) -> Iterator[Dict]:
    url = cdn_url(repo, filepath)
    # Stream download; no auth, CDN tier has high limits
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        tmp = Path("/tmp") / os.path.basename(filepath)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        table = pq.read_table(tmp, columns=columns)
        for batch in table.to_batches(max_chunksize=1024):
            for i in range(batch.num_rows):
                row = {c: batch.column(c)[i].as_py() for c in columns}
                yield row
        tmp.unlink(missing_ok=True)

def make_dataloader(manifest_path: str, repo: str):
    with open(manifest_path) as f:
        manifest = json.load(f)
    files = [f["path"] for f in manifest["files"]]

    def loader():
        for fp in tqdm(files, desc="Loading files"):
            yield from stream_parquet_from_cdn(repo, fp)
    return loader

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON from make_manifest.py")
    parser.add_argument("--repo", required=True, help="HF dataset repo (must match manifest)")
    args = parser.parse_args()

    loader = make_dataloader(args.manifest, args.repo)
    count = 0
    for item in loader():
        count += 1
        if count % 1000 == 0:
            print(f"Processed {count} rows")
    print(f"Done. Total rows: {count}")
```

### 3.3 Studio reuse guard
`/opt/axentx/vanguard/discovery/launch_studio.py`

```python
#!/usr/bin/env python3
"""
Reuse running Lightning Studio if present; avoid recreation quota burn.
"""
import argparse
import sys

# Use stable public SDK import pattern; fallback guidance if unavailable
try:
    from lightning import Studio, Machine, Teamspace
except Exception as exc:
    print(f"Import failed (check SDK): {exc}", file=sys.stderr)
    sys.exit(1)

def reuse_or_create(studio_name: str, machine: Machine):
    teamspace = Teamspace()
    running = [s for s in teamspace.studios if s.name == studio_name and getattr(s, "status", "").lower() == "running"]
    if running:
        print(f"Reusing running studio: {running[0].id}")
        return running[0]
    print(f"No running studio '{studio_name}' found. Creating...")
    return Studio(
        name=studio_name,
        machine=machine,
        create_ok=True,
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--studio-name", default="vanguard-train")
    parser.add_argument("--machine", default="lightningai/L40S", help="Machine type (H200 only in paid tier)")
    args = parser.parse_args()

    # Prefer free-tier-compatible machine; H200 requires paid account
    machine = Machine(args.machine)
    try:
        studio = reuse_or_create(args.studio_name, machine)
        print(f"Studio ready: {studio.name} ({studio.status})")
    except Exception as exc:
        print(f"Studio launch failed: {exc}", file=sys.stderr)
        sys.exit(1)
```

## 4. Verification (merged)
1. On Mac (orchestrator), run:
   ```bash
   cd /opt/axentx/vanguard/discovery
   python3 make_manifest.py --repo axentx/surrogate-1 --date 2026-04-29 --out-dir manifests
   ```
   Confirm `manifests/manifest-axentx_surrogate-1-2026-04-29.json` exists and lists parquet/jsonl files.

2. Run

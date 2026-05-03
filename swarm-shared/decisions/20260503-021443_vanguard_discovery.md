# vanguard / discovery

## 1. Diagnosis
- No persisted `(repo, dateFolder) → file-list` manifest: every training run triggers authenticated `list_repo_tree`, burning HF API quota and risking 429s.
- Training uses `load_dataset(streaming=True)` or per-file loads on heterogeneous repos, causing `pyarrow` schema mismatches and parse failures.
- No CDN-only fetch path: authenticated API calls during data loading are unnecessary and rate-limited; public CDN URLs bypass auth entirely.
- No reuse guard for Lightning Studio: scripts create new studios instead of reusing running ones, wasting 80+ hrs/mo of quota.
- No idle-stop resilience: Lightning idle timeouts kill training; no status check/restart logic before `.run()` calls.

## 2. Proposed change
Add a discovery-time manifest generator and a training-side loader that uses CDN-only fetches and schema projection.  
Files to touch (create/modify):
- `/opt/axentx/vanguard/scripts/build_file_manifest.py` (new) — one-shot Mac-side script to list a repo/dateFolder and emit `manifest.json`.
- `/opt/axentx/vanguard/train/train.py` (modify) — read `manifest.json`, fetch parquet files via CDN URLs, project to `{prompt, response}`, yield rows.
- `/opt/axentx/vanguard/train/lightning_launcher.py` (modify) — add Studio reuse + idle-stop guard before `.run()`.

## 3. Implementation

### 3.1 `scripts/build_file_manifest.py`
```python
#!/usr/bin/env python3
"""
Usage:
  python build_file_manifest.py \
    --repo huggingface.co/datasets/your-org/your-repo \
    --date-folder 2026-05-03 \
    --out manifest.json

Produces:
{
  "repo": "...",
  "date_folder": "2026-05-03",
  "created_utc": 1717344000,
  "files": [
    {"path": "batches/mirror-merged/2026-05-03/abc123.parquet", "cdn_url": "https://huggingface.co/datasets/.../resolve/main/..."},
    ...
  ]
}
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date_folder: str, out_path: Path):
    api = HfApi()
    # list non-recursive to avoid pagination explosion; rely on date_folder prefix
    entries = api.list_repo_tree(repo=repo, path=f"batches/mirror-merged/{date_folder}", recursive=False)

    files = []
    for e in entries:
        if not e.path.endswith(".parquet"):
            continue
        files.append({
            "path": e.path,
            "cdn_url": CDN_TEMPLATE.format(repo=repo, path=e.path),
        })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_utc": int(time.time()),
        "created_iso": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files -> {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="e.g. org/dataset")
    parser.add_argument("--date-folder", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", default="manifest.json", help="output JSON path")
    args = parser.parse_args()

    build_manifest(args.repo, args.date_folder, Path(args.out))
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/build_file_manifest.py
```

### 3.2 `train/train.py` (minimal diff)
Add near top:
```python
import json
from pathlib import Path
from typing import Iterator, Dict, Any

import pyarrow.parquet as pq
import requests

MANIFEST_PATH = Path(__file__).parent.parent / "manifest.json"

def iter_rows_from_cdn(manifest_path: Path = MANIFEST_PATH) -> Iterator[Dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text())
    for item in manifest["files"]:
        url = item["cdn_url"]
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        tbl = pq.read_table(pq.ParquetFile(pq.BufferReader(resp.content)))
        # Project to {prompt, response} only — ignore heterogeneous extra cols
        df = tbl.select(["prompt", "response"]).to_pandas()
        for _, row in df.iterrows():
            yield {"prompt": row["prompt"], "response": row["response"]}
```

Replace dataset-loading code with:
```python
# OLD: dataset = load_dataset("...", streaming=True)  # removed
train_data = iter_rows_from_cdn()
```

### 3.3 `train/lightning_launcher.py` (add reuse + idle guard)
```python
from lightning import Studio, Teamspace, Machine, L40S

def get_or_create_studio(name: str) -> Studio:
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return Studio(
        name=name,
        machine=Machine.L40S,
        create_ok=True,
    )

def run_with_idle_guard(studio: Studio, target, *args, **kwargs):
    # Lightning idle stop kills training; restart if stopped
    if studio.status != "Running":
        print(f"Studio {studio.name} not running (status={studio.status}), restarting...")
        studio.start(machine=Machine.L40S)
    return studio.run(target, *args, **kwargs)
```

Update launcher calls to use `get_or_create_studio` and `run_with_idle_guard`.

## 4. Verification
1. Run manifest build (Mac):
   ```bash
   cd /opt/axentx/vanguard
   python scripts/build_file_manifest.py \
     --repo your-org/your-repo \
     --date-folder 2026-05-03 \
     --out manifest.json
   ```
   Confirm `manifest.json` exists and contains parquet file entries with valid `cdn_url`s.

2. Dry-run loader locally (no GPU):
   ```python
   from train.train import iter_rows_from_cdn
   rows = list(iter_rows_from_cdn())
   assert len(rows) > 0
   assert "prompt" in rows[0] and "response" in rows[0]
   print("OK")
   ```

3. Lightning Studio reuse:
   - Start a studio manually via SDK once.
   - Run launcher script twice; second run should log “Reusing running studio” and not create a new one.

4. Idle-stop resilience:
   - Stop the studio in UI.
   - Invoke `run_with_idle_guard`; confirm it restarts the studio and proceeds without manual intervention.

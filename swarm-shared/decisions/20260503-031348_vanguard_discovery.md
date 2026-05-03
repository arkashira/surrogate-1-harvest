# vanguard / discovery

## 1. Diagnosis
- No deterministic CDN-first manifest exists; ingestion/training likely still call HF API (`list_repo_tree`, `load_dataset`) at runtime, risking 429s and non-reproducible runs.
- Missing content-addressed file list keyed by date/slug; training cannot run without API access and is not reproducible across runs.
- No lightweight orchestrator on the Mac to snapshot a date-folder once and embed the file list into training (violates “pre-list once, CDN-only training” pattern).
- Studio churn risk: training scripts may recreate studios instead of reusing running ones, burning quota.
- No guard against idle-stop killing training; no automatic resume on stopped studio.

## 2. Proposed change
Create `/opt/axentx/vanguard/scripts/make_manifest.py` (single-file orchestrator) plus `/opt/axentx/vanguard/train.py` updated to consume the manifest and use only CDN downloads. Scope:
- `scripts/make_manifest.py` — run on Mac; calls HF API once per date-folder; writes `manifests/{date}/filelist.json`.
- `train.py` — reads `manifests/{date}/filelist.json`; streams via CDN URLs; reuses running studio; checks status and restarts if idle-stopped.

## 3. Implementation

```bash
# /opt/axentx/vanguard/scripts/make_manifest.py
#!/usr/bin/env python3
"""
Generate a CDN-first manifest for one date-folder.
Run once per date snapshot from Mac (or trusted orchestrator).
"""
import json, os, sys, hashlib, datetime
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    print("pip install huggingface_hub")
    sys.exit(1)

API = HfApi()
REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-mirror")
OUT_DIR = Path(__file__).parents[2] / "manifests"

def date_from_argv() -> str:
    if len(sys.argv) < 2:
        # default: yesterday UTC YYYY-MM-DD
        return (datetime.datetime.utcnow() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    return sys.argv[1]

def build_manifest(date_folder: str):
    # single non-recursive list per date-folder (avoids 100x pagination)
    entries = API.list_repo_tree(repo_id=REPO, path=date_folder, recursive=False)
    files = []
    for e in entries:
        if e.type != "file":
            continue
        # path relative to repo root
        rel_path = f"{date_folder}/{e.path}"
        slug = Path(e.path).stem
        # CDN URL (no auth, no API)
        cdn_url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{rel_path}"
        files.append({
            "rel_path": rel_path,
            "slug": slug,
            "cdn_url": cdn_url,
            "size": e.size if hasattr(e, "size") else None
        })
    manifest = {
        "generated_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "date_folder": date_folder,
        "repo": REPO,
        "strategy": "cdn-only",
        "files": files
    }
    out_path = OUT_DIR / date_folder / "filelist.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files -> {out_path}")
    return out_path

if __name__ == "__main__":
    build_manifest(date_from_argv())
```

```python
# /opt/axentx/vanguard/train.py  (minimal viable diff)
"""
CDN-only training loader + Lightning Studio reuse + idle-stop guard.
"""
import json, os, sys, time
from pathlib import Path

import torch
from torch.utils.data import IterableDataset, DataLoader
import requests

try:
    from lightning import Studio, Machine, Teamspace
except ImportError:
    print("pip install lightning")
    sys.exit(1)

MANIFEST_PATH = Path(__file__).parents[0] / "manifests"

class CDNParquetIterable(IterableDataset):
    def __init__(self, manifest_path):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.urls = [f["cdn_url"] for f in self.manifest["files"]]

    def __iter__(self):
        for url in self.urls:
            # stream parquet rows lazily; project to {prompt,response} here
            resp = requests.get(url, timeout=30, stream=True)
            resp.raise_for_status()
            # minimal: yield raw bytes; replace with proper parquet row projection
            # For real usage: use pyarrow.parquet.ParquetFile on BytesIO chunks
            yield {"raw_bytes": resp.content, "source_url": url}

def get_or_create_studio(name="vanguard-train", machine="lightning-lambda-prod:L40S"):
    ts = Teamspace()
    for s in ts.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s
    print(f"Creating studio: {name}")
    return Studio(
        name=name,
        machine=Machine(machine),
        create_ok=True
    )

def ensure_running(studio, machine="lightning-lambda-prod:L40S"):
    if studio.status != "Running":
        print(f"Studio stopped ({studio.status}). Restarting...")
        studio.start(machine=Machine(machine))
        # wait briefly
        for _ in range(10):
            studio.refresh()
            if studio.status == "Running":
                return
            time.sleep(6)
        raise RuntimeError("Studio failed to start")

def main():
    date_folder = os.getenv("DATE_FOLDER", "2025-01-01")
    manifest = Path(__file__).parents[0] / "manifests" / date_folder / "filelist.json"
    if not manifest.exists():
        print(f"Manifest missing: {manifest}. Run scripts/make_manifest.py {date_folder}")
        sys.exit(1)

    studio = get_or_create_studio()
    ensure_running(studio)

    dataset = CDNParquetIterable(manifest)
    loader = DataLoader(dataset, batch_size=8, num_workers=4)

    @studio.run
    def train():
        for batch in loader:
            # replace with real training step
            print(f"Batch keys: {list(batch.keys())}, sample url: {batch['source_url'][0]}")
            break
        print("CDN-only training step completed (demo).")

    train()

if __name__ == "__main__":
    main()
```

```bash
# Make executable and ensure Bash where needed
chmod +x /opt/axentx/vanguard/scripts/make_manifest.py
# If you wrap this in a cron or bash script, use:
# SHELL=/bin/bash
# and invoke via: bash /opt/axentx/vanguard/scripts/make_manifest.sh "$@"
```

## 4. Verification
1. Run manifest generation (on Mac or orchestrator):
   ```bash
   export HF_DATASET_REPO=axentx/surrogate-mirror
   python3 /opt/axentx/vanguard/scripts/make_manifest.py 2025-01-01
   ```
   Confirm `manifests/2025-01-01/filelist.json` exists and contains CDN URLs.

2. Dry-run training loader locally (no GPU):
   ```bash
   DATE_FOLDER=2025-01-01 python3 /opt/axentx/vanguard/train.py
   ```
   Expect: “CDN-only training step completed (demo)” and no HF API calls during data load (check via network or by revoking HF token temporarily).

3. Studio reuse/idle-stop check:
   - Start a studio manually, rerun `train.py`; confirm log “Reusing running studio”.
   - Stop the studio, rerun `train.py`; confirm it restarts and proceeds.

4. Rate-limit safety:
   - Revoke HF token or run many iterations; confirm training continues (only CDN fetches).

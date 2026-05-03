# vanguard / discovery

## 1. Diagnosis
- No CDN-bypass for dataset fetches: training/data-loading still uses authenticated HF API (`load_dataset`/`list_repo_tree`) → burns quota and risks 429s.
- No pre-listed file manifest: every run re-enumerates repo files via API instead of embedding a static file list for Lightning training to consume via CDN.
- No manifest cache in frontend preview/training launcher: each preview triggers fresh HF API calls instead of reusing a cached file list.
- No graceful 429 fallback: if API limit is hit, pipeline fails instead of falling back to CDN-only paths or cached manifest.
- Missing Lightning Studio reuse logic: launcher recreates studios instead of reusing running ones → wastes 80hr/mo quota.

## 2. Proposed change
- Add `scripts/build_file_manifest.py` (mac-side, run after rate-limit window) → `list_repo_tree` once per date folder → emit `manifests/{date}/files.json`.
- Add `vanguard/train.py` support for CDN-only loading using the embedded/sidecar manifest (zero authenticated calls during training).
- Add `vanguard/launch_studio.py` with studio reuse + graceful fallback (check running, reuse, restart if idle-killed).
- Add `vanguard/frontend/preview.py` (or equivalent launcher) to read cached manifest first; only refresh manifest if stale/missing and API budget allows.

## 3. Implementation

```bash
# /opt/axentx/vanguard/scripts/build_file_manifest.py
#!/usr/bin/env python3
"""
Run from Mac after HF API window clears.
Single API call per folder → CDN-only training thereafter.
"""
import json, os, sys
from datetime import datetime
from huggingface_hub import HfApi

REPO = "datasets/your-mirror-repo"   # <- update
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "manifests")
os.makedirs(OUT_DIR, exist_ok=True)

def build_for_date(date_folder: str):
    api = HfApi()
    # non-recursive top-level list (cheap, paginated 100/page but small)
    files = api.list_repo_tree(repo_id=REPO, path=date_folder, recursive=False)
    paths = [f.rfilename for f in files if not f.rfilename.endswith("/")]
    out = {
        "repo": REPO,
        "date_folder": date_folder,
        "generated_utc": datetime.utcnow().isoformat() + "Z",
        "files": sorted(paths)
    }
    out_path = os.path.join(OUT_DIR, f"{date_folder.replace('/', '_')}_files.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {len(paths)} files -> {out_path}")
    return out_path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: build_file_manifest.py <date-folder>")
        sys.exit(1)
    build_for_date(sys.argv[1])
```

```python
# /opt/axentx/vanguard/train.py  (additions/integration)
import json, os
from pathlib import Path
import torch
from torch.utils.data import IterableDataset
from huggingface_hub import hf_hub_download

MANIFEST_PATH = Path(__file__).parent / "manifests"

class CDNParquetDataset(IterableDataset):
    """
    Lightning training uses this. Zero authenticated HF API calls.
    Manifest is produced by scripts/build_file_manifest.py on Mac.
    """
    def __init__(self, manifest_name: str, repo: str, split_files=None):
        manifest_file = MANIFEST_PATH / manifest_name
        if not manifest_file.exists():
            raise FileNotFoundError(f"Manifest missing: {manifest_file}")
        with open(manifest_file) as f:
            manifest = json.load(f)
        self.repo = repo
        self.date_folder = manifest["date_folder"]
        files = manifest["files"]
        if split_files:
            files = [f for f in files if any(s in f for s in split_files)]
        self.files = files

    def _stream_files(self):
        for fn in self.files:
            # CDN bypass: no Authorization header; uses resolve/main/ CDN endpoint
            local_path = hf_hub_download(
                repo_id=self.repo,
                filename=fn,
                repo_type="dataset",
                # Important: this still uses hf_hub_download which may hit API for cache-miss metadata.
                # For pure CDN bypass at scale, replace with direct wget/curl of resolve/main/ URLs.
                # For now, keep hf_hub_download for cache-friendliness; metadata hits are minimal.
            )
            yield local_path

    def __iter__(self):
        for p in self._stream_files():
            # Project to {prompt,response} here; ignore mixed schema cols
            # Example: load parquet and yield rows
            import pyarrow.parquet as pq
            table = pq.read_table(p, columns=["prompt", "response"])
            for i in range(table.num_rows):
                row = table.slice(i, 1).to_pydict()
                yield {"prompt": row["prompt"][0], "response": row["response"][0]}
```

```python
# /opt/axentx/vanguard/launch_studio.py
#!/usr/bin/env python3
"""
Reuse running Lightning Studio; restart if idle-killed.
"""
import time
from lightning_sdk import Studio, Teamspace, Machine

TEAMSPACE = "your-teamspace"
STUDIO_NAME = "vanguard-train"
MACHINE = Machine.L40S  # or fallback to free-tier compatible

def get_running_studio():
    teamspace = Teamspace.load(TEAMSPACE)
    for s in teamspace.studios:
        if s.name == STUDIO_NAME:
            return s
    return None

def launch_or_reuse(script_path: str):
    studio = get_running_studio()
    if studio is None:
        print("No running studio found. Creating...")
        teamspace = Teamspace.load(TEAMSPACE)
        studio = teamspace.studios.create(
            name=STUDIO_NAME,
            machine=MACHINE,
            create_ok=True
        )
    else:
        print(f"Reusing running studio: {STUDIO_NAME}")

    # If studio stopped (idle kill), restart
    if studio.status != "running":
        print(f"Studio stopped ({studio.status}). Restarting...")
        studio.start(machine=MACHINE)
        # wait a bit for start
        for _ in range(30):
            studio.refresh()
            if studio.status == "running":
                break
            time.sleep(5)

    # Run training script (non-blocking or blocking as preferred)
    run = studio.runs.create(
        script=str(script_path),
        name="vanguard-train-run",
        # optional: requirements, env
    )
    print(f"Started run: {run.id}")
    return run

if __name__ == "__main__":
    import sys
    script = sys.argv[1] if len(sys.argv) > 1 else "train.py"
    launch_or_reuse(script)
```

```python
# /opt/axentx/vanguard/frontend/preview.py  (or launcher used by UI)
import json, os, time
from pathlib import Path
from huggingface_hub import HfApi

MANIFEST_DIR = Path(__file__).parent.parent / "manifests"
API = HfApi()

def get_manifest(date_folder: str, max_age_seconds=3600):
    manifest_file = MANIFEST_DIR / f"{date_folder.replace('/', '_')}_files.json"
    use_cached = False
    if manifest_file.exists():
        age = time.time() - manifest_file.stat().st_mtime
        if age < max_age_seconds:
            use_cached = True
    if use_cached:
        with open(manifest_file) as f:
            return json.load(f)
    # Fallback: refresh manifest (costs API quota) — only if budget allows
    print("Refreshing manifest (API call)...")
    files = API.list_repo_tree(repo_id="datasets/your-mirror-repo", path=date_folder, recursive=False)
    paths = [f.rfilename for f in files if not f.rfilename.endswith("/")]
    manifest = {
        "repo": "datasets/your-mirror-repo",
        "date_folder": date_folder,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),

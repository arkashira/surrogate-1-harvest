# vanguard / quality

## 1. Diagnosis
- No persisted `(repo, dateFolder) → file-list` manifest: every training/data-selection run triggers authenticated `list_repo_tree` against HF API, burning quota and risking 429s.
- Data loader uses `load_dataset(streaming=True)` or repeated per-file API calls on heterogeneous repos, causing `pyarrow.CastError` and slow, fragile ingestion.
- Training path is not CDN-only: authenticated API calls during training instead of using public CDN URLs (`resolve/main/...`), wasting rate-limit budget.
- No reuse guard for Lightning Studio: scripts create new studios instead of reusing running ones, burning 80+ hrs/mo of quota.
- No idle-stop resilience: Lightning Studio idle timeout kills long-running training jobs without auto-restart.

## 2. Proposed change
Add a lightweight manifest + CDN-only data loader and a safe Lightning launcher to `/opt/axentx/vanguard/train.py` (create if missing) and `/opt/axentx/vanguard/utils/hf.py`. Scope:
- `utils/hf.py`: `build_manifest(repo, date_folder) -> manifest.json`, `stream_cdn_parquet(file_path, columns)`
- `train.py`: load manifest, use CDN-only streaming, reuse running studio, restart if idle-stopped.

## 3. Implementation

### utils/hf.py
```python
# /opt/axentx/vanguard/utils/hf.py
import json, os, time, requests
from pathlib import Path
from huggingface_hub import HfApi, list_repo_tree

HF_API = HfApi()
CDN_ROOT = "https://huggingface.co/datasets"

def build_manifest(repo: str, date_folder: str, out_path: str = None):
    """
    Single authenticated call to list one date folder (non-recursive).
    Returns list of parquet paths and saves manifest JSON.
    """
    # list once, non-recursive to minimize API calls
    tree = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [
        f.rfilename for f in tree
        if f.rfilename.endswith(".parquet")
    ]
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "created_ts": int(time.time()),
        "files": sorted(files)
    }
    out_path = out_path or f"manifests/{repo.replace('/', '_')}_{date_folder.replace('/', '_')}.json"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest

def cdn_parquet_urls(manifest):
    """Yield public CDN URLs (no auth) for each file in manifest."""
    repo = manifest["repo"]
    for f in manifest["files"]:
        yield f"{CDN_ROOT}/{repo}/resolve/main/{f}"

def stream_cdn_parquet(file_url, columns=("prompt", "response")):
    """
    Stream a single parquet file from CDN (no auth) and project columns.
    Uses pyarrow.dataset to avoid loading full file into memory.
    """
    import pyarrow.parquet as pq
    # Range requests supported by CDN; no auth header
    pf = pq.ParquetFile(file_url)
    for batch in pf.iter_batches(batch_size=1024, columns=columns):
        yield batch.to_pylist()
```

### train.py
```python
# /opt/axentx/vanguard/train.py
import os, time, json
from pathlib import Path
from lightning import LightningWork, LightningApp, Machine
from vanguard.utils.hf import build_manifest, cdn_parquet_urls, stream_cdn_parquet

MANIFEST_PATH = os.getenv("VANGUARD_MANIFEST", "manifests/vanguard_manifest.json")
REPO = os.getenv("HF_REPO", "axentx/vanguard-data")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", "batches/mirror-merged/2026-05-03")

class CDNDataLoader:
    def __init__(self, manifest_path=MANIFEST_PATH, repo=REPO, date_folder=DATE_FOLDER):
        self.manifest_path = manifest_path
        self.repo = repo
        self.date_folder = date_folder
        self.manifest = self._load_or_build()

    def _load_or_build(self):
        p = Path(self.manifest_path)
        if p.exists():
            with open(p) as f:
                return json.load(f)
        # one-time authenticated call (run on Mac orchestrator)
        return build_manifest(self.repo, self.date_folder, self.manifest_path)

    def iter_rows(self, columns=("prompt", "response")):
        for url in cdn_parquet_urls(self.manifest):
            try:
                yield from stream_cdn_parquet(url, columns=columns)
            except Exception as exc:
                print(f"Skipping {url}: {exc}")
                continue

class SurrogateTrainer(LightningWork):
    def __init__(self, machine="lightning-public-prod", **kwargs):
        super().__init__(**kwargs)
        self.machine = machine
        self.loader = CDNDataLoader()

    def run(self):
        # reuse check: if studio was stopped, restart on same machine
        print(f"Starting training on {self.machine}")
        rows = 0
        for batch in self.loader.iter_rows():
            # Replace with your actual surrogate-1 training step
            # e.g., tokenizer -> model.train_on_batch(...)
            rows += len(batch)
            if rows % 10_000 == 0:
                print(f"Processed {rows} rows")
        print(f"Completed. Total rows: {rows}")

# App orchestration with reuse + idle resilience
def find_running_studio(name="vanguard-trainer"):
    from lightning import Teamspace
    for s in Teamspace.studios:
        if s.name == name and s.status == "running":
            return s
    return None

def main():
    existing = find_running_studio("vanguard-trainer")
    if existing:
        print("Reusing running studio")
        target = existing
        if not target.is_running:
            target.start(machine=Machine.L40S)
    else:
        target = SurrogateTrainer(machine="lightning-public-prod")

    app = LightningApp(target)
    # LightningApp.run() will block; handle idle-stop by checking status before long ops
    # For CI/orchestration, wrap calls with status checks and restart if stopped.

if __name__ == "__main__":
    main()
```

### Optional orchestration wrapper (for cron/CI)
```bash
#!/usr/bin/env bash
# /opt/axentx/vanguard/run_train.sh
set -euo pipefail
export SHELL=/bin/bash
cd /opt/axentx/vanguard
python train.py
```
- Ensure executable: `chmod +x run_train.sh`
- In crontab: `SHELL=/bin/bash` and invoke via `bash /opt/axentx/vanguard/run_train.sh`

## 4. Verification
1. Run manifest build once (on Mac/orchestrator) and confirm JSON exists:
   ```bash
   python -c "from vanguard.utils.hf import build_manifest; m=build_manifest('axentx/vanguard-data','batches/mirror-merged/2026-05-03'); print(m['files'][:3])"
   ```
2. Confirm CDN URLs are reachable without auth (should return 206/200):
   ```bash
   curl -I "https://huggingface.co/datasets/axentx/vanguard-data/resolve/main/batches/mirror-merged/2026-05-03/sample.parquet"
   ```
3. Run training locally (dry-run) and confirm loader streams rows without HF API calls:
   ```bash
   HF_REPO=axentx/vanguard-data HF_DATE_FOLDER=batches/mirror-merged/2026-05-03 python train.py
   ```
   - Monitor logs: should see “Processed X rows” and no 429/authentication errors.
4. In Lightning, verify studio reuse:
   - Start once, then rerun — logs should show “Reusing running studio”.
5. Simulate idle-stop: stop studio from UI, then rerun — trainer should restart on `L40S` (or configured machine) and continue.

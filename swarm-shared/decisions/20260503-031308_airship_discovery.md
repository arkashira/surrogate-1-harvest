# airship / discovery

## Final Decision  
**Ship a zero-HF-API data loader + Lightning idle-resilient runner for Surrogate training (≤2h).**  

- HF API is used **once** (from Mac) to list a single date folder → JSON manifest.  
- Training uses **CDN-only** fetches and never calls the HF Hub API during data loading.  
- Lightning runner reuses a running studio, checks status before each run, and auto-restarts on idle stop.  
- No changes to Arkship; no speculative rewrites; no new dependencies beyond existing `lightning`, `pyarrow`, `requests`.  

---

## Implementation Plan (≤2h)

| Step | Owner | Time | Concrete deliverable |
|------|-------|------|----------------------|
| 1 | Engineer | 15m | `scripts/generate_file_manifest.py` — one-time script that calls `list_repo_tree` for a date folder and writes `manifests/{date}.json`. |
| 2 | Engineer | 30m | Replace Surrogate dataset loader with `surrogate/data/cdn_dataset.py` that reads the manifest and fetches via CDN (`resolve/main/...`). Projects to `{prompt,response}` at parse time. |
| 3 | Engineer | 30m | `scripts/run_surrogate_training.py` — Lightning launcher that (a) reuses a running studio, (b) checks status before each run, (c) restarts with `L40S` if stopped, (d) passes manifest path to training script. |
| 4 | Engineer | 30m | Update Docker/entrypoint or cron to invoke launcher via `bash scripts/run_surrogate_training.py "$@"`. Ensure scripts are executable and have correct shebangs. |
| 5 | QA | 15m | Smoke test: generate manifest, verify CDN downloads, run one training step, simulate idle stop and confirm auto-restart. |

---

## Code Snippets

### 1) scripts/generate_file_manifest.py
```python
#!/usr/bin/env python3
"""
One-time script to generate manifests/{date}.json listing files in a date folder.
Keeps HF API usage to a single list call and avoids auth/token issues during training.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-dataset")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
OUT_DIR = Path("manifests")
OUT_DIR.mkdir(exist_ok=True, parents=True)

def main() -> None:
    api = HfApi()
    tree = api.list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)
    files = [item.path for item in tree if item.type == "file"]

    manifest = {
        "repo": HF_REPO,
        "date": DATE_FOLDER,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": sorted(files),
    }

    out_path = OUT_DIR / f"{DATE_FOLDER}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out_path} ({len(files)} files)")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x scripts/generate_file_manifest.py
```

---

### 2) surrogate/data/cdn_dataset.py
```python
"""
CDN-only dataset loader for Surrogate training.
Uses a pre-generated manifest to avoid HF API calls during training.
"""
import json
import logging
from pathlib import Path
from typing import Dict, Iterator, List

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

logger = logging.getLogger(__name__)

CDN_BASE = "https://huggingface.co/datasets"

class CDNParquetDataset:
    """
    Lightweight dataset that yields {prompt, response} rows from Parquet files
    fetched via CDN (no HF API/auth during training).
    """
    def __init__(self, manifest_path: Path, repo: str = None, max_files: int = None):
        self.manifest_path = Path(manifest_path)
        with self.manifest_path.open() as f:
            self.manifest = json.load(f)

        self.repo = repo or self.manifest["repo"]
        self.files: List[str] = self.manifest["files"]
        if max_files:
            self.files = self.files[:max_files]

    def _cdn_url(self, file_path: str) -> str:
        return f"{CDN_BASE}/{self.repo}/resolve/main/{file_path}"

    def _download_parquet(self, file_path: str, local_cache: Path) -> Path:
        local_cache.parent.mkdir(parents=True, exist_ok=True)
        if local_cache.exists():
            return local_cache

        url = self._cdn_url(file_path)
        logger.info("Downloading (CDN): %s", url)
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        local_cache.write_bytes(resp.content)
        return local_cache

    def _project_row(self, row: Dict) -> Dict:
        return {
            "prompt": row.get("prompt") or row.get("input") or "",
            "response": row.get("response") or row.get("output") or "",
        }

    def __iter__(self) -> Iterator[Dict]:
        cache_root = Path(".cache/cdn_parquet")
        for file_path in tqdm(self.files, desc="Loading via CDN"):
            local_path = cache_root / file_path.replace("/", "_")
            try:
                local_path = self._download_parquet(file_path, local_path)
                table = pq.read_table(local_path)
                for batch in table.to_batches(max_chunksize=1024):
                    cols = {name: batch.column(name).to_pylist() for name in batch.schema.names}
                    rows = [dict(zip(cols, values)) for values in zip(*cols.values())]
                    for row in rows:
                        projected = self._project_row(row)
                        if projected["prompt"] and projected["response"]:
                            yield projected
            except pa.ArrowInvalid as exc:
                logger.warning("Skipping malformed parquet %s: %s", file_path, exc)
                continue
```

---

### 3) scripts/run_surrogate_training.py
```bash
#!/usr/bin/env bash
#
# Lightning launcher for Surrogate training.
# - Reuses running studio if available
# - Checks status before each run and restarts if stopped (idle timeout)
# - Passes manifest to training script to enable CDN-only data loading
#
set -euo pipefail

MANIFEST_PATH="${MANIFEST_PATH:-manifests/$(date +%Y-%m-%d).json}"
STUDIO_NAME="${STUDIO_NAME:-surrogate-training}"
MACHINE="${MACHINE:-L40S}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! python -c "import lightning" &>/dev/null; then
  echo "ERROR: lightning SDK not installed. Install with: pip install lightning"
  exit 1
fi

cd "$PROJECT_DIR"

# Use Lightning Python API to manage studio lifecycle
python - "$MANIFEST_PATH" "$STUDIO_NAME" "$MACHINE" <<'PY'
import os
import sys
import time
from pathlib import Path

from lightning import LightningWork, LightningApp, Machine
from lightning.app import Teamspace

MANIFEST_PATH = sys.argv[1]
STUDIO_NAME = sys.argv[2]
MACHINE = sys.argv[3]

class SurrogateTrainer(LightningWork):
    def __init__(self, manifest_path: str, **kwargs):
        super().__init__(**kwargs)
        self.manifest_path = manifest_path

    def run(self):
        # Import here so it runs inside the cloud worker
        from surrogate.training.train import main as train_main
        train_main(manifest_path=Path(self.manifest_path))

def ensure_studio_running():
    teamspace = Teamspace()
    for studio in teamspace.studios:
        if studio.name == STUDIO_NAME:
            if studio.status == "running":
                return studio
            # If stopped/idle, restart
            studio.stop()
            time.sleep(5)
    # Create new
    work = SurrogateTrainer(manifest_path=MANIFEST_PATH)
    app = LightningApp(work)
   

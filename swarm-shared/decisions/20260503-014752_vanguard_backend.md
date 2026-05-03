# vanguard / backend

## 1. Diagnosis

- No persisted `(repo, dateFolder) → file-list` artifact exists; every training launch re-queries HF API via `list_repo_tree`, burning quota and causing 429s.
- Training/data-selection paths likely call `load_dataset(streaming=True)` on heterogeneous repos, risking `pyarrow.CastError` from mixed schemas.
- No CDN-only data path; authenticated API calls are used for file access instead of public CDN URLs, missing the rate-limit bypass.
- No guard to reuse running Lightning Studio; each launch may create new sessions and waste 80hr/mo quota.
- Mac/local orchestration may attempt `model.from_pretrained()` or heavy compute instead of delegating training to Lightning/Kaggle.

## 2. Proposed change

Add a backend “snapshot + CDN-only” ingestion module:

- File: `/opt/axentx/vanguard/backend/data_snapshot.py` (new)
- File: `/opt/axentx/vanguard/backend/train_launcher.py` (modify or create)
- Scope: single-date folder snapshot → JSON manifest → Lightning training uses CDN-only fetches with zero API calls during data load.

## 3. Implementation

Create `/opt/axentx/vanguard/backend/data_snapshot.py`:

```python
#!/usr/bin/env python3
"""
Create and use a persisted CDN-only snapshot for a (repo, dateFolder).
Usage:
    python data_snapshot.py --repo datasets/opus-mt-ko-en --date 2026-04-27 --out snapshot_opus_2026-04-27.json
"""
import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import requests
from huggingface_hub import HfApi, hf_hub_download

HF_API = HfApi()
CDN_ROOT = "https://huggingface.co/datasets"

# Rate-limit safety: 1000/5min for API; space out list_repo_tree calls.
def list_date_folder_safe(repo_id: str, date_folder: str, retries: int = 3, backoff: int = 10) -> List[str]:
    for attempt in range(retries):
        try:
            tree = HF_API.list_repo_tree(repo_id, path=date_folder, recursive=False)
            return [item.rfilename for item in tree if item.rfilename.endswith((".parquet", ".jsonl", ".json"))]
        except Exception as exc:
            if attempt == retries - 1:
                raise
            wait = backoff * (2 ** attempt)
            print(f"list_repo_tree failed ({exc}), retry {attempt + 1}/{retries} in {wait}s")
            time.sleep(wait)
    return []

def build_snapshot(repo_id: str, date_folder: str, out_path: Path) -> Dict:
    files = list_date_folder_safe(repo_id, date_folder)
    cdn_urls = [
        f"{CDN_ROOT}/{repo_id}/resolve/main/{date_folder}/{f}"
        for f in files
    ]
    snapshot = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "files": files,
        "cdn_urls": cdn_urls,
        "note": "Use CDN URLs only during training to avoid HF API auth rate limits."
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=2))
    print(f"Snapshot written to {out_path}")
    return snapshot

def load_snapshot(path: Path) -> Dict:
    return json.loads(path.read_text())

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create CDN-only snapshot for a repo/date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo id, e.g. datasets/opus-mt-ko-en")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-04-27")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    build_snapshot(args.repo, args.date, Path(args.out))
```

Create `/opt/axentx/vanguard/backend/train_launcher.py`:

```python
#!/usr/bin/env python3
"""
Lightning training launcher that uses a persisted CDN snapshot.
Ensures studio reuse and CDN-only data loading.
"""
import json
import subprocess
import sys
from pathlib import Path

try:
    from lightning import LightningWork, LightningApp, Machine
    from lightning.app import BuildConfig
    from lightning.app.utilities import LightningCLI
    from lightning.app.utilities.team import Teamspace
except ImportError as e:
    print("Missing lightning dependency; install lightning before running launcher.")
    sys.exit(1)

SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"

def find_running_studio(name_prefix: str):
    for s in Teamspace().studios:
        if s.name.startswith(name_prefix) and s.status == "Running":
            return s
    return None

def run_training_with_snapshot(snapshot_path: Path, script_path: Path, machine: str = "CPU"):
    if not snapshot_path.is_file():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_path}")

    # Prefer existing running studio to save quota.
    studio = find_running_studio("vanguard-train")
    if studio:
        print(f"Reusing running studio: {studio.name}")
    else:
        # Minimal Lightning Work that runs training script.
        class TrainWork(LightningWork):
            def __init__(self, snapshot_path: str, script_path: str, **kwargs):
                super().__init__(**kwargs)
                self.snapshot_path = snapshot_path
                self.script_path = script_path

            def run(self):
                # Training script must read snapshot and use CDN URLs only.
                subprocess.check_call([sys.executable, self.script_path, "--snapshot", self.snapshot_path])

        # Map friendly machine names to Lightning machines (adjust as needed).
        machine_map = {
            "CPU": "CPU",
            "L40S": "lightning-public-prod://L40S",
            "H200": "lightning-lambda-prod://H200",
        }
        target_machine = Machine(machine_map.get(machine, "CPU"))

        work = TrainWork(
            snapshot_path=str(snapshot_path),
            script_path=str(script_path),
            cloud_compute=target_machine,
        )
        app = LightningApp(work)
        # In headless/automation contexts you may instead use LightningCLI or run via `lightning run`.
        print("Created Lightning app with training work (use `lightning run` or Studio to start).")
        return app

def main():
    if len(sys.argv) < 3:
        print("Usage: train_launcher.py <snapshot.json> <train_script.py> [machine]")
        sys.exit(1)

    snapshot_path = Path(sys.argv[1])
    script_path = Path(sys.argv[2])
    machine = sys.argv[3] if len(sys.argv) > 3 else "CPU"

    run_training_with_snapshot(snapshot_path, script_path, machine=machine)

if __name__ == "__main__":
    main()
```

Example training script (`train_cdn_only.py`) that consumes the snapshot and uses CDN-only downloads (no authenticated list calls during training):

```python
#!/usr/bin/env python3
import argparse
import json
import datasets
import requests
from pathlib import Path
from typing import Iterator

def iter_cdn_parquet(urls: list, columns=("prompt", "response")) -> Iterator[dict]:
    for url in urls:
        # Download via CDN (no Authorization header) and stream rows.
        # This avoids HF API auth checks and rate limits.
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        local_path = Path("/tmp") / url.split("/")[-1]
        local_path.write_bytes(resp.content)
        ds = datasets.load_dataset("parquet", data_files=str(local_path), split="train")
        for row in ds:
            # Project only required fields; ignore heterogeneous extras.
            yield {k: row.get(k, "") for k in columns}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, help="Path to snapshot JSON")
    parser.add_argument("--output", default="train_ready.parquet", help="Output parquet")
    args = parser.parse

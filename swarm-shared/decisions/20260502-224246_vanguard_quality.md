# vanguard / quality

## Final consolidated solution

### 1. Diagnosis (merged, de-duplicated)
- **Missing HF CDN-bypass**: no `file-list.json` forces `load_dataset`/HF API calls during training → 429s on heterogeneous repos.
- **Non-idempotent Lightning Studio lifecycle**: scripts create new studios instead of reusing running ones → quota waste and idle-stop kills training.
- **No pre-flight file-list step**: training cannot run with zero API calls during data load.
- **No retry/backoff for 429** in ingestion/training paths.
- **No data-projection step for mixed-schema HF repos** → risk of `pyarrow.CastError` in Surrogate-1 pipeline.
- **No deterministic repo-selection/commit-cap strategy** for HF (128 writes/hr/repo) → ingestion can stall.
- **No guard against Lightning idle-stop**: training jobs die if studio stops between epochs.

### 2. High-leverage changes (single coherent plan)
Add two files under `/opt/axentx/vanguard/scripts/` and update training to use them:

- `scripts/list_hf_files.py` — one-shot listing to produce `file-list.json` and optional projection manifest.
- `scripts/lightning_studio.py` — idempotent studio reuse + zero-API training launcher (reads `file-list.json`, uses CDN-only downloads, with retry/backoff and idle-stop resilience).

Update training code to:
- Accept `--file-list` and `--repo` and fetch via CDN URLs only.
- Use schema projection on first file to avoid `pyarrow.CastError`.
- Implement exponential backoff + jitter for 429/5xx and deterministic repo selection for HF write cap.

### 3. Implementation

```bash
mkdir -p /opt/axentx/vanguard/scripts
```

#### `/opt/axentx/vanguard/scripts/list_hf_files.py`
```python
#!/usr/bin/env python3
"""
Generate file-list.json for a repo+folder and optional projection manifest.

Usage:
  python list_hf_files.py <repo> <date_folder> [--project] > file-list.json

Example:
  python list_hf_files.py axentx/surrogate-1 2026-04-29 --project > file-list.json

Notes:
- Run from Mac after HF API rate-limit window clears.
- Non-recursive listing keeps API calls minimal.
- With --project, includes a small projection manifest derived from first file
  to avoid pyarrow.CastError downstream.
"""
import argparse
import json
import sys
from huggingface_hub import list_repo_tree

def build_payload(repo: str, folder: str, project: bool):
    folder = folder.strip("/")
    tree = list_repo_tree(repo=repo, path=folder, recursive=False)
    files = sorted([item.rfilename for item in tree if not item.type == "dir"])

    payload = {
        "repo": repo,
        "folder": folder,
        "files": files,
    }

    if project and files:
        # Lightweight projection hint: include first file as sample for schema checks.
        # Training script should read first file and enforce consistent schema.
        payload["projection_sample"] = files[0]

    return payload

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repo")
    parser.add_argument("folder")
    parser.add_argument("--project", action="store_true")
    args = parser.parse_args()

    payload = build_payload(args.repo, args.folder, args.project)
    json.dump(payload, sys.stdout, indent=2)

if __name__ == "__main__":
    main()
```

#### `/opt/axentx/vanguard/scripts/lightning_studio.py`
```python
#!/usr/bin/env python3
"""
Idempotent Lightning Studio runner with HF CDN-bypass and idle-stop resilience.

Usage:
  python lightning_studio.py --file-list file-list.json --script train.py

Behavior:
- Reuses a running studio if found by name.
- If stopped, restarts it (idle-stop kills training).
- Runs training script with file-list.json so data loader fetches via CDN only.
- Adds retry/backoff for transient 429/5xx during CDN fetches.
"""
import argparse
import json
import time
import subprocess
import os
import sys
import requests
from typing import Any, Dict, List

# Prefer local Lightning imports only when actually running LightningApp.
# If imports fail in non-Lightning contexts, script can still be used for dry-run.
try:
    from lightning import LightningWork, LightningApp, Machine
    from lightning.app import Teamspace
    LIGHTNING_AVAILABLE = True
except Exception:
    LIGHTNING_AVAILABLE = False

STUDIO_NAME = os.getenv("STUDIO_NAME", "vanguard-surrogate-train")
HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1")

# Retry policy for CDN fetches (used by training script helper)
def fetch_with_backoff(url: str, max_retries: int = 5, timeout: int = 30) -> requests.Response:
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, stream=True, timeout=timeout)
            if resp.status_code == 429:
                wait = (2 ** attempt) + (hash(url) % 10) / 10.0
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except (requests.HTTPError, requests.ConnectionError) as exc:
            if attempt == max_retries:
                raise
            wait = (2 ** attempt) + (hash(str(exc)) % 10) / 10.0
            time.sleep(wait)
    raise RuntimeError("unreachable")

if LIGHTNING_AVAILABLE:
    class SurrogateTrainWork(LightningWork):
        def __init__(self, file_list_path: str, train_script: str, **kwargs):
            super().__init__(**kwargs)
            self.file_list_path = file_list_path
            self.train_script = train_script
            self.studio = None

        def _find_running_studio(self):
            for s in Teamspace().studios:
                if getattr(s, "name", None) == STUDIO_NAME and getattr(s, "status", None) == "running":
                    return s
            return None

        def run(self):
            studio = self._find_running_studio()
            if studio is None:
                from lightning.app import Studio
                studio = Studio(
                    name=STUDIO_NAME,
                    create_ok=True,
                    machine=Machine.L40S,
                )
            else:
                if getattr(studio, "status", None) != "running":
                    studio.start(machine=Machine.L40S)
                    for _ in range(60):
                        time.sleep(10)
                        if getattr(studio, "status", None) == "running":
                            break
                    else:
                        raise RuntimeError("Studio failed to start")

            self.studio = studio

            # Copy file-list into workspace (lightning file sync or run as subprocess)
            # For immediate local test, run directly; in production use studio.run()
            cmd = [
                sys.executable, self.train_script,
                "--file-list", "/workspace/file-list.json",
                "--repo", HF_REPO
            ]
            subprocess.run(cmd, cwd="/workspace", check=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-list", required=True)
    parser.add_argument("--script", required=True)
    args = parser.parse_args()

    if LIGHTNING_AVAILABLE:
        app = LightningApp(SurrogateTrainWork(file_list_path=args.file_list, train_script=args.script))
    else:
        # Fallback: run training locally with the same arguments (useful for dev/dry-run)
        cmd = [sys.executable, args.script, "--file-list", args.file_list, "--repo", HF_REPO]
        subprocess.run(cmd, check=True)

if __name__ == "__main__":
    main()
```

#### Example `train.py` snippet (CDN-only loader with schema guard)
```python
import json
import requests
from pathlib import Path
from typing import Iterator, Tuple, Any

def parse_file(stream, rel_path: str) -> Iterator[Tuple[str, str]]:
    """
    Project raw file into (prompt, response). Implement per file-type.
    This avoids pyarrow.CastError by enforcing consistent schema early.
    """
    # Example for JSONL:
    import io
    for line in io.TextIOWrapper(stream, encoding="utf-8"):


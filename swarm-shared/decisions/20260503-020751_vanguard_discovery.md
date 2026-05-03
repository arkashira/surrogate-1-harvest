# vanguard / discovery

## 1. Diagnosis
- No persisted `(repo, dateFolder) → file-list` manifest: every discovery/training run triggers authenticated `list_repo_tree`, burning HF API quota and risking 429s.
- Data loader likely uses `load_dataset(streaming=True)` or repeated per-file loads on heterogeneous repos, causing `pyarrow.CastError` from mixed schemas.
- No CDN-only fetch path: training still relies on authenticated `/api/` endpoints instead of public CDN URLs, amplifying rate-limit exposure.
- No reuse guard for Lightning Studio: orchestrator likely recreates studios instead of reusing running ones, wasting 80hr/mo quota.
- No idle-stop resilience: training dies when Lightning Studio stops (idle timeout) without restart logic.

## 2. Proposed change
Create `/opt/axentx/vanguard/discovery/manifest.py` + update discovery orchestrator to:
- Single authenticated `list_repo_tree` per `(repo, dateFolder)` → write `manifests/{repo}__{date}.json` (list of relative file paths).
- Embed manifest path in training script; data loader uses CDN-only URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with zero API calls during training.
- Add lightweight schema projection: parse only `{prompt, response}` at load time, ignore extra columns to avoid `pyarrow.CastError`.
- Add Lightning Studio reuse + idle restart guard.

Scope: one new file (`manifest.py`) + one small patch to the discovery orchestrator (likely `discover.py` or `train.py` in repo root).

## 3. Implementation

```bash
# Create directories if missing
mkdir -p /opt/axentx/vanguard/manifests
```

```python
# /opt/axentx/vanguard/discovery/manifest.py
import json
import os
import time
from pathlib import Path
from typing import List, Optional

from huggingface_hub import HfApi, list_repo_tree

MANIFEST_DIR = Path(__file__).parents[2] / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

api = HfApi()

def repo_date_manifest(repo_id: str, date_folder: str, revision: str = "main") -> Path:
    """Return manifest path for (repo_id, date_folder)."""
    safe_repo = repo_id.replace("/", "__")
    return MANIFEST_DIR / f"{safe_repo}__{date_folder}.json"

def build_manifest(repo_id: str, date_folder: str, revision: str = "main") -> List[str]:
    """
    Single authenticated list_repo_tree call for one date folder (non-recursive).
    Returns list of relative file paths under that folder.
    Implements rate-limit backoff (360s on 429).
    """
    manifest_path = repo_date_manifest(repo_id, date_folder, revision)

    # If manifest exists and is fresh (<24h), reuse
    if manifest_path.exists():
        age = time.time() - manifest_path.stat().st_mtime
        if age < 86400:
            return json.loads(manifest_path.read_text())

    # Single non-recursive tree call per folder (avoids 100x pagination)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            tree = list_repo_tree(
                repo_id=repo_id,
                revision=revision,
                path=date_folder,
                recursive=False
            )
            files = [
                f.rfilename
                for f in tree
                if not f.rfilename.endswith("/")  # skip subdirs
            ]
            manifest_path.write_text(json.dumps(files, indent=2))
            return files
        except Exception as exc:
            if hasattr(exc, "status") and exc.status == 429:
                wait = 360
                print(f"HF 429 rate-limited, waiting {wait}s (attempt {attempt+1})")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Failed to list {repo_id}/{date_folder} after {max_retries} attempts")

def cdn_urls(repo_id: str, file_paths: List[str]) -> List[str]:
    """Convert repo+paths into public CDN URLs (no auth required)."""
    return [
        f"https://huggingface.co/datasets/{repo_id}/resolve/main/{p}"
        for p in file_paths
    ]

def load_jsonl_projection(urls: List[str], prompt_key: str = "prompt", response_key: str = "response"):
    """
    Lightweight generator: fetch each JSONL via CDN and yield {prompt, response}.
    Ignores extra fields to avoid schema mismatches / pyarrow errors.
    """
    import requests

    for url in urls:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        for line in resp.text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                prompt = obj.get(prompt_key) or obj.get("instruction") or obj.get("input") or ""
                response = obj.get(response_key) or obj.get("output") or ""
                if prompt and response:
                    yield {"prompt": prompt, "response": response}
            except json.JSONDecodeError:
                continue
```

```python
# Example patch to discovery/train.py (or orchestrator) — integrate manifest + CDN
# Insert near top:
# from discovery.manifest import build_manifest, cdn_urls, load_jsonl_projection

# Replace dataset loading section with:
# repo_id = "your-org/your-dataset"
# date_folder = "batches/mirror-merged/2026-04-29"
# files = build_manifest(repo_id, date_folder)
# urls = cdn_urls(repo_id, files)
# dataset = list(load_jsonl_projection(urls))  # or stream into your training pipeline
```

```python
# Lightning Studio reuse + idle restart guard (orchestrator snippet)
from lightning import Teamspace, Studio, Machine

def get_or_start_studio(name: str, machine: Machine = Machine.L40S):
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name:
            if s.status == "running":
                return s
            # restart if stopped/idle-killed
            s.start(machine=machine)
            return s
    return Studio(name=name, machine=machine, create_ok=True)

# Usage in training orchestrator:
# studio = get_or_start_studio("vanguard-surrogate-train")
# studio.run(["python", "train.py", "--manifest", manifest_path])
```

## 4. Verification
1. Run manifest build once:
   ```bash
   cd /opt/axentx/vanguard
   python -c "from discovery.manifest import build_manifest, cdn_urls; f=build_manifest('your-org/your-dataset','batches/mirror-merged/2026-04-29'); print('files:', len(f)); print('cdn samples:', cdn_urls('your-org/your-dataset', f[:2]))"
   ```
   - Expect: `manifests/your-org__your-dataset__batches__mirror-merged__2026-04-29.json` created with file list; CDN URLs printed.

2. Confirm zero authenticated API calls during data load:
   - Run training script with network monitoring or logs; ensure no `huggingface.co/api` requests, only CDN `resolve/main` fetches.

3. Confirm schema resilience:
   - Place a mixed-schema JSONL in the repo (extra fields, missing keys) and verify `load_jsonl_projection` yields only `{prompt, response}` without `pyarrow.CastError`.

4. Confirm Lightning reuse:
   - Start a studio manually, rerun orchestrator; it should reuse the running studio (check logs: "reusing running studio") instead of creating a new one.

5. Confirm idle restart:
   - Stop the studio, rerun orchestrator; it should restart the studio and continue training (check status transitions).

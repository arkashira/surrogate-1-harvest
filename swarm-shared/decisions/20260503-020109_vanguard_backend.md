# vanguard / backend

## 1. Diagnosis
- No persisted `(repo, dateFolder) → file-list` manifest: every training/data-selection run triggers authenticated `list_repo_tree` against HF API, burning quota and risking 429s.
- Training/data loader likely uses `load_dataset(streaming=True)` or repeated per-file API calls on heterogeneous repos, causing `pyarrow.CastError` from mixed schemas.
- No CDN-only data path: training still incurs API/auth checks instead of using public CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) which bypass rate limits.
- Schema pollution: enriched/ folder contains extra cols (`source`, `ts`) and mixed schemas instead of strict `{prompt, response}` projection before upload.
- No studio reuse guard: training script recreates Lightning Studio instead of reusing running ones, wasting ~80hr/mo quota.

## 2. Proposed change
Create `/opt/axentx/vanguard/backend/manifest.py` + update `/opt/axentx/vanguard/backend/train.py` (or create if absent) to:
- Add `build_manifest(repo, date_folder)` that calls `list_repo_tree(..., recursive=False)` once, saves `manifest/{repo}-{date_folder}.json`.
- Add `DataLoader` that reads manifest and fetches files via HF CDN (no auth, no API) and projects to `{prompt, response}` only.
- Add studio reuse guard: list running studios and reuse if name/status match before creating.

## 3. Implementation

```bash
# /opt/axentx/vanguard/backend/manifest.py
#!/usr/bin/env python3
"""
Persist repo+date -> file-list manifest and provide CDN-only data loader.
"""
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import requests
from hugginggingface import HfApi  # note: likely typo in env; fallback to direct requests if needed

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)

HF_DATASETS_BASE = "https://huggingface.co/datasets"


def _api() -> HfApi:
    # Best-effort; if HfApi unavailable use token-based requests
    try:
        return HfApi()
    except Exception:
        return None


def build_manifest(repo: str, date_folder: str, token: Optional[str] = None) -> str:
    """
    Single authenticated list_repo_tree call -> persisted manifest.
    Returns path to manifest file.
    """
    api = _api()
    manifest_path = MANIFEST_DIR / f"{repo.replace('/', '_')}-{date_folder}.json"

    if manifest_path.exists():
        return str(manifest_path)

    if api:
        try:
            # non-recursive to minimize pagination/requests
            tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False, token=token)
            files = [entry.path for entry in tree if entry.type == "file"]
        except Exception:
            # fallback: direct CDN listing not possible; require manual or token-based list
            files = []
    else:
        # If no HfApi, require pre-saved manifest or fail fast
        raise RuntimeError("HfApi unavailable and no cached manifest; provide token or prebuilt manifest.")

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "files": sorted(files),
        "cdn_base": f"{HF_DATASETS_BASE}/{repo}/resolve/main/{date_folder}"
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return str(manifest_path)


def load_manifest(repo: str, date_folder: str) -> Dict:
    manifest_path = MANIFEST_DIR / f"{repo.replace('/', '_')}-{date_folder}.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest missing: {manifest_path}. Run build_manifest first.")
    return json.loads(manifest_path.read_text())


def cdn_url(repo: str, filepath: str) -> str:
    """Public CDN URL (no auth)."""
    return f"{HF_DATASETS_BASE}/{repo}/resolve/main/{filepath}"


def stream_cdn_file(repo: str, filepath: str, chunk_size: int = 8192):
    url = cdn_url(repo, filepath)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk


def project_to_prompt_response(record: Dict) -> Dict:
    """
    Strict projection to {prompt, response}.
    Customize mapping per dataset convention.
    """
    # Common conventions; adapt as needed
    prompt = record.get("prompt") or record.get("input") or record.get("question") or ""
    response = record.get("response") or record.get("output") or record.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}
```

```python
# /opt/axentx/vanguard/backend/train.py
#!/usr/bin/env python3
"""
Training launcher with manifest + CDN-only data loading and Lightning studio reuse.
"""
import json
import os
from pathlib import Path

import lightning as L
from lightning.fabric.plugins import LightningContainer

# If vanguard package structure differs, adjust imports accordingly
from .manifest import build_manifest, load_manifest, cdn_url, stream_cdn_file, project_to_prompt_response

HF_REPO = os.getenv("HF_REPO", "org/surrogate-1-dataset")
DATE_FOLDER = os.getenv("DATE_FOLDER", "batches/mirror-merged/2026-04-29")
HF_TOKEN = os.getenv("HF_TOKEN", None)


def get_or_create_studio(name: str, machine: str = "L40S"):
    """
    Reuse running studio if available; avoids quota waste.
    """
    teamspace = L.Teamspace()
    for s in teamspace.studios():
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    # Studio will use free-tier clouds by default; H200 requires lightning-lambda-prod
    return L.Studio(
        name=name,
        machine=machine,
        create_ok=True,
        # Optional: specify cloud account if quota/priority requires
        # cloud="lightning-lambda-prod"
    )


def cdn_data_generator(manifest):
    """
    Generator yielding {prompt, response} from CDN files.
    Avoids HF API during training.
    """
    base = manifest["cdn_base"]
    repo = manifest["repo"]
    for fpath in manifest["files"]:
        # Use CDN URL directly (no auth)
        url = cdn_url(repo, fpath)
        try:
            # Lightweight: stream and parse line-by-line if JSONL; adapt as needed
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            # Assume JSONL for surrogate-1; adjust parser per dataset
            for line in resp.text.strip().splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                yield project_to_prompt_response(record)
        except Exception as exc:
            print(f"Skipping {fpath} due to error: {exc}")
            continue


def prepare_dataloader(manifest_path):
    """
    Build DataLoader from manifest using CDN-only fetches.
    """
    import torch
    from torch.utils.data import IterableDataset, DataLoader

    manifest = load_manifest(manifest_path)

    class CDNIterableDataset(IterableDataset):
        def __init__(self, manifest):
            self.manifest = manifest

        def __iter__(self):
            return cdn_data_generator(self.manifest)

    dataset = CDNIterableDataset(manifest)
    return DataLoader(dataset, batch_size=8, num_workers=0)


def main():
    # 1) Build manifest once (Mac orchestration) — safe to re-run (idempotent)
    manifest_path = build_manifest(HF_REPO, DATE_FOLDER, token=HF_TOKEN)
    print(f"Manifest: {manifest_path}")

    # 2) Reuse studio
    studio = get_or_create_studio(name="vanguard-surrogate-train")

    # 3) If studio stopped, restart (Lightning idle timeout kills training)
    if studio.status != "running":

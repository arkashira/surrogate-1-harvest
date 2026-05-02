# vanguard / backend

## Final Synthesized Solution

### Diagnosis (Consolidated)
- **Rate-limit exposure**: repeated `list_repo_tree`/`list_repo_files` and dataset-loading calls hit HF API (1000 req/5 min) instead of using CDN.
- **No manifest cache**: no persisted file manifest; every training run re-enumerates the repo.
- **Lifecycle inefficiency**: Lightning Studio is recreated rather than reused → quota burn and cold-start latency.
- **Schema hazard**: mixed-schema parquet/jsonl files from ingestion are not projected to `{prompt,response}` → `CastError`/training failures.
- **Missing local cache**: no local cache for repo file lists or downloaded shards → redundant network round-trips.

### Proposed Change (Minimal, Actionable)
- Add **`/opt/axentx/vanguard/backend/manifest.py`** — single-responsibility module that produces and caches repo file manifests as JSON with TTL.
- Add **`/opt/axentx/vanguard/backend/train.py`** — Lightning entrypoint that consumes a manifest, uses CDN-only fetches, projects heterogeneous files to `{prompt,response}`, and attaches to a running Lightning Studio.
- Update **`/opt/axentx/vanguard/backend/requirements.txt`** to include `lightning`, `requests`, `pyarrow`, `datasets`, `tqdm`.
- Scope: one date-folder manifest + one training launcher that reuses a running Lightning Studio.

---

### Implementation

#### `/opt/axentx/vanguard/backend/requirements.txt`
```text
lightning>=2.3
requests>=2.31
pyarrow>=14
datasets>=2.14
tqdm>=4.66
```

#### `/opt/axentx/vanguard/backend/manifest.py`
```python
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import requests
from huggingface_hub import HfApi

CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

HF_API = HfApi()


def _cache_path(repo_id: str, folder: str) -> Path:
    safe = repo_id.replace("/", "_")
    return CACHE_DIR / f"{safe}__{folder.rstrip('/').replace('/', '_')}.json"


def list_repo_folder(repo_id: str, folder: str = "", token: str = None) -> List[Dict[str, str]]:
    """
    Single non-recursive HF API call to list immediate folder contents.
    """
    entries = HF_API.list_repo_tree(
        repo_id=repo_id,
        path=folder.rstrip("/"),
        recursive=False,
        token=token,
    )
    return [{"path": e.path, "type": e.type} for e in entries]


def build_manifest(
    repo_id: str,
    folder: str,
    token: str = None,
    ttl_seconds: int = 3600,
) -> Dict[str, Any]:
    """
    Build/cached manifest for repo_id/folder with CDN URLs.
    """
    cache_file = _cache_path(repo_id, folder)
    now = datetime.now(timezone.utc).timestamp()

    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if now - float(cached.get("_cached_at", 0)) < ttl_seconds:
                return cached
        except Exception:
            pass  # fall through to rebuild

    entries = list_repo_folder(repo_id, folder, token=token)
    files = [e for e in entries if e["type"] == "file"]

    file_items = []
    for f in files:
        cdn_url = (
            f"https://huggingface.co/datasets/{repo_id}/resolve/main/{f['path']}"
        )
        file_items.append(
            {
                "path": f["path"],
                "cdn_url": cdn_url,
                "hf_path": f["path"],
            }
        )

    manifest = {
        "repo_id": repo_id,
        "folder": folder.rstrip("/"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "_cached_at": now,
        "files": file_items,
    }

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    return manifest


def download_manifest_file(manifest: Dict[str, Any], idx: int, local_dir: Path) -> Path:
    """
    Download one file from manifest via CDN (public datasets require no auth header).
    Returns local path.
    """
    item = manifest["files"][idx]
    local_dir.mkdir(parents=True, exist_ok=True)
    out_path = local_dir / Path(item["path"]).name
    if out_path.exists():
        return out_path

    resp = requests.get(item["cdn_url"], timeout=60)
    resp.raise_for_status()
    out_path.write_bytes(resp.content)
    return out_path
```

#### `/opt/axentx/vanguard/backend/train.py`
```python
import argparse
import json
import sys
from pathlib import Path

import torch
from lightning.pytorch import LightningModule, Trainer, seed_everything
from lightning.pytorch.callbacks import ModelCheckpoint
from torch.utils.data import DataLoader, Dataset
from tqdm import tqdm

from manifest import build_manifest, download_manifest_file


# Projection helpers
def _project_jsonl(raw_bytes: bytes):
    import json as _json

    pairs = []
    for ln in raw_bytes.decode("utf-8").strip().split("\n"):
        if not ln.strip():
            continue
        obj = _json.loads(ln)
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
        response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
        if prompt and response:
            pairs.append({"prompt": str(prompt), "response": str(response)})
    return pairs


def _project_parquet(raw_bytes: bytes):
    import io

    import pyarrow.parquet as pq

    table = pq.read_table(io.BytesIO(raw_bytes))
    df = table.to_pandas()
    pairs = []
    for _, row in df.iterrows():
        prompt = row.get("prompt") or row.get("input") or row.get("question") or ""
        response = row.get("response") or row.get("output") or row.get("answer") or ""
        if prompt and response:
            pairs.append({"prompt": str(prompt), "response": str(response)})
    return pairs


def project_to_pair(raw_bytes: bytes, ext: str):
    ext = ext.lower()
    if ext == ".jsonl":
        return _project_jsonl(raw_bytes)
    if ext == ".parquet":
        return _project_parquet(raw_bytes)
    return []


# Dataset
class ManifestDataset(Dataset):
    def __init__(self, manifest_path: str, cache_dir: str = ".cache_downloads", max_files: int = 64):
        with open(manifest_path, "r", encoding="utf-8") as f:
            self.manifest = json.load(f)
        self.cache_dir = Path(cache_dir)
        self.max_files = max_files
        self._pairs: list = None

    def _load_all(self):
        if self._pairs is not None:
            return
        pairs = []
        files = self.manifest["files"][: self.max_files]
        for idx in tqdm(range(len(files)), desc="Loading files"):
            try:
                local_path = download_manifest_file(self.manifest, idx, self.cache_dir)
                ext = local_path.suffix
                raw = local_path.read_bytes()
                out = project_to_pair(raw, ext)
                pairs.extend(out)
            except Exception as e:
                print(f"Skip {files[idx]['path']}: {e}", file=sys.stderr)
        self._pairs = pairs

    def __len__(self):
        self._load_all()
        return len(self._pairs)

    def __getitem__(self, idx):
        self._load_all()
        return self._pairs[idx]


def build_dataloader(manifest_path: str, batch_size: int = 4, max_files: int = 64, num_workers: int = 0):

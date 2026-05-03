# vanguard / backend

## 1. Diagnosis
- No persisted `(repo, dateFolder) → file-list` manifest: every training/data-selection run triggers authenticated `list_repo_tree` against HF API, burning quota and risking 429s.
- Training/data loader likely uses `load_dataset(streaming=True)` or repeated per-file API calls on heterogeneous repos, causing `pyarrow.CastError` from mixed schemas.
- Missing CDN-only data path: authenticated API calls are used for file fetches during training instead of public CDN URLs, amplifying rate-limit exposure.
- No studio reuse guard: training script may recreate Lightning Studio instead of reusing a running one, wasting ~80hr/mo quota.
- No idle-stop resilience: Lightning Studio idle timeout kills long-running training jobs without automatic restart.

## 2. Proposed change
Create `/opt/axentx/vanguard/backend/training/prepare_filelist.py` and modify `/opt/axentx/vanguard/backend/training/train.py` (or create if absent) to:
- Persist a deterministic file-list JSON for a given `(repo, dateFolder)` after a single authenticated `list_repo_tree` call.
- Embed that file-list in training and use CDN-only URLs (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with `requests`/`aiohttp` and `pyarrow` projection to `{prompt, response}`.
- Add Lightning Studio reuse + idle-restart guard.

Scope: add `prepare_filelist.py`; create/modify `train.py`; add small util `hf_cdn.py`.

## 3. Implementation

```bash
# /opt/axentx/vanguard/backend/training/prepare_filelist.py
#!/usr/bin/env python3
"""
Generate and persist a deterministic file-list manifest for a repo+dateFolder.
Run from Mac (or any orchestration host) after rate-limit window clears.
"""
import argparse
import json
import os
import hashlib
from pathlib import Path

try:
    from huggingface_hub import HfApi
except ImportError:
    raise RuntimeError("Install huggingface_hub in orchestration env: pip install huggingface_hub")

HF_API = HfApi()

def list_date_files(repo_id: str, date_folder: str, out_dir: Path):
    """
    Single authenticated list_repo_tree call (non-recursive) for date_folder.
    Persists manifest: {repo_id}/{date_folder}/manifest.json
    """
    out_dir = out_dir / repo_id.replace("/", "_") / date_folder
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"

    # Avoid recursive to reduce pagination/requests
    tree = HF_API.list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
    files = [item.rfilename for item in tree if not item.rfilename.endswith("/")]

    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder.rstrip("/"),
        "files": sorted(files),
        "count": len(files),
        "sha256": hashlib.sha256(json.dumps(sorted(files)).encode()).hexdigest()
    }

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written: {manifest_path} ({len(files)} files)")
    return manifest_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate HF file-list manifest for CDN-only training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo, e.g. 'datasets/mycorp/surrogate-1'")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 'batches/mirror-merged/2026-04-29'")
    parser.add_argument("--out", default="manifests", help="Output root directory")
    args = parser.parse_args()

    list_date_files(args.repo, args.date, Path(args.out))
```

```python
# /opt/axentx/vanguard/backend/training/hf_cdn.py
"""
Utilities for CDN-only dataset fetching (no authenticated API calls during training).
"""
import aiohttp
import asyncio
import pyarrow as pa
import pyarrow.parquet as pq
from typing import List, Dict, Any, Optional
import os

CDN_BASE = "https://huggingface.co/datasets"

def cdn_url(repo_id: str, path: str) -> str:
    return f"{CDN_BASE}/{repo_id}/resolve/main/{path}"

async def fetch_parquet(session: aiohttp.ClientSession, url: str) -> Optional[pa.Table]:
    """Download parquet via CDN and return projected {prompt,response} table."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                print(f"CDN fetch failed {url}: {resp.status}")
                return None
            data = await resp.read()
            # Use pyarrow to read from bytes and project only needed columns
            table = pq.read_table(pa.BufferReader(data), columns=["prompt", "response"])
            return table
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

async def load_manifest_cdn(manifest_path: str, max_concurrent: int = 8) -> pa.Table:
    """
    Load manifest and fetch all files via CDN, concatenate into single table.
    Manifest format: {"repo_id": "...", "date_folder": "...", "files": [...]}
    """
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    repo_id = manifest["repo_id"]
    date_folder = manifest["date_folder"]
    files = manifest["files"]

    sem = asyncio.Semaphore(max_concurrent)
    async with aiohttp.ClientSession() as session:
        tasks = []
        for fn in files:
            full_path = f"{date_folder}/{fn}" if not fn.startswith(date_folder) else fn
            url = cdn_url(repo_id, full_path)
            tasks.append(_bounded_fetch(sem, session, url))
        results = await asyncio.gather(*tasks)

    tables = [t for t in results if t is not None and t.num_rows > 0]
    if not tables:
        raise RuntimeError("No data loaded from CDN")
    return pa.concat_tables(tables)

async def _bounded_fetch(sem, session, url):
    async with sem:
        return await fetch_parquet(session, url)

# sync wrapper for simple use
def load_manifest_cdn_sync(manifest_path: str, max_concurrent: int = 8) -> pa.Table:
    return asyncio.run(load_manifest_cdn(manifest_path, max_concurrent=max_concurrent))
```

```python
# /opt/axentx/vanguard/backend/training/train.py
#!/usr/bin/env python3
"""
Surrogate-1 training entrypoint (Lightning Studio compatible).
Uses CDN-only data loading and studio reuse/idle-restart guards.
"""
import os
import json
import argparse
from pathlib import Path

try:
    import lightning as L
    from lightning.pytorch import Trainer
    from lightning.pytorch.callbacks import ModelCheckpoint
    import torch
    from torch.utils.data import Dataset, DataLoader
except ImportError as e:
    raise RuntimeError("Install lightning/pytorch in training env") from e

try:
    from hf_cdn import load_manifest_cdn_sync
except ImportError:
    raise RuntimeError("hf_cdn.py must be importable")

# ---- Dataset ----
class SurrogateDataset(Dataset):
    def __init__(self, table):
        self.prompts = table.column("prompt").to_pylist()
        self.responses = table.column("response").to_pylist()

    def __len__(self):
        return len(self.prompts)

    def __getitem__(self, idx):
        # Tokenization should happen in collate_fn/model; here return raw text
        return {"prompt": self.prompts[idx], "response": self.responses[idx]}

# ---- Simple LightningModule ----
class SurrogateLM(L.LightningModule):
    def __init__(self, model_name: str = "gpt2", lr: float = 1e-4):
        super().__init__()
        from transformers import GPT2LMHeadModel, GPT2Tokenizer
        self.tokenizer = GPT2Tokenizer.from_pretrained(model_name)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model = GPT

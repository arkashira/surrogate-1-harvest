# vanguard / quality

## Final synthesized solution

### 1. Diagnosis (merged)
- **No persisted manifest** per `(repo, date_folder)` → every training run performs authenticated `list_repo_tree`/HF API calls, burning quota and risking 429s.
- **Authenticated API paths used for data** instead of public CDN URLs → rate limits apply on every file read.
- **No retry/backoff or 360s sleep on 429** → transient rate-limit failures abort training.
- **No local file-list cache for Lightning jobs** → each epoch re-enumerates remote files via API instead of zero-API CDN-only fetches.
- **Missing parquet schema/validity checks** → malformed/mixed-schema files cause `pyarrow.CastError` at runtime.
- **`load_dataset(streaming=True)` on heterogeneous repos** can trigger schema mismatches and hidden 429/128-commit caps that stall ingestion/training.

### 2. Single concrete change
Add `/opt/axentx/vanguard/training/file_manifest.py` and update the training launcher (`train.py` or equivalent) to:
- Build and persist a manifest with **one authenticated `list_repo_tree` call per `(repo, date_folder)`** (cached to disk, TTL 24h).
- Use **public CDN URLs only** for downloads (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with no Authorization header.
- Perform **robust 429 handling** (sleep 360s, retry) for both manifest fetch and file downloads.
- **Validate parquet files on download** (readable by pyarrow + required field checks) and delete invalid files before training sees them.
- **Pin schema in training** (explicit `features` or column allowlist) and avoid heterogeneous `load_dataset(streaming=True)` across repos; use local cached parquet paths with a deterministic dataset wrapper.

### 3. Implementation

#### `/opt/axentx/vanguard/training/file_manifest.py`
```python
import json
import time
import os
from pathlib import Path
from typing import List, Dict, Optional, Set

import requests
from huggingface_hub import HfApi

HF_API = HfApi()
CDN_ROOT = "https://huggingface.co/datasets"
DEFAULT_REQUIRED_FIELDS: Set[str] = {"prompt", "response", "text"}

def _cdn_url(repo: str, path: str) -> str:
    return f"{CDN_ROOT}/{repo}/resolve/main/{path}"

def _is_429(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 429 or "429" in str(exc)

def build_manifest(
    repo: str,
    date_folder: str,
    out_dir: str = ".manifests",
    ttl_seconds: int = 86400,
    recursive: bool = False
) -> Path:
    """
    Single authenticated list_repo_tree call for one date folder.
    Returns local path to manifest.json.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    safe_repo = repo.replace("/", "_")
    manifest_file = out_path / f"{safe_repo}_{date_folder}.json"

    # Reuse fresh manifest.
    if manifest_file.exists() and (time.time() - manifest_file.stat().st_mtime) < ttl_seconds:
        return manifest_file

    # Single API call with 429 handling.
    for attempt in range(2):
        try:
            tree = HF_API.list_repo_tree(repo=repo, path=date_folder, recursive=recursive)
            break
        except Exception as e:
            if _is_429(e):
                time.sleep(360)
                continue
            raise

    entries: List[Dict[str, object]] = []
    for entry in tree:
        if entry.get("type") == "file" and str(entry.get("path", "")).endswith(".parquet"):
            entries.append({
                "repo": repo,
                "path": entry["path"],
                "cdn_url": _cdn_url(repo, entry["path"]),
                "size": entry.get("size", 0)
            })

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": int(time.time()),
        "recursive": recursive,
        "files": entries
    }

    with open(manifest_file, "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest_file


def download_with_cdn_and_validate(
    entry: Dict[str, object],
    dest_dir: str = ".cache",
    required_fields: Optional[Set[str]] = None,
    timeout: int = 120
) -> Path:
    """
    Download via public CDN (no auth) and validate parquet.
    Retries on 429 with 360s sleep.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out_file = dest / Path(str(entry["path"])).name

    if out_file.exists() and out_file.stat().st_size > 0:
        # quick existence/size check; full validation below.
        pass
    else:
        url = str(entry["cdn_url"])
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=timeout, stream=True)
                if resp.status_code == 429:
                    time.sleep(360)
                    continue
                resp.raise_for_status()
                with open(out_file, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=16384):
                        f.write(chunk)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))

    # Parquet validation.
    try:
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(out_file)
        cols = set(pf.schema.names)
        req = required_fields or DEFAULT_REQUIRED_FIELDS
        if not any(r in cols for r in req):
            raise ValueError(f"Parquet missing expected fields. Found: {cols}")
    except Exception as e:
        if out_file.exists():
            out_file.unlink(missing_ok=True)
        raise ValueError(f"Invalid parquet {out_file}: {e}")

    return out_file
```

#### Update training launcher (`train.py` or equivalent)
```diff
+ from file_manifest import build_manifest, download_with_cdn_and_validate
+ import json

  class HFDataModule(LightningDataModule):
-     def __init__(self, repo, date_folder, cache_dir=".cache"):
+     def __init__(
+         self,
+         repo,
+         date_folder,
+         manifest_dir=".manifests",
+         cache_dir=".cache",
+         required_fields=None
+     ):
          super().__init__()
          self.repo = repo
          self.date_folder = date_folder
+         self.manifest_path = build_manifest(repo, date_folder, manifest_dir)
+         with open(self.manifest_path) as f:
+             self.manifest = json.load(f)
          self.cache_dir = cache_dir
+         self.required_fields = required_fields

      def prepare_data(self):
          # CDN-only downloads; zero authenticated API calls during training.
          for entry in self.manifest["files"]:
              download_with_cdn_and_validate(
                  entry,
                  dest_dir=self.cache_dir,
                  required_fields=self.required_fields
              )

      def train_dataloader(self):
          # Use local cached parquet files; avoid heterogeneous load_dataset(streaming=True).
          paths = [Path(self.cache_dir) / Path(e["path"]).name for e in self.manifest["files"]]
          # Replace with your deterministic dataset that reads parquet -> {prompt,response}.
          ds = YourParquetDataset(paths, required_fields=self.required_fields)
          return DataLoader(ds, batch_size=8, num_workers=4)
```

#### Lightweight dataset wrapper (example)
```python
# In your dataset module (e.g., /opt/axentx/vanguard/training/dataset.py)
import pyarrow.parquet as pq
from torch.utils.data import Dataset

class YourParquetDataset(Dataset):
    def __init__(self, parquet_paths, required_fields=None):
        self.required_fields = required_fields or {"prompt", "response", "text"}
        self.records = []
        for p in parquet_paths:
            table = pq.read_table(p)
           

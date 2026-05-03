# vanguard / backend

## Final consolidated solution

**Core diagnosis (accepted from Candidate 1)**  
- No persisted `(repo, dateFolder)` manifest → repeated authenticated `list_repo_tree` burns quota and causes 429s.  
- Using authenticated `/api/` paths instead of public CDN URLs wastes rate limits.  
- No retry/backoff or cooldown after 429; transient failures become hard errors.  
- `load_dataset(streaming=True)` across heterogeneous repos can raise `pyarrow.CastError` on mixed schemas.  
- No reuse guard for Lightning Studio; each run may create a new studio and burn quota.

**Chosen approach**  
- Single source of truth: a JSON manifest of parquet files for a given `(repo, dateFolder)`, generated once (or on cache miss) via HF API, then reused.  
- Training uses **CDN-only** downloads (no authenticated API calls during training).  
- Robust retry with exponential backoff and special handling for 429.  
- Schema-resilient parquet reading with safe column selection and casting.  
- Lightning training entrypoint with explicit reuse guard (no duplicate studios) and resource controls.

---

## Implementation

```bash
# Ensure backend module exists
mkdir -p /opt/axentx/vanguard/backend
touch /opt/axentx/vanguard/backend/__init__.py
```

```python
# /opt/axentx/vanguard/backend/train.py
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests
import torch
from lightning import Fabric, LightningModule
from lightning.pytorch import Trainer
from lightning.pytorch.loggers import CSVLogger
from lightning.pytorch.strategies import DDPStrategy

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "datasets/your-org/your-repo")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")
MANIFEST_PATH = Path(os.getenv("MANIFEST_PATH", f"/tmp/vanguard_manifest_{DATE_FOLDER}.json"))
CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main"
CACHE_DIR = Path(os.getenv("VANGUARD_CACHE_DIR", "/tmp/vanguard_cache"))

# ---- Manifest ----
def build_manifest(token: str, date_folder: str, output_path: Path = MANIFEST_PATH, repo: str = HF_DATASET_REPO) -> List[str]:
    """
    Run once (or on cache miss) on an orchestration host with HF_TOKEN.
    Non-recursive list for the date folder; keeps only parquet files.
    """
    from huggingface_hub import HfApi
    api = HfApi(token=token)
    entries = api.list_repo_tree(repo_id=repo, path=date_folder, recursive=False)
    paths = sorted(e.path for e in entries if e.path.lower().endswith(".parquet"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({"repo": repo, "date_folder": date_folder, "parquet_files": paths}, indent=2))
    return paths

def load_manifest(manifest_path: Path = MANIFEST_PATH) -> List[str]:
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest not found: {manifest_path}. Run build_manifest once with HF_TOKEN."
        )
    data = json.loads(manifest_path.read_text())
    # Support both plain list and object envelope
    if isinstance(data, dict):
        files = data.get("parquet_files", [])
    else:
        files = data
    if not isinstance(files, list) or not files:
        raise ValueError(f"Invalid manifest content in {manifest_path}")
    return files

# ---- CDN download with retry ----
def download_parquet_cdn(
    rel_path: str,
    local_path: Path,
    max_retries: int = 5,
    initial_backoff: int = 5,
    max_backoff: int = 360,
) -> Path:
    url = f"{CDN_BASE}/{rel_path}"
    local_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=60, stream=True)
            if resp.status_code == 429:
                wait = min(initial_backoff * (2 ** (attempt - 1)), max_backoff)
                print(f"429 rate-limited. Waiting {wait}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return local_path
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                wait = min(initial_backoff * (2 ** (attempt - 1)), max_backoff)
                print(f"429 on download. Waiting {wait}s")
                time.sleep(wait)
                continue
            raise
        except requests.RequestException:
            if attempt == max_retries:
                raise
            wait = min(initial_backoff * (2 ** (attempt - 1)), max_backoff)
            time.sleep(wait)

    raise RuntimeError(f"Failed to download {url} after {max_retries} attempts")

# ---- Schema-resilient parquet iterable ----
class CDNParquetIterable(torch.utils.data.IterableDataset):
    def __init__(
        self,
        manifest: List[str],
        cache_dir: Path = CACHE_DIR,
        max_parquet_files: int = -1,
        columns: Optional[List[str]] = None,
    ):
        super().__init__()
        self.manifest = manifest if max_parquet_files <= 0 else manifest[:max_parquet_files]
        self.cache_dir = Path(cache_dir)
        self.columns = columns or ["prompt", "response"]

    def _safe_read_table(self, local_file: Path):
        import pyarrow as pa
        import pyarrow.parquet as pq

        try:
            table = pq.read_table(local_file, columns=self.columns)
            missing = [c for c in self.columns if c not in table.column_names]
            if missing:
                # fallback: read all and select/rename best-effort
                table = pq.read_table(local_file)
                table = self._project_best_effort(table, self.columns)
            return table
        except Exception:
            # Last resort: read raw and coerce
            table = pq.read_table(local_file)
            table = self._project_best_effort(table, self.columns)
            return table

    def _project_best_effort(self, table, desired_cols):
        import pyarrow as pa
        available = table.column_names
        selected = []
        for d in desired_cols:
            if d in available:
                selected.append(d)
            else:
                # pick first text-like column
                cand = next((c for c in available if any(k in c.lower() for k in ("prompt", "response", "text", "completion", "answer"))), available[0])
                selected.append(cand)
        # deduplicate while preserving order
        seen = set()
        uniq = []
        for s in selected:
            if s not in seen:
                uniq.append(s)
                seen.add(s)
        # ensure we have at least one column
        if not uniq:
            uniq = [available[0]]
        table = table.select(uniq)
        # rename to desired_cols length (truncate/pad)
        rename_map = {}
        for i, name in enumerate(table.column_names):
            if i < len(desired_cols):
                rename_map[name] = desired_cols[i]
        if rename_map:
            table = table.rename_columns(rename_map)
        # coerce to string to avoid pyarrow.CastError downstream
        for i, field in enumerate(table.schema):
            if not pa.types.is_string(field.type):
                table = table.set_column(i, field.name, table.column(i).cast(pa.string()))
        return table

    def __iter__(self):
        import pyarrow.parquet as pq
        for rel_path in self.manifest:
            local_file = self.cache_dir / Path(rel_path).name
            if not local_file.exists():
                download_parquet_cdn(rel_path, local_file)
            table = self._safe_read_table(local_file)
            # Batch conversion to reduce overhead
            for batch in table.to_batches(max_chunksize=512):
               

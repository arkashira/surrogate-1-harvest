# vanguard / backend

## Final Synthesized Implementation

**Diagnosis (merged, de-duplicated)**
- No persisted manifest for `(repo, dateFolder) → file-list`; every training run performs authenticated `list_repo_tree`/`load_dataset` discovery and burns HF API quota with 429 risk.
- Training script uses `load_dataset(streaming=True)` on heterogeneous repos, triggering pyarrow `CastError` on mixed-schema files.
- No CDN-bypass strategy; data loading relies on authenticated `/api/` endpoints instead of public CDN URLs.
- Lightning Studio is recreated on each run instead of reused, wasting quota and risking idle-stop kills on long jobs.
- Missing robust retry/backoff for CDN fetches and no row-group memory control for large Parquet files.

**Proposed change (merged)**
- Add `/opt/axentx/vanguard/backend/training/manifest.py` to generate and cache a `manifest.json` for a given `(repo, dateFolder)` via a single authenticated `list_repo_tree` call.
- Update `train.py` to:
  - Load the manifest and perform CDN-only fetches (zero HF API calls).
  - Project heterogeneous schemas to `{prompt, response}` at parse time.
  - Reuse a running Lightning Studio or restart cleanly if idle-stopped.
  - Add retries, timeouts, and row-group streaming to bound memory.
- Keep orchestration on Mac simple: run `manifest.py` once, then launch `train.py`.

---

### 1) Manifest utility
`/opt/axentx/vanguard/backend/training/manifest.py`

```python
#!/usr/bin/env python3
"""
Generate and cache file manifest for a HuggingFace dataset repo/dateFolder.
Manifest format:
{
  "repo": "...",
  "dateFolder": "...",
  "files": ["path/to/file1.parquet", ...],
  "generated_at": "ISO-8601"
}
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from huggingface_hub import HfApi
except Exception:  # noqa
    HfApi = None  # optional; CLI can populate manifest manually

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True, parents=True)


def manifest_path(repo: str, date_folder: str) -> Path:
    safe_repo = repo.replace("/", "__")
    return MANIFEST_DIR / f"{safe_repo}__{date_folder}.json"


def list_date_files(
    repo: str,
    date_folder: str,
    recursive: bool = False,
    extensions: Optional[List[str]] = None,
) -> List[str]:
    """
    Single authenticated list_repo_tree call for one date folder.
    Returns relative file paths under date_folder.
    """
    if HfApi is None:
        raise RuntimeError("huggingface_hub not installed")
    api = HfApi()
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=recursive)
    files = [item.path for item in tree if item.type == "file"]
    if extensions:
        ext_set = {e if e.startswith(".") else f".{e}" for e in extensions}
        files = [f for f in files if Path(f).suffix.lower() in ext_set]
    return sorted(files)


def build_manifest(
    repo: str,
    date_folder: str,
    recursive: bool = False,
    extensions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    files = list_date_files(repo, date_folder, recursive=recursive, extensions=extensions)
    manifest = {
        "repo": repo,
        "dateFolder": date_folder,
        "files": files,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return manifest


def save_manifest(repo: str, date_folder: str, manifest: Dict[str, Any]) -> Path:
    p = manifest_path(repo, date_folder)
    p.write_text(json.dumps(manifest, indent=2))
    return p


def load_manifest(repo: str, date_folder: str) -> Dict[str, Any]:
    p = manifest_path(repo, date_folder)
    if not p.exists():
        raise FileNotFoundError(f"Manifest not found: {p}")
    return json.loads(p.read_text())


def cli_build(repo: str, date_folder: str, recursive: bool = False) -> None:
    manifest = build_manifest(repo, date_folder, recursive=recursive, extensions=[".parquet"])
    p = save_manifest(repo, date_folder, manifest)
    print(f"Manifest saved: {p}")
    print(f"Files: {len(manifest['files'])}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: manifest.py <repo> <dateFolder> [--recursive]")
        sys.exit(1)
    repo_arg = sys.argv[1]
    date_arg = sys.argv[2]
    recursive_flag = "--recursive" in sys.argv
    cli_build(repo_arg, date_arg, recursive=recursive_flag)
```

---

### 2) Training script (CDN-only, schema projection, retries, Studio reuse)
`/opt/axentx/vanguard/backend/training/train.py`

```python
#!/usr/bin/env python3
"""
Lightning training script that uses CDN-only fetches via a pre-generated manifest.

Workflow (Mac orchestration):
1) Generate manifest once:
   python manifest.py <repo> <dateFolder> [--recursive]

2) Launch training:
   python train.py --repo <repo> --date <dateFolder> [--reuse-studio] [--max-files N]
"""
import argparse
import io
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import pyarrow as pa
import pyarrow.parquet as pq
import requests
import torch
from requests.adapters import HTTPAdapter, Retry
from torch.utils.data import IterableDataset, DataLoader

try:
    from lightning import Fabric, LightningModule
    from lightning.fabric.plugins import LightningStudioPlugin
    from lightning.fabric.strategies import DDPStrategy
except ImportError as e:
    print("Missing Lightning dependency:", e)
    sys.exit(1)

# Local
from backend.training.manifest import load_manifest, manifest_path

HF_DATASETS_CDN = "https://huggingface.co/datasets"
DEFAULT_COLUMNS = ("prompt", "response")


def _make_session() -> requests.Session:
    retries = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retries, pool_maxsize=32))
    session.mount("http://", HTTPAdapter(max_retries=retries, pool_maxsize=32))
    return session


def cdn_url(repo: str, file_path: str) -> str:
    # Public CDN URL — no Authorization header required
    return f"{HF_DATASETS_CDN}/{repo}/resolve/main/{file_path}"


class ParquetPairIterable(IterableDataset):
    """
    Stream parquet files from CDN and yield (prompt, response) pairs.
    Projects heterogeneous schemas to {prompt, response} at parse time.
    """

    def __init__(
        self,
        repo: str,
        file_paths: list,
        session: requests.Session,
        max_files: int = None,
        columns: Tuple[str, str] = DEFAULT_COLUMNS,
        timeout: float = 30.0,
    ):
        self.repo = repo
        self.file_paths = file_paths if max_files is None else file_paths[:max_files]
        self.session = session
        self.columns = columns
        self.timeout = timeout

    def __iter__(self) -> Iterator[Tuple[str, str]]:
        for rel_path in self.file_paths:
            url = cdn_url(self.repo, rel_path)
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            content = io.BytesIO(resp.content)

            try:
                with pq.ParquetFile(content) as pf:
                    # Iterate in small batches to bound memory

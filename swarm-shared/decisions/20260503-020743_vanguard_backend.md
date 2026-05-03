# vanguard / backend

## Final Synthesis & Actionable Implementation

**Core diagnosis (accepted from both candidates):**  
- Repeated authenticated `list_repo_tree` burns HF quota and risks 429s.  
- Data loader likely causes `pyarrow.CastError` from mixed schemas and repeated file loads.  
- Lightning training wastes quota by not reusing studios and lacks idle-stop handling.  
- No CDN bypass: authenticated API calls are used when public CDN URLs would avoid rate limits.  
- Attribution/schema pollution: extra columns and mixed schemas instead of strict `{prompt, response}` projection.

**Best-practice resolution (favoring correctness + concrete actionability):**  
1. **Single manifest per `(repo, dateFolder)`** persisted on disk; built once on orchestrator (Mac).  
2. **Training uses HF CDN URLs** (no auth, no API calls) to fetch only needed parquet files.  
3. **Strict projection to `{prompt, response}`** at parse time; malformed files skipped with clear logs.  
4. **Lightning studio reuse + idle-stop handling** to avoid unnecessary spins.  
5. **Defensive schema handling** and timeouts to prevent `pyarrow.CastError` and hangs.

---

### 1) Manifest module (orchestrator/Mac)

`/opt/axentx/vanguard/backend/manifest.py`

```python
#!/usr/bin/env python3
"""
Generate and cache (repo, dateFolder) -> file-list manifest.
Run from orchestrator (Mac) only. Avoids repeated HF API list_repo_tree calls.
"""
import json
import os
from pathlib import Path
from typing import Dict, List

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    list_repo_tree = None


MANIFEST_ROOT = Path(__file__).parent / "manifests"
MANIFEST_ROOT.mkdir(exist_ok=True, parents=True)


def manifest_path(repo: str, date_folder: str) -> Path:
    safe_repo = repo.replace("/", "_")
    return MANIFEST_ROOT / f"{safe_repo}_{date_folder}.json"


def build_manifest(repo: str, date_folder: str, token: str = None) -> List[str]:
    """
    Single API call to list top-level of date_folder.
    Returns list of file paths (relative to repo root).
    """
    if list_repo_tree is None:
        raise RuntimeError("huggingface_hub not installed")

    tree = list_repo_tree(
        repo_id=repo,
        path=date_folder,
        recursive=False,
        token=token,
    )
    files = [item.rfilename for item in tree if item.type == "file"]
    out = manifest_path(repo, date_folder)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"repo": repo, "date_folder": date_folder, "files": files}, f, indent=2)
    return files


def load_manifest(repo: str, date_folder: str) -> List[str]:
    p = manifest_path(repo, date_folder)
    if not p.is_file():
        raise FileNotFoundError(f"Manifest missing: {p}. Run build_manifest first.")
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return data["files"]
```

---

### 2) Lightning-compatible CDN loader

`/opt/axentx/vanguard/backend/train_loader.py`

```python
#!/usr/bin/env python3
"""
Lightning-compatible DataLoader that:
- Uses cached manifest to know which files to load.
- Downloads via HF CDN (no auth, no API calls).
- Projects each file to {prompt, response} only.
"""
import logging
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from torch.utils.data import IterableDataset

logger = logging.getLogger(__name__)

HF_CDN = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def cdn_url(repo: str, path: str) -> str:
    return HF_CDN.format(repo=repo, path=path)


def project_to_prompt_response(raw: Dict) -> Dict[str, str]:
    """
    Strict projection to {prompt, response}.
    """
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or raw.get("completion") or ""
    return {"prompt": str(prompt), "response": str(response)}


class CDNParquetIterable(IterableDataset):
    def __init__(
        self,
        repo: str,
        files: List[str],
        max_files: Optional[int] = None,
        start: int = 0,
        timeout: int = 30,
    ):
        self.repo = repo
        self.files = [f for f in files if f.endswith(".parquet")][start:max_files]
        self.timeout = timeout

    def __iter__(self) -> Iterator[Dict[str, str]]:
        for rpath in self.files:
            url = cdn_url(self.repo, rpath)
            try:
                resp = requests.get(url, timeout=self.timeout)
                resp.raise_for_status()
            except Exception as exc:
                logger.warning("Failed to fetch %s: %s", rpath, exc)
                continue

            try:
                with pa.BufferReader(resp.content) as reader:
                    table = pq.read_table(reader)
            except Exception as exc:
                logger.warning("Failed to parse parquet %s: %s", rpath, exc)
                continue

            # Validate schema minimally
            cols = table.column_names
            has_prompt = any(c in cols for c in ("prompt", "input", "question"))
            has_response = any(c in cols for c in ("response", "output", "answer", "completion"))
            if not (has_prompt and has_response):
                logger.warning("Skipping file with missing prompt/response cols: %s", rpath)
                continue

            for batch in table.to_batches(max_chunksize=1024):
                df = batch.to_pydict()
                n = len(next(iter(df.values())))
                for i in range(n):
                    row = {k: df[k][i] for k in df}
                    yield project_to_prompt_response(row)
```

---

### 3) Orchestrator + Lightning app with studio reuse and idle-stop

`/opt/axentx/vanguard/backend/orchestrator.py`

```python
#!/usr/bin/env python3
"""
Orchestrator (run on Mac):
- Build manifest once per (repo, dateFolder) when needed.
- Launch Lightning Studio and reuse running instance.
- Configure idle-stop to save quota.
"""
import os
import sys
from pathlib import Path

from lightning import LightningWork, LightningFlow, LightningApp

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from manifest import build_manifest, load_manifest, manifest_path
from train_loader import CDNParquetIterable


REPO = "your-org/your-dataset"
DATE_FOLDER = "batches/mirror-merged/2026-04-29"
HF_TOKEN = os.getenv("HF_TOKEN")


def ensure_manifest() -> list:
    p = manifest_path(REPO, DATE_FOLDER)
    if not p.is_file():
        print("Building manifest (single HF API call)...")
        build_manifest(REPO, DATE_FOLDER, token=HF_TOKEN)
    return load_manifest(REPO, DATE_FOLDER)


class SurrogateTrainer(LightningWork):
    def __init__(self, files: list, cloud_build_config=None, **kwargs):
        super().__init__(cloud_build_config=cloud_build_config, **kwargs)
        self.files = files

    def run(self):
        # This runs inside the Lightning Studio session.
        # Reuse this instance; Lightning will restart only if stopped.
        from torch.utils.data import DataLoader

        dataset = CDNParquetIterable(REPO, self.files, max_files=256)
        loader = DataLoader(dataset, batch_size=8, num_workers=2)

        for i, batch in enumerate(loader):
            # Replace with actual surrogate training step
            if i >= 10:
                break
            print(f"Step {i}: prompts={len

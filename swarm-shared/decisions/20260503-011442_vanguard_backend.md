# vanguard / backend

## Final Consolidated Implementation

### 1. Diagnosis (merged)
- **No persisted `(repo, dateFolder)` manifest** → every backend request triggers authenticated `list_repo_tree` / HF API calls, burning quota and risking 429s.
- **Data fetches use authenticated API paths** instead of public CDN → avoidable auth overhead and stricter rate limits.
- **Training/ingestion scripts re-enumerate repos repeatedly** (no file-list cache) → amplifies quota burn and commit-cap pressure.
- **Missing deterministic repo selection for writes** (no sibling repo hashing) → ingestion risks hitting HF’s 128-commit/hr/repo cap.
- **No reuse guard for Lightning Studio** → repeated `Studio(create_ok=True)` burns 80hr/mo quota on recreation instead of reuse.
- **`load_dataset(streaming=True)` on heterogeneous repos** → pyarrow `CastError` on mixed schemas (schema drift across shards).

### 2. Proposed change (merged)
Add a lightweight backend manifest service that:
- Persists `(repo, dateFolder)` → file-list JSON to disk (or KV) after a single API call.
- Uses public CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) for all data fetches (zero auth, higher CDN limits).
- Embeds the file list in training/ingest scripts so Lightning workers do CDN-only fetches.
- Adds deterministic repo selection for writes via hash-slug → sibling repo.
- Adds a small `lightning_studio_reuse()` helper to prevent unnecessary recreation.
- Adds schema validation and per-file schema checks to prevent `CastError` during streaming.

Scope:
- New file: `/opt/axentx/vanguard/backend/manifest.py`
- Update: `/opt/axentx/vanguard/backend/train.py` (or equivalent launcher) to accept file-list path and use CDN fetches + schema checks.
- Update: `/opt/axentx/vanguard/backend/ingest.py` (or equivalent) to use sibling repo hashing, CDN downloads, and schema validation.

### 3. Implementation

```bash
# Create manifest module
cat > /opt/axentx/vanguard/backend/manifest.py << 'PY'
import json
import os
import hashlib
from pathlib import Path
from typing import List, Optional, Dict, Any

try:
    from huggingface_hub import list_repo_tree, hf_hub_download
except ImportError:
    list_repo_tree = None
    hf_hub_download = None

MANIFEST_DIR = Path(os.getenv("VANGUARD_MANIFEST_DIR", "/opt/axentx/vanguard/data/manifests"))
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

SIBLING_REPOS = [
    "vanguard-dataset",
    "vanguard-dataset-1",
    "vanguard-dataset-2",
    "vanguard-dataset-3",
    "vanguard-dataset-4",
]

def _manifest_path(repo: str, date_folder: str) -> Path:
    safe = repo.replace("/", "_")
    return MANIFEST_DIR / f"{safe}__{date_folder}.json"

def list_files_cached(repo: str, date_folder: str, token: Optional[str] = None) -> List[str]:
    """
    Single authenticated API call per (repo,dateFolder).
    Returns list of file paths under date_folder (non-recursive by design).
    """
    mp = _manifest_path(repo, date_folder)
    if mp.exists():
        return json.loads(mp.read_text())

    if list_repo_tree is None:
        raise RuntimeError("huggingface_hub not installed")

    # One API call: non-recursive to avoid pagination explosion
    tree = list_repo_tree(repo=repo, path=date_folder, recursive=False, token=token)
    files = [item.rfilename for item in tree if item.type == "file"]

    mp.write_text(json.dumps(files, sort_keys=True))
    return files

def cdn_url(repo: str, repo_type: str, path: str) -> str:
    """
    Public CDN URL — no Authorization header required.
    Bypasses HF API rate limits entirely for downloads.
    """
    return f"https://huggingface.co/{repo_type}/{repo}/resolve/main/{path}"

def pick_sibling_repo(slug: str) -> str:
    """Deterministic sibling repo selection to spread commit load."""
    digest = hashlib.sha256(slug.encode()).hexdigest()
    idx = int(digest[:8], 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]

def lightning_studio_reuse(teamspace, name: str, create_ok: bool = True, **kwargs):
    """
    Reuse a running Studio to save quota.
    Returns (studio, created: bool)
    """
    from lightning.pytorch.studio import Studio  # type: ignore
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            return s, False
    if not create_ok:
        raise RuntimeError(f"No running Studio named {name}")
    studio = Studio(name=name, **kwargs)
    return studio, True

def infer_and_validate_schema(file_path: str, sample_size: int = 100) -> Dict[str, Any]:
    """
    Infer schema from a sample of JSON lines and validate consistency.
    Returns schema dict with field names and types.
    """
    import pyarrow as pa
    from pyarrow import json as paj

    schema = None
    with open(file_path) as f:
        for i, line in enumerate(f):
            if i >= sample_size:
                break
            if not line.strip():
                continue
            try:
                table = paj.read_json(line)
                if schema is None:
                    schema = table.schema
                else:
                    # Check compatibility
                    try:
                        table = table.cast(schema)
                    except pa.ArrowInvalid:
                        raise ValueError(f"Schema mismatch at line {i}: {line[:100]}")
            except Exception as e:
                raise ValueError(f"Failed to parse JSON line {i}: {e}")
    return schema
PY

# Update train.py (example snippet to embed file list, use CDN, and validate schema)
cat > /opt/axentx/vanguard/backend/train_snippet.py << 'PY'
import json
from pathlib import Path
import torch
from torch.utils.data import IterableDataset
import requests

class CDNTextDataset(IterableDataset):
    def __init__(self, file_list_path: str, repo: str, repo_type: str = "datasets", validate_schema: bool = True):
        super().__init__()
        self.repo = repo
        self.repo_type = repo_type
        with open(file_list_path) as f:
            self.files = json.load(f)
        self.validate_schema = validate_schema

    def _stream(self):
        for rel in self.files:
            url = f"https://huggingface.co/{self.repo_type}/{self.repo}/resolve/main/{rel}"
            # CDN download — no auth header
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            # Save to temp file for schema validation
            temp_path = Path("/tmp") / rel.replace("/", "_")
            temp_path.write_bytes(resp.content)
            if self.validate_schema:
                from .manifest import infer_and_validate_schema
                infer_and_validate_schema(str(temp_path))
            # Yield parsed records
            for line in resp.text.splitlines():
                line = line.strip()
                if line:
                    yield line

    def __iter__(self):
        return self._stream()

# Usage in Lightning training script:
#   dataset = CDNTextDataset("manifests/vanguard-dataset__2026-05-03.json", "owner/vanguard-dataset")
#   train_loader = torch.utils.data.DataLoader(dataset, batch_size=...)
PY

# Update ingest helper (sibling repo + CDN + schema validation)
cat > /opt/axentx/vanguard/backend/ingest_snippet.py << 'PY'
import requests
from .manifest import pick_sibling_repo, cdn_url, infer_and_validate_schema

def download_via_cdn(repo: str, repo_type: str, path: str, local_path: str):
    url = cdn_url(repo, repo_type, path)
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
   

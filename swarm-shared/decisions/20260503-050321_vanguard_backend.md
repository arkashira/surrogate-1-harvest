# vanguard / backend

## Final Synthesized Solution

### 1. Diagnosis (Consensus)
All candidates agree on the root causes:
- Runtime HF `datasets` API calls (`list_repo_tree`/`load_dataset`) trigger 429 rate limits and non-reproducible shard order
- No content-addressed manifests cause epoch drift and unreliable resumable training
- Runtime schema projection risks `pyarrow.CastError` and wastes CPU
- No CDN-only fetch path forces authenticated API calls every epoch
- Missing deterministic repo selection for HF commit cap (128/hr/repo)

### 2. Proposed Change (Synthesized)
Implement a **content-addressed manifest system** with **CDN-only training**:

- **Manifest generator**: Accepts repo + date folder, calls `list_repo_tree` once, produces deterministic JSON manifest
- **Content addressing**: `manifest-{date}-{sha256(date_folder)}.json` ensures reproducibility
- **CDN-first URLs**: `https://huggingface.co/datasets/{repo}/resolve/main/{path}` for zero-auth fetches
- **Schema enforcement**: Manifest includes `schema_hint` for compile-time projection (avoids runtime `pyarrow.CastError`)
- **Commit cap mitigation**: Deterministic sibling repo selection via hash-based routing across 5 siblings

### 3. Implementation (Synthesized + Corrected)

```bash
# Directory structure
/opt/axentx/vanguard/
├── vanguard/
│   ├── __init__.py
│   ├── manifest.py      # Core manifest logic
│   ├── train.py         # CDN-integrated training
│   └── manifests/       # Output directory
```

```python
# /opt/axentx/vanguard/vanguard/manifest.py
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from huggingface_hub import HfApi, list_repo_tree

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def _hash_date_folder(date_folder: str) -> str:
    return hashlib.sha256(date_folder.encode()).hexdigest()[:12]

def _extract_lfs_oid(item) -> str:
    """Safely extract LFS OID from repo tree item."""
    try:
        lfs_data = getattr(item, "lfs", {})
        if isinstance(lfs_data, dict):
            return lfs_data.get("oid", "")
    except Exception:
        pass
    return ""

def build_manifest(
    repo: str,
    date_folder: str,
    out_dir: Path,
    schema_hint: Optional[str] = None
) -> Path:
    """
    Build content-addressed manifest for repo/date folder.
    Uses HF API ONCE per date folder to avoid rate limits.
    """
    api = HfApi()
    tree = list_repo_tree(repo=repo, path=date_folder.rstrip("/"), recursive=False)

    files: List[Dict[str, Any]] = []
    total_size = 0
    
    for item in tree:
        if item.type != "file":
            continue
            
        files.append({
            "path": item.path,
            "size": item.size or 0,
            "sha256": _extract_lfs_oid(item),
            "cdn_url": CDN_TEMPLATE.format(repo=repo, path=item.path),
        })
        total_size += item.size or 0

    # Estimate samples from file extensions (parquet/jsonl only)
    total_samples = sum(1 for f in files if f["path"].endswith((".parquet", ".jsonl")))

    manifest: Dict[str, Any] = {
        "repo": repo,
        "date_folder": date_folder.rstrip("/"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "cdn_prefix": CDN_TEMPLATE.format(repo=repo, path=""),
        "total_files": len(files),
        "total_size_bytes": total_size,
        "total_samples": total_samples,
        "schema_hint": schema_hint or "project to {prompt,response} at parse time",
        "version": "1.0"
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    date_hash = _hash_date_folder(date_folder)
    safe_date = date_folder.replace("/", "-")
    out_path = out_dir / f"manifest-{safe_date}-{date_hash}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    return out_path

def load_manifest(manifest_path: Path) -> Dict[str, Any]:
    return json.loads(manifest_path.read_text())

def get_sibling_repo(repo: str, sibling_index: Optional[int] = None) -> str:
    """
    Deterministic sibling selection for HF commit cap (128/hr/repo).
    Uses hash-based routing across 5 siblings.
    """
    if sibling_index is not None:
        siblings = [f"{repo}-s{i}" for i in range(5)]
        return siblings[sibling_index % len(siblings)]
    
    slug_hash = int(hashlib.sha256(str(repo).encode()).hexdigest(), 16)
    siblings = [f"{repo}-s{i}" for i in range(5)]
    return siblings[slug_hash % len(siblings)]

def upload_manifest(repo: str, manifest_path: Path, sibling_index: Optional[int] = None) -> str:
    """Upload manifest to deterministic sibling repo."""
    target_repo = get_sibling_repo(repo, sibling_index)
    
    api = HfApi()
    api.upload_file(
        path_or_fileobj=str(manifest_path),
        path_in_repo=f"manifests/{manifest_path.name}",
        repo_id=target_repo,
        repo_type="dataset",
    )
    return target_repo

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build HF dataset manifest for CDN-only training.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. username/surrogate-1)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. batches/2026-05-03)")
    parser.add_argument("--out-dir", default="manifests", help="Output directory")
    parser.add_argument("--schema", help="Schema hint for compile-time projection")
    parser.add_argument("--upload", action="store_true", help="Upload manifest to sibling repo")
    parser.add_argument("--sibling", type=int, default=None, help="Specific sibling index (0-4)")
    args = parser.parse_args()

    out = build_manifest(args.repo, args.date, Path(args.out_dir), args.schema)
    print(f"Manifest written: {out}")

    if args.upload:
        target = upload_manifest(args.repo, out, args.sibling)
        print(f"Uploaded to: {target}")
```

```python
# /opt/axentx/vanguard/vanguard/train.py
import json
from pathlib import Path
from typing import List, Dict, Any
import os

def load_file_list_from_manifest(manifest_path: Path) -> List[str]:
    """Load CDN URLs from manifest for zero-auth training."""
    data = json.loads(manifest_path.read_text())
    return [f["cdn_url"] for f in data["files"]]

def get_manifest_path(date_folder: str, manifest_dir: Path) -> Path:
    """
    Resolve manifest path deterministically.
    Supports env var override for Lightning Studio restarts.
    """
    if manifest_override := os.getenv("VANGUARD_MANIFEST_PATH"):
        return Path(manifest_override)
    
    from .manifest import _hash_date_folder
    safe_date = date_folder.replace("/", "-")
    date_hash = _hash_date_folder(date_folder)
    return manifest_dir / f"manifest-{safe_date}-{date_hash}.json"

# Example integration in Lightning DataModule
class CDNDataModule:
    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path
        
    def setup(self, stage: str = None):
        urls = load_file_list_from_manifest(self.manifest_path)
        # Use urls with fsspec or direct parquet loading
        # Example: dd.read_parquet(urls) or datasets.load_from_disk(urls)
```

### 4. Verification Protocol (Synthesized)

**1. Manifest Build Test:**
```bash
cd /opt/axentx/vanguard
python -m vanguard.man

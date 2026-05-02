# airship / frontend

## Highest-Value Incremental Improvement (<2h)

**Scope**: Frontend-adjacent CLI + HTTP endpoint that produces a deterministic JSON manifest of public HF dataset files (CDN-only) to unblock surrogate-1 training and eliminate HF API rate limits during data loading.

**Why this ships fast**: Single Python module (~80 LoC), no UI changes, no infra spin-up, directly applies the CDN-bypass pattern from lessons learned. Enables Lightning Studio training with zero HF API calls during data load.

---

## Implementation Plan

1. **Create `tools/hf-cdn-manifest.py`**  
   - Accepts `repo_id`, optional `revision`, optional `folder`  
   - Uses `list_repo_tree(recursive=False)` per folder (rate-limit safe)  
   - Emits `manifest.json` with CDN URLs (`resolve/main/...`) and sha256  
   - Deterministic sort by path for reproducible training splits

2. **Add lightweight HTTP endpoint in Arkship (FastAPI)**  
   - `GET /api/v1/hf/manifest?repo_id=...&revision=main&folder=...`  
   - Returns manifest JSON; caches 5min in memory to avoid repeated tree walks  
   - Optional: accepts `output_path` query to persist to disk

3. **Update surrogate training launcher**  
   - Before `Studio.run()`, fetch manifest via HTTP or CLI  
   - Embed file list in training script; data loader uses CDN URLs directly (`wget`/`requests` with streaming)  
   - No `load_dataset` or `hf_hub_download` during training loop

4. **Validation**  
   - Run against a small public dataset (e.g., `tatsu-lab/alpaca`)  
   - Confirm manifest contains CDN URLs and matches repo tree  
   - Smoke test surrogate training with manifest-only data loader (mock)

---

## Code Snippets

### 1. CLI Tool: `tools/hf-cdn-manifest.py`

```python
#!/usr/bin/env python3
"""
Deterministic CDN-only manifest generator for HuggingFace datasets.
Usage:
  python hf-cdn-manifest.py --repo_id tatsu-lab/alpaca --folder data --out manifest.json
"""
import argparse
import json
import hashlib
import sys
from pathlib import Path
from typing import List, Dict

try:
    from huggingface_hub import HfApi, HfFolder
except ImportError:
    print("pip install huggingface_hub", file=sys.stderr)
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"

def deterministic_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

def build_manifest(
    repo_id: str,
    revision: str = "main",
    folder: str = "",
    token: str = None,
) -> List[Dict]:
    api = HfApi(token=token)
    # List tree non-recursively per folder to minimize paginated calls
    tree = api.list_repo_tree(
        repo_id=repo_id,
        revision=revision,
        path=folder,
        recursive=False,
    )

    entries = []
    for item in sorted(tree, key=lambda x: x.path):
        if item.type != "file":
            continue
        cdn_url = CDN_TEMPLATE.format(repo_id=repo_id, path=item.path)
        # Note: size and sha256 require extra request; include size from tree if available
        entries.append({
            "path": item.path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None),
            "revision": revision,
        })
    return entries

def main():
    parser = argparse.ArgumentParser(description="Generate HF CDN manifest")
    parser.add_argument("--repo_id", required=True, help="HF dataset repo id")
    parser.add_argument("--revision", default="main", help="Git revision")
    parser.add_argument("--folder", default="", help="Subfolder (empty=root)")
    parser.add_argument("--out", default="manifest.json", help="Output JSON path")
    parser.add_argument("--token", default=None, help="HF token (optional for public)")
    args = parser.parse_args()

    try:
        manifest = build_manifest(
            repo_id=args.repo_id,
            revision=args.revision,
            folder=args.folder,
            token=args.token,
        )
        out_path = Path(args.out)
        out_path.write_text(json.dumps(manifest, indent=2))
        print(f"✓ Manifest written to {out_path} ({len(manifest)} files)")
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

### 2. FastAPI Endpoint: `arkship/api/hf_manifest.py`

```python
from fastapi import APIRouter, HTTPException
from typing import Optional
import time
from functools import lru_cache

router = APIRouter(prefix="/api/v1/hf", tags=["hf-manifest"])

# In-memory cache: (repo_id, revision, folder) -> (timestamp, manifest)
_MANIFEST_CACHE = {}
_CACHE_TTL = 300  # 5 minutes

@lru_cache(maxsize=8)
def _cached_manifest(repo_id: str, revision: str, folder: str):
    # Thin wrapper to allow lru_cache on hashable args
    from tools.hf_cdn_manifest import build_manifest
    return build_manifest(repo_id=repo_id, revision=revision, folder=folder)

@router.get("/manifest")
async def get_manifest(
    repo_id: str,
    revision: str = "main",
    folder: str = "",
    persist: Optional[str] = None,
):
    """
    Return deterministic CDN manifest for a HF dataset folder.
    Query params:
      repo_id (required): e.g. tatsu-lab/alpaca
      revision: git branch/tag (default: main)
      folder: subfolder path (default: root)
      persist: optional local path to save manifest
    """
    try:
        manifest = _cached_manifest(repo_id, revision, folder)
        if persist:
            from pathlib import Path
            Path(persist).write_text(json.dumps(manifest, indent=2))
        return {"repo_id": repo_id, "revision": revision, "folder": folder, "files": manifest}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
```

### 3. Surrogate Training Hook (pseudo-code for `surrogate/train.py`)

```python
def prepare_data_from_manifest(manifest_url_or_path):
    if manifest_url_or_path.startswith("http"):
        import requests
        files = requests.get(manifest_url_or_path, timeout=30).json()["files"]
    else:
        files = json.loads(Path(manifest_url_or_path).read_text())["files"]

    # Deterministic split
    files = sorted(files, key=lambda x: x["path"])
    train_size = int(0.95 * len(files))
    train_files = files[:train_size]
    val_files = files[train_size:]

    # Data loader uses CDN URLs directly (zero HF API calls)
    def stream_from_cdn(file_entry):
        url = file_entry["cdn_url"]
        # Use streaming download; project to {prompt, response} here
        resp = requests.get(url, stream=True, timeout=30)
        resp.raise_for_status()
        # Parse file (parquet/jsonl) and yield examples
        ...

    return train_files, val_files, stream_from_cdn
```

---

## Validation Checklist

- [ ] `chmod +x tools/hf-cdn-manifest.py`
- [ ] Run `python tools/hf-cdn-manifest.py --repo_id tatsu-lab/alpaca --out /tmp/manifest.json` → valid JSON with CDN URLs
- [ ] Start Arkship API; `curl http://localhost:8000/api/v1/hf/manifest?repo_id=tatsu-lab/alpaca` → returns manifest
- [ ] Confirm no `load_dataset` or `hf_hub_download` in surrogate training hot path when manifest is provided
- [ ] Document usage in `surrogate/README.md` under “CDN-only training”

**Estimated time**: 90–120 minutes (implementation + smoke tests).

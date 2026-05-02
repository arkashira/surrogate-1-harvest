# airship / frontend

## Final Synthesized Implementation

**Goal**: Deterministic, CDN-only file manifest generator for HuggingFace datasets that enables surrogate-1 training without HF API rate limits.

**Key Synthesis Decisions**:
- **HTTP Method**: Use **POST** (not GET) to avoid URL length limits with large file lists and align with REST semantics for resource generation
- **CDN Strategy**: Use `huggingface.co/datasets/{repo}/resolve/main/{path}` pattern — zero auth, no 429 rate limits
- **Determinism**: Sort files lexicographically, use manifest-level SHA256 for cache-busting
- **Single API Call**: `list_repo_tree(recursive=True)` → embeddable file list for Lightning Studio

---

### 1. Project Structure (`/opt/axentx/airship/`)
```
airship/
├── airship/
│   ├── __init__.py
│   ├── __main__.py          # CLI entrypoint
│   ├── main.py              # FastAPI app
│   ├── discover.py          # core manifest generator
│   └── config.py            # settings
├── pyproject.toml           # deps: fastapi, uvicorn, huggingface-hub, httpx, pydantic-settings
└── tests/
    └── test_discover.py
```

---

### 2. Core Implementation

#### `airship/config.py`
```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    hf_cdn_base: str = "https://huggingface.co/datasets"
    user_agent: str = "airship/1.0 (+https://axentx/airship)"
    timeout: int = 30
    
    class Config:
        env_prefix = "AIRSHIP_"

settings = Settings()
```

#### `airship/discover.py`
```python
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import httpx
from huggingface_hub import HfApi
from pydantic import BaseModel, Field

from .config import settings

class FileEntry(BaseModel):
    path: str
    size: int
    etag: Optional[str] = None
    url: str
    sha256: Optional[str] = None

class Manifest(BaseModel):
    repo_id: str
    date_folder: str
    files: List[FileEntry]
    generated_at: str
    sha256: str = Field(..., description="Manifest-level SHA256 for cache-busting")
    total_files: int
    total_size: int

def _cdn_url(repo_id: str, file_path: str) -> str:
    """Build CDN URL that bypasses HF API auth/rate limits."""
    encoded_repo = repo_id.replace("/", "%2F")
    encoded_path = "/".join(p.replace("/", "%2F") for p in file_path.split("/"))
    return f"{settings.hf_cdn_base}/{encoded_repo}/resolve/main/{encoded_path}"

def _calculate_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def generate_manifest(repo_id: str, date_folder: str) -> Manifest:
    """
    Generate deterministic CDN-only manifest for a HF dataset date folder.
    
    Uses single HF API call (list_repo_tree) to enumerate files, then
    constructs CDN URLs that training can fetch without auth.
    """
    api = HfApi()
    
    # Single API call - list files recursively in date_folder
    try:
        tree = list_repo_tree(
            repo_id=repo_id,
            path=date_folder,
            repo_type="dataset",
            recursive=True,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to list repo tree for {repo_id}/{date_folder}: {e}")
    
    files: List[FileEntry] = []
    
    for item in tree:
        if item.type != "file":
            continue
        
        file_path = item.path
        url = _cdn_url(repo_id, file_path)
        
        files.append(FileEntry(
            path=file_path,
            size=item.size or 0,
            etag=getattr(item, "oid", None),
            url=url,
            sha256=None,
        ))
    
    if not files:
        raise ValueError(f"No files found in {repo_id}/{date_folder}")
    
    # Sort for deterministic output
    files.sort(key=lambda f: f.path)
    
    total_size = sum(f.size for f in files)
    manifest_data = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "files": [f.model_dump() for f in files],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_files": len(files),
        "total_size": total_size,
    }
    
    # Calculate manifest-level hash for cache-busting
    manifest_json = json.dumps(manifest_data, sort_keys=True, separators=(",", ":"))
    manifest_hash = _calculate_sha256(manifest_json.encode())
    manifest_data["sha256"] = manifest_hash
    
    return Manifest(**manifest_data)

def save_manifest(manifest: Manifest, output_path: str | Path) -> None:
    """Save manifest to JSON file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(manifest.model_dump(), f, indent=2)
```

#### `airship/main.py`
```python
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .discover import generate_manifest, save_manifest

app = FastAPI(
    title="Arkship Discovery Service",
    description="CDN-only manifest generator for HuggingFace datasets",
)

class DiscoverRequest(BaseModel):
    repo_id: str
    date_folder: str
    output: str | None = None

@app.post("/discover")
async def discover_endpoint(request: DiscoverRequest):
    """
    Generate CDN-only manifest for HF dataset.
    
    POST body: {"repo_id": "...", "date_folder": "...", "output": "optional/path.json"}
    Returns JSON manifest for CDN-only training.
    """
    try:
        manifest = generate_manifest(request.repo_id, request.date_folder)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    
    if request.output:
        save_manifest(manifest, request.output)
    
    return JSONResponse(content=manifest.model_dump())

@app.get("/health")
async def health():
    return {"status": "ok", "service": "discovery"}
```

#### `airship/__main__.py`
```python
#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from airship.discover import generate_manifest, save_manifest

def main():
    parser = argparse.ArgumentParser(
        description="Arkship HF Dataset Discovery - CDN-only manifest generator"
    )
    parser.add_argument("repo_id", help="HF dataset repo ID (e.g., 'username/dataset')")
    parser.add_argument("date_folder", help="Date folder path (e.g., '2024-01-15')")
    parser.add_argument("-o", "--output", help="Output JSON file path", default=None)
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    
    args = parser.parse_args()
    
    try:
        manifest = generate_manifest(args.repo_id, args.date_folder)
        
        if args.output:
            save_manifest(manifest, args.output)
        else:
            if args.pretty:
                print(json.dumps(manifest.model_dump(), indent=2))
            else:
                print(json.dumps(manifest.model_dump()))
                
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

---

### 3. Usage Examples

**CLI**:
```bash
# Generate and save manifest
python -m airship discover HuggingFaceTB/cosmopedia 2024-01-

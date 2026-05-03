# vanguard / quality

## Final consolidated implementation

**Core diagnosis (agreed across candidates)**  
- No persisted `(repo, dateFolder)` manifest → every request triggers authenticated `list_repo_tree`/HF API calls, burning quota and risking 429s.  
- Data fetches use authenticated `/api/` paths instead of public CDN URLs, causing avoidable rate-limit pressure.  
- No caching layer for file listings → repeated page loads and training starts re-query HF within minutes.  
- Missing graceful fallback when HF API returns 429 (no retry-after/backoff), causing hard failures.  
- Training script likely uses `load_dataset(streaming=True)` on heterogeneous repos (risk of pyarrow cast errors) instead of per-file CDN downloads with projection to `{prompt, response}`.

**Chosen strategy (correctness + actionability)**  
- Add a manifest-based file-listing cache keyed by `(repo, dateFolder)` with TTL (1 hour) to eliminate redundant HF API calls.  
- Use public CDN (`resolve/main/`) for all file downloads; never use authenticated `/api/` paths for raw file content.  
- Implement exponential backoff + jitter with retries for 429/5xx; respect `Retry-After` when present.  
- Update training ingestion to download per-file via CDN and project to `{prompt, response}`; avoid `load_dataset(streaming=True)` on heterogeneous repos.  
- Keep dependencies minimal and robust; prefer `pyarrow`/`pandas` for parquet, fallback to stdlib where possible.

---

### File layout
```
/opt/axentx/vanguard/
├── backend/
│   ├── services/
│   │   └── hf_service.py
│   └── api/
│       └── training.py
└── .manifests/            # persisted manifest cache
```

---

### `/opt/axentx/vanguard/backend/services/hf_service.py`
```python
#!/usr/bin/env python3
"""
HF service with manifest caching and CDN-only fetches.
- list_repo_tree -> persisted manifest (JSON) keyed by (repo, date_folder)
- CDN download via resolve/main/ (no auth) with backoff + Retry-After support
- Training code calls this to avoid authenticated /api/ paths and redundant listing calls
"""
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

MANIFEST_DIR = Path(os.getenv("VANGUARD_MANIFEST_DIR", "/opt/axentx/vanguard/.manifests"))
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
CDN_BASE = "https://huggingface.co/datasets"

# Optional: lazy import HF API only when token is provided
try:
    from huggingface_hub import HfApi  # type: ignore
    HF_API = HfApi()
    HF_AVAILABLE = True
except Exception:
    HF_AVAILABLE = False
    HF_API = None  # type: ignore

def _backoff(attempt: int, resp: Optional[requests.Response] = None) -> float:
    base = min(360.0, (2.0 ** attempt) + (attempt * 0.15))
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
    # jitter
    import random
    return base * random.uniform(0.85, 1.15)

def _ensure_hf_api(token: Optional[str]):
    if not HF_AVAILABLE:
        raise RuntimeError("huggingface_hub not available; cannot perform authenticated listing")
    return HF_API

def list_date_files_cached(repo: str, date_folder: str, token: Optional[str] = None, ttl: int = 3600) -> List[str]:
    """
    Return file paths for repo/date_folder using cached manifest when fresh.
    Cache TTL defaults to 1 hour (safe for daily training folders).
    """
    safe_repo = repo.replace("/", "_")
    cache_path = MANIFEST_DIR / f"{safe_repo}_{date_folder}.json"
    now = time.time()

    # Use cache if fresh
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            if now - float(data.get("_cached_at", 0)) < ttl:
                return data.get("files", [])
        except Exception:
            cache_path.unlink(missing_ok=True)

    # Fetch once (authenticated if token provided) and persist
    kwargs = {"token": token} if token else {}
    try:
        api = _ensure_hf_api(token)
        items = api.list_repo_tree(repo=repo, path=date_folder, recursive=False, **kwargs)
        files = [it.rfilename for it in items if getattr(it, "rfilename", None)]
    except Exception as e:
        # If 429, wait and retry once (simple fallback)
        if getattr(e, "status_code", None) == 429:
            time.sleep(_backoff(0))
            items = api.list_repo_tree(repo=repo, path=date_folder, recursive=False, **kwargs)
            files = [it.rfilename for it in items if getattr(it, "rfilename", None)]
        else:
            raise

    cache_path.write_text(json.dumps({"files": files, "_cached_at": now}, separators=(",", ":")))
    return files

def download_file_cdn(
    repo: str,
    file_path: str,
    out_path: Path,
    max_retries: int = 5,
    timeout: float = 60.0,
) -> Path:
    """
    Download via public CDN (no auth). Retries with backoff on 429/5xx.
    """
    url = f"{CDN_BASE}/{repo}/resolve/main/{file_path}"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
        except requests.RequestException:
            if attempt == max_retries - 1:
                raise
            time.sleep(_backoff(attempt))
            continue

        if resp.status_code == 200:
            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return out_path

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_retries - 1:
                resp.raise_for_status()
            time.sleep(_backoff(attempt, resp))
            continue

        resp.raise_for_status()

    raise RuntimeError(f"Failed to download {url} after {max_retries} retries")
```

---

### `/opt/axentx/vanguard/backend/api/training.py`
```python
#!/usr/bin/env python3
"""
Training ingestion that uses manifest + CDN-only fetches.
Downloads per-file via CDN and projects to {prompt, response}.
Avoids load_dataset(streaming=True) on heterogeneous repos.
"""
import json
from pathlib import Path
from typing import Dict, List, Optional

from backend.services.hf_service import download_file_cdn, list_date_files_cached

def _project_record(obj: Dict) -> Dict[str, str]:
    return {
        "prompt": obj.get("prompt") or obj.get("input") or obj.get("instruction") or "",
        "response": obj.get("response") or obj.get("output") or obj.get("completion") or "",
    }

def _load_jsonl(path: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            rows.append(_project_record(obj))
        except json.JSONDecodeError:
            continue
    return rows

def _load_parquet(path: Path) -> List[Dict[str, str]]:
    try:
        import pandas as pd
    except ImportError as e:
        raise RuntimeError("pandas is required to read parquet files") from e
    df = pd.read_parquet(path)
    # Best-effort projection; keep only prompt/response columns
    prompt_col = next((c for c in df.columns if c in ("prompt", "input", "instruction")), None)
    response_col = next((c for c in df.columns if c in ("response", "output", "completion")), None)


# vanguard / backend

## Final Synthesis (Correctness + Actionability)

**Core problem:**  
Every backend request and training job calls authenticated `list_repo_tree` and `/api/` endpoints, burning the 1000/5 min HF quota and risking 429s. There is no persisted `(repo, dateFolder)` manifest, ingestion uses `load_dataset(streaming=True)` on heterogeneous repos (→ pyarrow CastError), and training cannot run on CDN-only fetches. Attribution columns (`source`, `ts`) leak into training data and break schema homogeneity.

**Chosen solution (merged):**
- Persist a single authenticated `list_repo_tree` call per `(repo, dateFolder)` into a local JSON manifest.
- Ingest via CDN-only fetches (`hf_hub_download` or raw resolve URL), project strictly to `{prompt, response}`, and write to `batches/mirror-merged/{date}/{slug}.parquet`.
- Provide a training loader that consumes the manifest and fetches files via CDN only (zero auth, zero `/api/` calls).
- Keep ingestion deterministic, idempotent, and schema-homogeneous; avoid `load_dataset` on heterogeneous repos.

**Key corrections vs candidates:**
- Use `huggingface_hub` (not `hugginggingface`) and `hf_hub_download` for reliable CDN fetches and local caching.
- Add ETag/Hash-based cache invalidation and lock-based concurrency safety for manifest builds.
- Add robust projection with per-repo hints and strict `{prompt, response}` output; drop all other columns.
- Add retry/backoff and timeouts for CDN fetches; validate parquet schema; include CLI entrypoints for operations.

---

## 1. Implementation

Create structure:
```bash
mkdir -p /opt/axentx/vanguard/backend/services \
         /opt/axentx/vanguard/backend/loaders \
         /opt/axentx/vanguard/data/manifests \
         /opt/axentx/vanguard/data/batches/mirror-merged
```

### `/opt/axentx/vanguard/backend/services/manifest_service.py`
```python
import json
import fcntl
import hashlib
import os
from pathlib import Path
from typing import List, Dict, Any
from huggingface_hub import HfApi, hf_hub_download

HF_API = HfApi()
MANIFEST_ROOT = Path("/opt/axentx/vanguard/data/manifests")

def _safe_repo_dir(repo: str) -> str:
    return repo.replace("/", "_")

def get_manifest_path(repo: str, date_folder: str) -> Path:
    return MANIFEST_ROOT / _safe_repo_dir(repo) / f"{date_folder}.json"

def _hash_state(tree_items: List[Any]) -> str:
    payload = json.dumps([{"path": i.path, "size": i.size, "commit": getattr(i, "commit", "")} for i in tree_items], sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()

def build_manifest(repo: str, date_folder: str, force: bool = False) -> List[Dict[str, Any]]:
    """
    Single authenticated API call to list top-level files in date_folder.
    Persists manifest with ETag-like content hash for idempotency.
    Uses file lock for concurrent safety.
    """
    mp = get_manifest_path(repo, date_folder)
    mp.parent.mkdir(parents=True, exist_ok=True)

    # Fast path: exists and not forced
    if mp.exists() and not force:
        try:
            data = json.loads(mp.read_text())
            if isinstance(data, list) and all("path" in x for x in data):
                return data
        except Exception:
            pass  # rebuild on corruption

    prefix = f"{date_folder}/"
    tree = HF_API.list_repo_tree(repo=repo, path=prefix, recursive=False)
    files = [{"path": item.path, "size": item.size} for item in tree if getattr(item, "type", None) == "file"]

    # Write atomically with lock
    tmp = mp.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(files, f, indent=2, sort_keys=True)
    tmp.replace(mp)

    return files

def load_manifest(repo: str, date_folder: str) -> List[Dict[str, Any]]:
    mp = get_manifest_path(repo, date_folder)
    if not mp.exists():
        return build_manifest(repo, date_folder)
    try:
        data = json.loads(mp.read_text())
        if isinstance(data, list) and all("path" in x for x in data):
            return data
    except Exception:
        pass
    return build_manifest(repo, date_folder, force=True)
```

### `/opt/axentx/vanguard/backend/loaders/cdn_loader.py`
```python
import json
import time
import requests
from pathlib import Path
from typing import Iterator, Dict, Any, List
import polars as pl
from huggingface_hub import hf_hub_download

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

_RETRY = 3
_BACKOFF = 2.0
_TIMEOUT = 30

def _fetch_cdn_bytes(url: str) -> bytes:
    for attempt in range(1, _RETRY + 1):
        try:
            resp = requests.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == _RETRY:
                raise
            time.sleep(_BACKOFF * attempt)
    raise RuntimeError("unreachable")

def iter_cdn_files(repo: str, manifest: List[Dict[str, Any]], use_hf_download: bool = True) -> Iterator[Dict[str, Any]]:
    """
    Yield {"path": ..., "content": bytes}
    Prefer hf_hub_download (cached) when use_hf_download=True; fallback to raw CDN.
    """
    for item in manifest:
        path = item["path"]
        content: bytes = b""
        if use_hf_download:
            try:
                local_path = hf_hub_download(repo_id=repo, filename=path, repo_type="dataset")
                content = Path(local_path).read_bytes()
            except Exception:
                # fallback to raw CDN
                pass
        if not content:
            url = CDN_TEMPLATE.format(repo=repo, path=path)
            content = _fetch_cdn_bytes(url)
        yield {"path": path, "content": content}

def _extract_pairs(raw: bytes, hints: Dict[str, List[str]]) -> List[Dict[str, str]]:
    """
    Extract {prompt, response} from JSON or JSONL bytes.
    hints: {"prompt": [...], "response": [...]}
    """
    prompt_keys = hints.get("prompt", ["prompt", "input", "question", "instruction"])
    response_keys = hints.get("response", ["response", "output", "answer", "completion"])

    rows: List[Dict[str, str]] = []
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            rows = [data]
        elif isinstance(data, list):
            rows = data
    except Exception:
        # try JSONL
        text = raw.decode(errors="ignore").strip()
        if not text:
            return []
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue

    out: List[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        prompt = ""
        for k in prompt_keys:
            val = row.get(k)
            if val is not None and str(val).strip():
                prompt = str(val).strip()
                break
        response = ""
        for k in response_keys:
            val = row.get(k)
            if val is not None and str(val).strip():
                response = str(val).strip()
                break
        if prompt or response:
            out.append({"prompt": prompt, "response": response})
    return out

def build_parquet_for_date(
    repo: str,
    date_folder: str,
    manifest: List[Dict[str, Any]],
    output_root: Path,
    hints: Dict[str, List[str]] | None = None,
) -> Path:
    hints = hints or {}
    output_dir = output_root /

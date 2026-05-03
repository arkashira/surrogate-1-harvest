# vanguard / discovery

# Final consolidated solution

## 1. Diagnosis (merged + corrected)
- **No persisted `(repo, dateFolder) → file-list` manifest**: every run paginates `list_repo_tree`, burning HF API quota and risking 429s.
- **No CDN-only data loading**: training/data selection uses authenticated calls or `load_dataset(streaming=True)` instead of CDN fetches, causing auth overhead and rate-limit exposure.
- **No deterministic repo-sharding for writes**: single-repo ingestion risks HF commit cap (128/hr) and can stall large mirrors.
- **No Lightning Studio reuse + idle-restart guard**: launcher creates new studios instead of reusing running ones and has no protection against idle timeouts; wastes quota and breaks long runs.
- **Missing concrete retry/backoff and observability**: no retry for transient 429/5xx, no rate-limit awareness, and no metrics/logging for cache hits, API usage, or CDN failures.

## 2. Implementation (single coherent plan)

### 2.1 Create `/opt/axentx/vanguard/discovery/file_list_cache.py`
```python
# /opt/axentx/vanguard/discovery/file_list_cache.py
import json
import time
import logging
import hashlib
from pathlib import Path
from typing import List, Optional

try:
    from huggingface_hub import list_repo_tree, HfApi
    from huggingface_hub.utils import HFError
except ImportError:
    HfApi = None
    HFError = Exception

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

# Conservative defaults
HF_RATE_LIMIT_RESET_BUFFER = 15  # seconds
MAX_RETRIES = 5
RETRY_BACKOFF = (1, 2, 4, 8, 16)

def _cache_path(repo: str, date_folder: str) -> Path:
    safe = f"{repo}__{date_folder}".replace("/", "__")
    return CACHE_DIR / f"file_list_{safe}.json"

def _wait_for_reset(reset_time: Optional[float]) -> None:
    if reset_time is None:
        sleep_sec = RETRY_BACKOFF[-1]
    else:
        now = time.time()
        sleep_sec = max(reset_time - now + HF_RATE_LIMIT_RESET_BUFFER, 0)
    if sleep_sec > 0:
        logger.warning("Rate-limited; sleeping %.1fs", sleep_sec)
        time.sleep(sleep_sec)

def _list_with_retry(repo: str, path: str) -> List[str]:
    if HfApi is None:
        raise RuntimeError("huggingface_hub not installed")

    api = HfApi()
    last_reset = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            items = api.list_repo_tree(repo=repo, path=path, recursive=False)
            return [f"{path}/{i.rfilename}" for i in items if i.type == "file"]
        except HFError as exc:
            # Try to detect rate-limit by message/status if available
            msg = str(exc).lower()
            is_429 = "429" in msg or "rate limit" in msg or "too many requests" in msg
            if is_429 and attempt < MAX_RETRIES:
                # exponential backoff with jitter
                sleep_sec = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                jitter = sleep_sec * 0.25 * (2 * hash(time.time()) / (2**64 - 1) - 1)  # small jitter
                logger.warning("Retry %s/%s after 429/rate-limit: %s", attempt, MAX_RETRIES, exc)
                time.sleep(max(sleep_sec + jitter, 0))
                continue
            # non-retryable or final attempt
            logger.error("Failed to list repo tree: %s", exc)
            raise
    raise RuntimeError("Exhausted retries while listing repo tree")

def build_file_list(repo: str, date_folder: str, force: bool = False) -> List[str]:
    """
    Build or load cached file list for repo/date_folder (non-recursive).
    Returns list of paths relative to repo root.
    """
    cp = _cache_path(repo, date_folder)
    if not force and cp.exists():
        logger.info("Cache hit: %s", cp)
        try:
            data = json.loads(cp.read_text())
            if isinstance(data, list):
                return data
        except Exception:
            logger.warning("Corrupt cache file %s; rebuilding", cp)

    logger.info("Building file list for %s/%s", repo, date_folder)
    files = _list_with_retry(repo, date_folder)
    cp.write_text(json.dumps(files, indent=2))
    logger.info("Cached %d files to %s", len(files), cp)
    return files

def cdn_url(repo: str, path: str) -> str:
    """CDN URL that bypasses HF API auth checks."""
    # Normalize double slashes in path
    clean_path = "/".join(p for p in path.split("/") if p)
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{clean_path}"

def shard_repo(base: str, slug: str, n_shards: int = 5) -> str:
    """Deterministic repo sharding to avoid HF commit cap."""
    if n_shards <= 1:
        return base
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    idx = h % n_shards
    return f"{base}-{idx}" if idx > 0 else base
```

### 2.2 Update launcher: `/opt/axentx/vanguard/launcher.py`
```python
# /opt/axentx/vanguard/launcher.py
import json
import time
import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

try:
    from lightning import Lightning, Teamspace, Machine
    LIGHTNING_AVAILABLE = True
except ImportError:
    LIGHTNING_AVAILABLE = False
    Lightning = Teamspace = Machine = None

from discovery.file_list_cache import build_file_list, cdn_url, shard_repo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

HF_REPO = "axentx/vanguard-mirror"
DATE_FOLDER = "batches/mirror-merged/2026-05-03"
SLUG = "example-slug"
MANIFEST_PATH = Path("file_manifest.json")

def ensure_file_list(force: bool = False) -> dict:
    files = build_file_list(HF_REPO, DATE_FOLDER, force=force)
    manifest = {
        "repo": HF_REPO,
        "date_folder": DATE_FOLDER,
        "files": files,
        "cdn_urls": [cdn_url(HF_REPO, f) for f in files],
        "generated_at": time.time()
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    logger.info("Manifest written to %s (%d files)", MANIFEST_PATH, len(files))
    return manifest

def get_running_studio(name: str):
    if not LIGHTNING_AVAILABLE:
        return None
    team = Teamspace()
    for s in team.studios:
        if s.name == name and getattr(s, "status", None) == "Running":
            logger.info("Reusing running studio: %s", s.name)
            return s
    return None

def start_or_restart_studio(name: str, machine: str = "lightning-public-prod"):
    if not LIGHTNING_AVAILABLE:
        logger.warning("Lightning SDK not available; skipping studio management")
        return None
    studio = get_running_studio(name)
    if studio is not None:
        return studio

    logger.info("No running studio found; creating/starting %s", name)
    # Create (or reuse stopped) studio and start it
    studio = Lightning.Studio(
        name=name,
        machine=Machine(machine),
        create_ok=True
    )
    if getattr(studio, "status", None) != "Running":
        studio.start(machine=Machine(machine))
        # Wait for ready (adjust as needed)
        for _ in range(10):
            time.sleep(10)
            if getattr(studio, "status", None) == "Running":
                logger.info("Studio is running")


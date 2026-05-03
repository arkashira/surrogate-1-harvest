# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Replace `bin/dataset-enrich.sh` with a manifest-driven, CDN-bypass ingestion worker (`bin/dataset-enrich.py`) that eliminates HF API rate limits during training.**

### Core Architecture
- **Manifest-first**: single `list_repo_tree` call per date → `file-list.json`
- **CDN downloads**: `https://huggingface.co/datasets/{repo}/resolve/main/{path}` (no auth) to bypass `/api/` rate limits
- **Deterministic sharding**: `slug_hash(path) % SHARD_TOTAL == SHARD_ID`
- **Schema-robust projection**: per-format handlers → `{prompt, response}`
- **Central dedup**: MD5 store (`lib/dedup.py`) with LRU cache
- **Atomic commits**: one JSONL per shard → `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`

### Environment
```bash
SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
HF_TOKEN=hf_xxx \
REPO_ID=axentx/surrogate-1-training-pairs \
python bin/dataset-enrich.py
```

---

### bin/dataset-enrich.py
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker.

Deterministic sharding + CDN downloads + schema projection + dedup.
"""

import os
import sys
import json
import hashlib
import time
import logging
import gzip
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from collections import OrderedDict

import requests
from huggingface_hub import HfApi

# ---- Configuration ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("surrogate-ingest")

SHARD_ID = int(os.environ.get("SHARD_ID", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "16"))
DATE = os.environ.get("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.environ.get("HF_TOKEN")
REPO_ID = os.environ.get("REPO_ID", "axentx/surrogate-1-training-pairs")

if not HF_TOKEN:
    log.error("HF_TOKEN is required")
    sys.exit(1)

api = HfApi(token=HF_TOKEN)

CDN_BASE = "https://huggingface.co/datasets"
MAX_RETRIES = 5
RETRY_BACKOFF = 2
RATE_LIMIT_WAIT = 360

# ---- Dedup (LRU) ----
class DedupStore:
    def __init__(self, path: Path, max_size: int = 1_000_000):
        self.path = path
        self.max_size = max_size
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: "OrderedDict[str, bool]" = OrderedDict()
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open() as f:
                    for line in f:
                        h = line.strip()
                        if h:
                            self._cache[h] = True
                log.info("Loaded %d existing hashes from %s", len(self._cache), self.path)
            except Exception as exc:
                log.warning("Could not load dedup store: %s", exc)

    def flush(self) -> None:
        if not self._cache:
            return
        try:
            with self.path.open("a") as f:
                for h in self._cache:
                    f.write(h + "\n")
            self._cache.clear()
        except Exception as exc:
            log.warning("Could not flush dedup store: %s", exc)

    def exists(self, digest: str) -> bool:
        return digest in self._cache

    def add(self, digest: str) -> None:
        self._cache[digest] = True
        self._cache.move_to_end(digest)
        if len(self._cache) > self.max_size:
            self._cache.popitem(last=False)


dedup = DedupStore(Path("lib/dedup/hashes.txt"))

# ---- Utilities ----
def slug_hash(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest(), 16)


def in_shard(slug: str) -> bool:
    return slug_hash(slug) % SHARD_TOTAL == SHARD_ID


def list_files(date: str) -> List[str]:
    """List candidate source files for ingestion."""
    # Primary: raw/{date}/ on repo
    for prefix in (f"raw/{date}", f"batches/public-merged/{date}"):
        try:
            tree = api.list_repo_tree(repo_id=REPO_ID, path=prefix, repo_type="dataset", recursive=True)
            paths = []
            for item in tree:
                p = item.path if hasattr(item, "path") else (item["path"] if isinstance(item, dict) else str(item))
                if p.lower().endswith((".jsonl", ".json", ".parquet")):
                    paths.append(p)
            if paths:
                log.info("Discovered %d files under %s", len(paths), prefix)
                return paths
        except Exception:
            continue
    log.warning("No source files found for date=%s", date)
    return []


def save_manifest(manifest_path: Path, files: List[str]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w") as f:
        json.dump(files, f)
    log.info("Saved manifest: %s (%d files)", manifest_path, len(files))


def load_manifest(manifest_path: Path) -> List[str]:
    if manifest_path.exists():
        with manifest_path.open() as f:
            return json.load(f)
    return []


def cdn_download(url: str, dest: Path, max_retries: int = MAX_RETRIES) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    backoff = RETRY_BACKOFF
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=60, stream=True)
            if resp.status_code == 429:
                log.warning("CDN 429, waiting %ds", RATE_LIMIT_WAIT)
                time.sleep(RATE_LIMIT_WAIT)
                continue
            resp.raise_for_status()
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception as exc:
            log.warning("Download attempt %d/%d failed: %s", attempt, max_retries, exc)
            if attempt == max_retries:
                return False
            time.sleep(backoff)
            backoff *= 2
    return False


def read_jsonl_lines(path: Path) -> List[Dict[str, Any]]:
    lines = []
    opener = gzip.open if path.suffix == ".gz" else path.open
    mode = "rt" if path.suffix == ".gz" else "r"
    try:
        with opener(path, mode, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(json.loads(line))
    except Exception as exc:
        log.warning("Could not read %s as JSONL: %s", path, exc)
    return lines


def read_json(path: Path) -> List[Dict[str, Any]]:
    try:
        with path.open() as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return [data]
    except Exception as exc:
        log.warning("Could not read %s as JSON: %s", path, exc)
        return []


# ---- Projection handlers ----
def project_generic(record: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Best-effort projection for unknown schemas."""
    prompt_keys = {"prompt", "instruction", "input", "question", "user", "query"}
    response_keys = {"response", "completion", "output", "answer", "assistant", "system"}


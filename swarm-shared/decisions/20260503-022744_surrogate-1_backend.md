# surrogate-1 / backend

Below is the single, merged implementation that keeps every high-leverage idea from both proposals, removes duplication, and resolves conflicts in favor of **correctness + concrete actionability**.

Key merged decisions
- Keep manifest-driven, CDN-bypass architecture (both proposals agree).
- Keep **single** `list_repo_tree` call per DATE and embed/save `manifest.json` locally; optionally upload once to the repo (Candidate 2) but default to local-only to avoid commit-cap churn (Candidate 1).
- Deterministic shard assignment by content hash (slug/file) (Candidate 2) so workers never double-process.
- Sibling repo hashing for writes to dodge HF 128 commit/hr/repo cap (Candidate 1).
- Schema projection to `{prompt, response}` only; safe handling for parquet/json/jsonl; avoid `pyarrow.CastError` (both).
- Central dedup via `lib/dedup.py` (both).
- Retry/backoff for CDN 429/5xx; no auth headers on CDN (both).
- GitHub Actions matrix passes `SHARD_ID`, `SHARD_TOTAL`, `DATE`, `HF_TOKEN` (Candidate 1).

Conflict resolutions
- Manifest: save locally by default; upload only if requested (env `UPLOAD_MANIFEST=true`) to avoid burning commit quota.
- Sharding: use deterministic content hash (Candidate 2) instead of modulo-round-robin at worker start; prevents overlap and survives worker count changes.
- Parquet: project columns at read time; do not rely on `load_dataset` streaming for mixed schemas (Candidate 1).
- Dedup key: use `md5(content)` (bytes) rather than path-based to catch cross-file dupes (Candidate 1).
- CDN retries: exponential backoff + jitter (both); cap attempts at 5.

---

## Implementation Plan (≤2h)

### Steps (1h 45m)
1. **Backup old script** (2m)  
   `mv bin/dataset-enrich.sh bin/dataset-enrich.sh.bak`

2. **Write `bin/dataset-enrich.py`** (60m)  
   - Manifest generation + CDN download  
   - Deterministic shard assignment by content hash  
   - Schema projection + dedup  
   - Sibling repo hashing for commit cap  
   - Local manifest by default; optional repo upload

3. **Update GitHub Actions matrix** (10m)  
   - Pass `SHARD_ID`, `SHARD_TOTAL`, `DATE`, `HF_TOKEN`, optional `UPLOAD_MANIFEST`, `SIBLING_REPOS`

4. **Test locally** (20m)  
   - Dry-run with small sample and mocked HF_TOKEN

5. **Commit & push** (13m)

---

## Code: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Deterministic sharding by content hash, schema projection to {prompt, response},
central dedup, sibling repo writes to dodge HF 128/hr/repo cap.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Env:
  SHARD_ID            - worker index
  SHARD_TOTAL         - total shards (default 16)
  DATE               - date folder in repo (default today UTC)
  HF_TOKEN           - required
  REPO_ID            - default axentx/surrogate-1-training-pairs
  UPLOAD_MANIFEST    - if "1" or "true", upload manifest to repo (costs commits)
  SIBLING_REPOS      - number of sibling repos for write spread (default 5)
  OUTPUT_BASE        - default batches/public-merged
"""

from __future__ import annotations

import os
import sys
import json
import hashlib
import time
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download, upload_file

# ---- config ----
REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN")
UPLOAD_MANIFEST = str(os.getenv("UPLOAD_MANIFEST", "")).lower() in ("1", "true", "yes")
SIBLING_REPOS = max(1, int(os.getenv("SIBLING_REPOS", "5")))
OUTPUT_BASE = Path(os.getenv("OUTPUT_BASE", "batches/public-merged"))

if not HF_TOKEN:
    print("ERROR: HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

api = HfApi(token=HF_TOKEN)
cdn_session = requests.Session()

# ---- dedup ----
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from lib.dedup import DedupStore
except Exception as e:
    print(f"ERROR: cannot import lib.dedup: {e}", file=sys.stderr)
    sys.exit(1)

dedup = DedupStore()

# ---- paths ----
OUTPUT_DIR = OUTPUT_BASE / DATE
MANIFEST_LOCAL = Path("manifest") / DATE / "files.json"

# ---- helpers ----
def deterministic_shard(key: str, total: int) -> int:
    return int(hashlib.md5(key.encode()).hexdigest(), 16) % total

def sibling_repo_for(key: str) -> str:
    """Spread writes across sibling repos to avoid HF 128/hr/repo cap."""
    if SIBLING_REPOS <= 1:
        return REPO_ID
    idx = deterministic_shard(key, SIBLING_REPOS)
    if idx == 0:
        return REPO_ID
    return f"{REPO_ID}-s{idx}"

def list_date_folder() -> List[Dict[str, Any]]:
    """Single API call: list files in DATE folder (non-recursive)."""
    try:
        tree = api.list_repo_tree(
            repo_id=REPO_ID,
            path=DATE,
            recursive=False,
            token=HF_TOKEN,
        )
        return [t for t in tree if getattr(t, "type", None) == "file"]
    except Exception as e:
        print(f"ERROR listing repo tree: {e}", file=sys.stderr)
        sys.exit(1)

def build_manifest() -> List[Dict[str, Any]]:
    """Create or load manifest for DATE."""
    if MANIFEST_LOCAL.exists():
        return json.loads(MANIFEST_LOCAL.read_text())

    files = list_date_folder()
    manifest = [
        {
            "path": getattr(f, "rfilename", getattr(f, "path", "")),
            "size": getattr(f, "size", 0),
            "lfs": bool(getattr(f, "lfs", None)),
        }
        for f in files
    ]
    MANIFEST_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_LOCAL.write_text(json.dumps(manifest, indent=2))

    if UPLOAD_MANIFEST:
        try:
            upload_file(
                path_or_fileobj=str(MANIFEST_LOCAL),
                path_in_repo=f"manifest/{DATE}/files.json",
                repo_id=REPO_ID,
                token=HF_TOKEN,
                commit_message=f"manifest: {DATE}",
            )
        except Exception as e:
            print(f"WARN: failed to upload manifest: {e}", file=sys.stderr)

    return manifest

def cdn_download(repo_id: str, path: str) -> Optional[bytes]:
    """Download via CDN (no auth) with retry/backoff."""
    url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"
    for attempt in range(5):
        try:
            resp = cdn_session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.content
            if resp.status_code == 404:
                print(f"WARN: 404 for {url}", file=sys.stderr)
                return None
            wait = (2 ** attempt) + random.uniform(0, 1)
            print(f"WARN: {

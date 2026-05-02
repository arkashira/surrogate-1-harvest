# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate limits during ingestion and training by using `https://huggingface.co/datasets/{repo}/resolve/main/{path}` CDN URLs instead of authenticated `/api/` calls.

### Steps (1h 45m total)

1. **Create `bin/snapshot.sh`** (20m) — deterministic file listing for one date folder, outputs `snapshot-<date>.json` with CDN URLs and metadata.
2. **Add `lib/snapshot.py`** (25m) — loads snapshot JSON, yields local file paths, and provides CDN URL resolution.
3. **Add `lib/cdn_stream.py`** (20m) — lightweight CDN streamer with retry/backoff and integrity check.
4. **Update `bin/dataset-enrich.sh`** (30m) — accept snapshot path, skip recursive listing, stream from CDN URLs with fallback.
5. **Update workflow `ingest.yml`** (20m) — add snapshot step before matrix, pass `SNAPSHOT_FILE` to shards.
6. **Add training helper `bin/train_filelist.py`** (20m) — generate filelist JSON for Lightning training (CDN-only).
7. **Smoke test** (10m) — run snapshot + one shard locally.

---

### 1. `bin/snapshot.sh`

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Generate a snapshot of dataset files for CDN-only ingestion.
# Usage:
#   HF_TOKEN=hf_xxx SNAPSHOT_OUT=snapshots/snapshot-2026-05-02.json \
#     REPO=axentx/surrogate-1-training-pairs \
#     DATE=2026-05-02 \
#     ./bin/snapshot.sh

set -euo pipefail

: "${HF_TOKEN:?required}"
: "${SNAPSHOT_OUT:=snapshot.json}"
: "${REPO:=axentx/surrogate-1-training-pairs}"
: "${DATE:=$(date +%Y-%m-%d)}"

# Ensure snapshots directory exists
mkdir -p "$(dirname "$SNAPSHOT_OUT")"

# Use huggingface_hub to list files non-recursively for the date folder.
python3 - "$REPO" "$DATE" "$SNAPSHOT_OUT" <<'PY'
import os, json, sys
from datetime import datetime, timezone
from huggingface_hub import HfApi

repo_id, date_folder, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
api = HfApi(token=os.environ["HF_TOKEN"])

# List only top-level entries in the date folder (avoids recursive pagination).
entries = api.list_repo_tree(repo_id, path=date_folder, recursive=False)

files = []
for e in entries:
    if getattr(e, "type", None) == "file":
        path = e.path
        # CDN URL (no auth, bypasses /api/ rate limits)
        cdn_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"
        files.append({
            "path": path,
            "cdn_url": cdn_url,
            "size": getattr(e, "size", None),
            "lfs": getattr(e, "lfs", None) is not None,
        })

snapshot = {
    "repo_id": repo_id,
    "date": date_folder,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "files": files,
    "count": len(files),
}

os.makedirs(os.path.dirname(out_path) if os.path.dirname(out_path) else ".", exist_ok=True)
with open(out_path, "w") as f:
    json.dump(snapshot, f, indent=2)

print(f"Snapshot written: {out_path} ({len(files)} files)")
PY
```

Make executable:

```bash
chmod +x bin/snapshot.sh
```

---

### 2. `lib/snapshot.py`

```python
# lib/snapshot.py
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Iterator

def load_snapshot(snapshot_path: str) -> Dict:
    """Load snapshot JSON from file."""
    with open(snapshot_path) as f:
        return json.load(f)

def iter_snapshot_files(snapshot_path: str) -> Iterator[Dict]:
    """Yield file metadata dicts from snapshot."""
    snapshot = load_snapshot(snapshot_path)
    for fmeta in snapshot.get("files", []):
        yield fmeta

def get_cdn_urls(snapshot_path: str) -> List[str]:
    """Return list of CDN URLs from snapshot."""
    return [f["cdn_url"] for f in iter_snapshot_files(snapshot_path)]

def snapshot_dir() -> Path:
    """Default snapshots directory."""
    return Path("snapshots")

def latest_snapshot(date: Optional[str] = None) -> Optional[Path]:
    """Find latest snapshot file, optionally filtered by date."""
    snap_dir = snapshot_dir()
    if not snap_dir.exists():
        return None
    candidates = list(snap_dir.glob("snapshot-*.json"))
    if date:
        candidates = [p for p in candidates if date in p.name]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
```

---

### 3. `lib/cdn_stream.py`

```python
# lib/cdn_stream.py
import io
import time
import requests
from typing import BinaryIO, Optional

def cdn_stream(cdn_url: str, max_retries: int = 5, timeout: int = 30) -> BinaryIO:
    """
    Stream a dataset file from HF CDN with exponential backoff.
    Uses no Authorization header — CDN tier has separate (higher) rate limits.
    """
    headers = {}
    for attempt in range(max_retries):
        try:
            resp = requests.get(cdn_url, headers=headers, timeout=timeout, stream=True)
            resp.raise_for_status()
            # Wrap raw bytes into file-like object
            raw = io.BytesIO()
            for chunk in resp.iter_content(chunk_size=8192):
                raw.write(chunk)
            raw.seek(0)
            return raw
        except requests.HTTPError as e:
            if resp.status_code == 429:
                wait = 360 if attempt == 0 else (2 ** attempt) * 5
                time.sleep(wait)
                continue
            raise
        except (requests.RequestException, OSError) as e:
            if attempt == max_retries - 1:
                raise
            time.sleep((2 ** attempt) * 2)
    raise RuntimeError("Exhausted retries")
```

---

### 4. Updated `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Enrich dataset with embeddings; supports snapshot-based CDN ingestion.
# Usage:
#   SNAPSHOT_FILE=snapshots/snapshot-2026-05-02.json ./bin/dataset-enrich.sh

set -euo pipefail

: "${HF_TOKEN:?required}"
: "${SHARD_ID:=0}"
: "${SNAPSHOT_FILE:=}"

# If SNAPSHOT_FILE is provided, use it; otherwise fall back to listing.
if [ -n "${SNAPSHOT_FILE}" ] && [ -f "${SNAPSHOT_FILE}" ]; then
  echo "Using snapshot: ${SNAPSHOT_FILE}"
  export USE_SNAPSHOT=1
else
  export USE_SNAPSHOT=0
fi

# Existing enrichment logic continues below...
# Example Python integration (inline or via helper):
python3 - "$SHARD_ID" <<'PY'
import os, json, sys
from pathlib import Path

shard_id = int(sys.argv[1])
use_snapshot = os.environ.get("USE_SNAPSHOT") == "1"
snapshot_file = os.environ.get("SNAPSHOT_FILE", "")

if use_snapshot and snapshot_file and Path(snapshot_file).exists():
    # CDN-based ingestion path
    from lib.cdn_stream import cdn_stream
    from lib.snapshot import iter_snapshot_files

    # Optionally shard across files by modulo on index
    files = list(iter_snapshot_files(snapshot_file))
    shard_files = [f for i, f in enumerate(files) if i % 16 == shard_id] 

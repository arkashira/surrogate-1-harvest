# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value change**: Add `bin/snapshot.sh` that produces a deterministic file manifest per date folder and update ingestion/training to use CDN URLs exclusively when a snapshot is provided. This eliminates HuggingFace API calls during training (prevents 429s) and aligns with the HF CDN bypass pattern.

### Steps (1h 30m total)
1. **Create `bin/snapshot.sh`** (20m) — single API call to `list_repo_tree` for a date folder, emit `snapshot.json` with CDN URLs and metadata. Deterministic sort for reproducibility.
2. **Create `bin/cdn_loader.py`** (25m) — lightweight loader that reads `snapshot.json`, streams files via CDN (`resolve/main/...`) with `requests`/`urllib`, projects to `{prompt, response}` on parse. Zero HF API calls during data load.
3. **Update `bin/dataset-enrich.sh`** (15m) — accept optional `SNAPSHOT_PATH`; if provided, use `cdn_loader.py` instead of `datasets.load_dataset`. Keep existing schema normalization and dedup flow.
4. **Update training launcher** (20m) — add snapshot generation step in orchestration (Mac) before Lightning run; embed snapshot path in Lightning `run()` args/environment so training script uses CDN-only mode.
5. **Add safety + retry** (10m) — CDN download retries with backoff, timeout, and integrity check (size/md5 if available).

---

## Code Snippets

### 1. `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Generate deterministic snapshot.json for a date folder in axentx/surrogate-1-training-pairs
# Usage: SNAPSHOT_DATE=2026-04-29 ./bin/snapshot.sh > snapshot.json

set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
DATE="${SNAPSHOT_DATE:-$(date +%Y-%m-%d)}"
HF_TOKEN="${HF_TOKEN:-}"

# Single API call: list top-level objects in the date folder (non-recursive by default)
# We'll recurse client-side only for that date subtree to avoid 100x pagination.
# Use huggingface_hub via python for reliable tree listing.
python3 - "$REPO" "$DATE" "$HF_TOKEN" <<'PY'
import os
import json
import sys
from huggingface_hub import HfApi

repo = sys.argv[1]
date = sys.argv[2]
token = sys.argv[3] or None

api = HfApi(token=token)
# recursive=True limited to the date subtree only
items = api.list_repo_tree(repo=repo, path=date, recursive=True)

snapshot = {
    "repo": repo,
    "date": date,
    "generated_at_utc": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    "files": []
}

for item in sorted(items, key=lambda x: x.path):
    if item.type != "file":
        continue
    # CDN URL (no auth, bypasses API rate limits)
    cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{item.path}"
    snapshot["files"].append({
        "path": item.path,
        "cdn_url": cdn_url,
        "size": getattr(item, "size", None),
        "lfs": getattr(item, "lfs", None) is not None
    })

json.dump(snapshot, sys.stdout, indent=2)
PY
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

### 2. `bin/cdn_loader.py`
```python
#!/usr/bin/env python3
# bin/cdn_loader.py
# Stream files from snapshot.json via CDN and yield {prompt, response, source}
# Zero HuggingFace API calls during data loading.

import json
import sys
import time
import hashlib
from pathlib import Path
from typing import Iterator, Dict, Any

import requests
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.csv as pcsv

CDN_TIMEOUT = 30
MAX_RETRIES = 5
BACKOFF_FACTOR = 1.5


def robust_get(url: str) -> bytes:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=CDN_TIMEOUT, stream=True)
            resp.raise_for_status()
            content = b"".join(resp.iter_content(chunk_size=8192))
            return content
        except Exception as exc:
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts") from exc
            sleep = (BACKOFF_FACTOR ** (attempt - 1)) * 2
            print(f"[cdn_loader] retry {attempt}/{MAX_RETRIES} for {url} in {sleep}s: {exc}", file=sys.stderr)
            time.sleep(sleep)
    raise RuntimeError("unreachable")


def project_to_pair(obj: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Normalize heterogeneous schemas to {prompt, response} + source."""
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
    response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response), "source": source}


def stream_snapshot(snapshot_path: str) -> Iterator[Dict[str, Any]]:
    snapshot = json.loads(Path(snapshot_path).read_text())
    for fspec in snapshot["files"]:
        url = fspec["cdn_url"]
        path = fspec["path"]
        source = path  # keep attribution in filename per pattern
        try:
            raw = robust_get(url)
        except Exception as exc:
            print(f"[cdn_loader] skip {path}: {exc}", file=sys.stderr)
            continue

        # Try parquet first
        if path.endswith(".parquet"):
            try:
                table = pq.read_table(pa.BufferReader(raw))
                for batch in table.to_batches(max_chunksize=8192):
                    for row in batch.to_pylist():
                        yield project_to_pair(row, source)
                continue
            except Exception:
                pass

        # Try JSONL
        if path.endswith(".jsonl"):
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    yield project_to_pair(obj, source)
                except Exception:
                    continue
            continue

        # Try CSV
        if path.endswith(".csv"):
            try:
                table = pcsv.read_csv(pa.BufferReader(raw))
                for batch in table.to_batches(max_chunksize=8192):
                    for row in batch.to_pylist():
                        yield project_to_pair(row, source)
                continue
            except Exception:
                pass

        print(f"[cdn_loader] unsupported format, skipping: {path}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: cdn_loader.py snapshot.json", file=sys.stderr)
        sys.exit(1)
    for item in stream_snapshot(sys.argv[1]):
        print(json.dumps(item, ensure_ascii=False))
```

---

### 3. Update `bin/dataset-enrich.sh` (minimal diff)
Add optional snapshot mode at top:
```bash
#!/usr/bin/env bash
# ... existing header ...

# If SNAPSHOT_PATH is provided, use CDN loader to avoid HF API calls during ingestion.
if [[ -n "${SNAPSHOT_PATH:-}" && -f "$SNAPSHOT_PATH" ]]; then
    echo "Using CDN loader from snapshot: $SNAPSHOT_PATH"
    python3 bin/cdn_loader.py "$SNAPSHOT_PATH" | \
        python3 -c "
import sys, json, hashlib
from lib.dedup import DedupStore
dedup = DedupStore()
for line in sys.stdin:
    try:
        obj = json.loads(line)
        # existing normalization + dedup logic below (reuse)
        # ...
    except Exception:
        continue
"
    exit 0
fi

# ... existing datasets.load_dataset(...) path continues unchanged ...
```

---

### 4. Training launcher snippet (Mac orchestration)
Before starting Lightning Studio,

# surrogate-1 / frontend

## Final Implementation Plan  
**Goal:** Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass Python worker** (`bin/dataset-enrich.py`) that is correct, production-ready, and immediately actionable.

---

### High-value improvement (the “why”)
- **Deterministic sharding** via `SHARD_ID`/`SHARD_TOTAL` so 16 parallel jobs never overlap.
- **CDN-bypass ingestion** (`resolve/main/...`) to avoid HF API 429s during training.
- **Schema normalization** to strict `{prompt,response}` at parse time.
- **Central deduplication** via SQLite-backed md5 store to prevent dataset duplication.
- **Idempotent, single-file worker** with proper shebang and local testability.

---

### Implementation (≤2h)

1. **Create `bin/dataset-enrich.py`**
   - Shebang `#!/usr/bin/env python3`
   - Env: `SHARD_ID`, `SHARD_TOTAL=16`, `HF_REPO`, `HF_TOKEN`, `OUTPUT_ROOT`
   - Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
   - Pre-list repo tree once and cache to `file-manifest.json`
   - Download **only via HF CDN** (`https://huggingface.co/datasets/<repo>/resolve/main/<path>`) — no auth header
   - Parse heterogeneous formats into `{prompt,response}`
   - Deduplicate via `lib/dedup.py` (SQLite + WAL)
   - Write `batches/public-merged/<YYYY-MM-DD>/shard<N>-<HHMMSS>.jsonl`
   - Upload final shard to HF dataset repo

2. **Update `lib/dedup.py`** (if needed)
   - Expose: `is_duplicate(md5: str) -> bool`, `add(md5: str) -> None`
   - Use SQLite with WAL for concurrent safety

3. **Update GitHub Actions matrix** (`ingest.yml`)
   - Keep 16-shard matrix strategy
   - Replace bash invocation with `python3 bin/dataset-enrich.py`
   - Pass `SHARD_ID`, `SHARD_TOTAL`, `HF_TOKEN`, `HF_REPO`

4. **Make executable and test locally**
   - `chmod +x bin/dataset-enrich.py`
   - Dry-run against a small test repo slice

---

### Code — `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Usage (GH Actions):
  SHARD_ID=0 SHARD_TOTAL=16 HF_TOKEN=hf_xxx \
    HF_REPO=datasets/axentx/surrogate-1-training-pairs \
    python3 bin/dataset-enrich.py
"""

import os
import sys
import json
import hashlib
import datetime
import sqlite3
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from huggingface_hub import HfApi, list_repo_tree

# ── config --
HF_REPO = os.getenv("HF_REPO", "datasets/axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT", "batches/public-merged"))
MANIFEST_PATH = Path(os.getenv("MANIFEST_PATH", "file-manifest.json"))

if not HF_TOKEN:
    sys.exit("ERROR: HF_TOKEN is required")

API = HfApi(token=HF_TOKEN)

# ── dedup --
class DedupStore:
    def __init__(self, db_path: str = ".dedup.db"):
        self.conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
        self.conn.execute("CREATE TABLE IF NOT EXISTS md5s (md5 TEXT PRIMARY KEY)")
        self.conn.execute("PRAGMA journal_mode=WAL")

    def is_duplicate(self, md5: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM md5s WHERE md5=?", (md5,))
        return cur.fetchone() is not None

    def add(self, md5: str) -> None:
        try:
            self.conn.execute("INSERT INTO md5s(md5) VALUES (?)", (md5,))
        except sqlite3.IntegrityError:
            pass  # race/duplicate is fine

dedup = DedupStore()

# ── helpers --
def deterministic_shard(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % SHARD_TOTAL

def list_files_manifest() -> List[str]:
    if MANIFEST_PATH.exists():
        try:
            with open(MANIFEST_PATH) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [p for p in data if isinstance(p, str)]
                if isinstance(data, dict) and "files" in data:
                    return [p for p in data["files"] if isinstance(p, str)]
        except Exception:
            pass

    # fallback: list top-level and common folders non-recursively
    paths: List[str] = []
    seen = set()
    try:
        for folder in ["", "raw", "batches", "mirror-merged"]:
            try:
                tree = list_repo_tree(repo_id=HF_REPO, path=folder, recursive=False)
                for item in tree:
                    if item.type == "file":
                        p = item.path
                        if p not in seen:
                            paths.append(p)
                            seen.add(p)
            except Exception:
                continue
    except Exception as e:
        sys.exit(f"ERROR: failed to list repo tree: {e}")

    try:
        with open(MANIFEST_PATH, "w") as f:
            json.dump(paths, f)
    except Exception:
        pass
    return paths

def download_via_cdn(repo: str, path: str) -> Optional[bytes]:
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"CDN download failed {path}: {e}", file=sys.stderr)
        return None

def parse_to_pair(raw: bytes, filename: str) -> Optional[Dict[str, str]]:
    name = filename.lower()
    text = raw.decode("utf-8", errors="replace").strip()

    # JSONL
    if name.endswith(".jsonl"):
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
                response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
                if prompt and response:
                    return {"prompt": str(prompt).strip(), "response": str(response).strip()}
            except Exception:
                continue
        return None

    # single JSON object
    if name.endswith(".json"):
        try:
            obj = json.loads(text)
            if isinstance(obj, list):
                # take first valid pair
                for item in obj:
                    if isinstance(item, dict):
                        prompt = item.get("prompt") or item.get("input") or item.get("question") or ""
                        response = item.get("response") or item.get("output") or item.get("answer") or ""
                        if prompt and response:
                            return {"prompt": str(prompt).strip(), "response": str(response).strip()}
            elif isinstance(obj, dict):
                prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
                response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
                if prompt and response:
                    return {"prompt": str(prompt).strip(), "response": str(response).strip()}
        except Exception:
            pass
        return None

    # CSV with prompt/response columns
    if name.endswith(".csv"):
        import csv
        try:
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                prompt = row.get("prompt")

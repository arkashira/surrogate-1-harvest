# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single pre-listed file manifest** (`manifests/{DATE_FOLDER}.json`) produced by the Mac orchestrator after the rate-limit window clears; if missing, falls back to one non-recursive `list_repo_tree` call per top-level folder to avoid 429.
- Downloads only assigned shard files via **HF CDN direct URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero Authorization header, bypasses API rate limits during training data load.
- Projects each file to `{prompt, response}` at parse time (no schema assumptions), computes content-based md5 for dedup via central SQLite store on HF Space (shared across runners), and streams output to `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.
- Adds retry/backoff for 429 (wait 360s) and commit-cap mitigation: deterministic repo selection across 5 sibling repos using hash-slug → repo mapping (640/hr aggregate).
- Runs as executable Python script invoked by GitHub Actions matrix; keeps Mac as orchestrator only (no model loading).

---

## Code Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.
Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py
  SHARD_ID=0 SHARD_TOTAL=16 DATE_FOLDER=2026-05-03 python bin/dataset-enrich.py
"""
import os
import sys
import json
import hashlib
import time
import sqlite3
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))

# Sibling repos for commit-cap mitigation (5 siblings + primary = 640/hr)
SIBLING_REPOS = [
    HF_REPO,
    "axentx/surrogate-1-training-pairs-s1",
    "axentx/surrogate-1-training-pairs-s2",
    "axentx/surrogate-1-training-pairs-s3",
    "axentx/surrogate-1-training-pairs-s4",
    "axentx/surrogate-1-training-pairs-s5",
]

MANIFEST_PATH = Path(f"manifests/{DATE_FOLDER}.json")
OUTPUT_DIR = Path(f"batches/public-merged/{DATE_FOLDER}")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.utcnow().strftime("%H%M%S")
OUTPUT_FILE = OUTPUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# Central dedup store (HF Space writable path)
DEDUP_DB = Path(os.getenv("DEDUP_DB", "dedup/hashes.db"))
DEDUP_DB.parent.mkdir(parents=True, exist_ok=True)

HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}


def get_manifest() -> List[str]:
    """Return list of file paths for DATE_FOLDER."""
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())

    # Fallback: single non-recursive tree call per top folder to avoid 429
    # This should be run sparingly by Mac orchestrator after rate-limit clears.
    print("Manifest missing; listing top-level tree (non-recursive).", file=sys.stderr)
    url = f"https://huggingface.co/api/datasets/{HF_REPO}/tree"
    params = {"path": DATE_FOLDER, "recursive": "false"}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
    if resp.status_code == 429:
        wait = 360
        print(f"Rate limited 429. Waiting {wait}s.", file=sys.stderr)
        time.sleep(wait)
        return get_manifest()
    resp.raise_for_status()
    items = resp.json()
    # Expect nested folders; collect recursively with minimal calls
    files = []
    for item in items:
        if item["type"] == "file":
            files.append(item["path"])
        else:
            sub = requests.get(
                f"https://huggingface.co/api/datasets/{HF_REPO}/tree",
                headers=HEADERS,
                params={"path": item["path"], "recursive": "true"},
                timeout=30,
            )
            if sub.status_code == 429:
                time.sleep(360)
                sub = requests.get(sub.url, headers=HEADERS, params=sub.request.params, timeout=30)
            sub.raise_for_status()
            files.extend(x["path"] for x in sub.json() if x["type"] == "file")
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(files, indent=2))
    return files


def select_sibling_repo(path: str) -> str:
    """Deterministic repo selection for commit-cap mitigation."""
    h = hashlib.sha256(path.encode()).hexdigest()
    idx = int(h[:8], 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]


def cdn_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def init_dedup() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DEDUP_DB))
    conn.execute("CREATE TABLE IF NOT EXISTS hashes (md5 TEXT PRIMARY KEY, ts TEXT)")
    conn.commit()
    return conn


def is_duplicate(conn: sqlite3.Connection, md5: str) -> bool:
    cur = conn.execute("SELECT 1 FROM hashes WHERE md5=?", (md5,))
    return cur.fetchone() is not None


def mark_duplicate(conn: sqlite3.Connection, md5: str) -> None:
    try:
        conn.execute("INSERT INTO hashes (md5, ts) VALUES (?, ?)", (md5, datetime.utcnow().isoformat()))
        conn.commit()
    except sqlite3.IntegrityError:
        pass


def parse_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """Project heterogeneous schemas to {prompt, response}."""
    # Common field names seen in public datasets
    prompt_keys = {"prompt", "instruction", "input", "question", "user"}
    response_keys = {"response", "output", "answer", "assistant", "completion"}

    prompt = None
    response = None

    for k in raw:
        if k in prompt_keys and prompt is None:
            prompt = str(raw[k]).strip()
        if k in response_keys and response is None:
            response = str(raw[k]).strip()

    # Fallback: if only one text-like field exists, split by common separators
    if prompt is None or response is None:
        text_vals = [str(v).strip() for v in raw.values() if isinstance(v, str) and len(v) > 10]
        if len(text_vals) == 1:
            t = text_vals[0]
            # crude split on Q/A markers
            if "\n\nA:" in t or "\n\nAnswer:" in t:
                parts = t.split("\n\nA:", 1)
                if len(parts) == 2:
                    prompt, response = parts[0].strip(), "A:" + parts[1].strip()
            elif "\n\nResponse:" in t:
                parts = t.split("\n\nResponse:", 1)
                if len(parts) == 2:
                    prompt, response = parts[0].strip(), "Response:" + parts[1].strip()
            else:
                prompt = t[: len(t)

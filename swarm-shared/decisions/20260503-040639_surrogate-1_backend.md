# surrogate-1 / backend

Below is the **single, merged implementation plan and code** that keeps only the strongest, non-redundant parts of both proposals, resolves contradictions in favor of correctness + concrete actionability, and is ready to run end-to-end.

---

## Final Implementation Plan (≤2h)

**Highest-value improvement**:  
Replace fragile shell ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits and mixed-schema pyarrow errors while keeping reproducible, parallel 16-shard GitHub Actions execution.

### Concrete changes (ordered by priority)

1. **`bin/dataset-enrich.py`** (single-file worker) — deterministic sharding, manifest, CDN-only fetches, schema-tolerant projection, central dedup integration, and robust retry/backoff.
2. **Keep `lib/dedup.py` unchanged** (central md5 store) and import it programmatically.
3. **`requirements.txt`** — ensure `requests` is present.
4. **`.github/workflows/ingest.yml`** — switch from shell script to the Python worker with the same matrix strategy (`SHARD_ID`, `SHARD_TOTAL=16`).

---

## Why this is highest value
- Eliminates HF API rate limits during training (CDN-only fetches, no per-file auth checks).
- Avoids `load_dataset(streaming=True)` mixed-schema pyarrow errors by projecting at parse time.
- Manifest (`file-list.json`) enables reproducible, cache-friendly runs and easier debugging.
- Single Python file is simpler to maintain and test than brittle shell pipelines.
- Deterministic sharding + central dedup prevents duplicates across shards/runs.

---

## Final Code Snippets

### `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py

Env:
  SHARD_ID            - worker index (0..SHARD_TOTAL-1)
  SHARD_TOTAL         - total shards (default 16)
  DATE_FOLDER         - dataset subfolder (default today YYYY-MM-DD)
  HF_TOKEN            - write token for axentx/surrogate-1-training-pairs
  REPO                - HF dataset repo (default axentx/surrogate-1-training-pairs)
"""

import os
import sys
import json
import hashlib
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
from huggingface_hub import HfApi

# ── config ────────────────────────────────────────────────────────────────
REPO = os.getenv("REPO", "axentx/surrogate-1-training-pairs")
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN")

if not HF_TOKEN:
    print("ERROR: HF_TOKEN is required", file=sys.stderr)
    sys.exit(1)

if not (0 <= SHARD_ID < SHARD_TOTAL):
    print(f"ERROR: SHARD_ID must be in [0, {SHARD_TOTAL - 1}]", file=sys.stderr)
    sys.exit(1)

API = HfApi(token=HF_TOKEN)
BASE_DIR = Path(__file__).parent.parent
DEDUP_MODULE = BASE_DIR / "lib" / "dedup.py"
OUTPUT_DIR = BASE_DIR / "batches" / "public-merged" / DATE_FOLDER
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.now(timezone.utc).strftime("%H%M%S")
OUTPUT_FILE = OUTPUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ── helpers ───────────────────────────────────────────────────────────────
def deterministic_shard(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def load_dedup():
    if DEDUP_MODULE.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("dedup", DEDUP_MODULE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    return None

def project_to_pair(obj) -> dict:
    """
    Project arbitrary parsed file object to {prompt, response}.
    Handles common surrogate-1 schema variants.
    """
    if isinstance(obj, dict):
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
        response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
        return {"prompt": str(prompt), "response": str(response)}
    return {"prompt": "", "response": ""}

def list_date_files():
    """List files in DATE_FOLDER (non-recursive) and save manifest."""
    try:
        tree = API.list_repo_tree(repo_id=REPO, path=DATE_FOLDER, recursive=False)
        items = [t for t in tree if hasattr(t, "rfilename")]
        filenames = [it.rfilename for it in items]
    except Exception:
        # fallback: list root and filter by prefix
        tree = API.list_repo_tree(repo_id=REPO, path="", recursive=False)
        items = [t for t in tree if hasattr(t, "rfilename")]
        filenames = [it.rfilename for it in items if it.rfilename.startswith(f"{DATE_FOLDER}/")]

    manifest_path = BASE_DIR / "file-list.json"
    manifest = {
        "repo": REPO,
        "date_folder": DATE_FOLDER,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": filenames,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest saved: {manifest_path} ({len(filenames)} files)")
    return filenames

def download_via_cdn(repo: str, path: str, max_retries: int = 3, backoff: float = 2.0) -> bytes:
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            if attempt == max_retries:
                raise
            sleep_time = backoff ** attempt
            print(f"Retry {attempt}/{max_retries} for {path}: {e} — sleeping {sleep_time:.1f}s")
            time.sleep(sleep_time)
    raise RuntimeError(f"Failed to download {path} after {max_retries} attempts")

def parse_jsonl(content: bytes):
    for line in content.decode("utf-8", errors="replace").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue

def parse_parquet(content: bytes):
    import pyarrow.parquet as pq
    import io
    table = pq.read_table(io.BytesIO(content))
    # Try known columns first; avoid mixed-schema issues by projecting one column at a time
    for col in ("prompt", "response", "input", "output", "question", "answer", "text"):
        if col in table.column_names:
            for v in table[col].to_pylist():
                yield {col: v}
            return
    # fallback: first column
    if table.num_columns > 0:
        col = table.column_names[0]
        for v in table[col].to_pylist():
            yield {col: v}

def parse_file(path: str, content: bytes):
    path_l = path.lower()
    if path_l.endswith(".jsonl"):
        yield from parse_jsonl(content)
    elif path_l.endswith(".parquet"):
        yield from parse_parquet(content)
    else:
        # try jsonl first, then fallback to raw text lines
        try:
            yield from parse_jsonl(content)
        except Exception:
            for line in content.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if line:
                    yield {"text": line}

# ── worker ─────────────────────────────────────────────────

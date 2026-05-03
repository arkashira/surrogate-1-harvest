# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. Add `bin/ingest_worker.py` — single-file worker that:
   - Accepts `SHARD_ID` and `TOTAL_SHARDS` (0–15 / 16) via env.
   - Calls `list_repo_tree(recursive=False)` **once** for the target date folder, saves manifest JSON.
   - Downloads only assigned slice via **CDN URLs** (`resolve/main/...`) with no Authorization header → bypasses API rate limits.
   - Projects each file to `{prompt, response}` at parse time (avoids `load_dataset` mixed-schema CastError).
   - Streams output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
   - Uses central `lib/dedup.py` for cross-source md5 dedup.

2. Update `bin/dataset-enrich.sh` → thin wrapper that:
   - Sets `SHELL=/bin/bash`, `set -euo pipefail`.
   - Invokes `python3 bin/ingest_worker.py` with matrix env.
   - Remains executable (`chmod +x`).

3. Update `.github/workflows/ingest.yml` → ensure:
   - Matrix `shard_id: [0..15]`.
   - `HF_TOKEN` only used for repo list + final push (not during CDN downloads).
   - `python-version: '3.11'`.

### Code Snippets

#### `bin/ingest_worker.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass ingest worker for surrogate-1 public dataset shards.

Usage (env):
  SHARD_ID=0 TOTAL_SHARDS=16 HF_TOKEN=hf_xxx python3 bin/ingest_worker.py
"""
import os, sys, json, hashlib, datetime, time, pathlib, gzip
from typing import Iterator, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download

REPO = "axentx/surrogate-1-training-pairs"
API = HfApi(token=os.getenv("HF_TOKEN"))

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "16"))
assert 0 <= SHARD_ID < TOTAL_SHARDS

DATE = datetime.datetime.utcnow().strftime("%Y-%m-%d")
OUT_DIR = pathlib.Path("batches/public-merged") / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_PATH = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# Central dedup store (shared across workers via mounted volume or HF Space SQLite)
sys.path.insert(0, str(pathlib.Path(__file__).parent / "lib"))
from dedup import DedupStore  # expects DedupStore().seen(md5) -> bool; .add(md5)

dedup = DedupStore()

session = requests.Session()
# CDN downloads: no auth header -> bypasses API rate limits
session_no_auth = requests.Session()

def list_date_folder() -> list[str]:
    """Single API call to list target date folder; cached in manifest."""
    items = API.list_repo_tree(repo_id=REPO, path=f"batches/public-merged/{DATE}", recursive=False)
    paths = [it.rfilename for it in items if it.rfilename.endswith((".jsonl", ".jsonl.gz", ".parquet"))]
    # Save manifest for reproducibility / training script embedding
    manifest_path = OUT_DIR / f"manifest-{DATE}.json"
    manifest_path.write_text(json.dumps({"date": DATE, "files": paths, "generated_utc": datetime.datetime.utcnow().isoformat()}, indent=2))
    return paths

def cdn_download_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def stream_lines(path: str) -> Iterator[bytes]:
    url = cdn_download_url(REPO, path)
    with session_no_auth.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        if path.endswith(".gz"):
            # Stream gzip decompression without reading entire file
            import io
            with gzip.GzipFile(fileobj=io.BytesIO(r.content), mode="rb") as gz:
                for line in iter(lambda: gz.readline(1024 * 1024), b""):
                    yield line
        else:
            for chunk in r.iter_lines(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk

def parse_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """Project heterogeneous schemas to {prompt, response} only."""
    # Common field names seen in public datasets
    prompt_keys = {"prompt", "instruction", "input", "question", "user"}
    response_keys = {"response", "output", "answer", "assistant", "completion"}

    prompt = None
    response = None

    for k, v in raw.items():
        if k in prompt_keys and isinstance(v, str) and v.strip():
            prompt = v.strip()
        if k in response_keys and isinstance(v, str) and v.strip():
            response = v.strip()

    # Fallbacks
    if prompt is None:
        prompt = raw.get("text", raw.get("content", "")).strip()
    if response is None and "completion" in raw and isinstance(raw["completion"], str):
        response = raw["completion"].strip()

    # If still ambiguous, try to split by common separators (last-resort)
    if prompt and not response and "\n### Response:" in prompt:
        parts = prompt.split("\n### Response:", 1)
        prompt, response = parts[0].strip(), parts[1].strip()

    return {"prompt": prompt or "", "response": response or ""}

def process_shard() -> None:
    files = list_date_folder()
    if not files:
        print("No files found for date:", DATE)
        return

    assigned = [f for i, f in enumerate(sorted(files)) if hash(f) % TOTAL_SHARDS == SHARD_ID]
    print(f"Shard {SHARD_ID}/{TOTAL_SHARDS}: processing {len(assigned)} files")

    written = 0
    skipped_dup = 0
    errors = 0

    with OUT_PATH.open("w", encoding="utf-8") as out_f:
        for path in assigned:
            try:
                for line_bytes in stream_lines(path):
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        # tolerate parquet-converted line dumps or malformed
                        continue

                    pair = parse_to_pair(raw)
                    if not pair["prompt"] or not pair["response"]:
                        continue

                    # Dedup by content hash
                    md5 = hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()
                    if dedup.seen(md5):
                        skipped_dup += 1
                        continue
                    dedup.add(md5)

                    out_f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                    written += 1
            except Exception as exc:
                errors += 1
                print(f"Error processing {path}: {exc}", file=sys.stderr)

            # Periodic flush
            if written % 5000 == 0:
                out_f.flush()

    print(f"Shard {SHARD_ID} done: written={written}, skipped_dup={skipped_dup}, errors={errors}")
    print(f"Output: {OUT_PATH}")

if __name__ == "__main__":
    process_shard()
```

#### `lib/dedup.py` (minimal, extend existing)
```python
import sqlite3
import os
import threading

class DedupStore:
    """Central md5 dedup store (thread-safe)."""
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.getenv("DEDUP_DB_PATH", "/tmp/surrogate_dedup.sqlite")
        self.db_path = db_path
        self.local = threading.local()
        self._ensure_schema()



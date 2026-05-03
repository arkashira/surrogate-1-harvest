# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Uses a **single `list_repo_tree` snapshot** (JSON manifest) generated once per date on the Mac orchestrator and committed to the repo (or passed via `MANIFEST_PATH` or `MANIFEST_JSON` env).  
- Each GitHub Actions shard (0–15) deterministically hashes `slug → bucket = hash(slug) % 16` and only processes files in its bucket.  
- Downloads via **raw CDN URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero HF API calls during streaming, bypassing 429 rate limits.  
- Projects heterogeneous schemas to `{prompt, response}` at parse time (no `load_dataset(streaming=True)` on mixed schemas).  
- Deduplicates via central `lib/dedup.py` md5 store.  
- Writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.  
- Keeps the existing cron/workflow unchanged; only the worker script is swapped.

---

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.
Usage (GH Actions):
  SHARD_ID=0 MANIFEST_PATH=manifests/2026-05-03.json python bin/dataset-enrich.py
  # or inline manifest:
  SHARD_ID=0 MANIFEST_JSON='{"files":["..."]}' python bin/dataset-enrich.py
"""
import json
import os
import sys
import hashlib
import time
from pathlib import Path
from datetime import datetime, timezone

import requests
import pyarrow as pa
import pyarrow.parquet as pq

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa: E402

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
CDN_BASE = f"https://huggingface.co/{HF_REPO}/resolve/main"
BATCH_DIR = Path("batches/public-merged")

# Deterministic shard assignment
def shard_for_slug(slug: str, n_shards: int = 16) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % n_shards

def download_via_cdn(path: str, timeout: int = 30) -> bytes:
    """Download public file via CDN (no auth)."""
    url = f"{CDN_BASE}/{path}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def parse_to_pair(content: bytes, file_path: str) -> dict | None:
    """
    Project heterogeneous schemas to {prompt, response}.
    Supports: parquet, jsonl, json (basic).
    """
    suffix = Path(file_path).suffix.lower()
    try:
        if suffix == ".parquet":
            tbl = pq.read_table(pa.BufferReader(content))
            # Try common surrogate-1 column names
            prompt_col = next((c for c in tbl.column_names if "prompt" in c.lower()), None)
            resp_col = next((c for c in tbl.column_names if "response" in c.lower() or "completion" in c.lower()), None)
            if prompt_col and resp_col and tbl.num_rows > 0:
                return {
                    "prompt": str(tbl[prompt_col][0].as_py()),
                    "response": str(tbl[resp_col][0].as_py()),
                }
        elif suffix in (".jsonl", ".json"):
            text = content.decode("utf-8", errors="ignore")
            if suffix == ".jsonl":
                # take first non-empty line
                for line in text.strip().split("\n"):
                    line = line.strip()
                    if line:
                        data = json.loads(line)
                        break
                else:
                    return None
            else:
                data = json.loads(text)

            if isinstance(data, dict):
                prompt = data.get("prompt") or data.get("input") or data.get("question")
                response = data.get("response") or data.get("output") or data.get("answer")
                if prompt and response:
                    return {"prompt": str(prompt), "response": str(response)}
    except Exception as exc:
        print(f"[WARN] parse failed {file_path}: {exc}", file=sys.stderr)
    return None

def main() -> None:
    shard_id = int(os.getenv("SHARD_ID", "0"))
    manifest_path = os.getenv("MANIFEST_PATH")
    manifest_json = os.getenv("MANIFEST_JSON")
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    if manifest_json:
        manifest = json.loads(manifest_json)
    elif manifest_path and Path(manifest_path).exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
    else:
        print("[ERROR] provide MANIFEST_PATH or MANIFEST_JSON", file=sys.stderr)
        sys.exit(1)

    # manifest format: {"date": "2026-05-03", "files": ["path/to/file.parquet", ...]}
    files = manifest.get("files", [])
    my_files = [p for p in files if shard_for_slug(p) == shard_id]
    print(f"[INFO] shard={shard_id} total_files={len(files)} my_files={len(my_files)}")

    dedup = DedupStore()
    out_rows = []
    processed = 0
    skipped_dup = 0
    failed = 0

    for file_path in my_files:
        try:
            content = download_via_cdn(file_path)
            pair = parse_to_pair(content, file_path)
            if not pair:
                failed += 1
                continue

            # Dedup by content hash
            digest = hashlib.md5(f"{pair['prompt']}\n{pair['response']}".encode()).hexdigest()
            if dedup.exists(digest):
                skipped_dup += 1
                continue

            dedup.add(digest)
            out_rows.append({
                "prompt": pair["prompt"],
                "response": pair["response"],
                "source_file": file_path,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            processed += 1

            # Periodic flush to avoid memory blow
            if len(out_rows) >= 500:
                _flush(out_rows, shard_id, run_ts)
                out_rows = []

        except Exception as exc:
            print(f"[ERROR] {file_path}: {exc}", file=sys.stderr)
            failed += 1
            time.sleep(1)

    if out_rows:
        _flush(out_rows, shard_id, run_ts)

    print(f"[DONE] shard={shard_id} processed={processed} skipped_dup={skipped_dup} failed={failed}")

def _flush(rows, shard_id, run_ts):
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.utcnow().strftime("%Y-%m-%d")
    batch_dir = BATCH_DIR / date
    batch_dir.mkdir(exist_ok=True)
    out_path = batch_dir / f"shard{shard_id}-{run_ts}.jsonl"
    with open(out_path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[FLUSH] {len(rows)} -> {out_path}")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/dataset-enrich.py
```

---

### 2) Keep dedup store unchanged

`lib/dedup.py` remains as-is (central md5 SQLite store). No changes required.

---

### 3) Update workflow: `.github/workflows/ingest.yml`

Minimal change: switch to the Python worker and support both `MANIFEST_PATH` and `MANIFEST_JSON`.

```yaml
# .github/workflows/ingest.yml
name: surrogate-1-ingest

on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:
    inputs:
      manifest_path:
       

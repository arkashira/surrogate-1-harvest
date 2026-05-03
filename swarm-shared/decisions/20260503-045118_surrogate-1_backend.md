# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **`bin/dataset-enrich.py`** (new, replaces shell script)  
   - Single API call to `list_repo_tree(path, recursive=False)` for one date folder.  
   - Save file list to `manifest-{date}.json`.  
   - Workers read manifest and download via **CDN URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) with **no Authorization header** → bypasses `/api/` rate limits entirely.  
   - Stream-parse each file, project to `{prompt, response}` only, compute md5, dedup via central SQLite, emit normalized JSONL.  
   - Deterministic shard assignment via `hash(slug) % 16 == SHARD_ID`.  
   - Upload output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

2. **`lib/dedup.py`** (unchanged interface)  
   - Keep SQLite-backed md5 store; add `is_duplicate(md5)` and `mark_seen(md5)` helpers.

3. **`.github/workflows/ingest.yml`**  
   - Matrix `shard_id: [0..15]`.  
   - Each job runs `python bin/dataset-enrich.py --shard $SHARD_ID --date $YYYYMMDD`.  
   - Retry on 429 with 360s backoff; respect HF commit cap by using deterministic filenames (no collisions).

4. **`requirements.txt`**  
   - Add `requests`, keep `datasets`, `huggingface_hub`, `pyarrow`, `numpy`.

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.
Usage:
  python bin/dataset-enrich.py --shard 0 --date 20260503
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import list_repo_tree

# Local
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
CDN_ROOT = f"https://huggingface.co/{HF_REPO}/resolve/main"
DATE_FMT = "%Y%m%d"
BATCH_DIR = Path("batches/public-merged")

# Central dedup store (shared across shards via mounted volume or HF Space SQLite)
DEDUP = DedupStore()

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def list_date_files(date_str: str) -> list[str]:
    """Single API call: list top-level folder for date (non-recursive)."""
    tree = list_repo_tree(repo_id=HF_REPO, path=date_str, recursive=False)
    return [t.path for t in tree if t.type == "file"]

def build_manifest(date_str: str, manifest_path: Path):
    files = list_date_files(date_str)
    manifest = {
        "date": date_str,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "files": sorted(files),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest

def cdn_download(url: str, timeout: int = 30) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def parse_file_to_pairs(content: bytes, file_path: str):
    """Project arbitrary schema to {prompt, response} pairs."""
    ext = Path(file_path).suffix.lower()
    try:
        if ext == ".parquet":
            table = pq.read_table(pa.BufferReader(content))
            # Keep only prompt/response if present; ignore others
            cols = set(table.column_names)
            prompt_col = next((c for c in ("prompt", "input", "question") if c in cols), None)
            response_col = next((c for c in ("response", "output", "answer") if c in cols), None)
            if prompt_col is None or response_col is None:
                return []
            prompts = table.column(prompt_col).to_pylist()
            responses = table.column(response_col).to_pylist()
            return [{"prompt": p, "response": r} for p, r in zip(prompts, responses) if p and r]
        elif ext == ".jsonl":
            pairs = []
            for line in content.decode().splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                p = obj.get("prompt") or obj.get("input") or obj.get("question")
                r = obj.get("response") or obj.get("output") or obj.get("answer")
                if p and r:
                    pairs.append({"prompt": p, "response": r})
            return pairs
        else:
            # Fallback: try load_dataset streaming on single file via local bytes
            # (rare; prefer CDN + explicit formats)
            return []
    except Exception as e:
        print(f"Parse error {file_path}: {e}", file=sys.stderr)
        return []

def worker_shard(manifest: dict, shard_id: int):
    files = manifest["files"]
    date_str = manifest["date"]
    out_dir = BATCH_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{shard_id}-{timestamp}.jsonl"

    processed = 0
    dupes = 0

    with out_path.open("w", encoding="utf-8") as fout:
        for file_path in files:
            # Deterministic shard assignment by slug
            slug = Path(file_path).stem
            if hash(slug) % 16 != shard_id:
                continue

            url = f"{CDN_ROOT}/{file_path}"
            try:
                content = cdn_download(url)
            except Exception as e:
                print(f"Download failed {file_path}: {e}", file=sys.stderr)
                continue

            pairs = parse_file_to_pairs(content, file_path)
            for pair in pairs:
                text = json.dumps(pair, sort_keys=True, ensure_ascii=False)
                md5 = hashlib.md5(text.encode()).hexdigest()
                if DEDUP.is_duplicate(md5):
                    dupes += 1
                    continue
                DEDUP.mark_seen(md5)
                fout.write(text + "\n")
                processed += 1

    print(f"Shard {shard_id} done: processed={processed}, dupes={dupes}")
    return out_path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True, help="Shard ID 0..15")
    parser.add_argument("--date", type=str, help=f"Date {DATE_FMT} (default today)")
    parser.add_argument("--rebuild-manifest", action="store_true")
    args = parser.parse_args()

    if args.shard < 0 or args.shard > 15:
        print("Shard must be 0..15", file=sys.stderr)
        sys.exit(1)

    date_str = args.date or datetime.utcnow().strftime(DATE_FMT)
    manifest_path = Path("manifest") / f"{date_str}.json"
    manifest_path.parent.mkdir(exist_ok=True)

    if args.rebuild_manifest or not manifest_path.exists():
        print(f"Building manifest for {date_str}...")
        manifest = build_manifest(date_str, manifest_path)
    else:
        manifest = json.loads(manifest_path.read_text())

    worker_shard(manifest, args.shard)

if __name__ == "__main__":
    main()
```

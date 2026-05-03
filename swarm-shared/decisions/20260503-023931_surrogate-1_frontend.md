# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree` call from Mac (outside training) → `file-list-<DATE>.json` committed to repo (or passed via workflow artifact). Worker loads this manifest and processes only its deterministic shard.
- Uses **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) for all file downloads — zero API calls during ingestion, avoids 429/128-commit limits.
- Projects heterogeneous schemas to `{prompt, response}` at parse time; writes `batches/public-merged/<DATE>/shard<N>-<HHMMSS>.jsonl`.
- Deduplicates via central `lib/dedup.py` md5 store (same as existing).
- GitHub Actions matrix keeps 16 parallel runners; each runner runs this single Python script.

### Steps (1h45m)

1. **Create `bin/dataset-enrich.py`** (60m)  
   - Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
   - CDN download with `requests` (no auth header) + streaming to avoid memory spikes.
   - Schema projection: try HF `datasets` features first; fallback to per-file heuristic (parquet/jsonl) extracting `prompt`/`response` keys or `text` split.
   - Write JSONL lines with deterministic ordering.

2. **Create `bin/generate-manifest.py`** (15m)  
   - Run on Mac (or CI job) before ingestion window.
   - Calls `list_repo_tree(path=date_folder, recursive=True)` once.
   - Saves `file-list-<DATE>.json` to repo (or uploads as workflow artifact).
   - Commits only file list (small) — avoids per-run API churn.

3. **Update `.github/workflows/ingest.yml`** (15m)  
   - Add `DATE` input (default: today’s `%Y-%m-%d`).
   - Add step to fetch `file-list-<DATE>.json` (from repo or artifact).
   - Matrix `shard_id: [0..15]`.
   - Each job runs `python bin/dataset-enrich.py --shard $SHARD_ID --total 16 --date $DATE`.

4. **Deprecate `bin/dataset-enrich.sh`** (5m)  
   - Rename to `bin/dataset-enrich.sh.bak` or remove.
   - Update README if needed.

5. **Test locally** (10m)  
   - Run one shard against a small date folder; verify JSONL shape and dedup.

---

## Code Snippets

### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py --manifest file-list-2026-05-03.json
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

REPO = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO}/resolve/main"

def slug_hash(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def belongs_to_shard(slug: str, shard_id: int, shard_total: int) -> bool:
    return slug_hash(slug) % shard_total == shard_id

def parse_file_to_pairs(path: Path, file_url: str):
    """Yield (prompt, response) pairs from a file."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".parquet":
            # Stream via pyarrow; project only needed columns
            pf = pq.ParquetFile(file_url)
            for batch in pf.iter_batches(batch_size=1000, columns=["prompt", "response"]):
                df = batch.to_pydict()
                for p, r in zip(df.get("prompt", []), df.get("response", [])):
                    if p is not None and r is not None:
                        yield {"prompt": p, "response": r}
            return

        if suffix == ".jsonl":
            resp = requests.get(file_url, stream=True, timeout=60)
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                obj = json.loads(line)
                p = obj.get("prompt") or obj.get("text")
                r = obj.get("response") or obj.get("completion")
                if p is not None and r is not None:
                    yield {"prompt": p, "response": r}
            return

        # Fallback: try JSON array
        if suffix == ".json":
            resp = requests.get(file_url, timeout=60)
            resp.raise_for_status()
            arr = resp.json()
            if isinstance(arr, list):
                for obj in arr:
                    p = obj.get("prompt") or obj.get("text")
                    r = obj.get("response") or obj.get("completion")
                    if p is not None and r is not None:
                        yield {"prompt": p, "response": r}
            return
    except Exception as exc:
        print(f"Warning: failed to parse {file_url}: {exc}", file=sys.stderr)
        return

def dedup_key(prompt: str, response: str) -> str:
    return hashlib.md5(f"{prompt}\0{response}".encode()).hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to file-list-<DATE>.json")
    parser.add_argument("--shard", type=int, default=int(os.getenv("SHARD_ID", 0)))
    parser.add_argument("--total", type=int, default=int(os.getenv("SHARD_TOTAL", 16)))
    parser.add_argument("--date", default=os.getenv("DATE", datetime.utcnow().strftime("%Y-%m-%d")))
    parser.add_argument("--out-dir", default="batches/public-merged")
    args = parser.parse_args()

    with open(args.manifest) as f:
        files = json.load(f)  # list of relative paths

    hf_token = os.getenv("HF_TOKEN")
    if not hf_token:
        print("ERROR: HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    # Import central dedup store
    sys.path.insert(0, str(Path(__file__).parent))
    from lib.dedup import DedupStore

    dedup = DedupStore()
    out_dir = Path(args.out_dir) / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{args.shard}-{ts}.jsonl"

    seen_local = set()
    written = 0

    with out_path.open("w", encoding="utf-8") as out_f:
        for rel in tqdm(files, desc=f"Shard {args.shard}"):
            # Determine slug from path (heuristic)
            slug = Path(rel).stem
            if not belongs_to_shard(slug, args.shard, args.total):
                continue

            file_url = f"{BASE_CDN}/{rel}"
            pairs = list(parse_file_to_pairs(Path(rel), file_url))
            for pair in pairs:
                key = dedup_key(pair["prompt"], pair["response"])
                if key in seen_local:
                    continue
                if dedup.exists(key):
                    continue
                dedup.add(key)
                seen_local.add(key)
                out_f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                written += 1

        out_f.flush()

    print(f"Shard {args.shard}: wrote {written} pairs to {out_path}")

    # Upload via huggingface_hub (optional; can be separate CI step)
    # If HF_TOKEN provided, commit file.
    if written > 0

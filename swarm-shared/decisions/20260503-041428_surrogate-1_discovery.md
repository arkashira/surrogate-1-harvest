# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/generate_manifest.py`**  
   - Run once (or on cron) from Mac/Lightning SDK or CI.  
   - Uses HF API **only once** per date folder via `list_repo_tree(recursive=False)` to list parquet files.  
   - Saves `manifest-{date}.json` containing CDN URLs (`resolve/main/...`), expected schema tag, and **pre-computed shard assignment** (`hash(slug) % 16`) so workers never call `list_repo_files`/`load_dataset` during ingest.

2. **Replace `bin/dataset-enrich.sh` with `bin/worker.py`**  
   - Accepts `SHARD_ID`, `MANIFEST_PATH`, `OUTPUT_DIR`.  
   - Reads manifest → iterates only CDN URLs assigned to this shard (by `hash(slug) % 16`).  
   - Downloads each parquet via `requests.get(cdn_url, timeout=60, stream=True)` (bypasses HF API auth/rate limit).  
   - Projects to `{prompt, response}` at parse time; drops extra columns; computes md5 for dedup.  
   - Writes `shard-<N>-<HHMMSS>.jsonl` to output.  
   - Uses `pyarrow` with explicit schema enforcement to avoid CastError on mixed files.

3. **Update `.github/workflows/ingest.yml`**  
   - Add step to generate/fetch manifest before matrix launch (or include pre-built manifest in repo).  
   - Pass `MANIFEST_PATH` to each matrix job.  
   - Keep 16-shard matrix; each job runs `python bin/worker.py`.

4. **Dedup optimization**  
   - Keep `lib/dedup.py` but add batch `md5 IN (...)` check to reduce SQLite round-trips.  
   - Workers skip already-seen hashes before upload.

5. **Lightning training alignment**  
   - Training script reads the same manifest (committed to repo) and does **CDN-only** parquet fetches during `DataLoader` iteration — zero HF API calls, zero rate limits.

---

### Code Snippets

#### `bin/generate_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate manifest for a date folder.
Usage:
  HF_TOKEN=... python bin/generate_manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out manifest-2026-05-03.json
"""
import argparse
import json
import os
from datetime import datetime

from huggingface_hub import HfApi

HF_API = HfApi(token=os.getenv("HF_TOKEN"))

def build_manifest(repo_id: str, date: str, out_path: str, n_shards: int = 16):
    folder = f"batches/public-merged/{date}"
    items = HF_API.list_repo_tree(repo_id=repo_id, path=folder, recursive=False)
    files = [it for it in items if it.path.endswith(".parquet")]

    manifest = {
        "repo_id": repo_id,
        "date": date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "n_shards": n_shards,
        "files": [],
    }

    for f in files:
        import hashlib
        shard_id = int(hashlib.md5(f.path.encode()).hexdigest(), 16) % n_shards
        manifest["files"].append({
            "path": f.path,
            "cdn_url": f"https://huggingface.co/datasets/{repo_id}/resolve/main/{f.path}",
            "schema_tag": "public",  # could be inferred from path if needed
            "shard_id": shard_id,
        })

    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--n-shards", type=int, default=16)
    args = p.parse_args()
    build_manifest(args.repo, args.date, args.out, args.n_shards)
```

#### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
Shard worker: CDN-only ingest with schema projection and dedup.
Usage:
  SHARD_ID=3 python bin/worker.py \
    --manifest manifest-2026-05-03.json \
    --out-dir batches/public-merged/2026-05-03
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from lib.dedup import DedupStore  # assumes sqlite-backed dedup

HF_TOKEN = os.getenv("HF_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
SHARD_ID = int(os.getenv("SHARD_ID", "0"))

def safe_parquet_to_jsonl(parquet_path: Path, out_f, dedup: DedupStore):
    try:
        table = pq.read_table(
            parquet_path,
            columns=["prompt", "response"],
            use_threads=False,
        )
    except (pa.ArrowInvalid, KeyError, OSError):
        # fallback: read all and project
        try:
            table = pq.read_table(parquet_path, use_threads=False)
            if "prompt" not in table.column_names or "response" not in table.column_names:
                return 0
            table = table.select(["prompt", "response"])
        except Exception:
            return 0

    count = 0
    for batch in table.to_batches(max_chunksize=1000):
        prompts = batch.column("prompt").to_pylist()
        responses = batch.column("response").to_pylist()
        for p, r in zip(prompts, responses):
            if not isinstance(p, str) or not isinstance(r, str):
                continue
            row_hash = hashlib.md5(f"{p}\n{r}".encode()).hexdigest()
            if dedup.seen(row_hash):
                continue
            dedup.add(row_hash)
            obj = {"prompt": p, "response": r}
            out_f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            count += 1
    return count

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    with open(args.manifest) as f:
        manifest = json.load(f)

    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%H%M%S")
    out_path = Path(args.out_dir) / f"shard-{SHARD_ID}-{ts}.jsonl"

    dedup = DedupStore()
    total = 0

    with out_path.open("w") as out_f:
        for entry in manifest["files"]:
            if entry.get("shard_id") != SHARD_ID:
                continue

            # CDN download (no auth required for public datasets)
            resp = requests.get(entry["cdn_url"], headers=HEADERS, stream=True, timeout=60)
            resp.raise_for_status()

            tmp = Path("tmp.parquet")
            with tmp.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            count = safe_parquet_to_jsonl(tmp, out_f, dedup)
            total += count
            tmp.unlink(missing_ok=True)

    print(f"Shard {SHARD_ID}: wrote {total} pairs to {out_path}")

if __name__ == "__main__":
    main()
```

#### Updated `.github/workflows/ingest.yml` (excerpt)
```yaml
jobs:
  ingest:
    strategy:
      matrix:
        shard: [0,1,2,3,4

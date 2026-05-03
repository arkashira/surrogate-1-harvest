# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/manifest.py`**  
   - Single Mac-side script that uses HF API **once** (after rate-limit window) via `list_repo_tree(recursive=False)` for a date folder.  
   - Writes `manifest.json` containing `{repo, date, files: [{path, size}]}`.  
   - Embeds this manifest into the repo (or passes via workflow) so workers never call `list_repo_tree`/`list_repo_files` recursively.

2. **Replace `bin/dataset-enrich.sh` → `bin/worker.py`**  
   - Accepts `SHARD_ID` and `TOTAL_SHARDS` (16).  
   - Loads `manifest.json`, deterministically hashes each file path → `hash(slug) % TOTAL_SHARDS` to assign shards.  
   - Downloads assigned files **via CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header.  
   - Streams each file (parquet/jsonl), projects to `{prompt, response}` only at parse time, skips extra columns.  
   - Dedups via central `lib/dedup.py` md5 store.  
   - Outputs `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

3. **Update `.github/workflows/ingest.yml`**  
   - Matrix job (16 shards) that:  
     - Runs `python bin/manifest.py` once in a setup step (or reuses cached manifest).  
     - Runs 16 parallel `python bin/worker.py` with `SHARD_ID` matrix.  
     - Uses `actions/upload-artifact` per shard, then a final merge/upload step.

4. **Add `requirements.txt`** entries if missing: `requests`, `pyarrow`, `pandas`, `tqdm`.

### Why this wins
- **Zero HF API calls during data load** → bypasses 429 rate limits.  
- **No mixed-schema CastErrors** → project to `{prompt, response}` only at parse.  
- **Deterministic sharding** → no collisions, safe retries.  
- **Fits <2h** — small, focused changes with high leverage.

---

## Code Snippets

### `bin/manifest.py`
```python
#!/usr/bin/env python3
"""
Generate manifest for a single date folder.
Usage:
  python bin/manifest.py --repo axentx/surrogate-1-training-pairs \
                         --date 2026-05-03 \
                         --out manifest.json
"""
import argparse
import json
import os
from huggingface_hub import HfApi

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Folder name, e.g. 2026-05-03")
    parser.add_argument("--out", default="manifest.json")
    args = parser.parse_args()

    api = HfApi(token=os.getenv("HF_TOKEN"))
    # Single non-recursive call
    files = api.list_repo_tree(
        repo_id=args.repo,
        path=args.date,
        repo_type="dataset",
        recursive=False,
    )

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "files": [
            {"path": f.rfilename, "size": f.size}
            for f in files if f.size and f.rfilename
        ],
    }

    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(manifest['files'])} files to {args.out}")

if __name__ == "__main__":
    main()
```

### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass worker.
Usage:
  SHARD_ID=0 TOTAL_SHARDS=16 python bin/worker.py manifest.json
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def shard_for(path: str, total: int) -> int:
    return int(hashlib.sha256(path.encode()).hexdigest(), 16) % total

def project_record(batch, columns=("prompt", "response")):
    # Keep only required columns; tolerate missing ones
    existing = [c for c in columns if c in batch.column_names]
    return batch.select(existing)

def stream_parquet(url: str):
    # Stream remote parquet without full download via pyarrow's dataset?
    # For simplicity and memory control, download in chunks via CDN.
    # Parquet requires random access; fallback: download to temp file.
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=8192):
            tmp.write(chunk)
        tmp_path = tmp.name
    try:
        table = pq.read_table(tmp_path, columns=["prompt", "response"])
        for batch in table.to_batches():
            yield project_record(batch)
    finally:
        os.unlink(tmp_path)

def stream_jsonl(url: str):
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        try:
            obj = json.loads(line)
            yield {k: obj.get(k) for k in ("prompt", "response") if k in obj}
        except json.JSONDecodeError:
            continue

def worker(manifest_path: str, shard_id: int, total_shards: int, out_dir: Path):
    with open(manifest_path) as f:
        manifest = json.load(f)

    repo = manifest["repo"]
    date = manifest["date"]
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    outfile = out_dir / f"shard{shard_id}-{ts}.jsonl"

    store = DedupStore()
    total_in = 0
    total_out = 0

    for info in tqdm(manifest["files"], desc=f"Shard {shard_id}"):
        path = info["path"]
        if shard_for(path, total_shards) != shard_id:
            continue

        url = CDN_TEMPLATE.format(repo=repo, path=path)
        total_in += 1

        try:
            if path.endswith(".parquet"):
                records = stream_parquet(url)
            elif path.endswith(".jsonl"):
                records = stream_jsonl(url)
            else:
                continue

            for rec in records:
                if not rec.get("prompt") or not rec.get("response"):
                    continue
                md5 = hashlib.md5(
                    f"{rec['prompt']}\n{rec['response']}".encode()
                ).hexdigest()
                if store.exists(md5):
                    continue
                store.add(md5)
                with outfile.open("a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                total_out += 1
        except Exception as exc:
            print(f"Error processing {path}: {exc}", file=sys.stderr)
            continue

    print(f"Shard {shard_id}: processed {total_in} files, wrote {total_out} pairs to {outfile}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: SHARD_ID=0 TOTAL_SHARDS=16 python bin/worker.py manifest.json")
        sys.exit(1)
    manifest_path = sys.argv[1]
    shard_id = int(os.getenv("SHARD_ID", "0"))
    total_shards = int(os.getenv("TOTAL

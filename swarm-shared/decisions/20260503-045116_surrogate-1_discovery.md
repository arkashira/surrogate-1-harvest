# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/gen-manifest.py`** — run once per cron (Mac/CI) before workers start:
   - Uses HF API **once** with `list_repo_tree(path=date_folder, recursive=False)` to list parquet/jsonl files.
   - Writes `manifest.json` to repo root (or uploads to dataset repo as a small pointer file) so workers need no API access.

2. **Add `bin/worker.py`** — single, deterministic shard worker that:
   - Reads the pre-generated `manifest.json`.
   - Downloads only its 1/16 slice via raw CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) — zero API calls during ingest.
   - Projects each file to `{prompt, response}` at parse time, ignoring heterogeneous metadata columns.
   - Computes per-row md5 for dedup (reuses `lib/dedup.py` semantics) and streams output to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

3. **Update `.github/workflows/ingest.yml`**:
   - Add a prior job step that runs `gen-manifest.py` and uploads `manifest.json` as an artifact.
   - Pass `SHARD_ID` and `MANIFEST_DATE` to each matrix job.
   - Each job runs `python bin/worker.py --shard $SHARD_ID --date $MANIFEST_DATE`.

4. **Update `requirements.txt`**:
   - Add `requests`, keep `pyarrow`, `numpy`, `huggingface_hub` (only for initial listing).

5. **Keep `lib/dedup.py`** unchanged (or lightly adapt) — central md5 store semantics preserved.

---

## Code Snippets

### `bin/gen-manifest.py`
```python
#!/usr/bin/env python3
"""
Generate manifest.json for a date folder.
Run once per cron (Mac/CI) before workers start.
Usage:
  python bin/gen-manifest.py --repo axentx/surrogate-1-training-pairs --date 2026-05-03
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Folder name, e.g. 2026-05-03")
    parser.add_argument("--out", default="manifest.json")
    args = parser.parse_args()

    api = HfApi()
    # Single API call; recursive=False avoids pagination explosion.
    files = api.list_repo_tree(repo_id=args.repo, path=args.date, recursive=False)
    # Accept common training file patterns.
    paths = [f.rfilename for f in files if f.rfilename.endswith((".parquet", ".jsonl", ".json"))]

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "files": sorted(paths),
        "cdn_prefix": f"https://huggingface.co/datasets/{args.repo}/resolve/main/{args.date}"
    }

    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Wrote {len(paths)} files to {args.out}")

if __name__ == "__main__":
    main()
```

### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
CDN-bypass shard worker.
Usage:
  python bin/worker.py --shard 3 --date 2026-05-03 --out-dir batches/public-merged
"""
import argparse
import hashlib
import json
import os
import sys
from typing import Any, Dict, Iterable

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests

# Reuse dedup semantics from lib/dedup.py (inline for portability).
def row_md5(prompt: str, response: str) -> str:
    return hashlib.md5(f"{prompt}\0{response}".encode("utf-8")).hexdigest()

def parse_parquet(path: str) -> Iterable[Dict[str, str]]:
    try:
        pf = pq.ParquetFile(path)
    except Exception as exc:
        print(f"Skipping unreadable parquet {path}: {exc}", file=sys.stderr)
        return
    for batch in pf.iter_batches(batch_size=1024, columns=["prompt", "response"]):
        tbl = pa.Table.from_batches([batch])
        df = tbl.to_pydict()
        for prompt, response in zip(df.get("prompt", []), df.get("response", [])):
            if isinstance(prompt, str) and isinstance(response, str):
                yield {"prompt": prompt, "response": response}

def parse_jsonl(path: str) -> Iterable[Dict[str, str]]:
    with open(path, "r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer")
            if isinstance(prompt, str) and isinstance(response, str):
                yield {"prompt": prompt, "response": response}

def download_cdn(url: str, dest: str) -> None:
    # No Authorization header -> bypasses /api/ rate limits.
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

def worker(shard_id: int, n_shards: int, date: str, manifest_path: str, out_dir: str) -> None:
    with open(manifest_path, "r", encoding="utf-8") as fp:
        manifest = json.load(fp)

    files = manifest["files"]
    cdn_prefix = manifest["cdn_prefix"]
    os.makedirs(out_dir, exist_ok=True)

    # Deterministic shard assignment.
    my_files = [f for i, f in enumerate(files) if i % n_shards == shard_id]
    if not my_files:
        print("No files assigned to this shard.")
        return

    stamp = __import__("datetime").datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(out_dir, date, f"shard{shard_id}-{stamp}.jsonl")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    seen = set()
    written = 0
    tmp_dir = f"/tmp/surrogate_worker_{shard_id}"
    os.makedirs(tmp_dir, exist_ok=True)

    for fname in my_files:
        url = f"{cdn_prefix}/{fname}"
        local_path = os.path.join(tmp_dir, os.path.basename(fname))
        try:
            download_cdn(url, local_path)
        except Exception as exc:
            print(f"Failed to download {url}: {exc}", file=sys.stderr)
            continue

        if fname.endswith(".parquet"):
            rows = parse_parquet(local_path)
        else:
            rows = parse_jsonl(local_path)

        for row in rows:
            md5 = row_md5(row["prompt"], row["response"])
            if md5 in seen:
                continue
            seen.add(md5)
            # Central dedup store could be called here if cross-run dedup is required.
            # For now, per-run dedup prevents immediate duplicates in this shard file.
            with open(out_path, "a", encoding="utf-8") as out_fp:
                out_fp.write(json.dumps({"prompt": row["prompt"], "response": row["response"]}, ensure_ascii=False) + "\n")
            written += 1

        os.unlink(local_path)

    print(f"Shard {shard_id}: wrote {written} rows to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

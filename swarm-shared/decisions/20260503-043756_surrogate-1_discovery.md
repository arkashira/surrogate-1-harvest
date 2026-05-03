# surrogate-1 / discovery

## Final Implementation Plan (≤2 h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-only Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema `CastError`.

### Core design decisions (resolve contradictions)
- **CDN-only, zero API calls during training** (C1) is mandatory for reliability at scale; keep ingestion matrix but make each shard use the same pre-built manifest.  
- **Deterministic sharding by filename hash** (C2) is required for reproducible multi-GPU/distributed runs and to avoid double-counting or gaps.  
- **Schema projection at read time** (C1) is required to prevent `CastError`; drop all columns except `prompt`/`response`.  
- **Keep ingestion matrix** for parallelism, but **add a nightly manifest job** (C1) so training never hits API limits.  
- **Lightning Studio reuse + idle restart** (C1) is kept for cost and velocity.

---

### Changes (ordered by impact)

1. **Add `train_manifest.json` generation**  
   - Single API call via `list_repo_tree(..., recursive=False)` for one date folder.  
   - Save `{"date": "YYYY-MM-DD", "folder": "...", "files": [...]}` to repo root or `manifests/YYYY-MM-DD.json`.  
   - Exits non-zero on 429 with exponential backoff.

2. **Add `src/worker.py` (CDN-only, schema-projected, deterministic sharding)**  
   - Reads manifest, downloads via raw CDN URLs (no auth).  
   - Projects to `{prompt, response}` only.  
   - Deterministic sharding: `shard_id = hash(filename) % num_shards`.  
   - Streams via `pyarrow` (parquet/csv) in chunks to keep RAM low.  
   - Writes `shard-<id>.jsonl` for downstream training.

3. **Update GitHub Actions (`ingest.yml`)**  
   - Keep 16-shard matrix for ingestion.  
   - Pass `SHARD_ID` and `MANIFEST_PATH`.  
   - Run `python src/worker.py` instead of brittle shell scripts.  
   - Thin `bin/dataset-enrich.sh` wrapper preserved for backward compatibility (calls worker).

4. **Add nightly manifest job**  
   - Runs `scripts/gen_manifest.py` and commits manifest to repo.  
   - Scheduled nightly 02:00 UTC + manual trigger.

5. **Update `train.py` to use CDN dataset**  
   - Replace `load_dataset(streaming=True)` with `CdnDataset(manifest_path, shard_id, num_shards)`.  
   - Deterministic sharding at dataset level for multi-GPU.

6. **Lightning Studio reuse + idle restart**  
   - Reuse running studio by name; auto-restart on idle with `Machine.L40S`.

7. **Update README**  
   - Document new manifest-first flow and CDN-bypass guarantee.

---

### Code Snippets

#### 1. `scripts/gen_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate train_manifest.json for a date folder.
Usage: python gen_manifest.py axentx/surrogate-1-training-pairs 2026-05-03
"""
import json, sys, time
from pathlib import Path
from huggingface_hub import HfApi

API = HfApi()
REPO = sys.argv[1]
DATE = sys.argv[2]
FOLDER = f"batches/public-merged/{DATE}"
OUT = Path("manifests") / f"{DATE}.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

def backoff(attempt):
    t = 360 if attempt == 1 else 60 * attempt
    print(f"Rate limited — sleeping {t}s", file=sys.stderr)
    time.sleep(t)

for attempt in range(1, 4):
    try:
        tree = API.list_repo_tree(REPO, path=FOLDER, recursive=False)
        files = [item.path for item in tree if item.type == "file"]
        manifest = {"date": DATE, "folder": FOLDER, "files": sorted(files)}
        OUT.write_text(json.dumps(manifest, indent=2))
        print(f"Wrote {len(files)} files to {OUT}")
        break
    except Exception as e:
        if "429" in str(e):
            backoff(attempt)
        else:
            raise
```

#### 2. `src/data/cdn_loader.py`
```python
from pathlib import Path
import json, hashlib, requests, pyarrow.csv as pc, pyarrow.parquet as pq
from torch.utils.data import IterableDataset

CDN = "https://huggingface.co/datasets"

class CdnDataset(IterableDataset):
    def __init__(self, manifest_path, shard_id=0, num_shards=1):
        manifest = json.loads(Path(manifest_path).read_text())
        self.folder = manifest["folder"]
        # deterministic sharding by filename
        files = sorted(manifest["files"])
        self.files = [
            f for f in files
            if hashlib.md5(f.encode()).digest()[0] % num_shards == shard_id
        ]
        self.shard_id = shard_id
        self.num_shards = num_shards

    def __iter__(self):
        for fname in self.files:
            url = f"{CDN}/{self.folder}/{fname}"
            try:
                with requests.get(url, stream=True) as r:
                    r.raise_for_status()
                    if fname.endswith(".parquet"):
                        table = pq.read_table(pq.ParquetFile(r.raw))
                    else:
                        # project only prompt/response to avoid CastError
                        table = pc.read_csv(
                            r.raw,
                            read_options=pc.ReadOptions(column_names=["prompt", "response"]),
                            parse_options=pc.ParseOptions(delimiter="\t"),
                        )
                    for batch in table.to_batches(max_chunksize=1024):
                        cols = batch.to_pydict()
                        for p, r_ in zip(cols.get("prompt", []), cols.get("response", [])):
                            if p and r_:
                                yield {"prompt": p, "response": r_}
            except Exception as exc:
                print(f"Skipping {url}: {exc}")
                continue
```

#### 3. `src/worker.py`
```python
#!/usr/bin/env python3
import argparse, json, hashlib, sys
from pathlib import Path
import pyarrow.csv as pc, pyarrow.parquet as pq, requests

CDN = "https://huggingface.co/datasets"

def shard_id(filename, num_shards):
    return hashlib.md5(filename.encode()).digest()[0] % num_shards

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--num-shards", type=int, default=16)
    parser.add_argument("--out-dir", default="batches/public-merged")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    folder = manifest["folder"]
    out_dir = Path(args.out_dir) / manifest["date"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shard-{args.shard_id}.jsonl"

    count = 0
    with out_path.open("w", encoding="utf-8") as fout:
        for fname in sorted(manifest["files"]):
            if shard_id(fname, args.num_shards) != args.shard_id:
                continue
            url = f"{CDN}/{folder}/{fname}"
            try:
                with requests.get(url, stream=True) as r:
                    r.raise_for_status()
                    if fname.endswith(".parquet"):
                        table = pq.read_table(pq.ParquetFile(r.raw))
                    else:
                        table = pc.read_csv(
                            r.raw,
                            read_options=pc.ReadOptions(column_names=["prompt", "response"]),
                            parse_options=pc.ParseOptions(delimiter="\t"),
                        )
                    for batch in table.to_batches(max_chunksize=1024):
                        cols = batch.to_pydict()
                        for p, r_ in zip(cols.get("prompt", []), cols.get("response", [])):
                            if p and r_:
                                fout.write(json.dumps({"prompt": p, "response": r_

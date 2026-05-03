# surrogate-1 / backend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree` call (from Mac orchestrator) → `file-list-<DATE>.json` committed to repo; worker loads this manifest and processes only its shard
- Uses **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) for all file downloads — zero API/auth calls during training/ingest, avoids 429 rate limits
- Projects heterogeneous schemas to `{prompt, response}` at parse time (no `load_dataset(streaming=True)` on mixed schemas)
- Deduplicates via central `lib/dedup.py` md5 store (same as existing)
- Writes `batches/public-merged/<DATE>/shard<N>-<HHMMSS>.jsonl` with deterministic naming to prevent collisions
- Includes a small Mac-side orchestrator script (`bin/build-manifest.py`) to generate the file list once per date folder and commit it (so workers never call `list_repo_tree`/paginate)

### File changes

```
bin/
  dataset-enrich.py        # new worker (replaces .sh)
  build-manifest.py        # Mac orchestrator: list -> JSON
  lib/
    dedup.py               # unchanged
.github/workflows/
  ingest.yml               # updated to pass DATE and use python worker
```

---

## Code snippets

### 1. `bin/build-manifest.py` (run on Mac before cron kicks off)

```python
#!/usr/bin/env python3
"""
Generate file-list-<DATE>.json for a given DATE folder in the dataset repo.
Run from Mac (or CI once per day) to avoid per-worker list_repo_tree calls.
Usage:
  HF_TOKEN=... python build-manifest.py axentx/surrogate-1-training-pairs 2026-05-03
"""
import json, os, sys
from datetime import datetime
from huggingface_hub import HfApi

def main():
    if len(sys.argv) != 3:
        print("Usage: build-manifest.py <repo_id> <DATE>")
        sys.exit(1)

    repo_id, date_str = sys.argv[1], sys.argv[2]
    api = HfApi(token=os.environ.get("HF_TOKEN"))

    # List top-level for the date folder (non-recursive to avoid pagination explosion)
    entries = api.list_repo_tree(repo_id, path=date_str, recursive=False)

    files = []
    for e in entries:
        if not e.path.endswith((".parquet", ".jsonl", ".json")):
            continue
        files.append({
            "path": e.path,          # e.g. 2026-05-03/file1.parquet
            "size": getattr(e, "size", None),
            "rfilename": e.rfilename
        })

    out_name = f"file-list-{date_str}.json"
    with open(out_name, "w") as f:
        json.dump({"date": date_str, "repo_id": repo_id, "files": files}, f, indent=2)

    print(f"Wrote {len(files)} files -> {out_name}")

if __name__ == "__main__":
    main()
```

Make executable and commit generated manifest alongside workflow (or commit it to repo so workers fetch it via raw GitHub URL or repo file).

---

### 2. `bin/dataset-enrich.py` (worker)

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Deterministic sharding by slug-hash.

Env:
  SHARD_ID=0..15
  SHARD_TOTAL=16
  DATE=2026-05-03
  HF_TOKEN=...
  MANIFEST_URL_OR_PATH (optional) — if not provided, uses file-list-<DATE>.json in repo
"""
import hashlib, json, os, sys, time, requests
from pathlib import Path

HF_API = "https://huggingface.co"
CDN_ROOT = "https://huggingface.co/datasets"

def slug_for_path(path: str) -> str:
    # Deterministic slug used for sharding and dedup
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0]

def shard_for(slug: str, total: int) -> int:
    h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    return h % total

def parse_record(file_path: str, raw_bytes: bytes):
    """Project heterogeneous file to (prompt, response)."""
    # Minimal projection: try parquet/jsonl/json heuristics.
    # In practice expand per known schema; keep lightweight.
    if file_path.endswith(".parquet"):
        import pyarrow.parquet as pq
        import io
        tbl = pq.read_table(io.BytesIO(raw_bytes))
        df = tbl.to_pandas()
        # Heuristic column names
        prompt_col = next((c for c in df.columns if "prompt" in c.lower()), df.columns[0])
        response_col = next((c for c in df.columns if "response" in c.lower() or "completion" in c.lower()), df.columns[-1])
        for _, row in df.iterrows():
            yield {"prompt": str(row[prompt_col]), "response": str(row[response_col])}
        return

    if file_path.endswith(".jsonl"):
        import io
        for line in io.BytesIO(raw_bytes).read().splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or list(obj.values())[0]
            response = obj.get("response") or obj.get("output") or obj.get("answer") or list(obj.values())[-1]
            yield {"prompt": str(prompt), "response": str(response)}
        return

    if file_path.endswith(".json"):
        obj = json.loads(raw_bytes)
        if isinstance(obj, list):
            for item in obj:
                prompt = item.get("prompt") or item.get("input") or list(item.values())[0]
                response = item.get("response") or item.get("output") or list(item.values())[-1]
                yield {"prompt": str(prompt), "response": str(response)}
        else:
            prompt = obj.get("prompt") or obj.get("input") or list(obj.values())[0]
            response = obj.get("response") or obj.get("output") or list(obj.values())[-1]
            yield {"prompt": str(prompt), "response": str(response)}
        return

    # Fallback: skip
    return

def main():
    shard_id = int(os.environ.get("SHARD_ID", 0))
    shard_total = int(os.environ.get("SHARD_TOTAL", 16))
    date_str = os.environ.get("DATE")
    hf_token = os.environ.get("HF_TOKEN", "")
    if not date_str:
        print("DATE env required")
        sys.exit(1)

    # Load manifest (committed file or provided path)
    manifest_path = os.environ.get("MANIFEST_PATH", f"file-list-{date_str}.json")
    if not os.path.exists(manifest_path):
        # fallback: try to fetch from repo raw (if manifest committed)
        repo_id = os.environ.get("REPO_ID", "axentx/surrogate-1-training-pairs")
        manifest_url = f"{CDN_ROOT}/{repo_id}/resolve/main/{manifest_path}"
        r = requests.get(manifest_url, timeout=30)
        if r.status_code != 200:
            print(f"Manifest not found locally or via CDN: {manifest_path}")
            sys.exit(1)
        manifest = r.json()
    else:
        with open(manifest_path) as f:
            manifest = json.load(f)

    repo_id = manifest.get("repo_id", "axentx/surrogate-1-training-pairs")
    files = manifest.get("files", [])
    if not files:
        print("No files in manifest")
        sys.exit(0)

    # Import dedup lazily (assumes lib/ on PYTHONPATH)
    sys.path.insert(0, str(Path(__file__).parent / "lib"))
    from dedup import DedupStore

    dedup = DedupStore()
    out_dir = Path("batches/public

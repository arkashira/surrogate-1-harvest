# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### What we change
- Replace `bin/dataset-enrich.sh` with `bin/dataset_enrich.py`
- Single Mac-side manifest generation (`list_repo_tree` once, save JSON)
- Worker uses CDN-only downloads (`resolve/main/...`) — zero API/auth calls during stream
- Deterministic shard assignment via `hash(slug) % 16`
- Project to `{prompt, response}` only; write `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`
- Keep `lib/dedup.py` as central md5 store (SQLite)
- Preserve GitHub Actions 16-shard matrix; each runner invokes same script with `SHARD_ID`

### Why this matters
- Avoids HF API 429 during ingestion (CDN bypass)
- Avoids `load_dataset(streaming=True)` mixed-schema CastError
- Avoids recursive `list_repo_files` pagination (100x) — use `list_repo_tree` per folder
- Eliminates shell quoting/portability bugs
- Enables deterministic shard assignment and manifest reuse across runs

---

### Code changes

#### 1) New: `bin/manifest.py` (run once on Mac after rate-limit window)
```python
#!/usr/bin/env python3
"""
Generate manifest for a date folder.
Usage:
  python bin/manifest.py --repo axentx/surrogate-1-training-pairs \
                         --date 2026-05-03 \
                         --out manifest-2026-05-03.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("pip install huggingface_hub")
    sys.exit(1)

HF_TOKEN = os.getenv("HF_TOKEN")

def build_manifest(repo: str, date: str, out_path: str):
    # date folder at repo root, e.g. raw/2026-05-03/
    path = f"raw/{date}"
    print(f"Listing {repo}/{path} ...")
    tree = list_repo_tree(
        repo_id=repo,
        path=path,
        recursive=True,
        token=HF_TOKEN,
    )
    files = [item.path for item in tree if item.type == "file"]
    manifest = {
        "repo": repo,
        "date": date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": sorted(files),
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files -> {out_path}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--out", default="manifest.json")
    args = p.parse_args()
    build_manifest(args.repo, args.date, args.out)
```

#### 2) Replace `bin/dataset-enrich.sh` → `bin/dataset_enrich.py`
```python
#!/usr/bin/env python3
"""
Per-shard worker (GitHub Actions matrix).
Uses CDN bypass for downloads; projects to {prompt, response}.
Writes shard-N output to batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl

Env:
  SHARD_ID=0..15
  MANIFEST=manifest-2026-05-03.json
  HF_TOKEN=...
  HF_DATASET_REPO=axentx/surrogate-1-training-pairs
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as e:
    print(f"Missing dep: {e}")
    print("pip install requests pyarrow")
    sys.exit(1)

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore

CDN_ROOT = "https://huggingface.co/datasets"
BATCH_DIR = "batches/public-merged"

def shard_for(slug: str, n_shards: int = 16) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % n_shards

def cdn_download(url: str, timeout: int = 30) -> bytes:
    # No Authorization header -> CDN tier, bypasses API rate limits
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def parse_file_to_pairs(content: bytes, file_path: str):
    """
    Handle heterogeneous schemas:
    - parquet: read table, project to prompt/response
    - json/jsonl: attempt to extract prompt/response fields
    Yields (prompt, response, md5_of_raw)
    """
    suffix = file_path.lower()

    if suffix.endswith(".parquet"):
        try:
            table = pq.read_table(pa.BufferReader(content))
            df = table.to_pandas()
        except Exception as e:
            print(f"Parquet decode failed {file_path}: {e}")
            return

        # Common field names
        prompt_col = None
        response_col = None
        for c in df.columns:
            cl = c.lower()
            if "prompt" in cl:
                prompt_col = c
            if "response" in cl or "completion" in cl or "output" in cl:
                response_col = c

        if prompt_col and response_col:
            for _, row in df.iterrows():
                p = str(row[prompt_col])
                r = str(row[response_col])
                raw = f"{p}\n{r}".encode()
                yield (p, r, hashlib.md5(raw).hexdigest())
        else:
            print(f"Could not find prompt/response cols in {file_path}")

    elif suffix.endswith((".json", ".jsonl")):
        text = content.decode("utf-8", errors="replace").strip()
        lines = text.splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue

            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
            response = obj.get("response") or obj.get("output") or obj.get("answer")
            if prompt and response:
                p = str(prompt)
                r = str(response)
                raw = f"{p}\n{r}".encode()
                yield (p, r, hashlib.md5(raw).hexdigest())
    else:
        print(f"Unsupported file type: {file_path}")

def run_shard(manifest_path: str, shard_id: int):
    with open(manifest_path) as f:
        manifest = json.load(f)

    repo = manifest["repo"]
    date = manifest["date"]
    files = manifest["files"]

    my_files = [f for f in files if shard_for(f, 16) == shard_id]
    print(f"Shard {shard_id}: processing {len(my_files)} files")

    dedup = DedupStore()
    out_rows = []
    processed = 0
    skipped_dup = 0

    for file_path in my_files:
        # CDN URL (no auth header -> CDN tier)
        url = f"{CDN_ROOT}/{repo}/resolve/main/{file_path}"
        try:
            content = cdn_download(url)
        except Exception as e:
            print(f"Download failed {file_path}: {e}")
            continue

        for prompt, response, md5hex in parse_file_to_pairs(content, file_path):
            if dedup.exists(md5hex):
                skipped_dup += 1
                continue

            out_rows.append({
                "prompt": prompt,
                "response": response,
                "md5": md5hex,
                "source_file": file_path,
                "ingest_ts":

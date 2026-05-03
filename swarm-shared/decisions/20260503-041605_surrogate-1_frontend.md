# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. **Add `bin/worker.py`** — single-file worker that:
   - Accepts `SHARD_ID` and `N_SHARDS` (default 16) via env.
   - Uses one HF API call (`list_repo_tree`) to list top-level date folders in `batches/public-merged/`.
   - Deterministically assigns date folders to shards by hash.
   - For each assigned date folder, lists parquet files and downloads via CDN (`resolve/main/...`) with optional `HF_TOKEN` for private repos.
   - Projects each parquet to `{prompt, response}` with schema fallback (`prompt/input/question` × `response/output/answer`).
   - Streams NDJSON output for memory-safe, large-scale ingestion.

2. **Add `bin/build_manifest.py`** — optional dev helper that lists a specific date folder via HF API (respecting rate limits) and emits `manifest.json` for local testing or CI.

3. **Update `bin/dataset-enrich.sh`** — thin wrapper that invokes `worker.py` so GitHub Actions matrix keeps working.

4. **Add `requirements.txt` entries** if missing (`requests`, `pyarrow`, `tqdm`).

5. **Add `.github/workflows/ingest.yml`** (if absent) — 16-shard matrix, uses `HF_TOKEN`, passes `SHARD_ID`/`N_SHARDS`, uploads per-shard NDJSON as artifacts or pushes to a dataset branch.

### Why this wins
- **CDN bypass**: training uses `https://huggingface.co/datasets/.../resolve/main/...` — zero API calls during data loading, no 429s.
- **Schema safety**: project to `{prompt, response}` only at parse time; never rely on dataset streaming with heterogeneous files.
- **Deterministic sharding**: `hash(date_folder) % N_SHARDS` → shard id prevents collisions across runs and balances load.
- **Lightning reuse**: embed manifest or direct CDN paths in `train.py`; Lightning workers fetch via CDN only, no HF API quota burn.

---

## Code Snippets

### `bin/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Run on Mac (or any dev machine) after HF API rate-limit window clears.
Produces manifest.json for a single date folder.

Usage:
  HF_TOKEN=hf_xxx python bin/build_manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-04-30 \
    --out manifest.json
"""
import argparse
import json
import os
import sys
from typing import List, Dict

from huggingface_hub import HfApi, login

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="Folder under datasets, e.g. 2026-04-30")
    parser.add_argument("--out", default="manifest.json")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        print("HF_TOKEN required", file=sys.stderr)
        sys.exit(1)
    login(token=token)

    api = HfApi()
    folder = f"batches/public-merged/{args.date}"
    try:
        entries = api.list_repo_tree(repo_id=args.repo, path=folder, recursive=False)
    except Exception as e:
        print(f"Failed to list {args.repo}/{folder}: {e}", file=sys.stderr)
        sys.exit(1)

    files: List[Dict[str, str]] = []
    for entry in entries:
        if getattr(entry, "type", None) != "file":
            continue
        path = getattr(entry, "path", None)
        if not path or not path.endswith(".parquet"):
            continue
        cdn_url = f"https://huggingface.co/datasets/{args.repo}/resolve/main/{path}"
        files.append({"path": path, "cdn_url": cdn_url})

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "files": files,
        "generated_by": "bin/build_manifest.py",
    }

    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

### `bin/worker.py`
```python
#!/usr/bin/env python3
"""
GitHub Actions worker (one shard). Deterministic shard assignment via hash(date_folder) % N_SHARDS.
Lists date folders under batches/public-merged/, assigns shards, then downloads parquet via CDN
and projects rows to {prompt, response}.

Usage:
  SHARD_ID=3 N_SHARDS=16 HF_TOKEN=hf_xxx python bin/worker.py --out shard-3.jsonl
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Generator, List

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, login
from tqdm import tqdm

CDN_TIMEOUT = 30

def date_folder_slug(path: str) -> str:
    # e.g. batches/public-merged/2026-04-30 -> 2026-04-30
    parts = Path(path).parts
    if len(parts) >= 3 and parts[0] == "batches" and parts[1] == "public-merged":
        return parts[2]
    return path

def shard_id(key: str, total_shards: int) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % total_shards

def project_to_pair(table: pa.Table) -> Generator[Dict[str, str], None, None]:
    cols = {c.lower(): c for c in table.column_names}
    prompt_col = cols.get("prompt") or cols.get("input") or cols.get("question")
    response_col = cols.get("response") or cols.get("output") or cols.get("answer")

    if not prompt_col or not response_col:
        return

    nrows = table.num_rows
    prompt_data = table.column(prompt_col).to_pylist()
    response_data = table.column(response_col).to_pylist()

    for i in range(nrows):
        prompt = prompt_data[i]
        response = response_data[i]
        if not isinstance(prompt, str) or not isinstance(response, str):
            continue
        prompt = prompt.strip()
        response = response.strip()
        if not prompt or not response:
            continue
        yield {"prompt": prompt, "response": response}

def list_date_folders(api: HfApi, repo: str) -> List[str]:
    base = "batches/public-merged"
    try:
        entries = api.list_repo_tree(repo_id=repo, path=base, recursive=False)
    except Exception as e:
        print(f"Failed to list {repo}/{base}: {e}", file=sys.stderr)
        return []
    folders = []
    for entry in entries:
        if getattr(entry, "type", None) == "dir":
            p = getattr(entry, "path", "")
            slug = date_folder_slug(p)
            if slug and slug != p:
                folders.append(slug)
    return sorted(set(folders))

def list_parquet_in_date(api: HfApi, repo: str, date_folder: str) -> List[Dict[str, str]]:
    folder = f"batches/public-merged/{date_folder}"
    try:
        entries = api.list_repo_tree(repo_id=repo, path=folder, recursive=False)
    except Exception as e:
        print(f"Failed to list {repo}/{folder}: {e}", file=sys.stderr)
        return []
    files = []
    for entry in entries:
        if getattr(entry, "type", None) != "file":
            continue
        path = getattr(entry, "path", "")
        if not path or not path.endswith(".parquet"):
            continue
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
        files.append({"path":

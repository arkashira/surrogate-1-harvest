# airship / discovery

## Highest-Value Incremental Improvement (<2h)

**CDN-first ingestion + deterministic HF sharding guard**  
Eliminates HF API rate limits during training and prevents 128/hr commit-cap ingestion failures. Ships as a single, reusable utility module + updated training script pattern.

---

## Implementation Plan

1. **Create `hf_cdn_ingest.py`** (reusable utility)
   - `list_date_folder(date: str) -> List[str]` — one-time API call to `list_repo_tree` for a date folder; cache to JSON.
   - `build_cdn_urls(file_paths: List[str]) -> List[str]` — convert to `https://huggingface.co/datasets/{repo}/resolve/main/{path}`.
   - `pick_shard_repo(slug: str, n_shards: int = 5) -> str` — deterministic hash → sibling repo (e.g., `repo`, `repo-1`, … `repo-4`).
   - `stream_cdn_parquet(url: str) -> Iterator[pa.RecordBatch]` — download via CDN with `pyarrow`/`requests` and project only `{prompt, response}` at parse time; ignore extra schema.

2. **Update training entrypoint (`train.py`)**  
   - Accept `--date-folder` and `--file-list-json` (pre-listed). If file list missing, call `list_date_folder` once and save.
   - Use `stream_cdn_parquet` in `DataLoader`; zero HF API calls during training.
   - On dataset write (if any), route through `pick_shard_repo` to spread commits.

3. **Studio guard wrapper**  
   - Before `.run()`, check `Teamspace.studios`; reuse running studio by name.
   - If stopped, restart with `target.start(machine=Machine.L40S)` (or fallback to free-tier size).

4. **Validation**
   - Run ingestion for one date folder locally; confirm CDN-only fetches and no 429s.
   - Verify deterministic shard selection across runs.

---

## Code Snippets

### `hf_cdn_ingest.py`

```python
# hf_cdn_ingest.py
# -*- coding: utf-8 -*-
#!/usr/bin/env bash
# ^ kept for accidental direct execution safety; this is a Python module
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import List, Iterator, Dict, Any

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from hugginggingface import HfApi  # type: ignore  # HF SDK

HF_API = HfApi()
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
DATE_FOLDER_TREE_CACHE = Path(".cache_hf_tree")
DATE_FOLDER_TREE_CACHE.mkdir(exist_ok=True)

def list_date_folder(repo: str, date_folder: str) -> List[str]:
    """
    One-time API call to list files in a date folder (non-recursive).
    Returns list of file paths relative to repo root.
    """
    cache_path = DATE_FOLDER_TREE_CACHE / f"{repo.replace('/', '_')}_{date_folder}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    items = HF_API.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    # items may be dicts with 'path'; normalize
    paths: List[str] = []
    for it in items:
        if isinstance(it, dict) and "path" in it:
            paths.append(it["path"])
        elif isinstance(it, str):
            paths.append(it)

    cache_path.write_text(json.dumps(paths, indent=2))
    return paths

def build_cdn_urls(repo: str, file_paths: List[str]) -> List[str]:
    return [CDN_TEMPLATE.format(repo=repo, path=p.lstrip("/")) for p in file_paths]

def pick_shard_repo(base_repo: str, slug: str, n_shards: int = 5) -> str:
    """
    Deterministic sibling repo selection.
    Example: base_repo='org/mirror' -> 'org/mirror-2'
    """
    digest = hashlib.sha256(slug.encode()).hexdigest()
    idx = int(digest, 16) % n_shards
    if idx == 0:
        return base_repo
    return f"{base_repo}-{idx}"

def stream_cdn_parquet(url: str, batch_size: int = 1024) -> Iterator[pa.RecordBatch]:
    """
    Download parquet via CDN and stream record batches.
    Projects only 'prompt' and 'response' columns if present;
    ignores heterogeneous schemas safely.
    """
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    with pa.BufferReader(resp.content) as buf:
        pf = pq.ParquetFile(buf)
        # Determine available projection fields
        schema_names = set(pf.schema.names)
        proj = []
        if "prompt" in schema_names:
            proj.append("prompt")
        if "response" in schema_names:
            proj.append("response")

        # If neither exists, stream all and let caller handle projection
        columns = proj if proj else None

        for batch in pf.iter_batches(batch_size=batch_size, columns=columns):
            yield batch

def cdn_parquet_to_dicts(url: str) -> Iterator[Dict[str, Any]]:
    """Convenience: yield {prompt, response} dicts from CDN parquet."""
    for batch in stream_cdn_parquet(url):
        cols = batch.columns
        col_map = {n: batch.column(n) for n in batch.schema.names}
        if "prompt" in col_map and "response" in col_map:
            prompts = col_map["prompt"].to_pylist()
            responses = col_map["response"].to_pylist()
            for p, r in zip(prompts, responses):
                yield {"prompt": p, "response": r}
        else:
            # fallback: yield rows as dicts
            for i in range(batch.num_rows):
                yield {k: v[i] for k, v in col_map.items()}
```

### Updated `train.py` snippet

```python
# train.py  (excerpt)
import argparse
import json
from pathlib import Path

from hf_cdn_ingest import list_date_folder, build_cdn_urls, cdn_parquet_to_dicts

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="org/dataset-mirror")
    parser.add_argument("--date-folder", required=True, help="e.g. batches/mirror-merged/2026-05-01")
    parser.add_argument("--file-list-json", help="Optional pre-listed file list JSON")
    args = parser.parse_args()

    if args.file_list_json:
        file_paths = json.loads(Path(args.file_list_json).read_text())
    else:
        file_paths = list_date_folder(args.repo, args.date_folder)
        # save for reuse
        Path("file_list.json").write_text(json.dumps(file_paths, indent=2))

    cdn_urls = build_cdn_urls(args.repo, file_paths)

    # Example: build HF dataset via generator that uses CDN only
    def gen():
        for url in cdn_urls:
            yield from cdn_parquet_to_dicts(url)

    # Use gen() with your training loader / HF Dataset.from_generator
    # dataset = Dataset.from_generator(gen, features={"prompt": Value("string"), "response": Value("string")})
    print(f"Prepared {len(cdn_urls)} CDN files for training (zero HF API calls during data load).")

if __name__ == "__main__":
    main()
```

### Studio guard snippet (Lightning SDK)

```python
# studio_guard.py
from lightning import Studio, Teamspace, Machine, Cloud

def get_or_start_studio(name: str, machine: Machine = Machine.L40S, cloud: Cloud = Cloud.AWS) -> Studio:
    # Reuse running studio to save quota
    for s in Teamspace.studios:
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {name}")
            return s

    # If exists but stopped, restart
    for s in Teamspace.studios:
        if s.name == name:
            print(f"Restarting stopped studio: {name}")
            s.start(machine=machine, cloud=cloud)
            return s

    # Create new
    print(f"Creating studio: {name}")
    return Studio(name=name,

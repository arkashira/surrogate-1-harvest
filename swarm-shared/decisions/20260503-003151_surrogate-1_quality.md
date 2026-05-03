# surrogate-1 / quality

## Implementation Plan — CDN-first snapshot + zero-HF-API ingestion

**Highest-value improvement (≤2h)**  
Add a Mac-side snapshot script that lists the target date-partition once, emits a deterministic `file_manifest.json`, and patch the Lightning training script to perform CDN-only fetches with zero HF API calls during data loading.

### Why this matters
- Avoids 429s from `list_repo_files`/`load_dataset` during long training runs.
- Uses HF CDN (`resolve/main/`) which has much higher rate limits and no auth requirement.
- Enables reproducible training runs (same manifest → same data).
- Fits the existing architecture: Mac orchestrates, Lightning trains, HF Space ingests.

---

### Steps (concrete, executable)

1. **Create snapshot script** (`bin/make_snapshot.py`)  
   - Runs on Mac (or any dev machine) after rate-limit window clears.
   - Uses `huggingface_hub.list_repo_tree(path=date_partition, recursive=False)` once.
   - Emits `file_manifest.json` with `{"files": ["path1.parquet", ...], "repo": "...", "date": "...", "snapshot_ts": "..."}`.
   - Deterministic ordering (sorted) so manifest is stable.

2. **Add lightweight CDN fetcher utility** (`src/cdn_loader.py`)  
   - Accepts `file_manifest.json` + repo name.
   - Builds URLs: `f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"`.
   - Downloads via `requests` or `urllib` with retries + backoff.
   - Streams parquet → project to `{prompt, response}` on read (avoid mixed-schema issues).
   - No `datasets.load_dataset` and no HF API auth during training.

3. **Patch Lightning training script** (`train.py`)  
   - Add CLI arg: `--manifest file_manifest.json`.
   - At startup, load manifest and use `cdn_loader` to fetch files locally (or into tmp dir).
   - Build `IterableDataset` that yields `{prompt, response}` pairs.
   - Remove any `load_dataset(streaming=True)` or recursive HF API calls.

4. **Reuse existing Lightning Studio**  
   - Before `Studio(create_ok=True)`, list `Teamspace.studios` and reuse a running one if available.
   - If stopped, restart with `target.start(machine=Machine.L40S)` (or fallback to available cloud account).
   - Avoids quota waste and idle-stop kills.

5. **Update CI/README**  
   - Add note: run `bin/make_snapshot.py` before training to generate manifest.
   - Document CDN bypass rationale and rate-limit avoidance.

---

### Code snippets

#### `bin/make_snapshot.py`
```python
#!/usr/bin/env python3
"""
Generate a deterministic file manifest for a date-partition
to enable CDN-only training fetches (zero HF API during training).
"""
import argparse
import json
import os
from datetime import datetime

from huggingface_hub import HfApi

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--partition", required=True, help="Path in repo, e.g. batches/public-merged/2026-05-03")
    parser.add_argument("--output", default="file_manifest.json")
    args = parser.parse_args()

    api = HfApi()
    # Single API call; recursive=False avoids pagination explosion
    entries = api.list_repo_tree(repo_id=args.repo, path=args.partition, recursive=False)
    files = sorted([e.path for e in entries if e.type == "file" and e.path.endswith(".parquet")])

    manifest = {
        "repo": args.repo,
        "partition": args.partition,
        "snapshot_ts": datetime.utcnow().isoformat() + "Z",
        "files": files,
    }

    with open(args.output, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {args.output}")

if __name__ == "__main__":
    main()
```

#### `src/cdn_loader.py`
```python
import json
import tempfile
from pathlib import Path
from typing import Iterator, Tuple

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

CDN_ROOT = "https://huggingface.co/datasets"

def cdn_url(repo: str, path: str) -> str:
    return f"{CDN_ROOT}/{repo}/resolve/main/{path}"

def stream_parquet_from_cdn(repo: str, path: str, columns=("prompt", "response")) -> Iterator[Tuple[str, str]]:
    url = cdn_url(repo, path)
    # Stream download to temp file to allow memory-mapped parquet read
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp_path = tmp.name
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=8192):
                    tmp.write(chunk)
            # Read only required columns; avoids mixed-schema issues
            table = pq.read_table(tmp_path, columns=columns)
            for row in table.to_pylist():
                yield row.get("prompt", ""), row.get("response", "")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

def load_manifest(path: str):
    with open(path) as f:
        return json.load(f)

def iter_dataset_from_manifest(manifest_path: str) -> Iterator[Tuple[str, str]]:
    manifest = load_manifest(manifest_path)
    repo = manifest["repo"]
    for p in tqdm(manifest["files"], desc="Fetching parquet via CDN"):
        yield from stream_parquet_from_cdn(repo, p)
```

#### Patch to `train.py` (minimal diff)
```python
# Add near imports
from src.cdn_loader import iter_dataset_from_manifest

# Add CLI
parser.add_argument("--manifest", help="Path to file_manifest.json for CDN-only loading")

# Replace HF dataset loading
if args.manifest:
    # CDN path: zero HF API calls during training
    pairs = list(iter_dataset_from_manifest(args.manifest))
    # Build torch Dataset from pairs ...
else:
    # fallback (optional) or error
    raise ValueError("--manifest required for CDN-first training")
```

---

### Acceptance checklist
- [ ] `bin/make_snapshot.py` executable and produces valid `file_manifest.json`.
- [ ] `src/cdn_loader.py` streams parquet via CDN and projects `{prompt, response}`.
- [ ] `train.py` accepts `--manifest` and uses CDN loader (no `load_dataset` during training).
- [ ] Lightning Studio reuse logic present (list studios, restart if stopped).
- [ ] README updated with snapshot step and CDN bypass rationale.

This ships in <2h and directly addresses HF rate limits and schema/mixed-file risks while aligning with existing patterns (CDN bypass, deterministic manifests, Lightning reuse).

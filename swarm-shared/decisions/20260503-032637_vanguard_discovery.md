# vanguard / discovery

## 1. Diagnosis
- No CDN-first manifest: ingestion/training can still trigger `list_repo_tree`/`load_dataset` at runtime → 429s and non-reproducible runs.
- No content-addressed pinning: training inputs are path-based, not hash-based → silent drift when upstream files change.
- Lightning quota burn: scripts likely create new studios instead of reusing running ones; idle-stop kills training without restart logic.
- Mixed-schema ingestion risk: raw files may still be loaded directly (HF API) instead of projecting to `{prompt,response}` after CDN download.
- No local file-list artifact: training script cannot run with zero API calls during data load.

## 2. Proposed change
Create `/opt/axentx/vanguard/discovery/001-cdn-manifest/` with:
- `list_and_pin.py` — one-off Mac script: `list_repo_tree` per date folder → write `manifest-{date}.json` with `{path, sha256, size, url}` (CDN resolve URLs).
- `train_cdn_only.py` — Lightning training entrypoint that reads `manifest-*.json`, downloads via CDN URLs (no HF API), verifies sha256, streams to surrogate-1 format.
- `reuse_or_start_studio.py` — wrapper that lists Teamspace studios, reuses running studio by name, or starts L40S (fallback to public tier) and survives idle-stop by checking status before each run.

## 3. Implementation
```bash
# Create directory
mkdir -p /opt/axentx/vanguard/discovery/001-cdn-manifest
cd /opt/axentx/vanguard/discovery/001-cdn-manifest
```

### 3.1 list_and_pin.py
```python
#!/usr/bin/env python3
"""
One-off: pin exact file list + content hashes for a date folder.
Run from Mac (or any machine with HF token).
Usage:
  HF_TOKEN=hf_xxx python list_and_pin.py \
    --repo my-org/surrogate-1-data \
    --date 2026-04-29 \
    --out manifest-2026-04-29.json
"""
import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from huggingface_hub import HfApi, list_repo_tree

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def fetch_and_hash(url: str, timeout: int = 30) -> dict:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    data = r.content
    return {"sha256": sha256_bytes(data), "size": len(data)}

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    api = HfApi(token=os.getenv("HF_TOKEN"))
    folder = f"batches/mirror-merged/{args.date}"
    entries = list_repo_tree(repo_id=args.repo, path=folder, recursive=False, token=api.token)

    files = [e for e in entries if e.type == "file" and e.path.endswith(".parquet")]
    if not files:
        print("No parquet files found under", folder)
        sys.exit(1)

    manifest = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {}
        for f in files:
            url = CDN_TEMPLATE.format(repo=args.repo, path=f.path)
            fut = ex.submit(fetch_and_hash, url)
            futures[fut] = (f.path, url)

        for fut in as_completed(futures):
            path, url = futures[fut]
            try:
                h = fut.result()
                manifest.append({
                    "path": path,
                    "url": url,
                    "sha256": h["sha256"],
                    "size": h["size"],
                })
                print("OK", path, h["sha256"][:8])
            except Exception as exc:
                print("FAIL", path, exc)

    manifest.sort(key=lambda x: x["path"])
    with open(args.out, "w") as fp:
        json.dump(manifest, fp, indent=2)
    print("Wrote", args.out, "files=", len(manifest))

if __name__ == "__main__":
    main()
```

### 3.2 train_cdn_only.py
```python
#!/usr/bin/env python3
"""
Lightning training entrypoint: CDN-only, zero HF API calls during data load.
Expects manifest JSON produced by list_and_pin.py in the same directory
or passed via env MANIFEST_PATH.
"""
import json
import os
import hashlib
from pathlib import Path
from typing import Iterator

import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import IterableDataset, DataLoader

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifest-2026-04-29.json")
CACHE_DIR = Path(os.getenv("CACHE_DIR", ".cdn_cache"))
CACHE_DIR.mkdir(exist_ok=True)

def verify_sha256(path: Path, expected: str) -> bool:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest() == expected

class CDNParquetIterable(IterableDataset):
    def __init__(self, manifest_path: str):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.worker_id = None

    def set_worker_id(self, worker_id: int):
        self.worker_id = worker_id

    def __iter__(self) -> Iterator[dict]:
        # Simple sharding by worker_id to avoid duplicate downloads across workers
        items = self.manifest
        if self.worker_id is not None:
            items = [it for idx, it in enumerate(items) if idx % int(os.getenv("NUM_WORKERS", "1")) == self.worker_id]

        for item in items:
            url = item["url"]
            expected = item["sha256"]
            slug = item["path"].replace("/", "_")
            local_path = CACHE_DIR / f"{slug}.parquet"

            if not local_path.exists() or not verify_sha256(local_path, expected):
                r = requests.get(url, stream=True, timeout=60)
                r.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                if not verify_sha256(local_path, expected):
                    raise RuntimeError(f"Hash mismatch for {url}")

            table = pq.read_table(local_path)
            # Project to surrogate-1 {prompt,response} only
            for batch in table.to_batches():
                cols = batch.column_names
                prompt_col = next((c for c in cols if "prompt" in c.lower()), None)
                response_col = next((c for c in cols if "response" in c.lower()), None)
                if prompt_col and response_col:
                    prompts = batch.column(prompt_col).to_pylist()
                    responses = batch.column(response_col).to_pylist()
                    for p, r in zip(prompts, responses):
                        if p is not None and r is not None:
                            yield {"prompt": str(p), "response": str(r)}

class DummyModel(torch.nn.Module):
    def forward(self, x):
        return x

def train_step(batch):
    # Placeholder: replace with surrogate-1 training logic
    return {"loss": torch.tensor(0.0)}

def main():
    dataset = CDNParquetIterable(MANIFEST_PATH)
    loader = DataLoader(dataset, batch_size=8, num_workers=0)  # keep num_workers=0 for simplicity in example

    for batch in loader:
        out = train_step(batch)
        print(out)

if __name__ == "__main__":
    main()
```

### 3.3 reuse_or

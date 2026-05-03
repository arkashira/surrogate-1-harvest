# surrogate-1 / quality

## Final Synthesis — CDN-first snapshot + zero-HF-API ingestion (single, actionable plan)

**Core goal**  
Eliminate HF API rate-limit risk during training by producing a deterministic file manifest once (on the Mac orchestrator) and having Lightning training fetch exclusively via CDN URLs. No HF API calls occur during dataload.

**Why this now (fits <2h)**  
- Single manifest script + small train patch + one runner script.  
- Uses HF CDN for downloads (not HF API), so rate limits do not apply to data fetching.  
- Deterministic shard-aware streaming keeps memory low and training reproducible.

---

### 1/4 — Manifest builder (run once per date folder)

```python
# scripts/build_file_manifest.py
#!/usr/bin/env python3
"""
Usage:
  python scripts/build_file_manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out manifest-2026-05-03.json

Produces manifest with CDN URLs and byte sizes.
Run once per date folder after rate-limit window clears.
"""
import argparse
import json
import os
import time
from huggingface_hub import HfApi

HF_API = HfApi()
CDN_TMPL = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date: str, out_path: str):
    folder = f"batches/public-merged/{date}"
    print(f"Listing {repo}/{folder} ...")
    entries = HF_API.list_repo_tree(repo=repo, path=folder, recursive=False)

    files = []
    for e in entries:
        if getattr(e, "type", None) != "file":
            continue
        if not getattr(e, "path", "").endswith((".jsonl", ".parquet")):
            continue
        cdn_url = CDN_TMPL.format(repo=repo, path=e.path)
        files.append({
            "path": e.path,
            "cdn_url": cdn_url,
            "size": getattr(e, "size", None),
        })

    files.sort(key=lambda x: x["path"])

    manifest = {
        "repo": repo,
        "date": date,
        "folder": folder,
        "created_ts": int(time.time()),
        "files": files,
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files -> {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, args.out)
```

---

### 2/4 — CDN stream loader (zero HF API during training)

```python
# data/cdn_stream.py
import json
import os
import pyarrow.parquet as pq
import pyarrow as pa
import requests
from typing import Iterator, Dict, Any, List

def read_manifest(manifest_path: str) -> List[Dict[str, Any]]:
    with open(manifest_path) as f:
        return json.load(f)["files"]

def cdn_lines(url: str) -> Iterator[str]:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if line:
                yield line

def project_to_pair(obj: Dict[str, Any]) -> Dict[str, str]:
    return {
        "prompt": str(obj.get("prompt", "")),
        "response": str(obj.get("response", "")),
    }

def stream_jsonl_cdn(files: List[Dict[str, Any]], shard_id: int = 0, n_shards: int = 1) -> Iterator[Dict[str, str]]:
    for idx, entry in enumerate(files):
        if idx % n_shards != shard_id:
            continue
        url = entry["cdn_url"]
        if url.endswith(".jsonl"):
            for line in cdn_lines(url):
                try:
                    obj = json.loads(line)
                    yield project_to_pair(obj)
                except Exception:
                    continue
        elif url.endswith(".parquet"):
            resp = requests.get(url, timeout=120)
            resp.raise_for_status()
            table = pq.read_table(pa.BufferReader(resp.content))
            df = table.to_pandas()
            for _, row in df.iterrows():
                yield project_to_pair(row.to_dict())

def make_cdn_dataset(manifest_path: str, shard_id: int = 0, n_shards: int = 1):
    files = read_manifest(manifest_path)
    return stream_jsonl_cdn(files, shard_id=shard_id, n_shards=n_shards)
```

---

### 3/4 — Lightning-compatible training patch (zero HF API)

```python
# training/train_cdn.py
import lightning as L
from lightning.pytorch.utilities import rank_zero_only
from data.cdn_stream import make_cdn_dataset

def get_or_create_studio(name: str, machine: L.Machine = L.Machine.L40S):
    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return L.Studio(name=name, machine=machine, create_ok=True)

def train_with_cdn(manifest_path: str, studio_name: str = "surrogate-1-cdn"):
    studio = get_or_create_studio(studio_name)
    if studio.status != "Running":
        print("Studio not running; restarting...")
        studio.start(machine=L.Machine.L40S)

    dataset = list(make_cdn_dataset(manifest_path, shard_id=0, n_shards=1))
    print(f"Loaded {len(dataset)} pairs via CDN (zero HF API calls)")

    # Replace with your DataModule/Model/Trainer
    # dm = YourDataModule(dataset)
    # model = YourModel()
    # trainer = L.Trainer(max_epochs=1, devices=1)
    # trainer.fit(model, dm)

    rank_zero_only(print)("Training step skipped (replace with your trainer)")

if __name__ == "__main__":
    import sys
    manifest = sys.argv[1] if len(sys.argv) > 1 else "manifest-2026-05-03.json"
    train_with_cdn(manifest)
```

---

### 4/4 — Runner script for Mac orchestrator

```bash
#!/usr/bin/env bash
# scripts/run_cdn_training.sh
set -euo pipefail
export SHELL=/bin/bash

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
MANIFEST="manifest-${DATE}.json"

echo "Building manifest for ${DATE} ..."
python scripts/build_file_manifest.py --repo "$REPO" --date "$DATE" --out "$MANIFEST"

echo "Starting CDN-based training ..."
python training/train_cdn.py "$MANIFEST"
```

---

### README update (concise)

Add a **CDN-first run order** section:

```
## CDN-first run order (bypass HF API rate limits)

1. Build manifest (once per date folder):
   python scripts/build_file_manifest.py --repo axentx/surrogate-1-training-pairs --date YYYY-MM-DD --out manifest-YYYY-MM-DD.json

2. Train using CDN-only loader:
   python training/train_cdn.py manifest-YYYY-MM-DD.json

Notes:
- Manifest lists CDN URLs; training downloads via CDN (no HF API calls during dataload).
- Deterministic sharding supported via shard_id/n_shards in make_cdn_dataset.
- Studio reuse avoids quota churn; script auto-restarts idle-stopped studios.
```

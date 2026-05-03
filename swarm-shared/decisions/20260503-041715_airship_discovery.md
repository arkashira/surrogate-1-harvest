# airship / discovery

## Final Consolidated Implementation Plan  
**Goal:** Eliminate HF API 429s during Surrogate training and prevent idle-timeout training loss by:  
1) Switching to a CDN-only data loader with a pre-listed, deterministic manifest.  
2) Adding Lightning Studio lifecycle resilience (reuse, restart, checkpointing, idle-stop recovery).  

**Estimated effort:** <2h  

---

## 1) Manifest generation (run once per date folder from Mac orchestration)

- Use deterministic listing of a single date folder.  
- Produce a `manifest.json` mapping shard filename → CDN URL.  
- Embed or mount manifest into the training container.

**`scripts/build_cdn_manifest.py`**
```python
#!/usr/bin/env python3
"""
Build a CDN-only manifest for one date folder.
Usage:
  python scripts/build_cdn_manifest.py \
    --repo "axentx/surrogate-dataset" \
    --date "2026-05-03" \
    --out "manifests/2026-05-03_shards.json"
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def main():
    parser = argparse.ArgumentParser(description="Build CDN manifest for a date folder")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. axentx/surrogate-dataset)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-05-03)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    api = HfApi()
    try:
        files = list(api.list_repo_tree(repo_id=args.repo, path=args.date, recursive=True))
    except Exception as e:
        print(f"Failed to list repo tree: {e}", file=sys.stderr)
        sys.exit(1)

    parquet_files = [f for f in files if f.path.endswith(".parquet")]
    if not parquet_files:
        print(f"No parquet files found under {args.date}", file=sys.stderr)
        sys.exit(1)

    # Deterministic ordering
    parquet_files.sort(key=lambda f: f.path)

    manifest = [
        {
            "path": f.path,
            "url": CDN_TEMPLATE.format(repo=args.repo, path=f.path),
        }
        for f in parquet_files
    ]

    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(manifest)} shards to {args.out}")

if __name__ == "__main__":
    main()
```

**One-shot Bash helper (optional)**
```bash
#!/usr/bin/env bash
# scripts/generate_manifest.sh
set -euo pipepipefail
REPO="${1:-axentx/surrogate-dataset}"
DATE="${2:-$(date +%Y-%m-%d)}"
OUT="${3:-manifests/${DATE}_shards.json}"
python scripts/build_cdn_manifest.py --repo "$REPO" --date "$DATE" --out "$OUT"
```

---

## 2) CDN-only dataset loader (deterministic shard iteration + local caching)

- Avoid `load_dataset` and HF API calls.  
- Download each parquet to a local cache once.  
- Stream rows deterministically; shuffle at epoch level via shard order randomization.  
- Support checkpointing by tracking shard index and row offset.

**`surrogate/training/data.py`**
```python
import json
import os
import random
import tempfile
import aiohttp
import asyncio
import pyarrow.parquet as pq
from typing import List, Dict, Optional
from torch.utils.data import IterableDataset

class CDNParquetIterable(IterableDataset):
    """
    Deterministic CDN parquet loader with local caching.
    Yields rows as dicts with keys like {"prompt": ..., "response": ...}.
    """

    def __init__(
        self,
        manifest_path: str,
        cache_dir: str = ".cache/parquet",
        shuffle_shards: bool = True,
        seed: int = 42,
        max_retries: int = 3,
        timeout: int = 30,
    ):
        super().__init__()
        with open(manifest_path) as f:
            self.manifest: List[Dict] = json.load(f)
        if not self.manifest:
            raise ValueError("Manifest is empty")
        self.cache_dir = cache_dir
        self.shuffle_shards = shuffle_shards
        self.seed = seed
        self.max_retries = max_retries
        self.timeout = timeout
        os.makedirs(self.cache_dir, exist_ok=True)

    def _local_path(self, url: str) -> str:
        name = os.path.basename(url.split("?")[0])
        return os.path.join(self.cache_dir, name)

    async def _download_one(self, url: str, dest: str) -> None:
        for attempt in range(1, self.max_retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                    async with session.get(url) as resp:
                        resp.raise_for_status()
                        with open(dest, "wb") as f:
                            f.write(await resp.read())
                return
            except Exception as e:
                if attempt == self.max_retries:
                    raise
                await asyncio.sleep(2 ** attempt)

    def _read_parquet(self, path: str) -> List[Dict]:
        table = pq.read_table(path, columns=["prompt", "response"])
        return table.to_pylist()

    def __iter__(self):
        worker_info = getattr(self, "__worker_info__", None)
        if worker_info is not None:
            # Deterministic per-worker shard selection
            worker_id = worker_info.id
            num_workers = worker_info.num_workers
            shard_indices = list(range(len(self.manifest)))
            if self.shuffle_shards:
                rng = random.Random(self.seed + worker_id)
                rng.shuffle(shard_indices)
            # Assign shards round-robin to workers
            my_indices = [i for idx, i in enumerate(shard_indices) if idx % num_workers == worker_id]
        else:
            my_indices = list(range(len(self.manifest)))
            if self.shuffle_shards:
                rng = random.Random(self.seed)
                rng.shuffle(my_indices)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        for idx in my_indices:
            item = self.manifest[idx]
            url = item["url"]
            dest = self._local_path(url)

            if not os.path.exists(dest):
                loop.run_until_complete(self._download_one(url, dest))

            rows = self._read_parquet(dest)
            random.Random(self.seed + idx).shuffle(rows)
            for row in rows:
                yield row
```

**Loader factory**
```python
from torch.utils.data import DataLoader

def make_cdn_loader(
    manifest_path: str,
    batch_size: int = 8,
    num_workers: int = 4,
    shuffle_shards: bool = True,
    seed: int = 42,
) -> DataLoader:
    dataset = CDNParquetIterable(
        manifest_path=manifest_path,
        shuffle_shards=shuffle_shards,
        seed=seed,
    )
    return DataLoader(
        dataset,
        batch_size=batch_size,
        num_workers=num_workers,
        # IterableDataset handles shuffling internally
    )
```

---

## 3) Lightning Studio resilient launcher (reuse, restart, checkpointing, idle-stop recovery)

- Reuse a running Studio; restart cleanly if stopped.  
- Upload manifest and training script.  
- Run training with epoch-level checkpointing to HF repo.  
- Monitor Studio status; on idle-stop, restart and resume from latest checkpoint.

**`surrogate/training/launcher.py`**
```python
#!/usr/bin/env python3
"""
Lightning Studio launcher with reuse + idle-restart resilience.
"""
import os
import sys
import time
import argparse
from lightning_sdk import Teamspace, Studio, Machine

TEAMSPACE = os.getenv("LIGHNING_TEAMSPACE", "axentx")
STUDIO

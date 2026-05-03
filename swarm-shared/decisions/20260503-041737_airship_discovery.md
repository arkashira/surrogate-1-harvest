# airship / discovery

## Final Actionable Plan (Synthesized)

**Goal:** Eliminate HF API 429s during Surrogate training by using a deterministic CDN file manifest + Lightning Studio lifecycle resilience (auto-restart on idle-stop). Ship in <2h as a single launcher + one-time manifest generator.

---

## 1) One-time: Generate CDN manifest (run from Mac after rate-limit window)

- Use `list_repo_tree` for one date folder (e.g., `batches/mirror-merged/2026-05-03/`).
- Save minimal deterministic manifest: `{repo, date, files: [{path, size}]}`.
- Commit `manifest.json` into the repo so training never calls the HF API for file listing.

**`tools/gen_cdn_manifest.py`**
```python
#!/usr/bin/env python3
"""
Generate CDN manifest for a date folder.
Run after HF API rate-limit window clears.
"""
import json
import os
import sys
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-dataset")
DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-05-03"
OUT = sys.argv[2] if len(sys.argv) > 2 else "manifest.json"

api = HfApi()
folder = f"batches/mirror-merged/{DATE}"
print(f"Listing {HF_REPO}/{folder} ...")

items = api.list_repo_tree(repo_id=HF_REPO, path=folder, recursive=False)

manifest = {
    "repo": HF_REPO,
    "date": DATE,
    "files": [
        {"path": it.path, "size": getattr(it, "size", None)}
        for it in items
        if it.path.lower().endswith(".parquet")
    ]
}

with open(OUT, "w") as f:
    json.dump(manifest, f, indent=2)
print(f"Wrote {len(manifest['files'])} files to {OUT}")
```

---

## 2) CDN-only DataLoader (deterministic, no API)

- Read `manifest.json` at startup.
- Build an `IterableDataset` that downloads via CDN URLs:
  `https://huggingface.co/datasets/{repo}/resolve/main/{path}`
- Stream + project to `{prompt, response}` on the fly; drop other columns.
- Use `aiohttp` + `asyncio` + bounded semaphore (16–32 concurrent) to saturate CDN without 429.
- Keep memory low: process one Parquet file at a time and yield rows.

**`surrogate/cdn_dataset.py`**
```python
import aiohttp
import asyncio
import pyarrow.parquet as pq
import io
import json
from torch.utils.data import IterableDataset
from typing import Iterator

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path: str, columns=("prompt", "response"), concurrency: int = 32):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.repo = self.manifest["repo"]
        self.files = [f["path"] for f in self.manifest["files"]]
        self.columns = columns
        self.concurrency = concurrency

    def _stream_file(self, session: aiohttp.ClientSession, path: str):
        url = CDN_TEMPLATE.format(repo=self.repo, path=path)
        return session.get(url)

    async def _process_one(self, session: aiohttp.ClientSession, path: str):
        async with self._stream_file(session, path) as resp:
            data = await resp.read()
            table = pq.read_table(io.BytesIO(data), columns=self.columns)
            df = table.to_pandas()
            for _, row in df.iterrows():
                yield {k: row[k] for k in self.columns if k in row}

    async def _async_iter(self):
        sem = asyncio.Semaphore(self.concurrency)

        async def bounded(p):
            async with sem:
                async for item in self._process_one(session, p):
                    yield item

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            tasks = [bounded(p) for p in self.files]
            for coro in asyncio.as_completed(tasks):
                async for item in await coro:
                    yield item

    def __iter__(self) -> Iterator[dict]:
        # Simple blocking wrapper; adapt if your trainer expects async.
        return asyncio.run(self._async_iter())
```

---

## 3) Lightning Studio resilient launcher (correct + actionable)

- Reuse a running studio if present (match by name + Running).
- If stopped, restart with `L40S` (fallback to `L40S` if `H200` unavailable).
- Before each run, refresh and ensure Running; restart if not.
- Upload only the training script and manifest (small, fast).
- Retry loop with clear exit on success; stop after N failures to avoid infinite loops.
- **Fix contradiction from Candidate 1:** use correct Lightning SDK patterns and avoid invalid `Machine` enum usage.

**`train_cdn_launcher.py`**
```python
#!/usr/bin/env python3
"""
Lightning-resilient launcher for CDN-only training.
Usage:
  python train_cdn_launcher.py \
    --manifest manifest.json \
    --script train.py \
    --machine L40S \
    --studio surrogate-train-cdn
"""
import argparse
import time
from pathlib import Path
from lightning_sdk import Teamspace, Studio, Machine

MACHINE_MAP = {"L40S": Machine.L40S, "H200": Machine.H200}

def find_or_create_studio(name: str, machine: Machine) -> Studio:
    team = Teamspace()
    for s in team.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s

    print(f"No running studio '{name}' found. Creating with {machine.name}...")
    return Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )

def wait_for_running(studio: Studio, timeout: int = 300, interval: int = 10) -> bool:
    for _ in range(timeout // interval):
        studio.refresh()
        if studio.status == "Running":
            return True
        time.sleep(interval)
    return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest.json")
    parser.add_argument("--script", required=True, help="Training script (e.g., train.py)")
    parser.add_argument("--machine", default="L40S", choices=["L40S", "H200"])
    parser.add_argument("--studio", default="surrogate-train-cdn")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).absolute()
    script_path = Path(args.script).absolute()
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")

    machine_enum = MACHINE_MAP[args.machine]
    studio = find_or_create_studio(args.studio, machine_enum)

    if not wait_for_running(studio):
        print("Studio failed to start in time.")
        return

    # Upload minimal required files
    studio.upload_file(str(script_path), script_path.name)
    studio.upload_file(str(manifest_path), manifest_path.name)

    cmd = f"python {script_path.name} --manifest {manifest_path.name}"

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        studio.refresh()
        if studio.status != "Running":
            print(f"Studio not running (attempt {attempt}). Restarting...")
            studio.start(machine=machine_enum)
            if not wait_for_running(studio):
                print("Studio restart failed.")
                return

        print(f"Running training (attempt {attempt}/{max_retries}): {cmd}")
        run = studio.run(command=cmd, cwd="/workspace")
        run.wait()

        if run.status == "completed":
            print("Training completed successfully.")
            return
       

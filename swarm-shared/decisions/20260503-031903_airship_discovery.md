# airship / discovery

### Final Implementation Plan (≤2 h)

**Highest-value incremental improvement**  
Enable zero-API-training mode + Lightning auto-recovery so Surrogate training is HF-rate-limit-proof and survives idle timeouts.

---

### Concrete steps (90 min total)

1. **Pre-list CDN file manifest (15 min)**  
   - Add `scripts/build_cdn_manifest.py`.  
   - Call `list_repo_tree(repo, path=date_folder, recursive=False)` once per date folder.  
   - Emit `surrogate/training/manifests/cdn_manifest_{date}.json` with entries:  
     `repo`, `path`, `size`, `sha`, `url` (full CDN URL).  
   - Commit the latest manifest and reference it in `surrogate/training/config.yaml` (`cdn_manifest: manifests/cdn_manifest_latest.json`).

2. **CDN-only dataloader (25 min)**  
   - Add `surrogate/training/dataset.py` with `CdnParquetDataset`.  
   - Use `fsspec` + `pyarrow.parquet.ParquetDataset` (or `requests.get(..., stream=True)`) to read directly from CDN URLs; **never** call `load_dataset` or other HF API endpoints during training.  
   - Validate schema on first file; project to `{prompt, response}` and drop extra columns.  
   - Add retry with exponential backoff + jitter for CDN 429/5xx.

3. **Lightning auto-recovery wrapper (25 min)**  
   - Add `surrogate/training/lightning_launcher.py` with:  
     - `ensure_studio_running(name, machine=Machine.L40S)` that lists `Teamspace.studios`, reuses Running ones, or starts stopped ones.  
     - Before each `.run()`, assert `studio.status == "running"`; if not, call `studio.start(machine=machine)` and wait for running state.  
     - Wrap training command in a loop that catches failures/KeyboardInterrupt, logs, and re-launches (max 3 retries per session).  
   - Add env flags: `HF_API_MODE=cdn` (default) vs `api`; `LIGHTNING_AUTO_RECOVER=1`.

4. **Config + entrypoint (15 min)**  
   - Update `train.py` CLI to accept `--manifest` and `--hf-mode`.  
   - Add `requirements-cdn.txt` with `fsspec`, `requests`, `pyarrow`, `tqdm`.  
   - Ensure `train.py` loads manifest from config/env and uses `CdnParquetDataset` when `HF_API_MODE=cdn`.

5. **Test + ship (10 min)**  
   - Dry-run manifest build locally (<30 s for a date folder).  
   - Smoke-test dataloader by streaming one parquet and yielding 10 rows.  
   - Commit, push, and verify Lightning launcher reuses studio and auto-starts if stopped.

---

### Resolved contradictions (in favor of correctness + actionability)
- **API calls during training**: Use CDN-only URLs; no runtime HF API calls.  
- **Auto-recovery mechanism**: Implement programmatic checks inside the launcher (not external cron) to guarantee state checks happen immediately before each run and retries are bounded.  
- **Manifest scope**: Build per-date-folder manifests once on dev machine; embed latest in repo so Lightning jobs are fully offline for data loading.  
- **Dataloader implementation**: Prefer `fsspec` + `pyarrow.parquet` for robust, streaming reads over raw `requests` for large parquet files.

---

### Code snippets

#### `scripts/build_cdn_manifest.py`
```python
#!/usr/bin/env python3
import json, os, argparse
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_DATASET_REPO", "axentx/surrogate-training")
OUT_DIR = "surrogate/training/manifests"

def main(date_folder: str, out_name: str = None):
    api = HfApi()
    tree = api.list_repo_tree(repo_id=HF_REPO, path=date_folder, recursive=False)
    files = [f for f in tree if f.path.endswith(".parquet")]

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, out_name or f"cdn_manifest_{date_folder}.json")
    manifest = {
        "repo": HF_REPO,
        "date_folder": date_folder,
        "files": [
            {
                "repo": HF_REPO,
                "path": f.path,
                "size": f.size,
                "sha": f.lfs.get("sha256", None) if f.lfs else None,
                "url": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{f.path}",
            }
            for f in files
        ],
    }
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("date_folder")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    main(args.date_folder, args.out)
```

#### `surrogate/training/dataset.py`
```python
import os, json, fsspec, pyarrow.parquet as pq
from typing import List
import torch
from torch.utils.data import IterableDataset

class CdnParquetDataset(IterableDataset):
    def __init__(self, manifest_path: str, max_retries: int = 5):
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.urls = [f["url"] for f in manifest["files"]]
        self.max_retries = max_retries

    def _stream_table(self, url: str):
        import time, random
        for attempt in range(self.max_retries):
            try:
                with fsspec.open(url, "rb") as f:
                    table = pq.read_table(f)
                # Project to required schema
                cols = {k: table[k] for k in ("prompt", "response") if k in table.column_names}
                if not cols:
                    raise ValueError("Missing prompt/response columns")
                return cols
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise
                sleep = (2**attempt) + random.random()
                time.sleep(sleep)

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        urls = self.urls
        if worker_info is not None:
            urls = urls[worker_info.id::worker_info.num_workers]
        for url in urls:
            cols = self._stream_table(url)
            for i in range(len(cols["prompt"])):
                yield {"prompt": cols["prompt"][i].as_py(), "response": cols["response"][i].as_py()}
```

#### `surrogate/training/lightning_launcher.py`
```python
import time
from lightning import Studio, Machine, Teamspace

def ensure_studio_running(name: str, machine: Machine = Machine.L40S, timeout: int = 120) -> Studio:
    ts = Teamspace()
    studios = [s for s in ts.studios if s.name == name]
    if not studios:
        raise ValueError(f"No studio found with name {name}")
    studio = studios[0]

    if studio.status == "running":
        return studio
    if studio.status == "stopped":
        studio.start(machine=machine)

    # Wait for running
    start = time.time()
    while time.time() - start < timeout:
        studio.refresh()
        if studio.status == "running":
            return studio
        time.sleep(5)
    raise RuntimeError(f"Studio {name} failed to reach 'running' within {timeout}s")

def run_with_recovery(train_fn, studio_name: str, max_retries: int = 3, **train_kwargs):
    for attempt in range(1, max_retries + 1):
        try:
            studio = ensure_studio_running(studio_name)
            train_fn(studio=studio, **train_kwargs)
            return
        except Exception as e:
            print(f"Attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                raise
            time.sleep(5)
```

#### `train.py` (CLI excerpt)
```python
import argparse, os
from pathlib import Path
from surrogate.training.dataset import

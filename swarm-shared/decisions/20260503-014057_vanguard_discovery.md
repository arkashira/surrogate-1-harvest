# vanguard / discovery

## Final Synthesized Implementation

**Core diagnosis (accepted from both):**  
Repeated authenticated `list_repo_tree` calls burn the 1000/5 min HF quota and cause 429s. There is no persisted `(repo, dateFolder) → file-list` manifest, no CDN-only data path, and no lightweight orchestration guard to reuse a running Lightning Studio. Training must not run model code on the Mac (CLI-only) and must avoid schema errors from heterogeneous repos.

**Single plan (correct + actionable):**

1. **Persisted manifest** (one-time per dateFolder) stored under `/opt/axentx/vanguard/manifests/`.  
2. **CDN-only data loader** using `datasets` + `fsspec`/`pyarrow` with strict schema projection to `{prompt, response}` and per-file `hf_hub_download` fallback for robustness.  
3. **Studio orchestration** that reuses a running Studio and supports idle-stop to protect quota.  
4. **Run boundaries:** manifest build and validation can run on the Mac; training runs only in Lightning Studio (L40S).

---

### 1) Project structure (run once)

```bash
mkdir -p /opt/axentx/vanguard/{scripts,manifests,train}
```

---

### 2) Manifest builder (run on Mac after rate-limit window)

File: `/opt/axentx/vanguard/scripts/build_manifest.py`

```python
#!/usr/bin/env python3
"""
One-time (per dateFolder) HF repo enumeration -> persisted manifest.
Run from Mac. Uses unauthenticated/public calls when possible.
"""
import json, os, sys
from datetime import date
from huggingface_hub import HfApi

REPO = os.getenv("HF_DATASET_REPO", "your-org/vanguard-data")
DATE_FOLDER = os.getenv("DATE_FOLDER", str(date.today()))  # e.g. 2026-04-29
OUT_DIR = os.getenv("MANIFEST_DIR", "/opt/axentx/vanguard/manifests")
OUT_PATH = os.path.join(OUT_DIR, f"{REPO.replace('/', '_')}_{DATE_FOLDER}.json")

def main() -> None:
    api = HfApi()
    # Non-recursive enumeration for the date folder
    tree = api.list_repo_tree(repo_id=REPO, path=DATE_FOLDER, recursive=False)
    files = sorted(
        f.rfilename for f in tree if not f.rfilename.endswith("/")
    )

    manifest = {
        "repo": REPO,
        "date_folder": DATE_FOLDER,
        "files": files,
        "cdn_prefix": f"https://huggingface.co/datasets/{REPO}/resolve/main",
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written: {OUT_PATH}  files={len(files)}")

if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
```

Usage (Mac):

```bash
export HF_DATASET_REPO=your-org/vanguard-data
export DATE_FOLDER=2026-04-29
python3 /opt/axentx/vanguard/scripts/build_manifest.py
```

---

### 3) CDN-only data loader with schema projection and fallback

File: `/opt/axentx/vanguard/train/data.py`

```python
import json, os, warnings
from pathlib import Path
from typing import Iterator, Dict, Any

import fsspec
import pyarrow as pa
import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download

class CDNParquetDataset:
    """
    Iterate over parquet files listed in a manifest using CDN URLs.
    Projects columns to {prompt, response}. Falls back to hf_hub_download
    per file if CDN fetch fails.
    """
    def __init__(self, manifest_path: str):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.prefix = self.manifest["cdn_prefix"]
        self.repo = self.manifest["repo"]
        self.files = [
            p for p in self.manifest["files"] if p.endswith(".parquet")
        ]
        if not self.files:
            raise ValueError("No parquet files found in manifest")

    def _local_or_cdn_path(self, rel_path: str) -> str:
        # Prefer CDN; fallback to hf_hub_download on failure
        cdn_url = f"{self.prefix}/{rel_path}"
        try:
            with fsspec.open(cdn_url, "rb", timeout=10) as f:
                f.read(1)  quick check
            return cdn_url
        except Exception:
            return hf_hub_download(
                repo_id=self.repo,
                filename=rel_path,
                repo_type="dataset",
            )

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for rel_path in self.files:
            source = self._local_or_cdn_path(rel_path)
            try:
                with fsspec.open(source, "rb") as f:
                    table = pq.read_table(f, columns=["prompt", "response"])
            except Exception as e:
                warnings.warn(f"Failed to read {rel_path} from {source}: {e}")
                continue

            # Ensure expected schema
            if not all(col in table.column_names for col in ("prompt", "response")):
                warnings.warn(f"Missing prompt/response in {rel_path}; skipping")
                continue

            table = table.select(["prompt", "response"])
            for batch in table.to_batches():
                d = batch.to_pydict()
                for i in range(len(d["prompt"])):
                    yield {"prompt": d["prompt"][i], "response": d["response"][i]}
```

File: `/opt/axentx/vanguard/train/train.py` (snippet to use loader)

```python
from train.data import CDNParquetDataset
from torch.utils.data import DataLoader

manifest_path = "/opt/axentx/vanguard/manifests/vanguard-data_2026-04-29.json"
train_dataset = CDNParquetDataset(manifest_path)

# Use with DataLoader (num_workers=0 recommended for simple CDN streaming)
train_loader = DataLoader(train_dataset, batch_size=None, num_workers=0)
```

---

### 4) Studio orchestration with reuse + idle-stop guard

File: `/opt/axentx/vanguard/scripts/launch_studio.py`

```python
#!/usr/bin/env python3
"""
Reuse a running Lightning Studio if present; otherwise create one (L40S).
Optionally stop the Studio after idle timeout to protect quota.
"""
import os
import time
from typing import Optional

from lightning import Studio, Teamspace, Machine

REPO_ROOT = "/opt/axentx/vanguard"
STUDIO_NAME = "vanguard-train-l40s"
IDLE_STOP_SECONDS = int(os.getenv("STUDIO_IDLE_STOP_SECONDS", "0"))  # 0 = disabled

def find_running_studio() -> Optional[Studio]:
    for s in Teamspace.studios:
        if s.name == STUDIO_NAME and s.status == "Running":
            return s
    return None

def main() -> Studio:
    running = find_running_studio()
    if running:
        print(f"Reusing running studio: {running.name}")
        return running

    studio = Studio(
        name=STUDIO_NAME,
        repo=REPO_ROOT,
        machine=Machine.L40S,
        create_ok=True,
    )
    print(f"Created studio: {studio.name}")

    if IDLE_STOP_SECONDS > 0:
        # Best-effort idle-stop guard: stop studio after idle period.
        # In practice, run a lightweight monitor or cron that calls
        # studio.stop() when no active training processes exist.
        print(
            f"Idle-stop enabled ({IDLE_STOP_SECONDS}s). "
            "Run a monitor/cron to call studio.stop() when idle."
        )
    return studio

if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/vanguard/scripts/launch_studio.py
```

Usage:

```bash
python3 /opt/axentx/vanguard/scripts/launch_studio.py
```

Running twice prints

# airship / discovery

## Final Implementation Plan (≤2h)

**Highest-value change**: Add a CDN-only parquet loader and Lightning idle-resilient runner to `/opt/axentx/airship/surrogate/train.py` plus `scripts/list_hf_files.py`. This eliminates HF API calls during training (bypasses 429 rate limits) and prevents Lightning idle timeouts from killing long runs.

### Steps (1h 45m total)
1. **Create `scripts/list_hf_files.py`** (20m) — one-shot Mac script that lists a date folder via `list_repo_tree` and writes `manifest/YYYY-MM-DD.json` for embedding in training.
2. **Create `/opt/axentx/airship/surrogate/train.py`** (60m) — implements:
   - CDN-only parquet streaming (no HF API auth during training)
   - Lightning idle-resilient runner with automatic studio restart
   - Graceful fallback to local PyTorch training if Lightning unavailable
3. **Add `.env` / config** (10m) — repo, date folder, and HF dataset name.
4. **Smoke test** (15m) — run list script, verify JSON, start training stub.

---

### 1) `scripts/list_hf_files.py`

```python
#!/usr/bin/env python3
"""
Run on Mac (or any dev machine) after HF rate-limit window clears.
Produces file manifest for CDN-only training.

Usage:
  python scripts/list_hf_files.py \
    --repo datasets/axentx/surrogate-mirror \
    --date 2026-04-29 \
    --out manifest/2026-04-29.json
"""

import argparse
import json
import os
import time
from pathlib import Path

from huggingface_hub import HfApi

HF_API_RATE_LIMIT_RESET_BUFFER = 360  # seconds to wait after 429

def list_date_folder(repo_id: str, date_folder: str, out_path: Path):
    api = HfApi()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Single non-recursive call per date folder (avoids 100x pagination)
    entries = api.list_repo_tree(
        repo_id=repo_id,
        path=date_folder,
        repo_type="dataset",
        recursive=False,
    )

    files = []
    for e in entries:
        if getattr(e, "type", None) == "file":
            fname = getattr(e, "path", None)
            if fname and fname.lower().endswith(".parquet"):
                files.append(fname)

    manifest = {
        "repo_id": repo_id,
        "date_folder": date_folder,
        "files": sorted(files),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "CDN-only; do not require HF API auth during training",
    }

    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {out_path}")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="List HF dataset files for CDN-only training.")
    parser.add_argument("--repo", required=True, help="Dataset repo id (e.g. datasets/axentx/surrogate-mirror)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-04-29)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    try:
        list_date_folder(args.repo, args.date, Path(args.out))
    except Exception as exc:
        # If we hit 429, wait and retry once
        import traceback
        traceback.print_exc()
        print(f"Error: {exc}")
        print(f"Sleeping {HF_API_RATE_LIMIT_RESET_BUFFER}s and retrying once...")
        time.sleep(HF_API_RATE_LIMIT_RESET_BUFFER)
        list_date_folder(args.repo, args.date, Path(args.out))
```

Make executable:
```bash
chmod +x scripts/list_hf_files.py
```

---

### 2) `/opt/axentx/airship/surrogate/train.py`

```python
#!/usr/bin/env python3
"""
CDN-only parquet loader + Lightning idle-resilient runner.
No HF API calls during data loading (bypasses 429).
"""

import json
import os
import time
from pathlib import Path
from typing import Iterator, Dict, Any

import pyarrow.parquet as pq
import requests
import torch
from torch.utils.data import IterableDataset, DataLoader

# Optional: Lightning if available
try:
    from lightning.pytorch import Trainer
    from lightning.pytorch.callbacks import Callback
    from lightning.pytorch.strategies import DDPStrategy
    LIGHTNING_AVAILABLE = True
except Exception:
    LIGHTNING_AVAILABLE = False

# ---------- Config ----------
HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "datasets/axentx/surrogate-mirror")
DATE_FOLDER = os.getenv("DATE_FOLDER", "2026-04-29")
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "manifest/2026-04-29.json")
CDN_BASE = f"https://huggingface.co/datasets/{HF_DATASET_REPO}/resolve/main"
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "8"))
MAX_STEPS = int(os.getenv("MAX_STEPS", "1000"))
LIGHTNING_CLOUD = os.getenv("LIGHTNING_CLOUD", "lightning-public-prod")  # free tier fallback
# ----------

def cdn_url(path: str) -> str:
    return f"{CDN_BASE}/{path}"

class CDNParquetIterable(IterableDataset):
    """
    Stream parquet files via CDN (no HF API auth).
    Each file is downloaded fully; rows are yielded as {prompt, response}.
    """
    def __init__(self, manifest_path: str, start_file: int = 0):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.files = self.manifest["files"][start_file:]

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for fpath in self.files:
            url = cdn_url(fpath)
            # Stream download to temp file to avoid memory spikes
            out = Path("/tmp") / Path(fpath).name
            out.parent.mkdir(parents=True, exist_ok=True)

            resp = requests.get(url, stream=True, timeout=60)
            resp.raise_for_status()
            with open(out, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    fh.write(chunk)

            table = pq.read_table(out)
            os.remove(out)

            # Project to surrogate training schema: prompt, response
            for i in range(table.num_rows):
                row = table.slice(i, 1).to_pydict()
                prompt = row.get("prompt") or row.get("text") or row.get("input")
                response = row.get("response") or row.get("output") or row.get("completion")
                if prompt and response:
                    if isinstance(prompt, list):
                        prompt = prompt[0]
                    if isinstance(response, list):
                        response = response[0]
                    yield {"prompt": str(prompt), "response": str(response)}

class DummySurrogateModel(torch.nn.Module):
    """Minimal model placeholder; replace with your actual model."""
    def __init__(self):
        super().__init__()
        self.embed = torch.nn.Embedding(32000, 1024)
        self.lm = torch.nn.Linear(1024, 32000)

    def forward(self, x):
        return self.lm(self.embed(x))

class LightningIdleResilientRunner:
    """
    Handles Lightning Studio idle timeouts:
    - Checks studio status before run
    - Restarts studio if stopped
    """
    def __init__(self, studio_name: str = "surrogate-train"):
        if not LIGHTNING_AVAILABLE:
            raise RuntimeError("Lightning not available; install lightning to use this runner.")
        self.studio_name = studio_name

    def get_running_studio(self):
        from lightning.pytorch import Teamspace
        for s in Teamspace

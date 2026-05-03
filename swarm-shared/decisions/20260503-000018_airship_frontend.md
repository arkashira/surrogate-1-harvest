# airship / frontend

## Highest-Value Incremental Improvement (<2h)

**Problem**: Surrogate-1 training blocked by HF API 429s during dataset loading; training stalls waiting on API pagination instead of GPU.

**Fix**: Implement CDN-only data loading with a pre-computed file manifest. One-time Mac-side script to list files → JSON. Training script uses only CDN URLs (`https://huggingface.co/datasets/{repo}/resolve/main/...`) with zero HF API calls during data load. Studio reuse + idle restart guard to avoid quota waste.

---

## Implementation Plan (≤2h)

1. **Create file manifest generator** (Mac, one-time or cron after rate-limit window)  
   - `scripts/generate_file_manifest.py`  
   - Uses `list_repo_tree(path, recursive=False)` per date folder → saves `manifests/{date}_files.json`  
   - Embeds repo, path, sha, size for traceability

2. **Update training data loader**  
   - Read manifest JSON at startup  
   - Build list of CDN URLs  
   - Stream via `requests.get(url, stream=True)` + `pyarrow` parquet projection to `{prompt, response}` only  
   - No `load_dataset`, no `hf_hub_download`, no recursive listing during training

3. **Studio reuse + idle restart guard**  
   - Before `.run()`, list running studios and reuse if name/status match  
   - If stopped, restart with `target.start(machine=Machine.L40S)`  
   - Avoids recreating and saves quota

4. **Integrate into airship frontend config**  
   - Add small UI toggle/config field to pick manifest date  
   - Show last training run status and manifest file used

---

## Code Snippets

### 1) Manifest Generator (Mac)

```python
# scripts/generate_file_manifest.py
#!/usr/bin/env python3
"""
Generate CDN-only file manifest for a HuggingFace dataset repo.
Run from Mac after HF API rate-limit window clears.
"""
import json, os, sys
from datetime import datetime
from huggingface_hub import HfApi

API = HfApi()
REPO_ID = "axentx/surrogate-1-ingest"  # adjust
OUTPUT_DIR = "manifests"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def list_date_folder(date_folder: str):
    """List non-recursive top-level files in a date folder."""
    items = API.list_repo_tree(repo_id=REPO_ID, path=date_folder, recursive=False)
    files = [it for it in items if it.type == "file"]
    return files

def main():
    # Accept date arg or default to today
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.utcnow().strftime("%Y-%m-%d")
    folder = f"batches/mirror-merged/{date_str}"
    print(f"Listing {REPO_ID}/{folder} ...")

    files = list_date_folder(folder)
    manifest = {
        "repo_id": REPO_ID,
        "date_folder": folder,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": [
            {
                "path": f.path,
                "size": f.size,
                "sha": getattr(f, "sha", None),
                "cdn_url": f"https://huggingface.co/datasets/{REPO_ID}/resolve/main/{f.path}"
            }
            for f in files
        ]
    }

    out_path = os.path.join(OUTPUT_DIR, f"{date_str}_files.json")
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {out_path} ({len(files)} files)")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x scripts/generate_file_manifest.py
```

---

### 2) CDN-Only Data Loader (for Lightning training)

```python
# surrogate/train/data/cdn_parquet_loader.py
import json, io, pyarrow.parquet as pq, pyarrow as pa, requests
from torch.utils.data import IterableDataset

class CDNParquetDataset(IterableDataset):
    """
    Load {prompt,response} from parquet files via CDN URLs listed in a manifest.
    Zero HuggingFace API calls during training.
    """
    def __init__(self, manifest_path: str, columns=("prompt", "response")):
        with open(manifest_path) as f:
            manifest = json.load(f)
        self.urls = [f["cdn_url"] for f in manifest["files"]]
        self.columns = columns

    def _stream_parquet(self, url: str):
        # CDN downloads bypass HF API rate limits
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            buf = io.BytesIO(r.content)
        table = pq.read_table(buf, columns=self.columns)
        return table.to_pylist()

    def __iter__(self):
        for url in self.urls:
            try:
                rows = self._stream_parquet(url)
                for row in rows:
                    # Ensure required fields exist
                    prompt = row.get("prompt") or row.get("text") or ""
                    response = row.get("response") or row.get("completion") or ""
                    if prompt and response:
                        yield {"prompt": prompt, "response": response}
            except Exception as exc:
                # Log and skip bad files to avoid crashing training
                print(f"Skipping {url}: {exc}")
                continue
```

Usage in training script:

```python
from lightning.pytorch import Trainer
from surrogate.train.data.cdn_parquet_loader import CDNParquetDataset

manifest = "manifests/2026-05-02_files.json"
train_ds = CDNParquetDataset(manifest)
# Build DataLoader and train as usual
```

---

### 3) Lightning Studio Reuse + Idle Guard

```python
# surrogate/train/launch_studio.py
import time
from lightning.pytorch.cli import LightningCLI
from lightning.fabric.utilities.cloud_io import _load as load_yaml
from lightning.app import LightningFlow, LightningWork, LightningApp
from lightning.app.components.serve import ServeGradio
# Minimal example: reuse running studio or start new

from lightning.pytorch import seed_everything
from lightning.pytorch.callbacks import ModelCheckpoint
from lightning.pytorch.loggers import CSVLogger
from lightning.pytorch import Trainer
from lightning.pytorch.strategies import FSDPStrategy

# If using Lightning AI Studio directly via SDK:
try:
    from lightning.app import Teamspace, Studio, Machine
except ImportError:
    Teamspace = Studio = Machine = None

def get_or_create_studio(name="surrogate-train", machine="L40S"):
    if Teamspace is None:
        return None
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return Studio(
        name=name,
        machine=Machine(machine),
        create_ok=True
    )

def run_training_with_studio(manifest_path, script_path="train.py"):
    studio = get_or_create_studio()
    if studio is None:
        # fallback local
        import subprocess
        subprocess.run([sys.executable, script_path, "--manifest", manifest_path], check=True)
        return

    # If stopped, restart
    if studio.status != "Running":
        print("Studio stopped — restarting...")
        studio.start(machine=Machine("L40S"))
        # wait until running
        while studio.status != "Running":
            time.sleep(10)

    # Run training script inside studio (example via .run)
    # Adjust based on actual SDK capabilities
    studio.run(
        target=f"python {script_path}",
        env={"MANIFEST_PATH": manifest_path}
    )
```

---

### 4) Airship Frontend Config (small toggle)

Add to frontend config (e.g., `arkship/src/config/training.json`):

```json
{
  "surrogateTraining": {
    "manifestDate": "2026-05-02",
    "cdnOnly": true,
    "studioReuse": true,
    "machine": "L40S"
  }
}
```

Expose a minimal UI control in the Arkship DevOps panel to

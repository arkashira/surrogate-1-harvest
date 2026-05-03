# airship / frontend

## Final Integrated Solution (Best of Both Candidates)

**Goal**: Eliminate HuggingFace API rate-limit failures and Lightning idle timeouts with a production-ready, <2h deployable system.

**Core Architecture**:
- **CDN-only data pipeline**: Zero API calls during training
- **Self-healing Lightning runner**: Auto-restarts on idle timeout
- **Deterministic file listing**: Single JSON manifest per date (generated once on Mac)

---

## 1. File List Generator (Mac, One-Time)
**File**: `scripts/list_parquet_files.py`

```python
import argparse
import json
from huggingface_hub import HfApi

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, default="surrogate-dataset-mirror")
    parser.add_argument("--date", required=True)  # e.g., 2026-04-29
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    
    api = HfApi()
    files = api.list_repo_tree(
        repo_id=args.repo,
        path=f"batches/mirror-merged/{args.date}",
        recursive=False
    )
    
    entries = [
        {"path": f"batches/mirror-merged/{args.date}/{f.path.split('/')[-1]}", "size": getattr(f, "size", 0)}
        for f in files if f.path.endswith(".parquet")
    ]
    
    with open(args.out, "w") as f:
        json.dump(entries, f, indent=2)
    print(f"Wrote {len(entries)} parquet files to {args.out}")

if __name__ == "__main__":
    main()
```

**Usage**:
```bash
python scripts/list_parquet_files.py --repo surrogate-dataset-mirror --date 2026-04-29 --out surrogate/file_list_2026-04-29.json
```

---

## 2. CDN-Only Parquet Loader (Resilient + Schema-Safe)
**File**: `surrogate/data/cdn_parquet_loader.py`

```python
import json
import pyarrow.parquet as pq
import pyarrow as pa
import requests
from io import BytesIO
from typing import List, Dict, Iterator
import logging

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

class CDNParquetLoader:
    """Load parquet files via HF CDN (no API/auth/rate-limit)."""
    
    def __init__(self, repo: str, file_list_path: str):
        self.repo = repo
        with open(file_list_path) as f:
            self.files = json.load(f)
        self.logger = logging.getLogger(__name__)
    
    def _download_file(self, path: str) -> bytes:
        url = HF_CDN_TEMPLATE.format(repo=self.repo, path=path)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content
    
    def read_table(self, path: str) -> pa.Table:
        data = self._download_file(path)
        return pq.read_table(BytesIO(data))
    
    def stream_rows(self, batch_size: int = 1000) -> Iterator[Dict]:
        """Stream {prompt, response} rows across all parquet files with schema resilience."""
        for f in self.files:
            try:
                table = self.read_table(f["path"])
                cols = set(table.column_names)
                if "prompt" not in cols or "response" not in cols:
                    self.logger.warning(f"Skipping {f['path']}: missing required columns")
                    continue
                df = table.select(["prompt", "response"]).to_pylist()
                for i in range(0, len(df), batch_size):
                    yield from df[i:i + batch_size]
            except Exception as e:
                self.logger.error(f"Failed to process {f['path']}: {e}")
                continue
```

---

## 3. Unified Training Script (Single-File Entrypoint)
**File**: `/opt/axentx/airship/surrogate/train.py`

```python
import os
import json
import logging
from pathlib import Path
from surrogate.data.cdn_parquet_loader import CDNParquetLoader
from huggingface_hub import HfApi

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
REPO = "surrogate-dataset-mirror"
DATE = os.getenv("SURROGATE_DATE", "2026-04-29")
FILE_LIST = Path(f"surrogate/file_list_{DATE}.json")
OUTPUT_REPO = os.getenv("OUTPUT_REPO", "surrogate-enriched")

def train_step(loader: CDNParquetLoader, max_batches: int = 100):
    """Single training epoch using CDN data."""
    batch_count = 0
    for batch in loader.stream_rows(batch_size=512):
        # Replace with actual surrogate model training
        # e.g., surrogate.model.train_step(batch["prompt"], batch["response"])
        logger.info(f"Processing batch {batch_count}: {len(batch)} samples")
        batch_count += 1
        if batch_count >= max_batches:
            break
    return batch_count

def upload_enriched_shard(local_path: str, remote_path: str):
    """Upload enriched shard to HF dataset repo."""
    api = HfApi()
    api.upload_file(
        path_or_fileobj=local_path,
        path_in_repo=remote_path,
        repo_id=OUTPUT_REPO,
        repo_type="dataset"
    )

def main():
    if not FILE_LIST.exists():
        raise FileNotFoundError(f"File list not found: {FILE_LIST}")
    
    logger.info(f"Starting training with {FILE_LIST}")
    loader = CDNParquetLoader(repo=REPO, file_list_path=str(FILE_LIST))
    
    # Run training
    batches_processed = train_step(loader)
    logger.info(f"Completed {batches_processed} batches")
    
    # Example: Upload enriched shard (if generated)
    # upload_enriched_shard("enriched_shard.parquet", "batches/enriched/2026-04-29/shard_001.parquet")

if __name__ == "__main__":
    main()
```

---

## 4. Lightning Runner (Self-Healing)
**File**: `/opt/axentx/airship/surrogate/run_lightning.py`

```python
import time
import logging
from lightning import Lightning, Teamspace, Machine, L40S

# Configuration
STUDIO_NAME = "surrogate-train-l40s"
MACHINE = Machine.L40S
TRAIN_SCRIPT = "/opt/axentx/airship/surrogate/train.py"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def ensure_running_studio() -> Teamspace:
    """Reuse or start a running studio; never recreate."""
    ls = Lightning()
    ts = ls.teamspace()
    
    for s in ts.studios:
        if s.name == STUDIO_NAME:
            if s.status == "running":
                logger.info(f"Reusing running studio: {STUDIO_NAME}")
                return s
            else:
                logger.info(f"Restarting stopped studio: {STUDIO_NAME}")
                s.start(machine=MACHINE)
                return s
    
    logger.info(f"Creating new studio: {STUDIO_NAME}")
    return ts.create_studio(
        name=STUDIO_NAME,
        machine=MACHINE,
        create_ok=True
    )

def run_training():
    studio = ensure_running_studio()
    
    while True:
        if studio.status != "running":
            logger.warning("Studio stopped (idle timeout). Restarting...")
            studio.start(machine=MACHINE)
            time.sleep(60)
            continue
        
        try:
            # Execute training script remotely
            result = studio.run(TRAIN_SCRIPT)
            logger.info(f"Training completed: {result}")
            break  # Exit on successful completion
        except Exception as e:
            logger.error(f"Training error: {e}")
            time.sleep(60)  # Wait before retry

if __name__ == "__main__":
    run_training()
```

---

## 5. Deployment Steps (<2h)

### Phase 1: Setup (Mac)
```bash
# Generate file list (once per date folder)
python scripts/list

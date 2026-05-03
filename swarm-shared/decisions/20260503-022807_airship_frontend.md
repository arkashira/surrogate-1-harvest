# airship / frontend

## Highest-Value Incremental Improvement (<2h)

**Goal**: Eliminate HF API 429s and Lightning quota waste during Surrogate training by implementing CDN-first deterministic ingestion with Lightning Studio reuse.

**Why this ships fastest**: Uses existing infrastructure (HF CDN, Lightning SDK) with minimal new code; directly addresses the two biggest blockers (rate limits + quota waste) identified in the surrogate training pipeline.

---

## Implementation Plan

### 1. Create CDN-first file listing utility (15 min)
Single API call from Mac (after rate-limit window) → JSON file embedded in training. Lightning training uses CDN-only fetches (zero API calls during data load).

### 2. Lightning Studio reuse wrapper (15 min)
List running studios before creating; reuse if exists. Saves ~80hr/mo quota.

### 3. Update surrogate training entrypoint (45 min)
- Accept pre-computed file list JSON
- Stream from HF CDN URLs (no auth, no API)
- Project to `{prompt, response}` only
- Deterministic repo selection for commits (hash slug → sibling repo)

### 4. Add idle-aware training runner (30 min)
Check studio status before `.run()`; restart if stopped (Lightning idle timeout kills training).

---

## Code Snippets

### `scripts/generate_file_list.py`
```python
#!/usr/bin/env python3
"""
Generate deterministic file list for HF dataset ingestion.
Run from Mac after rate-limit window clears.
Embeds result in training script for CDN-only fetches.
"""
import json
import hashlib
from pathlib import Path
from huggingface_hub import HfApi

HF_REPO = "axentx/surrogate-mirror"
DATE_FOLDER = "2026-05-03"  # parameterized
OUTPUT_FILE = Path("data/file_list.json")
SIBLING_REPOS = [
    "axentx/surrogate-mirror",
    "axentx/surrogate-mirror-1",
    "axentx/surrogate-mirror-2",
    "axentx/surrogate-mirror-3",
    "axentx/surrogate-mirror-4",
]

def pick_repo(slug: str) -> str:
    """Deterministic repo selection for commit cap distribution."""
    digest = hashlib.md5(slug.encode()).hexdigest()
    idx = int(digest, 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]

def main():
    api = HfApi()
    
    # Single API call - recursive=False per folder
    tree = api.list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_FOLDER,
        recursive=False
    )
    
    files = []
    for item in tree:
        if item.rfilename.endswith(('.jsonl', '.parquet', '.json')):
            # CDN URL bypasses API auth entirely
            cdn_url = (
                f"https://huggingface.co/datasets/{HF_REPO}"
                f"/resolve/main/{DATE_FOLDER}/{item.rfilename}"
            )
            
            files.append({
                "filename": item.rfilename,
                "cdn_url": cdn_url,
                "size": getattr(item, 'size', None),
                "target_repo": pick_repo(item.rfilename)
            })
    
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, 'w') as f:
        json.dump({
            "date": DATE_FOLDER,
            "source_repo": HF_REPO,
            "files": files,
            "total": len(files)
        }, f, indent=2)
    
    print(f"Generated {len(files)} file entries -> {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
```

### `surrogate/train.py` (updated entrypoint)
```python
#!/usr/bin/env python3
"""
CDN-first training entrypoint for Surrogate AI.
Uses pre-computed file list for zero-API data loading.
"""
import json
import os
import requests
import pyarrow.parquet as pq
import pyarrow as pa
from io import BytesIO
from pathlib import Path
from lightning import Fabric, LightningModule, Trainer
from lightning.pytorch.strategies import FSDPStrategy

class CDNDataset:
    """Stream from HF CDN with zero API calls."""
    def __init__(self, file_list_path: str, max_files: int = None):
        with open(file_list_path) as f:
            manifest = json.load(f)
        
        self.files = manifest["files"]
        if max_files:
            self.files = self.files[:max_files]
    
    def __iter__(self):
        for entry in self.files:
            try:
                # CDN download - no Authorization header
                resp = requests.get(entry["cdn_url"], timeout=30)
                resp.raise_for_status()
                
                # Project to {prompt, response} only
                if entry["filename"].endswith(".parquet"):
                    table = pq.read_table(BytesIO(resp.content))
                    # Schema projection: keep only prompt/response cols
                    cols = [c for c in table.column_names if c in ("prompt", "response", "instruction", "output")]
                    if len(cols) >= 2:
                        prompt_col = cols[0]
                        response_col = cols[1]
                        for i in range(table.num_rows):
                            yield {
                                "prompt": table[prompt_col][i].as_py(),
                                "response": table[response_col][as_py()] if i < table.num_rows else ""
                            }
                
                elif entry["filename"].endswith(".jsonl"):
                    for line in resp.text.splitlines():
                        if line.strip():
                            obj = json.loads(line)
                            yield {
                                "prompt": obj.get("prompt") or obj.get("instruction") or "",
                                "response": obj.get("response") or obj.get("output") or ""
                            }
            
            except Exception as e:
                print(f"Skipping {entry['filename']}: {e}")
                continue

class SurrogateDataModule:
    def __init__(self, file_list_path: str, batch_size: int = 8):
        self.file_list_path = file_list_path
        self.batch_size = batch_size
    
    def train_dataloader(self):
        import torch
        from torch.utils.data import DataLoader, IterableDataset
        
        class IterableCDN(IterableDataset):
            def __init__(self, file_list):
                self.file_list = file_list
            
            def __iter__(self):
                return CDNDataset(self.file_list)
        
        return DataLoader(
            IterableCDN(self.file_list_path),
            batch_size=self.batch_size,
            num_workers=0
        )

class SurrogateModel(LightningModule):
    # Your model definition here
    pass

def get_running_studio(name: str):
    """Reuse existing running studio to save quota."""
    from lightning import Teamspace
    
    for studio in Teamspace.studios:
        if studio.name == name and studio.status == "running":
            print(f"Reusing running studio: {name}")
            return studio
    return None

def train_with_idle_check(config):
    """Lightning training with idle-aware restart."""
    from lightning import Studio, Machine
    
    studio_name = config.get("studio_name", "surrogate-train")
    studio = get_running_studio(studio_name)
    
    if not studio:
        print(f"Creating new studio: {studio_name}")
        # Free tier falls back to L40S, H200 requires lightning-lambda-prod
        studio = Studio(
            name=studio_name,
            machine=Machine.L40S,  # or Machine.H200 if in lambda-prod
            cloud="lightning-public-prod"
        )
    
    # Check status before run - idle stop kills training
    if studio.status != "running":
        print(f"Studio stopped, restarting...")
        studio.start(machine=Machine.L40S)
    
    # Run training with CDN file list
    studio.run(
        cloud_build_config={
            "requirements": [
                "torch",
                "lightning",
                "pyarrow",
                "requests",
                "huggingface-hub"
            ]
        },
        run_config={
            "entry_point": "train.py",
            "arguments": [
                "--file-list", "data/file_list.json",
                "--batch-size", str(config.get("batch_size", 8))
            ]
        }
    )

if __name__ == "__main__":
    import

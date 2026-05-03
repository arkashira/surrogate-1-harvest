# vanguard / quality

## Final synthesized answer

### Diagnosis (merged, highest-confidence)

- **No persisted `(repo, dateFolder) → file-list` manifest**: repeated authenticated `list_repo_tree` burns HF API quota and risks 429s.
- **Training/data loader likely uses `load_dataset(...)` or per-file streaming on heterogeneous repos**, causing `pyarrow` CastError from mixed schemas.
- **No CDN-only data path**: authenticated API calls during training inflate rate-limit pressure; public CDN URLs are available but unused.
- **No deterministic repo selection / commit-cap mitigation**: writes to a single HF repo risk hitting the 128-writes-per-hour limit.
- **No Studio reuse guard**: training script may recreate Lightning Studio instead of reusing running instances, wasting quota.

---

### Proposed change (merged, highest-leverage)

Add a small, high-leverage quality layer:

1. **Manifest generator** (one-time, authenticated) that records per-date-folder file lists and CDN URLs.
2. **Training-side CDN loader** that fetches only via CDN (no auth/rate-limit cost) and projects heterogeneous files to `{prompt,response}` at parse time.
3. **Training entrypoint** that uses the manifest, avoids HF API calls during data loading, and reuses existing Studio instances.

Scope:
- `/opt/axentx/vanguard/scripts/build_file_manifest.py`
- `/opt/axentx/vanguard/training/cdn_dataset.py`
- `/opt/axentx/vanguard/training/train_cdn_example.py` (minimal Lightning-compatible starter)

---

### Implementation (merged + hardened)

```bash
# Ensure directories
mkdir -p /opt/axentx/vanguard/{scripts,training}
```

#### `/opt/axentx/vanguard/scripts/build_file_manifest.py`

```python
#!/usr/bin/env python3
"""
Build a repo+date manifest for CDN-only training.
Usage:
  HF_TOKEN=hf_xxx python build_file_manifest.py \
    --repo datasets/owner/repo \
    --date-folder 2026-05-03 \
    --out manifest_2026-05-03.json
"""

import argparse
import json
import os
import sys
from typing import List, Dict

from huggingface_hub import HfApi

CDN_BASE = "https://huggingface.co/datasets"

def build_manifest(repo: str, date_folder: str, out_path: str) -> None:
    api = HfApi(token=os.getenv("HF_TOKEN"))
    repo_id = repo.replace("datasets/", "")

    # Single non-recursive call per date folder (avoids pagination explosion)
    items = api.list_repo_tree(
        repo_id=repo_id,
        path=date_folder,
        recursive=False,
    )

    files: List[Dict[str, str]] = []
    for item in items:
        if getattr(item, "type", None) != "file":
            continue
        # CDN URL bypasses API auth/rate limits during training
        cdn_url = f"{CDN_BASE}/{repo_id}/resolve/main/{date_folder}/{item.path}"
        files.append(
            {
                "repo": repo_id,
                "path": item.path,
                "date_folder": date_folder,
                "cdn_url": cdn_url,
            }
        )

    manifest = {
        "repo": repo_id,
        "date_folder": date_folder,
        "files": files,
    }

    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build CDN manifest for date folder.")
    parser.add_argument("--repo", required=True, help="e.g. datasets/owner/repo")
    parser.add_argument("--date-folder", required=True, help="e.g. 2026-05-03")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    try:
        build_manifest(args.repo, args.date_folder, args.out)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
```

```bash
chmod +x /opt/axentx/vanguard/scripts/build_file_manifest.py
```

---

#### `/opt/axentx/vanguard/training/cdn_dataset.py`

```python
import json
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from io import BytesIO
from typing import Dict, Iterator, List

# Minimal projection to avoid mixed-schema errors
REQUIRED_COLS = {"prompt", "response"}

def select_columns(table: pa.Table) -> pa.Table:
    available = set(table.column_names)
    missing = REQUIRED_COLS - available
    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    return table.select(list(REQUIRED_COLS))

def stream_parquet_from_cdn(url: str) -> pa.Table:
    # CDN fetch (no auth) — high CDN limits, no HF API quota burn
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    table = pq.read_table(BytesIO(resp.content))
    return select_columns(table)

def build_cdn_dataset(manifest_path: str) -> Iterator[Dict[str, pa.Table]]:
    with open(manifest_path) as f:
        manifest = json.load(f)

    for file_meta in manifest["files"]:
        url = file_meta["cdn_url"]
        try:
            table = stream_parquet_from_cdn(url)
            yield {"url": url, "table": table}
        except Exception as exc:
            # Log and skip bad files instead of failing whole run
            print(f"Skipping {url}: {exc}")
            continue

def iter_rows(manifest_path: str) -> Iterator[Dict[str, str]]:
    for item in build_cdn_dataset(manifest_path):
        table = item["table"]
        df = table.to_pandas()
        for _, row in df.iterrows():
            yield {"prompt": row["prompt"], "response": row["response"]}
```

---

#### `/opt/axentx/vanguard/training/train_cdn_example.py`

```python
#!/usr/bin/env python3
"""
Example: Lightning-compatible CDN-only training loop starter.
- Uses pre-built manifest (zero HF API calls during data load).
- Projects to {prompt,response} only (avoids pyarrow schema errors).
- Reuses running Studio when available.
"""

import os
from pathlib import Path

import torch
from torch.utils.data import Dataset, DataLoader

import lightning as L
from lightning.fabric.plugins import TorchCheckpointIO

from cdn_dataset import iter_rows

# -- simple dataset --
class PromptResponseDataset(Dataset):
    def __init__(self, manifest_path: str):
        self.items = list(iter_rows(manifest_path))

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, idx: int):
        item = self.items[idx]
        # Tokenization placeholder — replace with real tokenizer
        return item["prompt"], item["response"]

# -- dummy model --
class TinyLM(L.LightningModule):
    def __init__(self, lr: float = 1e-3):
        super().__init__()
        self.embed = torch.nn.Embedding(1000, 64)
        self.lm_head = torch.nn.Linear(64, 1000)
        self.lr = lr

    def training_step(self, batch, batch_idx):
        x, y = batch
        # dummy forward
        x = self.embed(x.abs().sum(-1, keepdim=True).long())
        logits = self.lm_head(x)
        loss = torch.nn.functional.cross_entropy(
            logits.view(-1, 1000), y.abs().sum(-1).long()
        )
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.lr)

# -- collate --
def collate_fn(batch):
    # Placeholder collate — adapt to real tokenizer
    prompts, responses = zip(*batch)
    x = torch.tensor([hash(p) % 1000 for p in prompts]).unsqueeze(1)
    y = torch.tensor([hash(r) % 1000 for

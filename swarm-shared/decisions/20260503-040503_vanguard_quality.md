# vanguard / quality

# Final Synthesis (Corrected + Actionable)

## 1. Diagnosis (resolved)
- **Runtime HF API calls in training**: training scripts call `load_dataset`/`list_repo_tree` during data loading → 429 rate-limits and non-reproducible runs.
- **No content-addressed manifest**: ingestion/training re-lists HF repos at runtime instead of using a deterministic file list → flaky runs and quota waste.
- **Mixed-schema pollution**: heterogeneous files land in `enriched/` without projection to `{prompt, response}` → downstream `surrogate-1` training fails on schema collisions and `pyarrow.CastError`.
- **Lightning Studio recreation per run**: wastes quota (~80 hr/mo) and loses training state on idle-stop.
- **HF API used during training data loading**: non-deterministic across runs and rate-limited.

## 2. Proposed change (single coherent plan)
Create a **manifest-first, CDN-only training path** for `vanguard` that:
- Runs **once** (on Mac or CI) after the rate-limit window clears.
- Builds a content-addressed `manifests/{date}/filelist.json` with `{repo, path, sha, size, url, slug, date}` using a single-level `list_repo_tree` call.
- Embeds that manifest into training so **Lightning does zero HF API calls** and uses only CDN URLs.
- Projects mixed-schema files to `{prompt, response}` at parse time (no schema pollution in `enriched/`).
- Persists Lightning checkpoints externally to avoid recreation loss and quota waste.

Scope:
- Add `/opt/axentx/vanguard/scripts/build_manifest.py`
- Add/update `/opt/axentx/vanguard/train.py` with CDN-only `IterableDataset` and checkpointing
- Update `/opt/axentx/vanguard/requirements.txt` as needed

## 3. Implementation (corrected and complete)

### 3.1 Directory layout
```bash
cd /opt/axentx/vanguard
mkdir -p scripts manifests data checkpoints logs
```

### 3.2 `scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Build a content-addressed manifest for a single date folder.
Usage:
  HF_REPO=datasets/your/repo HF_DATE=2026-04-29 python scripts/build_manifest.py
Outputs:
  manifests/{HF_DATE}/filelist.json
"""
import os
import json
import hashlib
import sys

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

HF_REPO = os.getenv("HF_REPO")
HF_DATE = os.getenv("HF_DATE")
if not HF_REPO or not HF_DATE:
    print("Set HF_REPO and HF_DATE env vars")
    sys.exit(1)

api = HfApi()
out_dir = f"manifests/{HF_DATE}"
os.makedirs(out_dir, exist_ok=True)

# Single-level listing for the date folder (avoids recursive pagination)
entries = api.list_repo_tree(repo_id=HF_REPO, path=HF_DATE, recursive=False)

filelist = []
for e in entries:
    if e.type != "file":
        continue
    # CDN URL (no auth, no API rate-limit)
    cdn_url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{e.path}"
    # Prefer LFS sha256 when available; fallback to path-based deterministic id
    sha256 = getattr(e, "lfs", {}).get("sha256", "")
    if not sha256:
        sha256 = hashlib.sha256(e.path.encode()).hexdigest()
    slug = hashlib.sha256(e.path.encode()).hexdigest()[:16]
    filelist.append({
        "repo": HF_REPO,
        "path": e.path,
        "sha": sha256,
        "size": e.size,
        "url": cdn_url,
        "slug": slug,
        "date": HF_DATE,
    })

out_path = os.path.join(out_dir, "filelist.json")
with open(out_path, "w") as f:
    json.dump(filelist, f, indent=2)

print(f"Wrote {len(filelist)} entries to {out_path}")
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/build_manifest.py
```

### 3.3 `train.py` (CDN-only dataloader + robust training stub)
```python
#!/usr/bin/env python3
"""
CDN-only surrogate-1 training dataloader with checkpointing.
Embeds a pre-built manifest to avoid HF API calls during training.
"""
import json
import os
import sys
from pathlib import Path
from typing import Dict, Iterator, Any

import torch
from torch.utils.data import IterableDataset, DataLoader
import pyarrow as pa
import pyarrow.parquet as pq
import requests

# ---- Config ----
MANIFEST_PATH = os.getenv(
    "MANIFEST_PATH",
    "manifests/2026-04-29/filelist.json"
)
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "4"))
NUM_WORKERS = int(os.getenv("NUM_WORKERS", "2"))
CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "checkpoints")
os.makedirs(CHECKPOINT_DIR, exist_ok=True)
# ----

class CDNParquetIterable(IterableDataset):
    """
    Downloads parquet files via CDN (no HF API) and yields {prompt, response}.
    Projects mixed schemas to expected fields at parse time.
    """
    def __init__(self, manifest_path: str):
        super().__init__()
        with open(manifest_path) as f:
            self.files = json.load(f)
        if not self.files:
            raise ValueError("No files in manifest")

    def _stream_files(self) -> Iterator[Dict[str, Any]]:
        for meta in self.files:
            url = meta["url"]
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
            except Exception as exc:
                print(f"Failed to fetch {url}: {exc}", file=sys.stderr)
                continue

            try:
                table = pq.read_table(pa.BufferReader(resp.content))
            except Exception as exc:
                print(f"Failed to read parquet {url}: {exc}", file=sys.stderr)
                continue

            # Project to {prompt, response} regardless of source schema
            prompt_col = None
            response_col = None
            for col in table.column_names:
                lc = col.lower()
                if "prompt" in lc:
                    prompt_col = col
                if "response" in lc or "completion" in lc or "output" in lc:
                    response_col = col

            if prompt_col is None or response_col is None:
                # Fallback: use first two text columns
                text_cols = [
                    c for c in table.column_names
                    if pa.types.is_string(table.schema.field(c).type)
                ]
                if len(text_cols) >= 2:
                    prompt_col, response_col = text_cols[0], text_cols[1]
                else:
                    print(f"Skipping {url}: cannot map to prompt/response", file=sys.stderr)
                    continue

            for batch in table.to_batches():
                df = batch.to_pandas()
                for _, row in df.iterrows():
                    yield {
                        "prompt": str(row[prompt_col]).strip(),
                        "response": str(row[response_col]).strip(),
                        "_source": meta["slug"],
                    }

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        yield from self._stream_files()

def build_dataloader(manifest_path: str) -> DataLoader:
    dataset = CDNParquetIterable(manifest_path)
    return DataLoader(
        dataset,
        batch_size=BATCH_SIZE,
        num_workers=NUM_WORKERS,
        pin_memory=True,
    )

def save_checkpoint(model, optimizer, step, path):
    torch.save({
        "model_state_dict": model.state_dict(),
        "optimizer_state_dict": optimizer.state_dict(),
        "step": step,
    }, path)

def load_checkpoint(model, optimizer, path):
    cp = torch.load(path, weights_only=False)
    model.load_state_dict(cp["model_state_dict"])
    optimizer.load_state_dict(cp["optimizer_state_dict"])
    return cp["step"]

def

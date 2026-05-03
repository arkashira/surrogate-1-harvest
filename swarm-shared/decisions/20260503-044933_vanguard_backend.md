# vanguard / backend

## Final Synthesis (Best Parts + Correctness + Actionability)

**Core diagnosis (unified):**  
- No content-addressed manifest per date folder → training performs runtime `list_repo_tree`/`load_dataset` calls that trigger HF API 429s and non-reproducible epochs.  
- Data loader relies on Hugging Face `datasets` API during training instead of CDN-only fetches, violating the CDN bypass pattern and wasting rate-limit quota.  
- Missing deterministic shard selection and ordering causes shard-order drift across training runs (non-deterministic epochs).  
- No local file-list cache for Lightning Studio jobs → each worker re-queries HF API instead of using CDN URLs embedded at launch.  
- Surrogate-1 ingestion likely produces mixed-schema parquet files in `enriched/` (with `source`, `ts`) instead of strict `{prompt,response}` projection, risking `pyarrow.CastError` on load.

---

## 1. Manifest generator (single source of truth)

**Location:** `/opt/axentx/vanguard/backend/manifest.py`  
**Behavior:**  
- One `list_repo_tree` call per date folder → deterministic shard list sorted by path.  
- Emits `manifest-<date>.json` with CDN URLs, sizes, and generation timestamp.  
- CLI: `python -m vanguard.backend.manifest --repo <repo> --date <YYYY-MM-DD> --out manifest-<date>.json`  
- Importable by training and orchestration scripts; zero HF API calls during training when manifest is provided.

```python
#!/usr/bin/env python3
"""
Generate content-addressed CDN manifest for a HF dataset date folder.
Usage:
  python -m vanguard.backend.manifest --repo <repo> --date 2026-05-03 --out manifest-2026-05-03.json
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

try:
    from huggingface_hub import HfApi
except ImportError:
    print("Missing huggingface_hub. Install with: pip install huggingface_hub")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date: str, folder_prefix: str = "batches/mirror-merged") -> Dict:
    """
    Single API call to list files for one date folder, then produce CDN manifest.
    """
    api = HfApi()
    base_path = f"{folder_prefix}/{date}"
    try:
        tree = api.list_repo_tree(repo=repo, path=base_path, recursive=False)
    except Exception as exc:
        raise RuntimeError(f"Failed to list repo tree for {repo}/{base_path}: {exc}")

    entries: List[Dict] = []
    for item in tree:
        if item.type != "file":
            continue
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=item.path)
        entries.append(
            {
                "path": item.path,
                "cdn_url": cdn_url,
                "size": getattr(item, "size", None),
                "lfs": getattr(item, "lfs", None),
            }
        )

    # Deterministic ordering
    entries.sort(key=lambda x: x["path"])

    manifest = {
        "repo": repo,
        "date": date,
        "base_path": base_path,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(entries),
        "entries": entries,
    }
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN manifest for HF dataset date folder")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. 'org/dataset')")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--folder-prefix", default="batches/mirror-merged", help="Base folder in dataset")
    args = parser.parse_args()

    manifest = build_manifest(repo=args.repo, date=args.date, folder_prefix=args.folder_prefix)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written to {out_path} ({manifest['count']} files)")

if __name__ == "__main__":
    main()
```

---

## 2. Training entry (CDN-only, deterministic, Lightning-ready)

**Location:** `/opt/axentx/vanguard/backend/train.py`  
**Behavior:**  
- Accepts a pre-generated manifest and loads data via CDN URLs only (`load_dataset(..., data_files=cdn_urls)`).  
- Projects schema to `{prompt,response}` at parse time; drops extra columns to avoid `pyarrow.CastError`.  
- Deterministic shard order from manifest → reproducible epochs.  
- Lightning-compatible; can be launched by Studio with manifest baked into job spec (no runtime HF API calls).  
- Includes simple `SurrogateDataset` and training loop stub; replace model/loss/collate as needed.

```python
#!/usr/bin/env python3
"""
Lightning-compatible training entry that uses CDN-only fetches via a pre-generated manifest.
"""
import json
from pathlib import Path
from typing import Optional

import torch
from datasets import DatasetDict, load_dataset
from torch.utils.data import DataLoader, Dataset

try:
    import lightning as L
except ImportError:
    L = None

def load_from_manifest(manifest_path: Path, split: str = "train") -> DatasetDict:
    """
    Load dataset using CDN URLs from manifest (zero HF API calls during data loading).
    Assumes parquet files with at least {prompt,response} columns.
    Projects to {prompt,response} only at parse time.
    """
    manifest = json.loads(manifest_path.read_text())
    cdn_urls = [e["cdn_url"] for e in manifest["entries"]]
    if not cdn_urls:
        raise ValueError("No files in manifest")

    # load_dataset supports CDN URLs directly via data_files
    ds = load_dataset("parquet", data_files={"train": cdn_urls}, split=split)

    # Project to {prompt,response} only; drop extra cols to avoid schema issues
    required = {"prompt", "response"}
    missing = required - set(ds.column_names)
    if missing:
        raise ValueError(f"Missing required columns {missing} in dataset")

    ds = ds.select_columns(list(required))
    return DatasetDict({split: ds})

class SurrogateDataset(Dataset):
    def __init__(self, hf_dataset):
        self.dataset = hf_dataset

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, idx):
        item = self.dataset[idx]
        # tokenizer should be applied in collate_fn or model; return raw text here
        return {
            "prompt": item["prompt"],
            "response": item["response"],
        }

def train_step(batch, model, optimizer, device):
    # Placeholder training step — replace with real surrogate training logic
    model.train()
    optimizer.zero_grad()
    # Dummy loss for illustration
    loss = torch.tensor(0.0, device=device, requires_grad=True)
    loss.backward()
    optimizer.step()
    return loss.item()

def run_training(
    manifest_path: Path,
    max_steps: int = 100,
    batch_size: int = 8,
    lr: float = 1e-4,
    seed: int = 42,
):
    # Reproducibility
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    raw_ds = load_from_manifest(manifest_path)
    dataset = SurrogateDataset(raw_ds["train"])
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True)

    # Minimal surrogate model placeholder
    model = torch.nn.Linear(1, 1).to(device)  # replace with real model
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr)

    step = 0
    for epoch in range(1000):
        for batch in loader:
            loss = train_step

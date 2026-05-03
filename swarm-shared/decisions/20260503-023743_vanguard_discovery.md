# vanguard / discovery

## Final Synthesized Answer

### 1. Unified Diagnosis (Consensus)
- **Primary bottleneck**: authenticated HF API calls (`load_dataset`, `list_repo_tree`) during training burn quota and cause 429s.
- **Missing optimization**: no static `file_manifest.json`; every run re-enumerates the repo instead of using CDN-only fetches.
- **Schema fragility**: pyarrow `CastError` from heterogeneous mirror outputs when loading via HF datasets.
- **Resource waste**: no reuse check for existing Lightning Studio sessions; repeated creation wastes quota.
- **Execution locality**: training script likely runs data loading on local/Mac instead of pushing ingestion/training to remote (Lightning/Kaggle/Cerebras).

### 2. Unified Proposed Change
Create a single, self-contained training entrypoint at `/opt/axentx/vanguard/train.py` (and companion `/opt/axentx/vanguard/generate_manifest.py`) that:
- accepts a pre-generated `file_manifest.json` (per date folder)
- downloads **only via HF CDN** (no auth, no API)
- projects to `{prompt, response}` at parse time (avoids schema issues)
- optionally reuses a running Lightning Studio session
- is explicitly designed to run training remotely (Lightning/Kaggle/Cerebras) after local manifest generation

### 3. Implementation (Final, Corrected, Actionable)

#### `/opt/axentx/vanguard/generate_manifest.py`
Run once from Mac (or any machine) after rate-limit clears.

```python
#!/usr/bin/env python3
"""
Generate file manifest for a date folder (CDN-only training).
Run once per date folder, then embed manifest in training.
"""
import argparse
import json
from pathlib import Path

try:
    from huggingface_hub import HfApi
    HF_AVAILABLE = True
except Exception:
    HF_AVAILABLE = False

def list_date_folder(repo: str, date_folder: str, token: str = None):
    """
    List parquet files in datasets/{repo}/{date_folder}/ (non-recursive).
    Returns [{"path": "...", "size": ...}, ...]
    """
    if not HF_AVAILABLE:
        raise RuntimeError("huggingface_hub not installed; install to generate manifest.")
    api = HfApi(token=token)
    # Non-recursive listing to minimize API calls
    files = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    out = []
    for f in files:
        if f.rfilename.endswith(".parquet"):
            out.append({"path": f.rfilename, "size": f.size})
    return out

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. 'your-org/your-dataset')")
    parser.add_argument("--date", required=True, help="Date folder (e.g. '2026-04-29')")
    parser.add_argument("--out", required=True, help="Output manifest JSON path")
    parser.add_argument("--token", default=None, help="Optional HF token for private repos")
    args = parser.parse_args()

    entries = list_date_folder(args.repo, args.date, token=args.token)
    manifest_path = Path(args.out)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w") as f:
        json.dump(entries, f, indent=2)
    print(f"Wrote manifest with {len(entries)} files to {manifest_path}")

if __name__ == "__main__":
    main()
```

#### `/opt/axentx/vanguard/train.py`
Run remotely (Lightning Studio / Kaggle / Cerebras) or locally. Uses CDN-only fetches.

```python
#!/usr/bin/env python3
"""
Vanguard surrogate-1 training entry (discovery).
Usage:
  # On Mac (or anywhere): generate manifest once (after rate-limit window)
  python generate_manifest.py --repo <hf-repo> --date 2026-04-29 --out manifest.json

  # Lightning Studio (or remote): train with CDN-only fetches
  python train.py --manifest manifest.json --out-model ./ckpt
"""
import argparse
import json
import os
from pathlib import Path
from typing import Dict, List

import requests
import pyarrow as pa
import pyarrow.parquet as pq
import torch
from torch.utils.data import Dataset, DataLoader

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

class CDNParquetDataset(Dataset):
    def __init__(self, manifest: List[Dict], repo: str, max_files: int = None):
        self.repo = repo
        files = [m["path"] for m in manifest if m["path"].endswith(".parquet")]
        if max_files:
            files = files[:max_files]
        self.files = files

    def __len__(self) -> int:
        return len(self.files)

    def _download_bytes(self, path: str) -> bytes:
        url = HF_CDN_TEMPLATE.format(repo=self.repo, path=path)
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        return resp.content

    def _project_record(self, batch: Dict) -> Dict:
        # Keep only {prompt, response}; drop other fields to avoid schema issues.
        return {
            "prompt": batch.get("prompt", ""),
            "response": batch.get("response", ""),
        }

    def __getitem__(self, idx: int) -> Dict:
        raw = self._download_bytes(self.files[idx])
        table = pq.read_table(pa.BufferReader(raw))
        # Convert to list of dicts and project
        records = table.to_pylist()
        # If multiple rows per file, return first valid row (simple strategy)
        for rec in records:
            proj = self._project_record(rec)
            if proj["prompt"] and proj["response"]:
                return proj
        # fallback empty
        return {"prompt": "", "response": ""}


def build_dataloader(manifest_path: str, repo: str, batch_size: int = 8, max_files: int = None):
    with open(manifest_path) as f:
        manifest = json.load(f)
    dataset = CDNParquetDataset(manifest, repo=repo, max_files=max_files)
    return DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=0)


def train_step(batch, model, optimizer, device):
    # Minimal supervised step (placeholder for real surrogate training)
    # Replace with tokenizer + LM objective for production.
    model.train()
    # Dummy loss to keep training loop valid
    loss = torch.tensor(0.0, device=device, requires_grad=True)
    loss.backward()
    optimizer.step()
    optimizer.zero_grad()
    return loss.item()


def maybe_reuse_studio(name: str):
    """
    Reuse running Lightning Studio if available (saves quota).
    Requires lightning installed: pip install lightning
    """
    try:
        from lightning import Studio
        studios = Studio.list()
        for s in studios:
            if s.name == name and s.status == "running":
                print(f"Reusing running studio: {s.name}")
                return s
        print("No running studio found; will create or run locally.")
        return None
    except Exception as e:
        print(f"Studio reuse check skipped: {e}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to file_manifest.json")
    parser.add_argument("--repo", default="your-org/your-dataset", help="HF dataset repo")
    parser.add_argument("--out-model", default="./ckpt", help="Output checkpoint dir")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--studio-name", default="vanguard-training", help="Lightning Studio name to reuse")
    args = parser.parse_args()

    # Optional: reuse studio (does not block local run)
    maybe_reuse_studio(args.studio_name)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    # Tiny model placeholder (replace with real surrogate model)
    model = torch.nn.Linear(768,

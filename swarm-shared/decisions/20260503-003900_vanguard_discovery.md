# vanguard / discovery

## 1. Diagnosis
- No persisted `(repo, dateFolder)` file manifest — every training run re-enumerates via authenticated HF API, burning quota and risking 429.
- Likely recursive `list_repo_files` or `load_dataset(streaming=True)` on heterogeneous repo schema triggers `pyarrow.CastError` and wastes API calls.
- Training script probably does not separate enumeration (Mac) from training (Lightning), violating “Mac=CLI rule + heavy compute on remote”.
- No reuse strategy for Lightning Studio (create vs reuse) — wastes ~80hr/mo quota on repeated studio spin-ups.
- Data loader probably includes extra columns (`source`, `ts`) and writes mixed-schema files to `enriched/`, breaking downstream surrogate-1 expectations.

## 2. Proposed change
Create `/opt/axentx/vanguard/scripts/discovery/persist_manifest.py` and update `/opt/axentx/vanguard/train.py` (or create `train_lightning.py`) so that:
- A single Mac-side script enumerates one date folder via `list_repo_tree(recursive=False)` and writes `manifests/{repo}/{date}.json`.
- Training uses only CDN URLs from that manifest (zero authenticated API calls during data load).
- Lightning Studio is reused if already running.

Scope:
- Add `scripts/discovery/persist_manifest.py`
- Add/modify `train.py` (or create `train_lightning.py`) to accept a manifest path and use CDN-only downloads.
- Add `requirements.txt` lines if `huggingface_hub` not present.

## 3. Implementation

### File: `/opt/axentx/vanguard/scripts/discovery/persist_manifest.py`
```bash
#!/usr/bin/env bash
# persist_manifest.sh - Mac-side; run after rate-limit window clears
# Usage: bash persist_manifest.sh <repo> <date_folder> [out_dir]
# Example: bash persist_manifest.py HuggingFaceH4/opus-mt-repo 2026-04-29 ./manifests

set -euo pipefail
REPO="${1:-HuggingFaceH4/opus-mt-repo}"
DATEFOLDER="${2:-$(date +%Y-%m-%d)}"
OUTDIR="${3:-./manifests}"

mkdir -p "$OUTDIR"

python3 - "$REPO" "$DATEFOLDER" "$OUTDIR" <<'PY'
import os, json, sys
from huggingface_hub import list_repo_tree, HfApi

repo = sys.argv[1]
date_folder = sys.argv[2]
out_dir = sys.argv[3]

# Non-recursive: one API call per date folder
tree = list_repo_tree(repo=repo, path=date_folder, recursive=False)
files = [f.rfilename for f in tree if f.type == "file"]

manifest = {
    "repo": repo,
    "date": date_folder,
    "files": sorted(files),
    "cdn_urls": [f"https://huggingface.co/datasets/{repo}/resolve/main/{f}" for f in sorted(files)]
}

os.makedirs(out_dir, exist_ok=True)
out_path = os.path.join(out_dir, f"{repo.replace('/', '_')}_{date_folder}.json")
with open(out_path, "w") as f:
    json.dump(manifest, f, indent=2)

print(f"Manifest written: {out_path}")
PY
```

Make executable:
```bash
chmod +x /opt/axentx/vanguard/scripts/discovery/persist_manifest.py
```

### File: `/opt/axentx/vanguard/train_lightning.py` (new or replace)
```python
#!/usr/bin/env python3
"""
Lightning training using CDN-only fetches from persisted manifest.
Run on Mac to launch Lightning Studio (reuse if running).
"""
import json, os, sys
from pathlib import Path
from lightning import Lightning, Studio, Teamspace, Machine
from datasets import load_dataset
import torch
from torch.utils.data import Dataset, DataLoader

MANIFEST_PATH = os.getenv("MANIFEST_PATH", "./manifests/HuggingFaceH4_opus-mt-repo_2026-04-29.json")
REPO = os.getenv("REPO", "HuggingFaceH4/opus-mt-repo")

class CDNTextDataset(Dataset):
    def __init__(self, manifest_path, max_files=None):
        with open(manifest_path) as f:
            m = json.load(f)
        self.urls = m["cdn_urls"]
        if max_files:
            self.urls = self.urls[:max_files]

    def __len__(self):
        return len(self.urls)

    def __getitem__(self, idx):
        # Lightweight: stream single file and project to {prompt,response}
        # Replace with your actual parsing logic per file schema.
        import requests
        url = self.urls[idx]
        r = requests.get(url, timeout=30)
        text = r.text
        # Placeholder projection — adapt to your corpus
        return {"prompt": "", "response": text[:2048]}

def reuse_or_create_studio(name="vanguard-train", machine=Machine.L40S):
    ts = Teamspace()
    for s in ts.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {s.name}")
            return s
    print(f"Creating studio: {name}")
    return Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )

def train():
    manifest = MANIFEST_PATH
    if not os.path.exists(manifest):
        print(f"Manifest missing: {manifest}. Run persist_manifest.py first.")
        sys.exit(1)

    # Reuse studio to save quota
    studio = reuse_or_create_studio()

    # Build DataLoader that uses CDN URLs only (no HF API calls during training)
    ds = CDNTextDataset(manifest, max_files=1000)
    loader = DataLoader(ds, batch_size=8, shuffle=True, num_workers=4)

    # Minimal training step placeholder
    model = torch.nn.Linear(1024, 1024)  # replace with real model
    opt = torch.optim.Adam(model.parameters(), lr=1e-4)

    studio.run(
        function=lambda: run_training(model, loader, opt),
        requirements=["torch", "datasets", "requests"],
    )

def run_training(model, loader, opt, epochs=1):
    model.train()
    for epoch in range(epochs):
        for batch in loader:
            # Placeholder: adapt to real surrogate-1 objective
            x = torch.randn(len(batch["response"]), 1024)  # mock embed
            loss = model(x).sum() * 0.0
            opt.zero_grad()
            loss.backward()
            opt.step()
            print(f"loss: {loss.item()}")

if __name__ == "__main__":
    train()
```

### Optional: requirements update
If not present, ensure `huggingface_hub` and `lightning` are available:
```text
huggingface_hub>=0.22.0
lightning>=2.3.0
datasets>=2.18.0
requests>=2.31.0
```

## 4. Verification
1. On Mac, run:
   ```bash
   bash /opt/axentx/vanguard/scripts/discovery/persist_manifest.py HuggingFaceH4/opus-mt-repo 2026-04-29 ./manifests
   ```
   Confirm `manifests/HuggingFaceH4_opus-mt-repo_2026-04-29.json` exists and contains `cdn_urls`.

2. Validate CDN accessibility (no auth):
   ```bash
   curl -I "$(jq -r '.cdn_urls[0]' manifests/HuggingFaceH4_opus-mt-repo_2026-04-29.json)"
   ```
   Expect HTTP 200.

3. Dry-run training locally (small):
   ```bash
   MANIFEST_PATH=./manifests/HuggingFaceH4_opus-mt-repo_2026-04-29.json python3 train_lightning.py --dry-run 2>&1 | head -20
   ```
   Confirm no `huggingface_hub` API calls appear in logs (only CDN fetches).

4. In Lightning workflow:
   - Launch via `python3 train_lightning.py` on

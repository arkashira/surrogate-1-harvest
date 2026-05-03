# vanguard / backend

## 1. Diagnosis

- No content-addressed manifest for dataset ingestion: ingestion scripts likely re-list HF repos at runtime, causing 429 rate-limits and non-reproducible runs.
- Mixed-schema files from `dataset-mirror` probably land in `enriched/` without projection to `{prompt,response}`, risking `pyarrow.CastError` during surrogate-1 training.
- Lightning Studio reuse is not implemented: training jobs probably recreate studios instead of reusing running ones, burning 80+ hours/month of quota.
- HF API calls during data loading in training: runtime `load_dataset` or recursive `list_repo_files` triggers CDN auth path and rate-limits; no CDN-only fetch strategy.
- No deterministic file list embedded in training: training script cannot run with zero HF API calls, making Lightning runs fragile and non-reproducible.

## 2. Proposed change

Create a backend ingestion/training utility that:
- Generates a content-addressed manifest (JSON) for one date folder after mirror ingestion.
- Projects mixed-schema files to `{prompt,response}` only and writes to `batches/mirror-merged/{date}/{slug}.parquet` (no extra metadata columns).
- Embeds the manifest into `train.py` so Lightning training uses CDN-only fetches (zero HF API calls).
- Reuses a running Lightning Studio if present instead of recreating.

Scope:
- Add `/opt/axentx/vanguard/backend/ingest_manifest.py`
- Add `/opt/axentx/vanguard/backend/train.py` (or update existing)
- Add small CLI wrapper to generate manifest after mirror ingestion.

## 3. Implementation

```bash
# /opt/axentx/vanguard/backend/ingest_manifest.py
#!/usr/bin/env bash
set -euo pipefail

# Usage: ./ingest_manifest.sh <date> <hf_repo> <out_dir>
# Example: ./ingest_manifest.sh 2026-05-03 axentx/dataset-mirror ./manifests

DATE="${1:-$(date +%Y-%m-%d)}"
HF_REPO="${2:-axentx/dataset-mirror}"
OUT_DIR="${3:-./manifests}"

mkdir -p "$OUT_DIR/$DATE"

# Use HF API once (after rate-limit window) to list top-level folder only
# Save JSON file list for CDN-only training
python3 - "$HF_REPO" "$DATE" "$OUT_DIR" <<'PY'
import os, json, sys
from huggingface_hub import list_repo_tree

repo = sys.argv[1]
date = sys.argv[2]
out_dir = sys.argv[3]

# List one folder (non-recursive) for the date
tree = list_repo_tree(repo, path=date, recursive=False)
files = [f.rfilename for f in tree if f.type == "file"]

manifest = {
    "repo": repo,
    "date": date,
    "files": files,
    "cdn_prefix": f"https://huggingface.co/datasets/{repo}/resolve/main/{date}"
}

out_path = os.path.join(out_dir, date, "manifest.json")
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w") as f:
    json.dump(manifest, f, indent=2)
print(f"Manifest written to {out_path}")
PY

echo "Manifest generated: $OUT_DIR/$DATE/manifest.json"
```

```python
# /opt/axentx/vanguard/backend/project_to_parquet.py
#!/usr/bin/env python3
"""
Project mixed-schema HF files to {prompt,response} and write to
batches/mirror-merged/{date}/{slug}.parquet

Run after manifest generation. Uses hf_hub_download per file to avoid
load_dataset streaming/schema issues.
"""
import json, os, hashlib
from pathlib import Path
from huggingface_hub import hf_hub_download
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

def safe_project(raw) -> dict:
    """Return {prompt, response} from heterogeneous raw record."""
    if isinstance(raw, dict):
        prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
        response = raw.get("response") or raw.get("output") or raw.get("answer") or ""
        return {"prompt": str(prompt), "response": str(response)}
    return {"prompt": "", "response": ""}

def process_date(manifest_path: str, out_root: str = "batches/mirror-merged"):
    with open(manifest_path) as f:
        manifest = json.load(f)

    repo = manifest["repo"]
    date = manifest["date"]
    out_dir = Path(out_root) / date
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for fname in manifest["files"]:
        # Skip non-data files
        if not fname.endswith((".json", ".jsonl", ".parquet", ".csv")):
            continue

        local_path = hf_hub_download(repo_id=repo, filename=f"{date}/{fname}")
        slug = hashlib.sha256(fname.encode()).hexdigest()[:16]

        try:
            if fname.endswith(".parquet"):
                df = pd.read_parquet(local_path)
            elif fname.endswith(".csv"):
                df = pd.read_csv(local_path)
            else:
                # JSON/JSONL
                df = pd.read_json(local_path, lines=True, dtype=False)
        except Exception as e:
            print(f"Skip {fname}: {e}")
            continue

        # Project to prompt/response
        projected = [safe_project(r) for r in df.to_dict(orient="records")]
        rows.extend(projected)

        # Per-file parquet (optional)
        pq.write_table(pa.Table.from_pylist(projected), out_dir / f"{slug}.parquet")

    # Combined parquet for the date
    if rows:
        combined = pa.Table.from_pylist(rows)
        pq.write_table(combined, out_dir / "combined.parquet")
        print(f"Wrote {len(rows)} rows to {out_dir}/combined.parquet")
    else:
        print("No rows to write.")

if __name__ == "__main__":
    import sys
    manifest = sys.argv[1] if len(sys.argv) > 1 else "./manifests/2026-05-03/manifest.json"
    process_date(manifest)
```

```python
# /opt/axentx/vanguard/backend/train.py
#!/usr/bin/env python3
"""
Surrogate-1 training with CDN-only dataset fetches and Lightning Studio reuse.

Expects manifest JSON at ./manifests/{date}/manifest.json embedded or passed.
"""
import json, os, sys
from pathlib import Path
from lightning import Fabric, LightningModule, Trainer
from lightning.pytorch.strategies import DeepSpeedStrategy
from torch.utils.data import Dataset, DataLoader
import torch
import pyarrow.parquet as pq

class CDNParquetDataset(Dataset):
    def __init__(self, parquet_path: str):
        self.table = pq.read_table(parquet_path)
        self.prompts = self.table["prompt"].to_pylist()
        self.responses = self.table["response"].to_pylist()

    def __len__(self):
        return len(self.prompts)

    def __getitem__(self, idx):
        # Tokenization placeholder — replace with surrogate-1 tokenizer
        text = f"Prompt: {self.prompts[idx]}\nResponse: {self.responses[idx]}"
        return {"text": text}

class Surrogate1(LightningModule):
    def __init__(self):
        super().__init__()
        self.lm = torch.nn.Linear(1024, 1024)  # placeholder

    def training_step(self, batch, batch_idx):
        return torch.tensor(0.0)

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-4)

def find_running_studio(name: str = "vanguard-surrogate1"):
    try:
        from lightning import Teamspace
        for s in Teamspace.studios:
            if s.name == name and s.status == "Running":
                return s
    except Exception:
        pass
    return None

def main():
    manifest_path = Path("./manifests/2026-05-03/manifest.json")
    if not manifest_path.exists():
        print("Manifest not found. Generate with ingest_manifest.sh first.")
        sys.exit(1

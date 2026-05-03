# vanguard / backend

## 1. Diagnosis
- No persisted `(repo, dateFolder)` manifest: every training run re-enumerates via authenticated HF API → quota burn + 429 risk.
- Recursive/heavy enumeration exposes mixed-schema files and wastes I/O during data discovery.
- Training script still relies on `load_dataset(streaming=True)` on heterogeneous repos → pyarrow `CastError` at runtime.
- Lightning Studio recreation on each run wastes 80hr/mo quota; no reuse logic for running studios.
- Data ingestion writes mixed attribution columns (`source`, `ts`) and schema noise into `enriched/` instead of projecting to `{prompt, response}` only.

## 2. Proposed change
- Add a lightweight backend manifest generator + training launcher under `/opt/axentx/vanguard/backend/`:
  - `manifest.py`: one-shot `list_repo_tree` for a date folder → write `manifests/{repo}/{date}.json` (file paths only).
  - `train.py`: reads manifest, uses HF CDN-only URLs (`resolve/main/...`) for parquet files, projects to `{prompt, response}`, and launches Lightning Studio with reuse logic.
- Scope: new files only; no changes to existing ingestion pipelines.

## 3. Implementation

```bash
# /opt/axentx/vanguard/backend/
mkdir -p manifests
```

### manifest.py
```python
#!/usr/bin/env python3
"""
Generate a file-path manifest for a repo+dateFolder.
Run from Mac (or any machine with HF token) once per dateFolder.
Avoids recursive enumeration and authenticated calls during training.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from huggingface_hub import HfApi, list_repo_tree

HF_REPO = os.getenv("HF_REPO", "datasets/your-org/your-repo")
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.utcnow().strftime("%Y-%m-%d"))
OUT_DIR = Path(__file__).parent / "manifests" / HF_REPO.replace("/", "_")
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_PATH = OUT_DIR / f"{DATE_FOLDER}.json"

def main() -> None:
    api = HfApi()
    # Non-recursive, single page per folder; we target one dateFolder at a time.
    tree = list_repo_tree(
        repo_id=HF_REPO,
        path=DATE_FOLDER,
        recursive=False,
        repo_type="dataset",
    )
    files = [item.rfilename for item in tree if item.type == "file"]
    manifest = {
        "repo": HF_REPO,
        "date": DATE_FOLDER,
        "generated_utc": datetime.utcnow().isoformat() + "Z",
        "files": sorted(files),
    }
    OUT_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {OUT_PATH} ({len(files)} files)")

if __name__ == "__main__":
    main()
```

### train.py
```python
#!/usr/bin/env python3
"""
Lightning-based surrogate-1 training launcher.
- Uses persisted manifest to avoid HF API calls during training.
- Downloads via HF CDN (resolve/main) to bypass auth rate limits.
- Projects parquet files to {prompt, response} only.
- Reuses running Lightning Studio when available.
"""
import json
import os
import sys
from pathlib import Path

import lightning as L
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import requests
from lightning.pytorch.utilities import Memory

HF_CDN = "https://huggingface.co/datasets"
MANIFEST_PATH = Path(__file__).parent / "manifests"

def load_manifest(repo: str, date: str) -> dict:
    p = MANIFEST_PATH / repo.replace("/", "_") / f"{date}.json"
    if not p.is_file():
        raise FileNotFoundError(f"Manifest not found: {p}")
    return json.loads(p.read_text())

def cdn_url(repo: str, file_path: str) -> str:
    return f"{HF_CDN}/{repo}/resolve/main/{file_path}"

def stream_parquet_rows(url: str):
    # Stream remote parquet without full download; project to prompt/response.
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    with pa.BufferReader(resp.content) as buf:
        table = pa.parquet.read_table(buf)
        # Keep only prompt/response; ignore mixed schema noise.
        keep = [c for c in table.column_names if c in {"prompt", "response"}]
        if not keep:
            # Fallback: try common aliases
            for alt in [("instruction", "prompt"), ("input", "prompt"), ("output", "response")]:
                if alt[0] in table.column_names and alt[1] not in keep:
                    keep.append(alt[0])
        if len(keep) < 2:
            raise ValueError(f"Cannot find prompt/response in {url}")
        table = table.select(keep)
        # Rename to canonical names
        rename = {}
        for c in table.column_names:
            if c == "instruction":
                rename[c] = "prompt"
            elif c == "output":
                rename[c] = "response"
        if rename:
            table = table.rename_columns([rename.get(c, c) for c in table.column_names])
        for batch in table.to_batches(max_chunksize=512):
            df = batch.to_pandas()
            for _, row in df.iterrows():
                yield {"prompt": str(row["prompt"]), "response": str(row["response"])}

def build_dataset(manifest: dict):
    rows = []
    for f in manifest["files"]:
        if not f.endswith(".parquet"):
            continue
        url = cdn_url(manifest["repo"], f)
        try:
            for row in stream_parquet_rows(url):
                rows.append(row)
        except Exception as exc:
            print(f"Skipping {f}: {exc}")
    return rows

def launch_studio_job(data_rows, repo: str, date: str):
    # Reuse running studio when available to save quota.
    teamspace = L.Teamspace()
    studio_name = f"vanguard-{repo.replace('/', '-')}-{date}"
    studio = None
    for s in teamspace.studios:
        if s.name == studio_name and s.status == "running":
            studio = s
            print(f"Reusing running studio: {studio_name}")
            break

    if studio is None or studio.status != "running":
        studio = L.Studio(
            name=studio_name,
            machine=L.Machine.L40S,
            create_ok=True,
        )
        print(f"Created studio: {studio_name}")

    # Simple training stub: replace with real surrogate-1 training script.
    script = """
import lightning as L
from torch.utils.data import Dataset, DataLoader

class TextDataset(Dataset):
    def __init__(self, rows):
        self.rows = rows
    def __len__(self):
        return len(self.rows)
    def __getitem__(self, idx):
        return self.rows[idx]

class Surrogate1(L.LightningModule):
    def __init__(self):
        super().__init__()
        # placeholder model
        self.lm = None
    def training_step(self, batch, batch_idx):
        return {"loss": self.lm(batch["prompt"]) if self.lm else 0.0}
    def configure_optimizers(self):
        return None

ds = TextDataset(__ROWS__)
dl = DataLoader(ds, batch_size=8)
m = Surrogate1()
trainer = L.Trainer(max_epochs=1, limit_train_batches=2)
trainer.fit(m, dl)
""".replace("__ROWS__", repr(data_rows[:128]))  # small sample for CI

    # Ensure studio is running before submit.
    if studio.status != "running":
        studio.start(machine=L.Machine.L40S)
    job = studio.run(script, name=f"train-{date}", wait=False)
    print(f"Launched job: {job}")
    return job

def main() -> None:
    repo = os.getenv("HF_REPO", "datasets/your-org/your-repo")
    date = os.getenv("DATE_FOLDER", "2026-04-27")
    manifest = load_manifest(repo, date)
    print(f"Loaded manifest: {len(manifest

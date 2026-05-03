# vanguard / quality

## 1. Diagnosis

- No content-addressed manifest: ingestion/training scripts likely re-list HF repos at runtime, causing 429s and non-reproducible runs.
- Mixed-schema files probably land in `enriched/` without projection to `{prompt,response}`, risking `pyarrow.CastError` during surrogate-1 training.
- Lightning Studio is probably recreated on every run instead of reused, burning quota and risking idle-stop training death.
- No CDN-only data path: training likely uses `load_dataset`/`list_repo_files` during data loading instead of embedding a pre-listed file manifest and fetching via CDN.
- No idle-stop guard: if a Lightning Studio stops between planning and execution, `.run()` will fail instead of restarting the studio.

## 2. Proposed change

Add a small, high-leverage orchestration module that:
- Pre-lists one date folder via HF API (single call) and writes `manifests/{date}/files.json`.
- Projects any raw/mixed-schema files to `{prompt,response}` on ingest and stores as `batches/mirror-merged/{date}/{slug}.parquet` (no extra metadata columns).
- Reuses a running Lightning Studio or restarts it if stopped, then submits a training run that uses only CDN URLs from the manifest (zero API calls during training).

Scope:
- Create `/opt/axentx/vanguard/orchestrator.py`
- Create `/opt/axentx/vanguard/train.py` (CDN-only dataloader)
- Add helper: `/opt/axentx/vanguard/project.py` (schema projection)
- Update any existing entrypoint/cron to invoke via `bash orchestrator.py` with proper shebang and `SHELL=/bin/bash` if cron is used.

## 3. Implementation

```bash
# Ensure executable and shebang for cron safety
cat > /opt/axentx/vanguard/orchestrator.py <<'PY'
#!/usr/bin/env bash
# orchestrator.sh equivalent in Python-friendly wrapper
# Usage: bash orchestrator.py <date> <hf_repo>
set -euo pipefail
SHELL=/bin/bash
exec python3 -m vanguard.orchestrator "${@}"
PY

mkdir -p /opt/axentx/vanguard
cat > /opt/axentx/vanguard/__main__.py <<'PY'
import sys
from vanguard.orchestrator import main
if __name__ == "__main__":
    sys.exit(main())
PY

cat > /opt/axentx/vanguard/orchestrator.py <<'PY'
import json
import os
import datetime
from pathlib import Path

import requests
from huggingface_hub import list_repo_tree, hf_hub_download, HfApi

from vanguard.project import project_to_prompt_response
from vanguard.lightning_utils import get_or_start_studio, run_training

HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/mirror-merged")
MANIFEST_ROOT = Path("manifests")
BATCH_ROOT = Path("batches/mirror-merged")

def list_date_folder(date_str: str):
    """Single API call: list one date folder (non-recursive)."""
    folder = f"{date_str}"
    items = list_repo_tree(repo_id=HF_REPO, path=folder, recursive=False)
    files = [it.rfilename for it in items if it.type == "file"]
    return files

def build_manifest(date_str: str):
    manifest_path = MANIFEST_ROOT / date_str / "files.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    files = list_date_folder(date_str)
    manifest = {
        "date": date_str,
        "repo": HF_REPO,
        "files": files,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z"
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest

def ingest_and_project(date_str: str, files: list):
    """Download each file, project to {prompt,response}, write parquet per slug."""
    BATCH_ROOT.mkdir(parents=True, exist_ok=True)
    out_rows = []
    for f in files:
        local_path = hf_hub_download(repo_id=HF_REPO, filename=f"{date_str}/{f}")
        projected = project_to_prompt_response(local_path)
        slug = Path(f).stem
        parquet_path = BATCH_ROOT / date_str / f"{slug}.parquet"
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        projected.to_parquet(parquet_path, index=False)
        # CDN URL for training (no auth)
        cdn_url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{date_str}/{f}"
        out_rows.append({"slug": slug, "parquet": str(parquet_path), "cdn_url": cdn_url})
    return out_rows

def main():
    date_str = os.getenv("VANGUARD_DATE", datetime.date.today().isoformat())
    print(f"[orchestrator] Building manifest for {date_str}")
    manifest = build_manifest(date_str)

    print("[orchestrator] Ingesting + projecting")
    assets = ingest_and_project(date_str, manifest["files"])

    # Write training manifest (CDN-only)
    train_manifest = Path("train_manifest.json")
    train_manifest.write_text(json.dumps({
        "date": date_str,
        "assets": assets
    }, indent=2))

    # Reuse or start Lightning Studio, then run training
    studio = get_or_start_studio(name="vanguard-train", machine="L40S")
    run_training(studio, train_manifest_path=str(train_manifest))
    print("[orchestrator] Done")

if __name__ == "__main__":
    main()
PY

cat > /opt/axentx/vanguard/project.py <<'PY'
import pandas as pd
from pathlib import Path

def project_to_prompt_response(file_path):
    """
    Project mixed-schema file to {prompt,response} only.
    Supports JSONL/JSON/CSV/Parquet inputs.
    """
    p = Path(file_path)
    if p.suffix == ".parquet":
        df = pd.read_parquet(p)
    elif p.suffix == ".jsonl":
        df = pd.read_json(p, lines=True)
    elif p.suffix == ".json":
        df = pd.read_json(p)
    elif p.suffix == ".csv":
        df = pd.read_csv(p)
    else:
        raise ValueError(f"Unsupported file type: {p.suffix}")

    # Heuristic: find prompt/response columns (case-insensitive)
    cols = {c.lower(): c for c in df.columns}
    prompt_col = None
    response_col = None
    for k in cols:
        if "prompt" in k:
            prompt_col = cols[k]
        if "response" in k or "completion" in k or "answer" in k:
            response_col = cols[k]

    if prompt_col is None or response_col is None:
        # Fallback: first text col as prompt, second as response
        text_cols = [c for c in df.columns if df[c].dtype == "object"]
        if len(text_cols) < 2:
            raise ValueError("Could not identify prompt/response columns")
        prompt_col, response_col = text_cols[0], text_cols[1]

    out = pd.DataFrame({
        "prompt": df[prompt_col].astype(str),
        "response": df[response_col].astype(str)
    })
    return out
PY

cat > /opt/axentx/vanguard/lightning_utils.py <<'PY'
from lightning_sdk import Teamspace, Studio, Machine
import time
import json
from pathlib import Path

def get_or_start_studio(name: str, machine: str = "L40S"):
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name:
            if s.status == "Running":
                print(f"[lightning] Reusing running studio: {name}")
                return s
            else:
                print(f"[lightning] Restarting stopped studio: {name}")
                s.start(machine=Machine(machine))
                # Wait until running
                while s.status != "Running":
                    time.sleep(10)
                    s.refresh()
                return s
    print(f"[lightning] Creating studio: {name}")
    return Studio.create(name=name, machine=Machine(machine), create_ok=True)

def run_training(studio: Studio, train_manifest_path: str):
    # Copy train.py and manifest into studio,

# vanguard / backend

## 1. Diagnosis
- No canonical discovery entrypoint exists to surface top-hub insights (e.g., MOC) before planning — violates `#knowledge-rag #graph #hub` pattern and forces ad-hoc exploration.
- Missing CDN-bypass file-list strategy for HF datasets; any future surrogate-1 training will hit API rate limits instead of using `resolve/main/` CDN fetches.
- No reusable Lightning Studio orchestration to avoid quota burn (create vs reuse) and idle-stop training loss.
- No wrapper script hygiene (shebang, executable, `SHELL=/bin/bash` in cron) for future `opus-pr-reviewer` / `active-learning` jobs.
- No centralized HF ingestion path that projects to `{prompt, response}` only and uses `batches/mirror-merged/{date}/{slug}.parquet` schema.

## 2. Proposed change
Create `/opt/axentx/vanguard/backend/orchestrate.py` (single orchestrator) + `/opt/axentx/vanguard/backend/train.py` (CDN-only data loader stub) + cron wrapper `/opt/axentx/vanguard/backend/run_vanguard.sh`.  
Scope: add files only; no existing files modified.

## 3. Implementation

```bash
# /opt/axentx/vanguard/backend/run_vanguard.sh
#!/usr/bin/env bash
set -euo pipefail
export SHELL=/bin/bash

cd "$(dirname "$0")/.."
exec python -m backend.orchestrate "$@"
```

```python
# /opt/axentx/vanguard/backend/orchestrate.py
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import lightning as L
    from lightning.fabric.accelerators import L40S
    HF_AVAILABLE = True
except Exception:
    HF_AVAILABLE = False

REPO_ROOT = Path(__file__).parent.parent
HF_REPO = os.getenv("HF_DATASET_REPO", "datasets/example/mirror-merged")
DATE_FOLDER = os.getenv("HF_DATE_FOLDER", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
FILE_LIST_PATH = REPO_ROOT / "backend" / "file_list.json"

def top_hub_insight() -> dict:
    """
    Canonical discovery: surface top-hub (e.g., MOC) insights before planning.
    Returns minimal context to guide downstream tasks.
    Tags: #knowledge-rag #graph #hub
    """
    # Placeholder: integrate with knowledge-rag when available.
    # For now, return deterministic stub so planning is consistent.
    return {
        "hub": "MOC",
        "rank": 1,
        "degree": 0,
        "summary": "Top hub (MOC) — review context before planning.",
        "tags": ["#knowledge-rag", "#graph", "#hub"]
    }

def list_hf_files_cdn_bypass() -> list[str]:
    """
    Single API call to list files for one date folder (non-recursive).
    Embed result into file_list.json so training can use CDN-only fetches.
    Tags: #huggingface #cdn #rate-limit-bypass #training
    """
    try:
        from huggingface_hub import HfApi
    except Exception as e:
        print("huggingface_hub not available, skipping HF listing", file=sys.stderr)
        return []

    api = HfApi()
    # Non-recursive to avoid pagination explosion.
    entries = api.list_repo_tree(repo_id=HF_REPO, path=DATE_FOLDER, recursive=False)
    files = [f.rfilename for f in entries if f.rfilename.endswith(".parquet")]
    # Save for training script.
    FILE_LIST_PATH.write_text(json.dumps(files, indent=2))
    print(f"Listed {len(files)} files -> {FILE_LIST_PATH}", file=sys.stderr)
    return files

def reuse_or_create_studio(name: str = "vanguard-train"):
    """
    Reuse running studio to save quota; restart if idle-stopped.
    Tags: #lightning-ai #quota
    """
    if not HF_AVAILABLE:
        print("Lightning not available, skipping studio reuse", file=sys.stderr)
        return None

    teamspace = L.Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "running":
            print(f"Reusing running studio: {name}", file=sys.stderr)
            return s

    print(f"Creating studio: {name}", file=sys.stderr)
    # Prefer L40S; fall back to available free tier.
    try:
        studio = L.Studio.create(
            name=name,
            machine=L40S(),
            open=True,
            create_ok=True,
        )
    except Exception:
        # free tier fallback
        studio = L.Studio.create(name=name, open=False, create_ok=True)
    return studio

def run_training_on_studio(file_list_path: Path):
    """
    Launch CDN-only training on Lightning Studio.
    """
    studio = reuse_or_create_studio("vanguard-train")
    if studio is None:
        print("No studio available; skipping training launch", file=sys.stderr)
        return

    # Copy train script and file list into studio workspace (simplified).
    # In practice, mount or rsync; here we run locally via studio.run for demo.
    try:
        studio.run(
            str((REPO_ROOT / "backend" / "train.py").absolute()),
            args=[str(file_list_path)],
        )
    except Exception as e:
        print(f"Studio run failed (may be idle-stopped): {e}", file=sys.stderr)
        # restart and retry once
        studio.start(machine=L40S() if HF_AVAILABLE else None)
        studio.run(
            str((REPO_ROOT / "backend" / "train.py").absolute()),
            args=[str(file_list_path)],
        )

def main():
    # 1) Canonical discovery
    insight = top_hub_insight()
    print(json.dumps(insight, indent=2))

    # 2) CDN-bypass file list (single API call)
    files = list_hf_files_cdn_bypass()
    if not files:
        print("No files listed; training may fail", file=sys.stderr)

    # 3) Launch training on Lightning (reuse studio)
    if FILE_LIST_PATH.exists():
        run_training_on_studio(FILE_LIST_PATH)
    else:
        print("No file_list.json; skipping training launch", file=sys.stderr)

if __name__ == "__main__":
    main()
```

```python
# /opt/axentx/vanguard/backend/train.py
import json
import sys
from pathlib import Path

# CDN-only dataset loader (zero API calls during training).
# HF CDN: https://huggingface.co/datasets/{repo}/resolve/main/{path}
# Tags: #huggingface #cdn #rate-limit-bypass #training

HF_CDN_BASE = "https://huggingface.co/datasets"
HF_REPO = "datasets/example/mirror-merged"  # override via env if needed

def load_parquet_cdn_only(file_rel: str):
    """
    Load a single parquet via CDN URL.
    Project to {prompt, response} only at parse time.
    Attribution via filename pattern: batches/mirror-merged/{date}/{slug}.parquet
    Tags: #ingestion #schema #surrogate-1
    """
    import pandas as pd
    url = f"{HF_CDN_BASE}/{HF_REPO}/resolve/main/{file_rel}"
    df = pd.read_parquet(url)
    # Project to canonical surrogate-1 fields.
    # Keep minimal attribution in filename, not columns.
    if "prompt" not in df.columns or "response" not in df.columns:
        # Best-effort fallback: try common aliases.
        col_map = {}
        for c in df.columns:
            low = c.lower()
            if "prompt" in low:
                col_map[c] = "prompt"
            elif "response" in low or "completion" in low or "answer" in low:
                col_map[c] = "response"
        if col_map:
            df = df.rename(columns=col_map)
    # Ensure required fields exist (may be empty).
    if "prompt" not in df.columns:
        df["prompt"] = ""
    if "response" not in df.columns:
        df["response"] = ""
    return df[["prompt", "response"]]

def main(file_list_path: str):
   

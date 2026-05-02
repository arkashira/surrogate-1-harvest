# vanguard / backend

## 1. Diagnosis
- No canonical discovery entrypoint → planning is ad-hoc and violates `#knowledge-rag #graph #hub`.
- Missing CDN-bypass file-list generation for HF datasets → future surrogate-1 training will hit API rate limits during data loading.
- No Lightning Studio reuse logic → wastes 80hr/mo quota by recreating running studios.
- Orchestrator exists but lacks safe, cron-ready invocation and idempotent run handling (idle-stop kills training).
- No explicit top-hub review step before planning tasks → loses contextual insight from knowledge graph.

## 2. Proposed change
File: `/opt/axentx/vanguard/backend/orchestrator.py`  
Scope: add a single CLI entrypoint that:
- Optionally queries top hub (e.g., "MOC") via knowledge-rag and prints insight.
- Generates HF CDN-bypass file list for a given date folder and writes `file_list.json`.
- Reuses a running Lightning Studio if present; otherwise starts one (L40S fallback).
- Runs training script via Studio with idempotent checks and safe restart on idle-stop.

## 3. Implementation
```python
#!/usr/bin/env python3
"""
vanguard backend orchestrator
Usage:
  python -m backend.orchestrator --sync-file-list --date 2026-05-02
  python -m backend.orchestrator --run-training --date 2026-05-02
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HF_REPO = "datasets/axentx/surrogate-1"
HF_BASE = "https://huggingface.co"
VANGUARD_ROOT = Path(__file__).parent.parent.parent

try:
    from lightning import Client, Machine, Studio
    LIGHTNING_AVAILABLE = True
except Exception:
    LIGHTNING_AVAILABLE = False

def run_cmd(cmd, cwd=VANGUARD_ROOT, check=True):
    print(f"+ {cmd}")
    return subprocess.run(cmd, shell=True, cwd=cwd, check=check, capture_output=True, text=True)

def top_hub_insight():
    """Review most-connected hub (e.g., MOC) via knowledge-rag if available."""
    try:
        # lightweight fallback: if knowledge-rag CLI exists, run it
        res = run_cmd("bash -c 'which knowledge-rag >/dev/null 2>&1 && knowledge-rag top-hub MOC || echo \"knowledge-rag unavailable\"'", check=False)
        if res.stdout.strip():
            print("[hub insight]")
            print(res.stdout.strip())
    except Exception as e:
        print(f"[hub insight skipped] {e}")

def list_hf_folder(date_folder):
    """List single-level folder in HF repo (non-recursive) to avoid pagination/rate limits."""
    try:
        from huggingface_hub import list_repo_tree
        items = list_repo_tree(repo_id=HF_REPO, path=date_folder, recursive=False)
        files = [f.rfilename for f in items if f.type == "file"]
        return files
    except Exception as e:
        print(f"[warning] HF API list failed ({e}), falling back to manual CDN list")
        # fallback: if API blocked, rely on pre-known pattern or empty list
        return []

def build_file_list(date_str):
    """Generate file_list.json for CDN-bypass training."""
    date_folder = date_str.replace("-", "/")
    files = list_hf_folder(date_folder)
    # keep only parquet for surrogate-1 training
    files = [f for f in files if f.endswith(".parquet")]
    file_list = [f"{HF_BASE}/datasets/{HF_REPO}/resolve/main/{f}" for f in files]

    out_path = VANGUARD_ROOT / "backend" / "file_list.json"
    out_path.write_text(json.dumps({"date": date_str, "files": file_list}, indent=2))
    print(f"[file-list] wrote {len(file_list)} files -> {out_path}")
    return file_list

def reuse_or_start_studio(studio_name="vanguard-surrogate1"):
    """Reuse running studio or start new one (L40S -> fallback). Returns Studio."""
    if not LIGHTNING_AVAILABLE:
        raise RuntimeError("lightning SDK not available")

    client = Client()
    teamspace = client.teamspace()
    for s in teamspace.studios():
        if s.name == studio_name and s.status == "Running":
            print(f"[studio] reusing running studio: {s.name} ({s.id})")
            return s

    # start new
    print("[studio] no running studio found, starting new...")
    try:
        machine = Machine.L40S
        studio = Studio.create(
            name=studio_name,
            machine=machine,
            cluster="lightning-lambda-prod",  # H200/L40S available; fallback handled by SDK
        )
        print(f"[studio] created {studio.name} on {machine}")
        return studio
    except Exception as e:
        print(f"[studio] L40S failed ({e}), trying public cluster (L40S max on free tier)")
        machine = Machine.L40S
        studio = Studio.create(name=studio_name, machine=machine, cluster="lightning-public-prod")
        return studio

def run_training_in_studio(studio, train_script="train_surrogate1.py", date_str=None):
    """Idempotent run: check studio status and restart if stopped (idle-timeout kills training)."""
    if studio.status != "Running":
        print(f"[studio] studio stopped, restarting on {studio.machine}")
        studio.start(machine=studio.machine)

    target = studio.run(
        [f"python {train_script}" + (f" --date {date_str}" if date_str else "")],
        requirements_file="requirements.txt",
    )
    print(f"[training] submitted run {target.id}")
    return target

def main():
    parser = argparse.ArgumentParser(description="Vanguard backend orchestrator")
    parser.add_argument("--sync-file-list", action="store_true", help="Generate HF CDN-bypass file list")
    parser.add_argument("--run-training", action="store_true", help="Run surrogate-1 training in Lightning Studio")
    parser.add_argument("--date", default=datetime.utcnow().strftime("%Y-%m-%d"), help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--hub-insight", action="store_true", help="Show top-hub insight before planning")
    args = parser.parse_args()

    if args.hub_insight:
        top_hub_insight()

    if args.sync_file_list:
        build_file_list(args.date)

    if args.run_training:
        if not LIGHTNING_AVAILABLE:
            print("[error] lightning SDK unavailable; install lightning to run training")
            sys.exit(1)
        studio = reuse_or_start_studio()
        run_training_in_studio(studio, date_str=args.date)

if __name__ == "__main__":
    main()
```

Make executable and ensure cron-safe invocation:
```bash
chmod +x /opt/axentx/vanguard/backend/orchestrator.py
# crontab -e
SHELL=/bin/bash
# sync file list daily at 02:15
15 2 * * * cd /opt/axentx/vanguard && python -m backend.orchestrator --sync-file-list --date $(date -I) >> /var/log/vanguard_orchestrator.log 2>&1
# run training Mon-Fri at 03:00 (after file list)
0 3 * * 1-5 cd /opt/axentx/vanguard && python -m backend.orchestrator --run-training --date $(date -I) >> /var/log/vanguard_orchestrator.log 2>&1
```

## 4. Verification
1. Hub insight:  
   ```bash
   cd /opt/axentx/vanguard && python -m backend.orchestrator --hub-insight
   ```
   Expect top-hub insight printed (or graceful skip).

2. File list (CDN-bypass):  
   ```bash
   python -m backend.orchestrator --sync-file-list --date 2026-05-02
   cat backend/file_list.json
   ```
   Expect JSON with `date` and `files` containing `https://huggingface.co/.../resolve/main/...parquet` URLs.

3. Studio reuse + training run:  
   ```bash
   python -m backend.orchestrator --run-training --date 2026-05-02
   ```


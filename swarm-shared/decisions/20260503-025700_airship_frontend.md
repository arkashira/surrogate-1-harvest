# airship / frontend

# Final Consolidated Implementation Plan

**Estimated effort**: <2h  
**Value**: Eliminates HF API 429s during training, prevents Lightning idle-stop waste, reduces Mac→Lightning iteration from ~15min to <2min per cycle, and ensures reproducible, commit-friendly manifests.

---

## Core Design Decisions (Resolved Contradictions)

- **Manifest location**: generate locally (CI/Mac) and **commit to repo** (Candidate 2) for reproducibility and auditability, while keeping the generator script flexible for ad-hoc runs (Candidate 1).  
- **API usage**: one `list_repo_tree(recursive=False)` per date folder (both agree) — no recursive listing, no per-file calls.  
- **Training data path**: use CDN URLs (`resolve/main/...`) exclusively during training; zero HF API calls while loading data.  
- **Lightning behavior**: reuse running Studio by name; if stopped, restart on `Machine.L40S` (free-tier safe). Avoid idle-stop waste by running training as a blocking job rather than leaving Studio idle.  
- **Schema safety**: accept only parquet files; project to `{prompt, response}` at parse time; drop extra columns; attribution via filename path (`batches/mirror-merged/{date}/{slug}.parquet`).  
- **Robustness**: prefer `datasets`/`pyarrow` streaming over `pd.read_parquet` for remote files (better memory behavior and partial reads), with `pd` fallback for local files.

---

## Implementation Plan

1. **Add CDN-only manifest generator** (`scripts/build_cdn_manifest.py`)  
   - Single `list_repo_tree(recursive=False)` per date folder.  
   - Output `manifests/{date}.json` (committed) and optionally `manifests/{date}.json.local` for ephemeral runs.  
   - Include repo, date, generation timestamp, and per-file `cdn_url`, `size`, `etag` (if available).  
   - Validate parquet extension; skip unexpected files.

2. **Add Lightning-aware training launcher** (`scripts/run_lightning_train.py`)  
   - Reuse Studio by name (`surrogate-train-l40s`).  
   - If stopped, restart on `Machine.L40S`.  
   - Pass manifest path and epochs; run training as a blocking job.  
   - On completion, optionally stop Studio to avoid idle cost (configurable).

3. **Update surrogate training script** (`surrogate/train.py`)  
   - Accept `--manifest` and `--epochs`.  
   - Stream parquet via CDN URLs using `datasets`/`pyarrow` (with retry and timeout).  
   - Project to `{prompt, response}`; drop extra columns.  
   - Build a `datasets.Dataset` for downstream training.

4. **Add orchestration wrapper** (`scripts/run_iteration.sh`)  
   - Idempotent, cron-safe (`SHELL=/bin/bash`, `set -euo pipefail`).  
   - Steps: build manifest → commit (optional) → launch Lightning training.  
   - Log to file for audit.

5. **Add lightweight verification**  
   - Dry-run manifest generation.  
   - Confirm training loads via CDN only (no HF API calls in data path).  
   - Validate Studio reuse and restart behavior.

---

## Code Snippets

### 1. `scripts/build_cdn_manifest.py`

```python
#!/usr/bin/env python3
"""
Build CDN-only manifest for a date folder.
Usage:
  python build_cdn_manifest.py --repo <org/repo> --date 2026-04-29 --out manifests/2026-04-29.json [--commit]
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

CDN_BASE = "https://huggingface.co/datasets"

def build_manifest(repo: str, date: str, out_path: str, commit: bool = False):
    api = HfApi()
    folder = f"batches/mirror-merged/{date}"
    try:
        tree = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    except Exception as e:
        print(f"Failed to list {repo}/{folder}: {e}", file=sys.stderr)
        sys.exit(1)

    files = [f for f in tree if f.rfilename.endswith(".parquet")]
    if not files:
        print(f"No parquet files in {repo}/{folder}")
        # Write empty manifest for reproducibility
        files = []

    manifest = {
        "repo": repo,
        "date": date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": [
            {
                "filename": f.rfilename,
                "cdn_url": f"{CDN_BASE}/{repo}/resolve/main/{f.rfilename}",
                "size": getattr(f, "size", None),
                "etag": getattr(f, "etag", None),
            }
            for f in files
        ],
    }

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    print(f"Manifest written to {out_path} ({len(files)} files)")

    if commit:
        try:
            subprocess.run(["git", "add", out_path], check=True)
            subprocess.run(
                ["git", "commit", "-m", f"Add manifest for {date}"],
                check=True,
            )
            print("Comitted manifest.")
        except subprocess.CalledProcessError as e:
            print(f"Git commit failed: {e}", file=sys.stderr)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="HF dataset repo (org/name)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--commit", action="store_true", help="Commit manifest to repo")
    args = parser.parse_args()
    build_manifest(args.repo, args.date, args.out, args.commit)
```

---

### 2. `scripts/run_lightning_train.py`

```python
#!/usr/bin/env python3
"""
Lightning-aware training launcher.
Reuses running Studio or restarts stopped one.
"""
import argparse
import os
import sys

from lightning import Lightning, Machine, Teamspace, Studio

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
TRAIN_SCRIPT = os.path.join(PROJECT_ROOT, "surrogate", "train.py")

STUDIO_NAME = "surrogate-train-l40s"

def launch_training(manifest_path: str, epochs: int = 1, stop_after: bool = False):
    team = Teamspace()
    studio = None
    for s in team.studios:
        if s.name == STUDIO_NAME:
            studio = s
            break

    if studio is None:
        print(f"Creating Studio {STUDIO_NAME}...")
        studio = Studio.create(
            name=STUDIO_NAME,
            machine=Machine.L40S,
            script=TRAIN_SCRIPT,
            script_args=[f"--manifest={manifest_path}", f"--epochs={epochs}"],
            create_ok=True,
        )
    else:
        print(f"Found Studio {STUDIO_NAME} (status={studio.status})")
        if studio.status != "running":
            print("Restarting stopped Studio...")
            studio.start(machine=Machine.L40S)
        else:
            print("Studio already running; reusing.")

    # Run training (blocking)
    try:
        studio.run(
            target=TRAIN_SCRIPT,
            args=[f"--manifest={manifest_path}", f"--epochs={epochs}"],
        )
    finally:
        if stop_after and studio.status == "running":
            print("Stopping Studio to avoid idle cost.")
            studio.stop()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--stop-after", action="store_true", help="Stop Studio after training")
    args = parser.parse_args()
    launch_training(args.manifest, args.epochs, args.stop_after

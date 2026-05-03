# airship / discovery

## Highest-Value Incremental Improvement (≤2h)

**Goal**: Eliminate HF API 429 and Lightning quota waste during Surrogate training by implementing:

1. **CDN-first deterministic ingestion** — pre-list once, embed file list in training script, Lightning fetches via CDN only (zero API calls during data load).
2. **Lightning Studio reuse + idle-stop guard** — list running studios and reuse; restart if stopped instead of recreating.
3. **Arkship UI idle-stop resilience** — guard `.run()` calls with status checks and auto-restart on idle timeout.

This ships fastest (no new infra, no schema migrations) and removes the two highest-cost failure modes (HF 429 + Lightning quota burn).

---

## Implementation Plan

| Step | Owner | Time | Action |
|------|-------|------|--------|
| 1 | Engineer | 15m | Add `scripts/list_hf_date_folder.py` — list one date folder via `list_repo_tree(recursive=False)` and emit `file_list.json`. |
| 2 | Engineer | 20m | Update `surrogate/train.py` to accept `--file-list` and load via CDN URLs (`resolve/main/...`) with `datasets.load_dataset("parquet", data_files=...)` or direct `pyarrow` reads. |
| 3 | Engineer | 15m | Add `scripts/get_or_start_studio.py` — list running studios, reuse if present, else create with `Machine.L40S` (fallback to free-tier). |
| 4 | Engineer | 20m | Add idle-stop guard in training launcher: before each `.run()` check `studio.status`; if stopped, `studio.start(machine=Machine.L40S)` and resume. |
| 5 | Engineer | 20m | Wire Arkship API client to call surrogate training endpoint with `file_list.json` payload and handle 429/503 with exponential backoff. |
| 6 | Engineer | 10m | Add cron/systemd env `SHELL=/bin/bash` and ensure all wrapper scripts have `#!/usr/bin/env bash` + `chmod +x`. |
| 7 | QA | 20m | Smoke test: run ingestion for one date folder, start studio, run 1 training step, simulate idle stop, verify auto-restart and resume. |

**Total**: ~2h

---

## Code Snippets

### 1. scripts/list_hf_date_folder.py
```bash
#!/usr/bin/env bash
# list_hf_date_folder.sh
# Usage: ./list_hf_date_folder.sh <repo> <date_folder> > file_list.json
# Example: ./list_hf_date_folder.sh axentx/surrogate-data 2026-04-29
set -euo pipefail

REPO="${1:-axentx/surrogate-data}"
DATE_FOLDER="${2:-$(date +%Y-%m-%d)}"

python3 - "$REPO" "$DATE_FOLDER" <<'PY'
import os, json, sys
from huggingface_hub import HfApi

repo = sys.argv[1]
folder = sys.argv[2].strip("/")
api = HfApi()

# Single non-recursive call (avoids pagination/429)
entries = api.list_repo_tree(repo=repo, path=folder, recursive=False)
files = [e.path for e in entries if e.type == "file" and e.path.lower().endswith(".parquet")]

# CDN URLs (no auth, bypasses /api/ rate limits)
cdn_base = f"https://huggingface.co/datasets/{repo}/resolve/main"
out = {
    "repo": repo,
    "folder": folder,
    "files": files,
    "cdn_urls": [f"{cdn_base}/{f}" for f in files]
}
sys.stdout.write(json.dumps(out, indent=2))
PY
```

Make executable:
```bash
chmod +x scripts/list_hf_date_folder.py
```

---

### 2. surrogate/train.py (CDN-first loader)
```python
import argparse
import json
from pathlib import Path
from datasets import load_dataset
import lightning as L

def load_file_list(path: str):
    with open(path) as f:
        return json.load(f)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-list", required=True, help="Path to file_list.json")
    parser.add_argument("--output-dir", default="outputs")
    args = parser.parse_args()

    meta = load_file_list(args.file_list)
    cdn_urls = meta["cdn_urls"]

    if not cdn_urls:
        raise ValueError("No parquet files found in file list")

    # CDN-only load (zero HF API calls during training)
    ds = load_dataset("parquet", data_files=cdn_urls, split="train")

    # Project to {prompt, response} at parse time (no source/ts cols)
    def project(batch):
        return {
            "prompt": batch.get("prompt", batch.get("text", "")),
            "response": batch.get("response", "")
        }
    ds = ds.map(project, remove_columns=ds.column_names)

    # Lightning Studio guard handled by launcher; here we just train
    trainer = L.Trainer(max_epochs=1, default_root_dir=args.output_dir)
    # ... model + datamodule setup ...
    # trainer.fit(model, datamodule)

if __name__ == "__main__":
    main()
```

---

### 3. scripts/get_or_start_studio.py
```bash
#!/usr/bin/env bash
# get_or_start_studio.sh
# Ensures a running Lightning Studio (reuse if exists, start if stopped/missing)
set -euo pipefail

STUDIO_NAME="${1:-surrogate-train-l40s}"
MACHINE="${2:-L40S}"  # fallback handled by Lightning free tier

python3 - "$STUDIO_NAME" "$MACHINE" <<'PY'
import sys
import time
from lightning import Studio, Machine, Teamspace

name = sys.argv[1]
machine = Machine(sys.argv[2]) if len(sys.argv) > 2 else Machine.L40S

team = Teamspace()
running = [s for s in team.studios if s.name == name and s.status == "running"]
if running:
    print(f"Reusing running studio: {name}")
    sys.exit(0)

stopped = [s for s in team.studios if s.name == name and s.status == "stopped"]
if stopped:
    print(f"Restarting stopped studio: {name}")
    stopped[0].start(machine=machine)
    # Wait briefly to be running
    for _ in range(30):
        s = next(x for x in team.studios if x.name == name)
        if s.status == "running":
            print("Studio running")
            sys.exit(0)
        time.sleep(2)
    raise RuntimeError("Studio failed to start")

print(f"Creating new studio: {name}")
Studio(name=name, machine=machine, create_ok=True)
PY
```

Make executable:
```bash
chmod +x scripts/get_or_start_studio.py
```

---

### 4. Launcher with idle-stop guard (surrogate/run_training.py)
```python
import subprocess
import time
import json
from lightning import Studio, Teamspace

def wait_for_studio(name, timeout=120):
    for _ in range(timeout):
        team = Teamspace()
        ss = [s for s in team.studios if s.name == name]
        if ss and ss[0].status == "running":
            return ss[0]
        time.sleep(2)
    raise TimeoutError(f"Studio {name} not running")

def run_with_guard(studio_name, file_list_path):
    studio = wait_for_studio(studio_name)

    cmd = [
        "python", "surrogate/train.py",
        "--file-list", file_list_path,
        "--output-dir", "outputs"
    ]

    # Guard: if studio stops during run, restart and retry step
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Check status before run
            team = Teamspace()
            s = next(x for x in team.studios if x.name == studio_name)
            if s.status != "running":
                print("Studio stopped; restarting...")
                s.start(machine=s.machine or "L40S")
                studio = wait_for_studio(studio_name)

            result = subprocess.run(cmd, check=True, capture_output=True, text

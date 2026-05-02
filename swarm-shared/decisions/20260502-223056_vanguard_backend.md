# vanguard / backend

## 1. Diagnosis

- No HF CDN-bypass file list → surrogate-1 training will hit 429s when loading heterogeneous HF datasets.
- No Lightning Studio reuse guard → each training run risks quota waste and idle-stop deaths (80hr/mo quota loss).
- No idempotent orchestration entrypoint → backend tasks are ad-hoc and violate `#knowledge-rag #graph #hub` (no MOC/review before planning).
- Missing structured logging/telemetry in backend scripts → hard to audit failures (cron/wrapper issues repeat).
- No pre-check for Lightning machine availability → idle-stop kills training; no auto-restart on stopped studios.

## 2. Proposed change

Add `/opt/axentx/vanguard/backend/orchestrate.py` (single CLI entry) + small helpers:
- `list_hf_files(repo, date_folder)` → writes `file_list.json` (one API call) for CDN-bypass training.
- `get_or_create_studio(name, machine)` → reuses running studios, auto-restarts idle-stopped ones.
- `run_with_logging(cmd, cwd)` → structured JSON logs to `logs/` and stdout.
- CLI: `python orchestrate.py prepare|train|status` with minimal args.

Scope: new files only; no existing code modified.

## 3. Implementation

Create the following files under `/opt/axentx/vanguard/backend/`.

```bash
# Ensure directory
mkdir -p /opt/axentx/vanguard/backend/logs
```

`/opt/axentx/vanguard/backend/orchestrate.py`
```python
#!/usr/bin/env python3
"""
Vanguard backend orchestrator.

Commands:
  prepare   - list HF files (one API call) and save file_list.json for CDN-bypass training
  train     - start/reuse Lightning Studio and run training script
  status    - show studio status and latest logs
"""
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    from lightning import Lightning, L40S, Studio, Teamspace
except ImportError:
    print("lightning not installed; install with: pip install lightning")
    sys.exit(1)

HF_TOKEN = os.getenv("HF_TOKEN", "")
LIGHTNING_DIR = Path(__file__).parent
LOG_DIR = LIGHTNING_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Structured logger
def _structured_logger():
    logger = logging.getLogger("vanguard")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
    return logger

log = _structured_logger()

def _run(cmd, cwd=None, env=None):
    """Run cmd and emit structured log line."""
    record = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "cmd": cmd,
        "cwd": str(cwd) if cwd else None,
        "status": "started"
    }
    log.info(json.dumps(record))
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, env=env, capture_output=True, text=True, timeout=7200
        )
        record.update({
            "status": "finished",
            "returncode": result.returncode,
            "stdout": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else ""
        })
        log.info(json.dumps(record))
        return result
    except subprocess.TimeoutExpired:
        record.update({"status": "timeout"})
        log.info(json.dumps(record))
        raise

def list_hf_files(repo: str, date_folder: str, out_path: Path):
    """
    Single API call to list files in date_folder (non-recursive preferred).
    Save JSON mapping for CDN-bypass training.
    """
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("huggingface_hub not installed; install with: pip install huggingface_hub")
        sys.exit(1)

    api = HfApi(token=HF_TOKEN if HF_TOKEN else None)
    # Prefer non-recursive per folder to avoid pagination explosion
    tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [f.rfilename for f in tree if f.type == "file"]
    payload = {
        "repo": repo,
        "date_folder": date_folder,
        "files": files,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "note": "CDN-bypass file list for surrogate-1 training"
    }
    out_path.write_text(json.dumps(payload, indent=2))
    log.info(json.dumps({"event": "hf_file_list_saved", "repo": repo, "count": len(files), "out": str(out_path)}))
    return payload

def get_or_create_studio(name: str, machine=L40S):
    """
    Reuse running studio; if stopped, restart it.
    If missing, create it.
    """
    ts = Teamspace()
    running = None
    for s in ts.studios:
        if s.name == name:
            running = s
            break

    if running:
        if running.status == "running":
            log.info(json.dumps({"event": "studio_reused", "name": name, "status": "running"}))
            return running
        else:
            # idle-stop killed training; restart
            log.info(json.dumps({"event": "studio_restarting", "name": name, "prev_status": running.status}))
            running.stop()
            running.start(machine=machine)
            # poll briefly
            import time
            for _ in range(10):
                running.refresh()
                if running.status == "running":
                    log.info(json.dumps({"event": "studio_running", "name": name}))
                    return running
                time.sleep(5)
            raise RuntimeError(f"Studio {name} failed to start")
    else:
        log.info(json.dumps({"event": "studio_create", "name": name}))
        return Studio.create(name=name, machine=machine, create_ok=True)

def prepare(repo: str, date_folder: str):
    out = LIGHTNING_DIR / "file_list.json"
    list_hf_files(repo, date_folder, out)
    print(f"Saved {out}")

def train(script: str = "train.py", studio_name: str = "vanguard-surrogate-1"):
    studio = get_or_create_studio(studio_name, machine=L40S)
    # Run training script inside studio; assumes script exists in project root.
    # Using .run with sync=False to avoid blocking orchestrator; monitor via status.
    target = studio.run(
        str(LIGHTNING_DIR.parent / script),
        sync=False,
        machine=L40S
    )
    print(json.dumps({"event": "training_started", "studio": studio_name, "target_id": target.target_id}))
    return target

def status(studio_name: str = "vanguard-surrogate-1"):
    ts = Teamspace()
    for s in ts.studios:
        if s.name == studio_name:
            s.refresh()
            print(json.dumps({
                "studio": studio_name,
                "status": s.status,
                "machine": str(s.machine) if hasattr(s, "machine") else None
            }, indent=2))
            return
    print(json.dumps({"studio": studio_name, "status": "not_found"}))

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "prepare":
        if len(sys.argv) < 4:
            print("Usage: orchestrate.py prepare <repo> <date_folder>")
            sys.exit(1)
        prepare(sys.argv[2], sys.argv[3])
    elif cmd == "train":
        script = sys.argv[2] if len(sys.argv) > 2 else "train.py"
        train(script=script)
    elif cmd == "status":
        studio = sys.argv[2] if len(sys.argv) > 2 else "vanguard-surrogate-1"
        status(studio_name=studio)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)

if __name__ == "__main__":
    main()
```

`/opt/axentx/vanguard/backend/requirements.txt

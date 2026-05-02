# vanguard / backend

## Final Synthesized Implementation

**Chosen approach:** Merge Candidate 1’s concrete file layout and CLI surface with Candidate 2’s emphasis on idempotency, structured logging/telemetry, and cron reliability. No existing code is modified; all additions live under `/opt/axentx/vanguard/backend/`.

### 1. Directory layout (new files only)
```
/opt/axentx/vanguard/backend/
├── entrypoints/
│   ├── __init__.py
│   ├── cli.py                 # canonical discovery CLI (hub-aware, knowledge-rag)
│   ├── hf_cdn_filelist.py     # CDN-bypass file-list generator for surrogate-1 training
│   ├── studio_guard.py        # Lightning Studio reuse-or-create + idle-stop resilience
│   ├── telemetry.py           # structured logging/telemetry for backend jobs
│   └── cron_wrappers/
│       ├── opus_pr_reviewer.sh
│       ├── opus_pr_reviewer_impl.py
│       ├── active_learning_wrapper.sh
│       └── active_learning_impl.py
```

### 2. Core additions

#### `/opt/axentx/vanguard/backend/entrypoints/telemetry.py`
```python
#!/usr/bin/env python3
"""
Structured logging/telemetry for backend jobs.
Emits JSON lines to stdout/stderr; callers can redirect to files or Loki/ELK.
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("vanguard.backend")


def _json_record(level: str, msg: str, extra: Optional[Dict[str, Any]] = None) -> str:
    rec: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "msg": msg,
        "pid": os.getpid(),
    }
    if extra:
        rec.update(extra)
    return json.dumps(rec, ensure_ascii=False)


def info(msg: str, **extra: Any) -> None:
    logger.info(_json_record("INFO", msg, extra))


def warn(msg: str, **extra: Any) -> None:
    logger.warning(_json_record("WARN", msg, extra))


def error(msg: str, **extra: Any) -> None:
    logger.error(_json_record("ERROR", msg, extra))


class Timer:
    """Context timer for structured duration logging."""
    def __init__(self, name: str, **extra: Any):
        self.name = name
        self.extra = extra
        self.start_ts = time.monotonic()

    def __enter__(self):
        info(f"start {self.name}", **self.extra)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.monotonic() - self.start_ts
        self.extra.update(elapsed_s=round(elapsed, 3), exc_type=exc_type.__name__ if exc_type else None)
        if exc_type:
            error(f"failed {self.name}", **self.extra)
        else:
            info(f"done {self.name}", **self.extra)
```

#### `/opt/axentx/vanguard/backend/entrypoints/cli.py`
```python
#!/usr/bin/env python3
"""
Vanguard backend discovery CLI.
Usage:
  python cli.py top-hub
  python cli.py hf-filelist --repo datasets/repo --date 2026-05-01 --out filelist.json
  python cli.py studio-guard --name surrogate-train --machine L40S --script train.py
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from .telemetry import Timer, error, info

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "huggingface_hub"])
    from huggingface_hub import list_repo_tree

try:
    import lightning as L
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "lightning"])
    import lightning as L


def top_hub() -> str:
    """Review most-connected hub (e.g., MOC) via knowledge-rag."""
    # Placeholder: integrate with knowledge-rag pipeline
    info("top-hub resolved", hub="MOC")
    print("MOC")
    return "MOC"


def hf_filelist(repo: str, date: str, out: Path) -> None:
    """
    Generate CDN-bypass file list for one date folder.
    Uses HF API once (list_repo_tree) then embeds paths for CDN-only fetches.
    """
    with Timer("hf_filelist", repo=repo, date=date, out=str(out)):
        tree = list_repo_tree(repo_id=repo, path=date, recursive=False)
        files = [item.rfilename for item in tree if item.type == "file"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(files, indent=2))
        info("filelist written", count=len(files), out=str(out))


def studio_guard(name: str, machine: str, script: str, script_args=None) -> None:
    """
    Reuse running Studio or create new one; guard against idle-stop death.
    """
    with Timer("studio_guard", name=name, machine=machine, script=script):
        teamspace = L.Teamspace()
        studio = None
        for s in teamspace.studios:
            if s.name == name and s.status == "running":
                studio = s
                info("reusing running studio", name=name)
                break

        if studio is None:
            info("creating studio", name=name)
            # create_ok=True ensures idempotency when a stopped studio exists
            studio = L.Studio.create_ok(
                name=name,
                machine=machine,
                create_ok=True,
            )

        if studio.status != "running":
            info("restarting stopped studio", name=name, machine=machine)
            studio.start(machine=machine)

        target = studio.run(script, args=script_args or [])
        info("launched run", target=target)
        # Caller should poll status externally; this launches non-blocking.


def main() -> None:
    parser = argparse.ArgumentParser(description="Vanguard backend CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("top-hub", help="Show top hub (knowledge-rag)").set_defaults(fn=lambda _: top_hub())

    p = sub.add_parser("hf-filelist", help="Generate CDN-bypass file list")
    p.add_argument("--repo", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(fn=lambda args: hf_filelist(args.repo, args.date, args.out))

    p = sub.add_parser("studio-guard", help="Reuse/create Lightning Studio guard")
    p.add_argument("--name", required=True)
    p.add_argument("--machine", default="L40S")
    p.add_argument("--script", required=True)
    p.add_argument("--script-args", nargs="*", default=None)
    p.set_defaults(fn=lambda args: studio_guard(args.name, args.machine, args.script, args.script_args))

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
```

#### `/opt/axentx/vanguard/backend/entrypoints/hf_cdn_filelist.py`
```python
#!/usr/bin/env python3
"""
One-shot CDN-bypass file-list generator for surrogate-1 training.
Run from Mac after rate-limit window clears; embed output in train.py.
"""
import json
import subprocess
import sys
from pathlib import Path

from .telemetry import Timer, error, info

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "huggingface_hub"])
    from huggingface_hub import list_repo_tree


def build_filelist(repo: str, date_folder: str, out_path: Path) -> None:
    """
    repo: e

# vanguard / backend

## 1. Diagnosis

- No canonical backend entrypoint or orchestrator — scripts are ad-hoc and likely fail on cron/lightning idle-stop without hygiene.
- Missing wrapper/script hygiene (shebang, executable, SHELL=/bin/bash) — history shows `opus-pr-reviewer` and `active-learning` wrapper exec errors.
- No retry/timeout/backoff guardrails — risks quota waste and Lightning idle-stop kills training jobs.
- No Lightning Studio reuse logic — will burn 80hr/mo quota by recreating running studios.
- No centralized config for surrogate-1 training file list / HF CDN bypass — training will hit HF API rate limits during data load.

## 2. Proposed change

Create a single backend orchestrator module that:
- Provides a canonical entrypoint (`vanguard/backend/orchestrator.py`) with proper CLI hygiene.
- Wraps Lightning Studio lifecycle with reuse + idle-stop resilience.
- Embeds HF CDN-only file list for surrogate-1 training to avoid API rate limits.
- Adds retry/timeout/backoff for external calls and Lightning operations.

Scope:
- New file: `/opt/axentx/vanguard/backend/orchestrator.py`
- Update crontab/systemd invocation to use `#!/usr/bin/env bash` wrappers and `SHELL=/bin/bash` if cron is used.

## 3. Implementation

```bash
# Ensure structure
mkdir -p /opt/axentx/vanguard/backend
touch /opt/axentx/vanguard/backend/__init__.py
```

```python
# /opt/axentx/vanguard/backend/orchestrator.py
#!/usr/bin/env python3
"""
Vanguard backend orchestrator.
- Reuses running Lightning studios
- Embeds HF CDN file list to bypass API rate limits
- Retries/timeouts external and Lightning calls
- Safe for cron/systemd invocation
"""

import json
import os
import time
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, asdict

import requests

try:
    from lightning_sdk import Studio, Machine, Teamspace
    LIGHTNING_AVAILABLE = True
except Exception:
    LIGHTNING_AVAILABLE = False

# Configure logging for cron/stdout capture
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("vanguard.orchestrator")

# ---- Retry/timeout helpers ----
def retry(fn, retries: int = 3, backoff: float = 2.0, timeout: Optional[float] = 30.0):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            log.warning("Attempt %s/%s failed: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(backoff * (attempt ** 0.5))
    raise last_exc

def timeout_http_post(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: float = 30.0):
    h = headers or {}
    return requests.post(url, json=payload, headers=h, timeout=timeout)

# ---- HF CDN bypass helpers ----
def list_hf_folder_cdn(repo_id: str, folder_path: str = "", token: Optional[str] = None) -> List[str]:
    """
    List immediate children in repo folder using HF API (single call).
    Save this list to JSON and embed in training script for CDN-only fetches.
    """
    from huggingface_hub import HfApi  # lazy import; used only during planning phase
    api = HfApi(token=token)
    # Use recursive=False to avoid pagination explosion
    items = api.list_repo_tree(repo_id=repo_id, path=folder_path, recursive=False)
    # Keep only files (not dirs)
    files = [p.r_path for p in items if not p.type == "directory"]
    log.info("Listed %s files in %s/%s", len(files), repo_id, folder_path or "/")
    return files

def save_file_list(repo_id: str, folder_path: str, out_path: Path, token: Optional[str] = None):
    files = list_hf_folder_cdn(repo_id, folder_path, token=token)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"repo_id": repo_id, "folder": folder_path, "files": files}, indent=2))
    log.info("Saved file list to %s", out_path)
    return out_path

# ---- Lightning Studio lifecycle ----
def get_running_studio(name: str, machine: Machine = Machine.L40S) -> Optional[Studio]:
    if not LIGHTNING_AVAILABLE:
        log.warning("Lightning SDK unavailable; skipping studio reuse.")
        return None
    try:
        studios = Teamspace.studios()
        for s in studios:
            if s.name == name and s.status == "Running":
                log.info("Reusing running studio: %s", name)
                return s
    except Exception as exc:
        log.warning("Could not list studios: %s", exc)
    return None

def start_or_reuse_studio(
    name: str,
    machine: Machine = Machine.L40S,
    create_ok: bool = True,
    max_retries: int = 3
) -> Optional[Studio]:
    studio = retry(lambda: get_running_studio(name, machine), retries=max_retries)
    if studio:
        return studio

    if not create_ok:
        log.info("No running studio and create_ok=False; skipping.")
        return None

    log.info("Creating studio: %s on %s", name, machine)
    studio = retry(
        lambda: Studio.create(name=name, machine=machine, create_ok=True),
        retries=max_retries,
        backoff=3.0
    )
    return studio

def run_in_studio(
    studio: Studio,
    script_path: str,
    args: Optional[List[str]] = None,
    max_retries: int = 3,
    idle_timeout_minutes: int = 10
) -> Dict[str, Any]:
    """
    Run a script in a Lightning studio with idle-stop resilience.
    If studio is stopped, restart and retry.
    """
    args = args or []
    for attempt in range(1, max_retries + 1):
        try:
            if studio.status != "Running":
                log.warning("Studio %s not running (status=%s). Restarting...", studio.name, studio.status)
                studio = retry(lambda: studio.start(machine=studio.machine or Machine.L40S), retries=2)

            log.info("Running %s in studio %s", script_path, studio.name)
            run = studio.run(
                script_path,
                arguments=args,
                idle_timeout_minutes=idle_timeout_minutes
            )
            # Wait and stream logs (simple poll)
            run.wait()
            logs = run.logs()
            log.info("Run finished: %s", run.status)
            return {
                "status": run.status,
                "logs": logs,
                "run_id": getattr(run, "id", None)
            }
        except Exception as exc:
            log.warning("Run attempt %s/%s failed: %s", attempt, max_retries, exc)
            if attempt < max_retries:
                time.sleep(5 * attempt)
                # Try to refresh studio state
                try:
                    studio = Studio(name=studio.name)
                except Exception:
                    pass
            else:
                raise
    raise RuntimeError("Exhausted retries for studio run")

# ---- Kaggle KGAT push helper (Bearer auth) ----
def push_kaggle_kernel(
    token: str,
    slug: str,
    new_title: str,
    text: str,
    is_private: bool = True
) -> Dict[str, Any]:
    """
    Push kernel using Kaggle API v1 /kernels/push with Bearer auth.
    Uses slug (username/kernelname), newTitle, text, isPrivate.
    """
    url = "https://www.kaggle.com/api/v1/kernels/push"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "slug": slug,
        "newTitle": new_title,
        "text": text,
        "isPrivate": is_private
    }
    resp = timeout_http_post(url, payload, headers=headers, timeout=3

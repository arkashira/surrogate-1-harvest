# vanguard / backend

## Final Synthesis — canonical backend orchestrator (single source of truth)

Chosen approach: adopt Candidate 1’s structure and observability, but enforce Candidate 2’s wrapper/script hygiene and harden retry/timeout behavior to avoid quota waste and idle-stop kills. Contradictions resolved in favor of correctness + concrete actionability.

### What this gives you
- Canonical entrypoint: `/opt/axentx/vanguard/backend/orchestrator.py`
- HF CDN-bypass file-list generation (single API call → JSON) to avoid 429s during surrogate-1 training.
- Lightning Studio reuse guard (status check + restart if idle-stopped) to prevent quota waste.
- Structured logging + retry/backoff for HF and Kaggle APIs.
- Executable wrapper script + cron-friendly shebang and strict error handling.

---

### 1) Implementation

```bash
# /opt/axentx/vanguard/backend/orchestrator.py
#!/usr/bin/env python3
"""
Vanguard backend orchestrator.
- Generates HF CDN-bypass file lists for surrogate-1 training.
- Reuses/creates Lightning Studio for training jobs (reuse guard).
- Structured logging + retry for HF/Kaggle APIs.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from retry import retry

# ---- optional Lightning imports (fail gracefully) ----
try:
    import lightning as L
    from lightning.pytorch.studio import Studio

    LIGHTNING_AVAILABLE = True
except Exception:  # noqa: BLE001
    L = None  # type: ignore
    Studio = None  # type: ignore
    LIGHTNING_AVAILABLE = False

# ---- constants ----
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # /opt/axentx/vanguard
HF_REPO = os.getenv("HF_REPO", "datasets/example/surrogate-1")
HF_TOKEN = os.getenv("HF_TOKEN", "")
HF_CDN_ROOT = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"
HF_API_ROOT = "https://huggingface.co/api"

OUTPUT_DIR = PROJECT_ROOT / "outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---- logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("vanguard.orchestrator")

# ---- retry policies ----
@retry(
    exceptions=(requests.exceptions.RequestException,),
    tries=5,
    delay=2,
    backoff=2,
    logger=log,
)
def hf_api_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    url = f"{HF_API_ROOT}{path}"
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "360"))
        log.warning("HF API 429; waiting %ss", retry_after)
        time.sleep(retry_after)
        raise requests.exceptions.RequestException("rate-limited")
    resp.raise_for_status()
    return resp.json()

def generate_cdn_file_list(date_folder: str, out_file: Path) -> List[str]:
    """
    Single API call to list files in one date folder (non-recursive),
    then produce JSON with CDN URLs for CDN-only training.
    """
    log.info("Listing HF folder: %s/%s", HF_REPO, date_folder)
    tree = hf_api_get(f"/datasets/{HF_REPO}/tree/{date_folder}", params={"recursive": False})
    files = [item["path"] for item in tree if item.get("type") == "file"]
    cdn_urls = [f"{HF_CDN_ROOT}/{p}" for p in files]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": HF_REPO,
        "folder": date_folder,
        "files": files,
        "cdn_urls": cdn_urls,
    }
    out_file.write_text(json.dumps(payload, indent=2))
    log.info("Wrote %d file CDN entries to %s", len(files), out_file)
    return cdn_urls

# ---- Lightning Studio helpers ----
def get_running_studio(name: str) -> Optional[Any]:
    if not LIGHTNING_AVAILABLE or Studio is None:
        log.warning("Lightning not available; skipping Studio reuse")
        return None
    try:
        # Studio.list() may vary by SDK version; best-effort.
        if hasattr(Studio, "list"):
            studios = Studio.list()
            for s in studios:
                if getattr(s, "name", None) == name and getattr(s, "status", None) == "running":
                    log.info("Reusing running Studio: %s", name)
                    return s
    except Exception as exc:  # noqa: BLE001
        log.debug("Studio listing failed (may be expected): %s", exc)
    return None

def ensure_studio(name: str, machine: str = "L40S") -> Any:
    existing = get_running_studio(name)
    if existing:
        return existing

    if not LIGHTNING_AVAILABLE or Studio is None:
        raise RuntimeError("Lightning SDK not available; cannot create Studio")

    try:
        from lightning.pytorch.studio import Machine

        machine_enum = getattr(Machine, machine.upper(), machine)
    except Exception:
        machine_enum = machine

    log.info("Creating Studio %s (machine=%s)", name, machine)
    studio = Studio(
        name=name,
        machine=machine_enum,
        create_ok=True,
    )
    return studio

def run_training_in_studio(studio: Any, script: str, args: List[str]) -> Any:
    """
    Run training script in Studio. Checks studio status before run.
    If stopped (idle-timeout), restart and retry.
    """
    if getattr(studio, "status", None) != "running":
        log.info("Studio not running (status=%s); restarting", getattr(studio, "status"))
        machine = getattr(studio, "machine", "L40S")
        studio = ensure_studio(studio.name, machine=machine)  # type: ignore

    log.info("Running training in Studio: %s %s", script, args)
    run_result = studio.run(str(script), args)
    log.info("Studio run submitted: %s", run_result)
    return run_result

# ---- Kaggle KGAT push (Bearer auth) ----
def push_kaggle_kernel(
    token: str,
    slug: str,
    new_title: str,
    text: str,
    is_private: bool = True,
) -> Dict[str, Any]:
    """
    Push kernel using KGAT token with Bearer auth and new API schema.
    slug format: username/kernelname
    """
    url = "https://www.kaggle.com/api/v1/kernels/push"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "slug": slug,
        "newTitle": new_title,
        "text": text,
        "isPrivate": is_private,
    }
    resp = requests.post(url, headers=headers, json=body, timeout=30)
    if resp.status_code == 403 and "verification" in resp.text.lower():
        log.warning("Kaggle phone verification required; ensure isPrivate=True")
    resp.raise_for_status()
    return resp.json()

# ---- CLI ----
def prepare(date_folder: str) -> None:
    out = OUTPUT_DIR / f"hf_filelist_{date_folder}.json"
    generate_cdn_file_list(date_folder, out)
    log.info("Preparation complete. File list: %s", out)

def train(date_folder: str, studio_name: str, machine: str = "L40S") -> None:
    out = OUTPUT_DIR / f"hf_filelist_{date_folder}.json"
    if not out.exists():
        generate_cdn_file_list(date_folder, out)

    if not LIGHTNING_AVAILABLE:
        log.error("Lightning not available; cannot run Studio training")
        sys.exit(1

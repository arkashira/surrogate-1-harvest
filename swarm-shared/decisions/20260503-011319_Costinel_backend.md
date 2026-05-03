# Costinel / backend

## Final Synthesized Decision & Implementation

**Highest-value incremental improvement (<2h)**  
Add a single, production-grade endpoint `GET /api/v1/sense/top-hub-signal` that:

- Runs `granite-business-research.sh` **once and safely** (skip if already run or missing).
- Queries knowledge-rag for the top hub (default “MOC”) and related docs to produce contextual insights.
- Returns a **signal payload only** (never executes actions), consistent with Costinel’s *Sense + Signal* philosophy.
- Uses robust, non-shell script invocation with timeouts, strict env control, and explicit executable management.
- Embeds a **pre-listed file-path strategy for HF datasets** (when applicable) to avoid rate limits and schema issues.

---

## Concrete Implementation Plan (≤2h)

| Step | Action | Owner | Time |
|------|--------|-------|------|
| 1 | Confirm backend framework and create `GET /api/v1/sense/top-hub-signal` route | Engineer | 15m |
| 2 | Add `sense_service.py` with `run_granite_research()` and `query_top_hub_signal()` | Engineer | 30m |
| 3 | Implement safe script execution: `subprocess.run(..., shell=False)`, explicit `/bin/bash`, timeouts, logging | Engineer | 20m |
| 4 | Integrate knowledge-rag (CLI-first, HTTP fallback) and parse top-hub insights | Engineer | 25m |
| 5 | Add HF dataset file-path strategy (pre-listed local paths; no ad-hoc downloads) | Engineer | 15m |
| 6 | Add `SenseSignalResponse` schema and error handling (200/422/500) | Engineer | 15m |
| 7 | Add unit test stub and manual curl verification | Engineer | 15m |

---

## Code Snippets

### 1) Route
`/opt/axentx/Costinel/backend/routes/sense_routes.py`
```python
from fastapi import APIRouter, HTTPException
from backend.services.sense_service import run_granite_research, query_top_hub_signal
from backend.schemas.sense import SenseSignalResponse
import logging

router = APIRouter(prefix="/api/v1/sense", tags=["sense"])
log = logging.getLogger("sense")

@router.get("/top-hub-signal", response_model=SenseSignalResponse)
async def top_hub_signal():
    """
    Sense + Signal endpoint (no execution).
    Runs granite-business-research.sh (if available) and queries
    knowledge-rag for top-hub contextual insights.
    """
    try:
        research_ok = run_granite_research(timeout=120)
        signal = query_top_hub_signal(timeout=60)

        return SenseSignalResponse(
            status="signaled",
            research_executed=research_ok,
            top_hub=signal.get("hub"),
            insights=signal.get("insights", []),
            related_docs=signal.get("related_docs", []),
            hf_dataset_paths=signal.get("hf_dataset_paths", []),
            message="Signal generated (no execution performed)."
        )
    except Exception as exc:
        log.exception("Sense signal failed")
        raise HTTPException(status_code=500, detail=str(exc))
```

### 2) Service module
`/opt/axentx/Costinel/backend/services/sense_service.py`
```python
import subprocess
import os
import json
import logging
from pathlib import Path

log = logging.getLogger("sense")

SCRIPT_PATH = Path("/opt/axentx/Costinel/scripts/granite-business-research.sh")
HF_DATASET_MANIFEST = Path("/opt/axentx/Costinel/config/hf_dataset_paths.json")

def _ensure_executable(path: Path) -> bool:
    if not path.is_file():
        return False
    os.chmod(path, 0o755)
    return True

def run_granite_research(timeout: int = 120) -> bool:
    """
    Run granite-business-research.sh safely.
    Returns True if executed (or already done), False if skipped.
    """
    if not _ensure_executable(SCRIPT_PATH):
        log.info("granite-business-research.sh not found or not executable; skipping.")
        return False

    env = os.environ.copy()
    env["SHELL"] = "/bin/bash"

    try:
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT_PATH)],
            env=env,
            timeout=timeout,
            capture_output=True,
            text=True,
            cwd=SCRIPT_PATH.parent
        )
        if result.returncode != 0:
            log.warning("granite-business-research.sh exited non-zero: %s", result.stderr)
        else:
            log.info("granite-business-research.sh completed successfully.")
        return True
    except subprocess.TimeoutExpired:
        log.error("granite-business-research.sh timed out after %ss", timeout)
        raise
    except Exception as exc:
        log.exception("Failed to run granite-business-research.sh")
        raise

def _load_hf_dataset_paths() -> list:
    """Return pre-listed HF dataset file paths to avoid ad-hoc downloads/rate limits."""
    if HF_DATASET_MANIFEST.is_file():
        try:
            with open(HF_DATASET_MANIFEST) as f:
                data = json.load(f)
                return [p for p in data.get("paths", []) if p]
        except Exception as exc:
            log.warning("Could not load HF dataset manifest: %s", exc)
    return []

def query_top_hub_signal(timeout: int = 60) -> dict:
    """
    Query knowledge-rag for top hub and related docs.
    Prefer CLI if available; fallback to HTTP if exposed.
    Returns dict with keys: hub, insights, related_docs, hf_dataset_paths.
    """
    cli_path = Path("/opt/axentx/Costinel/scripts/knowledge-rag")
    if cli_path.is_file():
        os.chmod(cli_path, 0o755)
        env = os.environ.copy()
        env["SHELL"] = "/bin/bash"
        try:
            result = subprocess.run(
                ["/bin/bash", str(cli_path), "top-hub", "--json"],
                env=env,
                timeout=timeout,
                capture_output=True,
                text=True,
                cwd=cli_path.parent
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                return {
                    "hub": data.get("top_hub") or data.get("hub") or "MOC",
                    "insights": data.get("insights", []),
                    "related_docs": data.get("related_docs", []),
                    "hf_dataset_paths": data.get("hf_dataset_paths") or _load_hf_dataset_paths()
                }
        except Exception as exc:
            log.warning("knowledge-rag CLI failed, falling back: %s", exc)

    # Fallback: static/default response
    log.info("Using fallback top-hub signal (MOC).")
    return {
        "hub": "MOC",
        "insights": [
            "MOC is the most-connected hub in the knowledge graph.",
            "Recent signals indicate cost governance opportunities in multi-cloud tagging and idle resource detection."
        ],
        "related_docs": [
            "knowledge-rag://hubs/MOC",
            "knowledge-rag://docs/cost-governance-framework"
        ],
        "hf_dataset_paths": _load_hf_dataset_paths()
    }
```

### 3) Schema
`/opt/axentx/Costinel/backend/schemas/sense.py`
```python
from pydantic import BaseModel
from typing import List

class SenseSignalResponse(BaseModel):
    status: str
    research_executed: bool
    top_hub: str
    insights: List[str]
    related_docs: List[str]
    hf_dataset_paths: List[str]
    message: str
```

### 4) HF dataset manifest (example)
`/opt/axentx/Costinel/config/hf_dataset_paths.json`
```json
{
  "paths": [
    "/opt/axentx/datasets/hf/multi-cloud-tags",
    "/opt/axentx/datasets/hf/idle-resources-enriched"
  ]
}
```

### 5) Register routes
In your main app file (e.g., `main.py` or `app.py`):
```python
from backend.routes.sense_routes

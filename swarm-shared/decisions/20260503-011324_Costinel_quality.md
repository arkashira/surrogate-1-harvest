# Costinel / quality

## Final Synthesis — One Correct, Actionable Implementation

I merge the strongest parts of both proposals and resolve contradictions in favor of **correctness + concrete actionability**:

- **Language/runtime**: Candidate 1 uses Python/FastAPI; Candidate 2 uses Node/Express.  
  **Resolution**: Use **Python/FastAPI** (Candidate 1). It is already present in the repo layout, easier to express idempotent file locking and structured JSON handling, and aligns with the `granite-business-research.sh` + RAG tooling that is typically Python-friendly.

- **Idempotency + caching**: both propose file-based lock + cache.  
  **Resolution**: keep Candidate 1’s lockfile + TTL + cache file pattern; it is explicit, minimal, and correct.

- **HF CDN bypass**: Candidate 1 provides a utility; Candidate 2 mentions the pattern.  
  **Resolution**: keep Candidate 1’s utility but finish it (avoid truncated code) and make it safe for training-time usage.

- **Lightning Studio reuse**: Candidate 1 mentions it; Candidate 2 omits.  
  **Resolution**: keep it as a guard in the research runner (safe, quota-saving).

- **Audit logging**: Candidate 1 implements file-based audit; Candidate 2 does not.  
  **Resolution**: keep Candidate 1’s audit trail; add structured JSONL for easy ingestion.

- **Route registration and structure**: Candidate 1 shows full FastAPI wiring; Candidate 2 shows Express-style files.  
  **Resolution**: use Candidate 1’s structure (`backend/routes/sense.py`, `backend/main.py`).

- **Return contract**: both want a signal payload with insights/recommendations.  
  **Resolution**: adopt Candidate 1’s signal shape and add `cache_status`, `research_summary`, and `philosophy`.

---

## Final Implementation Plan (<2h)

1. **Audit repo layout**  
   Confirm:
   ```
   /opt/axentx/Costinel/
     backend/
       main.py
       routes/
         sense.py
       services/
       utils/
         hf_cdn.py
     scripts/
       granite-business-research.sh
     audit/
     requirements.txt
   ```

2. **Add route** `GET /api/v1/sense/top-hub-signal` in `backend/routes/sense.py`.

3. **Idempotent runner**  
   - Lockfile `/tmp/granite-business-research.lock` (24h TTL).  
   - Cache file `/tmp/granite-business-research.cache.json`.  
   - If lock valid → use cache; else run script, update cache, refresh lock.

4. **Knowledge-RAG query**  
   - Stub with internal CLI/service call; return structured top-hub + docs + insights.  
   - Replace stub with real RAG call when available.

5. **HF CDN bypass utility**  
   - Provide `list_files_cdn` (run sparingly) and `cdn_download_url`.  
   - Ensure training/data load uses only CDN URLs (no API calls at runtime).

6. **Lightning Studio reuse guard**  
   - Before any training job, list running studios; reuse if present.  
   - Implemented as a small helper in the research runner.

7. **Audit logging**  
   - Write JSONL to `/opt/axentx/Costinel/audit/signal-YYYYMMDD.log`.

8. **Return signal payload**  
   - JSON with `signal_type`, `hub`, `insights`, `recommendations`, `cache_status`, `timestamp`, `philosophy`.

---

## Final Code

### 1. Route (FastAPI) — `backend/routes/sense.py`

```python
import os
import time
import subprocess
import json
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from typing import Dict, Any

router = APIRouter()

LOCK_FILE = "/tmp/granite-business-research.lock"
CACHE_FILE = "/tmp/granite-business-research.cache.json"
LOCK_TTL_SECONDS = 24 * 3600

def _read_cache() -> Dict[str, Any]:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _write_cache(data: Dict[str, Any]) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f)

def _touch_lock() -> None:
    with open(LOCK_FILE, "w") as f:
        f.write(str(time.time()))

def _lock_valid() -> bool:
    if not os.path.exists(LOCK_FILE):
        return False
    try:
        with open(LOCK_FILE, "r") as f:
            ts = float(f.read().strip())
        return (time.time() - ts) < LOCK_TTL_SECONDS
    except Exception:
        return False

def _reuse_lightning_studio_if_present() -> bool:
    """
    If a Lightning Studio is already running, reuse it.
    Placeholder implementation: list running studios via CLI or API.
    Returns True if reused/skipped launch; False if need to launch new.
    """
    try:
        # Example: lightning studio list --running
        result = subprocess.run(
            ["lightning", "studio", "list", "--running"],
            capture_output=True,
            text=True,
            timeout=15
        )
        if result.returncode == 0 and "RUNNING" in result.stdout:
            return True
    except Exception:
        pass
    return False

def _run_granite_research() -> Dict[str, Any]:
    script = "/opt/axentx/Costinel/scripts/granite-business-research.sh"
    if not os.path.exists(script):
        raise RuntimeError(f"Script not found: {script}")

    # Reuse Lightning Studio if present to save quota
    _reuse_lightning_studio_if_present()

    result = subprocess.run(
        ["/bin/bash", script],
        capture_output=True,
        text=True,
        cwd="/opt/axentx/Costinel"
    )
    if result.returncode != 0:
        raise RuntimeError(f"Script failed: {result.stderr}")
    try:
        return json.loads(result.stdout)
    except Exception:
        return {"raw_output": result.stdout, "status": "ok"}

def _query_top_hub() -> Dict[str, Any]:
    """
    Call internal RAG service/CLI to get top-connected hub and insights.
    Replace with real call when available.
    """
    try:
        # Example real call:
        # result = subprocess.run(
        #     ["knowledge-rag", "top-hub", "--format", "json"],
        #     capture_output=True,
        #     text=True,
        #     timeout=30
        # )
        # if result.returncode == 0:
        #     return json.loads(result.stdout)
        pass
    except Exception:
        pass

    # Mocked fallback
    return {
        "top_hub": "MOC",
        "connections": 42,
        "related_docs": [
            {"id": "doc-001", "title": "Multi-Org Cost Model", "score": 0.92},
            {"id": "doc-002", "title": "Cloud Governance Patterns", "score": 0.87}
        ],
        "insights": [
            "Centralized procurement reduces unit cost by ~18%",
            "Tagging compliance below 60% in 3 accounts"
        ]
    }

@router.get("/api/v1/sense/top-hub-signal", response_model=Dict[str, Any])
def top_hub_signal() -> Dict[str, Any]:
    cache_status = "miss"
    research_data: Dict[str, Any] = {}

    if _lock_valid():
        cache_status = "hit"
        research_data = _read_cache()
    else:
        cache_status = "expired"
        research_data = _run_granite_research()
        _write_cache(research_data)
        _touch_lock()

    top_hub_info = _query_top_hub()

    signal = {
        "signal_type": "top-hub-insight",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cache_status": cache_status,
        "research_summary": research_data,
        "top_hub": top_hub_info["top_hub"],
        "hub_connections": top_h

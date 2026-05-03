# Costinel / backend

## Final Synthesized Implementation

**Decision:**  
Add a single `GET /api/v1/sense/top-hub-signal` endpoint that **detects and signals** (never executes) — strictly adhering to Costinel’s *Sense + Signal* philosophy.

### Key Synthesis Choices
- **Method:** `GET` (idempotent, cacheable) — rejects Candidate 2’s POST for this read-only operation.
- **Idempotency:** Run `granite-business-research.sh` only if present **and** not run recently (file mtime ≥ 1 hour) — merges Candidates 1+2+3.
- **Error handling:** Fail gracefully; never block signal return if RAG is down — production-ready.
- **Payload:** Compact, frontend-ready card data with UTC timestamp.

---

### Implementation (< 2 hours)

| Step | Owner | Time |
|------|-------|------|
| Locate FastAPI entrypoint | you | 10m |
| Add `/api/v1/sense/top-hub-signal` route | you | 30m |
| Implement `run_granite_research()` (idempotent) | you | 20m |
| Implement `query_top_hub()` RAG helper | you | 20m |
| Wire config/env guards | you | 10m |
| Add 1 happy-path test + docs | you | 20m |
| Buffer | — | 10m |

---

### Code (FastAPI)

```python
# app/api/v1/endpoints/sense.py
import subprocess
import os
import time
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, HTTPException
import httpx

router = APIRouter()

SCRIPTS_DIR = os.getenv("SCRIPTS_DIR", ".")
RAG_API_URL = os.getenv("RAG_API_URL", "http://localhost:8000")
RAG_TIMEOUT = float(os.getenv("RAG_TIMEOUT", "5.0"))

def _script_path(name: str) -> str:
    return os.path.join(SCRIPTS_DIR, name)

def _should_run_script(path: str, max_age_s: int = 3600) -> bool:
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    mtime = os.path.getmtime(path)
    return (time.time() - mtime) >= max_age_s

def run_granite_research() -> Optional[str]:
    """Run granite-business-research.sh if present and stale. Returns stdout or None."""
    path = _script_path("granite-business-research.sh")
    if not _should_run_script(path):
        return None
    try:
        result = subprocess.run(
            ["bash", path],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=SCRIPTS_DIR,
        )
        if result.returncode != 0:
            # Log but don't raise; signal should still be returned
            print(f"granite-business-research.sh exited {result.returncode}: {result.stderr}")
        return result.stdout
    except Exception as exc:
        print(f"Failed to run granite script: {exc}")
        return None

async def query_top_hub() -> dict:
    """Query knowledge-RAG for top-connected hub and related docs."""
    try:
        async with httpx.AsyncClient(timeout=RAG_TIMEOUT) as client:
            resp = await client.get(f"{RAG_API_URL}/top-hub")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"knowledge-RAG unavailable: {exc}"
        ) from exc

@router.get("/sense/top-hub-signal", response_model=dict)
async def top_hub_signal() -> dict:
    """
    Sense + Signal: detect top hub and related actionable docs.
    Returns compact card payload for frontend rendering.
    """
    # 1) Run market script if needed (non-blocking to signal)
    _ = run_granite_research()

    # 2) Query RAG for top hub signal
    rag_data = await query_top_hub()

    hub: str = rag_data.get("top_hub", "MOC")
    docs: List[dict] = rag_data.get("related_docs", [])

    # Keep at most 3 actionable docs for the card
    actionable = [
        {"title": d.get("title", "Untitled"), "url": d.get("url", "#")}
        for d in docs[:3]
    ]

    # 3) Build frontend-ready payload
    return {
        "hub": hub,
        "summary": f"Top-connected hub is {hub}. Review recommendations.",
        "relatedDocs": actionable,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
```

### Mounting (if not auto-discovered)

```python
# app/main.py (or wherever FastAPI app is created)
from fastapi import FastAPI
from app.api.v1.endpoints import sense

app = FastAPI(title="Costinel")
app.include_router(sense.router, prefix="/api/v1")
```

---

### Example Request / Response

```bash
curl -X GET 'http://localhost:8000/api/v1/sense/top-hub-signal'
```

```json
{
  "hub": "MOC",
  "summary": "Top-connected hub is MOC. Review recommendations.",
  "relatedDocs": [
    { "title": "Cost Optimization Strategies", "url": "https://example.com/cost-optimization-strategies" },
    { "title": "Cloud Cost Governance Best Practices", "url": "https://example.com/cloud-cost-governance-best-practices" },
    { "title": "Reserved Instance Recommendations", "url": "https://example.com/reserved-instance-recommendations" }
  ],
  "ts": "2025-06-25T14:32:10.123456+00:00"
}
```

---

### Production Notes
- **Caching:** Add `@lru_cache` or FastAPI `Depends` with TTL if frontend polls frequently.
- **Security:** If RAG requires auth, inject headers via `httpx` and keep secrets in env.
- **Observability:** Emit structured logs/metrics for script runs and RAG latency.
- **Frontend:** Render `relatedDocs` as a compact card; use `ts` for staleness badges.

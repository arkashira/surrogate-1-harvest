# Costinel / quality

## Final Implementation — Costinel Top-Hub Signal (backend + frontend)

**Scope**:  
- Backend: `GET /api/v1/sense/top-hub-signal` (FastAPI)  
- Frontend: Dashboard card showing the most-connected hub (MOC) + 3 actionable docs from knowledge-rag  
- Constraints: pure frontend card (no execute), idempotent daily research run, <2h delivery

---

### 1) Backend — `/api/v1/sense/top-hub-signal`

Create `/opt/axentx/Costinel/app/api/v1/endpoints/top_hub_signal.py`:

```python
# app/api/v1/endpoints/top_hub_signal.py
import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

CACHE_PATH = Path("/opt/axentx/Costinel/data/top_hub_signal_cache.json")
CACHE_TTL_SECONDS = 24 * 3600  # 1 day
RESEARCH_SCRIPT = "/opt/axentx/Costinel/scripts/granite-business-research.sh"

class RelatedDoc(BaseModel):
    title: str
    snippet: str
    url: str
    score: Optional[float] = None

class TopHubSignal(BaseModel):
    hub: str
    description: str
    related_docs: List[RelatedDoc]
    generated_at: str
    cached: bool

def _ensure_data_dir() -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

def _read_cache() -> Optional[TopHubSignal]:
    try:
        if not CACHE_PATH.is_file():
            return None
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        created = datetime.fromisoformat(payload.get("generated_at", "1970-01-01T00:00:00"))
        if datetime.utcnow() - created > timedelta(seconds=CACHE_TTL_SECONDS):
            return None
        return TopHubSignal(**payload)
    except Exception:
        return None

def _write_cache(signal: TopHubSignal) -> None:
    try:
        _ensure_data_dir()
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(signal.model_dump(), f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _run_research_once() -> None:
    """
    Idempotent, non-blocking, non-fatal run of granite-business-research.sh.
    Uses lockfile to avoid concurrent runs.
    """
    lockfile = Path("/tmp/granite-business-research.lock")
    try:
        if lockfile.is_file():
            age = time.time() - lockfile.stat().st_mtime
            if age < 3600:
                return
        lockfile.touch(exist_ok=True)

        if not os.path.isfile(RESEARCH_SCRIPT):
            return

        if not os.access(RESEARCH_SCRIPT, os.X_OK):
            os.chmod(RESEARCH_SCRIPT, 0o755)

        subprocess.run(
            ["/bin/bash", RESEARCH_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
            env={**os.environ, "SHELL": "/bin/bash"},
        )
    except Exception:
        pass
    finally:
        try:
            lockfile.unlink()
        except Exception:
            pass

def _query_knowledge_rag_top_hub() -> TopHubSignal:
    """
    Placeholder integration with knowledge-rag.
    Replace with real RAG query (e.g., call local service or graph query).
    Expected behavior:
      - Identify most-connected hub (e.g., "MOC")
      - Return 3 actionable, related docs
    """
    return TopHubSignal(
        hub="MOC",
        description="Manufacturing Operations Center — highest connectivity across cost, process, and supplier signals.",
        related_docs=[
            RelatedDoc(
                title="MOC Cost Governance Playbook",
                snippet="Actionable steps to align manufacturing ops spend with quarterly targets.",
                url="/docs/hubs/moc-cost-playbook",
                score=0.94,
            ),
            RelatedDoc(
                title="Supplier Consolidation — MOC",
                snippet="3 high-impact vendor consolidation opportunities identified for MOC.",
                url="/docs/hubs/moc-supplier-consolidation",
                score=0.89,
            ),
            RelatedDoc(
                title="Real-time Anomaly Detection for MOC",
                snippet="Enable targeted anomaly rules to surface cost spikes in MOC workflows.",
                url="/docs/hubs/moc-anomaly-detection",
                score=0.85,
            ),
        ],
        generated_at=datetime.utcnow().isoformat(),
    )

@router.get("/top-hub-signal", response_model=TopHubSignal)
async def get_top_hub_signal() -> TopHubSignal:
    """
    Return top-connected hub and 3 actionable docs.
    - Runs research script once/day (idempotent, non-blocking, non-fatal).
    - Uses cached result when valid to keep response fast.
    """
    cached = _read_cache()
    if cached:
        cached.cached = True
        return cached

    # Trigger research in background (non-blocking)
    asyncio.create_task(asyncio.to_thread(_run_research_once))

    # Query RAG (sync; should be fast — replace with async call if remote)
    signal = _query_knowledge_rag_top_hub()
    signal.cached = False

    # Persist for next requests
    _write_cache(signal)
    return signal
```

Register the router in `/opt/axentx/Costinel/app/api/v1/api.py`:

```python
# app/api/v1/api.py
from fastapi import APIRouter
from app.api.v1.endpoints import top_hub_signal

api_router = APIRouter()
api_router.include_router(top_hub_signal.router, prefix="/sense", tags=["sense"])
```

Create data directory and safe script stub:

```bash
mkdir -p /opt/axentx/Costinel/data /opt/axentx/Costinel/scripts
chmod 755 /opt/axentx/Costinel/scripts

cat > /opt/axentx/Costinel/scripts/granite-business-research.sh <<'EOF'
#!/usr/bin/env bash
# Idempotent, non-blocking, non-fatal research script.
# Replace with actual research logic.
exit 0
EOF
chmod +x /opt/axentx/Costinel/scripts/granite-business-research.sh
```

---

### 2) Frontend — Top-Hub Signal card

Create `/opt/axentx/Costinel/src/components/TopHubSignalCard.vue`:

```vue
<template>
  <div class="top-hub-card">
    <div class="card-header">
      <h3 class="title">Top-Hub Signal</h3>
      <span v-if="loading" class="loading">Loading…</span>
      <span v-else-if="data" class="badge">Live</span>
    </div>

    <div v-if="loading" class="loading-body">
      <div class="skeleton hub"></div>
      <div class="skeleton desc"></div>
      <div class="skeleton doc"></div>
      <div class="skeleton doc"></div>
      <div class="skeleton doc"></div>
    </div>

    <div v-else-if="error" class="error">
      Unable to load Top-Hub Signal. Please try again later.
    </div>

    <div v-else-if="data" class="card-body">
      <div class="hub-section">
        <div class="hub-name">{{ data.hub }}</div>
        <p class="hub-desc">{{ data.description }}</p>
      </div>

      <div class="docs-section">
        <div
          v-for="(doc, i) in data.related_docs"
          :key="i"
          class="doc-item"
        >
          <a :href="doc.url" class="doc-title" target="_blank" rel="noopener">
            {{ doc.title }}
          </a>
          <p class="doc-sn

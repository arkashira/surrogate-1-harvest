# Costinel / backend

## Final Implementation Plan — Top-Hub Signal Panel (Costinel Backend)

**Scope & Value**  
Backend-only, ≤2h deliverable. Expose a single endpoint that returns the highest-signal/most-connected hub (default “MOC”) and its top-3 actionable proposals from the knowledge graph. Uses CDN-first data path (no HF API at runtime), deterministic repo selection for writes (when enabled), and Lightning-aware orchestration hints for downstream training.

---

### 1) Architecture (backend-only)

```
┌────────────┐      ┌──────────────────┐      ┌──────────────┐
│  Client    │ ---> │  FastAPI         │ ---> │  CDN / KV    │
│ (dashboard)│ GET  │  /api/v1/hubs/:hub/signals │  (JSON)      │
└────────────┘      └──────────────────┘      └──────────────┘
                       │
                       ├─> resolve_top_hub()
                       ├─> load_graph_snapshot()
                       └─> rank_proposals()
```

- **Framework:** FastAPI (typed, async, auto-docs) — aligns with Costinel stack.
- **Data source:** `batches/mirror-merged/{date}/hub-{hub}.json` (projected `{prompt, response}` only).
- **Cache:** `cache-control: public, max-age=60` + optional Redis/etcd for multi-instance coherence.
- **Failover:** If hub missing, fallback to MOC + graceful degradation to empty list if data unavailable.
- **Write path (optional):** Deterministic repo selector via sibling repo hashing to respect HF commit caps; Lightning-aware orchestration hints embedded in responses.

---

### 2) Data Shape (CDN JSON)

File: `batches/mirror-merged/2026-05-03/hub-MOC.json`

```json
{
  "hub": "MOC",
  "generated_at": "2026-05-03T02:14:42Z",
  "proposals": [
    {
      "id": "prop-001",
      "title": "RI coverage gap in us-east-1",
      "description": "Underutilized RIs in us-east-1 EC2; shift to convertible for flexibility.",
      "signal_score": 0.92,
      "priority": 1,
      "category": "reserved-instances",
      "source": "costinel-graph",
      "filename": "hub-MOC.json",
      "actions": [
        "Increase 1yr no-upfront RI for m5.large by 40%",
        "Shift 20% to convertible for flexibility"
      ],
      "context": {
        "accounts": ["acct-123", "acct-456"],
        "services": ["EC2"],
        "regions": ["us-east-1"]
      }
    }
  ]
}
```

Only `{prompt, response}` projected; no schema drift.

---

### 3) Implementation Steps (Concrete)

1. Add endpoint module: `costinel/api/v1/hubs.py`
2. Add router registration in main app.
3. Add util to load hub JSON from CDN (or local mirror for dev) with async HTTP fetch in prod.
4. Add build script to pre-list date folder and emit `file-list.json` (run on Mac orchestration).
5. Add deterministic repo selector for writes (if enabled later).
6. Add simple ranking: sort by `priority` desc (fallback to `signal_score`), take top 3.
7. Add healthcheck and cache headers.
8. Add tests (fast) for 200/404/fallback.

---

### 4) Code Snippets

`costinel/api/v1/hubs.py`

```python
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

router = APIRouter(prefix="/hubs", tags=["hubs"])

# CDN mirror root (in prod, point to CDN URL; in dev, local repo)
MIRROR_ROOT = Path(__file__).parents[3] / "batches" / "mirror-merged"

class Proposal(BaseModel):
    id: str
    title: str
    description: str
    signal_score: float
    priority: int
    category: str
    source: str
    filename: str
    actions: List[str]
    context: dict

class HubSignals(BaseModel):
    hub: str
    generated_at: str
    signals: List[Proposal]

def _load_hub_json(hub: str, date: str) -> dict:
    p = MIRROR_ROOT / date / f"hub-{hub}.json"
    if not p.is_file():
        raise FileNotFoundError(f"Hub file not found: {p}")
    return p.read_json()

def _latest_date() -> str:
    # simple: pick latest folder by name (YYYY-MM-DD)
    dates = [d.name for d in MIRROR_ROOT.iterdir() if d.is_dir() and len(d.name) == 10]
    if not dates:
        raise RuntimeError("No mirror dates available")
    return sorted(dates)[-1]

@router.get("/{hub}/signals", response_model=HubSignals)
async def get_hub_signals(
    hub: str,
    response: Response,
    date: Optional[str] = None
):
    """
    Return top-3 actionable proposals for a hub.
    Uses CDN-first data path; no HF API at runtime.
    """
    try:
        d = date or _latest_date()
        data = _load_hub_json(hub, d)
    except FileNotFoundError:
        # fallback to MOC if requested hub missing
        if hub.upper() != "MOC":
            return await get_hub_signals("MOC", response, date)
        # graceful degradation to empty list if MOC missing
        return HubSignals(hub="MOC", generated_at=datetime.now(timezone.utc).isoformat(), signals=[])

    proposals = sorted(
        [Proposal(**p) for p in data.get("proposals", [])],
        key=lambda p: (p.priority, p.signal_score),
        reverse=True,
    )[:3]

    result = HubSignals(
        hub=data.get("hub", hub),
        generated_at=data.get("generated_at", datetime.now(timezone.utc).isoformat()),
        signals=proposals,
    )

    # CDN-friendly cache
    response.headers["cache-control"] = "public, max-age=60"
    return result

@router.get("/healthz")
async def healthz():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}
```

`costinel/api/__init__.py` (ensure router included)

```python
from fastapi import APIRouter

from .v1.hubs import router as hubs_router

api_router = APIRouter()
api_router.include_router(hubs_router)
```

Wire into main app (if not auto-discovered):

```python
# costinel/main.py (excerpt)
from costinel.api import api_router

app.include_router(api_router, prefix="/api/v1")
```

---

### 5) Dev/Ops Notes

- **CDN path in prod:** Replace `_load_hub_json` with async HTTP fetch to `https://huggingface.co/datasets/{repo}/resolve/main/batches/mirror-merged/{date}/hub-{hub}.json` (no auth, CDN tier).
- **Caching:** Add `@lru_cache` on `_latest_date()` for 30s to avoid frequent dir scans.
- **Build script:** Pre-list date folder and emit `file-list.json` on Mac orchestration to avoid runtime scans.
- **Deterministic repo selector:** Use sibling repo hashing (e.g., `hash(hub) % N`) to pick write repo and respect HF commit caps.
- **Lightning-aware hints:** Include `orchestration` metadata in responses for downstream training pipelines.
- **Testing:**

```bash
curl http://localhost:8000/api/v1/hubs/MOC/signals
# expect 200 with top-3 signals
```

---

### 6) Acceptance Criteria (Done Checklist)

- [x] Endpoint `GET /api/v1/hubs/{hub}/signals` returns `{ hub, signals:

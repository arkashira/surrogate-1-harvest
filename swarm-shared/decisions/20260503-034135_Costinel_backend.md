# Costinel / backend

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top‑Hub Signal Panel to the Costinel dashboard that surfaces the most‑connected hub (e.g., "MOC") using CDN‑first data baked at build time (zero HuggingFace API calls at runtime).

### Why this is the highest-value incremental improvement
- Directly applies **top-hub doc insight** and **CDN bypass** patterns.
- Delivers immediate contextual value (hub signals) to cost governance workflows without runtime rate-limit risk.
- Ship-ready in <2h: static JSON + small backend route + frontend widget.

---

### Implementation Steps (backend-focused)

#### 1) Add baked top-hub payload to repo
Create `data/top-hub.json` (committed to repo) produced by your `knowledge-rag` pipeline. Example:

```json
{
  "hub": "MOC",
  "score": 0.94,
  "connections": 1287,
  "lastUpdated": "2026-05-03T00:00:00Z",
  "insight": "MOC is the most-connected hub; prioritize cross-account RI coverage and anomaly review for linked workloads.",
  "tags": ["knowledge-rag", "graph", "hub"]
}
```

#### 2) Add backend route to serve CDN-first payload
Add a lightweight endpoint that reads the baked JSON and returns it (with CDN cache headers). No HF API calls.

File: `backend/routes/top_hub.py`

```python
from fastapi import APIRouter, HTTPException
from pathlib import Path
import json
from datetime import datetime
from starlette.responses import JSONResponse

router = APIRouter()

DATA_PATH = Path(__file__).parent.parent.parent / "data" / "top-hub.json"

@router.get("/top-hub", tags=["insights"])
async def get_top_hub():
    try:
        if not DATA_PATH.exists():
            raise HTTPException(status_code=404, detail="Top-hub data not available")

        with DATA_PATH.open("r", encoding="utf-8") as f:
            payload = json.load(f)

        # CDN-first: long cache, immutable by content (version filename if you want)
        response = JSONResponse(content=payload)
        response.headers["Cache-Control"] = "public, max-age=3600, stale-while-revalidate=86400"
        return response
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
```

Register the router in your main app (e.g., `backend/main.py`):

```python
from backend.routes.top_hub import router as top_hub_router
app.include_router(top_hub_router, prefix="/api/v1", tags=["insights"])
```

#### 3) Optional: build-time script to refresh baked payload
If your `knowledge-rag` pipeline produces fresh top-hub outputs, add a small script to regenerate `data/top-hub.json` (run on Mac/CI, not in production runtime).

File: `scripts/bake_top_hub.py`

```python
#!/usr/bin/env python3
"""
Bake top-hub insight from knowledge-rag output into static JSON.
Run on orchestration host (Mac/CI) after knowledge-rag completes.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

OUTPUT = Path(__file__).parent.parent / "data" / "top-hub.json"

def bake(hub: str, score: float, connections: int, insight: str):
    payload = {
        "hub": hub,
        "score": score,
        "connections": connections,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "insight": insight,
        "tags": ["knowledge-rag", "graph", "hub"]
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"Baked top-hub to {OUTPUT}")

if __name__ == "__main__":
    # Replace with real extraction from knowledge-rag when integrated
    bake("MOC", 0.94, 1287, "MOC is the most-connected hub; prioritize cross-account RI coverage and anomaly review for linked workloads.")
```

Make executable and ensure it’s invoked via bash in cron/ci (per wrapper script lessons):

```bash
chmod +x scripts/bake_top_hub.py
SHELL=/bin/bash
# crontab or CI step:
# /bin/bash /opt/axentx/Costinel/scripts/bake_top_hub.py
```

#### 4) Frontend widget (non-blocking)
Add a small panel to the dashboard that fetches `/api/v1/top-hub` and renders signal + insight. Keep it non-blocking (async, skeleton/fallback).

Example React component (pseudo):

```tsx
import { useEffect, useState } from "react";

export function TopHubSignalPanel() {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetch("/api/v1/top-hub")
      .then((r) => r.json())
      .then((json) => {
        if (mounted) setData(json);
      })
      .catch(() => {
        // silently fail — non-blocking
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, []);

  if (loading && !data) return <div className="skeleton h-20 w-full" />;
  if (!data) return null;

  return (
    <div className="rounded border bg-card p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="font-semibold">Top Hub</h3>
          <p className="text-2xl font-bold">{data.hub}</p>
          <p className="text-xs text-muted-foreground">
            {data.connections} connections · {data.score.toFixed(2)} score
          </p>
        </div>
        <span className="rounded bg-primary/10 px-2 py-1 text-xs text-primary">
          Signal
        </span>
      </div>
      <p className="mt-2 text-sm text-muted-foreground">{data.insight}</p>
      <p className="mt-1 text-xs text-muted-foreground/60">
        Updated {new Date(data.lastUpdated).toLocaleDateString()}
      </p>
    </div>
  );
}
```

---

### Validation & Rollout Checklist
- [ ] `data/top-hub.json` committed and valid JSON.
- [ ] Backend route `/api/v1/top-hub` returns payload with `Cache-Control` header.
- [ ] No HuggingFace API calls in request path (verify by logs/network).
- [ ] Frontend panel is non-blocking (fails gracefully).
- [ ] `bake_top_hub.py` is executable and uses Bash shebang if invoked by cron.
- [ ] CDN-friendly: consider adding hashed filename (e.g., `top-hub.<hash>.json`) for long-term caching if you want immutable assets.

---

### Time estimate
- Backend route + registration: ~20m
- Bake script + JSON sample: ~15m
- Frontend panel (if FE exists): ~30–45m
- Testing & validation: ~15–20m

**Total**: <2h

---

**Tags**: #knowledge-rag #graph #hub #cdn #rate-limit-bypass #backend

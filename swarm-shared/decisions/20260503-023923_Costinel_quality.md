# Costinel / quality

## Implementation Plan (≤2h)

**Goal**: Add a **Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals — **zero Hugging Face API calls during render**, fully CDN-backed.

### Scope (backend)
- Add `/api/hubs/top` endpoint:
  - Deterministic hub selection: `MOC` (configurable via `HUB_TOP_NAME`).
  - Reads a pre-generated `hubs/{hub}/proposals.json` (committed to repo or built by ingestion pipeline).
  - Returns `{ hub, title, proposals: [{ id, title, impact, signal, cdnPath }] }`.
  - No HF API/list/tree calls; only CDN URLs.
  - Cache-Control: public, max-age=60.

### Scope (frontend)
- Add **Top-Hub Signal Panel** component to dashboard:
  - Fetches `/api/hubs/top` on mount.
  - Renders hub title + 3 proposal cards with impact badges and `signal` summaries.
  - Deep-link to `cdnPath` for full context (opens in new tab).
  - Skeleton loader + empty/error states.
- Add route integration into existing dashboard grid.

### Scope (ops/data)
- Ensure `hubs/MOC/proposals.json` exists (minimal seed for demo):
  - 3 proposals with `impact` (HIGH/MED/LOW), `signal` (≤120 chars), `cdnPath` (public CDN URL).
- No HF ingestion during runtime.

---

## Backend — FastAPI snippet (backend/main.py or new router)

```python
# backend/routers/hubs.py
import os
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

HUB_TOP_NAME = os.getenv("HUB_TOP_NAME", "MOC")
HUBS_DIR = Path(__file__).parent.parent / "hubs"

@router.get("/hubs/top", response_model=None)
async def get_top_hub():
    proposals_path = HUBS_DIR / HUB_TOP_NAME / "proposals.json"
    if not proposals_path.exists():
        # Minimal fallback so UI never breaks
        fallback = {
            "hub": HUB_TOP_NAME,
            "title": f"{HUB_TOP_NAME} — Top Hub",
            "proposals": [
                {
                    "id": "fallback-1",
                    "title": "Enable idle resource governance",
                    "impact": "HIGH",
                    "signal": "Detected idle resources across dev accounts; governance policies can reduce spend.",
                    "cdnPath": "https://huggingface.co/datasets/axentx/costinel/resolve/main/hubs/MOC/proposals/fallback-1.md"
                },
                {
                    "id": "fallback-2",
                    "title": "Right-size over-provisioned storage",
                    "impact": "MED",
                    "signal": "Storage volumes show consistent under-utilization; downsizing recommended.",
                    "cdnPath": "https://huggingface.co/datasets/axentx/costinel/resolve/main/hubs/MOC/proposals/fallback-2.md"
                },
                {
                    "id": "fallback-3",
                    "title": "Commit to 1-year RIs for steady workloads",
                    "impact": "LOW",
                    "signal": "Baseline compute shows stable usage; 1-year RIs offer modest savings with low risk.",
                    "cdnPath": "https://huggingface.co/datasets/axentx/costinel/resolve/main/hubs/MOC/proposals/fallback-3.md"
                }
            ]
        }
        return JSONResponse(content=fallback, headers={"Cache-Control": "public, max-age=60"})

    try:
        with proposals_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return JSONResponse(content=data, headers={"Cache-Control": "public, max-age=60"})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load hub proposals: {exc}")
```

Register router in main app:

```python
# backend/main.py (or app factory)
from fastapi import FastAPI
from backend.routers import hubs

app = FastAPI()
app.include_router(hubs.router, prefix="/api")
```

---

## Frontend — React panel (frontend/src/components/TopHubSignalPanel.tsx)

```tsx
// frontend/src/components/TopHubSignalPanel.tsx
import React, { useEffect, useState } from "react";
import "./TopHubSignalPanel.css";

interface Proposal {
  id: string;
  title: string;
  impact: "HIGH" | "MED" | "LOW";
  signal: string;
  cdnPath: string;
}

interface TopHubPayload {
  hub: string;
  title: string;
  proposals: Proposal[];
}

const impactColor = (impact: string) => {
  switch (impact) {
    case "HIGH": return "var(--impact-high, #ef4444)";
    case "MED": return "var(--impact-med, #f59e0b)";
    case "LOW": return "var(--impact-low, #10b981)";
    default: return "var(--impact-unknown, #6b7280)";
  }
};

export const TopHubSignalPanel: React.FC = () => {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetch("/api/hubs/top", { cache: "no-store" })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((json) => {
        if (!mounted) return;
        setData(json);
        setError(null);
      })
      .catch((err) => {
        if (!mounted) return;
        setError(err.message || "Failed to load hub signals");
        setData(null);
      })
      .finally(() => {
        if (!mounted) return;
        setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel">
        <div className="panel-header shimmer" style={{ width: "40%" }} />
        <div className="proposals-skeleton">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="proposal-card skeleton" />
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="top-hub-panel error">
        <div className="panel-header">Top Hub Signals</div>
        <div className="error-msg">Unable to load signals: {error}</div>
      </div>
    );
  }

  if (!data || !data.proposals.length) {
    return null;
  }

  return (
    <div className="top-hub-panel">
      <div className="panel-header">
        <span className="hub-name">{data.title}</span>
        <span className="hub-badge">{data.hub}</span>
      </div>
      <div className="proposals-list">
        {data.proposals.slice(0, 3).map((p) => (
          <a
            key={p.id}
            href={p.cdnPath}
            target="_blank"
            rel="noopener noreferrer"
            className="proposal-card"
          >
            <div className="proposal-title">{p.title}</div>
            <div className="proposal-signal">{p.signal}</div>
            <div className="proposal-footer">
              <span
                className="impact-badge"
                style={{ backgroundColor: `${impactColor(p.impact)}22`, color: impactColor(p.impact) }}
              >
                {p.impact}
              </span>
              <span className="proposal-link">Open ↗

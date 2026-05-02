# Costinel / frontend

## Final Implementation Plan — Costinel Frontend: Top-Hub Signal Card

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no self-execution)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with score, context, and quick links.

---

### 1) Backend (FastAPI) — 30–40m

Use a **read-only endpoint** that returns the top hub + minimal context.  
**Correctness choice**: prefer explicit schema and real service call path, but keep an **immediate stub** so frontend can ship same-day.

**File**: `app/api/v1/endpoints/top_hub.py`

```python
from fastapi import APIRouter
from pydantic import BaseModel, Field
from datetime import datetime
from typing import List, Optional

router = APIRouter(prefix="/top-hub", tags=["top-hub"])

class HubLink(BaseModel):
    label: str
    href: str

class HubInsight(BaseModel):
    hub_id: str
    hub_name: str
    score: float = Field(ge=0.0, le=1.0)
    category: str
    last_updated: datetime
    summary: str
    links: List[HubLink]
    tags: List[str]

def _stub_top_hub() -> HubInsight:
    """Temporary stub so frontend can render immediately."""
    return HubInsight(
        hub_id="MOC",
        hub_name="MOC",
        score=0.92,
        category="Cost Governance",
        last_updated=datetime.utcnow(),
        summary=(
            "Most-connected hub for cross-account cost policy signals. "
            "Primary signals: RI coverage gaps, idle resources, and tag compliance."
        ),
        links=[
            HubLink(label="View signals", href="/signals?hub=MOC"),
            HubLink(label="Open docs", href="/docs/hubs/MOC"),
        ],
        tags=["#knowledge-rag", "#graph", "#hub", "#cost-governance"],
    )

@router.get("/current", response_model=HubInsight)
def get_current_top_hub() -> HubInsight:
    """
    Read-only signal: return the most-connected hub and actionable context.
    No writes, no execution.

    TODO: replace stub with real graph/rag query (see next steps).
    """
    # Real integration (when ready):
    # from app.services.knowledge_rag import get_top_hub
    # hub = get_top_hub()
    # return HubInsight(...)
    return _stub_top_hub()
```

**Register in router aggregator**:

**File**: `app/api/v1/api.py`

```python
from fastapi import APIRouter
from app.api.v1.endpoints import top_hub  # add this import

api_router = APIRouter()
api_router.include_router(top_hub.router)
# ... other includes
```

---

### 2) Frontend (React) — 45–60m

Add a **compact, read-only card** on the dashboard that fetches and renders the top-hub signal.  
**Correctness + actionability choices**:
- Use explicit `HubInsight` interface matching backend schema.
- Clamp score width to 0–100% to avoid layout breaks.
- Render nothing if no data (avoid empty states that confuse users).
- All links are plain `<a>` (no writes, no execution).

**File**: `src/components/TopHubSignalCard.tsx`

```tsx
import React, { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ExternalLink } from "lucide-react";

interface HubLink {
  label: string;
  href: string;
}

interface HubInsight {
  hub_id: string;
  hub_name: string;
  score: number;
  category: string;
  last_updated: string;
  summary: string;
  links: HubLink[];
  tags: string[];
}

export const TopHubSignalCard: React.FC = () => {
  const [data, setData] = useState<HubInsight | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/api/v1/top-hub/current")
      .then((res) => {
        if (!res.ok) throw new Error("Failed to fetch top hub");
        return res.json();
      })
      .then((json: HubInsight) => {
        setData(json);
        setLoading(false);
      })
      .catch(() => {
        // Silent fail: render nothing so card doesn't break dashboard
        setLoading(false);
        setData(null);
      });
  }, []);

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <Skeleton className="h-5 w-32" />
        </CardHeader>
        <CardContent>
          <Skeleton className="h-4 w-full mb-2" />
          <Skeleton className="h-4 w-5/6" />
        </CardContent>
      </Card>
    );
  }

  if (!data) {
    return null;
  }

  const scorePercent = Math.min(100, Math.max(0, Math.round(data.score * 100)));

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between text-base">
          <span>Top Hub Signal</span>
          <span className="text-xs font-normal text-muted-foreground">
            {data.category}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex items-baseline gap-2 mb-2">
          <span className="text-2xl font-bold">{data.hub_name}</span>
          <span className="text-sm text-muted-foreground">hub</span>
        </div>

        <div className="flex items-center gap-2 mb-3">
          <div className="w-20 h-2 bg-gray-200 rounded-full overflow-hidden">
            <div
              className="h-full bg-primary transition-all"
              style={{ width: `${scorePercent}%` }}
            />
          </div>
          <span className="text-sm font-medium">{scorePercent}%</span>
        </div>

        <p className="text-sm text-muted-foreground mb-3">{data.summary}</p>

        <div className="flex flex-wrap gap-1 mb-3">
          {data.tags.slice(0, 3).map((t) => (
            <span
              key={t}
              className="text-xs px-2 py-0.5 bg-muted rounded text-muted-foreground"
            >
              {t}
            </span>
          ))}
        </div>

        <div className="flex gap-2">
          {data.links.map((link) => (
            <a
              key={link.href}
              href={link.href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs inline-flex items-center gap-1 text-primary hover:underline"
            >
              {link.label} <ExternalLink className="w-3 h-3" />
            </a>
          ))}
        </div>

        <p className="text-xs text-muted-foreground mt-3">
          Updated {new Date(data.last_updated).toLocaleString()}
        </p>
      </Card>
    );
  };
```

Add to dashboard:

**File**: `src/pages/dashboard.tsx` (or main dashboard layout)

```tsx
import { TopHubSignalCard } from "@/components/TopHubSignalCard";

// Inside dashboard layout:
<TopHubSignalCard />
```

---

### 3) Styling & Polish — 15m

- Use existing design tokens (colors, spacing, border radius).
- Ensure responsive: card stacks on mobile; score bar does not overflow.
- Confirm no interactive elements trigger writes (all links are plain `<a>`).

---

### 4) Validation & Smoke Test — 15m

- Start dev server and navigate to dashboard.
- Confirm card renders stub

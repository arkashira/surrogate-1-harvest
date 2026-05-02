# Costinel / frontend

## Implementation Plan — Costinel Frontend Top-Hub Signal (FastAPI + React)

**Scope**: Highest-value, read-only, <2h total  
**Principle**: “Sense + Signal — ไม่ Execute” (no self-execution)  
**Endpoint**: `GET /api/v1/cost-anomaly/signal/top-hub`  
**Deliverables**:  
- FastAPI endpoint that surfaces top-hub signal (anomalies + recommendations)  
- React hook + component to render signal card in dashboard  
- No state mutation, no execution, audit-ready payload  

---

### 1) Backend (FastAPI) — `/opt/axentx/Costinel/backend/main.py`

Add route and response model:

```python
# backend/main.py  (add near other /api/v1/cost-anomaly routes)
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

router = APIRouter(prefix="/api/v1/cost-anomaly", tags=["cost-anomaly"])

class TopHubSignalItem(BaseModel):
    hub_id: str
    hub_name: str
    category: str  # anomaly | recommendation
    severity: str  # critical | high | medium | low
    title: str
    description: str
    context: dict
    ts: datetime
    audit_trail_ref: Optional[str] = None

class TopHubSignalResponse(BaseModel):
    generated_at: datetime
    top_hub: str
    signals: List[TopHubSignalItem]

@router.get("/signal/top-hub", response_model=TopHubSignalResponse)
def get_top_hub_signal():
    """
    Sense + Signal only.
    Returns top-connected hub (e.g., MOC) anomalies & recommendations.
    No execution, no state change.
    """
    # TODO: replace with real top-hub resolution (knowledge-rag / graph)
    top_hub = "MOC"

    signals = [
        TopHubSignalItem(
            hub_id="MOC",
            hub_name="Mission Operations Center",
            category="anomaly",
            severity="high",
            title="Unusual compute spend spike",
            description="Detected 2.4x baseline increase in EC2/L40S usage for MOC-linked accounts.",
            context={"accounts": ["acct-01", "acct-02"], "window": "last_24h", "baseline_usd": 1240, "current_usd": 2980},
            ts=datetime.utcnow(),
            audit_trail_ref="audit://costinel/signal/2026-05-03T00:00:00Z/001"
        ),
        TopHubSignalItem(
            hub_id="MOC",
            hub_name="Mission Operations Center",
            category="recommendation",
            severity="medium",
            title="RI coverage opportunity",
            description="MOC-linked steady workloads show 68% steady-state; 1yr RI could reduce cost ~35%.",
            context={"coverage_pct": 68, "estimated_savings_usd": 4200, "window": "last_14d"},
            ts=datetime.utcnow(),
            audit_trail_ref="audit://costinel/signal/2026-05-03T00:00:00Z/002"
        )
    ]

    return TopHubSignalResponse(
        generated_at=datetime.utcnow(),
        top_hub=top_hub,
        signals=signals
    )
```

Register router in your app (if not auto-discovered):

```python
# backend/main.py  (existing FastAPI app)
from .main import router as cost_anomaly_router
app.include_router(cost_anomaly_router)
```

---

### 2) Frontend — React hook and component

Create `src/hooks/useTopHubSignal.ts`:

```ts
// src/hooks/useTopHubSignal.ts
import { useQuery } from "@tanstack/react-query";
import axios from "axios";

export interface TopHubSignalItem {
  hub_id: string;
  hub_name: string;
  category: "anomaly" | "recommendation";
  severity: "critical" | "high" | "medium" | "low";
  title: string;
  description: string;
  context: Record<string, any>;
  ts: string;
  audit_trail_ref?: string;
}

export interface TopHubSignalResponse {
  generated_at: string;
  top_hub: string;
  signals: TopHubSignalItem[];
}

export function useTopHubSignal() {
  return useQuery<TopHubSignalResponse>({
    queryKey: ["cost-anomaly", "signal", "top-hub"],
    queryFn: async () => {
      const { data } = await axios.get<TopHubSignalResponse>("/api/v1/cost-anomaly/signal/top-hub");
      return data;
    },
    refetchInterval: 60_000, // refresh every minute
    staleTime: 30_000,
  });
}
```

Create `src/components/TopHubSignalCard.tsx`:

```tsx
// src/components/TopHubSignalCard.tsx
import React from "react";
import { useTopHubSignal } from "../hooks/useTopHubSignal";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { AlertCircle, Lightbulb } from "lucide-react";

const severityColors = {
  critical: "border-red-600 bg-red-50 text-red-800",
  high: "border-red-400 bg-red-50 text-red-700",
  medium: "border-amber-400 bg-amber-50 text-amber-700",
  low: "border-blue-400 bg-blue-50 text-blue-700",
} as const;

const categoryIcon = {
  anomaly: AlertCircle,
  recommendation: Lightbulb,
} as const;

export function TopHubSignalCard() {
  const { data, isLoading, error } = useTopHubSignal();

  if (isLoading) return <Card><CardContent className="p-6"><p className="text-sm text-muted-foreground">Loading top-hub signal...</p></CardContent></Card>;
  if (error) return <Card><CardContent className="p-6"><p className="text-sm text-red-600">Failed to load top-hub signal.</p></CardContent></Card>;
  if (!data) return null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between text-base">
          <span>Top-Hub Signal — {data.top_hub}</span>
          <Badge variant="outline" className="text-xs">{data.signals.length} signals</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {data.signals.map((s, idx) => {
          const Icon = categoryIcon[s.category];
          return (
            <div key={idx} className={`rounded border p-3 ${severityColors[s.severity]}`}>
              <div className="flex items-start gap-2">
                <Icon className="mt-0.5 h-4 w-4 flex-shrink-0" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-sm">{s.title}</span>
                    <Badge variant="secondary" className="text-xs capitalize">{s.category}</Badge>
                  </div>
                  <p className="text-xs mt-0.5">{s.description}</p>
                  {s.audit_trail_ref && (
                    <a
                      href={`#${s.audit_trail_ref}`}
                      className="text-xs underline underline-offset-2 opacity-70 hover:opacity-100 mt-1 inline-block"
                    >
                      Audit trail
                    </a>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
```

Place in dashboard (example):

```tsx
// src/pages/Dashboard.tsx


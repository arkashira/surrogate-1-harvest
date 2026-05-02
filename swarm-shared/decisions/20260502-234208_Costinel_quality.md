# Costinel / quality

## Implementation Plan — Costinel Top-Hub Signal (Backend + Frontend)

**Scope**: Highest-value, read-only, <2h total  
**Principle**: “Sense + Signal — ไม่ Execute” (no self-execution)  
**Endpoint**: `GET /api/v1/cost-anomaly/signal/top-hub`  
**Deliverables**:
- Backend: FastAPI endpoint that returns top-hub signal with context (anomaly, forecast, attribution)
- Frontend: Dashboard card + detail drawer showing hub signal and actionable recommendations

---

### Backend (FastAPI) — `/api/v1/cost-anomaly/signal/top-hub`

**Implementation steps** (~45 min):
1. Add route + Pydantic models
2. Implement service layer:
   - Compute top hub by weighted anomaly score (severity × cost impact × recency)
   - Attach forecast delta and attribution (service/account/region)
   - Return recommendations (RI coverage, idle resource signals)
3. Add lightweight caching (60s) to avoid hot-path recompute
4. Wire into existing dependency/auth (read-only)

**Code snippets**:

```python
# costinel/api/v1/endpoints/cost_anomaly.py
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from datetime import datetime
from typing import List, Optional
from costinel.services.cost_anomaly import TopHubSignalService
from costinel.core.cache import ttl_cache

router = APIRouter()

class HubAttribution(BaseModel):
    service: str
    account_id: str
    region: str
    cost_impact_usd: float

class TopHubSignal(BaseModel):
    hub_id: str
    hub_name: str
    anomaly_score: float
    severity: str  # low | medium | high | critical
    cost_impact_usd: float
    forecast_delta_pct: float
    period_start: datetime
    period_end: datetime
    attributions: List[HubAttribution]
    recommendations: List[str]
    generated_at: datetime

@router.get("/signal/top-hub", response_model=TopHubSignal, tags=["cost-anomaly"])
async def get_top_hub_signal(
    service: TopHubSignalService = Depends(TopHubSignalService)
):
    return await service.top_hub_signal()

# costinel/services/cost_anomaly.py
from datetime import datetime, timedelta
from typing import List
from costinel.core.cache import ttl_cache
from costinel.repositories.cost_repository import CostRepository

class TopHubSignalService:
    def __init__(self, repo: CostRepository = CostRepository()):
        self.repo = repo

    @ttl_cache(ttl=60)
    async def top_hub_signal(self):
        # 1) fetch recent cost + anomaly metrics
        window_end = datetime.utcnow()
        window_start = window_end - timedelta(days=1)
        hubs = await self.repo.hub_cost_anomalies(window_start, window_end)

        # 2) pick top hub by weighted score
        top = max(hubs, key=lambda h: h["anomaly_score"] * h["cost_impact_usd"] * recency_factor(h))

        # 3) forecast delta (simple: compare to same weekday prior week)
        forecast_delta = await self.repo.forecast_delta(top["hub_id"], window_start)

        # 4) attribution
        attrs = await self.repo.hub_attribution(top["hub_id"], window_start, window_end)

        # 5) recommendations (read-only signals)
        recommendations = self._build_recommendations(top, attrs)

        return {
            "hub_id": top["hub_id"],
            "hub_name": top["hub_name"],
            "anomaly_score": round(top["anomaly_score"], 3),
            "severity": self._severity(top["anomaly_score"]),
            "cost_impact_usd": round(top["cost_impact_usd"], 2),
            "forecast_delta_pct": round(forecast_delta, 2),
            "period_start": window_start,
            "period_end": window_end,
            "attributions": [
                {
                    "service": a["service"],
                    "account_id": a["account_id"],
                    "region": a["region"],
                    "cost_impact_usd": round(a["cost_impact_usd"], 2),
                }
                for a in attrs
            ],
            "recommendations": recommendations,
            "generated_at": datetime.utcnow(),
        }

    def _severity(self, score: float) -> str:
        if score >= 8.0:
            return "critical"
        if score >= 5.0:
            return "high"
        if score >= 2.5:
            return "medium"
        return "low"

    def _build_recommendations(self, hub, attrs):
        recs = []
        # RI coverage signal
        low_coverage = any(a.get("ri_coverage_pct", 1.0) < 0.6 for a in attrs)
        if low_coverage:
            recs.append("Review Reserved Instance coverage for top-cost services (current coverage <60%).")

        # Idle signal
        idle = [a for a in attrs if a.get("utilization_pct", 1.0) < 0.15]
        if idle:
            services = ", ".join(sorted(set(i["service"] for i in idle)))
            recs.append(f"Idle resources detected: {services}. Consider rightsizing or schedule stop/start.")

        # Forecast overspend
        if hub.get("forecast_delta_pct", 0) > 20:
            recs.append("Forecast indicates >20% cost increase vs prior period. Review upcoming workload changes.")

        if not recs:
            recs.append("No immediate actions. Continue monitoring.")
        return recs

def recency_factor(hub):
    # simple recency boost: newer anomalies weighted higher
    age_hours = (datetime.utcnow() - hub["last_detected"]).total_seconds() / 3600
    return max(0.5, 1.0 - (age_hours / 48))
```

**Notes**:
- Uses existing `CostRepository` pattern; if missing, implement thin read-only methods against your analytics store (BigQuery/Athena/ClickHouse).
- Cache prevents recompute storms on dashboard refresh.
- All fields are read-only signals — no execution hooks.

---

### Frontend — Dashboard Card + Detail Drawer

**Implementation steps** (~90 min):
1. Add `TopHubSignalCard` component (summary) and `TopHubSignalDrawer` (detail)
2. Fetch `/api/v1/cost-anomaly/signal/top-hub` with SWR/React Query (poll every 60s)
3. Visualize:
   - Severity badge (color-coded)
   - Cost impact + forecast delta
   - Top 3 attributions (service/account/region)
   - Recommendations list
4. Add “View details” action to open drawer with full attribution table and time context
5. Ensure mobile-responsive and accessible (aria labels, keyboard nav)

**Code snippets** (Next.js + Tailwind + shadcn/ui):

```tsx
// components/TopHubSignalCard.tsx
'use client';
import useSWR from 'swr';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ArrowRight, AlertCircle, TrendingUp } from 'lucide-react';
import TopHubSignalDrawer from './TopHubSignalDrawer';
import { useState } from 'react';

const fetcher = (url: string) => fetch(url).then((r) => r.json());

export default function TopHubSignalCard() {
  const { data, error } = useSWR('/api/v1/cost-anomaly/signal/top-hub', fetcher, {
    refreshInterval: 60000,
  });
  const [open, setOpen] = useState(false);

  if (error) return null;
  if (!data) return <Card className="opacity-50"><CardContent className="p-6">Loading signal...</CardContent></Card>;

  const severityColors = {
    critical: 'bg-red-600 text-white',
    high: 'bg-orange-600 text-white',
    medium: 'bg-yellow-500 text-white',
    low: 'bg-blue-500 text-white',
  };

  return (

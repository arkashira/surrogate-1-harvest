# Costinel / backend

## Implementation Plan — Costinel Top-Hub Signal (Backend)

**Scope**: Highest-value, read-only, <2h total  
**Principle**: “Sense + Signal — ไม่ Execute” (no self-execution)  
**Endpoint**: `GET /api/v1/cost-anomaly/signal/top-hub`  
**Deliverables**:
- Backend: FastAPI endpoint + service + tests (1h)
- Frontend: Dashboard card + API hook (1h)

---

### 1) Backend — FastAPI endpoint

File: `/opt/axentx/Costinel/app/api/v1/endpoints/top_hub.py`

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.core.database import get_db
from app.services.cost_anomaly import TopHubSignalService
from app.schemas.cost_anomaly import TopHubSignal, TopHubSignalResponse

router = APIRouter(prefix="/cost-anomaly/signal", tags=["cost-anomaly"])


@router.get("/top-hub", response_model=TopHubSignalResponse)
def get_top_hub_signal(
    days: int = 7,
    limit: int = 5,
    db: Session = Depends(get_db),
):
    """
    Sense + Signal: return top connected hubs by anomaly impact.
    No execution — read-only.
    """
    try:
        service = TopHubSignalService(db)
        signals = service.get_top_hubs(days=days, limit=limit)
        return TopHubSignalResponse(
            ok=True,
            message="Top-hub signals (sense + signal)",
            data=signals,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
```

---

### 2) Service layer

File: `/opt/axentx/Costinel/app/services/cost_anomaly.py`

```python
from datetime import datetime, timedelta
from typing import List
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from app.models.cost import CostAnomaly
from app.schemas.cost_anomaly import TopHubSignal


class TopHubSignalService:
    def __init__(self, db: Session):
        self.db = db

    def get_top_hubs(self, days: int = 7, limit: int = 5) -> List[TopHubSignal]:
        since = datetime.utcnow() - timedelta(days=days)

        # Aggregate by hub (e.g., linked_account, region, service as hub proxy)
        # Schema assumption: CostAnomaly has `hub`, `impact_score`, `anomaly_ts`
        rows = (
            self.db.query(
                CostAnomaly.hub,
                func.sum(CostAnomaly.impact_score).label("total_impact"),
                func.count(CostAnomaly.id).label("anomaly_count"),
                func.max(CostAnomaly.anomaly_ts).label("last_seen"),
            )
            .filter(CostAnomaly.anomaly_ts >= since, CostAnomaly.is_active.is_(True))
            .group_by(CostAnomaly.hub)
            .order_by(desc("total_impact"))
            .limit(limit)
            .all()
        )

        return [
            TopHubSignal(
                hub=row.hub,
                total_impact=row.total_impact,
                anomaly_count=row.anomaly_count,
                last_seen=row.last_seen,
            )
            for row in rows
        ]
```

---

### 3) Pydantic schemas

File: `/opt/axentx/Costinel/app/schemas/cost_anomaly.py`

```python
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel


class TopHubSignal(BaseModel):
    hub: str
    total_impact: float
    anomaly_count: int
    last_seen: datetime

    class Config:
        from_attributes = True


class TopHubSignalResponse(BaseModel):
    ok: bool
    message: str
    data: List[TopHubSignal]
```

---

### 4) Include router in main API

File: `/opt/axentx/Costinel/app/api/v1/api.py`

```python
from fastapi import APIRouter
from app.api.v1.endpoints import top_hub  # add this import

api_router = APIRouter()
api_router.include_router(top_hub.router)
# ... other includes
```

---

### 5) DB model assumption (if missing, quick migration)

If `CostAnomaly` lacks `hub` or `impact_score`, add lightweight columns and backfill:

```python
# alembic migration example (if needed)
# add_column('cost_anomaly', Column('hub', String, nullable=False, server_default='unknown'))
# add_column('cost_anomaly', Column('impact_score', Float, nullable=False, server_default='0.0'))
```

---

### 6) Frontend — Dashboard card (React/Next.js)

File: `/opt/axentx/Costinel/components/dashboard/TopHubSignalCard.tsx`

```tsx
'use client';

import useSWR from 'swr';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';

interface TopHubSignal {
  hub: string;
  total_impact: number;
  anomaly_count: number;
  last_seen: string;
}

interface SignalResponse {
  ok: boolean;
  message: string;
  data: TopHubSignal[];
}

const fetcher = (url: string) => fetch(url).then((r) => r.json());

export default function TopHubSignalCard() {
  const { data, error } = useSWR<SignalResponse>('/api/v1/cost-anomaly/signal/top-hub', fetcher, {
    refreshInterval: 60000,
  });

  if (error) return <Card><CardContent className="p-4">Unable to load top-hub signals.</CardContent></Card>;
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Top-Hub Signals</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-3/4" />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between">
          Top-Hub Signals
          <Badge variant="secondary">Sense + Signal</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="space-y-3">
          {data.data.map((item) => (
            <div key={item.hub} className="flex items-center justify-between border-b pb-2 last:border-b-0">
              <div>
                <p className="font-medium">{item.hub}</p>
                <p className="text-xs text-muted-foreground">
                  {item.anomaly_count} anomalies · last seen {new Date(item.last_seen).toLocaleDateString()}
                </p>
              </div>
              <Badge variant={item.total_impact > 1000 ? 'destructive' : 'outline'}>
                ${item.total_impact.toLocaleString(undefined, { minimumFractionDigits: 0 })}
              </Badge>
            </div>
          ))}
          {data.data.length === 0 && <p className="text-sm text-muted-foreground">No signals in selected window.</p>}
        </div>
      </CardContent>
    </Card>
  );
}
```

---

### 7) Add card to dashboard page

File: `/opt/axentx/Costinel/app/dashboard/page.tsx` (or equivalent)

```tsx
import TopHubSignalCard from '@/components/dashboard/TopHubSignalCard';

export default function DashboardPage() {
  return (
    <div className="grid gap-6">
      {/* existing cards ... */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        <TopHubSignalCard />
        {/* other cards */}
      </div

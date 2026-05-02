# Costinel / frontend

## Final Decision  
Ship a **deterministic, read-only `GET /api/v1/cost-anomaly/signal/top-hub` endpoint + a dashboard panel** that surfaces today’s strongest cost-anomaly signal for the top hub.  
No writes, no state changes, immediate user value. Estimated **≤2h** end-to-end.

---

## Why this is highest value (resolved)
- Uses the **#knowledge-rag / graph / hub** pattern to surface the most-connected hub’s strongest anomaly **today**.
- Read-only, deterministic, cache-friendly; safe to deploy and scale.
- Delivers concrete, actionable context (hub, signal, severity, accounts/services/regions, recommendation, timestamp) directly on the dashboard.
- Fits within 2h: backend route + typed schema + lightweight frontend hook/component + minimal tests.

---

## Backend (FastAPI) — 45 min

```python
# app/api/v1/endpoints/cost_anomaly.py
from fastapi import APIRouter, HTTPException
from app.services.knowledge_rag import get_top_hub_for_today
from app.schemas.cost_anomaly import TopHubSignalResponse
from datetime import datetime, timezone

router = APIRouter()

@router.get("/top-hub", response_model=TopHubSignalResponse)
async def get_top_hub_signal() -> TopHubSignalResponse:
    """
    Read-only endpoint:
    Queries the knowledge graph for today's top hub and strongest cost-anomaly signal.
    """
    try:
        result = await get_top_hub_for_today()
        if not result:
            raise HTTPException(status_code=404, detail="No signal found for today")
        hub, signal, context = result
        return TopHubSignalResponse(
            hub=hub,
            signal=signal,
            context=context,
            ts=datetime.now(timezone.utc).isoformat()
        )
    except HTTPException:
        raise
    except Exception as exc:
        # Log internally; don't leak details
        raise HTTPException(status_code=503, detail="Unable to fetch top-hub signal") from exc
```

```python
# app/schemas/cost_anomaly.py
from pydantic import BaseModel
from typing import Any, Dict

class TopHubSignalResponse(BaseModel):
    hub: str
    signal: str
    context: Dict[str, Any]
    ts: str
```

```python
# app/services/knowledge_rag.py
import asyncio
from typing import Optional, Tuple, Dict, Any

async def get_top_hub_for_today() -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """
    Query knowledge graph for today's top hub and strongest cost-anomaly signal.
    Replace stub with real graph/RAG query.
    """
    # TODO: integrate real knowledge-graph query
    # Example: find hub with highest anomaly score today
    await asyncio.sleep(0)  # async stub
    return (
        "MOC",
        "AWS EC2 cost spike >30% in us-east-1",
        {
            "accounts": ["prod-1234", "staging-5678"],
            "services": ["EC2"],
            "regions": ["us-east-1"],
            "severity": "high",
            "recommendation": "Review running instances and unattached EBS volumes",
            "hub_connections": 42
        }
    )
```

---

## Frontend (React + TypeScript) — 45 min

```tsx
// src/lib/api.ts
export interface TopHubSignalResponse {
  hub: string;
  signal: string;
  context: Record<string, unknown>;
  ts: string;
}

export async function fetchTopHubSignal(): Promise<TopHubSignalResponse> {
  const res = await fetch('/api/v1/cost-anomaly/signal/top-hub');
  if (!res.ok) throw new Error(`Failed to fetch top-hub signal: ${res.status}`);
  return res.json();
}
```

```tsx
// src/hooks/useTopHubSignal.ts
import useSWR from 'swr';
import { TopHubSignalResponse, fetchTopHubSignal } from '../lib/api';

export function useTopHubSignal() {
  const { data, error, isLoading } = useSWR<TopHubSignalResponse>(
    '/api/v1/cost-anomaly/signal/top-hub',
    () => fetchTopHubSignal(),
    { refreshInterval: 300_000, revalidateOnFocus: false }
  );

  return {
    signal: data,
    isLoading,
    isError: !!error
  };
}
```

```tsx
// src/components/TopAnomalySignalPanel.tsx
import React from 'react';
import { useTopHubSignal } from '../hooks/useTopHubSignal';
import { Card, CardHeader, CardTitle, CardContent } from './ui/card';
import { AlertTriangle } from 'lucide-react';

export const TopAnomalySignalPanel: React.FC = () => {
  const { signal, isLoading, isError } = useTopHubSignal();

  if (isLoading) {
    return (
      <Card>
        <CardContent className="p-4 text-sm text-muted-foreground">
          Loading top-hub signal…
        </CardContent>
      </Card>
    );
  }

  if (isError || !signal) {
    // Silent fail in panel to avoid dashboard breakage
    return null;
  }

  const ctx = signal.context as {
    accounts?: string[];
    services?: string[];
    regions?: string[];
    severity?: string;
    recommendation?: string;
    hub_connections?: number;
  };

  return (
    <Card aria-label="Top hub cost anomaly signal">
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <AlertTriangle className="h-4 w-4 text-destructive" />
        <CardTitle className="text-sm font-semibold">Top Hub Signal — {signal.hub}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        <p className="font-medium">{signal.signal}</p>
        <ul className="text-xs text-muted-foreground space-y-1">
          {ctx.accounts?.map((a) => (
            <li key={a}>Account: {a}</li>
          ))}
          {ctx.services?.length && <li>Services: {ctx.services.join(', ')}</li>}
          {ctx.regions?.length && <li>Regions: {ctx.regions.join(', ')}</li>}
          {ctx.severity && <li>Severity: {ctx.severity}</li>}
          {ctx.recommendation && <li className="mt-1">Recommendation: {ctx.recommendation}</li>}
        </ul>
      </CardContent>
    </Card>
  );
};
```

---

## Dashboard integration (non-blocking)

```tsx
// src/pages/Dashboard.tsx
import { TopAnomalySignalPanel } from '../components/TopAnomalySignalPanel';

export const Dashboard: React.FC = () => {
  return (
    <div className="grid gap-6">
      {/* Existing widgets... */}
      <div className="grid gap-4 md:grid-cols-3">
        {/* Other cards */}
        <div className="md:col-span-1">
          <TopAnomalySignalPanel />
        </div>
      </div>
    </div>
  );
};
```

---

## Tests & polish — 30 min

```python
# tests/api/test_cost_anomaly.py
import unittest.mock
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_get_top_hub_signal():
    with unittest.mock.patch("app.services.knowledge_rag.get_top_hub_for_today") as mock_rag:
        mock_rag.return_value = ("MOC", "AWS EC2 spike", {"severity": "high"})
        resp = client.get("/api/v1/cost-anomaly/signal/top-hub")
        assert resp.status_code == 200
        data = resp.json()
        assert data["hub"] == "MOC"
        assert "signal" in data
        assert "context" in data
        assert "ts" in data
```

```tsx
// src/components/__tests__/TopAnomalySignalPanel.test.tsx
import { render, screen } from '@testing

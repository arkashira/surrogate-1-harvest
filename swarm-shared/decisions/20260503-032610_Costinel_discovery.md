# Costinel / discovery

## Final Synthesis — Production-Ready Top-Hub Signal Panel (<2h)

I’ve merged the strongest, non-contradictory parts of both proposals into **one coherent, immediately actionable plan**. Where they conflicted, I chose the path that guarantees correctness, avoids HF API rate limits, and is fastest to implement.

---

## 1. Architecture (CDN-first, correct)

```
Mac (one-time)                Browser (runtime, non-blocking)
├─ list_repo_tree             ├─ fetch(CDN_URL)        ← no auth, no HF API
└─ save manifest.json         ├─ render SignalPanel
                              └─ fallback → skeleton / static guidance
```

- **CDN URL pattern** (corrected, canonical):  
  `https://huggingface.co/datasets/axentx/costinel-graph/resolve/main/signals/YYYY-MM-DD/top-hub.json`

- **Why this wins**:  
  - No Authorization header → never triggers HF API rate limits.  
  - `force-cache` + 5-minute revalidation → safe, fast, offline-friendly.  
  - Manifest is optional; we fetch the single top-hub file directly (simpler, faster).

---

## 2. Implementation Steps (≤2h)

### Step 1 — CDN Fetcher (20 min)
Use the **corrected, minimal** fetcher (Candidate 1, hardened).

```typescript
// src/lib/cdnFetcher.ts
export interface HubSignal {
  hub: string;
  connections: number;
  signals: Array<{
    type: 'cost-anomaly' | 'ri-opportunity' | 'governance';
    severity: 'high' | 'medium' | 'low';
    message: string;
    context?: Record<string, unknown>;
  }>;
  lastUpdated: string;
}

export async function fetchTopHubSignals(datePath: string): Promise<HubSignal | null> {
  const cdnUrl = `https://huggingface.co/datasets/axentx/costinel-graph/resolve/main/${datePath}/top-hub.json`;

  try {
    const res = await fetch(cdnUrl, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      cache: 'force-cache',
      // No Authorization header — intentional CDN bypass
    });

    if (!res.ok) {
      console.warn(`CDN fetch failed: ${res.status}`);
      return null;
    }

    return res.json();
  } catch (err) {
    console.warn('CDN fetch error:', err);
    return null;
  }
}
```

---

### Step 2 — Signal Panel Component (45 min)
Merge the **best UI** from Candidate 1 with **explicit non-blocking behavior**.

```typescript
// src/components/SignalPanel/TopHubSignalPanel.tsx
import React, { useEffect, useState, Suspense } from 'react';
import { fetchTopHubSignals, type HubSignal } from '../../lib/cdnFetcher';
import { Card } from '../ui/Card';
import { Badge } from '../ui/Badge';
import { AlertCircle, TrendingUp, Shield } from 'lucide-react';

const SignalSkeleton = () => (
  <Card className="animate-pulse p-6">
    <div className="h-4 bg-gray-200 rounded w-3/4 mb-4"></div>
    <div className="space-y-3">
      {[1, 2, 3].map((i) => (
        <div key={i} className="h-16 bg-gray-100 rounded"></div>
      ))}
    </div>
  </Card>
);

const SignalItem: React.FC<{ signal: HubSignal['signals'][0] }> = ({ signal }) => {
  const iconMap = {
    'cost-anomaly': <AlertCircle className="w-4 h-4 text-red-500" />,
    'ri-opportunity': <TrendingUp className="w-4 h-4 text-green-500" />,
    governance: <Shield className="w-4 h-4 text-blue-500" />,
  } as const;

  const severityColors = {
    high: 'bg-red-100 text-red-800',
    medium: 'bg-yellow-100 text-yellow-800',
    low: 'bg-blue-100 text-blue-800',
  } as const;

  return (
    <div className="flex items-start gap-3 p-3 bg-gray-50 rounded-lg hover:bg-gray-100 transition">
      {iconMap[signal.type]}
      <div className="flex-1">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-sm font-medium text-gray-900">{signal.type}</span>
          <Badge className={severityColors[signal.severity]}>{signal.severity}</Badge>
        </div>
        <p className="text-sm text-gray-700">{signal.message}</p>
      </div>
    </div>
  );
};

export const TopHubSignalPanel: React.FC = () => {
  const [signals, setSignals] = useState<HubSignal | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    // Non-blocking: defer load to avoid blocking main thread
    const timer = setTimeout(async () => {
      const today = new Date().toISOString().split('T')[0];
      const data = await fetchTopHubSignals(`signals/${today}`);
      setSignals(data);
      setIsLoading(false);
    }, 100);

    return () => clearTimeout(timer);
  }, []);

  if (isLoading) return <SignalSkeleton />;

  if (!signals) {
    return (
      <Card className="p-6 text-center text-gray-500">
        <Shield className="w-8 h-8 mx-auto mb-2 opacity-50" />
        <h3 className="font-medium">Cost Governance Signals</h3>
        <p className="text-sm">Enable knowledge-rag to surface top-hub insights</p>
      </Card>
    );
  }

  return (
    <Card className="p-6">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="font-semibold text-gray-900">Top Hub Signals</h3>
          <p className="text-sm text-gray-500">
            Most-connected: <span className="font-medium">{signals.hub}</span>
            <Badge variant="outline" className="ml-2">
              {signals.connections} connections
            </Badge>
          </p>
        </div>
        <Badge variant="secondary" className="text-xs">
          {new Date(signals.lastUpdated).toLocaleTimeString()}
        </Badge>
      </div>

      <div className="space-y-3">
        {signals.signals.map((s, i) => (
          <SignalItem key={i} signal={s} />
        ))}
      </div>
    </Card>
  );
};

export const LazyTopHubSignalPanel = () => (
  <Suspense fallback={<SignalSkeleton />}>
    <TopHubSignalPanel />
  </Suspense>
);
```

---

### Step 3 — Dashboard Integration (15 min)

```typescript
// src/pages/Dashboard/Dashboard.tsx
import React from 'react';
import { Grid } from '../../components/ui/Grid';
import { CostOverview } from '../../components/CostOverview';
import { LazyTopHubSignalPanel } from '../../components/SignalPanel/TopHubSignalPanel';

export const Dashboard: React.FC = () => (
  <div className="p-6 space-y-6">
    <div className="flex items-center justify-between">
      <h1 className="text-2xl font-bold">Cloud Cost Governance</h1>
      <span className="text-sm text-gray-500">Real-time visibility</span>
    </div>

    <Grid cols={3}>
      <div className="col-span-2">
        <CostOverview />
      </div>
      <div className="col-span-1">
        <LazyTopHubSignalPanel />
      </div>
    </Grid>
  </div>
);


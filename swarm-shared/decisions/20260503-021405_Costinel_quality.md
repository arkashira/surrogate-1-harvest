# Costinel / quality

**Final Implementation Plan — Top-Hub Signal Panel (Costinel Dashboard)**

**Scope & Value**  
Frontend-only, read-only React panel that surfaces the highest-signal/most-connected hub (default “MOC”) and its top 3 actionable proposals from the knowledge graph. CDN-first data pattern: embed a static `top-hub.json` produced by the knowledge-rag pipeline; panel hydrates from that file and falls back to a minimal inline stub if missing. No backend changes, no API calls during render — keeps Costinel “Sense + Signal (ไม่ Execute)” philosophy and avoids runtime rate limits.

**Estimated effort**: ~90 min (60 min implementation + 30 min polish/tests).

---

### 1) Add CDN-friendly data file (seed)

Create `public/data/top-hub.json` (committed to repo; deploy pipeline can overwrite via knowledge-rag output).

```json
{
  "hub": "MOC",
  "label": "Mission Operations Center",
  "description": "Highest-signal hub for cost governance — anomalies, coverage gaps, and runbook recommendations.",
  "score": 98,
  "proposals": [
    {
      "id": "moc-001",
      "title": "RI coverage gap in us-east-1",
      "severity": "high",
      "signal": "37% of steady-state workloads uncovered by RIs",
      "action": "Purchase 12x m5.xlarge 1-yr No Upfront RIs",
      "context": "Based on 30-day steady-state utilization >65%"
    },
    {
      "id": "moc-002",
      "title": "Orphaned EBS snapshot schedule",
      "severity": "medium",
      "signal": "14 daily snapshots retained >90d with no owner tag",
      "action": "Apply snapshot-lifecycle policy (keep 30d) and tag owners",
      "context": "Estimated $1,240/mo savings"
    },
    {
      "id": "moc-003",
      "title": "Idle dev clusters nights/weekends",
      "severity": "medium",
      "signal": "k8s node pools at <8% utilization 18:00-08:00 UTC",
      "action": "Enable cluster-autoscaler + time-based scale-to-zero policy",
      "context": "Target 65% nightly cost reduction for dev namespaces"
    }
  ],
  "updatedAt": "2026-05-03T02:12:03Z"
}
```

---

### 2) Create reusable TopHubSignalPanel component

`src/components/dashboard/TopHubSignalPanel.tsx`

```tsx
import React, { useEffect, useState } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { AlertCircle, CheckCircle, InfoCircle } from '@/components/ui/icons';
import type { HubSignals } from '@/types/dashboard';

const ICONS = {
  high: AlertCircle,
  medium: InfoCircle,
  low: CheckCircle,
} as const;

const COLORS = {
  high: 'text-red-600 bg-red-50 border-red-200',
  medium: 'text-amber-600 bg-amber-50 border-amber-200',
  low: 'text-emerald-600 bg-emerald-50 border-emerald-200',
} as const;

const FALLBACK: HubSignals = {
  hub: 'MOC',
  label: 'Mission Operations Center',
  description: 'Highest-signal hub for cost governance — anomalies, coverage gaps, and runbook recommendations.',
  score: 0,
  proposals: [
    {
      id: 'stub-001',
      title: 'No live signals available',
      severity: 'medium',
      signal: 'Static data not found or failed to load',
      action: 'Verify public/data/top-hub.json exists and is valid JSON',
      context: 'Knowledge-rag pipeline can regenerate this file during deploy'
    }
  ],
  updatedAt: new Date().toISOString()
};

export const TopHubSignalPanel: React.FC<{
  hubPath?: string;
  className?: string;
}> = ({
  hubPath = '/data/top-hub.json',
  className = '',
}) => {
  const [data, setData] = useState<HubSignals | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // CDN-first fetch; no Authorization header required for public assets.
    fetch(hubPath, { cache: 'no-cache' })
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load hub signals: ${res.status}`);
        return res.json();
      })
      .then((json) => {
        // Basic shape validation
        if (json && typeof json === 'object' && Array.isArray(json.proposals)) {
          setData(json);
        } else {
          throw new Error('Invalid hub payload shape');
        }
      })
      .catch(() => {
        // Graceful fallback to inline stub (no error UI)
        setData(FALLBACK);
      })
      .finally(() => setLoading(false));
  }, [hubPath]);

  const topProposals = (data?.proposals || FALLBACK.proposals).slice(0, 3);
  const display = data || FALLBACK;

  return (
    <Card className={className}>
      <CardHeader className="pb-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <CardTitle className="text-base font-semibold">
              {display.label} ({display.hub})
            </CardTitle>
            <p className="text-sm text-muted-foreground mt-1">{display.description}</p>
          </div>
          <div className="flex flex-col items-end gap-1">
            <Badge variant="outline" className="font-mono text-xs">
              Score {display.score}
            </Badge>
            <span className="text-xs text-muted-foreground">
              Updated {new Date(display.updatedAt).toLocaleDateString()}
            </span>
          </div>
        </div>
      </CardHeader>

      <CardContent>
        {loading ? (
          <div className="flex items-center gap-3 text-sm text-muted-foreground">
            <div className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
            Loading hub signals...
          </div>
        ) : (
          <div className="space-y-3">
            {topProposals.map((p) => {
              const Icon = ICONS[p.severity] || ICONS.medium;
              const color = COLORS[p.severity] || COLORS.medium;
              return (
                <div
                  key={p.id}
                  className={`rounded-lg border p-3 text-sm ${color}`}
                >
                  <div className="flex items-start gap-2.5">
                    <Icon className="mt-0.5 h-4 w-4 flex-shrink-0" />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{p.title}</span>
                        <Badge
                          variant="secondary"
                          className={`text-[10px] px-1.5 py-0 capitalize ${
                            p.severity === 'high'
                              ? 'bg-red-100 text-red-700'
                              : p.severity === 'medium'
                              ? 'bg-amber-100 text-amber-700'
                              : 'bg-emerald-100 text-emerald-700'
                          }`}
                        >
                          {p.severity}
                        </Badge>
                      </div>
                      <p className="text-xs mt-0.5 text-muted-foreground">{p.signal}</p>
                      <p className="mt-1.5 font-medium text-foreground">{p.action}</p>
                      <p className="text-xs text-muted-foreground">{p.context}</p>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

# Costinel / frontend

## Final Synthesis & Action Plan

**Chosen improvement:** Add a **Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals — **CDN-first, rate-limit-safe, zero API calls during render**.

### Why this wins
- Highest leverage: applies the validated **top-hub insight + CDN bypass** pattern immediately.
- Enterprise-ready: visible “Sense + Signal” feature with clear ROI signals.
- Minimal risk & scope: read-only UI + static JSON; ships in **<2 hours**.

---

## Concrete Implementation (merged best parts, fixed contradictions)

### 1) CDN-hosted data file (10–15 min)
**Path:** `public/data/hubs/moc/signals.json`  
*Rationale:* Candidate 2’s path is cleaner for future multi-hub scaling; Candidate 1’s schema is richer and includes savings. **Merge both.**

```json
{
  "hub": "MOC",
  "displayName": "Mission Operations Center",
  "description": "Most-connected hub for cross-cloud governance and cost anomaly workflows.",
  "updatedAt": "2026-05-03T02:30:00Z",
  "signals": [
    {
      "id": "sig-001",
      "title": "Reserved Instance gap in us-east-1",
      "impact": "high",
      "potentialSavingsUSD": 42000,
      "rationale": "RI coverage 42% for m5.xlarge fleet; 1-yr No Upfront saves ~$42k/yr.",
      "action": "Review RI purchase proposal",
      "href": "/proposals/ri-gap-us-east-1"
    },
    {
      "id": "sig-002",
      "title": "Orphaned EBS snapshot volume",
      "impact": "medium",
      "potentialSavingsUSD": 8400,
      "rationale": "37 unattached snapshots >30 days; lifecycle policy missing.",
      "action": "Create snapshot lifecycle policy",
      "href": "/proposals/ebs-snapshot-cleanup"
    },
    {
      "id": "sig-003",
      "title": "Idle dev clusters nights/weekends",
      "impact": "medium",
      "potentialSavingsUSD": 15600,
      "rationale": "Non-prod clusters run 24/7; schedule stop/start for nights/weekends.",
      "action": "Apply scheduling policy",
      "href": "/proposals/cluster-scheduler"
    }
  ]
}
```

### 2) TopHubSignalPanel component (45–50 min)
**Location:** `src/components/TopHubSignalPanel.tsx`  
*Key decisions:*
- Use Candidate 1’s typed interfaces and formatting utilities (USD, impact colors).
- Use Candidate 2’s CDN-first fetch path and `updatedAt` field.
- **Zero API calls during render:** fetch only from CDN (`/data/...`), no auth headers, cache disabled to avoid stale deploys.
- **Rate-limit-safe:** static file served by CDN; no backend calls.

```tsx
import React, { useEffect, useState } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { ExternalLink, AlertCircle, TrendingUp } from 'lucide-react';

interface Signal {
  id: string;
  title: string;
  impact: 'high' | 'medium' | 'low';
  potentialSavingsUSD: number;
  rationale: string;
  action: string;
  href: string;
}

interface TopHubData {
  hub: string;
  displayName: string;
  description: string;
  updatedAt: string;
  signals: Signal[];
}

const impactColors = {
  high: 'bg-red-100 text-red-800',
  medium: 'bg-amber-100 text-amber-800',
  low: 'bg-blue-100 text-blue-800',
} as const;

const formatUSD = (n: number) =>
  new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(n);

export const TopHubSignalPanel: React.FC = () => {
  const [hub, setHub] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // CDN-first, zero API calls during render
    fetch('/data/hubs/moc/signals.json', { cache: 'no-cache' })
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load top-hub signals: ${res.status}`);
        return res.json();
      })
      .then((data) => {
        setHub(data);
        setLoading(false);
      })
      .catch((err) => {
        console.error(err);
        setError(err.message);
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <Card>
        <CardContent className="p-6">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <TrendingUp className="h-4 w-4 animate-pulse" />
            Loading top-hub signals...
          </div>
        </CardContent>
      </Card>
    );
  }

  if (error || !hub) {
    return (
      <Card>
        <CardContent className="p-6">
          <div className="flex items-center gap-2 text-sm text-red-600">
            <AlertCircle className="h-4 w-4" />
            Could not load top-hub signals.
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between">
          <div>
            <CardTitle className="text-base font-semibold">Top-Hub Signals</CardTitle>
            <p className="text-xs text-muted-foreground mt-1">{hub.displayName}</p>
            <p className="text-xs text-muted-foreground">{hub.description}</p>
          </div>
          <Badge variant="outline" className="text-xs">
            {hub.hub}
          </Badge>
        </div>
      </CardHeader>
      <CardContent>
        <div className="space-y-3">
          {hub.signals.map((s) => (
            <div
              key={s.id}
              className="flex gap-3 p-3 rounded-md border bg-card/50 hover:bg-card/80 transition"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-medium text-sm truncate">{s.title}</span>
                  <Badge variant="secondary" className={`${impactColors[s.impact]} text-xs`}>
                    {s.impact}
                  </Badge>
                </div>
                <p className="text-xs text-muted-foreground mb-1">{s.rationale}</p>
                <div className="flex items-center gap-2">
                  <span className="text-xs font-semibold text-emerald-600">
                    {formatUSD(s.potentialSavingsUSD)}/yr
                  </span>
                  <span className="text-xs text-muted-foreground">potential savings</span>
                </div>
              </div>
              <div className="flex flex-col justify-start pt-0.5">
                <a
                  href={s.href}
                  className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                  title={s.action}
                >
                  {s.action}
                  <ExternalLink className="h-3 w-3" />
                </a>
              </div>
            </div>
          ))}
        </div>
        <div className="mt-3 pt-3 border-t text-xs text-muted-foreground">
          Last updated: {new Date(hub.updatedAt).toLocaleString()}
       

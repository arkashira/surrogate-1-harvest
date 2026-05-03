# Costinel / frontend

## Final Synthesized Implementation Plan  
**Feature:** Read-only Top-hub Signal Panel (Costinel dashboard)  
**Goal:** Surface the most-connected hub and its actionable proposals in <2h, frontend-only, resilient, consistent with existing UI.

---

### 1) Scope & Constraints (merged + resolved)
- **Read-only** (Sense + Signal). No execution controls.
- **Frontend-only** (no backend changes).
- **Primary endpoint:** `/api/v1/sense/top-hub` (single call preferred for speed).  
  **Contingency:** If separate proposal endpoint is required for richer data, use `/api/v1/sense/hub/{id}/proposals` as secondary fetch (opt-in, only if primary response lacks proposals).
- **Placement:** Top-right of analytics area (or right-sidebar column on xl+ layouts).
- **UI kit:** Tailwind + shadcn/ui (Card, Badge, Skeleton, Alert, Drawer/Sheet).
- **Error handling:** Non-blocking fallbacks; never crash the dashboard.
- **Performance:** `cache: 'no-store'` but lightweight; avoid waterfall requests when possible.

---

### 2) Types (`src/types/sense.ts`)
```ts
export interface TopHubProposal {
  id: string;
  title: string;
  summary: string;
  signalStrength?: number;        // 0–100 (optional)
  impact?: 'high' | 'medium' | 'low';
  tags?: string[];
}

export interface TopHubResponse {
  hub: string;
  hubId?: string;                  // optional, for detail fetch
  insight: string;
  connectionCount?: number;
  updatedAt: string;               // ISO
  proposals: TopHubProposal[];
}
```

---

### 3) API Helpers (`src/lib/api/sense.ts`)
```ts
import { TopHubResponse, TopHubProposal } from '@/types/sense';

export async function fetchTopHub(): Promise<TopHubResponse | null> {
  try {
    const res = await fetch('/api/v1/sense/top-hub', {
      method: 'GET',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });

    if (!res.ok) {
      console.warn('Top-hub endpoint unavailable', res.status);
      return null;
    }

    const data = (await res.json()) as TopHubResponse;
    // Normalize: ensure proposals is an array
    if (!Array.isArray(data?.proposals)) {
      return { ...data, proposals: [] };
    }
    return data;
  } catch (err) {
    console.warn('Failed to fetch top-hub', err);
    return null;
  }
}

// Optional: fetch more proposal details if needed
export async function fetchHubProposals(hubId: string): Promise<TopHubProposal[] | null> {
  if (!hubId) return null;
  try {
    const res = await fetch(`/api/v1/sense/hub/${encodeURIComponent(hubId)}/proposals`, {
      cache: 'no-store',
    });
    if (!res.ok) return null;
    const json = await res.json();
    return Array.isArray(json) ? json : null;
  } catch {
    return null;
  }
}
```

---

### 4) Component (`src/components/dashboard/TopHubPanel.tsx`)
```tsx
'use client';

import { useEffect, useState } from 'react';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import {
  Drawer,
  DrawerContent,
  DrawerHeader,
  DrawerTitle,
  DrawerDescription,
} from '@/components/ui/drawer';
import { fetchTopHub, type TopHubResponse, type TopHubProposal } from '@/lib/api/sense';

function ProposalRow({ p, onClick }: { p: TopHubProposal; onClick: (p: TopHubProposal) => void }) {
  const impactColors = {
    high: 'bg-red-100 text-red-800',
    medium: 'bg-amber-100 text-amber-800',
    low: 'bg-emerald-100 text-emerald-800',
  } as const;

  const signalColor = p.signalStrength != null
    ? p.signalStrength >= 70
      ? 'text-red-600'
      : p.signalStrength >= 40
      ? 'text-amber-600'
      : 'text-emerald-600'
    : '';

  return (
    <div
      className="flex flex-col gap-1.5 py-2 border-b last:border-b-0 cursor-pointer hover:bg-muted/30 rounded-md p-2 -mx-2"
      onClick={() => onClick(p)}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="text-sm font-medium text-foreground line-clamp-2">{p.title}</span>
        <div className="flex items-center gap-1 shrink-0">
          {p.signalStrength != null && (
            <span className={`text-xs font-medium ${signalColor}`}>{p.signalStrength}</span>
          )}
          {p.impact && (
            <Badge variant="secondary" className={`${impactColors[p.impact]} text-xs`}>
              {p.impact}
            </Badge>
          )}
        </div>
      </div>
      <p className="text-xs text-muted-foreground line-clamp-2">{p.summary}</p>
      {p.tags && p.tags.length > 0 && (
        <div className="flex flex-wrap gap-1 pt-1">
          {p.tags.map((t) => (
            <Badge key={t} variant="outline" className="text-xs">
              {t}
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
}

export default function TopHubPanel() {
  const [data, setData] = useState<TopHubResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedProposal, setSelectedProposal] = useState<TopHubProposal | null>(null);

  useEffect(() => {
    let mounted = true;
    fetchTopHub().then((res) => {
      if (!mounted) return;
      setData(res);
      setLoading(false);
    });
    return () => {
      mounted = false;
    };
  }, []);

  const handleRetry = () => {
    setLoading(true);
    fetchTopHub().then((res) => {
      setData(res);
      setLoading(false);
    });
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base font-semibold">Top-hub Signal</CardTitle>
        <CardDescription className="text-xs">
          Most-connected hub and actionable proposals (Sense + Signal — read-only)
        </CardDescription>
      </CardHeader>
      <CardContent>
        {loading && (
          <div className="space-y-3">
            <Skeleton className="h-5 w-32" />
            <Skeleton className="h-4 w-40" />
            <div className="space-y-2">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          </div>
        )}

        {!loading && !data && (
          <Alert variant="default">
            <AlertDescription className="text-sm">
              Insights unavailable at the moment.
              <Button variant="link" size="sm" className="h-auto px-1" onClick={handleRetry}>
                Retry
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {!loading && data && (
          <div className="space-y-3">
            <div>
              <div className="flex items-baseline gap-2">
                <span className="text-lg font-semibold text-foreground">{data.hub}</span>

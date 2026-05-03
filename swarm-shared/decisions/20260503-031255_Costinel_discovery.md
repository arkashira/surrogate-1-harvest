# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, production-ready)

### Scope (single highest-value deliverable)
Add a **non-blocking Top-Hub Signal Panel** to the Costinel dashboard that:
- Surfaces the most-connected hub (default `MOC`) with 3 contextual insights.
- Uses **CDN-first** data fetching to bypass HF API rate limits.
- Fails gracefully (empty state, no dashboard breakage).
- Renders in <100ms, zero blocking of main dashboard paint.
- Caches locally to avoid thundering herd and repeat fetches.

### Architecture (merged best parts)
- **Data source**: `hubs/{hub}/latest.json` (canonical) with fields: `hub`, `updated_at`, `insights[]` (title, severity, description, action).
- **Delivery**: CDN via `https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/hubs/{hub}/latest.json` (no auth).
- **Caching**: `localStorage` with 5–10 min TTL (5 min chosen for freshness + cache efficiency).
- **Fallback**: Local static signals + optimistic UI; empty state if both unavailable.
- **Location**: Dashboard sidebar panel (SSR-safe, lazy-loaded, mobile responsive).

### File changes (3 files, ~150 lines total)
1. `src/lib/signals.ts` — CDN fetcher with TTL cache and fallback.
2. `src/components/TopHubSignalPanel.tsx` — React component with skeleton and error states.
3. `src/pages/Dashboard.tsx` — mount panel in sidebar.

Plus one-time scaffold and cron-friendly refresh script.

---

## Code Snippets

### 1) Signals scaffold (one-time)

```json
// /opt/axentx/Costinel/data/signals/hubs/MOC/latest.json
{
  "hub": "MOC",
  "updated_at": "2026-05-03T03:09:46Z",
  "insights": [
    {
      "id": "moc-001",
      "title": "Reserved Instance coverage gap",
      "severity": "high",
      "description": "Compute spend shows 62% on-demand vs 38% reserved; 14-day RI recommendation could reduce run-rate by ~18%.",
      "action": "Review RI recommendations in Costinel → Recommendations → Compute"
    },
    {
      "id": "moc-002",
      "title": "Orphaned storage volumes",
      "severity": "medium",
      "description": "Detected 23 unattached gp3 volumes (>30 days) totaling 4.2TB (~$420/mo).",
      "action": "Run cleanup proposal workflow"
    },
    {
      "id": "moc-003",
      "title": "Idle dev clusters nights/weekends",
      "severity": "low",
      "description": "Non-prod clusters show 65% idle CPU nights/weekends; schedule-based stop/start could save ~$1.1k/mo.",
      "action": "Apply governance policy template 'dev-off-hours'"
    }
  ],
  "links": {
    "dashboard": "https://huggingface.co/datasets/axentx/costinel-signals/tree/main/hubs/MOC",
    "raw": "https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/hubs/MOC/latest.json"
  }
}
```

### 2) CDN fetcher with TTL cache and fallback

```ts
// src/lib/signals.ts
const CDN_BASE = 'https://huggingface.co/datasets/axentx/costinel-signals/resolve/main';
const DEFAULT_HUB = 'MOC';
const TTL_MS = 5 * 60 * 1000; // 5 minutes

export interface SignalInsight {
  id: string;
  title: string;
  severity: 'low' | 'medium' | 'high';
  description: string;
  action: string;
}

export interface HubSignals {
  hub: string;
  updated_at: string;
  insights: SignalInsight[];
  links?: { dashboard?: string; raw?: string };
}

function getCacheKey(hub: string) {
  return `costinel:hub-signals:${hub}`;
}

function getCached(hub: string): HubSignals | null {
  try {
    const raw = localStorage.getItem(getCacheKey(hub));
    if (!raw) return null;
    const { data, ts } = JSON.parse(raw);
    if (Date.now() - ts > TTL_MS) return null;
    return data as HubSignals;
  } catch {
    return null;
  }
}

function setCached(hub: string, data: HubSignals) {
  try {
    localStorage.setItem(
      getCacheKey(hub),
      JSON.stringify({ data, ts: Date.now() })
    );
  } catch {
    // ignore storage quota errors
  }
}

export async function fetchHubSignals(hub = DEFAULT_HUB): Promise<HubSignals | null> {
  const cached = getCached(hub);
  if (cached) return cached;

  try {
    const res = await fetch(`${CDN_BASE}/hubs/${hub}/latest.json`, {
      cache: 'no-store',
      credentials: 'omit'
    });

    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    const data = (await res.json()) as HubSignals;
    setCached(hub, data);
    return data;
  } catch (err) {
    console.warn('[Costinel] CDN signals unavailable, using fallback', err);
    try {
      // Vite/ESBuild-friendly local import; adjust path if using different bundler
      const local = (await import(`../data/signals/hubs/${hub}/latest.json`)).default;
      if (local) {
        setCached(hub, local);
        return local;
      }
    } catch {
      // no local fallback
    }
    return null;
  }
}
```

### 3) TopHubSignalPanel component (non-blocking, skeleton, accessible)

```tsx
// src/components/TopHubSignalPanel.tsx
import React, { useEffect, useState } from 'react';
import { fetchHubSignals, HubSignals, SignalInsight } from '../lib/signals';
import './TopHubSignalPanel.css';

const severityColors = {
  high: '#ef4444',
  medium: '#f59e0b',
  low: '#10b981'
} as const;

function InsightItem({ insight }: { insight: SignalInsight }) {
  return (
    <div className="signal-insight" title={insight.description}>
      <div className="signal-header">
        <span
          className="signal-badge"
          style={{ backgroundColor: severityColors[insight.severity] }}
        >
          {insight.severity}
        </span>
        <span className="signal-title">{insight.title}</span>
      </div>
      <p className="signal-desc">{insight.description}</p>
      <a
        className="signal-action"
        href="#"
        onClick={(e) => {
          e.preventDefault();
          // Replace with real navigation if available
          alert(insight.action);
        }}
      >
        {insight.action} →
      </a>
    </div>
  );
}

function Skeleton() {
  return (
    <div className="signal-skeleton" aria-hidden="true">
      <div className="signal-skeleton-bar" style={{ width: '60%' }} />
      <div className="signal-skeleton-bar" style={{ width: '90%' }} />
      <div className="signal-skeleton-bar" style={{ width: '70%' }} />
    </div>
  );
}

export default function TopHubSignalPanel({ hub = 'MOC' }: { hub?: string }) {
  const [signals, setSignals] = useState<HubSignals | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchHubSignals(hub)
      .then((data) => {
        if (mounted)

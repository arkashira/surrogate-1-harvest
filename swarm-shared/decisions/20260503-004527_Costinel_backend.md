# Costinel / backend

## Final Implementation Plan — Costinel “Top-Hub Signal” Card (read-only, ≤2h)

**Scope & philosophy**  
- Pure-frontend, read-only “Sense + Signal” card.  
- Identifies the most-connected hub from the knowledge-rag graph and surfaces **hub summary + 3 contextual signals**.  
- Zero backend changes, zero runtime secrets, fits existing dashboard grid, supports light/dark.  
- Timeboxed to ≤2h (implementation + basic tests).

---

### 1) Highest-value design decisions (resolved)

| Decision | Rationale |
|---|---|
| **Data source**: static JSON at `public/knowledge-rag/top-hubs.json` (committed, CI-updated) | Guarantees ≤2h delivery, no backend, no rate-limits, works offline. Avoids unreliable runtime API dependency for v1. |
| **Card behavior**: client-side fetch with graceful fallback + `stale-while-revalidate` (`cache: 'no-store'` but render from cache if offline) | Balances freshness and resilience; users always see something useful. |
| **Rendering**: pick top hub by `centrality` (fallback to `connections`) | Correct centrality-first ranking; matches knowledge-graph semantics. |
| **Styling**: CSS variables + Tailwind-ready classes (no new runtime deps) | Fits existing dashboards; light/dark via `prefers-color-scheme` and CSS variables. |
| **Accessibility**: semantic `<article>`, aria labels, keyboard-friendly links, visible focus states | Non-negotiable for production dashboards. |
| **Type safety**: TypeScript interfaces for data shape | Prevents runtime surprises and aids future refactors. |

---

### 2) Static data contract

Create `public/knowledge-rag/top-hubs.json` (committed; CI job updates).

```json
{
  "generatedAt": "2026-05-03T04:00:00Z",
  "hubs": [
    {
      "id": "MOC",
      "label": "MOC",
      "description": "Multi-cloud observability and cost governance playbooks.",
      "connections": 42,
      "centrality": 0.92,
      "signals": [
        {
          "id": "sig-ri-coverage",
          "title": "RI coverage gap in us-east-1",
          "summary": "Current on-demand mix exceeds 38% for steady-state workloads; 12-month convertible RIs reduce run-rate by ~22%.",
          "severity": "high",
          "tags": ["cost", "ri", "us-east-1"],
          "ts": "2026-05-02T12:00:00Z"
        },
        {
          "id": "sig-ebs-snapshots",
          "title": "Orphaned EBS snapshot schedule",
          "summary": "73 unattached snapshots older than 30 days found; automated retention policy can recover ~$4.2k/mo.",
          "severity": "medium",
          "tags": ["storage", "snapshots"],
          "ts": "2026-05-02T11:30:00Z"
        },
        {
          "id": "sig-tag-drift",
          "title": "Tag compliance drift (prod)",
          "summary": "14% of prod-tagged resources missing mandatory cost-center tag; blocking chargeback reconciliation.",
          "severity": "high",
          "tags": ["governance", "tags"],
          "ts": "2026-05-02T10:15:00Z"
        }
      ]
    }
  ]
}
```

---

### 3) Component (TypeScript + CSS)

File: `src/components/costinel/TopHubSignalCard.tsx`

```tsx
import { useEffect, useState } from 'react';
import './TopHubSignalCard.css';

interface Signal {
  id: string;
  title: string;
  summary: string;
  severity: 'high' | 'medium' | 'low';
  tags: string[];
  ts: string;
}

interface Hub {
  id: string;
  label: string;
  description: string;
  connections: number;
  centrality: number;
  signals: Signal[];
  generatedAt?: string;
}

interface TopHubsPayload {
  generatedAt: string;
  hubs: Hub[];
}

const FALLBACK_HUB: Hub = {
  id: 'fallback',
  label: 'Loading...',
  description: 'Fetching top hub from knowledge graph.',
  connections: 0,
  centrality: 0,
  signals: [
    { id: 's1', title: '—', summary: 'No signals available.', severity: 'low', tags: [], ts: '' },
    { id: 's2', title: '—', summary: 'No signals available.', severity: 'low', tags: [], ts: '' },
    { id: 's3', title: '—', summary: 'No signals available.', severity: 'low', tags: [], ts: '' }
  ]
};

export default function TopHubSignalCard({ maxSignals = 3 }: { maxSignals?: number }) {
  const [hub, setHub] = useState<Hub | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    fetch('/knowledge-rag/top-hubs.json', { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<TopHubsPayload>;
      })
      .then((json) => {
        if (!mounted) return;
        const top = (json.hubs || []).sort(
          (a, b) => (b.centrality ?? b.connections ?? 0) - (a.centrality ?? a.connections ?? 0)
        )[0];
        setHub(top || null);
      })
      .catch((err) => {
        if (!mounted) return;
        setError(err.message);
        console.warn('[TopHubSignalCard] failed to load top-hubs.json', err);
      });
    return () => {
      mounted = false;
    };
  }, []);

  const resolved = hub || FALLBACK_HUB;
  const signals = (hub?.signals || resolved.signals || []).slice(0, maxSignals);

  return (
    <article className="top-hub-signal-card" data-testid="top-hub-signal-card">
      <header className="top-hub-signal-card__header">
        <div>
          <h3 className="top-hub-signal-card__title">{resolved.label}</h3>
          <p className="top-hub-signal-card__meta">Most-connected hub · {hub ? 'live' : 'cached'}</p>
        </div>
        {hub?.generatedAt && (
          <time className="top-hub-signal-card__time" dateTime={hub.generatedAt}>
            {new Date(hub.generatedAt).toLocaleString(undefined, {
              month: 'short',
              day: 'numeric',
              hour: '2-digit',
              minute: '2-digit'
            })}
          </time>
        )}
      </header>

      <p className="top-hub-signal-card__description">{resolved.description}</p>

      {error && (
        <div className="top-hub-signal-card__error" role="alert">
          Using fallback data — {error}
        </div>
      )}

      <ul className="top-hub-signal-card__signals" aria-label="Contextual signals">
        {signals.map((s, i) => (
          <li key={s.id || i} className="top-hub-signal-card__signal">
            <div className="top-hub-signal-card__signal-badge" aria-hidden="true">
              {i + 1}
            </div>
            <div className="top-hub-signal-card__signal-body">
              <div className="top-hub-signal-card__signal-title">
                <span>{s.title}</span>
              </div>
              <p className="top-hub-signal-card__signal-summary">{s.summary}</p>
              <div className="top-hub-signal-card__signal-m

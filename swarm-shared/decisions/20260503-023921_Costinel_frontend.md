# Costinel / frontend

## Final Synthesis — Highest-Value, Zero-API, CDN-First Top-Hub Signal Panel

**Goal**: Ship in ≤2h a read-only “Top-Hub” panel on the Costinel dashboard that surfaces the most-connected hub (default **MOC**) and its top 3 actionable cost-impact proposals — **zero Hugging Face API calls during render**, fully CDN-backed, quota-safe, and immediately actionable.

---

## Core Decisions (resolve contradictions)

- **CDN source**: use repo-committed `public/hubs/moc.json` (not external HuggingFace resolve URL) to eliminate auth, CORS, and external outage risk while keeping CDN caching via static hosting.  
- **Fetch strategy**: `cache: 'no-cache'` (or `max-age=900`) on the CDN JSON; **do not** use localStorage caching (stale data hurts cost signals).  
- **Render strategy**: client-only fetch (SSR pass-through) with robust loading/error fallbacks — never block page render.  
- **Data shape**: minimal, signal-focused (no `source`/`ts`), mirror dataset pattern; include `impact`, `signal`, `cta`, `title`, `id`, `updatedAt`.  
- **Actionability**: each proposal has a clear CTA button wired to existing flows (e.g., open modal, run job, navigate to RI planner) — not just “review”.

---

## Implementation Plan (≤2h)

1. **Create CDN-backed payload** (5–10m)  
   - File: `public/hubs/moc.json` (committed).  
   - Shape: `{ hub, updatedAt, proposals: [{ id, title, impact, signal, cta }] }`.

2. **Add loader module** (`src/lib/topHub.ts`) (10–15m)  
   - Exports `loadTopHub()` → fetches `/hubs/moc.json` (no auth).  
   - Timeout + typed parsing; throws on non-OK; returns `null` on failure.

3. **Create TopHubSignalPanel component** (45–60m)  
   - Location: `src/components/TopHubSignalPanel.tsx`.  
   - Client fetch via `useEffect`; skeleton + error + empty states.  
   - Impact badges (High/Medium/Low) with accessible colors.  
   - CTA buttons call optional callbacks (so dashboard can bind actions).

4. **Wire into dashboard** (20–30m)  
   - Mount near top of main pane or below primary cost summary.  
   - Map CTA ids to handlers (e.g., open RI planner, run cleanup job, schedule rightsizing).

5. **Polish & test** (15–20m)  
   - Verify CDN fetch in dev and production build; no auth headers.  
   - Lighthouse/network checks; graceful fallback when JSON missing/malformed.

6. **Commit & tag** (5m)  
   - Message: `feat: add CDN-backed Top-Hub signal panel (#knowledge-rag #hub #cdn)`.

---

## Code

### public/hubs/moc.json
```json
{
  "hub": "MOC",
  "updatedAt": "2026-05-03T02:40:00Z",
  "proposals": [
    {
      "id": "moc-ri-coverage",
      "title": "Increase Reserved Instance coverage for top 5 services",
      "impact": "High",
      "signal": "Detected 38% RI under-utilization in us-east-1; 22% potential savings available with 12-month convertible RIs.",
      "cta": "Review RI plan"
    },
    {
      "id": "moc-orphaned-ebs",
      "title": "Delete orphaned EBS volumes (>30 days unattached)",
      "impact": "Medium",
      "signal": "Found 14 unattached volumes totaling 2.1 TB (~$180/mo). Snapshot retention policy recommended before deletion.",
      "cta": "Run cleanup"
    },
    {
      "id": "moc-sagemaker-idle",
      "title": "Downsize idle SageMaker endpoints",
      "impact": "Medium",
      "signal": "Two endpoints averaging <5% CPU over 7 days; rightsizing to ml.t3.medium could save ~$310/mo.",
      "cta": "Schedule rightsizing"
    }
  ]
}
```

### src/lib/topHub.ts
```ts
const ENDPOINT = '/hubs/moc.json';

export interface Proposal {
  id: string;
  title: string;
  impact: 'High' | 'Medium' | 'Low';
  signal: string;
  cta: string;
}

export interface HubData {
  hub: string;
  updatedAt: string;
  proposals: Proposal[];
}

export async function loadTopHub(): Promise<HubData | null> {
  try {
    const res = await fetch(ENDPOINT, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`Failed to load hub data: ${res.status}`);
    const data = (await res.json()) as HubData;
    // Basic shape validation
    if (!data?.hub || !Array.isArray(data.proposals)) return null;
    return data;
  } catch {
    return null;
  }
}
```

### src/components/TopHubSignalPanel.tsx
```tsx
import React, { useEffect, useState } from 'react';
import { loadTopHub, HubData, Proposal } from '../lib/topHub';
import './TopHubSignalPanel.css';

interface Handlers {
  [id: string]: (p: Proposal) => void;
}

interface TopHubSignalPanelProps {
  handlers?: Handlers;
}

const impactColor = (impact: string) => {
  switch (impact) {
    case 'High':
      return 'var(--impact-high, #ef4444)';
    case 'Medium':
      return 'var(--impact-medium, #f59e0b)';
    default:
      return 'var(--impact-low, #10b981)';
  }
};

const ProposalCard: React.FC<{ p: Proposal; onAction?: (p: Proposal) => void }> = ({ p, onAction }) => (
  <div className="proposal-card" role="article">
    <div className="proposal-title">{p.title}</div>
    <div className="proposal-body">
      <span
        className="impact-badge"
        style={{ backgroundColor: `${impactColor(p.impact)}22`, color: impactColor(p.impact) }}
      >
        {p.impact}
      </span>
      <p className="signal">{p.signal}</p>
    </div>
    <div className="proposal-cta">
      <button
        type="button"
        className="cta-btn"
        onClick={() => (onAction ? onAction(p) : undefined)}
      >
        {p.cta}
      </button>
    </div>
  </div>
);

const TopHubSignalPanel: React.FC<TopHubSignalPanelProps> = ({ handlers }) => {
  const [hubData, setHubData] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    loadTopHub()
      .then((data) => {
        if (!mounted) return;
        if (!data) throw new Error('Invalid hub data');
        setHubData(data);
        setError(null);
      })
      .catch((err) => {
        if (!mounted) return;
        setError(err.message || 'Unable to load hub signals');
        setHubData(null);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel loading" aria-busy="true">
        <div className="skeleton-row" />
        <div className="skeleton-row short" />
        <div className="skeleton-row" />
      </div>
    );
  }

  if (error || !hubData || !hubData.proposals.length)

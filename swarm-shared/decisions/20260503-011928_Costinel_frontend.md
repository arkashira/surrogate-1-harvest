# Costinel / frontend

## Final Implementation Plan — Costinel Frontend: Top-Hub Signal Panel

**Estimated effort:** <2h  
**Scope:** Add a lightweight frontend panel that calls `/api/v1/sense/top-hub-signal`, renders the top-hub insight, and surfaces ranked, actionable proposals while strictly adhering to **Sense + Signal — ไม่ Execute**.

---

### What to ship (merged, concrete)
- A **panel** (and optional route `/sense/top-hub`) that:
  - Fetches `/api/v1/sense/top-hub-signal`
  - Displays the most-connected hub (ID, centrality score) and a concise insight
  - Lists ranked proposals with impact, confidence, and audit metadata
  - Provides **Acknowledge** (local UI state) and **Handoff** (opens handoff URL or audit URL) actions
  - *No execute actions* (no “Accept if it creates drafts or mutates proposals”)
- Minimal new state; optimistic UI only for local Acknowledge
- Graceful loading, error, and empty states
- Unit test for API helper + small README note on Sense+Signal

---

### Implementation steps (incremental, production-ready)

1. **Check current frontend structure** (≤10 min)
   - If React + Vite: add a route under `src/routes/` or embed as a card in the dashboard layout.
   - Prefer client-side fetch to avoid backend changes.

2. **Create API client helper** (`src/lib/api/sense.ts`)
   - Expose `fetchTopHubSignal(options?)`
   - Use `AbortSignal.timeout(10_000)` and exponential backoff on 429
   - Return normalized payload: `{ hub, proposals, generatedAt, requestId }`
   - Strongly typed with TypeScript interfaces

3. **Create UI component** (`src/components/TopHubSignalPanel.tsx`)
   - Shows:
     - Hub name/ID and centrality score
     - Short insight summary
     - Ranked proposals list with impact, confidence, audit trail link
   - Actions:
     - **Acknowledge** (local toggle)
     - **Handoff** (opens `proposal.handoffUrl` if present; otherwise `proposal.auditUrl`)
   - No execute/draft-creation actions

4. **Integrate into dashboard**
   - Add panel to main dashboard grid or expose as `/sense/top-hub` route
   - Reuse existing design tokens and cost-card styles

5. **Add tests & docs**
   - Unit test for `fetchTopHubSignal` (mock fetch)
   - Small README note explaining Sense+Signal philosophy and handoff behavior

---

### Code snippets (TypeScript-first, production-ready)

**Types and API helper** (`src/lib/api/sense.ts`)
```ts
export interface Hub {
  id: string;
  centrality?: number;
  insight?: string;
}

export interface Proposal {
  id: string;
  title: string;
  description?: string;
  impact?: string;
  confidence?: number;
  auditUrl?: string;
  handoffUrl?: string;
}

export interface TopHubSignalResponse {
  hub?: Hub;
  proposals: Proposal[];
  generatedAt: string;
  requestId?: string;
}

async function backoff(attempt: number) {
  return Math.min(1000 * 2 ** attempt + Math.random() * 200, 10000);
}

export async function fetchTopHubSignal(
  options: { retries?: number; timeoutMs?: number } = {}
): Promise<TopHubSignalResponse> {
  const { retries = 2, timeoutMs = 10_000 } = options;
  let lastError: Error | undefined;

  for (let attempt = 0; attempt <= retries; attempt++) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);

    try {
      const res = await fetch('/api/v1/sense/top-hub-signal', {
        method: 'GET',
        headers: { Accept: 'application/json' },
        signal: controller.signal,
        credentials: 'same-origin',
      });
      clearTimeout(timeout);

      if (res.status === 429 && attempt < retries) {
        await new Promise((r) => setTimeout(r, backoff(attempt)));
        continue;
      }

      if (!res.ok) {
        const err = new Error(`Top-hub signal failed: ${res.status}`);
        (err as any).status = res.status;
        throw err;
      }

      return res.json();
    } catch (err: any) {
      clearTimeout(timeout);
      lastError = err;
      if (attempt === retries || err.name === 'AbortError') break;
      await new Promise((r) => setTimeout(r, backoff(attempt)));
    }
  }

  throw lastError ?? new Error('Top-hub signal request failed');
}
```

**Panel component** (`src/components/TopHubSignalPanel.tsx`)
```tsx
import { useEffect, useState } from 'react';
import { fetchTopHubSignal, type TopHubSignalResponse } from '../lib/api/sense';

export default function TopHubSignalPanel() {
  const [data, setData] = useState<TopHubSignalResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [acknowledged, setAcknowledged] = useState<Record<string, boolean>>({});

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchTopHubSignal()
      .then((payload) => {
        if (mounted) setData(payload);
      })
      .catch((err) => {
        if (mounted) setError(err);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, []);

  const handleAcknowledge = (proposalId: string) => {
    setAcknowledged((s) => ({ ...s, [proposalId]: true }));
  };

  const handleHandoff = (proposal: TopHubSignalResponse['proposals'][0]) => {
    const url = proposal.handoffUrl || proposal.auditUrl;
    if (url) window.open(url, '_blank', 'noopener,noreferrer');
  };

  if (loading) return <div className="cost-card">Loading top-hub signal…</div>;
  if (error) return <div className="cost-card error">Unable to load signal: {error.message}</div>;
  if (!data || !data.proposals?.length) return <div className="cost-card">No active signals.</div>;

  const { hub, proposals, generatedAt } = data;

  return (
    <div className="cost-card">
      <header className="panel-header">
        <h3>Top-hub signal</h3>
        <small className="muted">
          {hub?.id ? `Hub: ${hub.id} (score: ${(hub.centrality ?? 0).toFixed(2)})` : '—'}
          <br />
          Updated {new Date(generatedAt).toLocaleString()}
        </small>
      </header>

      <section className="insight" aria-label="Insight">
        <p>{hub?.insight || 'No insight available.'}</p>
      </section>

      <section className="proposals" aria-label="Actionable proposals">
        <h4>Actionable proposals</h4>
        <ol>
          {proposals.map((p) => (
            <li key={p.id} className="proposal-item">
              <div className="proposal-meta">
                <strong>{p.title}</strong>
                <span className="badge">Impact: {p.impact || '—'}</span>
                <span className="badge">Confidence: {p.confidence != null ? `${Math.round(p.confidence * 100)}%` : '—'}</span>
              </div>
              <p className="proposal-desc">{p.description}</p>
              <div className="proposal-actions">
                <button
                  className="btn small"
                  onClick={() => handleAcknowledge(p.id)}
                  disabled={!!acknowledged[p.id]}
                  aria-pressed={!!acknowledged[p.id]}
                >
                  {acknowledged[p.id] ? 'Acknowledged

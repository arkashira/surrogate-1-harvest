# Costinel / quality

## Implementation Plan — Costinel Quality: Top-Hub Signal Card (Read-Only)

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no self-execution)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with score, summary, and actionable signals.

### Why this now
- Past pattern: top-hub doc insight (2026-04-27) — always review the most-connected hub before planning.
- Fits Costinel philosophy: sense (detect hub centrality) + signal (surface to user) without execution.
- Frontend-only, zero backend changes → safe to ship in <2h.

---

### Implementation Steps (concrete)

1. **Add mock data module** (`src/data/topHub.ts`)  
   - Exports a deterministic top-hub object (name, score, summary, signals, relatedLinks).
   - Uses a stable shape so UI can render immediately; later swapped to live graph query.

2. **Create card component** (`src/components/TopHubSignalCard.tsx`)  
   - Read-only card with:
     - Hub name + centrality score (0–100)
     - One-sentence summary
     - Bulleted actionable signals (max 3)
     - Related links (internal docs / dashboards)
   - No forms, no buttons that trigger writes, no polling.

3. **Add to dashboard layout** (`src/pages/Dashboard.tsx`)  
   - Insert card in the top-row “Signals” section (left-to-right priority).
   - Mobile responsive, accessible labels.

4. **Styling & QA**  
   - Use existing design tokens (colors, spacing).
   - Ensure color contrast ≥ 4.5:1.
   - Add `aria-label` for screen readers.

5. **Verify “no execute”**  
   - No `fetch`/`POST` in component.
   - No `useEffect` that mutates state from API.
   - No callbacks that call backend endpoints.

---

### Code Snippets

#### `src/data/topHub.ts`
```ts
export interface Signal {
  label: string;
  severity: 'critical' | 'high' | 'medium' | 'low';
}

export interface RelatedLink {
  label: string;
  href: string;
}

export interface TopHub {
  name: string;
  score: number; // 0-100 centrality score
  summary: string;
  signals: Signal[];
  relatedLinks: RelatedLink[];
}

// Deterministic top-hub (replace with live graph query later)
export const topHub: TopHub = {
  name: 'MOC',
  score: 92,
  summary:
    'Mission Operations Center is the most-connected hub; changes here propagate to cost policies, runbooks, and compliance checks.',
  signals: [
    { label: 'High cross-account IAM role usage', severity: 'high' },
    { label: 'Unoptimized reserved instance coverage (68%)', severity: 'medium' },
    { label: 'Anomalous data-transfer spikes in us-east-1', severity: 'critical' },
  ],
  relatedLinks: [
    { label: 'MOC Runbook', href: '/docs/moc-runbook' },
    { label: 'Cost Policy Dashboard', href: '/dashboards/cost-policy' },
  ],
};
```

#### `src/components/TopHubSignalCard.tsx`
```tsx
import React from 'react';
import { TopHub } from '../data/topHub';

const severityColors = {
  critical: 'text-red-700 bg-red-50 border-red-200',
  high: 'text-orange-700 bg-orange-50 border-orange-200',
  medium: 'text-yellow-700 bg-yellow-50 border-yellow-200',
  low: 'text-blue-700 bg-blue-50 border-blue-200',
} as const;

interface Props {
  hub: TopHub;
}

export const TopHubSignalCard: React.FC<Props> = ({ hub }) => {
  return (
    <section
      className="rounded-lg border bg-white p-5 shadow-sm"
      aria-label={`Top hub: ${hub.name}`}
    >
      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="text-xl font-semibold text-gray-900">{hub.name}</h2>
        <span className="rounded-full bg-indigo-100 px-3 py-1 text-sm font-medium text-indigo-800">
          Score {hub.score}
        </span>
      </div>

      <p className="mb-4 text-sm text-gray-600">{hub.summary}</p>

      <ul className="mb-4 space-y-2" aria-label="Actionable signals">
        {hub.signals.map((signal, idx) => (
          <li
            key={idx}
            className={`flex items-center gap-2 rounded border px-3 py-2 text-sm ${severityColors[signal.severity]}`}
          >
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{
                backgroundColor:
                  signal.severity === 'critical'
                    ? '#ef4444'
                    : signal.severity === 'high'
                    ? '#f97316'
                    : signal.severity === 'medium'
                    ? '#eab308'
                    : '#3b82f6',
              }}
              aria-hidden="true"
            />
            {signal.label}
          </li>
        ))}
      </ul>

      <nav aria-label="Related links">
        <ul className="flex flex-wrap gap-3 text-sm">
          {hub.relatedLinks.map((link, idx) => (
            <li key={idx}>
              <a
                href={link.href}
                className="text-indigo-600 hover:underline focus:outline-none focus:ring-2 focus:ring-indigo-300"
                target="_blank"
                rel="noopener noreferrer"
              >
                {link.label}
              </a>
            </li>
          ))}
        </ul>
      </nav>
    </section>
  );
};
```

#### `src/pages/Dashboard.tsx` (insert)
```tsx
import { TopHubSignalCard } from '../components/TopHubSignalCard';
import { topHub } from '../data/topHub';

// Inside your dashboard layout, add:
<TopHubSignalCard hub={topHub} />
```

---

### Acceptance Criteria (read-only)
- [x] Card renders hub name, score, summary, signals, and related links.
- [x] No network requests initiated by the card.
- [x] No UI controls that trigger writes or backend mutations.
- [x] Accessible (ARIA labels, keyboard-navigable links).
- [x] Responsive and styled with existing tokens.

### Next steps (post-ship)
- Replace mock `topHub` with live graph query (knowledge-rag) behind a read-only API endpoint.
- Add timestamp of last graph sync (read-only metadata).

Estimated effort: ~1.5h (implementation + QA).

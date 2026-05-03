# Costinel / discovery

## 2-Hour Implementation Plan — Costinel “Top-Hub Signal” Card

### Scope (frontend-only, read-only, ≤2h)
- Add a single reusable card component that surfaces the most-connected hub (e.g., "MOC") and its contextual insights.
- Uses existing data shape (no new APIs, no auth, no infra).
- Graceful fallback when data is missing.
- Lightweight, copy-paste-ready for dashboard/recommendations pages.

---

### File changes
- `src/components/costinel/TopHubSignalCard.tsx` (new)
- `src/components/costinel/index.ts` (export addition)
- Optional: add to an existing page (e.g., `src/pages/CostDashboard.tsx` or similar) as a single import to verify.

---

### Implementation

#### 1) Component: TopHubSignalCard.tsx
```tsx
// src/components/costinel/TopHubSignalCard.tsx
import React from 'react';
import { TrendingUp, AlertCircle, Info } from 'lucide-react';

export interface HubInsight {
  hub: string;
  label?: string;
  score?: number;
  signals: Array<{
    id: string;
    title: string;
    description?: string;
    severity?: 'low' | 'medium' | 'high';
    action?: string;
  }>;
  lastUpdated?: string;
}

export interface TopHubSignalCardProps {
  topHub?: HubInsight | null;
  loading?: boolean;
  emptyMessage?: string;
  className?: string;
}

const severityColor = (s: string | undefined) => {
  switch (s) {
    case 'high':
      return 'text-red-600 bg-red-50 border-red-200';
    case 'medium':
      return 'text-amber-600 bg-amber-50 border-amber-200';
    default:
      return 'text-blue-600 bg-blue-50 border-blue-200';
  }
};

const formatDate = (iso?: string) => {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
};

export const TopHubSignalCard: React.FC<TopHubSignalCardProps> = ({
  topHub,
  loading = false,
  emptyMessage = 'No top hub insights available.',
  className = '',
}) => {
  if (loading) {
    return (
      <div className={`rounded-xl border border-gray-200 bg-white p-5 shadow-sm animate-pulse ${className}`}>
        <div className="flex items-center gap-3">
          <div className="h-10 w-10 rounded-lg bg-gray-200" />
          <div className="h-5 w-32 rounded bg-gray-200" />
        </div>
        <div className="mt-4 space-y-3">
          <div className="h-4 w-full rounded bg-gray-100" />
          <div className="h-4 w-5/6 rounded bg-gray-100" />
        </div>
      </div>
    );
  }

  if (!topHub) {
    return (
      <div className={`rounded-xl border border-gray-200 bg-white p-5 shadow-sm ${className}`}>
        <div className="flex items-center gap-2 text-gray-500">
          <Info className="h-4 w-4" />
          <span className="text-sm">{emptyMessage}</span>
        </div>
      </div>
    );
  }

  const hubLabel = topHub.label || topHub.hub;
  const score = typeof topHub.score === 'number' ? topHub.score : undefined;

  return (
    <div className={`rounded-xl border border-gray-200 bg-white p-5 shadow-sm ${className}`}>
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
            <TrendingUp className="h-5 w-5" />
          </div>
          <div>
            <h3 className="font-semibold text-gray-900">Top Hub Signal</h3>
            <p className="text-sm font-medium text-indigo-600">{hubLabel}</p>
          </div>
        </div>
        {score != null && (
          <div className="shrink-0 rounded-full bg-indigo-100 px-2.5 py-1 text-xs font-medium text-indigo-700">
            Score {score.toFixed(2)}
          </div>
        )}
      </div>

      {/* Signals */}
      {topHub.signals && topHub.signals.length > 0 ? (
        <div className="mt-4 space-y-3">
          {topHub.signals.map((s) => (
            <div
              key={s.id}
              className={`rounded-lg border px-3 py-2.5 ${severityColor(s.severity)}`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex items-center gap-2">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 opacity-70" />
                  <p className="text-sm font-medium text-gray-900">{s.title}</p>
                </div>
                {s.severity && (
                  <span className="shrink-0 text-xs capitalize opacity-70">{s.severity}</span>
                )}
              </div>
              {s.description && (
                <p className="mt-1 pl-6 text-xs text-gray-700">{s.description}</p>
              )}
              {s.action && (
                <p className="mt-2 pl-6">
                  <a
                    href={s.action}
                    className="text-xs font-medium text-indigo-600 hover:underline"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    View details →
                  </a>
                </p>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="mt-3 flex items-center gap-2 text-sm text-gray-500">
          <Info className="h-4 w-4" />
          <span>No signals available for this hub.</span>
        </div>
      )}

      {/* Footer */}
      {topHub.lastUpdated && (
        <div className="mt-4 flex items-center justify-end gap-1 text-xs text-gray-400">
          Updated {formatDate(topHub.lastUpdated)}
        </div>
      )}
    </div>
  );
};
```

#### 2) Export from components index
```ts
// src/components/costinel/index.ts
export { TopHubSignalCard, type HubInsight } from './TopHubSignalCard';
```

#### 3) Example usage (drop into any page)
```tsx
// Example snippet to paste into a dashboard page (e.g., CostDashboard)
import { TopHubSignalCard } from '@/components/costinel';

// Mock data shape — replace with real selector/state when available
const mockTopHub = {
  hub: 'MOC',
  label: 'MOC (Mission Operations Center)',
  score: 0.87,
  signals: [
    {
      id: 's1',
      title: 'Unattached EBS volumes detected',
      description: '3 unattached volumes across us-east-1 totaling 150 GB potential savings.',
      severity: 'medium',
      action: '/costinel/proposals/ebs-cleanup',
    },
    {
      id: 's2',
      title: 'Low RI coverage for RDS',
      description: 'Only 42% coverage for production RDS instances; consider 1-year convertible RIs.',
      severity: 'high',
    },
  ],
  lastUpdated: '2026-05-03T0

# Costinel / frontend

## Final Synthesis — Top-Hub Signal Card (frontend-only)

**Chosen approach**  
- Pure React + TypeScript, no backend/auth changes.  
- Static JSON contract (`top-hub.json`) lives in `public/data/knowledge-rag/` (served as static asset).  
- Dashboard card shows: most-connected hub, short insight, related docs with scores, and a “Refresh from RAG” action.  
- Graceful fallback to built-in MOC defaults so the card always renders.  
- Follows existing design tokens and project patterns.

**Estimated effort**: 90–120 min (including tests/polish).  
**Risk**: Low (read-only, no infra/auth changes).

---

### 1) Type contract (single source of truth)

`src/types/knowledge-rag.ts`

```ts
export interface RelatedDoc {
  title: string;
  slug: string;
  summary: string;
  url?: string;
  score: number; // 0–1 connection strength
}

export interface TopHub {
  slug: string;
  title: string;
  description?: string;
}

export interface TopHubInsight {
  hub: TopHub;
  related: RelatedDoc[];
  generatedAt?: string; // ISO timestamp
  insight?: string;     // short human summary
}
```

---

### 2) Static data + fallback

`public/data/knowledge-rag/top-hub.json` (example)

```json
{
  "hub": {
    "slug": "MOC",
    "title": "MOC",
    "description": "Most-connected operational hub"
  },
  "related": [
    {
      "title": "Cost Governance Playbook",
      "slug": "cost-governance",
      "summary": "Policies, guardrails, and ownership for cloud cost control.",
      "score": 0.92
    },
    {
      "title": "Multi-cloud Tagging Strategy",
      "slug": "tagging-strategy",
      "summary": "Standard tags, enforcement, and allocation workflows.",
      "score": 0.87
    },
    {
      "title": "Reserved Instance Optimization",
      "slug": "ri-optimization",
      "summary": "Purchase planning, utilization tracking, and swap guidance.",
      "score": 0.81
    }
  ],
  "insight": "MOC drives the strongest cross-domain signals; prioritize governance and tagging to amplify savings.",
  "generatedAt": "2025-11-01T12:00:00.000Z"
}
```

Built-in fallback is embedded in the hook (see below) so the UI never blanks.

---

### 3) Hook: load, refresh, poll

`src/hooks/useTopHubInsight.ts`

```ts
import { useEffect, useState, useCallback } from 'react';
import { TopHubInsight, RelatedDoc } from '../types/knowledge-rag';

const DATA_URL = '/data/knowledge-rag/top-hub.json';
const FALLBACK: TopHubInsight = {
  hub: {
    slug: 'MOC',
    title: 'MOC',
    description: 'Most-connected operational hub (fallback)',
  },
  related: [
    {
      title: 'Cost Governance Playbook',
      slug: 'cost-governance',
      summary: 'Policies and ownership for cloud cost control.',
      score: 0.92,
    },
    {
      title: 'Multi-cloud Tagging Strategy',
      slug: 'tagging-strategy',
      summary: 'Standard tags and allocation workflows.',
      score: 0.87,
    },
    {
      title: 'Reserved Instance Optimization',
      slug: 'ri-optimization',
      summary: 'Purchase planning and utilization tracking.',
      score: 0.81,
    },
  ],
  insight: 'Prioritize governance and tagging to amplify savings.',
};

export function useTopHubInsight(pollIntervalMs = 0) {
  const [insight, setInsight] = useState<TopHubInsight>(FALLBACK);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const fetchInsight = useCallback(async () => {
    try {
      setLoading(true);
      const res = await fetch(DATA_URL, { cache: 'no-store' });
      if (!res.ok) throw new Error(`Failed to load top-hub data: ${res.status}`);
      const json = (await res.json()) as TopHubInsight;
      setInsight(json);
      setError(null);
    } catch (err: any) {
      console.warn(err);
      setError(err?.message || 'Unknown error');
      setInsight(FALLBACK);
    } finally {
      setLoading(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    // Best-effort: try to trigger RAG refresh, then always reload static file.
    try {
      await fetch('/api/refresh-knowledge-rag', { method: 'POST', keepalive: true }).catch(() => {
        /* ignore if endpoint missing */
      });
    } finally {
      await fetchInsight();
    }
  }, [fetchInsight]);

  useEffect(() => {
    fetchInsight();
    if (pollIntervalMs > 0) {
      const id = setInterval(fetchInsight, pollIntervalMs);
      return () => clearInterval(id);
    }
  }, [fetchInsight, pollIntervalMs]);

  return { insight, loading, error, refresh };
}
```

Notes:
- `cache: 'no-store'` avoids stale reads.
- Refresh is non-blocking and best-effort (works even if `/api/refresh-knowledge-rag` is absent).

---

### 4) Card component

`src/components/TopHubInsightCard.tsx`

```tsx
import React from 'react';
import { useTopHubInsight } from '../hooks/useTopHubInsight';
import { TopHubInsight, RelatedDoc } from '../types/knowledge-rag';

function RelatedItem({ doc }: { doc: RelatedDoc }) {
  return (
    <a
      href={doc.url || `#/${doc.slug}`}
      className="flex items-start gap-3 p-2 rounded hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
      target={doc.url ? '_blank' : undefined}
      rel={doc.url ? 'noopener noreferrer' : undefined}
    >
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium text-sm text-gray-900 dark:text-gray-100 truncate">
            {doc.title}
          </span>
          <span className="flex-shrink-0 text-xs px-1.5 py-0.5 rounded bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300">
            {(doc.score * 100).toFixed(0)}%
          </span>
        </div>
        {doc.summary && (
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-2">
            {doc.summary}
          </p>
        )}
      </div>
    </a>
  );
}

export default function TopHubInsightCard() {
  const { insight, loading, error, refresh } = useTopHubInsight();

  return (
    <div className="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-xl p-5 shadow-sm">
      <div className="flex items-start justify-between mb-4">
        <div>
          <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">
            Top Hub Insight
          </h3>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Most-connected hub and related signals
          </p>
        </div>
        <button
          onClick={refresh}
          className="text-xs px-2.5 py-1 rounded border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-3

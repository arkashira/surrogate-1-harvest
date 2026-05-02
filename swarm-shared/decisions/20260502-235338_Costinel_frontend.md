# Costinel / frontend

## Final Implementation Plan — Costinel Frontend: Top-Hub Signal Card

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no self-execution)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with score, summary, related count, and a drill-down link to the knowledge-rag graph.

---

### 1) Design Decisions (resolved)

- **Static-first with live fetch**: Start from static JSON for immediate delivery; wire to `GET /api/knowledge-rag/top-hub` (read-only) for production.  
- **No mutations**: Component never POST/PUT/DELETE and performs no local state changes that trigger side-effects.  
- **Accessibility**: Use `role="status"` for the score region; ensure keyboard-focusable link with visible focus ring.  
- **Visual**: Reuse existing Costinel card tokens; add a compact score ring (0–100 scale) for quick scanning.

---

### 2) API Contract (backend, read-only)

`GET /api/knowledge-rag/top-hub`

Success (200):
```json
{
  "slug": "MOC",
  "label": "Mission Operations Center",
  "score": 0.92,
  "summary": "Highest betweenness centrality; cross-cloud cost governance hub.",
  "relatedCount": 128,
  "graphUrl": "/knowledge-rag/graph?hub=MOC"
}
```

Errors:
- 404 → render empty/minimal state (no card or collapsed placeholder).  
- 5xx → show non-blocking toast; do not auto-retry.

---

### 3) Frontend Implementation

#### File: `src/data/topHub.json` (static fallback)
```json
{
  "slug": "MOC",
  "label": "Mission Operations Center",
  "score": 0.947,
  "summary": "Highest connectivity across cost governance policies, anomaly pipelines, and audit trails. Central signal router for multi-cloud recommendations.",
  "relatedCount": 128,
  "graphUrl": "/knowledge-rag/graph?hub=MOC",
  "lastUpdated": "2026-05-02T20:14:00Z"
}
```

#### File: `src/components/cards/TopHubSignalCard.tsx`
```tsx
'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import { ExternalLink } from 'lucide-react';

interface TopHub {
  slug: string;
  label: string;
  score: number; // 0..1
  summary: string;
  relatedCount: number;
  graphUrl: string;
  lastUpdated?: string;
}

function ScoreRing({ score }: { score: number }) {
  const radius = 40;
  const circumference = 2 * Math.PI * radius;
  const pct = Math.max(0, Math.min(1, score));
  const offset = circumference * (1 - pct);
  const displayScore = Math.round(pct * 100);

  return (
    <div className="relative inline-block">
      <svg width={96} height={96} viewBox="0 0 96 96" aria-hidden="true">
        <circle
          cx={48}
          cy={48}
          r={radius}
          fill="none"
          stroke="#e5e7eb"
          strokeWidth={8}
        />
        <circle
          cx={48}
          cy={48}
          r={radius}
          fill="none"
          stroke="#2563eb"
          strokeWidth={8}
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          transform="rotate(-90 48 48)"
        />
      </svg>
      <div className="absolute inset-0 flex items-center justify-center">
        <span className="text-lg font-semibold text-gray-900 dark:text-gray-100">
          {displayScore}
        </span>
      </div>
    </div>
  );
}

interface TopHubSignalCardProps {
  fallbackHub?: TopHub;
}

export function TopHubSignalCard({ fallbackHub }: TopHubSignalCardProps) {
  const [hub, setHub] = useState<TopHub | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetch('/api/knowledge-rag/top-hub', { method: 'GET' })
      .then((res) => {
        if (!res.ok) throw new Error(res.status === 404 ? 'not_found' : 'server_error');
        return res.json();
      })
      .then((data) => {
        if (mounted) setHub(data);
      })
      .catch(() => {
        if (mounted) setHub(fallbackHub || null);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, [fallbackHub]);

  if (loading) {
    return (
      <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-5 shadow-sm">
        <div className="h-5 w-32 bg-gray-200 dark:bg-gray-700 rounded animate-pulse" />
        <div className="mt-4 flex items-start gap-4">
          <div className="w-24 h-24 rounded-full bg-gray-200 dark:bg-gray-700 animate-pulse" />
          <div className="flex-1 space-y-2">
            <div className="h-4 w-20 bg-gray-200 dark:bg-gray-700 rounded animate-pulse" />
            <div className="h-4 w-full bg-gray-200 dark:bg-gray-700 rounded animate-pulse" />
            <div className="h-4 w-3/4 bg-gray-200 dark:bg-gray-700 rounded animate-pulse" />
          </div>
        </div>
      </div>
    );
  }

  if (!hub) {
    return null;
  }

  return (
    <article
      className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-5 shadow-sm hover:shadow-md transition-shadow"
      role="region"
      aria-label={`Top hub: ${hub.label}`}
    >
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide">
            Top Hub Signal
          </p>
          <h3 className="mt-1 text-lg font-semibold text-gray-900 dark:text-gray-100">
            {hub.label}
          </h3>
          <p className="text-sm text-gray-500 dark:text-gray-400">{hub.slug}</p>
        </div>
        <div className="flex items-center gap-2">
          <ScoreRing score={hub.score} />
          <Link
            href={hub.graphUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-gray-400 hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded"
            aria-label={`View ${hub.slug} in knowledge graph`}
          >
            <ExternalLink className="w-4 h-4" />
          </Link>
        </div>
      </div>

      <p className="mt-3 text-sm text-gray-600 dark:text-gray-300 leading-relaxed">
        {hub.summary}
      </p>

      <div className="mt-4 flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400">
        <span>Related: {hub.relatedCount}</

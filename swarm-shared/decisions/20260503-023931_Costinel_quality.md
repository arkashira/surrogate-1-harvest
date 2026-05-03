# Costinel / quality

### Final Synthesis (Single, Correct, Actionable)

**Goal:** Ship a Top-Hub Signal Panel in ≤2 hours that surfaces the most-connected hub (default “MOC”) and its top 3 cost-impact proposals, with **zero runtime Hugging Face API calls**, CDN-first delivery, and full accessibility.

---

## 1) Data layer (15 min)
- Create **`public/data/top-hub-signals.json`** (≤4 KB) with CDN-friendly schema:
  - `hubId`, `hubLabel`, `hubSlug`, `updatedAt`
  - `signals[]`: `id`, `title`, `impactUSD`, `confidence`, `actionUrl`, `expiresAt`
- **Cache headers:** `Cache-Control: public, max-age=3600, stale-while-revalidate=86400`
- **No HF API at runtime:** use static file; if/when syncing from HF, use public `resolve/main/...` URLs without Authorization headers.

```json
{
  "hubId": "MOC",
  "hubLabel": "Mission Operations Center",
  "hubSlug": "mission-operations-center",
  "updatedAt": "2026-05-03T03:00:00.000Z",
  "signals": [
    {
      "id": "SIG-2026-05-03-001",
      "title": "Idle RDS Aurora clusters in us-east-1",
      "impactUSD": 12400,
      "confidence": 0.92,
      "actionUrl": "/proposals/new?hub=MOC&signal=SIG-2026-05-03-001",
      "expiresAt": "2026-05-10T03:00:00.000Z"
    },
    {
      "id": "SIG-2026-05-03-002",
      "title": "Unattached EBS volumes (>100GB) across prod accounts",
      "impactUSD": 8700,
      "confidence": 0.88,
      "actionUrl": "/proposals/new?hub=MOC&signal=SIG-2026-05-03-002",
      "expiresAt": "2026-05-10T03:00:00.000Z"
    },
    {
      "id": "SIG-2026-05-03-003",
      "title": "Over-provisioned GKE node pools (CPU <30%)",
      "impactUSD": 6100,
      "confidence": 0.85,
      "actionUrl": "/proposals/new?hub=MOC&signal=SIG-2026-05-03-003",
      "expiresAt": "2026-05-10T03:00:00.000Z"
    }
  ]
}
```

---

## 2) UI component (45 min)
- **File:** `components/TopHubSignalPanel.tsx`
- **SSR-safe:** accepts `initialData` for prerendering via `getStaticProps`.
- **Client-side:** lightweight `useSWR` revalidation against CDN path (no auth, no HF API).
- **Accessibility:** semantic `<section aria-labelledby="top-hub-signals">`, keyboard nav, visible focus ring.
- **Visuals:** compact cards with impact badge (color by USD), confidence meter, “Review” CTA.

```tsx
'use client';

import useSWR from 'swr';
import Link from 'next/link';
import { ExternalLink } from 'lucide-react';

type Signal = {
  id: string;
  title: string;
  impactUSD: number;
  confidence: number;
  actionUrl: string;
  expiresAt: string;
};

type TopHubPayload = {
  hubId: string;
  hubLabel: string;
  hubSlug: string;
  updatedAt: string;
  signals: Signal[];
};

const fetcher = (url: string) => fetch(url).then((r) => r.json());

export default function TopHubSignalPanel({
  initialData,
}: {
  initialData: TopHubPayload;
}) {
  const { data } = useSWR<TopHubPayload>('/data/top-hub-signals.json', fetcher, {
    fallbackData: initialData,
    revalidateOnMount: true,
    refreshInterval: 3_600_000,
  });

  const hub = data || initialData;
  const top3 = hub.signals.slice(0, 3);

  return (
    <section
      aria-labelledby="top-hub-signals"
      className="mb-6 rounded-lg border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800"
    >
      <div className="mb-3 flex items-center justify-between">
        <h2
          id="top-hub-signals"
          className="text-base font-semibold text-gray-900 dark:text-gray-100"
        >
          Top-Hub: {hub.hubLabel}
        </h2>
        <span className="text-xs text-gray-500 dark:text-gray-400">
          Updated {new Date(hub.updatedAt).toLocaleDateString()}
        </span>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        {top3.map((s) => (
          <article
            key={s.id}
            className="rounded-md border border-gray-100 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900"
          >
            <h3 className="mb-2 line-clamp-2 text-sm font-medium text-gray-900 dark:text-gray-100">
              {s.title}
            </h3>
            <div className="flex items-center justify-between">
              <span
                className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-semibold ${
                  s.impactUSD >= 10000
                    ? 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400'
                    : s.impactUSD >= 5000
                    ? 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400'
                    : 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-400'
                }`}
              >
                ${s.impactUSD.toLocaleString()}
              </span>
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {Math.round(s.confidence * 100)}%
              </span>
            </div>
            <Link
              href={s.actionUrl}
              className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-blue-600 hover:underline dark:text-blue-400"
            >
              Review <ExternalLink className="h-3 w-3" />
            </Link>
          </article>
        ))}
      </div>
    </section>
  );
}
```

---

## 3) Route integration (15 min)
- Mount panel on `/dashboard` above the cost heatmap.
- Fetch JSON at build time via `getStaticProps` with ISR (`revalidate: 3600`) so CDN updates propagate without touching HF API.

```tsx
// pages/dashboard.tsx
import { GetStaticProps } from 'next';
import TopHubSignalPanel from '@/components/TopHubSignalPanel';
import path from 'path';
import fs from 'fs';

type TopHubPayload = {
  hubId: string;
  hubLabel: string;
  hubSlug: string;
  updatedAt: string;
  signals: Array<{
    id: string;
    title: string;
    impactUSD: number;
    confidence: number;
    actionUrl: string;
    expiresAt: string;
  }>;
};


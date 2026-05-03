# Costinel / quality

## Highest-Value Incremental Improvement (<2h)

**Add a Top-Hub Signal Panel** to the Costinel dashboard that surfaces the most-connected hub (default “MOC”) and its top 3 actionable, cost-impact proposals — **zero Hugging Face API calls during render**, fully CDN-backed, and precomputed at build/ingest time.

---

## Implementation Plan (≤2h)

1. **Data contract** (10m)  
   - Create `public/signals/top-hub.json` (committed by ingestion pipeline) with shape:
     ```json
     {
       "hub": "MOC",
       "updated": "2026-05-03T02:37:30Z",
       "proposals": [
         {
           "id": "ri-cpu-001",
           "title": "RI Coverage Gap — m5.large",
           "impactUSD": 12400,
           "confidence": 0.87,
           "action": "Purchase 1yr No Upfront",
           "detail": "37% RI coverage; 2100hrs/mo idle"
         },
         {
           "id": "snap-002",
           "title": "Orphaned EBS Snapshots",
           "impactUSD": 5600,
           "confidence": 0.92,
           "action": "Delete >30d unattached",
           "detail": "14 snapshots; 1.2TB"
         },
         {
           "id": "util-003",
           "title": "Underutilized r5.xlarge",
           "impactUSD": 8900,
           "confidence": 0.81,
           "action": "Downsize to r5.large",
           "detail": "avg CPU 18%; mem 22%"
         }
       ]
     }
     ```

2. **Static asset + CDN path** (5m)  
   - File served at:  
     `https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/top-hub.json`  
   - In repo: `public/signals/top-hub.json` (committed by ingestion).

3. **UI component** (45m)  
   - Create `components/TopHubSignalPanel.tsx` (Next.js/React).  
   - Fetch at **build time** (ISR) or client-side with `useSWR` + 300s revalidate — **no HF API auth**.  
   - CDN-only fetch:
     ```ts
     const { data, error } = useSWR(
       '/signals/top-hub.json',
       (key) => fetch(key).then((r) => r.json()),
       { refreshInterval: 300_000, revalidateOnFocus: false }
     );
     ```

4. **Embed in dashboard** (20m)  
   - Add panel to `app/dashboard/page.tsx` (or equivalent) near cost summary.  
   - Mobile-first card layout with impact color scale (green → red by USD impact).

5. **Build/ingest hook** (20m)  
   - Ensure ingestion pipeline writes `public/signals/top-hub.json` after each knowledge-rag run.  
   - Deterministic hub selection: pick highest degree node from graph; fallback “MOC”.

6. **Tests & lint** (10m)  
   - Add snapshot test for panel render with mock payload.  
   - Type-check JSON schema via Zod in pipeline.

---

## Code Snippets

### `components/TopHubSignalPanel.tsx`
```tsx
'use client';

import useSWR from 'swr';

interface Proposal {
  id: string;
  title: string;
  impactUSD: number;
  confidence: number;
  action: string;
  detail: string;
}

interface TopHubSignal {
  hub: string;
  updated: string;
  proposals: Proposal[];
}

const fetcher = (url: string) => fetch(url).then((r) => r.json());

export default function TopHubSignalPanel() {
  const { data, error } = useSWR<TopHubSignal>(
    '/signals/top-hub.json',
    fetcher,
    {
      refreshInterval: 300_000,
      revalidateOnFocus: false,
      dedupingInterval: 60_000,
    }
  );

  if (error) return null;
  if (!data) {
    return (
      <div className="rounded-lg border border-gray-200 bg-gray-50 p-4">
        <div className="h-6 w-32 animate-pulse rounded bg-gray-300" />
      </div>
    );
  }

  const top3 = data.proposals.slice(0, 3);
  const maxImpact = Math.max(...top3.map((p) => p.impactUSD), 1);

  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">
          Top-Hub Signal: <span className="text-blue-600">{data.hub}</span>
        </h3>
        <time className="text-xs text-gray-400" dateTime={data.updated}>
          {new Date(data.updated).toLocaleDateString()}
        </time>
      </div>

      <div className="space-y-3">
        {top3.map((p) => {
          const intensity = Math.round((p.impactUSD / maxImpact) * 100);
          const red = Math.min(200, 120 + intensity);
          return (
            <div
              key={p.id}
              className="rounded border-l-4 border-gray-200 bg-gray-50 p-3 transition hover:border-blue-300"
              style={{ borderLeftColor: `rgb(${red},230,230)` }}
            >
              <div className="flex items-start justify-between gap-2">
                <div>
                  <p className="text-sm font-medium text-gray-900">{p.title}</p>
                  <p className="text-xs text-gray-500">{p.detail}</p>
                </div>
                <span className="whitespace-nowrap text-xs font-semibold text-red-600">
                  ${p.impactUSD.toLocaleString()}
                </span>
              </div>
              <div className="mt-2 flex items-center gap-2 text-xs text-gray-500">
                <span>Confidence {(p.confidence * 100).toFixed(0)}%</span>
                <span>•</span>
                <span className="font-medium text-gray-700">{p.action}</span>
              </div>
            </div>
          );
        })}
      </div>

      <p className="mt-3 text-xs text-gray-400">
        Sense + Signal — ไม่ Execute
      </p>
    </section>
  );
}
```

### Ingestion writer (Node example)
```js
// scripts/write-top-hub.js
import fs from 'fs';
import path from 'path';
import { z } from 'zod';

const ProposalSchema = z.object({
  id: z.string(),
  title: z.string(),
  impactUSD: z.number(),
  confidence: z.number().min(0).max(1),
  action: z.string(),
  detail: z.string(),
});

const TopHubSignalSchema = z.object({
  hub: z.string(),
  updated: z.string().datetime(),
  proposals: z.array(ProposalSchema),
});

export function writeTopHubSignal(payload, outDir = 'public/signals') {
  const parsed = TopHubSignalSchema.parse(payload);
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(
    path.join(outDir, 'top-hub.json'),
    JSON.stringify(parsed, null, 2)
  );
}
```

### Dashboard usage (Next.js page)
```tsx
// app/dashboard/page.tsx
import TopHubSignalPanel from '@/components/TopHubSignalPanel';

export default function DashboardPage

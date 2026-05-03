# Costinel / backend

## Final Synthesis — CDN-First Top-Hub Signal Panel (deliverable <2h)

**Single highest-value improvement**  
Add a resilient “Top Hub” panel to Costinel’s dashboard that surfaces the most-connected hub (e.g., “MOC”) and related docs using **CDN-only fetches**, zero model compute, offline-first caching, and strict backend-controlled delivery.

---

### Why this wins (merged rationale)
- Applies **top-hub + CDN bypass** patterns directly (knowledge-rag/graph/hub).  
- Avoids runtime HF API calls, rate limits, and GPU cost.  
- Improves dashboard decision quality immediately (“Sense + Signal”) with no training or infra lift.  
- Contradiction resolved: **backend-first** (FastAPI or Next.js API) is favored over pure client-side CDN fetch for reliable caching, CORS control, and SLA; frontend uses lightweight fetch + offline fallback.

---

### Concrete architecture (backend-first, CDN-only)

```
Costinel Backend (FastAPI or Next.js API)
  ├─ GET /api/signal/top-hub
  │     ├─ reads cache/top-hub.json (CDN-fetched, TTL 1h)
  │     └─ fallback to repo-bundled seed if CDN fails
  └─ cache/top-hub.json (auto-updated by cron)

Frontend (React/Next.js)
  └─ TopHubPanel
       ├─ fetches /api/signal/top-hub
       ├─ localStorage offline cache (24h TTL)
       └─ graceful stale-on-failure UX

Mac/CI orchestration
  └─ scripts/update-hub-index.js (one-time or cron)
       ├─ lists repo tree or queries HF dataset (public or token)
       ├─ computes top-hub + related docs
       └─ writes cache/top-hub.json and optionally commits hub-index.json
```

---

### Implementation steps (timeboxed <2h)

1. **Generate hub index (once or cron)**
   - Add `scripts/update-hub-index.js` to repo.
   - Run locally (Mac) or via CI; outputs `cache/top-hub.json` (and optionally commits `hub-index.json`).
   - Uses HF dataset/tree listing (public or token) to identify top hub and related docs.

2. **Backend endpoint (FastAPI or Next.js)**
   - Path: `/api/signal/top-hub`
   - Behavior:
     - Serve `cache/top-hub.json` if fresh (<1h).
     - If stale, attempt CDN fetch to refresh; on failure, fall back to repo-bundled seed.
     - Set `Cache-Control: public, max-age=3600, stale-while-revalidate=86400`.

3. **Frontend panel**
   - Create `components/TopHubPanel.tsx`.
   - Fetch `/api/signal/top-hub`.
   - Cache response in `localStorage` (24h TTL) for offline-first.
   - Render:
     - Primary hub badge.
     - Up-to-4 related doc links (title + type).
     - Last-updated timestamp.
   - Mobile-responsive and accessible.

4. **Integrate into dashboard**
   - Place `TopHubPanel` in sidebar or top summary bar.
   - Ensure it does not block page load (async fetch, skeleton UI).

5. **CI/optional automation**
   - Add pre-commit or scheduled job to run `update-hub-index.js` and commit updated cache file.

---

### Data contract (canonical)

`cache/top-hub.json` (and API response)
```json
{
  "generated_at": "2026-05-03T05:00:00Z",
  "top_hub": "MOC",
  "related": [
    {
      "title": "MOC — Multi-Org Cost patterns",
      "slug": "moc-multi-org-cost",
      "cdn_url": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs/moc-multi-org-cost.md",
      "type": "hub"
    },
    {
      "title": "Reserved Instance optimization signals",
      "slug": "ri-signals",
      "cdn_url": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/signals/ri-optimization.md",
      "type": "signal"
    }
  ]
}
```

---

### Key code artifacts (merged best parts)

#### Backend (Next.js example)
```ts
// app/api/signal/top-hub/route.ts
import { NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';

const CACHE_PATH = path.join(process.cwd(), 'cache/top-hub.json');
const SEED_PATH = path.join(process.cwd(), 'hub-index.json');

export async function GET() {
  try {
    const raw = fs.readFileSync(CACHE_PATH, 'utf8');
    const data = JSON.parse(raw);
    return NextResponse.json(data, {
      headers: { 'Cache-Control': 'public, max-age=3600, stale-while-revalidate=86400' },
    });
  } catch {
    // fallback to bundled seed
    try {
      const raw = fs.readFileSync(SEED_PATH, 'utf8');
      const data = JSON.parse(raw);
      return NextResponse.json(data, {
        headers: { 'Cache-Control': 'public, max-age=60' },
      });
    } catch (err) {
      return NextResponse.json({ error: 'Top-hub unavailable' }, { status: 503 });
    }
  }
}
```

#### Frontend panel (React/Next.js)
```tsx
// components/TopHubPanel.tsx
'use client';

import { useEffect, useState } from 'react';

interface RelatedDoc {
  title: string;
  slug: string;
  cdn_url: string;
  type: string;
}

interface HubIndex {
  generated_at: string;
  top_hub: string;
  related: RelatedDoc[];
}

const CACHE_KEY = 'costinel-top-hub-cache';
const CACHE_TTL = 24 * 60 * 60 * 1000;

export default function TopHubPanel() {
  const [data, setData] = useState<HubIndex | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function load() {
      // try localStorage cache first
      const cached = localStorage.getItem(CACHE_KEY);
      if (cached) {
        try {
          const parsed = JSON.parse(cached);
          if (Date.now() - new Date(parsed.generated_at).getTime() < CACHE_TTL) {
            setData(parsed);
            setLoading(false);
          }
        } catch {
          // ignore invalid cache
        }
      }

      try {
        const res = await fetch('/api/signal/top-hub', { cache: 'no-store' });
        if (!res.ok) throw new Error('Failed to fetch top-hub');
        const fresh = await res.json();
        setData(fresh);
        localStorage.setItem(CACHE_KEY, JSON.stringify(fresh));
      } catch {
        // keep stale cache if present
        if (!data && cached) {
          try {
            setData(JSON.parse(cached));
          } catch {
            // noop
          }
        }
      } finally {
        setLoading(false);
      }
    }

    load();
  }, []);

  if (loading && !data) {
    return (
      <div className="p-4 border rounded bg-gray-50 text-sm text-gray-600">
        Loading signals...
      </div>
    );
  }

  if (!data) {
    return null;
  }

  return (
    <div className="p-4 border rounded bg-white shadow-sm">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-medium uppercase text-gray-500">Top Hub</span>
        <span className="text-xs text-gray-400">
          Updated {new Date(data.generated_at).toLocaleDateString()}
        </span>
      </div>
      <div className="mb-3">
        <span className="inline-flex

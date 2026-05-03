# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### What we ship
- A **non-blocking Top-Hub Signal Panel** on the Costinel dashboard.
- Defaults to the most-connected hub **MOC** (configurable via `TOP_HUB_DEFAULT`).
- Shows the **top-3 actionable, cost-impact proposals** from knowledge-rag.
- Uses **HF CDN bypass** for data fetches (zero runtime API calls to backend services).
- Graceful degradation: hides panel if CDN unavailable or feature disabled.

### Architecture (fits existing patterns)
- **Data layer**:  
  `knowledge-rag/top-hub/{hub}/proposals.json` in a public HF dataset repo (e.g., `axentx/costinel-knowledge`).  
  Delivered via CDN:  
  `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/knowledge-rag/top-hub/{hub}/proposals.json`
- **Frontend**: React component `TopHubSignalPanel` (client-side fetch with in-memory cache).
- **Backend**: Optional server-side proxy (`/api/top-hub/{hub}`) for SSR/SSG or stricter caching (5m).  
  Keep only one backend route to avoid duplication:  
  - If Next.js: `src/pages/api/top-hub/[hub].ts`  
  - If FastAPI backend: `backend/app/api/top_hub.py`  
  Choose **one** based on the actual stack; do not implement both.
- **Config**: `TOP_HUB_ENABLED`, `TOP_HUB_DEFAULT`, `TOP_HUB_DATASET_REPO`, `TOP_HUB_CACHE_TTL` via env.

### File changes (incremental)
1. `src/components/TopHubSignalPanel.tsx` — new component.
2. `src/lib/topHub.ts` — CDN fetch helper + in-memory cache.
3. Backend proxy (pick one):
   - Next.js: `src/pages/api/top-hub/[hub].ts`
   - FastAPI: `backend/app/api/top_hub.py`
4. `.env.example` — add config vars.
5. Dashboard page (e.g., `src/app/dashboard/page.tsx` or `src/pages/Dashboard.tsx`) — mount panel.
6. Sample data: `data/knowledge-rag/top-hub/MOC/proposals.json` (for local dev/testing).

---

## Concrete Code & Config

### 1) Environment (.env.example)
```bash
# Top-Hub Signal Panel
TOP_HUB_ENABLED=true
TOP_HUB_DEFAULT=MOC
TOP_HUB_DATASET_REPO=axentx/costinel-knowledge
TOP_HUB_CACHE_TTL=300
CDN_BASE_URL=https://huggingface.co/datasets
```

### 2) CDN fetch helper (src/lib/topHub.ts)
```ts
// src/lib/topHub.ts
type Proposal = {
  id: string;
  title: string;
  summary: string;
  impact: 'high' | 'medium' | 'low';
  estimated_savings_usd: number;
  action_url?: string;
  tags: string[];
};

type TopHubResponse = {
  hub: string;
  generated_at: string;
  proposals: Proposal[];
};

const DEFAULT_REPO = process.env.NEXT_PUBLIC_TOP_HUB_DATASET_REPO || process.env.TOP_HUB_DATASET_REPO || 'axentx/costinel-knowledge';
const CDN_BASE = process.env.NEXT_PUBLIC_CDN_BASE_URL || process.env.CDN_BASE_URL || 'https://huggingface.co/datasets';
const CACHE_TTL = Number(process.env.NEXT_PUBLIC_TOP_HUB_CACHE_TTL || process.env.TOP_HUB_CACHE_TTL || 300);

let cache: { data: TopHubResponse | null; ts: number } = { data: null, ts: 0 };

function getCdnUrl(hub: string): string {
  return `${CDN_BASE}/${DEFAULT_REPO}/resolve/main/knowledge-rag/top-hub/${hub}/proposals.json`;
}

export async function fetchTopHubProposals(hub: string, useCache = true): Promise<TopHubResponse | null> {
  const now = Date.now();
  if (useCache && cache.data && now - cache.ts < CACHE_TTL * 1000) {
    return cache.data;
  }

  try {
    const url = getCdnUrl(hub);
    const res = await fetch(url, {
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(4000),
    });

    if (!res.ok) {
      console.warn('[TopHub] CDN fetch failed', res.status, url);
      return cache.data || null;
    }

    const data = (await res.json()) as TopHubResponse;
    cache = { data, ts: now };
    return data;
  } catch (err) {
    console.warn('[TopHub] CDN fetch error', err);
    return cache.data || null;
  }
}
```

### 3) Backend proxy (choose one)

#### Next.js (src/pages/api/top-hub/[hub].ts)
```ts
// src/pages/api/top-hub/[hub].ts
import type { NextApiRequest, NextApiResponse } from 'next';
import { fetchTopHubProposals } from '@/lib/topHub';

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  const { hub } = req.query;
  if (!hub || typeof hub !== 'string') {
    return res.status(400).json({ error: 'hub required' });
  }

  res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=60');
  const data = await fetchTopHubProposals(hub, true);
  if (!data) {
    return res.status(404).json({ error: 'proposals not available' });
  }
  return res.status(200).json(data);
}
```

#### FastAPI (backend/app/api/top_hub.py)
```python
# backend/app/api/top_hub.py
import os
import httpx
from fastapi import APIRouter, HTTPException
from typing import Dict, Any

router = APIRouter()

REPO = os.getenv("TOP_HUB_DATASET_REPO", "axentx/costinel-knowledge")
CDN_BASE = os.getenv("CDN_BASE_URL", "https://huggingface.co/datasets")
CACHE_TTL = int(os.getenv("TOP_HUB_CACHE_TTL", "300"))

_cache: Dict[str, Any] = {"data": None, "ts": 0}

def _cache_get(hub: str):
    import time
    if _cache["data"] and _cache["data"].get("hub") == hub:
        if time.time() - _cache["ts"] < CACHE_TTL:
            return _cache["data"]
    return None

def _cache_set(data):
    import time
    _cache["data"] = data
    _cache["ts"] = time.time()

@router.get("/top-hub/{hub_id}/signals")
async def get_top_hub_signals(hub_id: str):
    cached = _cache_get(hub_id)
    if cached:
        return cached

    url = f"{CDN_BASE}/{REPO}/resolve/main/knowledge-rag/top-hub/{hub_id}/proposals.json"
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(url, headers={"Accept": "application/json"})
            if r.status_code != 200:
                if _cache["data"] and _cache["data"].get("hub") == hub_id:
                    return _cache["data"]
                raise HTTPException(status_code=404, detail="proposals not available")
            data = r.json()
            _cache_set(data)
            return data
    except Exception:
        if _cache["data"] and _cache["data"].get("hub") == hub_id:
            return _cache["data"]
        raise HTTPException(status_code=502, detail="CDN unavailable")
```

### 4) React component (src/components/TopHubSignalPanel.tsx)
```tsx
// src/components/TopHubSignalPanel.tsx
'use client';

import { useEffect, useState } from 'react';
import { fetchTopHubProposals } from '@/lib/topHub';

const

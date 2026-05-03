# Costinel / quality

## Highest-Value Incremental Improvement (<2h)

**Goal**: Add a read-only **Signal Panel** (top-hub insights) to Costinel that surfaces the most-connected hub (e.g., "MOC") and related contextual signals using CDN-first data fetching with zero HF API calls during render.

**Why this ships fast**:
- Pure read-only UI + lightweight proxy (no DB migrations, no auth changes).
- Uses existing CDN bypass pattern (`resolve/main/...`) and pre-listed file JSON.
- Reuses current design system (dashboard cards, badges, audit trail style).
- Aligns with top-hub insight pattern and avoids training/infra scope.

---

## Implementation Plan

### 1. File layout (additions only)

```
/opt/axentx/Costinel/
├── src/
│   ├── components/
│   │   └── SignalPanel.tsx        # new: top-hub + related signals
│   ├── lib/
│   │   └── cdn.ts                 # new: CDN fetcher (no auth)
│   └── routes/
│       └── api/
│           └── signal/
│               └── index.ts        # new: optional proxy endpoint (resilience)
├── public/
│   └── signal/
│       └── file-list.json         # committed: pre-listed CDN paths (date folder)
└── app/
    └── dashboard/
        └── page.tsx               # modify: mount SignalPanel
```

### 2. Pre-list CDN files (one-time, Mac orchestration)

Run once (or via cron after rate-limit window) and commit `public/signal/file-list.json`:

```bash
#!/usr/bin/env bash
# scripts/list-signal-files.sh
set -euo pipefail

REPO="axentx/signal-hub"
FOLDER="2026-05-03"
OUT="public/signal/file-list.json"

# Single API call (non-recursive) to list folder
curl -s \
  -H "Authorization: Bearer ${HF_TOKEN:-}" \
  "https://huggingface.co/api/datasets/${REPO}/tree?path=${FOLDER}&recursive=false" \
  | jq '[.tree[] | select(.type=="file") | .path]' > "${OUT}"

echo "Saved $(jq length ${OUT}) files to ${OUT}"
```

> Note: If `HF_TOKEN` absent, run once when window clears; CDN files remain publicly fetchable.

### 3. CDN fetcher (zero-auth)

`src/lib/cdn.ts`

```ts
export async function fetchCdnText(path: string): Promise<string> {
  // Public dataset file — no Authorization header
  const url = `https://huggingface.co/datasets/axentx/signal-hub/resolve/main/${path}`;
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`CDN fetch failed: ${res.status} ${path}`);
  return await res.text();
}

export async function fetchCdnJson<T = unknown>(path: string): Promise<T> {
  const text = await fetchCdnText(path);
  return JSON.parse(text) as T;
}
```

### 4. Optional lightweight proxy (resilience)

`src/routes/api/signal/index.ts`

```ts
import { NextResponse } from 'next/server';
import { fetchCdnJson } from '@/lib/cdn';

export async function GET() {
  try {
    // Use pre-listed file list from public/ (committed)
    const list = await fetchCdnJson<string[]>('/signal/file-list.json');
    const topHubPath = list.find((p) => p.includes('top-hub.json'));
    if (!topHubPath) throw new Error('top-hub.json not found');

    const payload = await fetchCdnJson<{
      hub: string;
      score: number;
      signals: Array<{ id: string; title: string; snippet: string; source: string }>;
    }>(topHubPath);

    return NextResponse.json(payload);
  } catch (err) {
    return NextResponse.json(
      { error: 'Unable to fetch signal', details: String(err) },
      { status: 502 }
    );
  }
}
```

### 5. SignalPanel component

`src/components/SignalPanel.tsx`

```tsx
'use client';

import { useEffect, useState } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';

interface Signal {
  id: string;
  title: string;
  snippet: string;
  source: string;
}

interface TopHubPayload {
  hub: string;
  score: number;
  signals: Signal[];
}

export default function SignalPanel() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Prefer proxy for resilience; fallback to direct CDN if needed
    fetch('/api/signal')
      .then((r) => (r.ok ? r.json() : Promise.reject(r)))
      .then(setData)
      .catch(() => {
        // Direct CDN fallback (public)
        fetch('/signal/file-list.json')
          .then((r) => r.json())
          .then((list: string[]) => list.find((p) => p.includes('top-hub.json')))
          .then((path) => (path ? fetch(`https://huggingface.co/datasets/axentx/signal-hub/resolve/main/${path}`).then((r) => r.json()) : null))
          .then(setData)
          .finally(() => setLoading(false));
      })
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <Skeleton className="h-5 w-32" />
        </CardHeader>
        <CardContent className="space-y-3">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-5/6" />
        </CardContent>
      </Card>
    );
  }

  if (!data) return null;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle className="text-base font-semibold">Top-Hub Signal</CardTitle>
        <Badge variant="secondary">{data.hub}</Badge>
      </CardHeader>
      <CardContent className="space-y-3">
        {data.signals.slice(0, 3).map((s) => (
          <div key={s.id} className="border-l-2 pl-3 py-1 text-sm">
            <p className="font-medium text-foreground">{s.title}</p>
            <p className="text-muted-foreground">{s.snippet}</p>
            <p className="text-xs text-muted-foreground mt-1">src: {s.source}</p>
          </div>
        ))}
        <p className="text-xs text-muted-foreground mt-2">
          Score: {data.score} — Sense + Signal (ไม่ Execute)
        </p>
      </CardContent>
    </Card>
  );
}
```

### 6. Mount on dashboard

`app/dashboard/page.tsx` (add near top insights row)

```tsx
import SignalPanel from '@/components/SignalPanel';

// Inside your grid/layout:
<SignalPanel />
```

### 7. Tests & checks (quick)

- Verify `public/signal/file-list.json` exists and contains `top-hub.json`.
- Run dev server: ensure `/api/signal` returns payload and panel renders.
- Disable JS: confirm CDN fallback URL is publicly accessible (no auth).

---

## Expected Outcome

- Users see a concise **Top-Hub Signal** card on the dashboard.
- Data is served via **CDN bypass** (zero HF API calls during render).
- Optional proxy provides resilience without auth or state.
- Fully read-only, consistent with Costinel philosophy: **Sense + Signal — ไม่ Execute**.

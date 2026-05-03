# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h)

### What we ship
- A **non-blocking Top-Hub Signal Panel** mounted at the top of the Costinel dashboard.
- Defaults to hub **MOC** (configurable via `VITE_HUB_NAME`).
- Shows: hub title, short insight, last-updated timestamp, and a “View in Knowledge Graph” link.
- **CDN-first data fetch**:  
  `https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs/{hubName}.json`  
  (bypasses HF API rate limits).
- Graceful fallback to local placeholder if CDN fails.
- Zero backend changes; pure frontend addition.

### Why this is highest-value (<2h)
- Reuses existing knowledge-rag + top-hub patterns.
- Adds immediate contextual awareness to Costinel users without touching sensitive cost controls (“Sense + Signal”).
- CDN-only fetch keeps ops simple and avoids rate limits.
- Non-blocking: panel failure does not break dashboard.
- Small, safe, and reversible.

---

## Implementation Steps

1. **Add optional env var**  
   In `.env` or Vite config: `VITE_HUB_NAME=MOC`.

2. **Create hub data fetcher utility**  
   - CDN URL template as above.  
   - 4s timeout + abort.  
   - Fallback to local placeholder JSON.

3. **Create TopHubSignalPanel component**  
   - Mounts near top of main dashboard view.  
   - Shows loading → data → error states (non-blocking).  
   - Links open in new tab when URL provided.

4. **Add to dashboard layout**  
   Insert `<TopHubSignalPanel />` at top of main dashboard.

5. **Styling**  
   Minimal, non-distracting; uses existing design tokens.

6. **Test**  
   Verify CDN fetch, fallback, and UI states; ensure no console errors on network failure.

---

## Code Snippets

### 1) Env var (optional)
```bash
# .env
VITE_HUB_NAME=MOC
```

### 2) Hub fetcher utility
```ts
// src/lib/hubFetcher.ts
const HUB_CDN_BASE = 'https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/hubs';
const LOCAL_HUBS = import.meta.glob('/src/data/hubs/*.json', { eager: true, import: 'default' });

export interface HubInsight {
  hub: string;
  title: string;
  insight: string;
  lastUpdated: string; // ISO
  url?: string;
}

export async function fetchHubInsight(hubName: string): Promise<HubInsight | null> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);

  try {
    const res = await fetch(`${HUB_CDN_BASE}/${encodeURIComponent(hubName)}.json`, {
      signal: controller.signal,
      cache: 'no-store'
    });
    clearTimeout(timeout);

    if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
    const data = await res.json();
    return {
      hub: hubName,
      title: data.title || hubName,
      insight: data.insight || data.summary || 'No insight available.',
      lastUpdated: data.lastUpdated || new Date().toISOString(),
      url: data.url
    };
  } catch (err) {
    clearTimeout(timeout);
    console.warn('Hub CDN fetch failed, using local fallback:', err);
    const localKey = `/src/data/hubs/${hubName}.json`;
    const local = LOCAL_HUBS[localKey] as HubInsight | undefined;
    if (local) return local;
    return {
      hub: hubName,
      title: hubName,
      insight: 'Hub insights unavailable.',
      lastUpdated: new Date().toISOString()
    };
  }
}
```

### 3) TopHubSignalPanel component (React)
```tsx
// src/components/TopHubSignalPanel.tsx
import { useEffect, useState } from 'react';
import { fetchHubInsight, type HubInsight } from '../lib/hubFetcher';

interface Props {
  hubName?: string;
}

export default function TopHubSignalPanel({ hubName }: Props) {
  const [insight, setInsight] = useState<HubInsight | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const name = hubName || import.meta.env.VITE_HUB_NAME || 'MOC';
    setLoading(true);
    fetchHubInsight(name).then((res) => {
      setInsight(res);
      setLoading(false);
    });
  }, [hubName]);

  // Non-blocking: render nothing while loading and no cached data
  if (loading && !insight) return null;
  if (!insight) return null;

  const timeAgo = new Date(insight.lastUpdated).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  });

  return (
    <div className="top-hub-signal-panel border-b bg-muted/50 px-4 py-2 text-sm">
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-foreground">{insight.title}</span>
          <span className="text-muted-foreground">—</span>
          <span className="text-muted-foreground">{insight.insight}</span>
        </div>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span>Updated {timeAgo}</span>
          {insight.url && (
            <a
              href={insight.url}
              target="_blank"
              rel="noopener noreferrer"
              className="underline hover:text-foreground"
            >
              View graph
            </a>
          )}
        </div>
      </div>
    </div>
  );
}
```

### 4) Add to dashboard layout
```tsx
// src/pages/Dashboard.tsx (or equivalent)
import TopHubSignalPanel from '../components/TopHubSignalPanel';

export default function Dashboard() {
  return (
    <div className="flex min-h-screen flex-col">
      <TopHubSignalPanel />
      <div className="flex-1 p-6">
        {/* existing dashboard content */}
      </div>
    </div>
  );
}
```

### 5) Local fallback example (optional)
```json
// src/data/hubs/MOC.json
{
  "hub": "MOC",
  "title": "MOC",
  "insight": "Most-connected hub for cost governance workflows; central to cross-account policy signals.",
  "lastUpdated": "2026-05-03T00:00:00.000Z",
  "url": "https://axentx.example/knowledge/hubs/MOC"
}
```

---

## Acceptance Criteria
- Panel appears on dashboard without blocking user actions.
- Defaults to MOC; respects `VITE_HUB_NAME`.
- CDN fetch attempted first; falls back silently to local.
- No console errors on network failure.
- Links open in new tab (if provided).
- Non-intrusive styling consistent with Costinel UI.

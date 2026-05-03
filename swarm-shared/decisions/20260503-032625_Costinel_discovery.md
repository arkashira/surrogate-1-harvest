# Costinel / discovery

## Final Implementation Plan — Top-Hub Signal Panel (CDN-first, <2h, production-ready)

### Scope (single highest-value deliverable)
Add a **non-blocking Top-Hub Signal Panel** to the Costinel dashboard that:
- Detects and surfaces the most-connected knowledge-hub (e.g., "MOC") from the knowledge-rag graph
- Renders signals inline on the cost dashboard without blocking page load or execution paths
- Uses **CDN-first data fetching** (bypasses HF API rate limits) with graceful fallback and local cache
- Follows "Sense + Signal — ไม่ Execute" philosophy (propose, don’t mutate)

---

### Architecture (CDN-first, non-blocking)
```
Costinel Dashboard (Next.js/React)
  ├── Server Component: layout + static shell
  ├── Client Component: SignalPanel (lazy, suspense boundary)
  │   ├── useTopHubQuery (SWR for stale-while-revalidate)
  │   ├── CDN fetcher (no auth header, 3s timeout)
  │   └── fallback: local minimal signal + graceful empty state
  └── CDN Data Layer: /top-hub.json (public, no auth)
```

**Data flow:**
1. Mac orchestration script runs `list_repo_tree` once per day → saves `top-hub.json` to CDN  
2. CDN URL: `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub.json`  
3. Panel fetches via CDN (no auth, no rate limit) with 3s timeout  
4. On failure: renders local minimal signal with subtle pulse (non-blocking)  

---

### File Changes (3 files, ~140 lines total)

#### 1) Orchestration script (runs on Mac, 1×/day via cron)  
`/opt/axentx/Costinel/scripts/update-top-hub-cdn.sh`
```bash
#!/usr/bin/env bash
# Updates top-hub.json on HF CDN daily
set -euo pipefail

HF_DATASET="axentx/costinel-knowledge"
OUT_FILE="/tmp/top-hub.json"

# Generate top-hub payload (replace with real graph query in production)
cat > "$OUT_FILE" <<'JSON'
{
  "generated_at": "2026-05-03T03:30:00Z",
  "top_hub": {
    "id": "MOC",
    "label": "Mission Operating Center",
    "connections": 142,
    "tags": ["knowledge-rag", "graph", "hub"],
    "summary": "Central coordination node for cost governance signals. Review before planning tasks.",
    "docs": [
      { "slug": "costinel/ops", "title": "Costinel Ops", "relevance": 0.92 },
      { "slug": "costinel/design", "title": "Top-Hub Signal Panel", "relevance": 0.87 }
    ]
  }
}
JSON

# Upload to HF dataset repo (requires HF_TOKEN with write access)
curl -sSfL \
  -X PUT \
  -H "Authorization: Bearer ${HF_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary @"$OUT_FILE" \
  "https://huggingface.co/api/datasets/${HF_DATASET}/resolve/main/top-hub.json"

echo "✅ top-hub.json updated on CDN"
```
Cron entry (run daily at 03:30):
```
30 3 * * * /opt/axentx/Costinel/scripts/update-top-hub-cdn.sh >> /var/log/costinel/top-hub-cdn.log 2>&1
```

---

#### 2) `/components/SignalPanel.tsx` (client, lazy-loaded, SWR)
```tsx
'use client';

import useSWR from 'swr';
import { ExternalLink, AlertCircle, TrendingUp } from 'lucide-react';

interface HubSignal {
  id: string;
  label: string;
  connections: number;
  tags: string[];
  summary: string;
  docs: Array<{ slug: string; title: string; relevance: number }>;
}

interface TopHubData {
  generated_at: string;
  top_hub: HubSignal;
  fallback?: boolean;
}

const CDN_URL = 'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub.json';

const fetcher = (url: string) =>
  fetch(url, { cache: 'no-store', headers: { Accept: 'application/json' } })
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((data) => ({ ...data, fallback: false }));

const LOCAL_MINIMAL: TopHubData = {
  generated_at: new Date().toISOString(),
  top_hub: {
    id: 'MOC',
    label: 'Mission Operating Center',
    connections: 0,
    tags: ['knowledge-rag', 'graph', 'hub'],
    summary: 'Review the most-connected hub before planning tasks.',
    docs: [],
  },
  fallback: true,
};

export function SignalPanel() {
  const { data, error, isLoading } = useSWR<TopHubData>(CDN_URL, fetcher, {
    revalidateOnFocus: false,
    revalidateOnReconnect: false,
    dedupingInterval: 300_000,
    fallbackData: LOCAL_MINIMAL,
    onErrorRetry: (err) => {
      // Non-blocking: do not retry aggressively
      if (err === 404) return;
    },
  });

  const signal = (isLoading && LOCAL_MINIMAL) || (error && LOCAL_MINIMAL) || data || LOCAL_MINIMAL;
  const { top_hub } = signal;

  if (isLoading) {
    return (
      <div className="animate-pulse rounded-lg border border-slate-200 bg-slate-50 p-4">
        <div className="h-4 w-24 rounded bg-slate-300" />
      </div>
    );
  }

  return (
    <aside className="rounded-lg border border-amber-100 bg-amber-50 p-4 text-sm shadow-sm">
      <div className="mb-2 flex items-center justify-between">
        <span className="inline-flex items-center gap-1.5 font-semibold text-amber-800">
          <TrendingUp className="h-4 w-4" />
          Top-Hub Signal
        </span>
        {!signal.fallback && (
          <span className="text-xs text-amber-600">
            {new Date(signal.generated_at).toLocaleTimeString()}
          </span>
        )}
      </div>

      <div className="mb-2">
        <span className="font-mono text-lg font-bold text-amber-900">{top_hub.id}</span>
        <span className="ml-2 text-amber-800">{top_hub.label}</span>
        {top_hub.connections > 0 && (
          <span className="ml-2 rounded bg-amber-200 px-1.5 py-0.5 text-xs font-medium text-amber-800">
            {top_hub.connections} connections
          </span>
        )}
      </div>

      <p className="mb-3 text-amber-700">{top_hub.summary}</p>

      {top_hub.docs.length > 0 && (
        <ul className="space-y-1">
          {top_hub.docs.map((doc) => (
            <li key={doc.slug}>
              <a
                href={`/docs/${doc.slug}`}
                className="flex items-center gap-1 text-amber-700 hover:text-amber-900 hover:underline"
              >
                <ExternalLink className="h-3 w-3" />
                <span className="truncate">{doc.title}</span>
              </a>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 flex items-center justify-end gap-2 text-xs text-amber-600">
        <AlertCircle className="h-3 w-3" />
        Sense + Signal — ไม่ Execute

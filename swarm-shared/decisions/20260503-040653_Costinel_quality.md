# Costinel / quality

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Scope**: Add a lightweight, non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data (zero runtime HF API). Deterministic, cache-friendly, and deployable in <2h.

---

### 1) Architecture (CDN-first, zero runtime HF API)
- **Data source**: `knowledge-rag` publishes `top-hub.json` to `batches/mirror-merged/{date}/top-hub.json` on HF dataset repo.
- **Delivery**: Use HF CDN URL (no auth, no API rate limit):  
  `https://huggingface.co/datasets/{repo}/resolve/main/batches/mirror-merged/{date}/top-hub.json`
- **Pre-list strategy**: Mac orchestration script lists `batches/mirror-merged/` once per day, saves `file-list.json`. Training/UI embeds latest path.
- **UI**: Frontend fetches latest `top-hub.json` via CDN at render time (cached). Falls back to local placeholder if unavailable.
- **No backend changes required** (static asset fetch). If backend exists, add a single `/api/signal/top-hub` proxy route that serves CDN content with local cache (5min TTL).

---

### 2) File layout (additions only)
```
/opt/axentx/Costinel/
├── public/
│   └── signals/
│       └── top-hub.json          # local fallback (committed)
├── src/
│   ├── components/
│   │   └── TopHubSignalPanel.tsx # new component
│   ├── hooks/
│   │   └── useCDNFetch.ts        # new hook (generic CDN fetch)
│   └── pages/
│       └── Dashboard.tsx         # integrate panel
├── scripts/
│   └── update-top-hub-ref.sh     # optional: update local fallback from CDN
└── package.json
```

---

### 3) Implementation steps (ordered)

#### Step 1 — Create local fallback `public/signals/top-hub.json`
```json
{
  "hub": "MOC",
  "score": 0.94,
  "connections": 1287,
  "lastUpdated": "2026-05-03",
  "source": "knowledge-rag",
  "cdnPath": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/batches/mirror-merged/2026-05-03/top-hub.json"
}
```

#### Step 2 — Hook: `src/hooks/useCDNFetch.ts`
```ts
import { useEffect, useState } from 'react';

export function useCDNFetch<T>(cdnUrl: string, fallback: T, ttlMs = 300_000) {
  const [data, setData] = useState<T>(fallback);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    const cached = sessionStorage.getItem(`cdn:${cdnUrl}`);
    const cachedTime = sessionStorage.getItem(`cdn:${cdnUrl}:ts`);
    const now = Date.now();

    if (cached && cachedTime && now - Number(cachedTime) < ttlMs) {
      try {
        setData(JSON.parse(cached));
        setLoading(false);
      } catch {
        // ignore cache corruption
      }
    }

    fetch(cdnUrl, { cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new Error(`CDN fetch failed: ${res.status}`);
        return res.json();
      })
      .then((json: T) => {
        setData(json);
        sessionStorage.setItem(`cdn:${cdnUrl}`, JSON.stringify(json));
        sessionStorage.setItem(`cdn:${cdnUrl}:ts`, String(now));
        setLoading(false);
      })
      .catch((err) => {
        setError(err);
        setLoading(false);
      });
  }, [cdnUrl, ttlMs]);

  return { data, loading, error };
}
```

#### Step 3 — Component: `src/components/TopHubSignalPanel.tsx`
```tsx
import React from 'react';
import { useCDNFetch } from '../hooks/useCDNFetch';

interface TopHubData {
  hub: string;
  score: number;
  connections: number;
  lastUpdated: string;
  source: string;
  cdnPath?: string;
}

const FALLBACK: TopHubData = require('../../../public/signals/top-hub.json');

const CDN_URL = FALLBACK.cdnPath || '';

export const TopHubSignalPanel: React.FC = () => {
  const { data, loading, error } = useCDNFetch<TopHubData>(CDN_URL, FALLBACK);

  if (loading) {
    return (
      <div className="p-4 rounded-lg border bg-white/50 animate-pulse">
        <div className="h-4 w-24 bg-gray-200 rounded mb-2"></div>
        <div className="h-6 w-32 bg-gray-200 rounded"></div>
      </div>
    );
  }

  if (error) {
    return null; // non-blocking: silently fallback to nothing if CDN fails
  }

  return (
    <div className="p-4 rounded-lg border bg-gradient-to-r from-blue-50 to-indigo-50 border-blue-200">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs font-medium text-blue-600 uppercase tracking-wide">
            Top Hub — Knowledge Graph
          </p>
          <p className="text-xl font-semibold text-gray-900">{data.hub}</p>
          <p className="text-sm text-gray-600">
            {data.connections.toLocaleString()} connections &middot; score {data.score}
          </p>
        </div>
        <div className="text-right text-xs text-gray-500">
          Updated {data.lastUpdated}
        </div>
      </div>
    </div>
  );
};
```

#### Step 4 — Integrate into Dashboard: `src/pages/Dashboard.tsx`
Locate the top section of the dashboard (near cost summary) and insert:
```tsx
import { TopHubSignalPanel } from '../components/TopHubSignalPanel';

// Inside your Dashboard component, near the top:
<TopHubSignalPanel />
```

#### Step 5 — Optional: script to refresh local fallback from CDN (for offline builds)
`scripts/update-top-hub-ref.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/knowledge-rag"
PREFIX="batches/mirror-merged"
DEST="public/signals/top-hub.json"

# Find latest date folder via CDN listing (non-recursive)
# We use GitHub-compatible raw listing via hfh (hf hub) or fallback to known date.
# Simpler: use a known latest date from CI env or fallback to today.
# For <2h scope, we pin to latest known or use a small API call once per day from Mac.

# One-time fetch from CDN (example using curl + GitHub-compatible tree API)
# If you have a daily list file, use it. Otherwise, hardcode latest date in CI.
# This script is optional for production; runtime uses CDN directly.

LATEST_DATE=$(curl -s "https://huggingface.co/api/datasets/${REPO}/tree?path=${PREFIX}&recursive=false" | \
  jq -r '.[].path' | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort -r | head -n1)

if [ -z "$LATEST_DATE" ]; then
  echo "No date folder found, using fallback"
  exit 0
fi

CDN_URL="https://huggingface.co/datasets/${REPO}/resolve/main/${PREFIX}/${LATEST_DATE}/top-hub.json"
echo "Fetching ${CDN_URL}"
curl -s -f "$CDN_URL" -o "$DEST"
echo "Updated ${DEST}"
```
Make executable:

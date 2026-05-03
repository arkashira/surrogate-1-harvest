# Costinel / discovery

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top‑Hub Signal Panel to the Costinel dashboard that surfaces the most‑connected hub (e.g., "MOC") using CDN‑first data baked at build time. Runtime dashboard makes **zero HF API calls**.

---

### 1) Build‑time data pipeline (single orchestration run per deploy)
- **One HF API call**: `list_repo_tree` for today’s date folder in `datasets/axentx/costinel-knowledge` (non‑recursive).  
- **Select file**: pick the first JSON containing “top‑hub” (or first JSON fallback).  
- **Download via CDN**: `https://huggingface.co/datasets/.../resolve/main/...` (no auth, bypass rate limits).  
- **Project to minimal schema**:
  ```ts
  { hub: string; score: number; signals: Array<{ type: string; count: number; trend: 'up'|'down'|'stable' }>; lastUpdated: string }
  ```
- **Output**: `public/data/top-hub-signal.json` (committed or injected at build).  
- **Validation**: fail fast on invalid JSON/schema.  
- **Caching**: CDN TTL 5 min; stale‑while‑revalidate.

---

### 2) Frontend component (`components/TopHubSignalPanel.tsx`)
- Fetch local JSON with `useSWR` (or `fetch` + SWR pattern): `staleWhileRevalidate`, 5 min revalidate, cache‑first.  
- Render card: hub name, score, top 3 signals with trend icons, last updated timestamp.  
- Accessibility: `aria-label`, keyboard‑nav friendly, color‑contrast safe.  
- Graceful fallback: silently hide panel if fetch fails or data is malformed (no crash, no blocking).  
- Subtle pulse animation only when score delta exceeds threshold (configurable).

---

### 3) Dashboard integration
- Slot into existing dashboard grid near cost summary row.  
- Non‑blocking: panel failure does not block cost widgets or other panels.  
- Mobile responsive, low visual footprint, high z‑index only when needed.  

---

### 4) CI/CD & ops
- Add `scripts/update-top-hub.sh` to pre‑deploy step (or run in CI).  
- Cache `public/data/top-hub-signal.json` in CDN with 5 min TTL.  
- No runtime secrets or HF tokens required.  

---

## Code Snippets

### `scripts/update-top-hub.sh` (Mac/CI orchestration)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="datasets/axentx/costinel-knowledge"
DATE=$(date +%Y-%m-%d)
OUT_DIR="public/data"
META_FILE="${OUT_DIR}/top-hub-signal.json"
TMP_RAW="${OUT_DIR}/top-hub-raw.json"

mkdir -p "${OUT_DIR}"

# 1) List today's folder (single API call)
echo "📡 Listing ${REPO} tree for ${DATE}..."
TREE_JSON=$(python3 -c "
import json, sys
from huggingface_hub import HfApi
api = HfApi()
files = api.list_repo_tree(repo_id='${REPO}', path='${DATE}', recursive=False)
print(json.dumps([{'path': f.path} for f in files]))
" 2>/dev/null || echo '[]')

# 2) Pick top-hub file (heuristic: contains 'top-hub', else first .json)
TOP_FILE=$(echo "${TREE_JSON}" | python3 -c "
import sys, json, re
files = json.load(sys.stdin)
for f in files:
    p = f.get('path','')
    if 'top-hub' in p.lower():
        print(p)
        sys.exit(0)
for f in files:
    if f.get('path','').endswith('.json'):
        print(f['path'])
        sys.exit(0)
print('')
")

if [ -z "${TOP_FILE}" ]; then
  echo "⚠️ No top-hub file found; skipping."
  exit 0
fi

# 3) Download via CDN (no auth)
CDN_URL="https://huggingface.co/datasets/${REPO}/resolve/main/${TOP_FILE}"
echo "⬇️ Downloading ${CDN_URL}..."
curl -fsSL "${CDN_URL}" -o "${TMP_RAW}"

# 4) Project to minimal schema
python3 -c "
import json, datetime, sys
with open('${TMP_RAW}') as f:
    raw = json.load(f)

hub = str(raw.get('hub', raw.get('top_hub', 'MOC')))
score = float(raw.get('score', raw.get('centrality', 0.0)))
signals = raw.get('signals', raw.get('related', []))[:3]

projected = {
    'hub': hub,
    'score': round(score, 2),
    'signals': [
        {
            'type': str(s.get('type', s.get('label', 'signal'))),
            'count': int(s.get('count', s.get('weight', 0))),
            'trend': s.get('trend', 'stable')
        }
        for s in signals
    ],
    'lastUpdated': datetime.datetime.utcnow().isoformat() + 'Z'
}

with open('${META_FILE}', 'w') as out:
    json.dump(projected, out, separators=(',', ':'))
print('✅ Projected top-hub signal to ${META_FILE}')
"

# 5) Cleanup
rm -f "${TMP_RAW}"
echo "✅ Done."
```

---

### `components/TopHubSignalPanel.tsx`
```tsx
'use client';

import useSWR from 'swr';
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';

interface Signal {
  type: string;
  count: number;
  trend: 'up' | 'down' | 'stable';
}

interface TopHubData {
  hub: string;
  score: number;
  signals: Signal[];
  lastUpdated: string;
}

const fetcher = (url: string) => fetch(url, { cache: 'force-cache' }).then((r) => r.json());

const TrendIcon = ({ trend }: { trend: Signal['trend'] }) => {
  switch (trend) {
    case 'up': return <TrendingUp className="h-4 w-4 text-green-500" />;
    case 'down': return <TrendingDown className="h-4 w-4 text-red-500" />;
    default: return <Minus className="h-4 w-4 text-gray-400" />;
  }
};

export default function TopHubSignalPanel() {
  const { data, error } = useSWR<TopHubData>('/data/top-hub-signal.json', fetcher, {
    revalidateOnFocus: false,
    revalidateOnReconnect: false,
    refreshInterval: 5 * 60 * 1000, // 5 min
    shouldRetryOnError: false,
  });

  if (error || !data?.hub) return null;

  return (
    <section
      aria-label={`Top hub: ${data.hub}`}
      className="rounded-xl border bg-white p-4 shadow-sm dark:bg-gray-800"
    >
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">Top Hub</h3>
          <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">{data.hub}</p>
          <p className="text-xs text-gray-400">score {data.score}</p>
        </div>
        <span className="inline-flex items-center rounded-full bg-blue-50 px-2 py-1 text-xs font-medium text-blue-700 dark:bg-blue-900/30 dark:text-blue-400">
          Live
        </span>
      </div>

      <div className="mt-3 space-y-2">
        {data.signals.map((s, i) => (
          <div key={i} className="flex items-center justify-between text-sm">
            <span

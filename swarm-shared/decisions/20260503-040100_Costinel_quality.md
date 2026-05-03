# Costinel / quality

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Scope**: Add a lightweight, non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build/deploy time (zero HF API calls at runtime).

---

### 1) Data pipeline (Mac orchestration) — 15m
- Single API call to list/export top-hub graph data from knowledge-rag output (date-partitioned folder).
- Save as `data/top-hub/{date}/panel.json` with minimal schema:
  ```json
  {
    "hub": "MOC",
    "score": 0.94,
    "label": "Most-connected hub",
    "insight": "Review MOC before planning tasks — high centrality for cost governance signals.",
    "tags": ["#knowledge-rag", "#graph", "#hub"],
    "generatedAt": "2026-05-03T04:00:00Z"
  }
  ```
- Upload to CDN path (public dataset repo or project CDN):  
  `https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/top-hub/panel.json`
- Script (run on Mac):
  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  SHELL=/bin/bash

  DATE=$(date -u +%Y-%m-%d)
  OUT="data/top-hub/${DATE}/panel.json"
  mkdir -p "$(dirname "$OUT")"

  # Placeholder: replace with actual knowledge-rag query / graph extraction
  # Example: call your local rag CLI or API and project to top-hub
  python3 -c "
import json, datetime, os
out = os.environ['OUT']
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, 'w') as f:
    json.dump({
        'hub': 'MOC',
        'score': 0.94,
        'label': 'Most-connected hub',
        'insight': 'Review MOC before planning tasks — high centrality for cost governance signals.',
        'tags': ['#knowledge-rag', '#graph', '#hub'],
        'generatedAt': datetime.datetime.utcnow().isoformat() + 'Z'
    }, f, indent=2)
  "

  # Upload to CDN (example using gh or hub CLI; adapt to your deploy)
  # gh release upload --clobber "signals-$(date -u +%Y%m)" "$OUT"
  echo "CDN upload step: implement per your deploy (rsync/gh/hf)"
  ```

---

### 2) Build-time embed (CI) — 10m
- Add a build step that fetches the CDN panel JSON (single file) and writes it into static assets:
  - Next.js: `public/data/top-hub-panel.json`
  - Or generate a small TypeScript module: `src/data/topHubPanel.ts`
- If CDN fetch fails, CI falls back to last-known good local copy (committed default).
- Example CI step (bash):
  ```bash
  curl -fsSL "https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/top-hub/panel.json" \
    -o public/data/top-hub-panel.json || cp public/data/top-hub-panel.json.fallback public/data/top-hub-panel.json
  ```

---

### 3) Frontend component — 30m
Create `components/TopHubSignalPanel.tsx` (React + Tailwind):

```tsx
'use client';

import { useEffect, useState } from 'react';

type PanelData = {
  hub: string;
  score: number;
  label: string;
  insight: string;
  tags: string[];
  generatedAt: string;
};

export default function TopHubSignalPanel() {
  const [panel, setPanel] = useState<PanelData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // CDN-first, zero-runtime HF API
    fetch('/data/top-hub-panel.json', { cache: 'no-store' })
      .then((res) => res.json())
      .then((data) => {
        setPanel(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  if (loading) return <div className="h-20 animate-pulse bg-gray-100 rounded" />;
  if (!panel) return null;

  return (
    <section className="border rounded-lg bg-white p-4 shadow-sm max-w-xl">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">{panel.label}</h3>
          <p className="text-2xl font-bold text-blue-600">{panel.hub}</p>
          <p className="text-xs text-gray-500 mt-1">
            Updated {new Date(panel.generatedAt).toLocaleDateString()}
          </p>
        </div>
        <div className="flex-shrink-0">
          <span className="inline-flex items-center rounded-full bg-yellow-50 px-2.5 py-0.5 text-xs font-medium text-yellow-800">
            {Math.round(panel.score * 100)}% centrality
          </span>
        </div>
      </div>

      <p className="mt-2 text-sm text-gray-700">{panel.insight}</p>

      <div className="mt-3 flex flex-wrap gap-1">
        {panel.tags.map((tag) => (
          <span
            key={tag}
            className="text-xs bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded"
          >
            {tag}
          </span>
        ))}
      </div>
    </section>
  );
}
```

---

### 4) Placement in dashboard — 15m
- Insert panel into the main dashboard layout (non-blocking) near cost summary or top-right of sidebar.
- Example placement in `app/dashboard/page.tsx` (Next.js App Router):
  ```tsx
  import TopHubSignalPanel from '@/components/TopHubSignalPanel';

  export default function DashboardPage() {
    return (
      <main className="p-6">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2">
            {/* existing cost analytics */}
          </div>

          <aside className="space-y-4">
            <TopHubSignalPanel />
            {/* other signals / quick links */}
          </aside>
        </div>
      </main>
    );
  }
  ```

---

### 5) Testing & rollout — 20m
- Unit: verify component renders from local JSON.
- Integration: run CI build to ensure CDN fetch/fallback works.
- Canary: deploy to staging; confirm no runtime HF API calls (network tab).
- Monitoring: add lightweight log (frontend) if panel fails to load (non-critical).

---

### 6) Ops notes (cron / automation) — 10m
- Schedule Mac orchestration script via cron (daily 04:00 UTC):
  ```
  SHELL=/bin/bash
  0 4 * * * /opt/axentx/Costinel/scripts/export-top-hub-panel.sh >> /var/log/costinel-top-hub.log 2>&1
  ```
- Ensure script is executable: `chmod +x scripts/export-top-hub-panel.sh`.

---

**Total estimate**: ~1h 40m (including polish).  
**Outcome**: Non-blocking Top-Hub Signal Panel baked from CDN, zero runtime HF API, consistent with Costinel “Sense + Signal” philosophy and past patterns.

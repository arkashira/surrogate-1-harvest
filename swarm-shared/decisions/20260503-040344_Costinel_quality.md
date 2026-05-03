# Costinel / quality

## Final Implementation Plan  
**CDN-First Top-Hub Signal Panel — Ship in <2h, zero runtime HF API, non-blocking**

---

### 1) Architecture (CDN-first, deterministic)

```
┌─────────────────────┐
│  Build/Deploy step  │  (GitHub Action or pre-deploy)
│  generate-top-hub   │
└─────────┬───────────┘
          │
          ▼
  list_repo_tree (once) → top_hub.json
          │
          ▼
  Commit → public/data/top_hub.json
          │
          ▼
  CDN: https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/data/top_hub.json
          │
          ▼
  Costinel frontend fetches CDN URL (no auth, no HF API at runtime)
          │
          ▼
  TopHubSignalPanel renders MOC + context tags + links
```

- **Why this ships fast and is safe**  
  - No backend changes; pure frontend + build-time asset.  
  - CDN bypass pattern (`resolve/main/`) — no runtime HF API calls, no rate limits.  
  - Reuses existing knowledge-rag/graph insight (MOC) and tagging.  
  - Non-blocking UI; graceful fallbacks and local cache.  
  - Estimated: ~15m design, ~45m implementation, ~30m test/integration, ~30m buffer.

---

### 2) File layout (additions only)

```
/opt/axentx/Costinel/
├─ public/
│  └─ data/
│     └─ top_hub.json          # baked at build/deploy; CDN-accessible
├─ src/
│  ├─ components/
│  │  └─ TopHubSignalPanel.tsx
│  ├─ hooks/
│  │  └─ useTopHubSignal.ts
│  └─ types/
│     └─ topHub.ts
├─ scripts/
│  └─ generate-top-hub-cdn.js  # build-time generator
└─ .github/workflows/
   └─ bake-top-hub.yml         # optional CI automation
```

---

### 3) Data contract (public/data/top_hub.json)

```json
{
  "hub": "MOC",
  "title": "MOC — Most Connected Hub",
  "summary": "Highest betweenness across knowledge-rag graph; prioritize for contextual insights.",
  "score": 0.94,
  "updatedAt": "2026-05-03T04:15:00Z",
  "tags": ["#knowledge-rag", "#graph", "#hub"],
  "links": [
    { "label": "View insights", "href": "/knowledge-rag/hubs/MOC" },
    { "label": "Graph", "href": "/knowledge-rag/graph?hub=MOC" }
  ]
}
```

- Baked by CI (or ops script) using `list_repo_tree` once per deploy, then committed to repo and served via CDN.  
- Served via `https://huggingface.co/datasets/axentx/costinel-signals/resolve/main/data/top_hub.json` (or your CDN path) — no Authorization header required.

---

### 4) Build-time script (scripts/generate-top-hub-cdn.js)

```js
#!/usr/bin/env node
/**
 * Generate top_hub.json for CDN-first consumption.
 * Run during CI/CD or manually before deploy.
 * Uses HF API once (list_repo_tree) → writes static JSON to public/data/
 */

import { writeFileSync, mkdirSync } from 'node:fs';
import { HfApi } from '@huggingface/hub';
import dotenv from 'dotenv';

dotenv.config();

const HF_REPO = process.env.HF_REPO || 'axentx/costinel-signals';
const OUT_PATH = 'public/data/top_hub.json';

async function main() {
  const api = new HfApi();
  // Example: derive top hub from repo tree or metadata.
  // Replace with your actual logic (e.g., graph analysis export).
  const tree = await api.listRepoTree({ repoId: HF_REPO, repoType: 'dataset' });
  const files = tree
    .filter((t) => t.type === 'file')
    .map((t) => t.path)
    .filter(Boolean);

  // Placeholder heuristic: pick most frequent prefix or known hub name.
  // In practice, replace with your exported graph metric.
  const hub = 'MOC';
  const score = 0.94;

  const payload = {
    hub,
    title: `${hub} — Most Connected Hub`,
    summary: 'Highest betweenness across knowledge-rag graph; prioritize for contextual insights.',
    score,
    updatedAt: new Date().toISOString(),
    tags: ['#knowledge-rag', '#graph', '#hub'],
    links: [
      { label: 'View insights', href: `/knowledge-rag/hubs/${hub}` },
      { label: 'Graph', href: `/knowledge-rag/graph?hub=${hub}` }
    ]
  };

  mkdirSync('public/data', { recursive: true });
  writeFileSync(OUT_PATH, JSON.stringify(payload, null, 2), 'utf8');
  console.log(`Wrote ${OUT_PATH}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

- Make executable: `chmod +x scripts/generate-top-hub-cdn.js`.  
- Add to CI (optional) or run locally before deploy.

---

### 5) Types (src/types/topHub.ts)

```ts
export interface TopHubLink {
  label: string;
  href: string;
}

export interface TopHubSignal {
  hub: string;
  title: string;
  summary: string;
  score: number;
  updatedAt: string;
  tags: string[];
  links: TopHubLink[];
}
```

---

### 6) Hook: CDN-first fetch with stale-while-revalidate (src/hooks/useTopHubSignal.ts)

```ts
// src/hooks/useTopHubSignal.ts
import { useEffect, useState, useCallback } from 'react';
import type { TopHubSignal } from '../types/topHub';

const CDN_TOP_HUB_URL =
  process.env.REACT_APP_TOP_HUB_CDN_URL || '/data/top_hub.json';

export function useTopHubSignal(enabled = true) {
  const [data, setData] = useState<TopHubSignal | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<Error | null>(null);

  const fetchSignal = useCallback(async () => {
    if (!enabled) return;
    try {
      const res = await fetch(CDN_TOP_HUB_URL, { cache: 'no-cache' });
      if (!res.ok) throw new Error(`Failed to fetch top-hub: ${res.status}`);
      const json = (await res.json()) as TopHubSignal;
      setData(json);
      setError(null);
      try {
        localStorage.setItem('costinel:top-hub', JSON.stringify(json));
      } catch {
        // ignore storage errors
      }
    } catch (err) {
      setError(err as Error);
      try {
        const cached = localStorage.getItem('costinel:top-hub');
        if (cached) setData(JSON.parse(cached));
      } catch {
        setData(null);
      }
    } finally {
      setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    fetchSignal();
    const interval = setInterval(fetchSignal, 15 * 60 * 1000);
    return () => clearInterval(interval);
  }, [fetchSignal]);

  return { data, loading, error, refetch: fetchSignal };
}
```

---

### 7) Component: TopHubSignalPanel (src/components/TopHubSignalPanel.tsx)

```tsx
// src/components/TopHubSignalPanel.tsx
import React from 'react';
import { useTopHubSignal } from '../hooks/useTopHubSignal';

export const TopHubSignalPanel: React.FC<{ enabled?: boolean }> = ({ enabled =

# Costinel / frontend

**Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)**

**Scope**  
Add a lightweight, non-blocking Top-Hub Signal Panel to the Costinel frontend that surfaces the most-connected hub (e.g., “MOC”) using CDN-first data baked at build/deploy time. Zero Hugging Face API calls at runtime. Uses `https://huggingface.co/datasets/.../resolve/main/...` CDN bypass pattern plus a build-time bake step for correctness and freshness.

---

### Why this ships fast and safely
- No backend changes — pure frontend + build script.
- Runtime: CDN fetch only (no auth, no rate limits).
- Build/deploy: single HF API list call (outside rate-limit window) + CDN fetch to bake latest data into `public/data/top-hub.json`.
- Non-blocking UI: silent fail if CDN fetch fails; no impact on main views.
- Reuses existing patterns: top-hub insight and HF CDN bypass.

---

### File layout (additions only)

```
/opt/axentx/Costinel/
├─ public/
│  └─ data/
│     └─ top-hub.json            # baked at build/deploy time
├─ src/
│  ├─ components/
│  │  └─ TopHubSignalPanel.tsx   # new component
│  ├─ hooks/
│  │  └─ useCDNTopHub.ts         # new hook
│  └─ pages/Dashboard.tsx        # mount point (or similar)
├─ scripts/
│  └─ bake-top-hub.js            # build-time script (Node)
└─ package.json
```

---

### 1) Build-time script — `scripts/bake-top-hub.js`

Runs in CI or pre-build. Uses HF API **once** to list date folders, then CDN-fetches the latest `top-hub.json` and writes it to `public/data/top-hub.json`.

```js
#!/usr/bin/env node
/**
 * Bake top-hub.json into public/data/ for CDN-first runtime.
 * Uses HF API sparingly (single list call) then CDN fetch.
 */
const fs = require('fs');
const path = require('path');
const https = require('https');

const REPO = 'datasets/axentx/costinel-knowledge'; // adjust if needed
const FOLDER = 'top-hub'; // e.g. top-hub/YYYY-MM-DD/
const OUT_DIR = path.resolve(__dirname, '../public/data');
const OUT_FILE = path.join(OUT_DIR, 'top-hub.json');

function httpsGet(url) {
  return new Promise((resolve, reject) => {
    https
      .get(url, (res) => {
        if (res.statusCode === 302 || res.statusCode === 301) {
          return resolve(httpsGet(res.headers.location));
        }
        let data = '';
        res.on('data', (chunk) => (data += chunk));
        res.on('end', () => resolve(data));
      })
      .on('error', reject);
  });
}

async function listDates() {
  // HF datasets repo file listing via API (no auth for public)
  const apiUrl = `https://huggingface.co/api/datasets/${REPO}/tree?path=${encodeURIComponent(FOLDER)}`;
  const raw = await httpsGet(apiUrl);
  const items = JSON.parse(raw);
  if (!Array.isArray(items) || items.length === 0) {
    throw new Error(`No items found in ${FOLDER}/`);
  }
  // pick latest date folder by name (YYYY-MM-DD)
  const sorted = items
    .map((i) => i.path)
    .filter((p) => /^\d{4}-\d{2}-\d{2}$/.test(p.split('/').pop()))
    .sort()
    .reverse();
  if (sorted.length === 0) throw new Error('No date folders found');
  return sorted[0]; // e.g. top-hub/2026-05-03
}

async function bake() {
  try {
    const datePath = await listDates(); // e.g. top-hub/2026-05-03
    // CDN URL to the baked JSON (resolve via HuggingFace CDN)
    const cdnUrl = `https://huggingface.co/datasets/${REPO}/resolve/main/${datePath}/top-hub.json`;
    const raw = await httpsGet(cdnUrl);
    const parsed = JSON.parse(raw);

    // Ensure minimal schema
    if (!parsed.hub || typeof parsed.score !== 'number') {
      throw new Error('Invalid top-hub.json schema');
    }

    if (!fs.existsSync(OUT_DIR)) fs.mkdirSync(OUT_DIR, { recursive: true });
    fs.writeFileSync(OUT_FILE, JSON.stringify(parsed, null, 2), 'utf8');
    console.log(`Baked top-hub.json to ${OUT_FILE}`);
  } catch (err) {
    console.error('Failed to bake top-hub.json:', err.message);
    process.exitCode = 1;
  }
}

if (require.main === module) {
  bake();
}
```

Add to CI/CD (e.g., run before `npm run build`) and commit or stage `public/data/top-hub.json` as part of deploy artifacts.

---

### 2) CDN fetch hook — `src/hooks/useCDNTopHub.ts`

Encapsulates fetch logic, caching, and error handling.

```ts
import { useEffect, useState } from 'react';

export interface HubData {
  hub: string;
  label: string;
  score: number;
  connections: number;
  updated: string;
  insight: string;
  actions: Array<{ label: string; href: string }>;
}

const CDN_URL = '/data/top-hub.json'; // served from public/ (CDN at deploy)

export default function useCDNTopHub() {
  const [hub, setHub] = useState<HubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(CDN_URL, { cache: 'no-cache' })
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load top-hub data (${res.status})`);
        return res.json();
      })
      .then((data) => {
        if (!cancelled) {
          setHub(data);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          console.warn(err);
          setError(err.message);
          setHub(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return { hub, loading, error };
}
```

---

### 3) TopHubSignalPanel component — `src/components/TopHubSignalPanel.tsx`

Uses the hook; silent fail when no data (non-blocking).

```tsx
import React from 'react';
import useCDNTopHub from '../hooks/useCDNTopHub';
import './TopHubSignalPanel.css';

const TopHubSignalPanel: React.FC = () => {
  const { hub, loading, error } = useCDNTopHub();

  if (loading) {
    return (
      <div className="top-hub-panel loading">
        <span className="spinner" /> Loading top hub...
      </div>
    );
  }

  if (error || !hub) {
    // Non-blocking: render nothing on failure
    return null;
  }

  return (
    <div className="top-hub-panel" title={`Updated ${new Date(hub.updated).toLocaleString()}`}>
      <div className="top-hub-header">
        <span className="top-hub-badge">Top Hub</span>
        <strong className="top-hub-name">{hub.label}</strong>
        <span className="top-hub-score">{Math.round(hub.score * 100)}% centrality</span>
      </div>
      <p className="top-hub-insight">{hub.insight}</p>
      <div className="top

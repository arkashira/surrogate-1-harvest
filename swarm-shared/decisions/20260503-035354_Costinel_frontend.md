# Costinel / frontend

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**: Add a non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data baked at build time — zero HuggingFace API calls at runtime.

---

### 1. Build-time data pipeline (Mac/CI)
- Single API call (after rate-limit window) to list date folder:  
  `list_repo_tree(path="knowledge-rag/top-hub/2026-04-27", recursive=False)`
- Save minimal JSON: `{ "hub": "MOC", "score": 0.94, "updated": "2026-04-27T12:00:00Z", "links": [...] }`
- Upload to CDN:  
  `https://huggingface.co/datasets/AXENTX/knowledge-rag/resolve/main/top-hub/2026-04-27/hub.json`
- Embed filename in repo (or fetch at build): `src/data/top-hub.json` (committed or CI-copied)

### 2. Runtime behavior (Costinel frontend)
- Fetch via CDN URL (no Authorization header) at app init or panel mount.
- Fail-open: if CDN fails, render nothing (non-blocking).
- Cache: `localStorage` with 6h TTL to avoid repeat fetches.

### 3. UI placement
- New card in existing dashboard sidebar or top bar:  
  `Top Hub Signal — MOC (94% relevance)`
- Click opens modal with related docs (from baked `links`).

---

## Code Changes

### A. Add static data file (committed)
```json
// src/data/top-hub.json
{
  "hub": "MOC",
  "score": 0.94,
  "updated": "2026-04-27T12:00:00Z",
  "links": [
    { "title": "Cloud Cost Governance Playbook", "url": "/docs/governance-playbook" },
    { "title": "MOC Runbook", "url": "/docs/moc-runbook" }
  ]
}
```

### B. Create TopHubPanel component
```tsx
// src/components/TopHubPanel.tsx
import { useEffect, useState } from 'react';
import './TopHubPanel.css';

interface HubData {
  hub: string;
  score: number;
  updated: string;
  links: Array<{ title: string; url: string }>;
}

const CDN_URL = 'https://huggingface.co/datasets/AXENTX/knowledge-rag/resolve/main/top-hub/2026-04-27/hub.json';
const LOCAL_KEY = 'costinel:top-hub';
const TTL_MS = 6 * 60 * 60 * 1000; // 6h

export default function TopHubPanel() {
  const [data, setData] = useState<HubData | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    async function load() {
      try {
        const cached = localStorage.getItem(LOCAL_KEY);
        if (cached) {
          const { ts, value } = JSON.parse(cached);
          if (Date.now() - ts < TTL_MS) {
            setData(value);
            return;
          }
        }

        const res = await fetch(CDN_URL, { cache: 'no-cache' });
        if (!res.ok) throw new Error('CDN fetch failed');
        const value: HubData = await res.json();
        setData(value);
        localStorage.setItem(LOCAL_KEY, JSON.stringify({ ts: Date.now(), value }));
      } catch {
        // fail-open: silently ignore
        setData(null);
      }
    }

    load();
  }, []);

  if (!data) return null;

  return (
    <>
      <button className="top-hub-trigger" onClick={() => setOpen(true)} title="Top connected hub">
        <span className="hub-badge">{data.hub}</span>
        <span className="hub-score">{Math.round(data.score * 100)}%</span>
      </button>

      {open && (
        <div className="top-hub-modal-backdrop" onClick={() => setOpen(false)}>
          <div className="top-hub-modal" onClick={(e) => e.stopPropagation()}>
            <header>
              <h3>Top Hub: {data.hub}</h3>
              <small>Updated {new Date(data.updated).toLocaleDateString()}</small>
            </header>
            <section>
              <h4>Related</h4>
              <ul>
                {data.links.map((l, i) => (
                  <li key={i}>
                    <a href={l.url} target="_blank" rel="noopener noreferrer">
                      {l.title}
                    </a>
                  </li>
                ))}
              </ul>
            </section>
            <button className="close-btn" onClick={() => setOpen(false)}>Close</button>
          </div>
        </div>
      )}
    </>
  );
}
```

### C. Minimal styles
```css
/* src/components/TopHubPanel.css */
.top-hub-trigger {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: 1px solid #e2e8f0;
  background: #fff;
  border-radius: 8px;
  font-size: 13px;
  cursor: pointer;
  transition: box-shadow 0.15s;
}
.top-hub-trigger:hover {
  box-shadow: 0 2px 8px rgba(0,0,0,0.08);
}
.hub-badge {
  font-weight: 700;
  color: #2563eb;
}
.hub-score {
  color: #64748b;
}

.top-hub-modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.top-hub-modal {
  background: #fff;
  padding: 20px 24px;
  border-radius: 12px;
  max-width: 420px;
  width: 90%;
}
.top-hub-modal ul {
  list-style: none;
  padding: 0;
  margin: 12px 0 0;
}
.top-hub-modal li a {
  color: #2563eb;
  text-decoration: none;
}
.top-hub-modal li a:hover {
  text-decoration: underline;
}
.close-btn {
  margin-top: 16px;
  padding: 8px 16px;
  border: 1px solid #e2e8f0;
  background: #f8fafc;
  border-radius: 6px;
  cursor: pointer;
}
```

### D. Mount in dashboard
```tsx
// src/pages/Dashboard.tsx (or wherever sidebar/topbar lives)
import TopHubPanel from '../components/TopHubPanel';

export default function Dashboard() {
  return (
    <div>
      {/* existing content */}
      <header className="dashboard-header">
        {/* other controls */}
        <TopHubPanel />
      </header>
      {/* rest of dashboard */}
    </div>
  );
}
```

---

## Build/CI step (optional, if not committing JSON)
```bash
# Mac/CI: one-time after rate-limit window
node scripts/fetch-top-hub.js   # writes src/data/top-hub.json
git add src/data/top-hub.json && git commit -m "chore: update top-hub CDN snapshot"
```

---

## Verification
- Start dev server: panel appears only when CDN JSON reachable.
- Disable network: panel gracefully hides (non-blocking).
- localStorage caching prevents repeated CDN hits.

**ETA**:

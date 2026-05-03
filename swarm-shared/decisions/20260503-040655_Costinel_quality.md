# Costinel / quality

## Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Scope**: Add a lightweight, non-blocking Top-Hub Signal Panel to Costinel frontend that surfaces the most-connected hub (e.g., "MOC") using CDN-first data (zero runtime HF API). Deterministic, cache-friendly, and deployable in <2h.

---

### 1) Architecture (CDN-first, zero HF API at runtime)
- **Data source**: `knowledge-rag` produces `top-hub.json` (deterministic; updated by batch job).
- **Hosting**: Commit `public/data/top-hub.json` to repo (or serve via CDN path `https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub.json`).
- **Frontend**: Fetch via `fetch('/data/top-hub.json')` (or CDN URL) at build-time or runtime with stale-while-revalidate.
- **No HF API calls during app runtime** → avoids 429s and quota.

---

### 2) File changes

#### A) Create public data file (deterministic top-hub)
`public/data/top-hub.json`
```json
{
  "hub": "MOC",
  "label": "MOC",
  "connections": 12743,
  "description": "Most-connected hub: Multi-Org Cost governance signals and cross-account policy graph.",
  "tags": ["#knowledge-rag", "#graph", "#hub"],
  "updated": "2026-05-03T04:10:00Z",
  "source": "knowledge-rag/batch-2026-05-03",
  "url": "https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/graph/2026-05-03/top-hub.json"
}
```

#### B) Add TopHubSignalPanel component
`src/components/TopHubSignalPanel.tsx`
```tsx
import { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

interface TopHub {
  hub: string;
  label: string;
  connections: number;
  description: string;
  tags: string[];
  updated: string;
  source: string;
  url: string;
}

interface TopHubSignalPanelProps {
  cdnUrl?: string;
  localPath?: string;
  refreshIntervalMs?: number;
}

export function TopHubSignalPanel({
  cdnUrl = 'https://huggingface.co/datasets/axentx/costinel-knowledge/resolve/main/top-hub.json',
  localPath = '/data/top-hub.json',
  refreshIntervalMs = 300_000,
}: TopHubSignalPanelProps) {
  const [hub, setHub] = useState<TopHub | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        // Prefer local in production for speed; fallback to CDN if needed.
        const res = await fetch(localPath, { cache: 'force-cache' });
        if (!res.ok) throw new Error('local fetch failed');
        const data = (await res.json()) as TopHub;
        if (!cancelled) {
          setHub(data);
          setLoading(false);
        }
      } catch {
        // CDN fallback (zero-auth public URL)
        const res = await fetch(cdnUrl, { cache: 'force-cache' });
        if (res.ok) {
          const data = (await res.json()) as TopHub;
          if (!cancelled) {
            setHub(data);
          }
        }
        if (!cancelled) setLoading(false);
      }
    }

    load();
    const id = setInterval(load, refreshIntervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [cdnUrl, localPath, refreshIntervalMs]);

  if (loading && !hub) {
    return (
      <div className="top-hub-panel loading" aria-busy="true">
        <span className="pulse-dot" />
        Loading signals…
      </div>
    );
  }

  if (!hub) return null;

  return (
    <a
      href={hub.url}
      target="_blank"
      rel="noopener noreferrer"
      className="top-hub-panel"
      title={`Updated ${new Date(hub.updated).toLocaleString()}`}
    >
      <div className="top-hub-panel__header">
        <span className="top-hub-panel__label">Top Hub</span>
        <span className="top-hub-panel__name">{hub.label}</span>
      </div>
      <p className="top-hub-panel__desc">{hub.description}</p>
      <div className="top-hub-panel__meta">
        <span className="top-hub-panel__stat">{hub.connections.toLocaleString()} connections</span>
        <span className="top-hub-panel__tags">
          {hub.tags.map((t) => (
            <span key={t} className="top-hub-panel__tag">
              {t}
            </span>
          ))}
        </span>
      </div>
    </a>
  );
}
```

#### C) Panel styles
`src/components/TopHubSignalPanel.css`
```css
.top-hub-panel {
  display: block;
  position: relative;
  padding: 12px 14px;
  border-radius: 8px;
  background: linear-gradient(135deg, #0ea5e91a 0%, #10b9811a 100%);
  border: 1px solid rgba(14, 165, 233, 0.12);
  color: #0f172a;
  text-decoration: none;
  font-family: inherit;
  transition: transform 160ms ease, box-shadow 160ms ease, border-color 160ms ease;
  max-width: 320px;
}

.top-hub-panel:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 18px rgba(14, 165, 233, 0.08);
  border-color: rgba(14, 165, 233, 0.28);
}

.top-hub-panel__header {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 4px;
}

.top-hub-panel__label {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #0369a1;
}

.top-hub-panel__name {
  font-size: 15px;
  font-weight: 700;
  color: #0c4a6e;
}

.top-hub-panel__desc {
  margin: 6px 0;
  font-size: 13px;
  color: #334155;
  line-height: 1.4;
}

.top-hub-panel__meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  color: #475569;
}

.top-hub-panel__stat {
  font-weight: 600;
  color: #0c4a6e;
}

.top-hub-panel__tags {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.top-hub-panel__tag {
  background: rgba(14, 165, 233, 0.08);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 11px;
  color: #0369a1;
}

.top-hub-panel.loading {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 

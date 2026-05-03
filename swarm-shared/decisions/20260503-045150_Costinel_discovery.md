# Costinel / discovery

## Implementation Plan — Top Hub Signal Panel (CDN-first)

**Scope**: Frontend-only addition to Costinel dashboard.  
**Effort**: ~60–90 minutes.  
**Mechanism**: CDN JSON fetch (no auth, no backend) with local fallback and client cache.  
**Goal**: Show the most-connected hub (e.g., “MOC”) and top 3 related docs as actionable signals on the dashboard.

---

### 1) File changes

- `src/components/TopHubSignalPanel.tsx` — new component (CDN fetch + local fallback + cache).  
- `src/pages/Dashboard.tsx` — import and mount panel in the top-right of the main metrics area.  
- `public/data/top-hub-moc.json` — local fallback payload (checked into repo).  
- `tsconfig.json` / `vite-env.d.ts` — no changes required.

---

### 2) CDN payload contract (public)

URL (example):  
`https://huggingface.co/datasets/axentx/top-hubs/resolve/main/moc/latest.json`

Shape (must match fallback):
```json
{
  "hub": "MOC",
  "score": 0.94,
  "updated": "2026-05-03T04:45:00Z",
  "related": [
    { "title": "Cost Anomaly Playbook", "url": "https://docs.axentx/cost-anomaly", "score": 0.91 },
    { "title": "RI Coverage Quickwin", "url": "https://docs.axentx/ri-coverage", "score": 0.87 },
    { "title": "Tag Governance Policy", "url": "https://docs.axentx/tag-policy", "score": 0.83 }
  ]
}
```

---

### 3) Component implementation

`src/components/TopHubSignalPanel.tsx`
```tsx
import { useEffect, useState } from "react";
import "./TopHubSignalPanel.css";

type RelatedDoc = {
  title: string;
  url: string;
  score: number;
};

type HubPayload = {
  hub: string;
  score: number;
  updated: string;
  related: RelatedDoc[];
};

const CDN_URL =
  "https://huggingface.co/datasets/axentx/top-hubs/resolve/main/moc/latest.json";
const FALLBACK_URL = "/data/top-hub-moc.json";
const CACHE_KEY = "top-hub-signal:v1";
const CACHE_TTL_MS = 5 * 60 * 1000; // 5m

function isFresh(cached: { ts: number }): boolean {
  return Date.now() - cached.ts < CACHE_TTL_MS;
}

export default function TopHubSignalPanel() {
  const [payload, setPayload] = useState<HubPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        // 1) try in-memory cache via localStorage
        const raw = localStorage.getItem(CACHE_KEY);
        if (raw) {
          const cached = JSON.parse(raw);
          if (isFresh(cached) && cached.payload) {
            setPayload(cached.payload);
            setLoading(false);
            return;
          }
        }

        // 2) CDN-first fetch (no Authorization header)
        const res = await fetch(CDN_URL, { cache: "no-store" });
        let data: HubPayload;

        if (res.ok) {
          data = await res.json();
        } else {
          // 3) local fallback
          const fb = await fetch(FALLBACK_URL, { cache: "no-store" });
          if (!fb.ok) throw new Error("CDN and fallback unavailable");
          data = await fb.json();
        }

        // validate minimal shape
        if (!data?.hub || !Array.isArray(data?.related)) {
          throw new Error("Invalid payload shape");
        }

        localStorage.setItem(
          CACHE_KEY,
          JSON.stringify({ payload: data, ts: Date.now() })
        );
        setPayload(data);
      } catch (err: any) {
        setError(err?.message || "Failed to load hub signal");
      } finally {
        setLoading(false);
      }
    }

    load();
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel loading">
        <span className="spinner" />
        Loading signals…
      </div>
    );
  }

  if (error || !payload) {
    return (
      <div className="top-hub-panel error">
        <strong>Signal unavailable</strong>
        <small>{error}</small>
      </div>
    );
  }

  return (
    <div className="top-hub-panel">
      <div className="header">
        <span className="badge">Top Hub</span>
        <strong className="hub-name">{payload.hub}</strong>
        <span className="score" title="connection score">
          {Math.round(payload.score * 100)}%
        </span>
      </div>

      <div className="related">
        {payload.related.slice(0, 3).map((doc, idx) => (
          <a
            key={idx}
            className="doc"
            href={doc.url}
            target="_blank"
            rel="noopener noreferrer"
            title={`Score: ${Math.round(doc.score * 100)}%`}
          >
            <span className="doc-title">{doc.title}</span>
            <span className="doc-score">{Math.round(doc.score * 100)}%</span>
          </a>
        ))}
      </div>

      <div className="footer">
        <small>Updated {new Date(payload.updated).toLocaleDateString()}</small>
      </div>
    </div>
  );
}
```

`src/components/TopHubSignalPanel.css`
```css
.top-hub-panel {
  border: 1px solid #e6e9ef;
  border-radius: 10px;
  padding: 14px 16px;
  background: #fff;
  min-width: 260px;
  max-width: 320px;
  box-shadow: 0 1px 3px rgba(16,24,40,0.06);
}

.top-hub-panel.loading,
.top-hub-panel.error {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #6b7280;
  font-size: 13px;
}

.spinner {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  border: 2px solid #e6e9ef;
  border-top-color: #2563eb;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.badge {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #fff;
  background: #0ea5e9;
  padding: 2px 6px;
  border-radius: 4px;
}

.hub-name {
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
}

.score {
  margin-left: auto;
  font-size: 13px;
  color: #16a34a;
  font-weight: 600;
}

.related {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.doc {
  display: flex;
  align-items: center;
  justify-content: space-between;

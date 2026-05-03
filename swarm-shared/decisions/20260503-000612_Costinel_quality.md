# Costinel / quality

## Implementation Plan — Costinel “Top-Hub Signal” Card (Read-Only)

**Scope**: Read-only frontend card (≤2h)  
**Principle**: “Sense + Signal — ไม่ Execute” (strictly no writes, no runtime mutations, no self-execution)  
**Goal**: Surface the most-connected hub (e.g., “MOC”) with contextual insights from knowledge-rag; zero backend changes; pure frontend/static-data integration.

---

### 1) Design (5 min)
- Place a new card in the dashboard sidebar or top-row:
  - Title: **Top-Hub Signal**
  - Subtitle: *Most-connected hub (graph centrality)*
  - Body:
    - Hub name + icon (e.g., “MOC”)
    - Short insight (1–2 sentences) from knowledge-rag
    - Timestamp of last insight refresh
    - “View context” link (opens modal or external doc)
- Visual: neutral, information-only; no buttons that trigger actions.

### 2) Data Source (read-only) (10 min)
- Use a static JSON file committed to repo (or fetched from a read-only CDN path) so the UI remains purely declarative:
  - Path: `/data/top-hub.json`
  - Schema:
    ```json
    {
      "hub": "MOC",
      "insight": "MOC is the most-connected hub across cost governance policies; centralizes cross-account tagging and anomaly detection signals.",
      "updatedAt": "2026-05-03T08:00:00Z",
      "contextUrl": "https://axentx.internal/knowledge-rag/hubs/MOC"
    }
    ```
- Update cadence: ops or docs process updates this file (outside this PR). Frontend only reads.

### 3) Implementation (60–90 min)

#### Add static data
```bash
# /opt/axentx/Costinel
mkdir -p public/data
cat > public/data/top-hub.json <<'JSON'
{
  "hub": "MOC",
  "insight": "MOC is the most-connected hub across cost governance policies; centralizes cross-account tagging and anomaly detection signals.",
  "updatedAt": "2026-05-03T08:00:00Z",
  "contextUrl": "https://axentx.internal/knowledge-rag/hubs/MOC"
}
JSON
```

#### React component (TypeScript) — `src/components/TopHubSignalCard.tsx`
```tsx
import { useEffect, useState } from 'react';
import './TopHubSignalCard.css';

interface TopHubPayload {
  hub: string;
  insight: string;
  updatedAt: string;
  contextUrl: string;
}

export default function TopHubSignalCard() {
  const [data, setData] = useState<TopHubPayload | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Read-only fetch from static CDN/public path
    fetch('/data/top-hub.json', { cache: 'no-cache' })
      .then((res) => {
        if (!res.ok) throw new Error('Failed to load top-hub signal');
        return res.json();
      })
      .then((json) => {
        setData(json);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="top-hub-card loading" aria-busy="true">
        Loading signal…
      </div>
    );
  }

  if (!data) {
    return null; // Fail silently — strictly read-only, no self-healing mutations
  }

  return (
    <div className="top-hub-card" role="region" aria-label="Top-Hub Signal">
      <div className="top-hub-header">
        <span className="top-hub-badge">{data.hub}</span>
        <span className="top-hub-meta">
          Updated {new Date(data.updatedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
        </span>
      </div>
      <p className="top-hub-insight">{data.insight}</p>
      <a
        className="top-hub-context"
        href={data.contextUrl}
        target="_blank"
        rel="noopener noreferrer"
        title="Open context (read-only)"
      >
        View context
      </a>
    </div>
  );
}
```

#### Styles — `src/components/TopHubSignalCard.css`
```css
.top-hub-card {
  border: 1px solid #e6e9ee;
  border-radius: 8px;
  padding: 14px 16px;
  background: #fff;
  max-width: 320px;
}

.top-hub-card.loading {
  color: #9aa4b2;
  font-size: 13px;
}

.top-hub-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.top-hub-badge {
  font-weight: 700;
  font-size: 16px;
  color: #0b5cff;
  text-transform: uppercase;
  letter-spacing: 0.4px;
}

.top-hub-meta {
  font-size: 12px;
  color: #9aa4b2;
  white-space: nowrap;
}

.top-hub-insight {
  margin: 0 0 8px;
  font-size: 13px;
  line-height: 1.5;
  color: #344054;
}

.top-hub-context {
  font-size: 12px;
  color: #0b5cff;
  text-decoration: none;
}

.top-hub-context:hover {
  text-decoration: underline;
}
```

#### Mount in dashboard (example)
Add to an existing dashboard layout (e.g., sidebar or top-row grid):
```tsx
import TopHubSignalCard from '@/components/TopHubSignalCard';

// Inside your dashboard JSX:
<aside>
  <TopHubSignalCard />
  {/* other widgets */}
</aside>
```

### 4) Validation (5 min)
- Start dev server and verify:
  - Card renders with hub name and insight.
  - No network requests to `/api/` (only `/data/top-hub.json`).
  - No console errors.
  - Links open in new tab (read-only).
- Confirm no state mutations or POST/PUT calls from this component.

### 5) Rollout (5 min)
- Commit `public/data/top-hub.json` and `TopHubSignalCard` files.
- Deploy static assets (CDN) — no backend restart required.
- Tag: `#knowledge-rag #graph #hub`

---

**Estimated time**: ~100 min (design + implementation + validation)  
**Risk**: Low — read-only, no schema or backend changes.  
**Outcome**: Costinel now surfaces the most-connected hub with contextual insight while honoring “Sense + Signal — ไม่ Execute”.

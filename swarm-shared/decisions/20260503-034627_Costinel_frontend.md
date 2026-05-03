# Costinel / frontend

### Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Goal**  
Add a non-blocking Top-Hub Signal Panel to the Costinel dashboard that surfaces the most-connected hub (e.g., "MOC") using CDN-first, baked data with **zero HuggingFace API calls at runtime**.

---

### Scope & Decisions (resolve contradictions)
- **Data source**: Use baked JSON committed to repo and served via CDN (or same origin).  
  - *Why*: Both candidates agree; Candidate 2’s explicit field list and example are more actionable.
- **Backend route**: Provide `/api/top-hub` that reads local baked JSON and sets cache headers.  
  - *Why*: More robust than direct CDN fetch in frontend (avoids CORS, enables cache control, graceful fallback).
- **Frontend**: Lightweight React component with loading and empty (non-blocking) states.  
  - *Why*: Candidate 2’s skeleton + graceful fallback is production-ready; Candidate 1’s inline fetch is acceptable but less resilient.
- **CDN path**: Prefer same-origin `/api/top-hub` in production; during build, `public/data/top-hub.json` can be synced to CDN (e.g., `https://cdn.example.com/top-hub.json`).  
  - *Why*: Balances simplicity (Candidate 1) and control (Candidate 2).

---

### Implementation Steps (≤2h)

1. **Prepare baked data (10–15m)**
   - Run `granite-business-research.sh` and `knowledge-rag` offline to identify top hub and insights.
   - Create `public/data/top-hub.json` with canonical fields.

2. **Add backend route (15–20m)**
   - Add `server/routes/topHub.js` (Express) to serve `/api/top-hub` with cache headers and graceful fallback.
   - Register route in main app.

3. **Add frontend component (30–40m)**
   - Create `src/components/TopHubSignalPanel.tsx` with loading, error-tolerant, and empty states.
   - Add minimal CSS.

4. **Integrate into dashboard (15–20m)**
   - Import and place `TopHubSignalPanel` in the dashboard layout (sidebar or top-row).
   - Verify non-blocking behavior and performance.

5. **Build/deploy & CDN sync (10–15m)**
   - Ensure `public/data/top-hub.json` is included in build output.
   - Optionally sync to CDN and configure `/api/top-hub` to proxy or redirect with cache.

---

### Code Snippets

#### 1) Baked data — `public/data/top-hub.json`
```json
{
  "hub": "MOC",
  "rank": 1,
  "connections": 127,
  "insight": "Most-connected hub driving cross-team cost visibility. Prioritize tagging alignment to amplify signal reach.",
  "updatedAt": "2026-05-03T04:00:00.000Z"
}
```

#### 2) Backend route — `server/routes/topHub.js`
```js
const express = require('express');
const fs = require('fs').promises;
const path = require('path');
const router = express.Router();

router.get('/api/top-hub', async (req, res) => {
  try {
    const filePath = path.join(__dirname, '../../public/data/top-hub.json');
    const raw = await fs.readFile(filePath, 'utf8');
    const data = JSON.parse(raw);

    // CDN-first: short cache to allow quick updates on redeploy
    res.set('Cache-Control', 'public, max-age=600, stale-while-revalidate=3600');
    res.json({ ok: true, data });
  } catch (err) {
    // Non-blocking: return empty payload; frontend handles gracefully
    res.status(200).json({ ok: true, data: null });
  }
});

module.exports = router;
```

Register in main app (e.g., `server/index.js`):
```js
const topHubRoutes = require('./routes/topHub');
app.use(topHubRoutes);
```

#### 3) Frontend component — `src/components/TopHubSignalPanel.tsx`
```tsx
import React, { useEffect, useState } from 'react';
import './TopHubSignalPanel.css';

interface TopHubData {
  hub: string;
  rank: number;
  connections: number;
  insight: string;
  updatedAt: string;
}

interface Payload {
  ok: boolean;
  data: TopHubData | null;
}

const TopHubSignalPanel: React.FC = () => {
  const [data, setData] = useState<TopHubData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    fetch('/api/top-hub', { cache: 'no-store' })
      .then((r) => r.json())
      .then((json: Payload) => {
        if (mounted && json.ok && json.data) setData(json.data);
      })
      .catch(() => {
        // swallow — non-blocking
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    return () => {
      mounted = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="top-hub-panel skeleton">
        <div className="sh-title" />
        <div className="sh-badge" />
        <div className="sh-text" />
      </div>
    );
  }

  if (!data) {
    return null; // non-blocking: render nothing if unavailable
  }

  return (
    <div className="top-hub-panel">
      <div className="top-hub-header">
        <span className="top-hub-badge">Top Hub</span>
        <span className="top-hub-rank">#{data.rank}</span>
      </div>
      <h3 className="top-hub-name">{data.hub}</h3>
      <p className="top-hub-meta">{data.connections} connections</p>
      <p className="top-hub-insight">{data.insight}</p>
      <small className="top-hub-updated">
        Updated {new Date(data.updatedAt).toLocaleDateString()}
      </small>
    </div>
  );
};

export default TopHubSignalPanel;
```

#### 4) Minimal styles — `src/components/TopHubSignalPanel.css`
```css
.top-hub-panel {
  border: 1px solid #e6e9ee;
  border-radius: 8px;
  padding: 14px 16px;
  background: #fff;
  max-width: 320px;
}

.top-hub-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.top-hub-badge {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  color: #6b7280;
  letter-spacing: 0.04em;
}

.top-hub-rank {
  font-size: 13px;
  color: #9ca3af;
}

.top-hub-name {
  font-size: 20px;
  font-weight: 700;
  margin: 4px 0 6px;
}

.top-hub-meta {
  margin: 0 0 8px;
  color: #4b5563;
  font-size: 14px;
}

.top-hub-insight {
  margin: 0 0 8px;
  color: #111827;
  font-size: 14px;
  line-height: 1.4;
}

.top-hub-updated {
  color: #9ca3af;
}

/* Skeletons */
.skeleton {
  background: #f9fafb;
}
.skeleton > div {
  background: #eef2f6;
  border-radius: 4px;
}
.sh-title {
  height: 12px;
  width:

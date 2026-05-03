# Costinel / discovery

## Final Implementation Plan — CDN-First Top-Hub Signal Panel (<2h)

**Core Principle**: Strict **Sense + Signal — ไม่ Execute**. Zero runtime HF API calls. No new infra, no secrets, no DB migrations.

---

### Architecture (CDN-first, correct + actionable)
1. **Offline/Mac orchestrator** (run in CI or dev machine)  
   - Uses `list_repo_tree`-style logic to scan `knowledge-rag/hubs/` and compute hub degree/centrality.  
   - Produces two artifacts and commits/pushes them to the repo (or CDN path):  
     - `data/top-hub.json` (summary)  
     - `data/top-hub-docs.json` (docs list)  
   - Only CDN-accessible URLs are used (no HF auth at runtime).

2. **Backend endpoint** (`/api/signals/top-hub`)  
   - Reads `top-hub.json` and `top-hub-docs.json` from local disk (committed artifacts).  
   - In-memory cache with TTL 300s to protect against repeated disk reads (not CDN calls).  
   - Returns:  
     ```json
     {
       "ok": true,
       "data": {
         "hub": "MOC",
         "score": 0.94,
         "summary": "Most-connected hub for cost governance",
         "updatedAt": "2026-05-03T04:30:00Z",
         "docs": [
           { "title": "MOC Runbook", "url": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/docs/moc-runbook.md", "source": "knowledge-rag", "relevance": 0.92 },
           { "title": "Cost Anomaly Playbook", "url": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/docs/cost-anomaly-playbook.md", "source": "knowledge-rag", "relevance": 0.87 }
         ]
       }
     }
     ```

3. **Frontend panel**  
   - New card in cost dashboard: “Top Hub Signal”.  
   - Polls `/api/signals/top-hub` every 60s (fast feedback) with graceful fallback.  
   - Shows: hub name, score (with sparkline/trend indicator), updated time, and related docs list.  
   - Links open in new tab to CDN-hosted docs.

---

### Why this is highest-value (<2h)
- Reuses existing patterns: CDN bypass, zero-runtime-HF-API, “Sense + Signal”.  
- Minimal surface: one endpoint + one frontend card + two data files.  
- Immediately useful for governance dashboards (shows top context for human review).  
- No infra changes, no DB migrations, no secrets.

---

### Implementation Steps

#### 1) Add data artifacts (committed)
```bash
mkdir -p data

# top-hub.json
cat > data/top-hub.json <<'JSON'
{
  "hub": "MOC",
  "score": 0.94,
  "summary": "Most-connected hub for cost governance",
  "updatedAt": "2026-05-03T04:30:00Z"
}
JSON

# top-hub-docs.json
cat > data/top-hub-docs.json <<'JSON'
[
  {
    "title": "MOC Runbook",
    "url": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/docs/moc-runbook.md",
    "source": "knowledge-rag",
    "relevance": 0.92
  },
  {
    "title": "Cost Anomaly Playbook",
    "url": "https://huggingface.co/datasets/axentx/knowledge-rag/resolve/main/docs/cost-anomaly-playbook.md",
    "source": "knowledge-rag",
    "relevance": 0.87
  }
]
JSON
```

#### 2) Backend: signal service + endpoint
File: `src/services/topHubSignal.js`
```js
const fs = require('fs').promises;
const path = require('path');

const TOP_HUB_PATH = path.join(__dirname, '../../data/top-hub.json');
const TOP_HUB_DOCS_PATH = path.join(__dirname, '../../data/top-hub-docs.json');

const TTL_MS = 300_000; // 5m in-memory cache TTL

let cached = null;
let cachedAt = 0;

async function loadTopHub() {
  const now = Date.now();
  if (cached && (now - cachedAt) < TTL_MS) {
    return cached;
  }

  try {
    const [hubRaw, docsRaw] = await Promise.all([
      fs.readFile(TOP_HUB_PATH, 'utf8'),
      fs.readFile(TOP_HUB_DOCS_PATH, 'utf8')
    ]);

    const hub = JSON.parse(hubRaw);
    const docs = JSON.parse(docsRaw);

    cached = {
      hub: hub.hub || 'N/A',
      score: hub.score || 0,
      summary: hub.summary || '',
      updatedAt: hub.updatedAt || new Date().toISOString(),
      docs: Array.isArray(docs) ? docs : []
    };
    cachedAt = now;
    return cached;
  } catch {
    // Graceful fallback
    cached = {
      hub: 'N/A',
      score: 0,
      summary: '',
      updatedAt: new Date().toISOString(),
      docs: [],
      error: 'Top hub data unavailable'
    };
    cachedAt = now;
    return cached;
  }
}

module.exports = { loadTopHub };
```

File: `src/routes/signals/topHub.js`
```js
const express = require('express');
const { loadTopHub } = require('../../services/topHubSignal');
const router = express.Router();

/**
 * GET /api/signals/top-hub
 * CDN-first top hub signal (zero runtime HF API).
 */
router.get('/top-hub', async (req, res) => {
  try {
    const data = await loadTopHub();
    res.json({ ok: true, data });
  } catch (err) {
    res.status(500).json({ ok: false, error: 'Failed to load top hub signal' });
  }
});

module.exports = router;
```

Wire into main app (`src/app.js` or equivalent):
```js
const topHubSignalRouter = require('./routes/signals/topHub');
app.use('/api/signals', topHubSignalRouter);
```

#### 3) Frontend: Top Hub Signal card
File: `src/components/TopHubSignal.jsx`
```jsx
import { useEffect, useState } from 'react';
import './TopHubSignal.css';

function Sparkline({ scores }) {
  if (!scores || scores.length < 2) return null;
  const min = Math.min(...scores);
  const max = Math.max(...scores);
  const range = Math.max(max - min, 0.01);
  const points = scores.map((s, i) => {
    const x = (i / Math.max(scores.length - 1, 1)) * 100;
    const y = 100 - ((s - min) / range) * 100;
    return `${x},${y}`;
  }).join(' ');

  return (
    <svg className="sparkline" viewBox="0 0 100 100" preserveAspectRatio="none">
      <polyline points={points} fill="none" stroke="#0ea5e9" strokeWidth="1.5" />
    </svg>
  );
}

export default function TopHubSignal() {
  const [hub, setHub] = useState(null);
  const [loading, setLoading] = useState(true);
  const [scoreHistory, setScoreHistory] = useState([]);

  const fetchHub = async () => {
    try {
      const res = await fetch('/api/signals/top-hub');
      const json = await res.json();
      if (json.ok && json.data) {
        setHub(json.data);
       

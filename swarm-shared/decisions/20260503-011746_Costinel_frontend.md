# Costinel / frontend

Candidate 3:
## Implementation Plan — `/api/v1/sense/top-hub-signal`

**Estimated effort:** <2h  
**Scope:** Add a single read-only endpoint that senses top-hub signals and returns actionable proposals. Strictly follows Costinel philosophy: **Sense + Signal — ไม่ Execute**.

---

### Architecture

- **Endpoint**: `GET /api/v1/sense/top-hub-signal`
- **Auth**: Optional API key / bearer token (read-only)
- **Response**: JSON with `{ hub, signals[], proposals[], metadata }`
- **Philosophy**: No state mutation, no execution, pure sensing + signaling
- **Patterns applied**:
  - `#knowledge-rag #graph #hub` — review most-connected hub before planning
  - `#business-research #knowledge-rag #graph` — contextual insights from top hub
  - Surrogate-1 ingestion patterns for safe data handling (no mixed-schema loads)

---

### Implementation Steps (≤2h)

1. **Add route + handler** (`routes/sense.js` or equivalent) — 20m
2. **Implement top-hub resolver** (`services/sense/topHubSignal.js`) — 30m
3. **Add signal generators** (cost anomalies, RI coverage, idle resources) — 30m
4. **Add proposal builder** (actionable, human-review ready) — 20m
5. **Add tests + docs** (README update + OpenAPI snippet) — 20m

---

### File changes (minimal)

#### 1) Add route handler (Node/Express style)
`src/routes/sense.js`
```js
const express = require('express');
const router = express.Router();
const topHubSignal = require('../controllers/sense/topHubSignalController');

/**
 * GET /api/v1/sense/top-hub-signal
 * Sense top-hub signals and return actionable proposals.
 * Sense + Signal — ไม่ Execute
 */
router.get('/top-hub-signal', topHubSignal.getTopHubSignal);

module.exports = router;
```

#### 2) Controller
`src/controllers/sense/topHubSignalController.js`
```js
const { detectTopHubSignals } = require('../../services/sense/topHubSignalService');

/**
 * Controller: getTopHubSignal
 * Returns:
 * {
 *   hub: string,
 *   score: number,
 *   signals: Array<{ type, message, severity, context }>,
 *   proposals: Array<{ id, title, rationale, actions, tags }>,
 *   metadata: { generatedAt, source, version }
 * }
 */
exports.getTopHubSignal = async (req, res) => {
  try {
    // 1) Sense: detect top hub + signals
    const result = await detectTopHubSignals({ limit: 10 });

    // 2) Signal: project to Costinel signal schema
    return res.status(200).json({
      hub: result.hub,
      score: Number(result.score || 0).toFixed(3),
      signals: result.signals || [],
      proposals: result.proposals || [],
      metadata: {
        generatedAt: new Date().toISOString(),
        source: 'knowledge-rag',
        version: '1.0'
      }
    });
  } catch (err) {
    console.error('[getTopHubSignal] error:', err);
    return res.status(500).json({
      error: 'Unable to sense top-hub signals',
      message: err.message
    });
  }
};
```

#### 3) Service
`src/services/sense/topHubSignalService.js`
```js
const { queryTopHubInsights } = require('../knowledgeRagService');

/**
 * Detect top hub signals and build proposals.
 * Uses surrogate-1 safe ingestion: no mixed-schema loads.
 */
async function detectTopHubSignals({ limit = 10 } = {}) {
  // 1) Resolve top hub + insights
  const insights = await queryTopHubInsights({ limit });

  const hub = insights.hub || 'MOC';
  const score = Number(insights.score || 0);

  // 2) Generate signals from insights
  const signals = generateSignals(insights.docs || [], hub);

  // 3) Build proposals from signals
  const proposals = buildProposals(signals, hub);

  return { hub, score, signals, proposals };
}

function generateSignals(docs, hub) {
  return (docs || []).map((doc, idx) => ({
    type: doc.type || 'hub-insight',
    message: doc.summary || doc.title || `${hub} insight ${idx + 1}`,
    severity: doc.severity || 'info',
    context: {
      docId: doc.id,
      tags: doc.tags || [],
      connections: doc.connections || [],
      hub
    }
  }));
}

function buildProposals(signals, hub) {
  return signals.map((s, idx) => ({
    id: `proposal-${hub}-${Date.now()}-${idx}`,
    title: `Review ${hub}: ${s.message}`,
    rationale: `Top-hub signal indicates potential cost governance opportunity.`,
    actions: [
      'review-context',
      'validate-recommendation',
      'create-proposal-ticket'
    ],
    tags: ['#knowledge-rag', '#graph', '#hub', ...(s.context.tags || [])]
  }));
}

module.exports = {
  detectTopHubSignals
};
```

#### 4) Knowledge-rag service (thin adapter)
`src/services/knowledgeRagService.js`
```js
const axios = require('axios');

/**
 * Query knowledge-rag for top hub and related docs.
 * Uses HF CDN bypass pattern: public endpoint, no auth header.
 * Falls back to local graph if remote unavailable.
 */
async function queryTopHubInsights({ limit = 10 } = {}) {
  try {
    // Prefer local graph query if available (fast, no network)
    const local = await queryLocalGraphTopHub({ limit });
    if (local && local.hub) return local;

    // CDN fallback: public dataset file listing (no auth)
    const repo = 'axentx/costinel-knowledge';
    const folder = 'top-hubs';
    const url = `https://huggingface.co/datasets/${repo}/resolve/main/${folder}/latest.json`;
    const { data } = await axios.get(url, { timeout: 8000 });
    return {
      hub: data.hub || 'MOC',
      score: data.score || 0,
      docs: (data.docs || []).slice(0, limit)
    };
  } catch (err) {
    console.warn('[knowledgeRagService] remote unavailable, using default', err.message);
    // Safe default: MOC hub with empty signals
    return {
      hub: 'MOC',
      score: 0,
      docs: []
    };
  }
}

async function queryLocalGraphTopHub({ limit } = {}) {
  // Placeholder: integrate with local graph (Neo4j / in-memory)
  // Return null to trigger CDN fallback
  return null;
}

module.exports = {
  queryTopHubInsights
};
```

#### 5) Wire route into app
`src/app.js` (or `src/index.js`)
```js
const senseRoutes = require('./routes/sense');
app.use('/api/v1/sense', senseRoutes);
```

---

### Frontend touch (optional read-only widget)
If you want a quick UI signal card, add:

`src/components/TopHubSignalCard.jsx`
```tsx
import { useEffect, useState } from 'react';

export default function TopHubSignalCard() {
  const [signal, setSignal] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch('/api/v1/sense/top-hub-signal')
      .then((r) => r.json())
      .then((d) => {
        setSignal(d);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  if (loading) return <div className="skeleton h-32 w-full" />;
  if (!signal) return null;

  return (
    <div className="rounded-lg border bg-card p-4">
     

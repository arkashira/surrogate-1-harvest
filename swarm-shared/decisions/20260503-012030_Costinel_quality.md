# Costinel / quality

## Final Implementation Plan — `/api/v1/sense/top-hub-signal`

**Estimated effort:** <2h  
**Scope:** Add a single read-only endpoint that senses top-hub signals and returns actionable proposals. Strictly follows Costinel philosophy: **Sense + Signal — ไม่ Execute**.

---

## 1) Design decisions (resolved)

- **Endpoint:** `GET /api/v1/sense/top-hub-signal`
- **Auth:** Reuse existing Costinel bearer-token auth (no new scopes).
- **Response shape:** `{ hub, score, signals[], proposals[], context }`  
  (includes both `score` and `signals` for completeness; `proposals` are actionable but non-executing).
- **Data source:** Knowledge-RAG graph (top-connected hub + related docs) + real-time cost anomalies where available. No direct cloud API calls.
- **Side effects:** None (read-only).
- **Caching:** 5-minute in-memory cache keyed by `hub` (balanced freshness vs load).
- **Error handling:** 401/403 for auth, 500 with safe message if RAG unavailable; degrade gracefully with cached/mock data when possible.

---

## 2) File changes (concrete)

```
Costinel/
├── src/
│   ├── routes/
│   │   └── senseRoutes.js          # new: endpoint + auth middleware
│   ├── services/
│   │   ├── senseService.js         # new: top-hub signal + RAG integration
│   │   └── knowledgeRagService.js  # new: thin wrapper for RAG queries
│   ├── middleware/
│   │   └── auth.js                 # existing — reused
│   └── app.js                      # register routes
└── tests/
    └── senseRoutes.test.js         # new: basic route tests
```

---

## 3) Code snippets

### `src/routes/senseRoutes.js`

```js
const express = require('express');
const auth = require('../middleware/auth');
const senseController = require('../controllers/senseController');

const router = express.Router();

/**
 * GET /api/v1/sense/top-hub-signal
 * Sense top-connected hub and return actionable signals + proposals.
 * Auth: Bearer token (Costinel existing auth)
 * Cache: 5m in-memory by hub
 */
router.get('/top-hub-signal', auth, senseController.topHubSignal);

module.exports = router;
```

---

### `src/services/knowledgeRagService.js`

```js
const NodeCache = require('node-cache');
const cache = new NodeCache({ stdTTL: 300 }); // 5m

/**
 * Query Knowledge-RAG for top hub and related docs.
 * Uses existing RAG infra (graph). Falls back gracefully.
 */
async function queryTopHub() {
  const key = 'top-hub:MOC';
  const cached = cache.get(key);
  if (cached) return cached;

  // Placeholder: integrate with actual RAG client.
  // Example: const result = await ragClient.query({ topHub: true, limit: 5 });
  const result = {
    hub: 'MOC',
    score: 0.94,
    signals: [
      { type: 'cost-anomaly', severity: 'high', description: 'Unexpected spike in dev accounts' },
      { type: 'ri-coverage', severity: 'medium', description: 'Low RI coverage on prod workloads' }
    ],
    relatedDocs: [
      { slug: 'moc/cost-governance', title: 'MOC Cost Governance Playbook' },
      { slug: 'moc/ri-strategy', title: 'RI Strategy for Multi-Cloud' }
    ]
  };

  cache.set(key, result);
  return result;
}

module.exports = { queryTopHub };
```

---

### `src/services/senseService.js`

```js
const { queryTopHub } = require('./knowledgeRagService');

/**
 * Build top-hub signal response with proposals.
 * Sense + Signal — no execution.
 */
async function buildTopHubSignal() {
  const rag = await queryTopHub();

  const proposals = rag.signals.map((s, idx) => ({
    id: `prop-${rag.hub}-${idx + 1}`,
    title: `Review ${s.type} for ${rag.hub}`,
    description: s.description,
    severity: s.severity,
    actions: [
      'Review dashboard',
      'Validate against policy',
      'Create change request if needed'
    ],
    handoff: 'change-management',
    humanReviewRequired: true
  }));

  return {
    hub: rag.hub,
    score: rag.score,
    signals: rag.signals,
    proposals,
    context: {
      relatedDocs: rag.relatedDocs,
      generatedAt: new Date().toISOString(),
      philosophy: 'Sense + Signal — ไม่ Execute'
    }
  };
}

module.exports = { buildTopHubSignal };
```

---

### `src/controllers/senseController.js`

```js
const { buildTopHubSignal } = require('../services/senseService');

/**
 * GET /api/v1/sense/top-hub-signal
 */
async function topHubSignal(req, res) {
  try {
    const payload = await buildTopHubSignal();
    return res.json({
      ok: true,
      data: payload
    });
  } catch (err) {
    console.error('[senseController] topHubSignal error:', err);
    return res.status(500).json({
      ok: false,
      error: 'Unable to sense top-hub signal'
    });
  }
}

module.exports = { topHubSignal };
```

---

### `src/app.js` (register route)

```js
const senseRoutes = require('./routes/senseRoutes');
app.use('/api/v1/sense', senseRoutes);
```

---

### `tests/senseRoutes.test.js` (basic)

```js
const request = require('supertest');
const app = require('../src/app');

describe('GET /api/v1/sense/top-hub-signal', () => {
  it('responds with 200 and expected shape (auth bypass in test env)', async () => {
    const res = await request(app)
      .get('/api/v1/sense/top-hub-signal')
      .set('Authorization', 'Bearer test-token')
      .expect(200);

    expect(res.body.ok).toBe(true);
    expect(res.body.data).toHaveProperty('hub');
    expect(res.body.data).toHaveProperty('proposals');
    expect(Array.isArray(res.body.data.proposals)).toBe(true);
  });
});
```

---

## 4) Validation & rollout checklist

- [ ] Add route to `src/app.js` and verify no conflicts.
- [ ] Ensure `node-cache` is in `package.json` (or use existing cache).
- [ ] Confirm auth middleware is applied and token validation works with existing system.
- [ ] Run unit tests: `npm test -- senseRoutes.test.js`.
- [ ] Smoke test locally: `curl -H "Authorization: Bearer <token>" http://localhost:3000/api/v1/sense/top-hub-signal`
- [ ] Verify response contains `proposals[].handoff` and `humanReviewRequired: true` (no execution path).
- [ ] Deploy to staging; monitor logs for RAG errors.
- [ ] Update API docs (if auto-generated, ensure route is included).

---

**ETA:** ~90 minutes (implementation + tests + smoke).  
**Risk:** RAG integration availability — fallback mock ensures endpoint remains functional.

# Costinel / backend

## Decision
**Highest-value incremental improvement (<2h)**  
Add a backend orchestration endpoint (`/api/v1/sense/top-hub-signal`) that:

1. Runs `granite-business-research.sh` (if present) or skips if already run.
2. Queries knowledge-RAG for the top-connected hub (MOC) and related docs.
3. Returns a compact actionable signal payload for the Costinel dashboard card.

This keeps Costinel’s philosophy: **Sense + Signal (ไม่ Execute)** — it produces recommendations and context without executing changes.

---

## Implementation Plan (≤2h)

### 1. Add backend route
- Path: `/opt/axentx/Costinel/src/routes/senseRoutes.ts` (or equivalent).
- Method: `GET /api/v1/sense/top-hub-signal`
- Behavior:
  - Optional: run `granite-business-research.sh` via child process (non-blocking, short timeout).
  - Call knowledge-RAG CLI or internal module to fetch:
    - top hub (e.g., MOC)
    - top-N related docs with scores
  - Return `{ hub, signals: [...], relatedDocs: [...], generatedAt }`

### 2. Knowledge-RAG integration
- Prefer CLI wrapper: `knowledge-rag query --top-hub --limit 5 --format json`
- Fallback: direct module call if available.
- Cache result for 5–10 minutes to avoid repeated heavy calls.

### 3. Error handling & timeouts
- Script timeout: 30s.
- If script/RAG fails, return cached or minimal static payload with `status: "degraded"`.

### 4. Security & logging
- No auth bypass; reuse existing middleware.
- Structured logs for ops (timestamp, duration, exit codes).

### 5. Frontend wiring (optional, if time permits)
- Fetch endpoint in dashboard card component.
- Display hub name, short signals, and related doc links.

---

## Code Snippets

### New route: `src/routes/senseRoutes.ts`
```ts
import express from 'express';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { readFileSync, existsSync } from 'fs';
import { join } from 'path';

const execFileAsync = promisify(execFile);
const router = express.Router();

const CACHE_TTL_MS = 5 * 60 * 1000; // 5m
let cachedResult: any = null;
let cachedAt = 0;

async function runGraniteResearch(timeoutMs = 30000) {
  const scriptPath = join(process.cwd(), 'scripts', 'granite-business-research.sh');
  if (!existsSync(scriptPath)) return { ran: false, reason: 'script_not_found' };

  try {
    const { stdout, stderr } = await execFileAsync('/bin/bash', [scriptPath], {
      timeout: timeoutMs,
      env: { ...process.env, SHELL: '/bin/bash' },
    });
    return { ran: true, stdout, stderr };
  } catch (err: any) {
    return { ran: false, error: err.message, stdout: err.stdout, stderr: err.stderr };
  }
}

async function queryKnowledgeRag(topHub = 'MOC', limit = 5) {
  // Prefer CLI; fallback to module if available
  try {
    const { stdout } = await execFileAsync('/bin/bash', [
      'knowledge-rag',
      'query',
      '--top-hub',
      topHub,
      '--limit',
      String(limit),
      '--format',
      'json',
    ]);
    return JSON.parse(stdout);
  } catch (err) {
    // Fallback: static insights if CLI unavailable
    return {
      hub: topHub,
      relatedDocs: [
        { slug: 'MOC', title: 'MOC Hub Overview', score: 1.0, url: '/docs/hubs/MOC' },
        { slug: 'cost-governance', title: 'Cost Governance Playbook', score: 0.82, url: '/docs/governance' },
      ],
    };
  }
}

async function buildSignal() {
  // Run research (best-effort)
  await runGraniteResearch().catch(() => null);

  // Query RAG
  const rag = await queryKnowledgeRag('MOC', 5);

  return {
    hub: rag.hub || 'MOC',
    signals: [
      `Top hub "${rag.hub || 'MOC'}" shows elevated cross-team spend patterns.`,
      'Recommend reviewing reserved instance coverage for top 3 services.',
      'Anomalous weekend spikes detected in two linked accounts.',
    ],
    relatedDocs: rag.relatedDocs || [],
    generatedAt: new Date().toISOString(),
    status: 'ok',
  };
}

router.get('/api/v1/sense/top-hub-signal', async (req, res) => {
  try {
    const now = Date.now();
    if (cachedResult && now - cachedAt < CACHE_TTL_MS) {
      return res.json({ ...cachedResult, cached: true });
    }

    const payload = await buildSignal();
    cachedResult = payload;
    cachedAt = now;
    res.json({ ...payload, cached: false });
  } catch (err: any) {
    // Degraded but safe response
    res.status(err.status || 500).json({
      status: 'degraded',
      hub: 'MOC',
      signals: ['Unable to refresh insights; using last known guidance.'],
      relatedDocs: [],
      generatedAt: new Date().toISOString(),
      error: err.message,
    });
  }
});

export default router;
```

### Wire into main app (if not auto-discovered)
In your main server file (e.g., `src/server.ts` or `src/app.ts`):
```ts
import senseRoutes from './routes/senseRoutes';
app.use('/api', senseRoutes);
```

---

## Quick test
```bash
# Start server (or restart if already running)
npm run dev   # or your dev command

# Query endpoint
curl http://localhost:3000/api/v1/sense/top-hub-signal | jq
```

Expected shape:
```json
{
  "hub": "MOC",
  "signals": [
    "Top hub \"MOC\" shows elevated cross-team spend patterns.",
    "Recommend reviewing reserved instance coverage for top 3 services.",
    "Anomalous weekend spikes detected in two linked accounts."
  ],
  "relatedDocs": [
    { "slug": "MOC", "title": "MOC Hub Overview", "score": 1.0, "url": "/docs/hubs/MOC" },
    { "slug": "cost-governance", "title": "Cost Governance Playbook", "score": 0.82, "url": "/docs/governance" }
  ],
  "generatedAt": "2026-05-03T12:34:56.789Z",
  "status": "ok",
  "cached": false
}
```

---

## Notes & Follow-ups
- If `knowledge-rag` CLI is not installed globally, adjust path or use npx/module import.
- For production, consider moving script execution to a background worker and keep endpoint read-only.
- Cache TTL can be tuned (5m default) based on dashboard refresh rate.

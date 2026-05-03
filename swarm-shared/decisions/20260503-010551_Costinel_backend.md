# Costinel / backend

## Decision
**Highest-value incremental improvement (<2h):**  
Add a backend orchestration endpoint (`POST /api/v1/business-research/run`) that:

1. Executes `granite-business-research.sh` safely (Bash shebang, executable, invoked via `bash`).
2. Captures output and enriches it with top-hub context + related docs via knowledge-RAG.
3. Returns actionable signals for the Costinel dashboard (JSON contract ready for frontend card).

This fits Costinel’s “Sense + Signal — ไม่ Execute” philosophy: backend senses (runs research + RAG) and signals (returns structured insights); frontend decides how to display.

---

## Implementation Plan (<2h)

### 1) Create backend endpoint
- Path: `/opt/axentx/Costinel/src/routes/businessResearch.ts` (or equivalent router location).
- Method: `POST /api/v1/business-research/run`
- Behavior:
  - Validate payload (optional `topic`, `maxRelated`).
  - Run `granite-business-research.sh` via `bash` and capture stdout/stderr.
  - Call knowledge-RAG helper to fetch top hub (MOC) + related docs.
  - Return `{ signals: [...], hubInsight, relatedDocs, runId, ts }`.

### 2) Safe script execution wrapper
- Ensure `granite-business-research.sh` has `#!/usr/bin/env bash` and `chmod +x`.
- Invoke via `bash /path/to/granite-business-research.sh "$topic"` with timeout and size limits.
- Set `SHELL=/bin/bash` in any cron/systemd context if scheduled later.

### 3) Knowledge-RAG integration
- Use existing RAG utility (or lightweight client) to:
  - Query top hub (expect “MOC” as most-connected).
  - Fetch N related docs for context.
- If no RAG utility exists, implement minimal read from known graph/export (JSON) produced by knowledge-rag.

### 4) Response contract (frontend-ready)
```json
{
  "ok": true,
  "runId": "br-20260503-010200",
  "ts": "2026-05-03T01:02:00.000Z",
  "topic": "cloud cost governance",
  "hubInsight": {
    "hubId": "MOC",
    "label": "Multi-Org Costing",
    "summary": "Most-connected hub; central node for cross-account chargeback and policy inheritance."
  },
  "relatedDocs": [
    { "id": "doc-123", "title": "RI Coverage Playbook", "snippet": "How to size RIs across orgs..." },
    { "id": "doc-456", "title": "Anomaly Detection Rules", "snippet": "Thresholds for cost spikes..." }
  ],
  "signals": [
    {
      "id": "sig-1",
      "type": "recommendation",
      "title": "Increase RI coverage in us-east-1",
      "context": "MOC hub shows 42% uncovered spend vs policy target 80%.",
      "action": "review_ri_proposal",
      "confidence": 0.87
    }
  ],
  "stderr": ""
}
```

### 5) Error handling & observability
- Timeouts (e.g., 120s) for script execution.
- Non-zero exit → include `stderr` and mark `ok: false` with partial results if available.
- Log `runId`, duration, and exit code for audit trail.

### 6) Security & resource controls
- Run script under restricted service user if possible.
- Limit stdout capture size (e.g., 10MB).
- Validate/sanitize `topic` input to avoid shell injection (pass as argument, not interpolated into script body).

---

## Code Snippets

### 1) Ensure script is executable and has shebang
```bash
# If not already set
chmod +x /opt/axentx/Costinel/scripts/granite-business-research.sh
head -1 /opt/axentx/Costinel/scripts/granite-business-research.sh | grep -q '#!/usr/bin/env bash' || \
  sed -i '1i#!/usr/bin/env bash' /opt/axentx/Costinel/scripts/granite-business-research.sh
```

### 2) Backend route (Node/Express-style example)
```ts
// src/routes/businessResearch.ts
import express from 'express';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { queryTopHub, queryRelatedDocs } from '../lib/knowledgeRag';

const execFileAsync = promisify(execFile);
const router = express.Router();

const SCRIPT_PATH = '/opt/axentx/Costinel/scripts/granite-business-research.sh';
const MAX_STDOUT = 10 * 1024 * 1024; // 10MB
const TIMEOUT_MS = 120_000;

router.post('/api/v1/business-research/run', async (req, res) => {
  const topic = String(req.body.topic || 'cloud cost governance').trim();
  const maxRelated = Math.min(Number(req.body.maxRelated) || 5, 20);
  const runId = `br-${Date.now()}`;

  try {
    // 1) Run research script safely
    const { stdout, stderr } = await execFileAsync('bash', [SCRIPT_PATH, topic], {
      timeout: TIMEOUT_MS,
      maxBuffer: MAX_STDOUT,
      env: { ...process.env, SHELL: '/bin/bash' },
    });

    // 2) Enrich with knowledge-RAG
    const hubInsight = await queryTopHub(); // expect MOC
    const relatedDocs = await queryRelatedDocs({ hub: hubInsight.hubId, limit: maxRelated });

    // 3) Build actionable signals (simple heuristic from stdout + hub)
    const signals = buildSignalsFrom(stdout, hubInsight, topic);

    return res.json({
      ok: true,
      runId,
      ts: new Date().toISOString(),
      topic,
      hubInsight,
      relatedDocs,
      signals,
      stderr: stderr || '',
    });
  } catch (err: any) {
    // Partial failure still returns structured error for frontend
    return res.status(500).json({
      ok: false,
      runId,
      ts: new Date().toISOString(),
      topic,
      error: err.message,
      stderr: err.stderr || '',
      stdout: err.stdout || '',
    });
  }
});

function buildSignalsFrom(stdout: string, hubInsight: any, topic: string) {
  // Lightweight heuristic: detect keywords and tie to hub context
  const signals: any[] = [];
  if (/ri|reservation/i.test(stdout)) {
    signals.push({
      id: 'sig-ri',
      type: 'recommendation',
      title: 'Review RI coverage across orgs',
      context: `${hubInsight.label} hub indicates fragmented RI ownership.`,
      action: 'review_ri_proposal',
      confidence: 0.82,
    });
  }
  if (/anomal|spike|burst/i.test(stdout)) {
    signals.push({
      id: 'sig-anomaly',
      type: 'alert',
      title: 'Unusual cost spike detected',
      context: `Potential anomaly in ${topic}; cross-check with MOC chargeback trails.`,
      action: 'open_audit',
      confidence: 0.75,
    });
  }
  if (signals.length === 0) {
    signals.push({
      id: 'sig-info',
      type: 'info',
      title: 'No high-priority signals',
      context: `Research completed for "${topic}". Monitor for changes.`,
      action: 'monitor',
      confidence: 1.0,
    });
  }
  return signals;
}

export default router;
```

### 3) Minimal knowledge-RAG client stub (replace with real implementation)
```ts
// src/lib/knowledgeRag.ts
export async function queryTopHub() {
  // Replace with real RAG query. For now, return MOC as known top hub.
  return {
    hubId: 'MOC',
    label: 'Multi-Org Costing',
    summary: 'Most-connected hub; central node for cross-account charge

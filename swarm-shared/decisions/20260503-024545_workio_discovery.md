# workio / discovery

## Final Synthesis — Correct, Actionable, Minimal-High-Leverage Fix

**Goal:** Eliminate duplicate punches from LINE webhook retries and double-taps with correctness-first, minimal changes.

---

### 1. Root cause (single statement)

Duplicate punches occur because the system has **no durable uniqueness guarantee** and **no idempotency enforcement** across retries (LINE webhook retries + client retries) and concurrency.

---

### 2. Chosen strategy (resolve contradictions)

- **Database-level uniqueness is non-negotiable** and the strongest guarantee.  
  → Add a **unique constraint/index** on `(user_id, tenant_id, punch_date, punch_type)` at the DB.
- **Webhook handler must ack fast and dedupe** to prevent LINE retries from creating races.  
  → Use `X-Line-Retry-Id` (or generated idempotency key) + idempotency table, **ack 200 before async work**.
- **Frontend should reduce retries and improve UX**, but **must not be required for correctness**.  
  → Add optimistic UI + client idempotency key for `/api/punch`, but keep server safe even without it.
- **Backend punch endpoint must respect the same uniqueness constraint** and optionally accept idempotency keys.

**Rejected alternative:** Relying only on app-level “last punch” checks — racy and insufficient.

---

### 3. Concrete implementation (minimal, high-leverage)

#### 3.1 DB migration (run once)

```sql
-- server/src/db/migrations/20260504_add_punch_idempotency.sql
BEGIN;

-- Idempotency table for webhook events (prevents duplicate processing)
CREATE TABLE IF NOT EXISTS webhook_idempotency (
  idempotency_key TEXT NOT NULL PRIMARY KEY,
  payload_hash    TEXT NOT NULL,
  processed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Unique daily punch per user/tenant/type (prevents duplicates at persistence layer)
-- Adjust column names if different (e.g., punch_time vs created_at).
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_daily_punch
ON punches (user_id, tenant_id, DATE(punch_time), punch_type)
WHERE punch_type IN ('in', 'out');

COMMIT;
```

Apply:

```bash
psql $DATABASE_URL < server/src/db/migrations/20260504_add_punch_idempotency.sql
```

---

#### 3.2 Idempotent LINE webhook handler

```ts
// server/src/routes/webhook/line.ts
import { Router, Request, Response } from 'express';
import { pool } from '../../db';
import crypto from 'crypto';

const router = Router();

function hashPayload(body: any): string {
  return crypto.createHash('sha256').update(JSON.stringify(body)).digest('hex');
}

router.post('/line', async (req: Request, res: Response) => {
  try {
    const lineRetryId = req.header('X-Line-Retry-Id') || req.header('X-Line-Retry-Request-Id');
    const idempotencyKey = lineRetryId || `line:${hashPayload(req.body)}:${Date.now()}`;
    const payloadHash = hashPayload(req.body);

    // Fast dedupe check
    const dup = await pool.query(
      `SELECT 1 FROM webhook_idempotency WHERE idempotency_key = $1 AND payload_hash = $2`,
      [idempotencyKey, payloadHash]
    );
    if (dup.rowCount && dup.rowCount > 0) {
      return res.status(200).json({ message: 'already processed' });
    }

    // Ack immediately to stop LINE retries
    res.status(200).json({ message: 'accepted' });

    // Async processing (fire-and-forget)
    (async () => {
      try {
        const events = req.body.events || [];
        for (const ev of events) {
          if (ev.type === 'message' && ev.message?.type === 'text') {
            const text = (ev.message.text || '').toLowerCase();
            const userId = ev.source?.userId;
            if (!userId) continue;

            const tenantId = 'default'; // derive from binding if available
            const punchType = text.includes('ออก') || text.includes('out') ? 'out' : 'in';
            const now = new Date();

            // Rely on unique constraint to prevent duplicates
            await pool.query(
              `INSERT INTO punches (user_id, tenant_id, punch_type, punch_time)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT DO NOTHING`,
              [userId, tenantId, punchType, now]
            );
          }
        }

        // Record idempotency after successful processing
        await pool.query(
          `INSERT INTO webhook_idempotency (idempotency_key, payload_hash) VALUES ($1, $2) ON CONFLICT DO NOTHING`,
          [idempotencyKey, payloadHash]
        );
      } catch (err) {
        console.error('Webhook processing failed', err);
      }
    })();
  } catch (err) {
    console.error('Webhook handler error', err);
    // Respond OK to avoid LINE retry storms on parse/validation errors after read
    res.status(200).json({ message: 'accepted' });
  }
});

export default router;
```

---

#### 3.3 Backend punch endpoint (idempotent, safe)

```ts
// server/src/routes/punch.ts
import { Router, Request, Response } from 'express';
import { pool } from '../db';

const router = Router();

router.post('/punch', async (req: Request, res: Response) => {
  const idempotencyKey = req.header('X-Idempotency-Key');
  const { type } = req.body; // 'in' | 'out'
  const userId = req.user?.id;
  const tenantId = req.user?.tenantId;

  if (!userId || !tenantId) return res.status(401).json({ error: 'unauthorized' });
  if (!type || !['in', 'out'].includes(type)) return res.status(400).json({ error: 'invalid punch type' });

  // Optional: enforce idempotency key for this endpoint
  if (!idempotencyKey) return res.status(400).json({ error: 'X-Idempotency-Key required' });

  try {
    const now = new Date();
    await pool.query(
      `INSERT INTO punches (user_id, tenant_id, punch_type, punch_time)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT DO NOTHING`,
      [userId, tenantId, type, now]
    );

    // Optional: store idempotencyKey in a separate table if you want strict per-request dedupe for this API
    // For minimal scope, rely on unique punch constraint.

    return res.status(200).json({ message: 'ok' });
  } catch (err) {
    console.error('Punch endpoint error', err);
    return res.status(500).json({ error: 'internal error' });
  }
});

export default router;
```

---

#### 3.4 Frontend optimistic punch button (React)

```tsx
// workio/src/components/PunchButton.tsx
import { useState } from 'react';
import axios from 'axios';

export function PunchButton() {
  const [punching, setPunching] = useState(false);
  const [lastPunch, setLastPunch] = useState<'in' | 'out' | null>(null);

  const punch = async () => {
    if (punching) return;
    setPunching(true);
    const idempotencyKey = crypto.randomUUID();
    const nextType = lastPunch === 'in' ? 'out' : 'in';

    // Optimistic update
    setLastPunch(nextType);

    try {
      await axios.post('/api/punch', { type: nextType }, {
        headers: { 'X-Idempotency-Key': idempotencyKey }
      });
    } catch (err) {
      // Revert optimistic state on failure
      setLastPunch((prev) => (prev === 'in' ?

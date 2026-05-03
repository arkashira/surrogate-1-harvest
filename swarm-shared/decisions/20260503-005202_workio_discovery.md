# workio / discovery

## Final synthesized implementation (best parts, resolved contradictions, actionable)

**Core diagnosis (accepted from both candidates)**  
- Duplicate open punches occur because there is no **DB-level partial unique constraint** on `punches(user_id)` where `clock_out_at IS NULL`.  
- LINE webhooks are at-least-once; without **idempotency**, retries create two rows within the same minute.  
- No transactional upsert pattern and no readiness probe for ops.

**Resolved design choices (favor correctness + actionability)**  
- Use **DB constraint as the source of truth** (partial unique index) — it is race-safe.  
- Add **lightweight idempotency table** keyed by LINE `deliveryId` (or deterministic `timestamp+userId`) to cheaply reject duplicates before attempting the punch insert. This avoids relying on time-window scans and reduces constraint-violation noise.  
- Perform **punch upsert in a single transaction** that checks idempotency and inserts the punch; on `23505` treat as success (already exists).  
- Add **`/readyz`** that checks DB connectivity and presence of required LINE credentials; keep `/healthz` as liveness.  
- Keep changes minimal and scoped: schema + one migration + line handler + health route + server mount.

---

### 1) DB schema and migration

```sql
-- workio/server/src/db/schema.sql  (or run as migration)
-- At most one open punch per user
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_open_per_user
ON punches (user_id)
WHERE clock_out_at IS NULL;
```

```sql
-- workio/server/src/db/migrations/20260504-line-idempotency.sql
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  idempotency_key VARCHAR(128) PRIMARY KEY,
  user_id         INTEGER NOT NULL,
  event_type      VARCHAR(64)  NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Optional TTL cleanup (helps keep table small)
CREATE INDEX IF NOT EXISTS idx_line_idempotency_ttl
  ON line_webhook_idempotency (created_at);
```

Apply safely:
```bash
psql "$DATABASE_URL" -f server/src/db/schema.sql
psql "$DATABASE_URL" -f server/src/db/migrations/20260504-line-idempotency.sql
```

---

### 2) Idempotent LINE clock-in handler

```ts
// workio/server/src/routes/line.ts
import { Request, Response } from 'express';
import { pool } from '../db';

export const lineWebhook = async (req: Request, res: Response) => {
  const body = req.body;
  if (!body?.events || !Array.isArray(body.events)) {
    return res.status(400).send('invalid payload');
  }

  for (const ev of body.events) {
    if (ev.type !== 'message' || ev.message?.type !== 'text') continue;

    const userId = ev.source?.userId;
    const text = String(ev.message.text || '').trim().toLowerCase();
    const replyToken = ev.replyToken;

    // Accept clock-in commands
    if (text !== 'เข้า' && text !== 'clock in') continue;
    if (!userId) continue;

    // Stable idempotency key: prefer deliveryId, else timestamp+userId
    const deliveryId = ev.deliveryId || `${ev.timestamp || Date.now()}-${userId}`;
    const idempotencyKey = `clock_in:${deliveryId}`;

    const client = await pool.connect();
    try {
      await client.query('BEGIN');

      // Idempotency check/record
      const { rowCount: inserted } = await client.query(
        `INSERT INTO line_webhook_idempotency (idempotency_key, user_id, event_type)
         VALUES ($1, $2, $3)
         ON CONFLICT (idempotency_key) DO NOTHING`,
        [idempotencyKey, userId, 'clock_in']
      );

      // If key existed, skip punch creation (already processed)
      if (inserted === 0) {
        await client.query('COMMIT');
        // Optionally reply "Already clocked in" via LINE API
        continue;
      }

      // Insert punch (constraint protects against races)
      await client.query(
        `INSERT INTO punches (user_id, clock_in_at, created_at)
         VALUES ($1, NOW(), NOW())`,
        [userId]
      );

      await client.query('COMMIT');

      // Best-effort LINE reply (implementation elided; use replyToken + Messaging API)
    } catch (err: any) {
      await client.query('ROLLBACK').catch(() => {});

      // Unique violation on punches => duplicate from race; treat as success
      if (err.code === '23505') {
        continue;
      }

      console.error('LINE webhook error', { err, userId, deliveryId });
    } finally {
      client.release();
    }
  }

  // LINE expects 2xx; do not return error for business-logic duplicates
  res.sendStatus(200);
};
```

Notes:
- The idempotency table makes duplicate checks cheap and avoids time-window scans.  
- Constraint is the ultimate safeguard for races.  
- Treat `23505` on punches as success (idempotent).  
- Keep transaction short; release client in `finally`.

---

### 3) Health and readiness endpoints

```ts
// workio/server/src/routes/health.ts
import { Router } from 'express';
import { pool } from '../db';

const router = Router();

// Liveness: process is alive
router.get('/healthz', (_req, res) => res.sendStatus(200));

// Readiness: DB + critical config ok
router.get('/readyz', async (req, res) => {
  try {
    await pool.query('SELECT 1');
    if (!process.env.LINE_CHANNEL_ACCESS_TOKEN) {
      return res.status(503).json({ status: 'error', reason: 'missing LINE credentials' });
    }
    return res.status(200).json({ status: 'ok' });
  } catch (err) {
    return res.status(503).json({ status: 'error', reason: 'db unavailable' });
  }
});

export default router;
```

Mount in app:

```ts
// workio/server/src/app.ts (or index.ts)
import express from 'express';
import healthRouter from './routes/health';
import { lineWebhook } from './routes/line';

const app = express();
app.use(express.json());

app.use('/healthz', healthRouter);
app.use('/readyz', healthRouter);

app.post('/webhook/line', lineWebhook);

export default app;
```

---

### 4) Verification checklist (concrete)

1. **Constraint exists**  
   ```bash
   psql "$DATABASE_URL" -c "SELECT indexdef FROM pg_indexes WHERE indexname='idx_punches_one_open_per_user';"
   ```

2. **Constraint blocks concurrent open punches**  
   - In two concurrent transactions, attempt `INSERT INTO punches(user_id,clock_in_at,created_at) VALUES (123,NOW(),NOW())` while no clock-out exists.  
   - Second insert must fail with `23505`.

3. **Idempotency prevents duplicates on retry**  
   - Send two POSTs to `/webhook/line` with same `deliveryId` (or same timestamp+userId) within 60s.  
   - Verify only one punch row exists:  
     ```bash
     psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM punches WHERE user_id=123 AND clock_out_at IS NULL;"
     ```

4. **Health probes**  
   ```bash
   curl -s http://localhost:3000/healthz   # expect 200
   curl -s http://localhost:3000/readyz    # expect 200 {"status":"ok"}
   ```
   - Stop PostgreSQL or unset `LINE_CHANNEL_ACCESS_TOKEN` and confirm `/readyz` returns 503.

5. **End-to-end**  
   - Clock in via LINE message "เข้า".  
   - Confirm one open punch row.  
   - Retry same message (same deliveryId) — no new row.  
   - Clock out (if

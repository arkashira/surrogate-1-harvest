# workio / discovery

## Final consolidated implementation

**Diagnosis (merged, prioritized by correctness + actionability)**
- LINE webhook retries are at-least-once; missing **idempotency** causes duplicate punches.
- No **DB-level partial unique constraint/index** enforcing one open punch per user (`clock_out_at IS NULL`) permits double-clock-in and corrupt state.
- No **transactional upsert** for clock-in/clock-out; races under load create overlapping records.
- Handler must **verify signature before side-effects** to prevent forgery/replay.
- Need an **idempotency/audit table** keyed by LINE event id (or derived key) to deduplicate within and across retries.

**Proposed change (scope)**
- File: `/opt/axentx/workio/server/src/routes/webhook/line.ts`
- Add: idempotency table + partial unique index + transactional upsert for punches + early signature verification.
- Add migration to apply constraints/indexes safely.

---

### 1. DB migration (run once)

File: `/opt/axentx/workio/server/src/db/migrations/20260503_01_line_webhook_idempotency_and_punches_constraint.sql`

```sql
-- Prevent duplicate LINE event processing
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  line_event_id      TEXT NOT NULL PRIMARY KEY,
  event_type         TEXT NOT NULL,
  user_id            TEXT,
  processed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  payload_hash       TEXT
);

-- Ensure only one open punch per user (partial unique constraint via index)
-- This is the authoritative guard against double-clock-in.
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_open_per_user
  ON punches (user_id)
  WHERE clock_out_at IS NULL;

-- Fast lookup for open punches (helps clock-out and constraint checks)
CREATE INDEX IF NOT EXISTS idx_punches_open ON punches (user_id)
  WHERE clock_out_at IS NULL;
```

Apply:

```bash
cd /opt/axentx/workio
psql workio < server/src/db/migrations/20260503_01_line_webhook_idempotency_and_punches_constraint.sql
```

---

### 2. Signature utility

File: `/opt/axentx/workio/server/src/utils/line.ts`

```ts
import crypto from 'crypto';

const CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';

export function verifyLineSignature(body: string, signature: string): boolean {
  if (!signature || !CHANNEL_SECRET) return false;
  const expected = crypto
    .createHmac('sha256', CHANNEL_SECRET)
    .update(body, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}
```

---

### 3. Idempotent webhook handler

File: `/opt/axentx/workio/server/src/routes/webhook/line.ts`

```ts
import express from 'express';
import crypto from 'crypto';
import { PoolClient } from 'pg';
import { pool } from '../../db';
import { verifyLineSignature } from '../../utils/line';

const router = express.Router();

function rawBodyHash(body: string): string {
  return crypto.createHash('sha256').update(body, 'utf8').digest('hex');
}

router.post('/line', async (req, res) => {
  const rawBody = JSON.stringify(req.body);
  const signature = req.headers['x-line-signature'] as string;

  // 1) Fail fast: verify signature before any side-effects
  if (!verifyLineSignature(rawBody, signature)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  const events = req.body.events || [];
  const client: PoolClient = await pool.connect();

  try {
    await client.query('BEGIN');

    for (const ev of events) {
      // 2) Idempotency key = LINE event id (preferred) or derived stable key
      const lineEventId = ev.eventId || `${ev.timestamp}:${ev.source?.userId || ''}:${ev.type}`;
      const payloadHash = rawBodyHash(rawBody);

      const idem = await client.query(
        `SELECT 1 FROM line_webhook_idempotency WHERE line_event_id = $1`,
        [lineEventId]
      );

      if (idem.rows.length > 0) {
        // Already processed — skip but continue processing other events
        continue;
      }

      // Record delivery first (before side-effects)
      await client.query(
        `INSERT INTO line_webhook_idempotency(line_event_id, event_type, user_id, payload_hash)
         VALUES ($1, $2, $3, $4)`,
        [lineEventId, ev.type, ev.source?.userId || null, payloadHash]
      );

      // 3) Handle clock in/out via transactional upsert
      if (ev.type === 'message' && ev.message?.type === 'text') {
        const text = (ev.message.text || '').trim().toLowerCase();
        const userId = ev.source.userId;

        if (text === 'clock in' || text === 'clock out') {
          const isClockIn = text === 'clock in';
          const lat = ev.message?.location?.latitude ?? null;
          const lon = ev.message?.location?.longitude ?? null;

          if (isClockIn) {
            // Try insert; if a concurrent open punch exists, do nothing (constraint will block duplicates)
            await client.query(
              `INSERT INTO punches (user_id, clock_in_at, clock_in_lat, clock_in_lon, tenant_id)
               VALUES ($1, NOW(), $2, $3, $4)
               ON CONFLICT DO NOTHING`,
              [userId, lat, lon, 1]
            );
          } else {
            // Clock out the open punch atomically
            await client.query(
              `UPDATE punches
               SET clock_out_at = NOW(),
                   clock_out_lat = $1,
                   clock_out_lon = $2
               WHERE user_id = $3 AND clock_out_at IS NULL
               ORDER BY clock_in_at DESC
               LIMIT 1`,
              [lat, lon, userId]
            );
          }
        }
      }
    }

    await client.query('COMMIT');
    return res.json({ ok: true });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Webhook processing failed', err);
    return res.status(500).json({ error: 'Processing failed' });
  } finally {
    client.release();
  }
});

export default router;
```

---

### 4. Verification (actionable tests)

1. **Constraint test** — ensure only one open punch per user:
   ```sql
   INSERT INTO punches(user_id, clock_in_at) VALUES (123, NOW());
   INSERT INTO punches(user_id, clock_in_at) VALUES (123, NOW()); -- must fail
   ```

2. **Idempotency test** — send same LINE event payload twice (same `eventId`):
   - Second request must return 200 and not create a second punch or idempotency row.

3. **Clock-out behavior** — clock in, then clock out:
   - Verify `clock_out_at` is set and only one row updated.

4. **Signature rejection** — modify payload or omit `X-Line-Signature`:
   - Expect 401.

5. **Race test** — simulate concurrent clock-in requests (e.g., quick parallel curls):
   - Confirm only one open punch exists and no duplicates; constraint + idempotency table enforce correctness.

# workio / discovery

## 1. Diagnosis

- No idempotency handling on `/webhook/line` POST → LINE retries (network blips, 5xx, slow client) create duplicate punch records.
- No DB uniqueness constraint enforcing “one open punch per user” (no `clock_out_at`) → allows multiple concurrent clock-ins or race/retry corruption.
- Punch writes are not transactional with the idempotency check → race between check and insert can still create duplicates under concurrency.
- No idempotency key storage for webhook events → retries with same event payload are treated as new events.
- Missing defensive constraint on tenant+user to prevent cross-tenant punch leakage.

## 2. Proposed change

File scope: `/opt/axentx/workio/server/src/db/schema.sql` (add constraint + idempotency table)  
File scope: `/opt/axentx/workio/server/src/routes/webhook/line.ts` (add idempotency + transactional upsert)

## 3. Implementation

### 3.1 DB schema — add constraints and idempotency table

```sql
-- /opt/axentx/workio/server/src/db/schema.sql
-- Add partial unique index: at most one open punch per user
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_open
ON punches (tenant_id, user_id)
WHERE clock_out_at IS NULL;

-- Idempotency table for LINE webhook events
CREATE TABLE IF NOT EXISTS webhook_idempotency (
  id              BIGSERIAL PRIMARY KEY,
  tenant_id       BIGINT NOT NULL,
  source          TEXT   NOT NULL,            -- e.g. 'line'
  event_id        TEXT   NOT NULL,            -- LINE webhook event id
  payload_hash    TEXT   NOT NULL,            -- sha256 of normalized body
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (tenant_id, source, event_id)
);

-- Optional: index for fast lookup
CREATE INDEX IF NOT EXISTS idx_webhook_idempotency_lookup
ON webhook_idempotency (tenant_id, source, event_id);
```

### 3.2 Webhook route — idempotent, transactional punch upsert

```ts
// /opt/axentx/workio/server/src/routes/webhook/line.ts
import { Router, Request, Response } from 'express';
import { pool } from '../../db';
import crypto from 'crypto';

const router = Router();

function hashPayload(body: any): string {
  return crypto.createHash('sha256').update(JSON.stringify(body)).digest('hex');
}

router.post('/line', async (req: Request, res: Response) => {
  const { events } = req.body || {};
  if (!events || !Array.isArray(events) || events.length === 0) {
    return res.status(400).json({ error: 'invalid payload' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const ev of events) {
      const eventId = ev?.source?.userId && ev?.mode && ev?.webhookEventId
        ? ev.webhookEventId
        : null;
      const source = 'line';
      // derive tenant_id from channel or from user mapping (simplified)
      // In production, map source.userId -> tenant_id via your user/channel table.
      const tenantId = await resolveTenantId(client, ev);
      if (!tenantId) {
        // skip unknown tenant but continue processing others
        continue;
      }

      const payloadHash = hashPayload(ev);

      // Idempotency check
      const idem = await client.query(
        `SELECT 1 FROM webhook_idempotency WHERE tenant_id = $1 AND source = $2 AND event_id = $3`,
        [tenantId, source, eventId]
      );
      if (idem.rows.length > 0) {
        // already processed — skip but continue ack so LINE stops retrying
        continue;
      }

      // Record idempotency first (best-effort; if punch insert fails we still avoid replays)
      await client.query(
        `INSERT INTO webhook_idempotency (tenant_id, source, event_id, payload_hash)
         VALUES ($1, $2, $3, $4)`,
        [tenantId, source, eventId, payloadHash]
      );

      // Handle clock in/out
      if (ev.type === 'message' && ev.message?.type === 'text') {
        const text = (ev.message.text || '').trim().toLowerCase();
        const userId = await resolveUserIdByLineUid(client, tenantId, ev.source.userId);
        if (!userId) continue;

        if (text === 'clock in' || text === 'เข้างาน') {
          // upsert: close any open punch (defensive) then insert new one
          await client.query(
            `UPDATE punches SET clock_out_at = NOW()
             WHERE tenant_id = $1 AND user_id = $2 AND clock_out_at IS NULL`,
            [tenantId, userId]
          );
          await client.query(
            `INSERT INTO punches (tenant_id, user_id, clock_in_at, clock_in_lat, clock_in_lon, line_user_id)
             VALUES ($1, $2, NOW(), $3, $4, $5)`,
            [tenantId, userId, null, null, ev.source.userId]
          );
        } else if (text === 'clock out' || text === 'เลิกงาน') {
          await client.query(
            `UPDATE punches
             SET clock_out_at = NOW()
             WHERE tenant_id = $1 AND user_id = $2 AND clock_out_at IS NULL`,
            [tenantId, userId]
          );
        }
      }
    }

    await client.query('COMMIT');
    res.status(200).json({ ok: true });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('LINE webhook error', err);
    // 5xx will cause LINE to retry (idempotency prevents duplicates)
    res.status(500).json({ error: 'internal' });
  } finally {
    client.release();
  }
});

// Minimal resolvers — replace with your real mappings
async function resolveTenantId(client: any, ev: any): Promise<number | null> {
  // Example: map by channel or user. Returning 1 as placeholder.
  return 1;
}
async function resolveUserIdByLineUid(client: any, tenantId: number, lineUid: string): Promise<number | null> {
  const r = await client.query(
    `SELECT id FROM users WHERE tenant_id = $1 AND line_user_id = $2`,
    [tenantId, lineUid]
  );
  return r.rows[0]?.id || null;
}

export default router;
```

### 3.3 Apply migrations

```bash
cd /opt/axentx/workio
psql workio < server/src/db/schema.sql
```

## 4. Verification

1. **Schema check**  
   ```bash
   psql workio -c "\d punches"
   psql workio -c "\d webhook_idempotency"
   ```
   Confirm partial unique index `idx_punches_one_open` and idempotency table exist.

2. **Duplicate prevention (manual)**  
   - Clock in via LINE text `clock in`.  
   - Immediately simulate LINE retry by sending same webhook payload again (use `curl` with same JSON).  
   - Query punches: `SELECT * FROM punches WHERE user_id = <uid> ORDER BY created_at DESC LIMIT 5;` — should show one open punch.

3. **Concurrency test (quick)**  
   Use a small script to fire two concurrent POSTs with same event payload. After both complete, verify only one punch row exists and one idempotency row exists.

4. **Idempotency table**  
   ```bash
   psql workio -c "SELECT tenant_id, source, event_id FROM webhook_idempotency ORDER BY created_at DESC LIMIT 5;"
   ```
   Each processed event should appear once.

5. **Behavioral check**  
   - Clock in → open punch.  
   - Clock out → punch closed.  
   - Clock in again → new punch row (allowed).  
   - No rows with `clock_out_at IS NULL` for same tenant+user beyond one.

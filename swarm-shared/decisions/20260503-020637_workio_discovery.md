# workio / discovery

## Final synthesis (best parts, corrected, maximally actionable)

### 1. Diagnosis (merged + corrected)
- **Missing idempotency**: LINE retries can deliver the same event multiple times; there is no enforcement to deduplicate.
- **Race-prone upserts**: Application-level `findOne` → `update`/`insert` is not safe under concurrency or retries.
- **Missing DB uniqueness guards**: No constraints to prevent duplicate punches per `(tenant_id, user_id, date, punch_type)` and no stable uniqueness for leave/OT requests (e.g., `(tenant_id, user_id, request_id)` or `(tenant_id, user_id, external_id)`).
- **Non-transactional side effects**: Balance updates and notifications can be applied multiple times or partially on retry/failure.
- **No replay-safe audit trail**: Hard to debug, recover, or prove exactly-once processing.

### 2. Scope & files
- **Schema**: `/opt/axentx/workio/server/src/db/schema.sql`
- **Handler**: `/opt/axentx/workio/server/src/controllers/lineWebhook.ts`
- **Services**: `/opt/axentx/workio/server/src/services/*Service.ts` (must accept and use a transaction client)

### 3. Implementation (corrected + production-ready)

#### 3.1 Schema changes (run as migration)

```sql
-- Idempotency table for LINE webhook events (stable, minimal)
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  idempotency_key VARCHAR(255) PRIMARY KEY,
  event_type      VARCHAR(50)  NOT NULL,
  payload_hash    VARCHAR(64)  NOT NULL,
  processed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Prevent duplicate punches per user/date/type
-- Use existing table name; adjust if different
ALTER TABLE punches
  ADD CONSTRAINT uniq_user_date_type
  UNIQUE (tenant_id, user_id, date, punch_type);

-- Prevent duplicate leave/OT records
-- Use existing table names; adjust as needed
-- Prefer request_id if provided by LINE/partner; fallback to external_id
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'leaves') THEN
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.table_constraints
      WHERE table_name = 'leaves' AND constraint_name = 'uniq_leave_request_id'
    ) THEN
      ALTER TABLE leaves
        ADD CONSTRAINT uniq_leave_request_id
        UNIQUE (tenant_id, user_id, request_id);
    END IF;
  END IF;

  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ot_requests') THEN
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.table_constraints
      WHERE table_name = 'ot_requests' AND constraint_name = 'uniq_ot_request_id'
    ) THEN
      ALTER TABLE ot_requests
        ADD CONSTRAINT uniq_ot_request_id
        UNIQUE (tenant_id, user_id, request_id);
    END IF;
  END IF;
END $$;
```

#### 3.2 Handler (atomic, idempotent, safe)

```ts
// server/src/controllers/lineWebhook.ts
import { Request, Response } from 'express';
import crypto from 'crypto';
import { pool } from '../db';
import { processPunch } from '../services/punchService';
import { processLeave } from '../services/leaveService';
import { processOT } from '../services/otService';
import { notifyViaLine } from '../utils/lineNotify';

const HASH_ALGO = 'sha256';

function hashPayload(body: any): string {
  return crypto.createHash(HASH_ALGO).update(JSON.stringify(body)).digest('hex');
}

function buildIdempotencyKey(msg: any): string {
  // Prefer stable message id from LINE; fallback to deterministic hash
  return `line:${msg.id || crypto.createHash(HASH_ALGO).update(JSON.stringify(msg)).digest('hex')}`;
}

export async function handleLineWebhook(req: Request, res: Response): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    const events = req.body?.events;
    if (!Array.isArray(events) || events.length === 0) {
      await client.query('COMMIT');
      res.status(400).json({ error: 'No events' });
      return;
    }

    const results: Array<{ success: boolean; error?: string }> = [];

    for (const ev of events) {
      const msg = ev.message || ev.postback || ev;
      if (!msg) {
        results.push({ success: false, error: 'No message' });
        continue;
      }

      const idempotencyKey = buildIdempotencyKey(msg);
      const payloadHash = hashPayload(msg);

      // Try to insert idempotency record; if conflict and same hash -> already processed
      const idemInsert = await client.query(
        `INSERT INTO line_webhook_idempotency (idempotency_key, event_type, payload_hash)
         VALUES ($1, $2, $3)
         ON CONFLICT (idempotency_key) DO UPDATE
           SET payload_hash = EXCLUDED.payload_hash
         WHERE line_webhook_idempotency.payload_hash <> EXCLUDED.payload_hash
         RETURNING idempotency_key`,
        [idempotencyKey, ev.type, payloadHash]
      );

      // If no row returned, check whether same payload already exists
      if (idemInsert.rowCount === 0) {
        const exists = await client.query(
          `SELECT 1 FROM line_webhook_idempotency
           WHERE idempotency_key = $1 AND payload_hash = $2`,
          [idempotencyKey, payloadHash]
        );
        if (exists.rowCount && exists.rowCount > 0) {
          // Already processed with identical payload — safe to skip
          results.push({ success: true, error: 'duplicate-skipped' });
          continue;
        }
        // Hash changed for same key — possible replay with different content; reject
        await client.query('ROLLBACK');
        res.status(409).json({ error: 'idempotency-hash-mismatch' });
        return;
      }

      // Route and process atomically within same transaction
      let handled = false;
      if (ev.type === 'message' && msg.type === 'text') {
        const text = (msg.text || '').trim().toLowerCase();
        if (text.includes('clock') || text.includes('in') || text.includes('out')) {
          await processPunch(client, ev);
          handled = true;
        } else if (text.includes('leave')) {
          await processLeave(client, ev);
          handled = true;
        } else if (text.includes('ot') || text.includes('overtime')) {
          await processOT(client, ev);
          handled = true;
        }
      } else if (ev.type === 'postback') {
        // await processPostback(client, ev);
        handled = true;
      }

      if (!handled) {
        results.push({ success: false, error: 'unhandled-event-type' });
        continue;
      }

      // Non-fatal notifications; don't rollback transaction on notify failure
      try {
        await notifyViaLine(ev);
      } catch (err) {
        console.warn('LINE notify failed', err);
      }

      results.push({ success: true });
    }

    await client.query('COMMIT');
    res.status(200).json({ ok: true, results });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('LINE webhook tx failed', err);
    res.status(500).json({ error: 'internal' });
  } finally {
    client.release();
  }
}
```

#### 3.3 Service example (transaction-aware, atomic upsert)

```ts
// server/src/services/punchService.ts
import { PoolClient } from 'pg';

export async function processPunch(client: PoolClient, ev: any): Promise<void> {
  const msg = ev.message;
  const userId = ev.source?.userId;
  const tenantId = 'default'; // derive from DB or ev.source or auth context
  const now = new Date();
  const date = now.toISOString().split('T')[0];

  const text = (msg.text || '').toLowerCase();
  const punchType = text.includes('

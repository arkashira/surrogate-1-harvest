# workio / discovery

## Final consolidated solution (best parts, corrected, actionable)

### 1. Diagnosis (merged + clarified)
- **Duplicate processing from LINE retries**: no idempotency key or idempotency table → retries (network blips, slow consumer, 5xx) create extra punches.
- **Race condition on open punches**: no DB-level enforcement of “one open punch per user” (`clock_out_at IS NULL`) → concurrent clock-ins can bypass app checks.
- **No cheap de-duplication layer**: without an idempotency table/index, duplicate detection requires expensive scans or is best-effort only.
- **Webhook latency increases retries**: heavy checks/geo calls before ack increase chance LINE retries; handler must ack quickly after idempotency acceptance.
- **Missing observability & cleanup**: no metrics/logs for duplicates and no TTL/index to bound idempotency table growth.

### 2. Schema changes (corrected + safe)

```sql
-- /opt/axentx/workio/server/src/db/schema.sql

-- 1) One open punch per (tenant,user).
-- Use a unique partial index (more flexible/standard than constraint on some PG setups).
-- If you prefer a constraint, replace with:
-- ALTER TABLE punches ADD CONSTRAINT one_open_punch_per_user UNIQUE (tenant_id, user_id) WHERE clock_out_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_open_punch_per_user
  ON punches (tenant_id, user_id)
  WHERE clock_out_at IS NULL;

-- 2) Idempotency table to dedupe LINE retries.
-- Keep key simple and stable: prefer LINE deliveryId when available.
CREATE TABLE IF NOT EXISTS punch_idempotency (
  id             BIGSERIAL PRIMARY KEY,
  tenant_id      BIGINT      NOT NULL,
  user_id        BIGINT      NOT NULL,
  event_type     TEXT        NOT NULL, -- 'clock_in' | 'clock_out'
  idempotency_key TEXT       NOT NULL,
  punch_id       BIGINT,               -- optional FK to punches
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uniq_idempotency_key UNIQUE (tenant_id, user_id, event_type, idempotency_key)
);

-- Fast lookup + bounded cleanup
CREATE INDEX IF NOT EXISTS idx_idempotency_recent
  ON punch_idempotency (tenant_id, user_id, event_type, idempotency_key)
  WHERE created_at > NOW() - INTERVAL '24 hours';
```

Notes:
- Prefer `UNIQUE INDEX` for portability; if your team requires a constraint, use the commented `ALTER TABLE ... ADD CONSTRAINT`.
- Do not add redundant indexes; the partial index above covers recent dedupe and cleanup.

### 3. Webhook handler (corrected + production-ready)

Key principles:
- Derive a **stable idempotency key** from LINE (`deliveryId` when present).
- **Check idempotency first** inside the transaction; skip work if already processed.
- Enforce “one open punch” via DB uniqueness/index; handle conflicts gracefully.
- **Auto-close** an open punch on duplicate clock-in only if your policy allows (explicit, configurable).
- **Acknowledge quickly** (200) after transaction commit to reduce LINE retries.
- Log duplicates/errors for observability; return 200 only when safe, otherwise 5xx to allow retry.

```typescript
// /opt/axentx/workio/server/src/routes/webhook/line.ts
import { Router, Request, Response } from 'express';
import { pool } from '../db';

const router = Router();

function makeIdempotencyKey(event: any): string {
  // LINE may provide deliveryId in the event envelope or headers.
  // Prefer an explicit deliveryId; fallback to deterministic composite.
  const deliveryId = event.deliveryId;
  if (deliveryId) return String(deliveryId);

  const userId = event.source?.userId;
  const ts = event.timestamp;
  const type = event.type;
  if (userId && ts) return `${userId}-${ts}-${type}`;

  // Last resort: stable hash-ish from payload subset (avoid randomness)
  return JSON.stringify({ userId: userId, ts, type, msgId: event.message?.id });
}

router.post('/line', async (req: Request, res: Response) => {
  const events = req.body.events;
  if (!Array.isArray(events) || events.length === 0) return res.sendStatus(200);

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const ev of events) {
      if (!ev?.source?.userId) continue;

      const userId = Number(ev.source.userId);
      const tenantId = Number(ev.source.tenantId || ev.tenantId || 1); // adapt to your payload
      const eventType = ev.type === 'message' && ev.message?.text?.includes('ออก') ? 'clock_out' : 'clock_in';
      const idemKey = makeIdempotencyKey(ev);

      // 1) Idempotency check (fast, inside tx)
      const idem = await client.query(
        `SELECT punch_id FROM punch_idempotency
         WHERE tenant_id = $1 AND user_id = $2 AND event_type = $3 AND idempotency_key = $4`,
        [tenantId, userId, eventType, idemKey]
      );

      if (idem.rows.length > 0) {
        // Already processed — skip but continue other events
        continue;
      }

      // 2) Resolve latest open punch
      const latest = await client.query(
        `SELECT id FROM punches
         WHERE tenant_id = $1 AND user_id = $2 AND clock_out_at IS NULL
         ORDER BY clock_in_at DESC LIMIT 1`,
        [tenantId, userId]
      );

      let punchId: number | null = null;

      if (eventType === 'clock_in') {
        if (latest.rows.length > 0) {
          // Policy: auto-close previous open punch to respect uniqueness.
          // Make this configurable if business rules differ.
          await client.query(
            `UPDATE punches SET clock_out_at = clock_in_at, updated_at = NOW()
             WHERE id = $1 AND clock_out_at IS NULL`,
            [latest.rows[0].id]
          );
        }

        const insert = await client.query(
          `INSERT INTO punches (tenant_id, user_id, clock_in_at, clock_out_at, created_at, updated_at)
           VALUES ($1, $2, NOW(), NULL, NOW(), NOW()) RETURNING id`,
          [tenantId, userId]
        );
        punchId = insert.rows[0].id;
      } else if (eventType === 'clock_out') {
        if (latest.rows.length === 0) {
          // No open punch: create a punch with same in/out time
          const insert = await client.query(
            `INSERT INTO punches (tenant_id, user_id, clock_in_at, clock_out_at, created_at, updated_at)
             VALUES ($1, $2, NOW(), NOW(), NOW(), NOW()) RETURNING id`,
            [tenantId, userId]
          );
          punchId = insert.rows[0].id;
        } else {
          const update = await client.query(
            `UPDATE punches SET clock_out_at = NOW(), updated_at = NOW()
             WHERE id = $1 AND clock_out_at IS NULL RETURNING id`,
            [latest.rows[0].id]
          );
          punchId = update.rows[0]?.id || null;
        }
      }

      // 3) Record idempotency (must be inside same tx to prevent races)
      await client.query(
        `INSERT INTO punch_idempotency (tenant_id, user_id, event_type, idempotency_key, punch_id)
         VALUES ($1, $2, $3, $4, $5)`,
        [tenantId, userId, eventType, idemKey, punchId]
      );
    }

    await client.query('COMMIT');
    // Acknowledge quickly to reduce LINE retries
    res.sendStatus(200);
  } catch (err: any) {
    await client.query('ROLLBACK').catch(() => {});
    // Log for observability
    console.error('LINE webhook processing failed', { error: err?.message, stack: err?.stack });

    // Return 200 only if failure is non-retryable (e.g., validation).
    // For transient DB/network errors, prefer 5xx so LINE may retry.
    const isValidationError = err?.code && ['23505','2350

# workio / discovery

## Final synthesized answer

### 1. Diagnosis (merged, duplicates removed)

- **No database-level idempotency**: duplicate `punch` rows on LINE redelivery/concurrency because uniqueness is enforced only in app logic or not at all.
- **No durable LINE delivery event log**: retries and replays rely on transient request state; no audit trail for compliance or debugging.
- **Clock-in/out mutations are not atomic with idempotency checks**: race between SELECT-then-INSERT/UPDATE allows duplicates under concurrent webhook deliveries.
- **Missing back-pressure and retry hygiene**: handler can fail silently or retry without idempotency, causing cascading duplicates or lost punches.
- **Missing tenant-scoped uniqueness**: `line_message_id` must be scoped by tenant to prevent cross-tenant collisions in multi-tenant deployments.

---

### 2. Proposed change (merged scope)

- **Files**:
  - `/opt/axentx/workio/server/src/db/schema.sql` — add durable event log and DB-level uniqueness.
  - `/opt/axentx/workio/server/src/routes/line/webhook.ts` — atomic upsert + event log in a single transaction.
- **Goal**: guarantee exactly-once processing per `(tenant_id, line_message_id)` and durable audit of every delivery.

---

### 3. Implementation (merged + corrected)

#### 3.1 Schema changes

```sql
-- /opt/axentx/workio/server/src/db/schema.sql

-- Durable LINE delivery event log (idempotency + audit)
CREATE TABLE IF NOT EXISTS line_delivery_events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  line_message_id TEXT NOT NULL,
  event_type      TEXT NOT NULL,            -- 'clock_in', 'clock_out', 'leave_request', etc.
  entity_id       UUID,                     -- e.g., punch_id or leave_id
  payload         JSONB NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, line_message_id)
);

-- Ensure punches can enforce idempotency at DB level
-- If line_message_id does not exist, add it; make it tenant-scoped unique
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS line_message_id TEXT;

-- Prefer a partial unique index (safe with NULLs) or a composite unique constraint.
-- Composite unique constraint (preferred if you always populate tenant_id + line_message_id for LINE punches):
ALTER TABLE punches
  ADD CONSTRAINT uq_tenant_line_message UNIQUE (tenant_id, line_message_id);

-- Optional partial unique index if you want to allow NULL line_message_id without conflict:
-- CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_line_message_nonnull
--   ON punches (tenant_id, line_message_id)
--   WHERE line_message_id IS NOT NULL;

-- Indexes for common access patterns
CREATE INDEX IF NOT EXISTS idx_punches_tenant_user_time ON punches (tenant_id, user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_line_delivery_events_lookup ON line_delivery_events (tenant_id, line_message_id);
```

#### 3.2 Webhook handler (atomic upsert + event log)

```ts
// /opt/axentx/workio/server/src/routes/line/webhook.ts
import { pool } from '../../db';
import { v4 as uuidv4 } from 'uuid';

export async function handleLineWebhook(req, res) {
  const { events } = req.body;
  const tenantId = req.tenant?.id; // set by prior auth middleware
  if (!tenantId) return res.status(400).json({ error: 'tenant missing' });

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const ev of events) {
      const lineMessageId = ev?.message?.id;
      if (!lineMessageId) continue;

      // 1) Idempotency: skip if already processed for this tenant
      const exists = await client.query(
        `SELECT 1 FROM line_delivery_events WHERE tenant_id = $1 AND line_message_id = $2`,
        [tenantId, lineMessageId]
      );
      if (exists.rows.length > 0) continue;

      // 2) Interpret event (simplified: clock in/out via text)
      const text = (ev?.message?.text || '').trim().toLowerCase();
      const isClockIn = text === 'in' || text === 'clock in';
      const isClockOut = text === 'out' || text === 'clock out';

      let entityId = null;
      if (isClockIn || isClockOut) {
        // Atomic insert with idempotency guard via uq_tenant_line_message.
        // If you need business-rule checks (e.g., no duplicate clock-ins per day),
        // perform them here inside the transaction before insert.
        const punchResult = await client.query(
          `INSERT INTO punches (id, tenant_id, user_id, type, line_message_id, created_at, metadata)
           VALUES ($1, $2, $3, $4, $5, now(), $6)
           ON CONFLICT (tenant_id, line_message_id) DO NOTHING
           RETURNING id`,
          [
            uuidv4(),
            tenantId,
            ev.source.userId, // LINE user; map to internal user_id in real usage
            isClockIn ? 'clock_in' : 'clock_out',
            lineMessageId,
            JSON.stringify({ raw: ev })
          ]
        );
        if (punchResult.rows.length > 0) entityId = punchResult.rows[0].id;
      }

      // 3) Record durable delivery event (always insert; UNIQUE prevents duplicates)
      await client.query(
        `INSERT INTO line_delivery_events (tenant_id, line_message_id, event_type, entity_id, payload)
         VALUES ($1, $2, $3, $4, $5)`,
        [
          tenantId,
          lineMessageId,
          isClockIn ? 'clock_in' : isClockOut ? 'clock_out' : 'unknown',
          entityId,
          ev
        ]
      );
    }

    await client.query('COMMIT');
    res.status(200).json({ ok: true });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('LINE webhook processing failed', err);
    // Return 500 so LINE may retry; idempotency prevents duplicates on retry
    res.status(500).json({ error: 'processing failed' });
  } finally {
    client.release();
  }
}
```

---

### 4. Verification (merged + concrete)

- **Run migration** and confirm objects exist:
  ```bash
  psql -d workio -f /opt/axentx/workio/server/src/db/schema.sql
  # Verify line_delivery_events and uq_tenant_line_message exist
  ```

- **Duplicate delivery test**:
  ```bash
  curl -X POST http://localhost:3000/webhook/line \
    -H "Content-Type: application/json" \
    -d '{"events":[{"type":"message","message":{"id":"test-dup-001","text":"in"},"source":{"userId":"U123"}}]}'
  ```
  Repeat 5x; verify only one punch row and one `line_delivery_events` row for `test-dup-001`.

- **Concurrent delivery test**:
  ```bash
  for i in {1..10}; do
    curl -s -X POST http://localhost:3000/webhook/line \
      -H "Content-Type: application/json" \
      -d '{"events":[{"type":"message","message":{"id":"concurrent-001","text":"out"},"source":{"userId":"U123"}}]}' &
  done
  wait
  ```
  Confirm exactly one `clock_out` punch and one event row for `concurrent-001`.

- **Tenant isolation test** (if multi-tenant setup available):
  Send same `line_message_id` for two different tenants and verify both are recorded (unique per tenant).

---

### 5. Operational notes (merged best practices)

- **Idempotency**: the `UNIQUE (tenant_id, line_message_id)` on `line_delivery_events` plus `ON CONFLICT DO NOTHING` on `punches` guarantees exactly-once processing per message per tenant.
- **Atomicity**: all per-message work (business logic + punch insert + event log) occurs in one transaction with a single idempotency check.
- **Retry

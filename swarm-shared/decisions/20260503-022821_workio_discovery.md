# workio / discovery

## 1. Diagnosis

- No persistent, tenant-scoped idempotency key for LINE webhook punches → retries create duplicate clock-in/out rows.
- Upsert logic is application-level and non-atomic under concurrency (read-then-write) → race conditions between duplicate deliveries.
- Missing DB unique constraint to enforce one punch per employee per date per event type → database allows duplicates.
- No deduplication window or de-duplication log for late/retried webhooks (same event_id can arrive minutes apart).
- No observability (request_id / event_id) in logs to detect or debug duplicate deliveries.

## 2. Proposed change

File: `/opt/axentx/workio/server/src/db/schema.sql`  
Scope: add `tenant_id`-scoped uniqueness and an idempotency table; modify punches table to include `line_event_id` and unique constraint.  

File: `/opt/axentx/workio/server/src/routes/line/webhook.ts` (or equivalent)  
Scope: make punch creation an atomic upsert keyed by `(tenant_id, employee_id, line_event_id)` and return fast on duplicates.

## 3. Implementation

### 3.1 Schema changes (`schema.sql`)

```sql
-- Add line_event_id to punches if not present
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS line_event_id TEXT;

-- Ensure tenant_id exists and is not null (adjust if column name differs)
-- Create unique constraint: one LINE event processed once per tenant
CREATE UNIQUE INDEX IF NOT EXISTS uq_punches_tenant_line_event
  ON punches (tenant_id, line_event_id)
  WHERE line_event_id IS NOT NULL;

-- Optional: idempotency table for non-punch LINE events (future-proof)
CREATE TABLE IF NOT EXISTS line_event_dedupe (
  tenant_id      TEXT NOT NULL,
  line_event_id  TEXT NOT NULL,
  handler        TEXT NOT NULL,
  created_at     TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (tenant_id, line_event_id, handler)
);
```

### 3.2 Webhook handler (pseudo-route)

```ts
// server/src/routes/line/webhook.ts
import { db } from '../../db';

export async function handleLineWebhook(req, res) {
  const { events } = req.body;
  const tenantId = req.tenant.id; // set by auth middleware

  for (const ev of events) {
    if (ev.type !== 'message' || ev.message?.type !== 'text') continue;

    // Fast path: dedupe at DB level
    const result = await db.query(
      `INSERT INTO punches (tenant_id, employee_id, line_event_id, clock_in, clock_out, created_at)
       SELECT $1, $2, $3, $4, NULL, NOW()
       WHERE NOT EXISTS (
         SELECT 1 FROM punches
         WHERE tenant_id = $1 AND line_event_id = $3
       )
       ON CONFLICT (tenant_id, line_event_id) DO NOTHING
       RETURNING id`,
      [tenantId, ev.source.userId, ev.message.id, new Date(ev.timestamp)]
    );

    if (result.rowCount === 0) {
      // duplicate — safe to ignore
      continue;
    }

    // Optional: emit LINE reply or log
    console.log(`Processed punch tenant=${tenantId} event=${ev.message.id}`);
  }

  res.status(200).send('OK');
}
```

If your table uses separate `clock_in`/`clock_out` rows (one per event), adapt the upsert to update the existing row instead (same unique key).

### 3.3 Deployment checklist (2h budget)

- [ ] Apply schema diff to dev DB.
- [ ] Update webhook route to include `line_event_id` and atomic upsert.
- [ ] Add tenant resolution to webhook middleware if missing.
- [ ] Smoke test with duplicate payloads.

## 4. Verification

- **Unit**: insert same `(tenant_id, employee_id, line_event_id)` twice → second insert must return 0 rows and not create a new punch.
- **DB constraint**: attempt to insert duplicate `line_event_id` for same tenant → must raise unique violation.
- **Integration**: use `ngrok` to replay a real LINE webhook JSON twice (5–60s apart) → verify only one punch row created and no application errors.
- **Logs**: confirm duplicate deliveries are logged as “duplicate — ignored” and do not throw exceptions.

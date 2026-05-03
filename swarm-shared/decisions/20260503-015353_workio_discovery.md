# workio / discovery

## Final synthesized solution (best parts, resolved contradictions, concrete + correct)

**Core principles applied**
- Enforce exactly-once at the **database layer** (constraints + atomic upserts), not only in application logic.
- Use a **stable, verifiable idempotency key** derived from the LINE event (prefer a LINE-provided `eventId`; fall back to a hash of `X-Line-Signature` + canonical payload).
- Keep idempotency records **bounded in time** (TTL) so storage doesn’t grow forever and legitimate late retries within the retry window are deduplicated.
- Make active-punch uniqueness **simple and robust**: one active punch per user (clocked_out_at IS NULL) enforced by a partial unique index.
- Perform punch updates **atomically** in a single transaction that also records idempotency, so races and retries cannot create duplicates or inconsistent state.

---

### 1. Schema changes (`workio/server/src/db/schema.sql`)

```sql
-- Idempotency for LINE webhook events (bounded TTL)
CREATE TABLE IF NOT EXISTS line_event_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  line_event_id   TEXT,            -- populated if LINE provides it
  payload_hash    TEXT NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Automatically purge old idempotency records (e.g., > 48h)
-- Run as a periodic job (cron/pg_cron) or application task:
-- DELETE FROM line_event_idempotency WHERE created_at < NOW() - INTERVAL '48 hours';

-- Ensure one active punch per user (active = clocked_out_at IS NULL)
-- This is the source-of-truth constraint preventing double-active punches.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_punch_per_user
  ON punches (user_id, tenant_id)
  WHERE clocked_out_at IS NULL;

-- Optional: prevent duplicate punches within same minute for same user+tenant+type
-- Helps catch application-level bugs; keep if your domain requires it.
-- CREATE UNIQUE INDEX IF NOT EXISTS uniq_punch_per_user_date_type
--   ON punches (user_id, tenant_id, date_trunc('minute', clocked_in_at), punch_type)
--   WHERE clocked_out_at IS NOT NULL;
```

**Notes**
- The partial unique index on `(user_id, tenant_id)` where `clocked_out_at IS NULL` is simpler and safer than a multi-column constraint with nullable columns; it guarantees exactly one active punch.
- Do **not** use a `DEFERRABLE` constraint for this rule—active punch violations should fail immediately.
- Keep idempotency table minimal and keyed by a deterministic string; index is implicit via PRIMARY KEY.

---

### 2. Idempotency key derivation (stable + verifiable)

```ts
import crypto from 'crypto';

function deriveIdempotencyKey(lineEvent: any, lineSignature: string): string {
  // Prefer a stable LINE event ID if provided; otherwise hash signature + canonical payload.
  if (lineEvent.id && typeof lineEvent.id === 'string') {
    return `line:event:${lineEvent.id}`;
  }
  const payloadHash = crypto
    .createHash('sha256')
    .update(JSON.stringify(lineEvent))
    .digest('hex');
  return `line:sig:${crypto.createHash('sha256').update(lineSignature).digest('hex')}:${payloadHash}`;
}
```

---

### 3. Webhook handler (`workio/server/src/routes/line/webhook.ts`)

```ts
import { Pool } from 'pg';
import crypto from 'crypto';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

function deriveIdempotencyKey(lineEvent: any, lineSignature: string): string {
  if (lineEvent.id && typeof lineEvent.id === 'string') {
    return `line:event:${lineEvent.id}`;
  }
  const payloadHash = crypto.createHash('sha256').update(JSON.stringify(lineEvent)).digest('hex');
  return `line:sig:${crypto.createHash('sha256').update(lineSignature).digest('hex')}:${payloadHash}`;
}

export async function handleLineWebhook(req, res) {
  const lineSignature = req.get('X-Line-Signature') || '';
  const events = req.body.events || [];

  for (const ev of events) {
    const idemKey = deriveIdempotencyKey(ev, lineSignature);
    const userId = ev.source?.userId; // adapt to your payload shape
    const tenantId = 'default-tenant'; // resolve from user/context
    if (!userId) continue;

    const client = await pool.connect();
    try {
      await client.query('BEGIN');

      // 1) Idempotency check+insert (atomic)
      const idemRes = await client.query(
        `INSERT INTO line_event_idempotency (idempotency_key, line_event_id, payload_hash)
         VALUES ($1, $2, $3)
         ON CONFLICT (idempotency_key) DO NOTHING
         RETURNING id`,
        [idemKey, ev.id || null, crypto.createHash('sha256').update(JSON.stringify(ev)).digest('hex')]
      );
      if (idemRes.rowCount === 0) {
        await client.query('COMMIT');
        continue; // duplicate event, skip processing
      }

      // 2) Process clock in/out atomically
      if (ev.type === 'clock_out') {
        // Close the active punch for this user+tenant
        await client.query(
          `UPDATE punches
           SET clocked_out_at = NOW(), updated_at = NOW()
           WHERE user_id = $1 AND tenant_id = $2 AND clocked_out_at IS NULL
           RETURNING *`,
          [userId, tenantId]
        );
        // If no active punch exists, this is a no-op (log if desired)
      } else {
        // Clock in: attempt insert; unique active-punch index prevents double active punches
        try {
          await client.query(
            `INSERT INTO punches (user_id, tenant_id, clocked_in_at, clocked_out_at, punch_type)
             VALUES ($1, $2, NOW(), NULL, $3)`,
            [userId, tenantId, 'clock_in']
          );
        } catch (err: any) {
          if (err.code === '23505') {
            // Active punch already exists — safe to ignore or optionally update metadata
            // Do NOT auto-close+reopen here unless business rules require it.
          } else {
            throw err;
          }
        }
      }

      await client.query('COMMIT');
    } catch (err) {
      await client.query('ROLLBACK');
      console.error('Failed to process LINE event', { err, eventId: ev.id });
      // Return non-200 to allow LINE retry (idempotency will dedupe)
      res.status(500).json({ error: 'processing_failed' });
      client.release();
      return;
    } finally {
      if (!client._ending) client.release();
    }
  }

  res.status(200).json({ ok: true });
}
```

---

### 4. Verification checklist (run these)

1. **Schema checks**
   - Confirm `line_event_idempotency` exists with PRIMARY KEY on `idempotency_key`.
   - Confirm partial unique index `uniq_active_punch_per_user` exists on `punches(user_id, tenant_id) WHERE clocked_out_at IS NULL`.

2. **Idempotency**
   - Send the same LINE event twice (same signature + payload) → second request must not create a new punch row.
   - Verify `line_event_idempotency` contains one entry for the event.

3. **Concurrency**
   - Fire two simultaneous clock-in requests for the same user → only one active punch row should exist (DB constraint prevents second insert).

4. **Clock-out correctness**
   - Clock in once, then send multiple clock-out events → only one `clocked_out_at` is set; subsequent attempts are no-ops.

5. **Active-punch invariant**
   - Attempt to insert a second active punch for the same user via direct SQL → must be rejected by unique index.

6. **TTL hygiene**
   - Run the cleanup for `line_event_idempotency` and confirm old records are removed (e.g., >48h).

---

### Why this resolves contradictions

- **Idempotency key**: Candidate 1 used `line_signature` alone; Candidate 2 emphasized `(

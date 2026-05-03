# workio / discovery

## 1. Diagnosis

- Missing **database-level partial unique index** to enforce at most one open punch (`clock_out_at IS NULL`) per `(user_id, date(clock_in_at))`, allowing duplicates under LINE webhook retries or client double-taps.
- App-layer “find-then-insert” clock-in/clock-out handlers are race-prone; concurrent LINE webhook deliveries can create duplicate open punches or orphaned punches.
- No idempotency key on webhook handler; LINE may retry deliveries and the handler re-processes without deduplication.
- No defensive constraint to prevent clock-in when an open punch already exists (should auto-close or reject).
- Missing audit column (`created_at` precision + optional `updated_at`) to reliably detect duplicates and support idempotency.

## 2. Proposed change

- **File**: `/opt/axentx/workio/server/src/db/schema.sql`
- **Scope**: add partial unique index on punches; add idempotency table for LINE webhooks; tighten constraints on punch lifecycle.

## 3. Implementation

```sql
-- /opt/axentx/workio/server/src/db/schema.sql
-- Add partial unique index to enforce at most one open punch per user per calendar day
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_open_per_day
ON punches (user_id, DATE(clock_in_at))
WHERE clock_out_at IS NULL;

-- Optional: ensure clock_in_at is always present and sensible
ALTER TABLE punches
  ALTER COLUMN clock_in_at SET NOT NULL;

-- Idempotency table for LINE webhook deliveries (simple, effective)
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  event_id        TEXT NOT NULL,
  processed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  payload_hash    TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for fast expiry / cleanup (keep 24-48h)
CREATE INDEX IF NOT EXISTS idx_line_idempotency_processed_at
ON line_webhook_idempotency (processed_at);

-- Optional helper to auto-close stale open punches older than 24h (safety)
-- Run manually or via cron; not part of schema but useful in discovery:
-- UPDATE punches SET clock_out_at = clock_in_at + INTERVAL '24 hours', updated_at = NOW()
-- WHERE clock_out_at IS NULL AND clock_in_at < NOW() - INTERVAL '24 hours';
```

Backend handler sketch (Node/Express) — apply idempotency in the LINE webhook route:

```js
// /opt/axentx/workio/server/src/routes/line.js
const crypto = require('crypto');
const db = require('../db');

async function handleLineWebhook(req, res) {
  const body = req.body;
  const eventId = body.events?.[0]?.webhookEventId || body.events?.[0]?.deliveryId;
  if (!eventId) return res.status(400).send('missing event id');

  // Deterministic idempotency key from eventId (or use X-Line-Signature + body hash)
  const idempotencyKey = `line:${eventId}`;
  const payloadHash = crypto.createHash('sha256').update(JSON.stringify(body)).digest('hex');

  const client = await db.pool.connect();
  try {
    await client.query('BEGIN');

    // Check idempotency
    const { rows } = await client.query(
      'SELECT 1 FROM line_webhook_idempotency WHERE idempotency_key = $1',
      [idempotencyKey]
    );
    if (rows.length > 0) {
      await client.query('ROLLBACK');
      return res.status(200).send('duplicate');
    }

    // Record idempotency first
    await client.query(
      'INSERT INTO line_webhook_idempotency (idempotency_key, event_id, payload_hash) VALUES ($1, $2, $3)',
      [idempotencyKey, eventId, payloadHash]
    );

    // Process events (example: clock-in/out)
    for (const ev of body.events) {
      if (ev.type === 'message' && ev.message?.type === 'text') {
        const userId = ev.source.userId; // map LINE userId -> workio user_id via your mapping table
        const text = ev.message.text.trim().toLowerCase();

        if (text === 'clock in' || text === 'clock out') {
          // Use atomic upsert pattern: try to close existing open punch on clock-in if policy allows,
          // otherwise enforce unique open punch via DB constraint (will throw on duplicate).
          if (text === 'clock in') {
            // Close any stale open punch for same user same day (safety) then insert new punch
            await client.query(`
              UPDATE punches
              SET clock_out_at = NOW(), updated_at = NOW()
              WHERE user_id = $1 AND clock_out_at IS NULL AND DATE(clock_in_at) = DATE(NOW())
            `, [userId]);

            await client.query(`
              INSERT INTO punches (user_id, clock_in_at, clock_out_at, source, line_event_id)
              VALUES ($1, NOW(), NULL, 'line', $2)
            `, [userId, eventId]);
          } else {
            // Clock out: close latest open punch
            const { rows: updated } = await client.query(`
              UPDATE punches
              SET clock_out_at = NOW(), updated_at = NOW()
              WHERE user_id = $1 AND clock_out_at IS NULL
              AND id = (
                SELECT id FROM punches
                WHERE user_id = $1 AND clock_out_at IS NULL
                ORDER BY clock_in_at DESC
                LIMIT 1
              )
              RETURNING id
            `, [userId]);

            if (updated.rowCount === 0) {
              // No open punch — optionally create a punch with clock_in=clock_out or reject
              // For discovery, log and continue
              console.warn('clock out with no open punch', { userId, eventId });
            }
          }
        }
      }
    }

    await client.query('COMMIT');
    res.status(200).send('ok');
  } catch (err) {
    await client.query('ROLLBACK');
    // Unique violation from partial index -> duplicate open punch rejected
    if (err.code === '23505') {
      console.warn('duplicate open punch prevented by DB constraint', { userId: body.events?.[0]?.source?.userId, err: err.message });
      return res.status(409).send('duplicate open punch');
    }
    console.error('line webhook error', err);
    res.status(500).send('error');
  } finally {
    client.release();
  }
}

module.exports = { handleLineWebhook };
```

## 4. Verification

1. **Schema check** — run `psql workio -c "\d punches"` and confirm the partial unique index exists:
   ```
   \d punches
   -- look for idx_punches_one_open_per_day
   ```
   Confirm idempotency table exists: `\d line_webhook_idempotency`.

2. **Duplicate prevention test** — simulate concurrent clock-ins for same user same day:
   ```bash
   # In psql, attempt two open punches (should fail on second)
   BEGIN;
   INSERT INTO punches (user_id, clock_in_at, clock_out_at) VALUES (1, NOW(), NULL);
   INSERT INTO punches (user_id, clock_in_at, clock_out_at) VALUES (1, NOW(), NULL); -- should error 23505
   ROLLBACK;
   ```

3. **Webhook idempotency test** — send same LINE event payload twice to `/webhook/line` (with same event id) and verify:
   - First request: 200 OK, punch created.
   - Second request: 200 OK (or 409 if policy), no duplicate punch row, idempotency row present.

4. **Race test** — use a simple parallel HTTP client (e.g., `ab` or `hey`) to POST two clock-in events for same user at nearly the same time; confirm only one open punch row exists afterward and no constraint violations crash the server.

5. **Cleanup check** — verify old idempotency records can be pruned:
   ```sql
   DELETE FROM line_webhook_idempotency WHERE processed_at < NOW() - INTERVAL '48 hours';
   ```

# workio / discovery

## Final synthesized answer

### Diagnosis (merged)
- **Missing idempotency handling** on `/webhook/line` allows LINE at-least-once retries to create duplicate punch records.
- **No DB-level enforcement** of “one open punch per user” (`clock_out_at IS NULL`) permits double-clock-in races.
- **No request deduplication layer** (in-memory or Redis) to cheaply reject replays within short windows.
- **Webhook handler performs side effects before acknowledging LINE**, increasing retry risk.
- **No audit field to trace original LINE event IDs** for reconciliation.

### Proposed changes (merged)
- **Files**: `workio/server/src/db/schema.sql` (constraint + idempotency table), `workio/server/src/routes/webhook/line.ts` (webhook handler), optional `workio/server/src/db/migrations/` (safe deploy).
- **Scope**: add partial unique index; add idempotency table + unique index on `line_event_id`; wrap clock-in in upsert/transaction; return 200 fast on duplicate; add in-memory recent-event cache for fast rejection; store `line_event_id` on punches for audit.

### Implementation

```sql
-- workio/server/src/db/schema.sql
-- 1) Partial unique constraint: at most one open punch per user
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_open
ON punches (user_id)
WHERE clock_out_at IS NULL;

-- 2) Idempotency table for LINE webhook events (lightweight)
CREATE TABLE IF NOT EXISTS line_webhook_idempotency (
  line_event_id VARCHAR(255) PRIMARY KEY,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_line_idempotency_created
ON line_webhook_idempotency (created_at);

-- 3) Store LINE event id on punches for audit and optional dedupe
ALTER TABLE punches
ADD COLUMN IF NOT EXISTS line_event_id VARCHAR(255);
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_line_event_id
ON punches (line_event_id)
WHERE line_event_id IS NOT NULL;
```

```ts
// workio/server/src/routes/webhook/line.ts
import { Router } from 'express';
import { db } from '../../db/index.js';

const router = Router();

// In-memory recent-event cache (per worker). For multi-instance, use Redis.
const RECENT_TTL_MS = 5 * 60 * 1000; // 5m
const recentEvents = new Map<string, number>(); // event_id -> timestamp

function isRecent(eventId: string): boolean {
  const ts = recentEvents.get(eventId);
  if (!ts) return false;
  if (Date.now() - ts < RECENT_TTL_MS) return true;
  recentEvents.delete(eventId);
  return false;
}

function markRecent(eventId: string) {
  recentEvents.set(eventId, Date.now());
}

router.post('/line', async (req, res) => {
  try {
    const events = req.body.events;
    if (!Array.isArray(events) || events.length === 0) {
      return res.status(400).send('no events');
    }

    for (const ev of events) {
      const lineEventId = ev.webhookEventId || ev.deliveryId;
      const userId = ev.source?.userId;
      if (!lineEventId || !userId) continue;

      // Fast in-memory dedupe (best-effort)
      if (isRecent(lineEventId)) {
        continue;
      }

      if (ev.type === 'message' && ev.message?.type === 'text') {
        const text = ev.message.text.trim().toLowerCase();

        if (text === 'in' || text === 'clock in') {
          await db.begin(async (tx) => {
            // Record idempotency first (optimistic)
            await tx`
              INSERT INTO line_webhook_idempotency (line_event_id)
              VALUES (${lineEventId})
              ON CONFLICT DO NOTHING
            `;

            // Try insert open punch; partial index prevents second open
            const inserted = await tx`
              INSERT INTO punches (user_id, clock_in_at, clock_in_lat, clock_in_lng, line_user_id, line_event_id)
              VALUES (${userId}, NOW(), NULL, NULL, ${userId}, ${lineEventId})
              ON CONFLICT (user_id) WHERE clock_out_at IS NULL
              DO NOTHING
              RETURNING *
            `;

            // If no row inserted, user already has an open punch — safe no-op
            if (inserted.length > 0) {
              markRecent(lineEventId);
            }
          });
        } else if (text === 'out' || text === 'clock out') {
          await db.begin(async (tx) => {
            await tx`
              INSERT INTO line_webhook_idempotency (line_event_id)
              VALUES (${lineEventId})
              ON CONFLICT DO NOTHING
            `;

            await tx`
              UPDATE punches
              SET clock_out_at = NOW(), line_event_id = ${lineEventId}
              WHERE id = (
                SELECT id FROM punches
                WHERE user_id = ${userId} AND clock_out_at IS NULL
                ORDER BY clock_in_at DESC LIMIT 1
              )
            `;

            markRecent(lineEventId);
          });
        }
      }
    }

    // LINE expects 200 OK quickly
    res.sendStatus(200);
  } catch (err) {
    console.error('LINE webhook error:', err);
    // Still return 200 to prevent LINE retries for transient errors
    // (or return 500 if you want LINE to retry on server errors — prefer 200 + alerting)
    res.sendStatus(200);
  }
});

export { router as lineRouter };
```

### Verification (merged)
- **Schema check**: run `psql workio -c "\d punches"` and confirm partial index `idx_punches_one_open` exists; confirm `line_webhook_idempotency` table and `line_event_id` column/index exist.
- **Duplicate retry test**: send same LINE webhook payload twice (same `deliveryId`/`webhookEventId`) — second request should not create a second punch and should return 200.
- **Race test**: fire two concurrent clock-in requests for same user — only one open punch should be created (check `SELECT count(*) FROM punches WHERE user_id = ? AND clock_out_at IS NULL` returns 1).
- **Clock-out flow**: clock in, then clock out — verify `clock_out_at` populated and subsequent clock-in allowed (new open row created).
- **Logs/metrics**: add temporary console logs or query idempotency table to confirm duplicates are skipped; monitor `line_event_id` population for audit.

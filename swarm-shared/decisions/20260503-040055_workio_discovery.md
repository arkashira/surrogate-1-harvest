# workio / discovery

## 1. Diagnosis

- **No idempotency key on LINE webhook events** — retries on timeout/non-2xx can create duplicate clock/leave/OT records.
- **No transactional upsert for clock events** — concurrent/retried deliveries can double-insert for same user+date+session.
- **Missing unique constraint** — schema allows duplicate `(user_id, date, session_type)` which breaks data integrity.
- **No deduplication guard in webhook handler** — endpoint relies on client-side retry backoff instead of server-side idempotency.
- **No audit trail for webhook deliveries** — cannot distinguish retries from new events during incident analysis.

## 2. Proposed change

Add idempotent ingestion for LINE clock-in/out events:

- **Schema**: `server/src/db/schema.sql` — add unique constraint on `clock_events(user_id, date, session_type)` and optional `idempotency_key` column.
- **Handler**: `server/src/routes/line.ts` (or webhook handler) — upsert with `ON CONFLICT DO NOTHING` and commit-before-200.
- **Migration**: one-time migration to backfill constraint safely.

## 3. Implementation

### 3.1 Schema change (`server/src/db/schema.sql`)

```sql
-- Add idempotency key if you want explicit dedupe tracking (optional)
ALTER TABLE clock_events
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

-- Prevent duplicate clock records per user per date per session
CREATE UNIQUE INDEX IF NOT EXISTS idx_clock_events_unique_session
  ON clock_events (user_id, date, session_type)
  WHERE session_type IN ('clock_in', 'clock_out');

-- Optional: index for idempotency lookups
CREATE UNIQUE INDEX IF NOT EXISTS idx_clock_events_idempotency
  ON clock_events (idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

### 3.2 Webhook handler idempotent upsert (`server/src/routes/line.ts`)

```ts
import { db } from '../db';
import { clockEvents } from '../db/schema';
import { eq } from 'drizzle-orm';

export async function handleLineClockEvent(req, res) {
  const { userId, eventType, timestamp, idempotencyKey } = req.body;

  // Normalize date (YYYY-MM-DD) and session type
  const date = new Date(timestamp).toISOString().split('T')[0];
  const sessionType = eventType === 'clock_in' ? 'clock_in' : 'clock_out';

  try {
    await db.transaction(async (tx) => {
      // Idempotent insert: if unique constraint violation, skip
      const result = await tx
        .insert(clockEvents)
        .values({
          userId,
          date,
          sessionType,
          timestamp: new Date(timestamp),
          idempotencyKey,
          createdAt: new Date(),
        })
        .onConflictDoNothing()
        .returning();

      // If you prefer upsert (update existing), use:
      // await tx.insert(clockEvents).values(...).onConflictDoUpdate({
      //   target: [clockEvents.userId, clockEvents.date, clockEvents.sessionType],
      //   set: { timestamp: new Date(timestamp), idempotencyKey }
      // });

      if (result.length === 0) {
        // Duplicate — safe to ignore
        console.log(`Duplicate clock event ignored: ${userId} ${date} ${sessionType}`);
      }
    });

    // Commit-before-200: transaction already committed by Drizzle
    res.status(200).json({ ok: true });
  } catch (err) {
    console.error('Clock event processing failed', err);
    // Let LINE retry on 5xx; do not 200 on real failure
    res.status(500).json({ error: 'processing_failed' });
  }
}
```

### 3.3 Apply migration

```bash
cd /opt/axentx/workio
psql workio < server/src/db/schema.sql
```

## 4. Verification

1. **Constraint exists**
   ```bash
   psql workio -c "\d clock_events"
   # Confirm unique index idx_clock_events_unique_session
   ```

2. **Duplicate insert is rejected**
   ```bash
   # Simulate duplicate webhook payload
   curl -X POST http://localhost:3000/webhook/line \
     -H "Content-Type: application/json" \
     -d '{"userId":1,"eventType":"clock_in","timestamp":"2026-05-03T09:00:00Z"}'

   # Repeat same payload — second call must return 200 and not create new row
   psql workio -c "SELECT count(*) FROM clock_events WHERE user_id=1 AND date='2026-05-03';"
   # Expected: count = 1
   ```

3. **Retries after failure still succeed**
   - Force a 5xx on first attempt (e.g., throw in handler), then fix and retry — second attempt should insert cleanly (no duplicate).

4. **Idempotency key uniqueness (if used)**
   ```bash
   psql workio -c "INSERT INTO clock_events(user_id,date,session_type,idempotency_key) VALUES (1,'2026-05-03','clock_in','abc123') ON CONFLICT DO NOTHING;"
   # Repeat same idempotency_key — second insert must be ignored
   ```

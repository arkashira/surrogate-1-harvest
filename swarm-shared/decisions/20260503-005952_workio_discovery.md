# workio / discovery

## 1. Diagnosis

- Missing **database-level uniqueness guard** for open punches: no constraint prevents multiple `clock_out_at IS NULL` rows per user per calendar day, allowing duplicates under LINE webhook retries or concurrency.
- App-layer “find-then-insert” is racy: `/webhook/line/clock` does `findOne` then `insert`/`update`, which can lose to concurrent requests and create overlapping open punches.
- No **idempotency key** from LINE webhook retries: LINE can redeliver the same event; without deduplication by `webhook_event_id` or similar, retries create duplicate punches.
- **Clock-out ambiguity**: if two clock-out events arrive (retry/duplicate), the later can update the wrong row or create a second open row before the first is closed, corrupting attendance state.
- **No safe upsert path**: missing `ON CONFLICT` clause or advisory lock to serialize per-user-per-day open punch transitions.

## 2. Proposed change

- **File**: `/opt/axentx/workio/server/src/db/schema.sql` (add constraint)  
- **File**: `/opt/axentx/workio/server/src/routes/line.ts` (add idempotent upsert for clock in/out)  
- **Scope**: add partial unique index for open punches and convert clock handler to atomic upsert using `ON CONFLICT DO UPDATE` keyed by user+date, plus dedupe by LINE event id stored in a small lookup.

## 3. Implementation

### 3.1 Add DB constraint (schema.sql)

```sql
-- Prevent multiple open punches per user per calendar day
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_user_day_open
ON punches (user_id, DATE(clock_in_at))
WHERE clock_out_at IS NULL;

-- Optional: dedupe table for LINE event ids (lightweight)
CREATE TABLE IF NOT EXISTS line_event_dedupe (
  event_id TEXT PRIMARY KEY,
  punch_id INTEGER NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 3.2 Idempotent upsert in line route (line.ts)

```ts
// Simplified handler sketch for /webhook/line/clock
import { db } from '../db';

export async function handleLineClock(req, res) {
  const { userId, eventId, type } = req.body; // type: 'clock_in' | 'clock_out'
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());

  // Fast dedupe: skip if we already processed this event
  const exists = await db.oneOrNone(
    'SELECT 1 FROM line_event_dedupe WHERE event_id = $1',
    [eventId]
  );
  if (exists) return res.sendStatus(200);

  if (type === 'clock_in') {
    // Atomic upsert: ensure at most one open punch per user per day
    const result = await db.one(
      `
      INSERT INTO punches (user_id, clock_in_at, clock_out_at)
      VALUES ($1, $2, NULL)
      ON CONFLICT (user_id, DATE(clock_in_at))
      WHERE clock_out_at IS NULL
      DO UPDATE SET clock_in_at = EXCLUDED.clock_in_at
      RETURNING id
      `,
      [userId, now]
    );

    await db.none(
      'INSERT INTO line_event_dedupe(event_id, punch_id) VALUES ($1, $2)',
      [eventId, result.id]
    );
  } else {
    // Clock-out: close today's open punch
    const result = await db.oneOrNone(
      `
      UPDATE punches
      SET clock_out_at = $2
      WHERE user_id = $1
        AND DATE(clock_in_at) = $3
        AND clock_out_at IS NULL
      RETURNING id
      `,
      [userId, now, today]
    );

    if (result) {
      await db.none(
        'INSERT INTO line_event_dedupe(event_id, punch_id) VALUES ($1, $2)',
        [eventId, result.id]
      );
    }
  }

  res.sendStatus(200);
}
```

### 3.3 Apply migration safely

```bash
cd /opt/axentx/workio/server
psql workio < src/db/schema.sql   # or run via migration runner if present
```

## 4. Verification

1. **Schema check**  
   ```sql
   SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'punches';
   -- Should show idx_punches_user_day_open with WHERE clock_out_at IS NULL
   ```

2. **Race test (quick)**  
   - Simulate concurrent clock-ins for same user:
     ```bash
     for i in {1..10}; do
       curl -X POST http://localhost:3000/webhook/line/clock \
         -H 'Content-Type: application/json' \
         -d '{"userId":1,"eventId":"test-'$i'","type":"clock_in"}' &
     done
     wait
     ```
   - Verify only one open punch exists:
     ```sql
     SELECT * FROM punches WHERE user_id = 1 AND clock_out_at IS NULL;
     ```

3. **Idempotency test**  
   - Send same `eventId` twice; second request should be no-op and not create duplicate dedupe row or alter punch.

4. **Clock-out correctness**  
   - Clock in, then send two clock-out events with different `eventId`s; only the first should close the punch, second should be ignored (no second open row created).

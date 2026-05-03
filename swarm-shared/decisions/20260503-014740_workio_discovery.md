# workio / discovery

## 1. Diagnosis

- **No idempotency key on punch writes**: LINE retries webhook events with the same `X-Line-Signature`/event ID; current code does `findOne → if-exists-update-else-insert` which is non-atomic and allows duplicates under concurrency.
- **No unique constraint at DB level**: `punches` table lacks a unique constraint on `(user_id, punch_date, shift_type, line_event_id)` so race conditions create duplicate rows.
- **Non-atomic upsert path**: Application-level upsert (`findOne` then `update`/`insert`) is unsafe under parallel retries; requires DB-level `ON CONFLICT` to be safe.
- **Missing audit column for external event traceability**: No column to store the LINE event idempotency token; prevents deduplication and replay debugging.
- **No idempotency index for webhook replays**: Without an index on the idempotency key, duplicate checks remain slow and non-atomic at scale.

## 2. Proposed change

- **File scope**: `workio/server/src/db/schema.sql` (add column + unique constraint + index) and `workio/server/src/routes/punch.ts` (use atomic `ON CONFLICT DO UPDATE`).
- **Change type**: Add `line_event_id` column to `punches`, add unique constraint on `(user_id, punch_date, shift_type, line_event_id)`, and convert upsert to a single `INSERT ... ON CONFLICT DO UPDATE` statement.

## 3. Implementation

### 3.1 DB schema migration (`schema.sql`)

```sql
-- Add idempotency column if not exists
ALTER TABLE punches
ADD COLUMN IF NOT EXISTS line_event_id VARCHAR(255);

-- Create unique constraint for idempotent punch writes
-- (user_id, punch_date, shift_type, line_event_id)
-- line_event_id may be null for legacy/manual punches; exclude nulls from uniqueness
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_line_event_unique
ON punches (user_id, punch_date, shift_type, line_event_id)
WHERE line_event_id IS NOT NULL;
```

### 3.2 Atomic upsert in route (`routes/punch.ts`)

```ts
import { db } from '../db';

export async function handlePunchWebhook(req, res) {
  const { userId, punchDate, shiftType, lineEventId, latitude, longitude, imageUrl } = req.body;

  // Validate required fields
  if (!userId || !punchDate || !shiftType) {
    return res.status(400).json({ error: 'Missing required fields' });
  }

  try {
    // Atomic upsert: insert or update on conflict (idempotent)
    const result = await db.query(
      `INSERT INTO punches (user_id, punch_date, shift_type, line_event_id, latitude, longitude, image_url, updated_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
       ON CONFLICT (user_id, punch_date, shift_type, line_event_id)
       DO UPDATE SET
         latitude = EXCLUDED.latitude,
         longitude = EXCLUDED.longitude,
         image_url = EXCLUDED.image_url,
         updated_at = NOW()
       RETURNING *`,
      [userId, punchDate, shiftType, lineEventId, latitude, longitude, imageUrl]
    );

    return res.status(200).json({ punch: result.rows[0], ok: true });
  } catch (err) {
    console.error('Punch webhook failed', err);
    return res.status(500).json({ error: 'Internal server error' });
  }
}
```

### 3.3 Optional: ensure idempotency for LINE signature verification

If you want to store the raw `X-Line-Signature` for audit, add a small middleware that persists the signature alongside `line_event_id` (or in a separate `webhook_events` table). For now, storing `line_eventId` is sufficient to deduplicate LINE retries.

## 4. Verification

1. **Apply migration**:
   ```bash
   psql workio < server/src/db/schema.sql
   ```
   Confirm index exists:
   ```sql
   SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'punches';
   ```

2. **Idempotency test (single process)**:
   - Send same payload twice (same `lineEventId`) to `/webhook/punch`.
   - Expect: 1 row created, second request returns same row (no duplicate).

3. **Concurrency test**:
   - Run 10 parallel requests with identical payload (same `lineEventId`).
   - Query count:
     ```sql
     SELECT COUNT(*) FROM punches WHERE line_event_id = '<test-id>';
     ```
   - Expect: exactly 1 row.

4. **Conflict update behavior**:
   - First request: `{ latitude: 1.1, longitude: 2.2 }`
   - Second request (same `lineEventId`): `{ latitude: 3.3, longitude: 4.4 }`
   - Expect: row updated to second lat/lon (not duplicated).

5. **Null handling**:
   - Send payload without `lineEventId` (manual punch).
   - Expect: insert succeeds; multiple manual punches for same `(user_id, punch_date, shift_type)` are allowed (constraint excludes nulls).

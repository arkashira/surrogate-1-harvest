# workio / discovery

## 1. Diagnosis

- **No idempotent webhook handling**: LINE retries identical webhook deliveries with the same `X-Line-Signature` on transient network errors/timeouts; current `findOne → update/insert` is non-atomic and races under concurrency, producing duplicate punches.
- **Missing idempotency key**: no DB unique constraint or idempotency token (e.g., `line_delivery_id` or `X-Line-Signature` + timestamp hash) to deduplicate at write time.
- **Clock-in/out state races**: concurrent or retried webhooks can create multiple `attendances` for the same user/day without a reliable “latest valid punch” guard, corrupting shift state.
- **No transactional upsert**: attendance writes are not wrapped in a serializable/atomic upsert (PostgreSQL `ON CONFLICT`), allowing duplicates between `find` and `insert`.
- **No webhook replay protection**: no short-term dedupe cache or DB index to cheaply reject replays within the same day or across retry windows.

## 2. Proposed change

- **File**: `/opt/axentx/workio/server/src/services/attendanceService.ts` (or equivalent handler module that processes LINE webhook punches).
- **Scope**: add atomic idempotent punch handling via a new `line_delivery_id` column (or `external_id`), unique constraint, and `ON CONFLICT DO NOTHING` upsert; add minimal index for fast dedupe.
- **Boundary**: touch only the attendance write path for clock-in/out events from LINE; leave leave/OT flows unchanged.

## 3. Implementation

### Step 1 — DB migration (one-time)

```sql
-- server/src/db/migrations/20260503_add_line_delivery_id.sql
ALTER TABLE attendances
  ADD COLUMN line_delivery_id TEXT NULL;

CREATE UNIQUE INDEX uq_attendances_line_delivery_id
  ON attendances (line_delivery_id)
  WHERE line_delivery_id IS NOT NULL;

-- Optional: ensure one active punch per user per day (if business rule is one clock-in/out pair)
-- CREATE UNIQUE INDEX uq_attendances_user_date_active
--   ON attendances (user_id, DATE(clock_in))
--   WHERE clock_out IS NULL;
```

Apply:
```bash
psql workio < server/src/db/migrations/20260503_add_line_delivery_id.sql
```

### Step 2 — Service change (atomic upsert)

```ts
// server/src/services/attendanceService.ts
import { pool } from '../db';

export async function recordPunch(payload: {
  userId: number;
  lineDeliveryId: string; // e.g., webhook event timestamp + signature hash or event.id
  type: 'clock_in' | 'clock_out';
  latitude?: number;
  longitude?: number;
  timestamp: Date;
}) {
  const { userId, lineDeliveryId, type, latitude, longitude, timestamp } = payload;

  // Atomic upsert by line_delivery_id; if duplicate delivery arrives, nothing happens.
  const res = await pool.query(
    `INSERT INTO attendances (user_id, line_delivery_id, clock_in, clock_out, latitude, longitude, created_at)
     VALUES ($1, $2, CASE WHEN $3 = 'clock_in' THEN $4 ELSE NULL END, CASE WHEN $3 = 'clock_out' THEN $4 ELSE NULL END, $5, $6, $7)
     ON CONFLICT (line_delivery_id) DO NOTHING
     RETURNING *`,
    [userId, lineDeliveryId, type, timestamp, latitude ?? null, longitude ?? null, new Date()]
  );

  // If duplicate, res.rowCount === 0
  return { applied: res.rowCount > 0, record: res.rows[0] ?? null };
}
```

### Step 3 — Webhook handler integration

```ts
// server/src/routes/lineWebhook.ts
import crypto from 'crypto';
import { recordPunch } from '../services/attendanceService';

function verifySignature(body: Buffer, signature: string, channelSecret: string): boolean {
  const expected = crypto
    .createHmac('sha256', channelSecret)
    .update(body)
    .digest('base64');
  return signature === expected;
}

export async function handleLineWebhook(req: any, res: any) {
  const signature = req.get('X-Line-Signature');
  const body = req.body; // raw buffer should be preserved upstream; adapt as needed
  const channelSecret = process.env.LINE_CHANNEL_SECRET!;

  if (!verifySignature(Buffer.from(JSON.stringify(body)), signature, channelSecret)) {
    return res.status(401).send('Invalid signature');
  }

  // Process each event idempotently
  const results = [];
  for (const event of body.events) {
    if (event.type !== 'message' && event.type !== 'postback') continue;

    // Derive stable idempotency key from LINE event
    const lineDeliveryId = event.source.userId + ':' + event.timestamp + ':' + event.replyToken;
    // Or use event.source.userId + ':' + event.message?.id if available for message events

    // Determine punch type from postback/message payload (simplified)
    const type = event.postback?.data === 'clock_out' ? 'clock_out' : 'clock_in';
    const userId = await resolveLineUserIdToLocal(event.source.userId); // implement mapping

    results.push(
      await recordPunch({
        userId,
        lineDeliveryId,
        type,
        timestamp: new Date(event.timestamp),
      })
    );
  }

  // Always 200 to stop LINE retries regardless of duplicates
  res.status(200).json({ ok: true, results });
}
```

### Step 4 — Optional: short-term in-memory dedupe for ultra-fast rejection (if desired)

```ts
// server/src/lib/dedupeCache.ts
const seen = new Set<string>();
export function isDuplicate(key: string): boolean {
  if (seen.has(key)) return true;
  seen.add(key);
  // keep bounded; acceptable for small throughput
  if (seen.size > 50000) seen.clear();
  return false;
}
```

Use before DB upsert to avoid DB hit on obvious replays within same process.

## 4. Verification

1. **Schema check**:
   ```bash
   psql workio -c "\d attendances"
   # confirm line_delivery_id column exists
   psql workio -c "\d uq_attendances_line_delivery_id"
   # confirm unique index
   ```

2. **Duplicate delivery simulation**:
   - Send same LINE webhook payload twice (same `X-Line-Signature` and body) within seconds.
   - Expect HTTP 200 both times and exactly one `attendances` row for that `line_delivery_id`.

3. **Concurrency test**:
   - Fire 10 parallel requests with identical payload.
   - Verify only one row inserted (`SELECT count(*) FROM attendances WHERE line_delivery_id = '...'` == 1).

4. **Behavior check**:
   - Normal clock-in and clock-out flows produce correct `clock_in`/`clock_out` timestamps and do not create extra rows.
   - Duplicate deliveries do not mutate existing rows (idempotent).

5. **Logs/metrics**:
   - Add a log line when `recordPunch` returns `applied: false` (duplicate detected) to confirm dedupe is working in production.

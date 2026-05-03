# workio / discovery

## 1. Diagnosis

- **No idempotency key enforcement on LINE webhook events** — LINE retries identical deliveries; duplicate clock-in/out and leave/OT records can be created.
- **Race-prone non-atomic upsert in attendance flow** — `findOne` → conditional `update`/`insert` leaves a window for duplicates under concurrency.
- **Missing server-side idempotency token handling for leave/OT requests** — client retries (network blips) can create duplicate requests.
- **No deduplication index on attendance/events tables** — duplicates can accumulate silently and corrupt reports.
- **Frontend does not surface or persist idempotency keys for retries** — user-facing retries (refresh/back) can re-submit.

## 2. Proposed change

Add atomic, idempotent handling for LINE clock-in/out and leave/OT requests:

- **Files**:
  - `server/src/db/schema.sql` — add unique constraint/index for idempotency.
  - `server/src/controllers/attendanceController.ts` — use `INSERT ... ON CONFLICT DO NOTHING` (or upsert) with idempotency key.
  - `server/src/controllers/leaveController.ts` — same pattern for leave/OT requests.
  - `server/src/routes/lineWebhook.ts` — extract idempotency key from LINE event (use `webhookEventId` + `userId` + type) and pass to controllers.
- **Scope**: backend only; no breaking API changes; safe to deploy independently.

## 3. Implementation

### 3.1 DB schema (add unique constraint)

```sql
-- server/src/db/schema.sql
-- Add to attendance table (if not exists)
ALTER TABLE attendance
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_idempotency
  ON attendance (idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- Add to leave_requests table (if not exists)
ALTER TABLE leave_requests
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_leave_requests_idempotency
  ON leave_requests (idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

### 3.2 Attendance controller — atomic upsert

```ts
// server/src/controllers/attendanceController.ts
import { pool } from '../db';

export async function recordAttendance({
  userId,
  tenantId,
  type, // 'CLOCK_IN' | 'CLOCK_OUT'
  location,
  idempotencyKey,
}: {
  userId: string;
  tenantId: string;
  type: 'CLOCK_IN' | 'CLOCK_OUT';
  location?: { lat: number; lng: number };
  idempotencyKey: string;
}) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Try insert; if idempotency_key exists, do nothing
    const insertRes = await client.query(
      `INSERT INTO attendance (user_id, tenant_id, type, location, idempotency_key, created_at)
       VALUES ($1, $2, $3, $4, $5, NOW())
       ON CONFLICT (idempotency_key) DO NOTHING
       RETURNING *`,
      [userId, tenantId, type, location ? JSON.stringify(location) : null, idempotencyKey]
    );

    // If no row inserted, return existing row for this idempotency key
    if (insertRes.rowCount === 0) {
      const existing = await client.query(
        `SELECT * FROM attendance WHERE idempotency_key = $1`,
        [idempotencyKey]
      );
      await client.query('COMMIT');
      return existing.rows[0];
    }

    await client.query('COMMIT');
    return insertRes.rows[0];
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}
```

### 3.3 Leave controller — idempotent create

```ts
// server/src/controllers/leaveController.ts
import { pool } from '../db';

export async function createLeaveRequest({
  userId,
  tenantId,
  leaveType,
  startDate,
  endDate,
  reason,
  idempotencyKey,
}: {
  userId: string;
  tenantId: string;
  leaveType: string;
  startDate: string;
  endDate: string;
  reason: string;
  idempotencyKey: string;
}) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    const insertRes = await client.query(
      `INSERT INTO leave_requests (user_id, tenant_id, leave_type, start_date, end_date, reason, status, idempotency_key, created_at)
       VALUES ($1, $2, $3, $4, $5, $6, 'PENDING', $7, NOW())
       ON CONFLICT (idempotency_key) DO NOTHING
       RETURNING *`,
      [userId, tenantId, leaveType, startDate, endDate, reason, idempotencyKey]
    );

    if (insertRes.rowCount === 0) {
      const existing = await client.query(
        `SELECT * FROM leave_requests WHERE idempotency_key = $1`,
        [idempotencyKey]
      );
      await client.query('COMMIT');
      return existing.rows[0];
    }

    await client.query('COMMIT');
    return insertRes.rows[0];
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}
```

### 3.4 LINE webhook — derive idempotency key

```ts
// server/src/routes/lineWebhook.ts
import { recordAttendance } from '../controllers/attendanceController';
import { createLeaveRequest } from '../controllers/leaveController';

function makeIdempotencyKey(event: any, userId: string): string {
  // Use LINE's webhookEventId when available; fallback to hash of event + timestamp
  if (event.webhookEventId) {
    return `line:${userId}:${event.webhookEventId}`;
  }
  // For non-LINE-originated internal retries, clients should send X-Idempotency-Key
  return `internal:${userId}:${Date.now()}:${Math.random().toString(36).slice(2)}`;
}

export async function handleLineWebhook(req, res) {
  const events = req.body.events || [];
  const results = [];

  for (const event of events) {
    const userId = event.source?.userId;
    if (!userId) continue;

    try {
      if (event.type === 'message' && event.message?.type === 'text') {
        const text = event.message.text.toLowerCase();
        const idempotencyKey = makeIdempotencyKey(event, userId);

        // Simple command handling for demo; map to your business rules
        if (text.includes('clock in') || text.includes('เข้างาน')) {
          const record = await recordAttendance({
            userId,
            tenantId: 'default-tenant', // resolve from user
            type: 'CLOCK_IN',
            location: event.source?.areaId ? { lat: 0, lng: 0 } : undefined, // enrich from GPS if available
            idempotencyKey,
          });
          results.push({ status: 'ok', record });
        } else if (text.includes('clock out') || text.includes('เลิกงาน')) {
          const record = await recordAttendance({
            userId,
            tenantId: 'default-tenant',
            type: 'CLOCK_OUT',
            location: undefined,
            idempotencyKey,
          });
          results.push({ status: 'ok', record });
        }
      }
    } catch (err) {
      console.error('LINE webhook handling error', err);
      results.push({ status: 'error', error: String(err) });
    }
  }

  res.json({ results });
}
```

### 3.5 Client-side: add idempotency header for non-LINE requests

For internal leave/OT submissions (non-LINE), require clients to send:

```http
POST /api/leave-requests
X-Idempotency-Key: <uuid>
```



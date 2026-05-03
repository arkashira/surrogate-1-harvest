# workio / discovery

## Final consolidated solution

**Core diagnosis (merged, de-duplicated)**
- LINE retries with identical `X-Line-Signature` + payload are verified but not deduplicated, allowing duplicate punches.
- The `findOne → update/insert` path is racy under concurrent replays or user double-taps and can create multiple active punches.
- No DB-level unique constraint enforces “one active punch per user/tenant” or “exactly-once per external event.”
- Clock-in/out state is derived from app queries and can diverge if duplicate rows exist.
- No persisted idempotency/external-event column to short-circuit replays cheaply or provide a stable audit key.

**Chosen approach (correctness + actionability)**
- Use **LINE signature as the idempotency key** (simple, available, stable per retry) plus a **partial unique index** to enforce one active punch per tenant+user.
- Perform **atomic `INSERT … ON CONFLICT DO NOTHING`** for the idempotency key, then **conditionally close any previous open punch** in the same transaction.
- Add **DB constraints** so correctness does not rely on app logic alone.

---

## 1. DB schema (run once)

File: `workio/server/src/db/schema.sql`

```sql
-- Idempotency/external-event key
ALTER TABLE punches ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(255);

-- One idempotent record per external event (LINE signature)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_idempotency_key
  ON punches (tenant_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- One active punch per tenant+user (prevents double-open rows)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_active_per_tenant_user
  ON punches (tenant_id, user_id)
  WHERE clock_out_time IS NULL;
```

Notes:
- `idempotency_key` stores `X-Line-Signature` (or a hash of the delivery ID if you prefer).  
- The partial index on `(tenant_id, user_id)` where `clock_out_time IS NULL` enforces the “one open punch” invariant at the DB level.  
- If your table is huge or write-heavy, benchmark index impact; correctness benefit outweighs cost for this workload.

---

## 2. Service layer — atomic upsert with idempotency

File: `workio/server/src/services/punch.service.ts`

```ts
import { PoolClient } from 'pg';
import { db } from '../db';

export interface PunchInput {
  tenantId: string;
  userId: string;
  // For clock-in: provide clockInTime (now), clockOutTime = null
  // For clock-out: provide clockOutTime (now), clockInTime is ignored (we close open punch)
  clockInTime?: Date;
  clockOutTime?: Date;
  latitude?: number;
  longitude?: number;
  idempotencyKey: string; // X-Line-Signature
}

/**
 * Atomic, idempotent punch handling.
 * Guarantees:
 * - Exactly one row per idempotencyKey.
 * - At most one active punch (clock_out_time IS NULL) per (tenant_id, user_id).
 */
export async function upsertPunch(input: PunchInput, client?: PoolClient): Promise<void> {
  const dbClient = client || db;

  // 1) Insert if not already processed (idempotency)
  await dbClient.query(
    `INSERT INTO punches (
       tenant_id, user_id, clock_in_time, clock_out_time,
       latitude, longitude, idempotency_key, created_at, updated_at
     ) VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),NOW())
     ON CONFLICT (tenant_id, idempotency_key) DO NOTHING`,
    [
      input.tenantId,
      input.userId,
      input.clockInTime || null,
      input.clockOutTime || null,
      input.latitude ?? null,
      input.longitude ?? null,
      input.idempotencyKey,
    ]
  );

  // 2) If this is a clock-out, close the currently open punch for this user/tenant
  //    (but do not close the one we just inserted if it was already a clock-out).
  //    If this is a clock-in and there is a previous open punch, close it so only one is active.
  const shouldClosePrevious = input.clockOutTime != null || input.clockInTime != null;
  if (shouldClosePrevious) {
    await dbClient.query(
      `UPDATE punches
       SET clock_out_time = COALESCE($1, NOW()), updated_at = NOW()
       WHERE tenant_id = $2
         AND user_id = $3
         AND clock_out_time IS NULL
         AND idempotency_key IS DISTINCT FROM $4`,
      [
        input.clockOutTime || null,
        input.tenantId,
        input.userId,
        input.idempotencyKey,
      ]
    );
  }
}
```

Behavior:
- **Idempotent**: repeated calls with same `idempotencyKey` do not create extra rows.
- **Atomic**: each statement runs in the provided client/transaction; wrap the call in a transaction if you combine with other writes.
- **Invariant preserved**: at most one row per `(tenant_id, user_id)` with `clock_out_time IS NULL`.

---

## 3. Webhook handler integration

File: `workio/server/src/routes/line.webhook.ts`

```ts
import { Router } from 'express';
import { upsertPunch } from '../services/punch.service';
import { verifySignature } from '../utils/line-signature';

const router = Router();

router.post('/webhook/line', async (req, res) => {
  const signature = req.get('X-Line-Signature') || '';
  const body = JSON.stringify(req.body);

  if (!verifySignature(body, signature)) {
    return res.status(401).send('Invalid signature');
  }

  const { source, events } = req.body;
  if (!events || !Array.isArray(events)) {
    return res.status(400).send('Invalid payload');
  }

  // Use a transaction if you want all events in this webhook atomic
  for (const ev of events) {
    if (ev.type === 'message' && ev.message.type === 'text') {
      const tenantId = source.userId; // adapt to your tenant<->line mapping
      const userId = source.userId;   // adapt as needed
      const now = new Date();
      const isClockOut = /^(out|เลิก|สิ้นสุด)$/i.test(ev.message.text);

      await upsertPunch({
        tenantId,
        userId,
        clockInTime: isClockOut ? undefined : now,
        clockOutTime: isClockOut ? now : undefined,
        idempotencyKey: signature, // LINE retry-safety
      });
    }
  }

  return res.status(200).send('OK');
});

export default router;
```

Notes:
- If you need per-user mapping (e.g., LINE userId → internal userId/tenantId), perform that lookup before calling `upsertPunch`.
- For stronger auditability, store `line_delivery_id` (from headers) alongside `idempotencyKey` if available.

---

## 4. Verification checklist

1. **Apply migration**
   ```bash
   psql workio < server/src/db/schema.sql
   ```
   Confirm:
   ```sql
   \d punches
   -- should show idempotency_key and the two partial indexes
   ```

2. **Replay test**
   - Send the same LINE webhook twice (identical signature and body).
   - Query:
     ```sql
     SELECT * FROM punches WHERE idempotency_key = '<signature>';
     ```
     → exactly one row.

3. **Concurrency test**
   - Simulate near-simultaneous clock-ins for the same tenant+user (different signatures).
   - Verify only one active punch remains (`clock_out_time IS NULL`).

4. **Active-punch invariant**
   - Attempt to create two open punches via direct SQL (should be blocked by partial unique index).

5. **UI/report sanity**
   - Confirm dashboard hours and logs show one punch per event and correct totals.

---

## 5. Operational notes

- **Transactions**: wrap `upsertPunch` in a transaction if you combine it with other writes that must be

# workio / discovery

## 1) Diagnosis

- No idempotency key enforcement on `/punches` write path → repeated LINE webhook deliveries or frontend retries create duplicate punch records.
- Missing DB uniqueness constraint for “one open punch per user” → allows multiple concurrent clock-ins or overlapping `clock_in`/`clock_out` rows for same tenant/user.
- Punch timestamps are stored without explicit day-boundary normalization → ambiguity when users cross midnight or operate in mixed timezones.
- No deduplication window or retry token on webhook handler → at-least-once delivery from LINE causes double punches within seconds.
- Frontend lacks debounce/disabled-state on clock-in/out button → UI spam compounds duplicate writes.

## 2) Proposed change

File: `/opt/axentx/workio/server/src/db/schema.sql`  
Add:  
- Unique partial index for open punches (`clock_out IS NULL`) per `(tenant_id, user_id)`.  
- Idempotency table `punch_idempotency` keyed by `(tenant_id, idempotency_key)` with TTL.  
- Alter `punches` to store `timezone` and normalize `clock_in` to UTC with explicit date boundary column `day_local` for simpler reporting.

File: `/opt/axentx/workio/server/src/routes/punches.ts`  
Add:  
- Idempotency check/upsert in POST `/punches` before insert.  
- Use `ON CONFLICT DO NOTHING` for open-punch uniqueness.

File: `/opt/axentx/workio/src/components/PunchButton.tsx` (or similar)  
Add:  
- Debounced button with `disabled` state during inflight request and 1.5s cooldown after success.

## 3) Implementation

```sql
-- /opt/axentx/workio/server/src/db/schema.sql
-- 1) Idempotency table (lightweight, TTL via cron or app cleanup)
CREATE TABLE IF NOT EXISTS punch_idempotency (
  idempotency_key TEXT NOT NULL,
  tenant_id       INTEGER NOT NULL,
  punch_id        INTEGER NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (tenant_id, idempotency_key)
);

-- 2) Ensure one open punch per user per tenant
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_open
ON punches (tenant_id, user_id)
WHERE clock_out IS NULL;

-- 3) Optional: add timezone/day_local for boundary clarity
ALTER TABLE punches ADD COLUMN IF NOT EXISTS timezone TEXT DEFAULT 'UTC';
ALTER TABLE punches ADD COLUMN IF NOT EXISTS day_local DATE
  GENERATED ALWAYS AS (DATE(clock_in AT TIME ZONE timezone)) STORED;
```

```ts
// /opt/axentx/workio/server/src/routes/punches.ts
import { Router, Request, Response } from 'express';
import { db } from '../db';
import { eq, and } from 'drizzle-orm';
import { punches, punchIdempotency } from '../db/schema';

const router = Router();

router.post('/', async (req: Request, res: Response) => {
  const { tenant_id, user_id, clock_in, clock_out, idempotency_key, timezone = 'UTC' } = req.body;

  if (!idempotency_key) {
    return res.status(400).json({ error: 'idempotency_key required' });
  }

  return await db.transaction(async (tx) => {
    // Idempotency check
    const existing = await tx.query.punchIdempotency.findFirst({
      where: and(
        eq(punchIdempotency.tenant_id, tenant_id),
        eq(punchIdempotency.idempotency_key, idempotency_key)
      ),
    });

    if (existing) {
      const punch = await tx.query.punches.findFirst({
        where: eq(punches.id, existing.punch_id),
      });
      return res.status(200).json(punch);
    }

    // Insert punch with uniqueness guard (ON CONFLICT DO NOTHING)
    const [inserted] = await tx
      .insert(punches)
      .values({
        tenant_id,
        user_id,
        clock_in: new Date(clock_in),
        clock_out: clock_out ? new Date(clock_out) : null,
        timezone,
      })
      .onConflictDoNothing()
      .returning();

    if (!inserted || inserted.length === 0) {
      // Likely violated unique open-punch constraint
      const open = await tx.query.punches.findFirst({
        where: and(
          eq(punches.tenant_id, tenant_id),
          eq(punches.user_id, user_id),
          eq(punches.clock_out, null)
        ),
      });
      if (open) {
        await tx.insert(punchIdempotency).values({
          tenant_id,
          idempotency_key,
          punch_id: open.id,
        });
        return res.status(200).json(open);
      }
      return res.status(409).json({ error: 'Could not create punch (conflict)' });
    }

    const punch = inserted[0];

    await tx.insert(punchIdempotency).values({
      tenant_id,
      idempotency_key,
      punch_id: punch.id,
    });

    return res.status(201).json(punch);
  });
});

export { router as punchesRouter };
```

```tsx
// /opt/axentx/workio/src/components/PunchButton.tsx
import React, { useState, useCallback } from 'react';

export function PunchButton({ onPunch }: { onPunch: () => Promise<void> }) {
  const [inflight, setInflight] = useState(false);
  const [cooldown, setCooldown] = useState(false);

  const handleClick = useCallback(async () => {
    if (inflight || cooldown) return;
    setInflight(true);
    try {
      await onPunch();
      setCooldown(true);
      setTimeout(() => setCooldown(false), 1500);
    } finally {
      setInflight(false);
    }
  }, [inflight, cooldown, onPunch]);

  return (
    <button
      onClick={handleClick}
      disabled={inflight || cooldown}
      className={`px-4 py-2 rounded font-medium ${
        inflight || cooldown
          ? 'bg-gray-300 cursor-not-allowed'
          : 'bg-blue-600 text-white hover:bg-blue-700'
      }`}
    >
      {inflight ? 'กำลังบันทึก...' : cooldown ? 'รอสักครู่' : 'ลงเวลา'}
    </button>
  );
}
```

## 4) Verification

- Run migration: `psql workio < server/src/db/schema.sql` and confirm index `idx_punches_one_open` and table `punch_idempotency` exist.
- Simulate duplicate LINE webhook: send two POSTs to `/punches` with same `idempotency_key` within seconds → second returns 200 with same punch row; DB contains exactly one row for that `(tenant_id, user_id)` with `clock_out IS NULL`.
- Attempt to create two open punches via API without idempotency (or with different keys) → second receives 409 and no second open row is created.
- In UI, rapid clicks on PunchButton trigger only one request; button shows disabled/cooldown states and network tab shows single POST.
- Confirm `day_local` and `timezone` columns populate correctly for a punch created with non-UTC timezone.

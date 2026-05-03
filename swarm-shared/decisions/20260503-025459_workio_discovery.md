# workio / discovery

## 1. Diagnosis

- No idempotency key enforcement on LINE webhook `/webhook/line` → duplicate clock-in/out events on retries create double punches and corrupt state.
- Clock-in/out API and webhook lack tenant-scoped uniqueness guard for open punches → race conditions allow multiple concurrent “open” punches per user per tenant.
- Frontend has no optimistic UI or client-side request deduplication for clock-in/out → perceived lag and double-taps queue duplicate requests.
- Missing explicit idempotency table for cross-entity safety (punch/leave/OT) → retries across services can create duplicates.
- No retry/back-off strategy or circuit-breaker for LINE API outbound calls (notifications) → transient failures can lose notifications.

## 2. Proposed change

Add server-side idempotency + tenant-scoped uniqueness for punch creation and lightweight client-side deduplication for the clock-in/out button.

- **Files**:
  - `server/src/routes/line.ts` (or wherever `/webhook/line` lives) — add idempotency check before processing events.
  - `server/src/services/punchService.ts` (or equivalent) — enforce one open punch per `(tenantId, userId)` and provide idempotent upsert by `idempotencyKey`.
  - `server/src/db/schema.sql` — add `punch_idempotency` table (or extend punches with `idempotency_key` + unique constraint).
  - `workio/src/components/ClockButton.tsx` — add optimistic UI + client-side request deduplication (in-flight promise map).

## 3. Implementation

### 3.1 DB: idempotency table + unique open punch constraint

```sql
-- server/src/db/schema.sql

-- Idempotency log for punch operations (and later leave/OT)
CREATE TABLE IF NOT EXISTS punch_idempotency (
  idempotency_key TEXT NOT NULL,
  tenant_id       TEXT NOT NULL,
  user_id         TEXT NOT NULL,
  punch_id        TEXT NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (idempotency_key)
);

-- Ensure only one open punch per tenant+user (null clock_out_at means open)
-- If your punches table uses a different name, adjust accordingly.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_punch_per_user
ON punches (tenant_id, user_id)
WHERE clock_out_at IS NULL;
```

### 3.2 Service: idempotent punch creation

```ts
// server/src/services/punchService.ts
import { db } from '../db';

export async function createOrUpdatePunch({
  tenantId,
  userId,
  idempotencyKey,
  clockInAt,
  clockOutAt = null,
  location,
}: {
  tenantId: string;
  userId: string;
  idempotencyKey: string;
  clockInAt: Date;
  clockOutAt?: Date | null;
  location?: { lat: number; lng: number };
}) {
  return db.transaction(async (trx) => {
    // 1) Idempotency check
    const existing = await trx
      .selectFrom('punch_idempotency')
      .where('idempotency_key', '=', idempotencyKey)
      .select('punch_id')
      .executeTakeFirst();

    if (existing) {
      // Return existing punch (safe for retries)
      const punch = await trx
        .selectFrom('punches')
        .where('id', '=', existing.punch_id)
        .selectAll()
        .executeTakeFirstOrThrow();
      return punch;
    }

    // 2) Try to find open punch for this tenant+user
    const openPunch = await trx
      .selectFrom('punches')
      .where('tenant_id', '=', tenantId)
      .where('user_id', '=', userId)
      .where('clock_out_at', 'is', null)
      .selectAll()
      .executeTakeFirst();

    let punchId: string;
    if (openPunch) {
      // Update existing open punch (e.g., clock-out)
      await trx
        .updateTable('punches')
        .set({
          clock_out_at: clockOutAt,
          updated_at: new Date(),
        })
        .where('id', '=', openPunch.id)
        .execute();
      punchId = openPunch.id;
    } else {
      // Insert new punch
      const [punch] = await trx
        .insertInto('punches')
        .values({
          tenant_id: tenantId,
          user_id: userId,
          clock_in_at: clockInAt,
          clock_out_at: clockOutAt,
          location: location ? JSON.stringify(location) : null,
          created_at: new Date(),
          updated_at: new Date(),
        })
        .returning(['id'])
        .execute();
      punchId = punch.id;
    }

    // 3) Record idempotency
    await trx
      .insertInto('punch_idempotency')
      .values({
        idempotency_key: idempotencyKey,
        tenant_id: tenantId,
        user_id: userId,
        punch_id: punchId,
        created_at: new Date(),
      })
      .execute();

    const result = await trx
      .selectFrom('punches')
      .where('id', '=', punchId)
      .selectAll()
      .executeTakeFirstOrThrow();

    return result;
  });
}
```

### 3.3 LINE webhook: require idempotency and use service

```ts
// server/src/routes/line.ts
import express from 'express';
import { createOrUpdatePunch } from '../services/punchService';
import { v4 as uuidv4 } from 'uuid';

const router = express.Router();

router.post('/webhook/line', async (req, res) => {
  const events = req.body.events;
  if (!events) return res.sendStatus(400);

  // LINE may retry; use their webhookEventId as idempotency key or generate one per logical action
  for (const ev of events) {
    try {
      if (ev.type === 'message' && ev.message.type === 'text') {
        const text = ev.message.text.toLowerCase();
        const userId = ev.source.userId;
        const tenantId = 'default'; // derive from DB/channel binding in real system

        // Use LINE webhookEventId + userId to make idempotency key stable per event
        const idempotencyKey = `line:${ev.webhookEventId}:${userId}`;

        if (text === 'clock in' || text === 'clock out') {
          const isClockIn = text === 'clock in';
          const now = new Date();

          await createOrUpdatePunch({
            tenantId,
            userId,
            idempotencyKey,
            clockInAt: isClockIn ? now : undefined,
            clockOutAt: isClockIn ? undefined : now,
            location: null, // populate from ev.source or enrich later
          });
        }
      }
    } catch (err) {
      console.error('LINE webhook processing error:', err);
      // Do not ACK so LINE can retry (or selectively ACK on idempotent success)
    }
  }

  res.sendStatus(200);
});

export default router;
```

### 3.4 Frontend: optimistic UI + client deduplication

```tsx
// workio/src/components/ClockButton.tsx
import { useState, useCallback, useRef } from 'react';

const inFlight = new Map<string, Promise<any>>();

export function ClockButton() {
  const [status, setStatus] = useState<'in' | 'out'>('out');
  const [loading, setLoading] = useState(false);
  const tenantId = 'default'; // derive from context
  const userId = 'user-123'; // derive from auth

  const doClock = useCallback(async (type: 'in' | 'out') => {
    const key = `${tenantId}:${userId}:clock`;
    if (inFlight.has(key)) {
      return inFlight.get(key);
    }

    const promise = fetch('/api/punch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenantId,
        userId,
        idempot

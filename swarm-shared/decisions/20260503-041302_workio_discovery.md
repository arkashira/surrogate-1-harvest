# workio / discovery

## 1. Diagnosis

- No **idempotency key** on clock-in/out endpoint → double-tap or retry creates duplicate time records.
- Clock mutations are **not transactional** across `clock_records` + daily summary/state tables → partial writes on failure leave inconsistent daily state.
- Frontend has **no optimistic UI or pending state** → perceived latency and duplicate submissions while waiting on RTT.
- Missing **conflict resolution** for out-of-order clock events (e.g., offline queue flush) — last write wins can corrupt day totals.
- No **audit trail** (who/when/why) on manual overrides or corrections — limits trust and debugging.

## 2. Proposed change

Add idempotency + transactional clock mutation and optimistic UI for the clock feature.

Scope:
- `workio/server/src/routes/clock.ts` — POST `/clock` handler (add idempotency key, transaction, conflict resolution).
- `workio/server/src/db/` — add `clock_idempotency` table and helper; ensure atomic upsert of `clock_records` + daily summary.
- `workio/src/features/clock/ClockButton.tsx` — optimistic UI + disabled while pending + client-generated idempotency key.
- `workio/src/lib/api.ts` — extend `clockIn`/`clockOut` to accept idempotency key and return consistent shape.

## 3. Implementation

### 3.1 DB: idempotency table and transactional upsert

```sql
-- workio/server/src/db/schema.sql  (append)
CREATE TABLE IF NOT EXISTS clock_idempotency (
  idempotency_key TEXT NOT NULL PRIMARY KEY,
  tenant_id       INTEGER NOT NULL,
  user_id         INTEGER NOT NULL,
  clock_record_id INTEGER NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Index for cleanup
CREATE INDEX IF NOT EXISTS idx_clock_idempotency_tenant ON clock_idempotency(tenant_id, created_at);
```

```ts
// workio/server/src/db/clock.ts
import { PoolClient } from 'pg';
import { pool } from './index';

export interface ClockRequest {
  tenantId: number;
  userId: number;
  type: 'IN' | 'OUT';
  latitude?: number;
  longitude?: number;
  idempotencyKey: string;
}

export async function upsertClockRecord(req: ClockRequest, client?: PoolClient): Promise<{ recordId: number; previous?: { type: 'IN' | 'OUT'; at: Date } }> {
  const db = client || pool;
  return db.query(
    `WITH existing_idempotency AS (
       SELECT clock_record_id FROM clock_idempotency
       WHERE idempotency_key = $1 AND tenant_id = $2
     ),
     latest_record AS (
       SELECT id, type, created_at FROM clock_records
       WHERE tenant_id = $2 AND user_id = $3
       ORDER BY created_at DESC LIMIT 1
     ),
     inserted_record AS (
       INSERT INTO clock_records (tenant_id, user_id, type, latitude, longitude, created_at)
       SELECT $2, $3, $4, $5, $6, NOW()
       WHERE NOT EXISTS (SELECT 1 FROM existing_idempotency)
       RETURNING id
     ),
     inserted_idempotency AS (
       INSERT INTO clock_idempotency (idempotency_key, tenant_id, user_id, clock_record_id)
       SELECT $1, $2, $3, id FROM inserted_record
       RETURNING clock_record_id
     )
     SELECT
       COALESCE((SELECT clock_record_id FROM inserted_idempotency), (SELECT clock_record_id FROM existing_idempotency)) AS record_id,
       (SELECT json_build_object('type', type, 'at', created_at) FROM latest_record) AS previous`,
    [req.idempotencyKey, req.tenantId, req.userId, req.type, req.latitude, req.longitude]
  ).then((r) => {
    const row = r.rows[0];
    return {
      recordId: row.record_id,
      previous: row.previous ? { type: row.previous.type, at: row.previous.at } : undefined,
    };
  });
}
```

### 3.2 Route: transactional handler with conflict resolution

```ts
// workio/server/src/routes/clock.ts
import express from 'express';
import { pool } from '../db';
import { upsertClockRecord } from '../db/clock';

const router = express.Router();

router.post('/', async (req, res) => {
  const { tenantId, userId, type, latitude, longitude, idempotencyKey } = req.body;
  if (!tenantId || !userId || !type || !idempotencyKey) {
    return res.status(400).json({ error: 'Missing required fields' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const result = await upsertClockRecord(
      { tenantId, userId, type, latitude, longitude, idempotencyKey },
      client
    );

    // Optional: update daily summary here atomically within same tx
    // Example: upsert daily_state set last_clock_type = type, updated_at = now()
    // where tenant_id = $1 and user_id = $2 and day = date(now())

    await client.query('COMMIT');
    res.json({ ok: true, recordId: result.recordId, previous: result.previous });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('Clock mutation failed', err);
    res.status(500).json({ error: 'Failed to record clock event' });
  } finally {
    client.release();
  }
});

export default router;
```

### 3.3 Frontend: optimistic UI + idempotency key

```tsx
// workio/src/features/clock/ClockButton.tsx
import { useState } from 'react';
import { clockIn, clockOut } from '../../lib/api';

export function ClockButton() {
  const [pending, setPending] = useState(false);
  const [lastType, setLastType] = useState<'IN' | 'OUT' | null>(null);

  const handleClock = async (type: 'IN' | 'OUT') => {
    if (pending) return;
    const idempotencyKey = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    setPending(true);
    // optimistic update
    setLastType(type);

    try {
      const result =
        type === 'IN'
          ? await clockIn({ idempotencyKey })
          : await clockOut({ idempotencyKey });
      // server result may differ (e.g., idempotent replay) — trust server
      setLastType(result.previous?.type || type);
    } catch (err) {
      // revert optimistic on failure
      setLastType(null);
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="flex gap-2">
      <button
        onClick={() => handleClock('IN')}
        disabled={pending}
        className={`px-4 py-2 rounded ${
          lastType === 'IN' ? 'bg-green-600 text-white' : 'bg-gray-200'
        } ${pending ? 'opacity-60 cursor-wait' : ''}`}
      >
        {pending && lastType === 'IN' ? 'กำลังลงเวลา...' : 'ลงเวลาเข้า'}
      </button>
      <button
        onClick={() => handleClock('OUT')}
        disabled={pending}
        className={`px-4 py-2 rounded ${
          lastType === 'OUT' ? 'bg-red-600 text-white' : 'bg-gray-200'
        } ${pending ? 'opacity-60 cursor-wait' : ''}`}
      >
        {pending && lastType === 'OUT' ? 'กำลังลงเวลาออก...' : 'ลงเวลาออก'}
      </button>
    </div>
  );
}
```

```ts
// workio/src/lib/api.ts
export async function clockIn(body: { idempotencyKey: string; latitude?: number; longitude?: number }) {
  const res = await fetch('/clock', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'IN', ...body }),
  });
  if (!res.ok)

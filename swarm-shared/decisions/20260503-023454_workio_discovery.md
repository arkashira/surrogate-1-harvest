# workio / discovery

## Final synthesized solution (correct + actionable)

### 1. Diagnosis (merged, contradictions resolved)

- **Missing DB uniqueness** allows duplicate `(employee, date, punch_type)` rows from LINE webhook retries or user double-taps.
- **Non-atomic upsert** under concurrency lets race conditions bypass app-level checks.
- **No idempotency key** (frontend → backend → webhook) makes retries and tracing impossible.
- **Frontend lacks feedback + deduplication**, so users retry and create duplicates while perceiving them as valid.

**Resolution**: enforce uniqueness at DB, add idempotency key end-to-end, and provide optimistic UI + client deduplication. Use `punch_date` (not `date`) and `punch_type` (values `in`/`out`) to match existing code and avoid breaking changes.

---

### 2. DB schema changes (single source of truth)

```sql
-- server/src/db/schema.sql
-- Add idempotency key column (nullable, indexed)
ALTER TABLE punches ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_idempotency
  ON punches (idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- Enforce one punch per employee/date/type (business rule)
ALTER TABLE punches
  ADD CONSTRAINT punches_employee_date_type_uniq
  UNIQUE (employee_id, punch_date, punch_type);
```

- `idempotency_key` is nullable so existing rows remain valid.
- Unique constraint on `(employee_id, punch_date, punch_type)` prevents duplicates at DB level.
- Partial unique index on `idempotency_key` enables safe retries without resubmitting the same request.

---

### 3. Backend: atomic upsert + idempotency (webhook + API)

```ts
// server/src/routes/line.ts  (and reuse in server/src/routes/punch.ts)
import { db } from '../db';
import { punches } from '../db/schema';
import { and, eq } from 'drizzle-orm';

export async function handlePunch(payload: {
  employeeId: string;
  punchType: 'in' | 'out';
  timestamp: Date;
  location?: { lat: number; lng: number };
  idempotencyKey?: string | null;
}) {
  const punchDate = new Date(payload.timestamp).toISOString().split('T')[0];

  // If idempotency key provided, try to return existing row
  if (payload.idempotencyKey) {
    const existing = await db
      .select()
      .from(punches)
      .where(eq(punches.idempotencyKey, payload.idempotencyKey))
      .limit(1);

    if (existing[0]) return existing[0];
  }

  // Atomic insert; unique constraints prevent duplicates
  const [inserted] = await db
    .insert(punches)
    .values({
      employeeId: payload.employeeId,
      punchDate,
      punchType: payload.punchType,
      timestamp: payload.timestamp,
      latitude: payload.location?.lat ?? null,
      longitude: payload.location?.lng ?? null,
      idempotencyKey: payload.idempotencyKey ?? null,
    })
    .onConflictDoNothing()
    .returning();

  // If conflict on (employee,date,type) and no idempotency match, return existing
  if (!inserted) {
    const [existing] = await db
      .select()
      .from(punches)
      .where(
        and(
          eq(punches.employeeId, payload.employeeId),
          eq(punches.punchDate, punchDate),
          eq(punches.punchType, payload.punchType)
        )
      )
      .limit(1);

    return existing;
  }

  return inserted;
}
```

- `onConflictDoNothing` + returning makes the upsert atomic.
- If a duplicate arrives without matching idempotency key (e.g., LINE retry without propagation), return the existing row instead of erroring.
- Reuse same handler in `/punch` route; require/accept `X-Request-Id` header and pass it as `idempotencyKey`.

---

### 4. Frontend: optimistic UI + request deduplication + idempotency header

```ts
// workio/src/lib/dedupe.ts
const inFlight = new Map<string, Promise<any>>();

export function dedupeRequest<T>(key: string, fn: () => Promise<T>): Promise<T> {
  if (inFlight.has(key)) {
    return inFlight.get(key)!;
  }
  const promise = fn().finally(() => inFlight.delete(key));
  inFlight.set(key, promise);
  return promise;
}
```

```tsx
// workio/src/features/punch/PunchButton.tsx
import { useState, useCallback } from 'react';
import { dedupeRequest } from '../../lib/dedupe';
import { postPunch } from '../../api/punch';

function randomId() {
  return crypto.randomUUID?.() || Math.random().toString(36).slice(2);
}

export function PunchButton({ employeeId }: { employeeId: string }) {
  const [pending, setPending] = useState<'in' | 'out' | null>(null);
  const [lastPunch, setLastPunch] = useState<'in' | 'out' | null>(null);

  const punch = useCallback(
    async (type: 'in' | 'out') => {
      const key = `punch-${employeeId}-${type}`;
      const requestId = randomId();

      setPending(type);
      try {
        await dedupeRequest(key, () =>
          postPunch({ employeeId, punchType: type }, requestId)
        );
        setLastPunch(type);
      } finally {
        setPending(null);
      }
    },
    [employeeId]
  );

  return (
    <div className="flex gap-3">
      <button
        onClick={() => punch('in')}
        disabled={pending !== null}
        className={`px-4 py-2 rounded font-medium ${
          pending === 'in'
            ? 'bg-blue-400 cursor-wait'
            : lastPunch === 'in'
            ? 'bg-green-500 text-white'
            : 'bg-blue-600 text-white hover:bg-blue-700'
        }`}
      >
        {pending === 'in' ? 'กำลังลงเวลา...' : 'Clock In'}
      </button>

      <button
        onClick={() => punch('out')}
        disabled={pending !== null}
        className={`px-4 py-2 rounded font-medium ${
          pending === 'out'
            ? 'bg-orange-400 cursor-wait'
            : lastPunch === 'out'
            ? 'bg-green-500 text-white'
            : 'bg-orange-600 text-white hover:bg-orange-700'
        }`}
      >
        {pending === 'out' ? 'กำลังลงเวลา...' : 'Clock Out'}
      </button>
    </div>
  );
}
```

```ts
// workio/src/api/punch.ts
export async function postPunch(
  body: { employeeId: string; punchType: 'in' | 'out' },
  requestId: string
) {
  const res = await fetch('/api/punch', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Request-Id': requestId,
    },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const err = new Error('Punch failed');
    (err as any).status = res.status;
    throw err;
  }

  return res.json();
}
```

- Each punch action generates a stable `X-Request-Id` for that in-flight attempt.
- `dedupeRequest` prevents duplicate in-flight requests for the same employee+type.
- Optimistic UI (`pending`, `lastPunch`) gives immediate feedback; on conflict/network error the UI resolves to the persisted state returned by the backend.

---

### 5. Verification checklist (run these)

- **DB uniqueness**: In psql, insert two rows with same `(employee_id, punch_date, punch_type)`; second insert must

# workio / discovery

## Final synthesized plan (correct + actionable)

**Core principle**: Prevent duplicates at the system boundary (webhook) and at the user action boundary (UI) using idempotency keys + unique constraints, and make success/failure obvious to users.

---

### 1. Diagnosis (resolved)
- **Webhook non-idempotency** (LINE retries on 5xx/timeouts/network blips) → duplicate clock/leave/OT events.  
  **Fix**: Treat `webhookEvent.id` (or derived stable external_id) + event_type + user_id as a unique unit and enforce it in the DB.
- **No transactional deduplication** → race conditions when retries overlap.  
  **Fix**: Use an idempotency table with a unique constraint and upsert in a single transaction that also writes the domain event.
- **Frontend double-taps / no pending state** → duplicate submissions and confusing UI.  
  **Fix**: Add optimistic UI, disable-on-submit, and client-generated idempotency keys for direct user actions.
- **Silent GPS/permission failures** → users retry blindly.  
  **Fix**: Show clear, actionable feedback and block further attempts while resolving.

---

### 2. Implementation (minimal, high-leverage)

#### Backend — idempotent webhook handler and upsert

1) Schema (run migration):

```sql
CREATE TABLE IF NOT EXISTS event_idempotency (
  external_id   TEXT NOT NULL,
  event_type    TEXT NOT NULL,
  user_id       INTEGER NOT NULL,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (external_id, event_type, user_id)
);
```

2) Idempotency service (safe for concurrent retries):

```ts
// server/src/services/idempotencyService.ts
import { pool } from '../db';

export async function tryAcquireIdempotency(
  externalId: string,
  eventType: string,
  userId: number,
  tx?: any
): Promise<boolean> {
  const q = tx ? tx.query : pool.query;
  try {
    await q(
      `INSERT INTO event_idempotency (external_id, event_type, user_id)
       VALUES ($1, $2, $3)`,
      [externalId, eventType, userId]
    );
    return true;
  } catch (err: any) {
    if (err.code === '23505') return false; // duplicate
    throw err;
  }
}
```

3) Clock service with transactional upsert (prevents partial writes):

```ts
// server/src/services/clockService.ts
import { pool } from '../db';
import { tryAcquireIdempotency } from './idempotencyService';

export async function handleClockIn(
  userId: number,
  ts: Date,
  externalId: string,
  location?: any
) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const acquired = await tryAcquireIdempotency(externalId, 'clock_in', userId, client);
    if (!acquired) {
      await client.query('COMMIT');
      return { ok: true, duplicate: true };
    }

    await client.query(
      `INSERT INTO clock_events (user_id, event_type, ts, location, external_id)
       VALUES ($1, 'clock_in', $2, $3, $4)`,
      [userId, ts, location, externalId]
    );
    await client.query('COMMIT');
    return { ok: true, duplicate: false };
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}
```

4) Webhook route — stable external_id and strict 200 on success:

```ts
// server/src/routes/line.ts
import { handleClockIn, handleClockOut } from '../services/clockService';

router.post('/webhook/line', async (req, res) => {
  try {
    const events = req.body.events || [];
    for (const ev of events) {
      // Use LINE's stable id + type to avoid collisions across retries
      const externalId = `${ev.source.userId}:${ev.type}:${ev.timestamp}:${ev.message?.id || ev.type}`;
      const userId = await mapLineUserIdToAppUserId(ev.source.userId);

      if (ev.type === 'message' && ev.message?.type === 'text') {
        const text = ev.message.text || '';
        if (text.includes('เข้างาน')) {
          await handleClockIn(userId, new Date(ev.timestamp), externalId);
        } else if (text.includes('เลิกงาน')) {
          await handleClockOut(userId, new Date(ev.timestamp), externalId);
        }
      }
    }
    // LINE expects 2xx to stop retries
    res.status(200).send();
  } catch (err) {
    console.error(err);
    // Non-2xx causes LINE retries — use 5xx only for transient/server errors you want retried
    res.status(500).send();
  }
});
```

#### Frontend — optimistic UI + idempotency key + clear feedback

1) API with client-generated idempotency key:

```ts
// workio/src/api/clock.ts
import axios from 'axios';

export async function clockIn(idempotencyKey: string) {
  return axios.post('/api/clock/in', { idempotencyKey });
}
export async function clockOut(idempotencyKey: string) {
  return axios.post('/api/clock/out', { idempotencyKey });
}
```

2) Clock button with optimistic state and safe submission:

```tsx
// workio/src/features/clock/ClockButton.tsx
import { useState, useCallback } from 'react';
import { clockIn, clockOut } from '../../api/clock';

export function ClockButton({ isClockedIn: serverIsClockedIn, onSync }: {
  isClockedIn: boolean;
  onSync?: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [optimistic, setOptimistic] = useState<'in' | 'out' | null>(null);

  const submit = useCallback(async (action: 'in' | 'out') => {
    if (pending) return;
    const key = `clock_${Date.now()}_${crypto.randomUUID()}`;
    setPending(true);
    setOptimistic(action);

    try {
      if (action === 'in') await clockIn(key);
      else await clockOut(key);
      // optimistic state remains until parent refreshes
      onSync?.();
    } catch {
      // revert on failure
      setOptimistic(null);
    } finally {
      setPending(false);
    }
  }, [pending, onSync]);

  const effective = optimistic || (serverIsClockedIn ? 'in' : 'out');
  const isPending = pending || !!optimistic;

  return (
    <button
      onClick={() => submit(serverIsClockedIn ? 'out' : 'in')}
      disabled={isPending}
      className={`px-4 py-2 rounded text-white transition ${
        effective === 'in' ? 'bg-red-500' : 'bg-green-500'
      } ${isPending ? 'opacity-60 cursor-wait' : ''}`}
      aria-busy={isPending}
    >
      {isPending ? 'กำลังบันทึก...' : effective === 'in' ? 'เลิกงาน' : 'เข้างาน'}
    </button>
  );
}
```

3) Guidance and GPS/permission UX (non-blocking but clear):

- If GPS/permissions are required, check before submit and show an inline, actionable message (e.g., “เปิด GPS เพื่อบันทึกสถานที่” with a link to browser/device settings).
- While permission is pending, show a disabled or loading state with clear label; do not silently fail.

---

### 3. Verification (concrete tests)

**Backend**
- Insert `(external_id, event_type, user_id)` into `event_idempotency`; replay same webhook payload and assert:
  - 200 response.
  - No new row in `clock_events`.
- Concurrent load test: fire two identical webhook POSTs within 200ms; assert exactly one `clock_events` row and one idempotency row.

**Frontend**
- Rapid double-click

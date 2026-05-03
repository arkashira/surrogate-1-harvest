# workio / discovery

## 1) Diagnosis

- No server-side idempotency key enforcement → repeated LINE webhook deliveries or frontend retries create duplicate punches.
- Missing DB uniqueness constraint for “one open punch per user” → allows multiple concurrent clock-ins or overlapping records.
- Punch timestamp boundaries rely on server local time without explicit tenant timezone → day-rollover ambiguity for late/early punches.
- No optimistic-UI lock on clock-in/out button → users can spam and queue multiple requests.
- Missing dedupe middleware for LINE webhook events (LINE may retry on 5xx/timeouts).

## 2) Proposed change

File/line scope:
- `server/src/db/schema.sql` — add partial unique index for open punches.
- `server/src/routes/punch.ts` — add idempotency middleware and timezone-aware day boundary.
- `server/src/middleware/idempotency.ts` — new file for idempotency key validation.
- `frontend/src/components/PunchButton.tsx` — add optimistic lock + disabled state.

## 3) Implementation

### server/src/db/schema.sql
```sql
-- Ensure one open punch per user (status='in' without matching 'out')
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_punch_per_user
ON punches (user_id)
WHERE status = 'in';
```

### server/src/middleware/idempotency.ts
```ts
import { Request, Response, NextFunction } from 'express';
import crypto from 'crypto';

const seen = new Map<string, { ts: number; resBody: string }>();
const TTL_MS = 5 * 60 * 1000;

export function idempotencyKey(key?: string): string | null {
  if (!key || typeof key !== 'string' || key.length > 255) return null;
  return crypto.createHash('sha256').update(key).digest('hex');
}

export function idempotencyMiddleware(
  req: Request,
  res: Response,
  next: NextFunction
) {
  const key = req.headers['idempotency-key'] as string;
  const hash = idempotencyKey(key);
  if (!hash) return next();

  const now = Date.now();
  const cached = seen.get(hash);
  if (cached && now - cached.ts < TTL_MS) {
    return res
      .status(200)
      .set('Idempotent-Replay', 'true')
      .json(JSON.parse(cached.resBody));
  }

  const origSend = res.json;
  // @ts-ignore
  res.json = function (body) {
    seen.set(hash, { ts: now, resBody: JSON.stringify(body) });
    return origSend.call(this, body);
  };
  next();
}
```

### server/src/routes/punch.ts (excerpt)
```ts
import { Router } from 'express';
import { idempotencyMiddleware } from '../middleware/idempotency';
import { getTenantByLineChannel } from '../db/tenant';
import { pool } from '../db';

const router = Router();

function tzDate(tenantTz: string): { date: string; dayStart: string; dayEnd: string } {
  const now = new Date().toLocaleString('sv-SE', { timeZone: tenantTz });
  const date = now.split('T')[0];
  return {
    date,
    dayStart: `${date}T00:00:00`,
    dayEnd: `${date}T23:59:59.999`,
  };
}

router.post('/punch', idempotencyMiddleware, async (req, res) => {
  const { userId, type, lineChannelId } = req.body;
  try {
    const tenant = await getTenantByLineChannel(lineChannelId);
    const { date } = tzDate(tenant.timezone || 'Asia/Bangkok');

    if (type === 'in') {
      // Let DB unique index block duplicates
      const result = await pool.query(
        `INSERT INTO punches (user_id, tenant_id, type, ts, date, status)
         VALUES ($1, $2, $3, NOW(), $4, 'in')
         ON CONFLICT DO NOTHING
         RETURNING *`,
        [userId, tenant.id, type, date]
      );
      if (result.rowCount === 0) {
        return res.status(409).json({ error: 'Already clocked in' });
      }
      return res.json(result.rows[0]);
    }

    if (type === 'out') {
      const result = await pool.query(
        `UPDATE punches
         SET type = 'out', status = 'out', updated_at = NOW()
         WHERE id = (
           SELECT id FROM punches
           WHERE user_id = $1 AND tenant_id = $2 AND status = 'in'
           ORDER BY ts DESC LIMIT 1
         )
         RETURNING *`,
        [userId, tenant.id]
      );
      if (result.rowCount === 0) {
        return res.status(404).json({ error: 'No open punch to clock out' });
      }
      return res.json(result.rows[0]);
    }

    return res.status(400).json({ error: 'Invalid punch type' });
  } catch (err: any) {
    // Handle unique violation explicitly in case ON CONFLICT DO NOTHING is bypassed
    if (err.code === '23505') {
      return res.status(409).json({ error: 'Duplicate punch' });
    }
    console.error(err);
    return res.status(500).json({ error: 'Server error' });
  }
});

export default router;
```

### frontend/src/components/PunchButton.tsx (excerpt)
```tsx
import { useState } from 'react';
import { postPunch } from '../api/punch';

export function PunchButton({ userId, lineChannelId }: { userId: string; lineChannelId: string }) {
  const [pending, setPending] = useState<'in' | 'out' | null>(null);

  const handle = async (type: 'in' | 'out') => {
    if (pending) return;
    setPending(type);
    try {
      await postPunch({ userId, type, lineChannelId });
    } catch (err) {
      // handled by toast/UI upstream
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="flex gap-2">
      <button
        onClick={() => handle('in')}
        disabled={pending != null}
        className="btn btn-primary"
      >
        {pending === 'in' ? 'Clocking in...' : 'Clock In'}
      </button>
      <button
        onClick={() => handle('out')}
        disabled={pending != null}
        className="btn btn-secondary"
      >
        {pending === 'out' ? 'Clocking out...' : 'Clock Out'}
      </button>
    </div>
  );
}
```

## 4) Verification

1. Apply schema: `psql workio < server/src/db/schema.sql`
2. Start backend and frontend.
3. Clock in as a user → success (200).
4. Immediately retry same request with same `idempotency-key` header → 200 with `Idempotent-Replay: true` and same punch record (no duplicate).
5. Attempt second clock-in without clock-out → 409 “Already clocked in” and DB rejects duplicate open punch (index violation).
6. Clock out → 200 and status updated.
7. Verify timestamps respect tenant timezone by inserting test rows with mocked tenant TZ and confirming `date` column matches local calendar day.

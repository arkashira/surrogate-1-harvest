# workio / discovery

Candidate 1 gives the cleanest, most complete end-to-end plan (DB constraint + idempotency middleware + frontend lock + verification).  
Candidate 2 adds two important missing pieces:

- A **partial unique index** to enforce “only one open punch per user” at the DB level (stronger than the date+type unique index for preventing double-in).  
- A **proper idempotency middleware** that replays previous responses for repeated keys (safer than in-memory seen-set that only dedupes within TTL window).

Candidate 2’s index naming and partial-index approach is also more future-proof for multi-day punches and for queries like “who is currently clocked in”.

Below is the **single, merged, corrected, and actionable** plan that keeps Candidate 1’s clarity and verification steps, but replaces weaker parts with Candidate 2’s stronger DB constraint and idempotency middleware.

---

## 1. Diagnosis (merged)

- No idempotency key on `/webhook/line` → LINE retries and double-taps create duplicate punches.  
- No DB-level uniqueness → race conditions and duplicate rows persist.  
- Frontend has no optimistic state/debounce → UI queues concurrent requests on slow networks.  
- No validation of last punch state → employees can clock-in twice without warning.  
- Missing audit metadata (source, ip, user_agent) → hard to debug duplicates and reconcile reports.

---

## 2. Proposed change (merged)

- **Backend**  
  - Add idempotency middleware (key: `X-Idempotency-Key` or `line_event_id`) with replay of previous response.  
  - Add partial unique index to enforce at most one open punch per user.  
  - Add uniqueness constraint on `(user_id, punch_date, punch_type)` for closed punches to prevent duplicate in/out on same day.  
  - Add `source`, `ip`, `user_agent` columns for auditability.  
  - Upsert logic for idempotent webhook handling.

- **Frontend**  
  - Add optimistic state, 2s client debounce, and last-punch preflight check.  
  - Disable button while pending and prevent double submissions.

---

## 3. Implementation

### 3.1 Database schema (PostgreSQL)

```sql
-- server/src/db/schema.sql

-- Audit columns (add if missing)
ALTER TABLE punches ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'dashboard';
ALTER TABLE punches ADD COLUMN IF NOT EXISTS ip INET;
ALTER TABLE punches ADD COLUMN IF NOT EXISTS user_agent TEXT;

-- Stronger constraints:
-- 1) At most one open punch per user (partial index)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_user_open
  ON punches (user_id)
  WHERE clock_out_at IS NULL;

-- 2) Prevent duplicate closed punches per user per date per type
-- Assumes table has punch_type and timestamp; adapt names to your schema
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_user_date_type_closed
  ON punches (user_id, DATE(timestamp), punch_type)
  WHERE clock_out_at IS NOT NULL;
```

Notes:
- If your table uses `timestamp` for clock-in and `clock_out_at` for clock-out, the partial index above works directly.  
- If you track state differently, adjust the `WHERE` clause to match “open punch” semantics.

---

### 3.2 Idempotency middleware (Node/Express)

```ts
// server/src/middleware/idempotency.ts
import { Request, Response, NextFunction } from 'express';

const IDEMPOTENCY_TTL_MS = 24 * 60 * 60 * 1000; // 24h
const seen = new Map<string, { status: number; body: any; ts: number }>();

export function idempotency(opts: { ttl?: number } = {}) {
  const ttl = opts.ttl ?? IDEMPOTENCY_TTL_MS;
  return (req: Request, res: Response, next: NextFunction) => {
    const key = req.header('X-Idempotency-Key') || (req.body as any)?.line_event_id;
    if (!key) return next();

    const now = Date.now();
    const cached = seen.get(key);
    if (cached) {
      if (now - cached.ts < ttl) {
        return res.status(cached.status).json(cached.body);
      }
      seen.delete(key);
    }

    const origJson = res.json.bind(res);
    res.json = function (body) {
      seen.set(key, { status: res.statusCode, body, ts: Date.now() });
      return origJson(body);
    };

    next();
  };
}
```

Production note: replace in-memory `Map` with Redis or another shared store for multi-instance deployments.

---

### 3.3 Webhook handler (upsert + idempotency)

```ts
// server/src/routes/webhook.ts
import { Router } from 'express';
import { db } from '../db';
import { idempotency } from '../middleware/idempotency';

const router = Router();

router.post('/line', idempotency(), async (req, res) => {
  const { userId, type, line_event_id } = req.body; // type: 'clock_in' | 'clock_out'
  const now = new Date();
  const ip = req.ip;
  const userAgent = req.get('User-Agent') || '';

  try {
    // Try insert; unique indexes prevent duplicates at DB level
    const result = await db.query(
      `INSERT INTO punches (user_id, timestamp, punch_type, source, ip, user_agent)
       VALUES ($1, $2, $3, 'line', $4, $5)
       ON CONFLICT (user_id, DATE(timestamp), punch_type)
       WHERE clock_out_at IS NOT NULL
       DO UPDATE SET timestamp = EXCLUDED.timestamp
       RETURNING *`,
      [userId, now, type, ip, userAgent]
    );

    return res.json({ ok: true, punch: result.rows[0] });
  } catch (err: any) {
    // If conflict on open punch (idx_punches_user_open), return existing open punch
    if (err.code === '23505') {
      const existing = await db.query(
        `SELECT * FROM punches WHERE user_id = $1 AND clock_out_at IS NULL`,
        [userId]
      );
      return res.status(200).json({ ok: true, punch: existing.rows[0], conflict: true });
    }
    console.error('Webhook upsert failed', err);
    return res.status(500).json({ error: 'db_error' });
  }
});

export default router;
```

---

### 3.4 Frontend PunchButton (React)

```tsx
// src/components/PunchButton.tsx
import { useState, useCallback } from 'react';
import { useLastPunch } from '../hooks/useLastPunch';

export function PunchButton() {
  const [pending, setPending] = useState(false);
  const { lastPunch, mutate } = useLastPunch();

  const send = useCallback(async (type: 'clock_in' | 'clock_out') => {
    if (pending) return;
    setPending(true);
    try {
      await fetch('/api/webhook/line', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          userId: window.USER_ID,
          type,
          line_event_id: `${window.USER_ID}-${Date.now()}`, // client-side idempotency hint
        }),
      });
      await mutate();
    } finally {
      setPending(false);
    }
  }, [pending, mutate]);

  const disabled = pending || (lastPunch?.punch_type === 'clock_in' && !lastPunch?.clocked_out);

  return (
    <button
      onClick={() => send(lastPunch?.punch_type === 'clock_in' ? 'clock_out' : 'clock_in')}
      disabled={disabled}
      className={`px-6 py-3 rounded font-semibold ${
        disabled ? 'bg-gray-300' : 'bg-blue-600 text-white'
      }`}
    >
      {pending
        ? 'กำลังบันทึก...'
        : lastPunch?.punch_type === 'clock_in'
        ? 'Clock Out'
        : 'Clock In'}
    </

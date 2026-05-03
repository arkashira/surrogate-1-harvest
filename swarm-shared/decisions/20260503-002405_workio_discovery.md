# workio / discovery

## 1) Diagnosis

- No idempotency key on punch endpoints → retries or UI double-submit create duplicate rows.
- Missing DB uniqueness to enforce “one open punch per user” (no partial unique index on `(user_id, clock_out)` where `clock_out IS NULL`).
- Frontend has no submission lock or debounce; slow LINE/webhook roundtrips invite double-taps.
- Punch timestamp boundaries use local JS `Date()` without explicit timezone/day normalization → risk of off-by-one-day punches when crossing midnight.
- No lightweight verification route to list a user’s latest punch state (open/closed) for client-side pre-checks and debugging.

## 2) Proposed change

- **Backend**:  
  - `server/src/db/schema.sql` — add partial unique index and `idempotency_key` column (+ index) on punches.  
  - `server/src/routes/punch.ts` — idempotency middleware (UPSERT on `idempotency_key`, reject stale replays) and timezone-safe day-boundary handling.  
  - `server/src/routes/punch.ts` — add `GET /punch/latest` for client pre-check.
- **Frontend**:  
  - `src/components/PunchButton.tsx` — add submission lock + debounce + use `GET /punch/latest` to gate “Clock in” vs “Clock out” label.

## 3) Implementation

### DB schema (`server/src/db/schema.sql`)

```sql
-- Add idempotency support and prevent duplicate open punches
ALTER TABLE punches ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_idempotency_key
  ON punches (idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- Only one open punch per user (clock_out is NULL)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_open_per_user
  ON punches (user_id)
  WHERE clock_out IS NULL;
```

### Backend punch route (`server/src/routes/punch.ts`)

```ts
import express from 'express';
import { pool } from '../db';
import { verifyLineSignature } from '../middleware/line';

const router = express.Router();

// GET /punch/latest?userId=...
router.get('/latest', async (req, res) => {
  const { userId } = req.query;
  if (!userId || typeof userId !== 'string') {
    return res.status(400).json({ error: 'userId required' });
  }
  try {
    const { rows } = await pool.query(
      `SELECT id, user_id, clock_in, clock_out, created_at
       FROM punches
       WHERE user_id = $1
       ORDER BY clock_in DESC
       LIMIT 1`,
      [userId]
    );
    return res.json({ punch: rows[0] || null });
  } catch (err) {
    console.error(err);
    return res.status(500).json({ error: 'internal' });
  }
});

// POST /punch/clock
// Body: { userId, idempotencyKey, tz? }
router.post('/clock', verifyLineSignature, async (req, res) => {
  const { userId, idempotencyKey, tz = 'Asia/Bangkok' } = req.body;
  if (!userId || !idempotencyKey) {
    return res.status(400).json({ error: 'userId and idempotencyKey required' });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Idempotency check
    const { rows: existing } = await client.query(
      `SELECT punch_id FROM punches WHERE idempotency_key = $1`,
      [idempotencyKey]
    );
    if (existing.length > 0) {
      await client.query('ROLLBACK');
      return res.json({ ok: true, punchId: existing[0].punch_id, replayed: true });
    }

    // Determine latest punch state
    const { rows: latest } = await client.query(
      `SELECT id, clock_out FROM punches
       WHERE user_id = $1 AND clock_out IS NULL
       ORDER BY clock_in DESC
       LIMIT 1`,
      [userId]
    );

    const now = new Date();
    // Normalize day boundaries in provided tz for consistent date logic
    // Store as UTC; tz used only for business-day checks if needed
    if (latest.length > 0) {
      // Close open punch
      await client.query(
        `UPDATE punches
         SET clock_out = $1, updated_at = NOW()
         WHERE id = $2`,
        [now, latest[0].id]
      );
    } else {
      // Create new open punch
      await client.query(
        `INSERT INTO punches (user_id, clock_in, idempotency_key, created_at, updated_at)
         VALUES ($1, $2, $3, NOW(), NOW())`,
        [userId, now, idempotencyKey]
      );
    }

    await client.query('COMMIT');
    return res.json({ ok: true, replayed: false });
  } catch (err: any) {
    await client.query('ROLLBACK');
    // Unique violation on idempotency or partial index -> treat as conflict
    if (err.code === '23505') {
      return res.status(409).json({ error: 'duplicate' });
    }
    console.error(err);
    return res.status(500).json({ error: 'internal' });
  } finally {
    client.release();
  }
});

export default router;
```

### Frontend PunchButton (`src/components/PunchButton.tsx`)

```tsx
import React, { useState, useEffect, useCallback } from 'react';

export default function PunchButton({ userId }: { userId: string }) {
  const [latest, setLatest] = useState<{ clock_out: string | null } | null>(null);
  const [loading, setLoading] = useState(false);

  const fetchLatest = useCallback(async () => {
    try {
      const r = await fetch(`/punch/latest?userId=${encodeURIComponent(userId)}`);
      const j = await r.json();
      setLatest(j.punch);
    } catch {
      setLatest(null);
    }
  }, [userId]);

  useEffect(() => {
    fetchLatest();
  }, [fetchLatest]);

  const onPunch = async () => {
    if (loading) return;
    setLoading(true);
    try {
      const idempotencyKey = `${userId}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const r = await fetch('/punch/clock', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ userId, idempotencyKey, tz: 'Asia/Bangkok' }),
      });
      if (!r.ok) throw new Error('failed');
      await fetchLatest();
    } finally {
      setLoading(false);
    }
  };

  const isCheckedIn = latest && !latest.clock_out;

  return (
    <button
      onClick={onPunch}
      disabled={loading}
      className={`px-6 py-3 rounded font-medium transition ${
        isCheckedIn
          ? 'bg-red-600 hover:bg-red-700 text-white'
          : 'bg-green-600 hover:bg-green-700 text-white'
      } ${loading ? 'opacity-60 cursor-wait' : ''}`}
    >
      {loading ? 'กำลังดำเนินการ...' : isCheckedIn ? 'Clock Out' : 'Clock In'}
    </button>
  );
}
```

## 4) Verification

1. Apply the SQL migration against the dev database.
2. Start backend and frontend.
3. Open two browser tabs (or simulate two clients) for the same `userId`.
4. Rapidly click “Clock In” — only one open punch should appear in the DB (partial unique index prevents two open rows).
5. Use the same `idempotencyKey` in a retry request — API should return `{ replayed: true }` and not create a second punch.
6. After clocking in, UI should switch to “Clock Out”; after clocking out, UI should switch back to “Clock In”.
7. Check `GET /punch/latest?userId=...` returns the most recent punch and correct `clock_out` state.

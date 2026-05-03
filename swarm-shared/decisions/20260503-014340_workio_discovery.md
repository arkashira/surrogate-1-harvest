# workio / discovery

## Final synthesized solution

**Core diagnosis (merged)**  
- LINE uses at-least-once delivery with no idempotency key in the body we control; retries create duplicates.  
- Punch handling is non-atomic (`find` then `insert/update`) and races under concurrency.  
- No DB-level guard against same-user same-day same-event duplicates in short windows.  
- UI optimistically updates and can diverge from server truth.  
- Missing stable idempotency key and audit trail for retries.

**Chosen approach (correct + actionable)**  
1. Use LINE’s `webhookEventId` (or `X-Line-Delivery-Id` header if present) as the stable idempotency key stored in a small `webhook_events` table.  
2. Enforce business-valid transitions and duplicate prevention inside a single transaction with row-level locking on the user’s latest punch row.  
3. Add a partial unique index to block duplicates within a short time window as a defense-in-depth measure.  
4. Make webhook handler idempotent (return 200 on duplicates) and add minimal audit fields.  
5. Add lightweight client reconciliation so optimistic UI converges quickly.

---

### 1) Database guardrails (run once)

```sql
-- Idempotency table for LINE events (stable across retries)
CREATE TABLE IF NOT EXISTS webhook_events (
  idempotency_key TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Partial index: prevent duplicate punches within 60s for same user+event_type
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_user_event_dedupe_60s
ON punches (user_id, event_type, date)
WHERE created_at >= (NOW() - INTERVAL '60 seconds');

-- Fast lookup of latest punch per user
CREATE INDEX IF NOT EXISTS idx_punches_user_created_desc
ON punches (user_id, created_at DESC);
```

---

### 2) Server: atomic, idempotent webhook handler

File: `workio/server/src/routes/webhook/line.ts`

```ts
import { pool } from '../../db/index.js';

type LineWebhookBody = {
  events: Array<{
    type: string;
    source: { userId: string };
    message?: { text?: string };
    webhookEventId: string; // stable idempotency key from LINE
    timestamp: number;
  }>;
};

export async function lineWebhookHandler(req: any, res: any) {
  const body: LineWebhookBody = req.body;
  const client = await pool.connect();

  try {
    await client.query('BEGIN');

    for (const ev of body.events) {
      if (ev.type !== 'message' || !ev.message?.text) continue;
      const text = ev.message.text.trim().toLowerCase();
      if (!['clock in', 'clock out'].includes(text)) continue;

      const userId = ev.source.userId;
      const eventType = text === 'clock in' ? 'clock_in' : 'clock_out';
      const now = new Date(ev.timestamp);
      const today = now.toISOString().split('T')[0];
      const idempotencyKey = ev.webhookEventId;

      // Idempotency check
      const idemp = await client.query(
        `SELECT 1 FROM webhook_events WHERE idempotency_key = $1`,
        [idempotencyKey]
      );
      if (idemp.rows.length > 0) {
        // Already processed; skip but count as success
        continue;
      }

      // Lock latest punch row for this user to serialize transitions
      const latest = await client.query(
        `SELECT id, event_type, created_at
         FROM punches
         WHERE user_id = $1
         ORDER BY created_at DESC
         LIMIT 1
         FOR UPDATE`,
        [userId]
      );

      const last = latest.rows[0];

      // Validate transitions
      if (eventType === 'clock_in') {
        // Allow clock-in if no recent clock-in (use time window to avoid false blocks)
        if (last && last.event_type === 'clock_in') {
          const gap = now.getTime() - new Date(last.created_at).getTime();
          if (gap < 60_000) continue; // likely duplicate/replay
        }
      } else {
        // clock_out only valid if there's an open clock_in
        if (!last || last.event_type !== 'clock_in') continue;
      }

      // Record idempotency first (so retries are safe even if later steps fail)
      await client.query(
        `INSERT INTO webhook_events (idempotency_key, user_id, event_type)
         VALUES ($1, $2, $3)`,
        [idempotencyKey, userId, eventType]
      );

      // Insert punch (partial unique index provides defense-in-depth)
      await client.query(
        `INSERT INTO punches (user_id, date, event_type, created_at)
         VALUES ($1, $2, $3, $4)`,
        [userId, today, eventType, now]
      );
    }

    await client.query('COMMIT');
    res.status(200).json({ ok: true });
  } catch (err: any) {
    await client.query('ROLLBACK');
    // Unique violation from defense-in-depth index => treat as duplicate
    if (err.code === '23505') {
      res.status(200).json({ ok: true, duplicate: true });
    } else {
      console.error('LINE webhook error', err);
      res.status(500).json({ error: 'internal' });
    }
  } finally {
    client.release();
  }
}
```

---

### 3) Client: reconcile optimistic UI quickly

File: `workio/src/features/clock/ClockButton.tsx`

```tsx
import { useEffect } from 'react';
import { useClock } from '../../hooks/useClock';

export function ClockButton() {
  const { latestPunch, clockIn, clockOut, refetch } = useClock();

  // Periodic + action-triggered reconciliation
  useEffect(() => {
    refetch();
    const id = setInterval(refetch, 30_000);
    return () => clearInterval(id);
  }, [refetch]);

  const handleClockIn = async () => {
    await clockIn();
    // Fast reconcile to absorb races/duplicates
    setTimeout(() => refetch(), 500);
  };

  const handleClockOut = async () => {
    await clockOut();
    setTimeout(() => refetch(), 500);
  };

  return (
    <div>
      <button onClick={handleClockIn}>Clock In</button>
      <button
        onClick={handleClockOut}
        disabled={!latestPunch || latestPunch.event_type === 'clock_out'}
      >
        Clock Out
      </button>
      <p>Status: {latestPunch ? latestPunch.event_type : 'none'}</p>
    </div>
  );
}
```

---

### 4) Verification checklist

- **Idempotency**: resend the same `webhookEventId` payload multiple times; confirm only one punch row is created and handler returns 200.  
- **Concurrency**: run concurrent requests (e.g., `ab -n 20 -c 5`) for same-user clock-in; confirm no double clock-in rows and valid transitions.  
- **DB constraints**: verify partial unique index exists and rejects duplicates within 60s; verify `webhook_events` prevents replays beyond 60s.  
- **UI reconciliation**: after optimistic update, server state overwrites local state within ~500ms and periodic checks keep it consistent.  
- **Auditability**: `webhook_events` table provides traceability between LINE deliveries and processed events.

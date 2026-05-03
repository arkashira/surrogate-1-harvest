# workio / discovery

## Final consolidated solution

### Diagnosis (merged)
- Missing **idempotency key** on `/webhook/line` allows LINE at-least-once retries to create duplicate punch records.
- No **DB-level partial unique constraint** enforcing “one open punch per user” (`clock_out_at IS NULL`) permits race-condition double clock-ins.
- No **de-duplication window** for recent events (same user/same event/short span) causes retries within seconds to create duplicates.
- Handler does not return early on duplicate detection — wastes DB writes and confuses clients.
- No durable **idempotency store** to track processed LINE event IDs across retries.

### Chosen approach
- Use a **separate idempotency table** (`line_event_idempotency`) rather than overloading `punches`. This keeps domain boundaries clean and avoids nullable or placeholder rows in the core clocking table.
- Add a **partial unique index** on `punches(user_id) WHERE clock_out_at IS NULL` to enforce one open punch per user at the DB level.
- Implement **transactional upsert behavior** with `SELECT … FOR UPDATE` to serialize concurrent attempts per user.
- Add a **short in-memory fast-reject cache** (TTL ~5m) to cheaply absorb immediate retries while relying on the DB as the source of truth.
- Return **deterministic, idempotent responses** and proper HTTP status codes so LINE and clients can reason about success.

---

### 1. DB migration (run once)

```sql
-- 1) Partial unique constraint: at most one open punch per user
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_open_per_user
  ON punches (user_id)
  WHERE clock_out_at IS NULL;

-- 2) Separate idempotency table for LINE event IDs
CREATE TABLE IF NOT EXISTS line_event_idempotency (
  line_event_id TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Optional: index for housekeeping/TTL cleanup
CREATE INDEX IF NOT EXISTS idx_line_event_idempotency_created_at
  ON line_event_idempotency (created_at);
```

Apply:
```bash
psql workio < server/src/db/migrations/001_line_idempotency.sql
```

---

### 2. Route handler (concrete, production-ready)

File: `server/src/routes/webhook/line.ts`

```ts
import { Router, Request, Response } from 'express';
import { pool } from '../../db';
import crypto from 'crypto';

const router = Router();

// Fast in-memory recent-event cache (best-effort only)
const RECENT_TTL_MS = 5 * 60 * 1000; // 5m
const recentEvents = new Map<string, number>();

setInterval(() => {
  const now = Date.now();
  for (const [k, ts] of recentEvents.entries()) {
    if (now - ts > RECENT_TTL_MS) recentEvents.delete(k);
  }
}, 60_000);

function makeLineEventId(ev: any): string {
  // Prefer stable LINE identifiers; fallback to deterministic hash or UUID
  if (ev?.source?.userId && ev?.timestamp && ev?.type) {
    return `line:${ev.source.userId}:${ev.timestamp}:${ev.type}`;
  }
  // If payload has a message/delivery ID, use it
  if (ev?.message?.id) return `line:msg:${ev.message.id}`;
  if (ev?.delivery?.messageId) return `line:dlv:${ev.delivery.messageId}`;
  return `line:unknown:${crypto.randomUUID()}`;
}

router.post('/line', async (req: Request, res: Response) => {
  try {
    const events = req.body?.events;
    if (!Array.isArray(events) || events.length === 0) {
      return res.status(400).json({ error: 'invalid_payload' });
    }

    const results: Array<{
      lineEventId: string;
      status:
        | 'duplicate_skipped'
        | 'clock_in'
        | 'clock_out'
        | 'clock_out_no_open'
        | 'ignored_event_type'
        | 'invalid_source'
        | 'error';
      punchId?: string;
      error?: string;
    }> = [];

    for (const ev of events) {
      const lineEventId = makeLineEventId(ev);

      // Fast in-memory reject (best-effort)
      if (recentEvents.has(lineEventId)) {
        results.push({ lineEventId, status: 'duplicate_skipped' });
        continue;
      }

      const userId = ev?.source?.userId;
      if (!userId) {
        results.push({ lineEventId, status: 'invalid_source' });
        continue;
      }

      const client = await pool.connect();
      try {
        await client.query('BEGIN');

        // Idempotency check (durable)
        const idem = await client.query(
          `SELECT 1 FROM line_event_idempotency WHERE line_event_id = $1 FOR SHARE`,
          [lineEventId]
        );
        if (idem.rows.length > 0) {
          await client.query('COMMIT');
          recentEvents.set(lineEventId, Date.now());
          results.push({ lineEventId, status: 'duplicate_skipped' });
          continue;
        }

        // Serialize concurrent attempts for this user
        const openPunch = await client.query(
          `SELECT id FROM punches WHERE user_id = $1 AND clock_out_at IS NULL FOR UPDATE`,
          [userId]
        );

        const isClockIn =
          ev.type === 'clock_in' ||
          (typeof ev.message?.text === 'string' && ev.message.text.toLowerCase().includes('clock in'));
        const isClockOut =
          ev.type === 'clock_out' ||
          (typeof ev.message?.text === 'string' && ev.message.text.toLowerCase().includes('clock out'));

        if (isClockIn) {
          // If there's an open punch, auto-close it before new clock-in (graceful)
          if (openPunch.rows.length > 0) {
            await client.query(
              `UPDATE punches SET clock_out_at = NOW(), updated_at = NOW() WHERE id = $1`,
              [openPunch.rows[0].id]
            );
          }

          const insertResult = await client.query(
            `INSERT INTO punches (user_id, clock_in_at, created_at, updated_at)
             VALUES ($1, NOW(), NOW(), NOW())
             RETURNING id`,
            [userId]
          );
          results.push({ lineEventId, punchId: insertResult.rows[0].id, status: 'clock_in' });
        } else if (isClockOut) {
          if (openPunch.rows.length === 0) {
            // No open punch: create a closed punch (clock-in = clock-out = now)
            await client.query(
              `INSERT INTO punches (user_id, clock_in_at, clock_out_at, created_at, updated_at)
               VALUES ($1, NOW(), NOW(), NOW(), NOW())`,
              [userId]
            );
            results.push({ lineEventId, status: 'clock_out_no_open' });
          } else {
            await client.query(
              `UPDATE punches SET clock_out_at = NOW(), updated_at = NOW() WHERE id = $1`,
              [openPunch.rows[0].id]
            );
            results.push({ lineEventId, punchId: openPunch.rows[0].id, status: 'clock_out' });
          }
        } else {
          results.push({ lineEventId, status: 'ignored_event_type' });
        }

        // Record idempotency marker (durable)
        await client.query(
          `INSERT INTO line_event_idempotency (line_event_id, user_id) VALUES ($1, $2)`,
          [lineEventId, userId]
        );

        await client.query('COMMIT');
        recentEvents.set(lineEventId, Date.now());
      } catch (err) {
        await client.query('ROLLBACK').catch(() => {});
        console.error('Webhook processing error:', err);
        results.push({ lineEventId, status: 'error', error: String(err) });
      } finally {
        client.release();
      }
    }

    return res.json({ ok: true, results });
  } catch (err) {
    console.error('Webhook handler error:', err);
    return res.status(500).json({ error: 'server_error' });


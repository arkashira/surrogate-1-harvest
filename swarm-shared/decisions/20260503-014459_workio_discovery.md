# workio / discovery

## Final Synthesized Solution

### Diagnosis (merged, highest-risk items prioritized)
- **LINE at-least-once delivery with no idempotency** (both candidates): retries create duplicate punches and corrupt state.
- **Non-atomic find-then-upsert** (both): concurrent/retried webhooks create multiple open punches or lose clock-outs.
- **No transactional boundary for punch + audit/side-effects** (both): partial failures leave inconsistent state.
- **Missing DB-level uniqueness guard for active punch** (Candidate 1) **and for user+date+event-type** (Candidate 2): app-only enforcement is insufficient under concurrency/retries.
- **No optimistic locking for punch updates** (Candidate 2): concurrent edits/corrections can silently overwrite.
- **No compensating recovery path** (Candidate 1): no safe automated repair when duplicates occur.

### Scope
- Enforce “one active punch per user” at the DB level.
- Make webhook handler idempotent and atomic.
- Add auditability and safe update path for corrections.
- Keep changes minimal and production-safe (no breaking migrations).

---

### 1) DB schema (workio/server/src/db/schema.sql)

```sql
-- 1) Prevent multiple open punches per user (hard guarantee)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_active
ON punches (user_id)
WHERE clock_out_ts IS NULL;

-- 2) Prevent duplicate clock-in rows for same user+date (defensive)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_user_clock_in_date
ON punches (user_id, DATE(clock_in_ts))
WHERE clock_out_ts IS NULL;

-- 3) Add optimistic locking for punch updates (corrections/edits)
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

-- 4) Lightweight LINE idempotency (fast dedupe)
CREATE TABLE IF NOT EXISTS line_event_idempotency (
  event_id   VARCHAR(255) PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  punch_id   INTEGER NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 5) Audit table for punch changes (traceability)
CREATE TABLE IF NOT EXISTS punch_audit (
  id          BIGSERIAL PRIMARY KEY,
  punch_id    INTEGER NOT NULL,
  user_id     INTEGER NOT NULL,
  action      VARCHAR(32) NOT NULL,        -- 'create','update','correction'
  old_values  JSONB,
  new_values  JSONB,
  changed_by  INTEGER,                     -- system or admin user
  changed_at  TIMESTAMPTZ DEFAULT NOW()
);
```

---

### 2) Punch service (workio/server/src/services/punchService.ts)

```ts
// workio/server/src/services/punchService.ts
import { db } from '../db';
import { punches, lineEventIdempotency, punchAudit } from '../db/schema';
import { and, eq, isNull } from 'drizzle-orm';

export async function handleClockInOut({
  userId,
  eventId,
  location,
  timestamp,
  changedBy,
}: {
  userId: number;
  eventId: string;
  location?: { lat: number; lng: number };
  timestamp: Date;
  changedBy?: number;
}) {
  return db.transaction(async (tx) => {
    // Idempotency: return existing if already processed
    const idem = await tx
      .select({ punchId: lineEventIdempotency.punch_id })
      .from(lineEventIdempotency)
      .where(eq(lineEventIdempotency.event_id, eventId))
      .limit(1);

    if (idem[0]?.punchId) {
      const punch = await tx
        .select()
        .from(punches)
        .where(eq(punches.id, idem[0].punchId))
        .limit(1);
      return punch[0] ?? null;
    }

    // Find open punch
    const open = await tx
      .select()
      .from(punches)
      .where(and(eq(punches.user_id, userId), isNull(punches.clock_out_ts)))
      .limit(1);

    let punch;
    if (open[0]) {
      // Clock out with optimistic lock
      const [updated] = await tx
        .update(punches)
        .set({
          clock_out_ts: timestamp,
          version: open[0].version + 1,
          updated_at: new Date(),
        })
        .where(and(
          eq(punches.id, open[0].id),
          eq(punches.version, open[0].version)
        ))
        .returning();

      if (!updated) {
        throw new Error('Punch update conflict (version mismatch)');
      }

      punch = updated;

      // Audit clock-out
      await tx.insert(punchAudit).values({
        punch_id: punch.id,
        user_id: userId,
        action: 'update',
        old_values: { clock_out_ts: open[0].clock_out_ts, version: open[0].version },
        new_values: { clock_out_ts: punch.clock_out_ts, version: punch.version },
        changed_by: changedBy ?? userId,
      });
    } else {
      // Clock in
      const [inserted] = await tx
        .insert(punches)
        .values({
          user_id: userId,
          clock_in_ts: timestamp,
          clock_out_ts: null,
          location,
          created_at: new Date(),
          updated_at: new Date(),
          version: 1,
        })
        .returning();

      punch = inserted;

      // Audit create
      await tx.insert(punchAudit).values({
        punch_id: punch.id,
        user_id: userId,
        action: 'create',
        old_values: null,
        new_values: {
          clock_in_ts: punch.clock_in_ts,
          clock_out_ts: punch.clock_out_ts,
          location: punch.location,
        },
        changed_by: changedBy ?? userId,
      });
    }

    // Record idempotency
    await tx
      .insert(lineEventIdempotency)
      .values({
        event_id: eventId,
        user_id: userId,
        punch_id: punch.id,
      })
      .onConflictDoNothing();

    return punch;
  });
}
```

---

### 3) Webhook handler (workio/server/src/routes/line/webhook.ts)

```ts
// workio/server/src/routes/line/webhook.ts
import { Router } from 'express';
import { handleClockInOut } from '../../services/punchService';
import { verifySignature } from '../../utils/lineSignature';
import { getUserByLineId } from '../../services/userService';

const router = Router();

router.post('/webhook/line', async (req, res) => {
  const signature = req.headers['x-line-signature'] as string;
  const body = JSON.stringify(req.body);
  if (!verifySignature(body, signature)) {
    return res.status(401).send('Invalid signature');
  }

  const { events } = req.body;
  const results = [];

  for (const ev of events) {
    try {
      if (ev.type !== 'message' || ev.message.type !== 'text') continue;
      const text = ev.message.text.toLowerCase();
      if (!['clock in', 'clock out', 'ลงงาน', 'ออกงาน'].some((kw) => text.includes(kw))) continue;

      const user = await getUserByLineId(ev.source.userId);
      if (!user) {
        results.push({ eventId: ev.id, status: 'unknown_user' });
        continue;
      }

      const punch = await handleClockInOut({
        userId: user.id,
        eventId: ev.id,
        location: ev.message.location || undefined,
        timestamp: new Date(ev.timestamp),
        changedBy: user.id,
      });

      results.push({ eventId: ev.id, status: 'ok', punchId: punch?.id ?? null });
    } catch (err: any) {
      // Unique violations from idempotency/constraints are safe to ignore
      const code = err?.code || err?.message;
      if (code === '23505' || code === 'P2002') {
        results.push({ eventId: ev.id, status: 'duplicate_ignored' });
        continue;
      }
      console.error('Punch

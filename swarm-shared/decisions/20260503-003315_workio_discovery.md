# workio / discovery

**Final synthesized implementation (best parts, corrected + actionable)**

---

### 1. Diagnosis (merged & corrected)
- **Missing idempotency** on `/webhook/line` POST allows LINE retries (network blips, 5xx, slow client) to create duplicate punches.
- **No “one open punch per user” enforcement** at the database level (missing partial unique index) permits multiple open punches and corrupt state under race/retry.
- **Application-level checks are racy** without transactional upsert or DB-level de-duplication.
- **No durable idempotency record** to deduplicate across retries within a time window.
- **Inadequate observability** (logging, metrics, tracing) makes diagnosis hard.

---

### 2. Proposed change (scope + constraints)
- **Scope**:  
  - Webhook route handler (`/webhook/line`)  
  - DB schema + migration  
  - Punch model/service with transactional upsert  
  - Idempotency store (DB-backed, short TTL fallback acceptable)
- **Constraints**:  
  - Enforce **exactly-once** punch side-effects per external delivery.  
  - Enforce **at most one open punch per user** (clock_out_at IS NULL).  
  - Keep operations **atomic and safe under concurrency** (DB transaction + unique index).  
  - Be **actionable now** (migration, code, tests, verification steps).

---

### 3. Implementation (concrete steps)

#### 3.1 DB schema change (run as migration)
```sql
-- Add partial unique index: at most one open punch per user
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_user_open
ON punches (user_id)
WHERE clock_out_at IS NULL;

-- Optional: index for idempotency key lookups (helps de-dup)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_idempotency_key
ON punches (idempotency_key)
WHERE idempotency_key IS NOT NULL;

-- Ensure idempotency_key column exists (run if missing)
-- ALTER TABLE punches ADD COLUMN idempotency_key TEXT;
```

#### 3.2 Idempotent webhook handler (TypeScript/Express + Drizzle)
```ts
// workio/server/src/routes/webhook/line.ts
import { Request, Response } from 'express';
import { db } from '../db';
import { and, eq, isNull } from 'drizzle-orm';
import { punches } from '../db/schema';

function deriveIdempotencyKey(event: any): string {
  // Prefer stable, deterministic key from LINE event when possible
  const { type, timestamp, source, message } = event || {};
  const userId = source?.userId;
  if (userId && timestamp && type) {
    return `line:${type}:${timestamp}:${userId}:${message?.id || ''}`;
  }
  // Fallback (should rarely happen)
  return `line:fallback:${Date.now()}:${Math.random().toString(36).slice(2)}`;
}

export async function handleLineWebhook(req: Request, res: Response) {
  try {
    const events = req.body?.events;
    if (!Array.isArray(events) || events.length === 0) {
      return res.status(400).json({ error: 'invalid payload' });
    }

    const results = [];

    for (const event of events) {
      if (event?.type !== 'message' || event.message?.type !== 'text') {
        continue;
      }

      const userId = event.source?.userId;
      const text = String(event.message.text || '').trim().toLowerCase();
      if (!userId) continue;

      // Idempotency key: header overrides, else derive from event
      const idemKey = (req.headers['x-idempotency-key'] as string) || deriveIdempotencyKey(event);

      // Transactional upsert with idempotency check
      const result = await db.transaction(async (tx) => {
        // 1) Check idempotency key first (fast path for retries)
        const byIdem = await tx
          .select()
          .from(punches)
          .where(eq(punches.idempotency_key, idemKey))
          .limit(1);

        if (byIdem.length > 0) {
          return { punch: byIdem[0], action: 'idempotent_retry' };
        }

        // 2) Find existing open punch for user
        const existing = await tx
          .select()
          .from(punches)
          .where(and(eq(punches.user_id, userId), isNull(punches.clock_out_at)))
          .limit(1);

        const now = new Date();

        // Clock in
        if (text === 'clock in' || text === 'เข้างาน') {
          if (existing.length > 0) {
            // Already open — return existing (idempotent)
            return { punch: existing[0], action: 'already_open' };
          }

          const [inserted] = await tx
            .insert(punches)
            .values({
              user_id: userId,
              clock_in_at: now,
              clock_in_location: event.message.text, // adapt if location parsed elsewhere
              idempotency_key: idemKey,
              created_at: now,
              updated_at: now,
            })
            .returning();

          return { punch: inserted, action: 'clocked_in' };
        }

        // Clock out
        if (text === 'clock out' || text === 'เลิกงาน') {
          if (existing.length === 0) {
            // No open punch — create a closed punch record for audit (or return error)
            const [inserted] = await tx
              .insert(punches)
              .values({
                user_id: userId,
                clock_in_at: now, // best-effort; adapt per business rules
                clock_out_at: now,
                clock_out_location: event.message.text,
                idempotency_key: idemKey,
                created_at: now,
                updated_at: now,
              })
              .returning();

            return { punch: inserted, action: 'closed_without_open' };
          }

          const [updated] = await tx
            .update(punches)
            .set({
              clock_out_at: now,
              clock_out_location: event.message.text,
              updated_at: now,
              idempotency_key: idemKey,
            })
            .where(eq(punches.id, existing[0].id))
            .returning();

          return { punch: updated[0], action: 'clocked_out' };
        }

        // Unknown command
        return { error: 'unknown_command', command: text };
      });

      results.push(result);
    }

    return res.json({ ok: true, results });
  } catch (err: any) {
    // Unique violation from partial index or idempotency index -> safe retry
    if (err?.code === '23505') {
      return res.status(409).json({ error: 'duplicate', message: 'request already processed' });
    }
    console.error('Webhook handler error:', err);
    return res.status(500).json({ error: 'internal_error' });
  }
}
```

#### 3.3 Express route wiring (add idempotency header support)
```ts
// workio/server/src/routes/webhook/line.routes.ts
import { Router } from 'express';
import { handleLineWebhook } from './line';

const router = Router();

router.post('/webhook/line', handleLineWebhook);

export default router;
```

#### 3.4 Model/service (if not using direct db calls)
If you keep a Punch model/service, expose a transactional upsert that:
- Accepts `idempotencyKey`
- Uses `ON CONFLICT (idempotency_key) DO NOTHING` or equivalent
- Falls back to select-then-insert/update inside a transaction (as shown above)

---

### 4. Verification (actionable checklist)

1. **Idempotency test**  
   - Send same request twice with same `X-Idempotency-Key` (or same LINE event).  
   - Expect second response to indicate `idempotent_retry` and no new punch row.

2. **Partial index enforcement**  
   - Attempt concurrent clock-ins for same user (e.g., two requests in parallel).  
   - Expect one success and one 409 or

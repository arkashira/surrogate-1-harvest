# workio / discovery

## Final Synthesized Solution

**Core diagnosis (agreed by both):**  
- LINE retries identical webhook deliveries with the same `X-Line-Signature`.  
- App-level `findOne → update/insert` is not atomic and races under concurrency.  
- No DB-level idempotency key or unique constraint.  
- Punch and audit inserts are not transactional, so partial writes can occur.  
- No short-term replay suppression, so retries hit DB and can still cause races.

**Chosen approach (correctness + concrete actionability):**  
- Add a DB-level idempotency key (`line_delivery_id`) and a composite unique constraint/index to enforce exactly-once at storage level.  
- Use atomic `INSERT … ON CONFLICT DO NOTHING` + `RETURNING` to upsert punches.  
- Wrap punch creation and audit insert in a single transaction.  
- Add a lightweight in-memory cache (TTL 5 min) keyed by signature hash to short-circuit replays before hitting DB (optimizes for common case; DB remains source of truth).  
- Keep clock-type derivation in app code (unchanged) but ensure all writes are idempotent and transactional.

---

### 1. DB schema change (`workio/server/src/db/schema.sql`)

```sql
-- Idempotency column (nullable for existing rows)
ALTER TABLE punches
  ADD COLUMN line_delivery_id TEXT;

-- Unique constraint for idempotent LINE webhook punches
-- Allows multiple NULLs; enforces uniqueness only when line_delivery_id is present
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_line_delivery
  ON punches (tenant_id, user_id, line_delivery_id)
  WHERE line_delivery_id IS NOT NULL;
```

If you want stricter prevention of duplicate punches for the same user/tenant regardless of delivery ID (defensive), add a separate constraint/index for time-bounded uniqueness (e.g., per minute) — but for LINE retries, the `line_delivery_id` constraint above is sufficient and safest.

---

### 2. Idempotent punch service (`workio/server/src/services/linePunchService.ts`)

```ts
import { Pool } from 'pg';
import crypto from 'crypto';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

// In-memory short-term dedupe cache (TTL 5m)
const SIGNATURE_TTL = 5 * 60 * 1000;
const sigCache = new Map<string, { ts: number; punchId: number }>();

function hashSignature(signature: string): string {
  return crypto.createHash('sha256').update(signature).digest('hex');
}

function pruneSigCache() {
  const now = Date.now();
  for (const [k, v] of sigCache.entries()) {
    if (now - v.ts > SIGNATURE_TTL) sigCache.delete(k);
  }
}
setInterval(pruneSigCache, 60_000).unref();

export async function handleLinePunch({
  tenantId,
  userId,
  lineSignature,
  payload,
  clockType,
  latitude,
  longitude,
}: {
  tenantId: number;
  userId: number;
  lineSignature: string;
  payload: any;
  clockType: 'in' | 'out';
  latitude?: number;
  longitude?: number;
}) {
  const lineDeliveryId = hashSignature(lineSignature);

  // Fast-path: short-term dedupe (cache hit)
  const cached = sigCache.get(lineDeliveryId);
  if (cached && Date.now() - cached.ts < SIGNATURE_TTL) {
    return { punchId: cached.punchId, alreadyProcessed: true };
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Atomic upsert: ignore duplicates, return existing row if conflict
    const upsertRes = await client.query(
      `INSERT INTO punches (tenant_id, user_id, line_delivery_id, clock_type, latitude, longitude, created_at)
       VALUES ($1, $2, $3, $4, $5, $6, NOW())
       ON CONFLICT (tenant_id, user_id, line_delivery_id) DO NOTHING
       RETURNING id, clock_type, created_at`,
      [tenantId, userId, lineDeliveryId, clockType, latitude, longitude]
    );

    let punchId: number;
    if (upsertRes.rowCount === 0) {
      // Conflict: fetch existing row
      const fetchRes = await client.query(
        `SELECT id, clock_type, created_at FROM punches
         WHERE tenant_id = $1 AND user_id = $2 AND line_delivery_id = $3`,
        [tenantId, userId, lineDeliveryId]
      );
      if (fetchRes.rowCount === 0) {
        // Should not happen under normal conditions; fail fast
        throw new Error('Idempotency conflict but row not found');
      }
      punchId = fetchRes.rows[0].id;
    } else {
      punchId = upsertRes.rows[0].id;

      // Insert audit row in same transaction
      await client.query(
        `INSERT INTO punch_audits (tenant_id, punch_id, action, metadata, created_at)
         VALUES ($1, $2, $3, $4, NOW())`,
        [tenantId, punchId, 'webhook_received', { lineSignatureHash: lineDeliveryId, payload }]
      );
    }

    await client.query('COMMIT');

    // Cache successful result (only on success)
    sigCache.set(lineDeliveryId, { ts: Date.now(), punchId });

    return { punchId, alreadyProcessed: upsertRes.rowCount === 0 };
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}
```

Key points:  
- The unique index allows multiple `NULL` `line_delivery_id` rows (existing punches without LINE IDs) while enforcing uniqueness for LINE-originated punches.  
- `ON CONFLICT DO NOTHING` + `RETURNING` makes the upsert atomic.  
- Transaction guarantees punch + audit are written together or not at all.  
- Cache reduces DB load for retries; DB remains the source of truth.

---

### 3. Webhook handler integration (`workio/server/src/routes/lineWebhook.ts`)

```ts
import express from 'express';
import { handleLinePunch } from '../services/linePunchService';

const router = express.Router();

router.post('/webhook/line', async (req, res) => {
  const lineSignature = req.get('X-Line-Signature') || '';
  const { events } = req.body;

  if (!events || !Array.isArray(events)) {
    return res.status(400).json({ error: 'invalid_payload' });
  }

  try {
    for (const ev of events) {
      // Example handling: adapt to your actual mapping/parsing
      if (ev.type === 'message' && ev.message.type === 'text') {
        const userId = ev.source.userId; // map to internal userId via your tenant mapping
        const tenantId = 1; // resolve from your mapping

        // Derive clockType from your business logic (e.g., parse text or last state)
        const clockType: 'in' | 'out' = 'in'; // replace with real derivation

        await handleLinePunch({
          tenantId,
          userId,
          lineSignature,
          payload: ev,
          clockType,
        });
      }
    }
    return res.status(200).json({ ok: true });
  } catch (err) {
    console.error('LINE webhook error', err);
    return res.status(500).json({ error: 'processing_failed' });
  }
});

export default router;
```

---

### 4. Verification (actionable checks)

1. **Schema verification**  
   ```bash
   psql "$DATABASE_URL" -c "\d punches"
   psql "$DATABASE_URL" -c "\d idx_punches_line_delivery"
   ```
   Confirm the `line_delivery_id` column and partial unique index exist.

2. **Idempotency test**  
   - Send the same LINE webhook (same signature + payload) twice within 5 minutes.  
   - Expect: one punch row; second request returns `alreadyProcessed: true`.  
   - Verify no duplicate audit rows:
     ```sql
     SELECT id, clock_type, created_at FROM punches

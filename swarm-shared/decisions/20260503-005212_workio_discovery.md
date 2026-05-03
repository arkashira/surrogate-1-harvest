# workio / discovery

## Final synthesized solution

**Core principle**: enforce uniqueness at the database (source of truth) and add cheap, fast idempotency at the application layer to absorb LINE at-least-once retries. Keep changes minimal, transactional, and observable.

---

### 1. Database schema (single source of truth)

Apply to `workio/server/src/db/schema.sql`:

```sql
-- 1) Prevent more than one open punch per user (races/retries)
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_open
  ON punches (user_id)
  WHERE (clock_out_at IS NULL);

-- 2) Fast idempotency for webhook retries (short/long-term)
ALTER TABLE punches
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_idempotency
  ON punches (idempotency_key)
  WHERE (idempotency_key IS NOT NULL);

-- 3) Index to accelerate "find my open punch" queries
CREATE INDEX IF NOT EXISTS idx_punches_user_open_lookup
  ON punches (user_id)
  WHERE (clock_out_at IS NULL);
```

- The partial unique index guarantees exactly one open punch per user.  
- The `idempotency_key` index gives O(log n) duplicate detection and supports replay-safe deduplication (use LINE webhook `deliveryId`/message `id` or a short hash of the request body).  
- All indexes are `IF NOT EXISTS` so migrations are repeatable.

---

### 2. Readiness probe (observability)

Create `server/src/routes/health.ts`:

```ts
import { Router } from 'express';
import { pool } from '../db';

const router = Router();

router.get('/healthz', (_req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

router.get('/readyz', async (_req, res) => {
  const checks = {
    db: false,
    line_token: !!process.env.LINE_CHANNEL_ACCESS_TOKEN,
    line_secret: !!process.env.LINE_CHANNEL_SECRET,
  };

  try {
    await pool.query('SELECT 1');
    checks.db = true;
  } catch {
    checks.db = false;
  }

  const ok = Object.values(checks).every(Boolean);
  res.status(ok ? 200 : 503).json({
    status: ok ? 'ready' : 'not ready',
    checks,
    timestamp: new Date().toISOString(),
  });
});

export default router;
```

Register in your main app file (e.g. `server/src/index.ts` or `app.ts`):

```ts
import healthRoutes from './routes/health';
app.use('/healthz', healthRoutes);
app.use('/readyz', healthRoutes);
```

- `/readyz` returns **200** only when DB is reachable and required LINE env vars exist; otherwise **503**.  
- Keeps ops ability to stop routing traffic before DB or config problems cause user-visible errors.

---

### 3. Idempotent, transactional clock-in

Place this in a service (recommended: `server/src/services/punchService.ts`) and call it from your LINE webhook handler.

```ts
// server/src/services/punchService.ts
import { PoolClient } from 'pg';
import { pool } from '../db';

export async function clockIn(
  userId: string,
  clockInAt: Date,
  idempotencyKey: string,
  client?: PoolClient
): Promise<{ id: string; created: boolean }> {
  const ownsTx = !client;
  const pg = client ?? (await pool.connect());

  try {
    if (ownsTx) await pg.query('BEGIN');

    // Fast idempotency check
    const dup = await pg.query(
      `SELECT id FROM punches WHERE idempotency_key = $1`,
      [idempotencyKey]
    );
    if (dup.rows.length > 0) {
      if (ownsTx) await pg.query('COMMIT');
      return { id: dup.rows[0].id, created: false };
    }

    // Try insert; partial unique index prevents >1 open punch
    const result = await pg.query(
      `INSERT INTO punches (user_id, clock_in_at, idempotency_key, meta)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (idempotency_key) DO UPDATE
       SET idempotency_key = EXCLUDED.idempotency_key
       RETURNING id`,
      [userId, clockInAt, idempotencyKey, { source: 'line_webhook' }]
    );

    if (ownsTx) await pg.query('COMMIT');
    return { id: result.rows[0].id, created: true };
  } catch (err: any) {
    if (ownsTx) await pg.query('ROLLBACK');

    // Unique violation on partial index: duplicate open punch (race/retry)
    if (err?.code === '23505') {
      // Resolve to the existing open punch row
      const existing = await pg.query(
        `SELECT id FROM punches WHERE user_id = $1 AND clock_out_at IS NULL`,
        [userId]
      );
      if (existing.rows.length > 0) {
        return { id: existing.rows[0].id, created: false };
      }
      // If no open row exists but we got 23505, rethrow (unexpected)
    }
    throw err;
  } finally {
    if (ownsTx) pg.release();
  }
}
```

In your LINE webhook handler (`server/src/routes/line.ts`):

```ts
import { clockIn } from '../services/punchService';

async function handleClockInEvent(event) {
  const userId = /* extract from LINE source/user */;
  const clockInAt = new Date(event.timestamp);
  // Use a stable dedupe key: LINE message/delivery id or hash of body
  const idempotencyKey = event.message?.id || stableHash(event);

  const { created } = await clockIn(userId, clockInAt, idempotencyKey);
  if (!created) {
    // Duplicate or already open; safe to ack
    console.info(`Idempotent clock-in deduped for user=${userId}`);
  }
  // respond to LINE / continue processing
}
```

- Uses **transaction + idempotency key** for fast duplicate detection.  
- Falls back to the partial unique index for absolute enforcement (races, concurrent retries).  
- Returns existing open punch on conflict so callers have a stable `punchId`.

---

### 4. Verification checklist

1. **Apply schema**  
   ```bash
   psql $DATABASE_URL -f server/src/db/schema.sql
   ```
   Confirm:
   ```sql
   SELECT indexname, indexdef FROM pg_indexes
   WHERE tablename = 'punches'
     AND indexname LIKE '%punches%';
   ```

2. **Readiness probe**  
   ```bash
   curl -i http://localhost:3000/readyz
   # 200 when healthy; 503 when DB/config missing
   ```

3. **Idempotency test**  
   - Send two identical LINE webhook payloads for the same user within seconds.  
   - Query open punches:
     ```sql
     SELECT * FROM punches
     WHERE user_id = '<test-user>' AND clock_out_at IS NULL;
     ```
     Must return exactly one row.

4. **Constraint enforcement**  
   - Manual duplicate insert attempt must fail with `23505`.  
   - Explain plan for open-punch lookup should show `Index Scan using idx_punches_user_open_lookup`.

5. **Traceability**  
   - Verify `idempotency_key` is populated and unique per webhook delivery.  
   - Use it to build replay/deduplication windows or audit logs.

---

### Why this synthesis wins

- Combines **Candidate 1’s partial unique constraint and readiness probe** (correct, minimal) with **Candidate 2’s idempotency column** (fast, explicit dedupe).  
- Resolves contradictions by preferring **database-enforced uniqueness** (always correct) and adding **lightweight idempotency key** (fast, cheap) rather than choosing one.  
- Provides **transactional upsert**, observability, and concrete verification steps in a single actionable plan.

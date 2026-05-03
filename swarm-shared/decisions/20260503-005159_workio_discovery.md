# workio / discovery

## Final synthesized implementation (best of both proposals)

### 1. Diagnosis (merged)
- **Race/retry duplicates**: at-least-once LINE webhook + check-then-insert allows multiple open punches (`clock_out_at IS NULL`) for the same user/tenant.
- **Missing DB guard**: no partial unique index to enforce “one open punch per user per tenant”.
- **No idempotency storage**: repeated webhook deliveries (same event) create duplicate rows.
- **Non-atomic upsert**: app-layer check-then-insert has race windows under concurrency/retries.
- **Missing ops observability**: no `/readyz` to verify DB + LINE config before traffic.
- **Missing reporting index**: tenant + time queries degrade at scale.

---

### 2. Scope & goal
- **Files**:
  - `server/src/db/schema.sql` — constraint + idempotency table + reporting index.
  - `server/src/db/indexes.sql` — kept consistent with schema changes.
  - `server/src/routes/health.ts` — `/readyz` (+ `/healthz`).
  - `server/src/routes/line.ts` — idempotent, transactional upsert handler.
- **Goal**: eliminate duplicate open punches, make retries safe, provide readiness probe — implementable in <2h.

---

### 3. Implementation

#### 3.1 DB schema (schema.sql)
```sql
-- Idempotency for LINE webhook events
CREATE TABLE IF NOT EXISTS line_idempotency (
  idempotency_key TEXT PRIMARY KEY,
  event_type      TEXT NOT NULL,
  user_id         INTEGER NOT NULL,
  tenant_id       INTEGER NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_line_idempotency_expiry
  ON line_idempotency (created_at);

-- One open punch per user per tenant (DB-level guard)
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_punch_per_tenant
  ON clock_in_records (tenant_id, user_id)
  WHERE clock_out_at IS NULL;

-- Reporting index: tenant + time
CREATE INDEX IF NOT EXISTS idx_clock_in_tenant_time
  ON clock_in_records (tenant_id, clock_in_at DESC);
```

#### 3.2 Optional: cleanup job (recommended)
Add a daily job to prune `line_idempotency` older than 48–72h to keep table small.

#### 3.3 Health/readiness endpoint (health.ts)
```ts
// server/src/routes/health.ts
import { Router } from 'express';
import { pool } from '../db';

const router = Router();

router.get('/healthz', (_req, res) => res.json({ status: 'ok' }));

router.get('/readyz', async (_req, res) => {
  try {
    await pool.query('SELECT 1');
    if (!process.env.LINE_CHANNEL_ACCESS_TOKEN) {
      return res.status(503).json({ status: 'error', reason: 'missing_LINE_CHANNEL_ACCESS_TOKEN' });
    }
    res.json({ status: 'ready' });
  } catch (err) {
    res.status(503).json({ status: 'error', reason: 'db_unavailable' });
  }
});

export default router;
```

Wire into app:
```ts
// server/src/app.ts (or index.ts)
import healthRouter from './routes/health';
app.use(healthRouter);
```

#### 3.4 Idempotent LINE clock-in handler (line.ts)
```ts
// server/src/routes/line.ts
import { Router, Request, Response } from 'express';
import { pool } from '../db';

const router = Router();

function deriveIdempotencyKey(ev: any, userId: number): string {
  // Prefer stable LINE delivery ID; fallback to deterministic hash
  if (ev.source?.userId && ev.deliveryId) {
    return `line:${ev.deliveryId}`;
  }
  const payload = `${ev.timestamp}:${ev.type}:${ev.message?.id || ''}:${userId}`;
  return `line:sha256:${require('crypto').createHash('sha256').update(payload).digest('hex')}`;
}

router.post('/webhook', async (req: Request, res: Response) => {
  const events = req.body?.events;
  if (!events || !Array.isArray(events)) return res.sendStatus(400);

  // Process serially per webhook to reduce contention; keep order
  for (const ev of events) {
    if (ev.type !== 'message' || ev.message?.type !== 'text') continue;

    // Extract identity (adjust to your payload shape)
    const userId = Number(ev.source?.userId) || Number(ev?.userId);
    const tenantId = Number(ev?.tenantId); // or derive from user record
    const clockInAt = ev.timestamp ? new Date(ev.timestamp) : new Date();

    if (!userId || !tenantId) continue;

    const idempotencyKey = deriveIdempotencyKey(ev, userId);

    try {
      await upsertClockIn(userId, tenantId, clockInAt, idempotencyKey);
    } catch (err) {
      // Log and continue processing other events; avoid failing entire batch for one item
      console.error('Failed to process LINE clock-in', { userId, tenantId, idempotencyKey, err });
    }
  }

  res.sendStatus(200);
});

async function upsertClockIn(userId: number, tenantId: number, clockInAt: Date, idempotencyKey: string) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Idempotency check
    const dup = await client.query(
      'SELECT 1 FROM line_idempotency WHERE idempotency_key = $1',
      [idempotencyKey]
    );
    if (dup.rows.length > 0) {
      await client.query('ROLLBACK');
      return { applied: false, reason: 'duplicate' };
    }

    // Close any open punch for this user+tenant (safety/cleanup)
    await client.query(
      `UPDATE clock_in_records
       SET clock_out_at = NOW()
       WHERE user_id = $1 AND tenant_id = $2 AND clock_out_at IS NULL`,
      [userId, tenantId]
    );

    // Insert new open punch
    await client.query(
      `INSERT INTO clock_in_records (user_id, tenant_id, clock_in_at, clock_out_at)
       VALUES ($1, $2, $3, NULL)`,
      [userId, tenantId, clockInAt]
    );

    // Record idempotency
    await client.query(
      `INSERT INTO line_idempotency (idempotency_key, event_type, user_id, tenant_id)
       VALUES ($1, 'clock_in', $2, $3)`,
      [idempotencyKey, userId, tenantId]
    );

    await client.query('COMMIT');
    return { applied: true };
  } catch (err: any) {
    await client.query('ROLLBACK');
    // Unique violation from partial index: treat as no-op (race/duplicate)
    if (err.code === '23505') {
      return { applied: false, reason: 'duplicate_open_punch' };
    }
    throw err;
  } finally {
    client.release();
  }
}

export default router;
```

---

### 4. Verification (merged)

1. **DB constraint**
   ```bash
   psql workio -c "SELECT indexname, indexdef FROM pg_indexes WHERE tablename='clock_in_records';"
   # Should show idx_one_open_punch_per_tenant with WHERE clock_out_at IS NULL
   ```
   - Try inserting two open punches for same tenant+user in separate transactions; second must fail with unique violation.

2. **Idempotency**
   - Replay same LINE webhook payload (or same derived idempotency key) twice.
   - Verify only one row with `clock_out_at IS NULL` exists and `line_idempotency` contains the key.

3. **Readiness probe**
   ```bash
   curl -i http://localhost:3000/readyz
   # 200 + {"status":"ready"} when DB up and LINE token present
   # 503 when DB down or token missing
   ```

4. **Race/retry test**
   - Fire concurrent `upsertClockIn` calls

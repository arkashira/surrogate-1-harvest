# workio / discovery

Below is the **single, merged implementation** that keeps the strongest, most actionable parts from both proposals, removes contradictions, and prioritizes correctness + deployable safety.

---

## 1. Diagnosis (merged)

- LINE webhook retries create duplicate punches because ingestion lacks idempotency at the message-id level (no dedup table or idempotency-key enforcement).
- Punch upsert is non-atomic (`findOne` → conditional `insert`/`update`) and races under concurrency (bursts from LINE retries or frontend retries).
- Tenant+employee+date+event window is not protected by a unique constraint, so duplicates can persist and corrupt daily summaries.
- No transactional boundary between punch write and idempotency/dedup state, risking partial updates on failure.
- Missing tenant isolation in writes and missing audit trail for dedupe decisions make debugging and replay safety opaque.
- Frontend clock-in/out API lacks server-side idempotency key support (`Idempotency-Key`).

---

## 2. Proposed change (merged scope)

- `workio/server/src/db/schema.sql`  
  - Add idempotency table and tenant-isolated unique constraint/index for punches.
- `workio/server/src/middleware/idempotency.ts` (new)  
  - Parse/validate `Idempotency-Key` and `X-Line-Message-Id` headers; transactional dedup.
- `workio/server/src/controllers/punchController.ts`  
  - Atomic upsert using constraint; safe race handling; return canonical punch.
- `workio/server/src/controllers/lineWebhook.ts`  
  - Use idempotency + atomic upsert; return final punch to caller for LINE-side observability.
- `workio/server/src/routes/punchRoutes.ts`  
  - Wire idempotency middleware for API routes.

---

## 3. Implementation

### 3.1 DB schema — idempotency + constraints

```sql
-- workio/server/src/db/schema.sql

-- Idempotency keys for API calls and LINE webhook messages
CREATE TABLE IF NOT EXISTS idempotency_keys (
  id            SERIAL PRIMARY KEY,
  tenant_id     INTEGER NOT NULL,
  key_hash      TEXT    NOT NULL,
  payload_hash  TEXT    NOT NULL,
  entity_type   TEXT    NOT NULL, -- 'punch'
  entity_id     INTEGER,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (tenant_id, key_hash)
);

-- Ensure one punch record per tenant+employee+date+event_type window
-- Protects daily summaries and prevents duplicates
CREATE UNIQUE INDEX IF NOT EXISTS idx_punch_tenant_employee_date_type
  ON punches (tenant_id, employee_id, DATE(timestamp), event_type)
  WHERE deleted_at IS NULL;

-- Fast lookups for idempotency
CREATE INDEX IF NOT EXISTS idx_idempotency_tenant_key
  ON idempotency_keys (tenant_id, key_hash);
```

---

### 3.2 Idempotency middleware (unified)

```ts
// workio/server/src/middleware/idempotency.ts
import { Request, Response, NextFunction } from 'express';
import { Pool, PoolClient } from 'pg';
import { getPool } from '../db';
import { createHash } from 'crypto';

const IDENTITY_TTL_MS = 1000 * 60 * 60 * 24; // 24h

function hash(value: string) {
  return createHash('sha256').update(value).digest('hex');
}

export async function idempotency(
  req: Request & { dbClient?: PoolClient },
  res: Response,
  next: NextFunction
) {
  const key =
    req.get('Idempotency-Key') ||
    req.get('X-Idempotency-Key') ||
    req.get('X-Line-Message-Id');
  const tenantId = req.body.tenantId || req.query.tenantId;

  // Skip if no key provided (controller may handle LINE dedup separately)
  if (!key || !tenantId) return next();

  const client: PoolClient = req.dbClient || (await getPool().connect());
  try {
    await client.query('BEGIN');

    const keyHash = hash(String(key));
    const payloadHash = hash(JSON.stringify(req.body));

    // Check existing
    const { rows } = await client.query(
      `SELECT entity_id, created_at FROM idempotency_keys
       WHERE tenant_id = $1 AND key_hash = $2`,
      [tenantId, keyHash]
    );

    if (rows.length > 0) {
      const row = rows[0];
      // If within TTL, return previous entity id; else allow overwrite (replay after expiry)
      if (Date.now() - new Date(row.created_at).getTime() < IDENTITY_TTL_MS) {
        await client.query('ROLLBACK');
        if (!req.dbClient) client.release();
        (req as any).idempotentReplay = { entityId: row.entity_id };
        return next();
      }
    }

    // Store intent (entity_id filled later by controller)
    await client.query(
      `INSERT INTO idempotency_keys (tenant_id, key_hash, payload_hash, entity_type, entity_id)
       VALUES ($1, $2, $3, $4, NULL)
       ON CONFLICT (tenant_id, key_hash) DO UPDATE
       SET payload_hash = EXCLUDED.payload_hash, created_at = NOW()`,
      [tenantId, keyHash, payloadHash, 'punch']
    );

    (req as any).idempotency = { client, committed: false, keyHash, tenantId };
    next();
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    if (!req.dbClient) client.release();
    next(err);
  }
}

export async function commitIdempotency(req: Request, entityId: number | null) {
  const imp = (req as any).idempotency;
  if (!imp || imp.committed) return;
  const client: PoolClient = imp.client;
  try {
    await client.query(
      `UPDATE idempotency_keys
       SET entity_id = $1
       WHERE tenant_id = $2 AND key_hash = $3`,
      [entityId, imp.tenantId, imp.keyHash]
    );
    await client.query('COMMIT');
    imp.committed = true;
  } finally {
    if (!req.dbClient) client.release();
  }
}
```

---

### 3.3 Atomic punch upsert in controller

```ts
// workio/server/src/controllers/punchController.ts
import { Request, Response } from 'express';
import { Pool, PoolClient } from 'pg';
import { getPool } from '../db';
import { commitIdempotency } from '../middleware/idempotency';

export async function clockInOut(req: Request, res: Response) {
  const { tenantId, employeeId, eventType, timestamp, latitude, longitude } = req.body;
  const client = (req as any).idempotency?.client as PoolClient | undefined;
  const db = client || getPool();

  // If idempotent replay detected, return previous punch
  if ((req as any).idempotentReplay) {
    const { entityId } = (req as any).idempotentReplay;
    const { rows } = await db.query('SELECT * FROM punches WHERE id = $1', [entityId]);
    return res.json({ ok: true, punch: rows[0], replay: true });
  }

  try {
    if (!client) await db.query('BEGIN');

    // Atomic upsert using unique constraint
    const { rows } = await db.query(
      `INSERT INTO punches (tenant_id, employee_id, event_type, timestamp, latitude, longitude)
       VALUES ($1, $2, $3, $4, $5, $6)
       ON CONFLICT ON CONSTRAINT idx_punch_tenant_employee_date_type
       DO UPDATE SET timestamp = EXCLUDED.timestamp,
                     latitude = EXCLUDED.latitude,
                     longitude = EXCLUDED.longitude,
                     updated_at = NOW()
       RETURNING *`,
      [tenantId, employeeId, eventType, timestamp, latitude, longitude]
    );

    const punch = rows[0];

    if (client) {
      await commitIdempotency(req, punch.id);
    } else {
      await db.query('COMMIT');
    }

    return res.json({ ok: true, punch });

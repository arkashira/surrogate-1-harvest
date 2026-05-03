# workio / discovery

## Final consolidated solution (correct + immediately actionable)

### 1. Root causes (merged + prioritized)
- **Missing LINE signature verification** (`X-Line-Signature`) → allows tampered/replayed webhooks.
- **No idempotency / replay protection** → LINE retries and client replays create duplicate punches.
- **Race condition on open punches** → concurrent requests can create multiple `(tenant_id, employee_id)` rows with `clock_out_at IS NULL`.
- **No tenant-scoped uniqueness enforcement** → database permits multiple open punches per employee per tenant.
- **Body parsing breaks signature check** → JSON middleware mutates body and invalidates HMAC verification.

### 2. Required database constraints (run first)
```sql
-- Prevent multiple open punches per employee per tenant
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_one_open_per_employee
ON punches (tenant_id, employee_id)
WHERE clock_out_at IS NULL;

-- Fast lookup for closing latest open punch and idempotency checks
CREATE INDEX IF NOT EXISTS idx_punches_open_lookup
ON punches (tenant_id, employee_id, clock_in_at DESC)
WHERE clock_out_at IS NULL;
```

### 3. Idempotency store (production-ready choice)
- Use **Redis** with key:  
  `line-webhook:{tenantId}:{sha256(signature + rawBody)}`  
  TTL: **10 minutes** (safe window for LINE retries).  
  If Redis is unavailable, fall back to DB with same key and a `processed_at` timestamp + unique constraint to enforce idempotency.

### 4. Implementation files

#### 4.1. Install dependencies
```bash
npm install ioredis @types/ioredis
# or yarn add ioredis @types/ioredis
```

#### 4.2. `/opt/axentx/workio/server/src/routes/webhook/line.ts`
```ts
import { Router, Request, Response, NextFunction } from 'express';
import crypto from 'crypto';
import { Pool } from 'pg';
import Redis from 'ioredis';

const router = Router();
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

const redis = new Redis(process.env.REDIS_URL || 'redis://localhost:6379');
const IDEMPOTENCY_TTL_SEC = 600; // 10 minutes

function verifyLineSignature(rawBody: string, signature: string): boolean {
  const channelSecret = process.env.LINE_CHANNEL_SECRET;
  if (!channelSecret || !signature) return false;
  const expected = crypto
    .createHmac('sha256', channelSecret)
    .update(rawBody, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

function getIdempotencyKey(tenantId: string, signature: string, rawBody: string): string {
  const hash = crypto.createHash('sha256').update(signature + rawBody).digest('hex');
  return `line-webhook:${tenantId}:${hash}`;
}

async function resolveLineUserIdToEmployee(lineUserId: string, tenantId: string): Promise<string | null> {
  const result = await pool.query(
    `SELECT employee_id FROM employee_line_mapping WHERE line_user_id = $1 AND tenant_id = $2`,
    [lineUserId, tenantId]
  );
  return result.rows[0]?.employee_id || null;
}

async function upsertPunch(tenantId: string, employeeId: string, action: 'clock_in' | 'clock_out') {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    if (action === 'clock_in') {
      // Check for existing open punch (FOR UPDATE to block races)
      const openCheck = await client.query(
        `SELECT id FROM punches
         WHERE tenant_id = $1 AND employee_id = $2 AND clock_out_at IS NULL
         FOR UPDATE`,
        [tenantId, employeeId]
      );

      if (openCheck.rows.length > 0) {
        await client.query('ROLLBACK');
        return { employee_id: employeeId, action: 'clock_in', status: 'already_open', punch_id: openCheck.rows[0].id };
      }

      const punchId = `punch_${crypto.randomUUID()}`;
      await client.query(
        `INSERT INTO punches (id, tenant_id, employee_id, clock_in_at, clock_out_at, created_at)
         VALUES ($1, $2, $3, NOW(), NULL, NOW())`,
        [punchId, tenantId, employeeId]
      );
      await client.query('COMMIT');
      return { employee_id: employeeId, action: 'clock_in', status: 'created', punch_id: punchId };
    } else {
      // clock_out: close latest open punch
      const openCheck = await client.query(
        `SELECT id FROM punches
         WHERE tenant_id = $1 AND employee_id = $2 AND clock_out_at IS NULL
         ORDER BY clock_in_at DESC LIMIT 1 FOR UPDATE`,
        [tenantId, employeeId]
      );

      if (openCheck.rows.length === 0) {
        await client.query('ROLLBACK');
        return { employee_id: employeeId, action: 'clock_out', status: 'no_open_punch' };
      }

      const punchId = openCheck.rows[0].id;
      await client.query(
        `UPDATE punches SET clock_out_at = NOW() WHERE id = $1`,
        [punchId]
      );
      await client.query('COMMIT');
      return { employee_id: employeeId, action: 'clock_out', status: 'closed', punch_id: punchId };
    }
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally {
    client.release();
  }
}

router.post('/line', async (req: Request, res: Response, NextFunction: NextFunction) => {
  try {
    const signature = req.headers['x-line-signature'] as string;
    const rawBody = req.rawBody as string;
    const tenantId = req.headers['x-tenant-id'] as string;

    if (!tenantId) {
      return res.status(400).json({ error: 'Missing tenant identifier' });
    }

    // 1) Signature verification
    if (!signature || !verifyLineSignature(rawBody, signature)) {
      return res.status(401).json({ error: 'Invalid signature' });
    }

    // 2) Idempotency check
    const idemKey = getIdempotencyKey(tenantId, signature, rawBody);
    const cached = await redis.get(idemKey);
    if (cached) {
      return res.status(200).json(JSON.parse(cached));
    }

    // 3) Process events
    const payload = req.body;
    const events = payload.events || [];
    const results = [];

    for (const event of events) {
      if (event?.type === 'message' && event.message?.type === 'text') {
        const employeeId = await resolveLineUserIdToEmployee(event.source.userId, tenantId);
        if (!employeeId) continue;

        const text = event.message.text.trim().toLowerCase();
        if (text === 'in' || text === 'out') {
          const punch = await upsertPunch(tenantId, employeeId, text === 'in' ? 'clock_in' : 'clock_out');
          results.push(punch);
        }
      }
    }

    const responseBody = { ok: true, results };

    // 4) Store idempotent response
    await redis.setex(idemKey, IDEMPOTENCY_TTL_SEC, JSON.stringify(responseBody));
    return res.status(200).json(responseBody);
  } catch (error) {
    console.error('LINE webhook error:', error);
    return res.status(500).json({ error: 'Internal server error' });
  }
});

export default router;
```

#### 4.3. `/opt/axentx/workio/server/src/routes/webhook/index.ts`
```ts
import { Router } from 'express';
import lineWebhook from './line';

const router = Router();
router.use('/line', lineWebhook);
export default router;
```

#### 4.4. Wire up in app (preserve raw body for LINE)
In your main app file (e.g., `app.ts` or `server.ts`), ensure the LINE route uses a raw-body parser **before** JSON parsing:


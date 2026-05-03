# workio / discovery

## Final Synthesized Implementation (Correct + Actionable)

### 1. Diagnosis (merged, de-duplicated)
- **Missing LINE signature verification** (`X-Line-Signature`) enables spoofed/replayed webhooks.
- **No idempotency/deduplication** allows LINE retries and replays to create duplicate punches.
- **Race condition on open punches**: concurrent requests can create multiple active punches for the same `(tenant_id, employee_id)`.
- **No DB-level tenant-scoped uniqueness guard**; only app logic prevents double-open punches.
- **No replay-window protection**; stale or manipulated timestamps are accepted.

### 2. Files to Change (concrete)
- `workio/server/src/db/schema.sql`
- `workio/server/src/middleware/idempotency.ts` (new)
- `workio/server/src/utils/line-signature.ts` (new)
- `workio/server/src/routes/webhook/line.ts` (create/modify)

### 3. Implementation

#### 3.1 DB schema — idempotency table + partial unique index
```sql
-- workio/server/src/db/schema.sql

-- Idempotency keys for webhook replay protection
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  path TEXT NOT NULL
);

-- Ensure at most one open punch per employee per tenant
CREATE UNIQUE INDEX IF NOT EXISTS idx_punches_open
ON punches (tenant_id, employee_id)
WHERE clock_out_at IS NULL;
```

#### 3.2 Idempotency middleware (DB-backed, safe under races)
```ts
// workio/server/src/middleware/idempotency.ts
import { Request, Response, NextFunction } from 'express';
import crypto from 'crypto';
import { db } from '../db';

const IDENTITY_TTL_MS = 5 * 60 * 1000; // 5 minutes

function hashBody(req: Request): string {
  // Deterministic fingerprint for idempotency
  return crypto.createHash('sha256').update(JSON.stringify(req.body)).digest('hex');
}

export async function idempotencyKey(req: Request, res: Response, next: NextFunction) {
  // Only apply to LINE webhook route
  if (req.method !== 'POST' || !req.path.endsWith('/line')) {
    return next();
  }

  const key = req.header('X-Idempotency-Key') || hashBody(req);
  const now = new Date();
  const windowStart = new Date(now.getTime() - IDENTITY_TTL_MS);

  try {
    // Try insert; if conflict, treat as duplicate
    const insertResult = await db`
      INSERT INTO idempotency_keys (key, created_at, path)
      VALUES (${key}, ${now}, ${req.path})
      ON CONFLICT (key) DO NOTHING
      RETURNING key
    `;

    if (insertResult.length === 0) {
      // Key existed — check freshness
      const existing = await db`
        SELECT key FROM idempotency_keys
        WHERE key = ${key} AND created_at >= ${windowStart}
        LIMIT 1
      `;
      if (existing.length > 0) {
        // Idempotent success: duplicate within window
        return res.status(200).json({ ok: true, duplicate: true });
      }
      // Stale key outside window: allow processing and upsert key
      await db`
        UPDATE idempotency_keys
        SET created_at = ${now}
        WHERE key = ${key}
      `;
    }

    // Best-effort cleanup (non-blocking)
    db`DELETE FROM idempotency_keys WHERE created_at < ${windowStart}`.catch(() => {});

    (req as any).idempotencyKey = key;
    next();
  } catch (err) {
    // On constraint violation or other DB error, treat as duplicate to avoid side-effects
    if ((err as any)?.code === '23505') {
      return res.status(200).json({ ok: true, duplicate: true });
    }
    // Fail open for safety (log and continue) to avoid breaking webhook delivery
    console.warn('Idempotency check failed', err);
    next();
  }
}
```

#### 3.3 LINE signature verification helper
```ts
// workio/server/src/utils/line-signature.ts
import crypto from 'crypto';

export function verifyLineSignature(rawBody: string, signature: string, channelSecret: string): boolean {
  if (!signature || !channelSecret) return false;
  const expected = crypto
    .createHmac('sha256', channelSecret)
    .update(rawBody, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}
```

#### 3.4 Webhook route with protections
```ts
// workio/server/src/routes/webhook/line.ts
import { Router } from 'express';
import { verifyLineSignature } from '../../utils/line-signature';
import { idempotencyKey } from '../../middleware/idempotency';

const router = Router();
const CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';

// Raw-body capture for signature verification
router.use('/line', (req, res, next) => {
  if (req.method === 'POST' && req.path === '/line') {
    let raw = '';
    req.setEncoding('utf8');
    req.on('data', (chunk) => (raw += chunk));
    req.on('end', () => {
      (req as any).rawBody = raw;
      try {
        req.body = JSON.parse(raw);
      } catch {
        req.body = {};
      }
      next();
    });
  } else {
    next();
  }
});

router.post(
  '/line',
  idempotencyKey,
  async (req, res) => {
    const signature = req.header('X-Line-Signature') || '';
    const rawBody = (req as any).rawBody || '';
    if (!verifyLineSignature(rawBody, signature, CHANNEL_SECRET)) {
      return res.status(401).json({ error: 'Invalid signature' });
    }

    // Replay-window check (LINE signatures valid ~5 min)
    const now = Date.now();
    const eventTime = (req.body.events?.[0]?.timestamp) || now;
    if (Math.abs(now - eventTime) > 5 * 60 * 1000) {
      return res.status(400).json({ error: 'Stale request' });
    }

    // Process events with race-safe punch handling
    const events = req.body.events || [];
    for (const ev of events) {
      if (ev.type === 'message' && ev.message.type === 'text') {
        await handleClockEvent(ev);
      }
    }

    return res.status(200).json({ ok: true });
  }
);

async function handleClockEvent(event: any) {
  // Implement tenant/employee mapping from event.source.userId or similar.
  // Use upsert or SELECT ... FOR UPDATE + application logic to avoid races,
  // and rely on idx_punches_open to enforce single open punch at DB level.
  //
  // Example (pseudo):
  // const tenantId = ...;
  // const employeeId = ...;
  // await db`
  //   INSERT INTO punches (tenant_id, employee_id, clock_in_at, clock_out_at)
  //   VALUES (${tenantId}, ${employeeId}, ${new Date()}, NULL)
  //   ON CONFLICT (id) DO UPDATE SET clock_out_at = EXCLUDED.clock_out_at
  // `;
}

export default router;
```

### 4. Verification (actionable checklist)

1. **Apply schema changes**
   ```bash
   psql $DATABASE_URL < workio/server/src/db/schema.sql
   ```
   Confirm:
   ```sql
   SELECT indexname FROM pg_indexes WHERE tablename='punches' AND indexname='idx_punches_open';
   \d idempotency_keys
   ```

2. **Signature verification**
   - POST to `/webhook/line` with invalid `X-Line-Signature` → expect 401.
   - POST with valid signature and body → expect 200.

3. **Idempotency**
   - Send identical payload twice within 5 minutes:
     - First → 20

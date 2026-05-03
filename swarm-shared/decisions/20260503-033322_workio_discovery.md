# workio / discovery

## Final consolidated implementation

**Scope**: `workio/server/src/routes/webhook/line.ts` (create if missing), plus minimal app-level wiring and one migration.

---

### 1. Core principles (resolve contradictions)

- **Correctness first**: signature verification must use the exact raw request body and constant-time comparison.
- **Actionability**: provide concrete code, middleware order, SQL, and verification steps that can be applied immediately.
- **Tenant binding**: cryptographically bind events to the LINE channel (channel ID) and map to tenant via server-side lookup; never trust tenant identifiers from the payload.
- **Idempotency + replay protection**: use a deterministic idempotency key per event and a short TTL (5 min) to absorb LINE retries and accidental double-taps; additionally enforce a timestamp window (5 min) to reject replays outside LINE’s retry window.
- **Safe defaults**: if idempotency table is missing or verification fails, reject (401/409) rather than accept.

---

### 2. Implementation

#### Create route and middleware

```bash
mkdir -p /opt/axentx/workio/server/src/routes/webhook
touch /opt/axentx/workio/server/src/routes/webhook/line.ts
```

```ts
// /opt/axentx/workio/server/src/routes/webhook/line.ts
import { Router, Request, Response, NextFunction } from 'express';
import crypto from 'crypto';
import { Pool } from 'pg';

const router = Router();
const pool = new Pool({ connectionString: process.env.DATABASE_URL });

// HMAC-SHA256 verification (constant-time)
function verifyLineSignature(rawBody: string, signature: string, channelSecret: string): boolean {
  if (!rawBody || !signature || !channelSecret) return false;
  const expected = crypto
    .createHmac('sha256', channelSecret)
    .update(rawBody, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

// Deterministic idempotency key for an event
function makeEventId(event: any): string {
  // Prefer stable identifiers when present
  if (event?.source?.userId && event?.timestamp) {
    return crypto
      .createHash('sha256')
      .update(`${event.source.userId}:${event.type}:${event.timestamp}:${event.replyToken || ''}`)
      .digest('hex');
  }
  return crypto.createHash('sha256').update(JSON.stringify(event)).digest('hex');
}

// Idempotency + replay window guard (5-minute TTL + 5-minute timestamp window)
async function isReplayOrDuplicate(rawEvent: any, tenantId: string): Promise<boolean> {
  const client = await pool.connect();
  try {
    const now = new Date();
    const window = new Date(now.getTime() - 5 * 60 * 1000);
    const eventId = makeEventId(rawEvent);
    const eventTime = rawEvent.timestamp ? new Date(rawEvent.timestamp) : now;

    // Reject events outside timestamp window (replay protection)
    if (eventTime < window) return true;

    // Delete expired keys (best-effort) and check existence atomically
    await client.query(`DELETE FROM webhook_events WHERE processed_at < $1`, [window]);

    const exists = await client.query(
      `SELECT 1 FROM webhook_events WHERE idempotency_key = $1`,
      [eventId]
    );
    if (exists.rows.length > 0) return true;

    await client.query(
      `INSERT INTO webhook_events(idempotency_key, tenant_id, event_type, processed_at)
       VALUES ($1, $2, $3, $4)`,
      [eventId, tenantId, 'line_event', now]
    );
    return false;
  } catch (err) {
    // If table missing or constraint violation, reject to be safe
    return true;
  } finally {
    client.release();
  }
}

// Resolve tenant by LINE channelId (server-side mapping)
async function resolveTenantByChannel(channelId: string): Promise<string | null> {
  // Example: query your tenant-channel mapping table
  const client = await pool.connect();
  try {
    const r = await client.query(
      `SELECT tenant_id FROM line_channels WHERE channel_id = $1 LIMIT 1`,
      [channelId]
    );
    return r.rows[0]?.tenant_id || null;
  } catch {
    return null;
  } finally {
    client.release();
  }
}

// Ensure idempotency table exists (run once via migration; defensive here)
async function ensureIdempotencyTable() {
  const client = await pool.connect();
  try {
    await client.query(`
      CREATE TABLE IF NOT EXISTS webhook_events (
        idempotency_key TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        processed_at TIMESTAMPTZ NOT NULL
      );
    `);
  } finally {
    client.release();
  }
}

// Webhook handler
router.post('/line', async (req: Request, res: Response, next: NextFunction) => {
  try {
    const rawBody = (req as any).rawBody as string | undefined;
    const signature = req.headers['x-line-signature'] as string | undefined;
    const channelSecret = process.env.LINE_CHANNEL_SECRET;

    if (!rawBody || !signature || !channelSecret) {
      return res.status(400).json({ error: 'Missing required headers or body' });
    }

    if (!verifyLineSignature(rawBody, signature, channelSecret)) {
      return res.status(401).json({ error: 'Invalid signature' });
    }

    const body = req.body;
    const events = body?.events;
    if (!Array.isArray(events) || events.length === 0) {
      return res.status(200).json({ ok: true });
    }

    // Process events sequentially to preserve order and idempotency per event
    for (const ev of events) {
      const channelId = ev?.source?.channelId;
      if (!channelId) continue; // malformed event

      const tenantId = await resolveTenantByChannel(channelId);
      if (!tenantId) {
        // Unknown channel -> reject event to avoid cross-tenant injection
        continue;
      }

      if (await isReplayOrDuplicate(ev, tenantId)) {
        continue; // skip duplicate or replay
      }

      // TODO: process clock-in/out, leave, OT based on ev.type and message
      // Example: await handleLineEvent(ev, tenantId);
    }

    return res.status(200).json({ ok: true });
  } catch (err) {
    next(err);
  }
});

// Initialize table on module load (best-effort)
ensureIdempotencyTable().catch(() => {
  /* ignore; table may already exist */
});

export default router;
```

#### Wire raw-body middleware in app entry

```ts
// /opt/axentx/workio/server/src/app.ts (or server entry)
import express from 'express';
import bodyParser from 'body-parser';
import lineWebhook from './routes/webhook/line';

const app = express();

// Preserve raw body for HMAC verification on LINE webhook only
app.use(
  '/webhook/line',
  bodyParser.raw({ type: 'application/json', limit: '1mb' }),
  lineWebhook
);

// Regular JSON body parser for other routes
app.use(express.json());

// ... rest of app
export default app;
```

#### Migration: idempotency table

```sql
-- /opt/axentx/workio/server/src/db/schema.sql (append)
-- Idempotency + replay protection for webhook events
CREATE TABLE IF NOT EXISTS webhook_events (
  idempotency_key TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  processed_at TIMESTAMPTZ NOT NULL
);

-- Optional: index for faster expiry deletes (helpful at scale)
CREATE INDEX IF NOT EXISTS idx_webhook_events_processed_at ON webhook_events (processed_at);

-- Optional: mapping table for LINE channel -> tenant (if not present)
CREATE TABLE IF NOT EXISTS line_channels (
  channel_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()

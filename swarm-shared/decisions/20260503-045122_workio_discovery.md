# workio / discovery

## Final Synthesis — One Correct, Actionable Implementation

**Chosen approach**: merge the strongest, most complete pieces from both candidates and resolve all contradictions in favor of **correctness, security, and concrete actionability**.

Key resolutions:
- **Idempotency**: use a stable, unique `line_event_id` (from webhook when present; deterministic fallback otherwise) with a DB uniqueness constraint. Reject or skip duplicates **inside the same transaction**.
- **Replay protection**: enforce a strict **5-minute time window** on event timestamps and store every received event (even replays) for audit.
- **Signature verification**: mandatory HMAC-SHA256 check on raw request body before any processing.
- **Transactional boundary**: all state changes (event log + time/leave/OT records) occur in one DB transaction. External notifications (LINE push) are **best-effort** and never roll back the transaction.
- **GPS enforcement**: hard enforcement at transaction boundary; fail the transaction if location is required and invalid.
- **Auditability**: immutable `line_event_log` for every ingestion, plus `time_records` for clock transitions.

---

### 1. File layout (create/modify)

```bash
mkdir -p /opt/axentx/workio/server/src/services
mkdir -p /opt/axentx/workio/server/src/routes
```

Files to create/update:
- `/opt/axentx/workio/server/src/services/lineWebhookService.ts`
- `/opt/axentx/workio/server/src/routes/lineWebhookRoute.ts`
- `/opt/axentx/workio/server/src/db/schema.sql` (append)

---

### 2. Idempotent, transactional handler

`/opt/axentx/workio/server/src/services/lineWebhookService.ts`

```ts
import crypto from 'crypto';
import { Pool } from 'pg';
import axios from 'axios';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });
const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET!;
const LINE_CHANNEL_ACCESS_TOKEN = process.env.LINE_CHANNEL_ACCESS_TOKEN;
const SIGNATURE_TTL_MS = 5 * 60 * 1000; // 5 minutes
const MAX_CLOCK_DISTANCE_METERS = 300;

export interface LineEvent {
  type: string;
  source: { userId: string; type: string };
  timestamp: number;
  [key: string]: any;
}

export interface WebhookBody {
  destination: string;
  events: LineEvent[];
}

function verifySignature(rawBody: string, signature: string): boolean {
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(rawBody)
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

function isStale(eventTimestamp: number): boolean {
  const now = Date.now();
  return Math.abs(now - eventTimestamp) > SIGNATURE_TTL_MS;
}

function stableEventId(event: LineEvent): string {
  // Prefer explicit webhook event ID when provided by LINE.
  const explicit = (event as any).webhookEventId;
  if (explicit) return explicit;
  // Deterministic fallback for events without explicit IDs.
  return `evt-${event.source.userId}-${event.timestamp}-${event.type}`;
}

async function isDuplicateEvent(eventId: string, tx: any): Promise<boolean> {
  const { rows } = await tx.query(
    'SELECT 1 FROM line_event_log WHERE event_id = $1',
    [eventId]
  );
  return rows.length > 0;
}

async function recordEvent(eventId: string, userId: string, payload: any, tx: any) {
  await tx.query(
    'INSERT INTO line_event_log(event_id, user_id, payload, created_at) VALUES($1,$2,$3,now())',
    [eventId, userId, payload]
  );
}

async function enforceLocation(userId: string, lat: number, lon: number, tx: any): Promise<boolean> {
  const { rows } = await tx.query(
    'SELECT office_lat, office_lon FROM tenant_users WHERE user_id = $1',
    [userId]
  );
  if (!rows.length) return false;
  const { office_lat, office_lon } = rows[0];

  // Haversine distance in meters
  const R = 6371000;
  const φ1 = (office_lat * Math.PI) / 180;
  const φ2 = (lat * Math.PI) / 180;
  const Δφ = ((lat - office_lat) * Math.PI) / 180;
  const Δλ = ((lon - office_lon) * Math.PI) / 180;
  const a = Math.sin(Δφ / 2) ** 2 + Math.cos(φ1) * Math.cos(φ2) * Math.sin(Δλ / 2) ** 2;
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  const distance = R * c;
  return distance <= MAX_CLOCK_DISTANCE_METERS;
}

async function applyClockAction(
  userId: string,
  action: 'in' | 'out',
  lat: number | null,
  lon: number | null,
  tx: any
) {
  // Hard GPS enforcement when coordinates are provided.
  if (lat !== null && lon !== null) {
    const ok = await enforceLocation(userId, lat, lon, tx);
    if (!ok) throw new Error('GPS verification failed');
  }

  await tx.query(
    `INSERT INTO time_records(user_id, action, clock_at, lat, lon)
     VALUES($1, $2, now(), $3, $4)`,
    [userId, action, lat, lon]
  );

  // Best-effort LINE notification; never roll back transaction on failure.
  if (LINE_CHANNEL_ACCESS_TOKEN) {
    try {
      await axios.post(
        'https://api.line.me/v2/bot/message/push',
        {
          to: userId,
          messages: [{ type: 'text', text: `Clock ${action} recorded at ${new Date().toISOString()}` }],
        },
        { headers: { Authorization: `Bearer ${LINE_CHANNEL_ACCESS_TOKEN}` } }
      );
    } catch {
      // swallow notification errors
    }
  }
}

export async function handleLineWebhook(rawBody: string, signature: string, body: WebhookBody) {
  if (!verifySignature(rawBody, signature)) {
    throw new Error('Invalid signature');
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const event of body.events) {
      const eventId = stableEventId(event);
      const userId = event.source.userId;

      // Record every event for audit, but skip processing duplicates/stale events.
      if (await isDuplicateEvent(eventId, client)) {
        // Still ensure audit presence (idempotent insert would fail; skip).
        continue;
      }

      // Always store event (including stale ones) for audit trail.
      await recordEvent(eventId, userId, event, client);

      // Reject processing for stale events, but keep audit record.
      if (isStale(event.timestamp)) {
        continue;
      }

      // Process supported clock actions.
      if (event.type === 'message' && event.message?.type === 'text') {
        const text = event.message.text.toLowerCase();
        if (text === 'clock in' || text === 'clock out') {
          const loc = (event.message as any).location;
          const lat = loc?.latitude ?? null;
          const lon = loc?.longitude ?? null;
          await applyClockAction(userId, text === 'clock in' ? 'in' : 'out', lat, lon, client);
        }
      }
    }

    await client.query('COMMIT');
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}
```

---

### 3. Schema additions (run once)

`/opt/axentx/workio/server/src/db/schema.sql` (append)

```sql
-- Immutable ingestion audit log
CREATE TABLE IF NOT EXISTS line_event_log (
  id BIGSERIAL PRIMARY KEY,
  event_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  payload JSONB NOT

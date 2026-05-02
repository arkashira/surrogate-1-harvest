# workio / discovery

## Final Synthesized Solution

### Diagnosis (merged, de-duplicated)
- **No idempotency key** on punch records allows LINE webhook redeliveries and concurrent deliveries to create duplicate clock‑in/out rows.
- **Non‑atomic read‑then‑insert** for punch state permits races that can produce two active punches for the same `user_id`/`date`.
- **Missing DB constraints**: no unique index to enforce at most one active punch per user per day, and no uniqueness on delivery identifiers.
- **No webhook deduplication**: retries from LINE (timeouts, 5xx) and concurrent deliveries are not detected or suppressed.
- **No audit trail** for webhook deliveries makes distinguishing duplicates from legitimate punches difficult.

---

### Required Changes

#### 1) Database schema (run once)
```sql
-- Add stable delivery identifier
ALTER TABLE punches
  ADD COLUMN line_delivery_id TEXT NULL;

-- One delivery → one punch (idempotency)
CREATE UNIQUE INDEX idx_punches_line_delivery_id
  ON punches(line_delivery_id)
  WHERE line_delivery_id IS NOT NULL;

-- At most one active punch per user per tenant per day
CREATE UNIQUE INDEX idx_punches_active_per_user
  ON punches(user_id, tenant_id, date)
  WHERE clock_out_at IS NULL;
```

#### 2) Express middleware (preserve raw body for LINE signature)
```ts
// server/src/index.ts
import express from 'express';
import lineWebhook from './routes/line/webhook';

const app = express();

app.use(
  '/webhook/line',
  express.raw({ type: 'application/json' }),
  (req, res, next) => {
    try {
      req.body = JSON.parse(req.body.toString());
    } catch {
      req.body = {};
    }
    next();
  },
  lineWebhook
);
```

#### 3) Idempotent, atomic webhook handler
```ts
// server/src/routes/line/webhook.ts
import { Request, Response } from 'express';
import crypto from 'crypto';
import { db } from '../../db';

function verifySignature(rawBody: Buffer, signature: string, channelSecret: string): boolean {
  const hash = crypto
    .createHmac('sha256', channelSecret)
    .update(rawBody)
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(hash), Buffer.from(signature));
}

export async function lineWebhook(req: Request, res: Response) {
  const channelSecret = process.env.LINE_CHANNEL_SECRET!;
  const signature = req.headers['x-line-signature'] as string;
  const rawBody = Buffer.from(JSON.stringify(req.body));

  if (!verifySignature(rawBody, signature, channelSecret)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  // Stable idempotency key derived from payload
  const deliveryId = `line:${crypto
    .createHash('sha256')
    .update(rawBody)
    .digest('hex')
    .slice(0, 24)}`;

  // Fast idempotency check
  const existing = await db
    .selectFrom('punches')
    .select('id')
    .where('line_delivery_id', '=', deliveryId)
    .executeTakeFirst();

  if (existing) {
    return res.status(200).json({ ok: true, reason: 'duplicate_delivery' });
  }

  const events = req.body.events || [];
  const now = new Date();
  const today = now.toISOString().slice(0, 10);
  const tenantId = process.env.DEFAULT_TENANT_ID;

  for (const ev of events) {
    if (ev?.type !== 'message' || ev.message?.type !== 'text') continue;

    const text = ev.message.text.trim().toLowerCase();
    const userId = ev.source.userId;
    const isClockIn = text === 'clock in' || text === 'เข้างาน';
    const isClockOut = text === 'clock out' || text === 'เลิกงาน';
    if (!isClockIn && !isClockOut) continue;

    try {
      await db.transaction().execute(async (trx) => {
        // Idempotency: ensure delivery not processed within this transaction
        const delivered = await trx
          .selectFrom('punches')
          .select('id')
          .where('line_delivery_id', '=', deliveryId)
          .executeTakeFirst();
        if (delivered) return;

        if (isClockIn) {
          // Try insert; unique active-punch index prevents double clock-in
          await trx
            .insertInto('punches')
            .values({
              user_id: userId,
              tenant_id: tenantId,
              date: today,
              clock_in_at: now,
              clock_out_at: null,
              line_delivery_id: deliveryId,
              created_at: now,
              updated_at: now,
            })
            .execute();
        } else {
          // Try to close existing active punch atomically
          const updated = await trx
            .updateTable('punches')
            .set({ clock_out_at: now, updated_at: now })
            .where('user_id', '=', userId)
            .where('tenant_id', '=', tenantId)
            .where('date', '=', today)
            .where('clock_out_at', 'is', null)
            .execute();

          // If no active punch, create a closed punch (best-effort per business rules)
          if ((updated as any).rowCount === 0) {
            await trx
              .insertInto('punches')
              .values({
                user_id: userId,
                tenant_id: tenantId,
                date: today,
                clock_in_at: now,
                clock_out_at: now,
                line_delivery_id: deliveryId,
                created_at: now,
                updated_at: now,
              })
              .execute();
          }
        }
      });
    } catch (err: any) {
      // Unique violation on delivery or active punch → treat as duplicate
      if (err?.code === '23505') continue;
      console.error('Punch processing error', { userId, text, err });
      // Do not fail entire webhook for one bad record
    }
  }

  return res.status(200).json({ ok: true });
}
```

---

### Verification (concrete steps)

1. Apply the SQL migration to dev.
2. Start backend (`npm run dev` in `server/`).
3. Send identical payload twice:
   ```bash
   BODY='{"events":[{"type":"message","message":{"type":"text","text":"clock in"},"source":{"userId":"U12345"}}]}'
   SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$LINE_CHANNEL_SECRET" -binary | base64)
   curl -X POST http://localhost:3000/webhook/line \
     -H "Content-Type: application/json" \
     -H "X-Line-Signature: $SIG" \
     --data "$BODY"
   curl -X POST http://localhost:3000/webhook/line \
     -H "Content-Type: application/json" \
     -H "X-Line-Signature: $SIG" \
     --data "$BODY"
   ```
   - First: creates one punch row.  
   - Second: returns `200` with `reason: duplicate_delivery`; no new row.
4. Concurrent race test:
   ```bash
   seq 10 | xargs -n1 -P10 -I{} curl -s -X POST http://localhost:3000/webhook/line \
     -H "Content-Type: application/json" \
     -H "X-Line-Signature: $SIG" \
     --data "$BODY"
   ```
   - Verify only one punch row exists for `U12345` today.
5. Clock‑out behavior:
   - Send `clock in`, verify active punch.
   - Send `clock out`, verify `clock_out_at` set and no second active punch.
   - Send `clock out` again, verify idempotency (no extra rows).

---

### Key Design Decisions
- **Idempotency key**: derived from `X-Line-Signature` and payload hash; stored in `line_delivery_id` with a unique index.
- **Atomicity

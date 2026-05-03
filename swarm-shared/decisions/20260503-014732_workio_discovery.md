# workio / discovery

Candidate 3:
## 1. Diagnosis

- **No idempotency key storage** — LINE webhook `X-Line-Signature` is verified but not persisted; retries with same signature still produce duplicate punch rows.
- **Non-atomic upsert path** — current flow does `findOne → if exists update else insert`; concurrent/retried webhooks can create duplicate or conflicting punch records.
- **No request deduplication layer** — each webhook is processed independently; no short-term cache or unique constraint to block duplicates within the retry window (LINE retries for up to several hours).
- **Missing unique constraint on punch records** — schema allows multiple `(user_id, date, punch_type)` at same timestamp or overlapping times, making duplicates hard to detect reliably.
- **No idempotency table** — no lightweight table to store processed webhook signatures with TTL; simplest and safest fix for at-least-once delivery.

## 2. Proposed change

- **File scope**: `workio/server/src/controllers/lineWebhook.ts` (or equivalent) + `workio/server/src/db/schema.sql` + `workio/server/src/middleware/idempotency.ts`
- **Change**: Add an `idempotency_keys` table (key_hash PK, created_at), enforce uniqueness at DB level, and wrap webhook processing in an atomic upsert that checks/stores the signature before creating punches. Use `ON CONFLICT DO NOTHING` for punch creation keyed by `(user_id, date, punch_type, ts)` to make punch creation idempotent.

## 3. Implementation

### 3.1 DB schema (add idempotency table + punch uniqueness)

```sql
-- workio/server/src/db/schema.sql
-- Add idempotency keys table
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key_hash TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Optional: index for cleanup (older than 48h)
-- CREATE INDEX idx_idempotency_created ON idempotency_keys (created_at);

-- Make punches idempotent: unique constraint per user/date/punch_type/ts
-- Adjust table name/columns to your actual schema (example: punches)
ALTER TABLE punches
  ADD CONSTRAINT uniq_user_date_punch_ts
  UNIQUE (user_id, date, punch_type, ts);
```

### 3.2 Idempotency middleware

```ts
// workio/server/src/middleware/idempotency.ts
import { Request, Response, NextFunction } from 'express';
import { db } from '../db';
import crypto from 'crypto';

export async function withIdempotency(
  req: Request,
  res: Response,
  next: NextFunction
) {
  const sig = req.get('X-Line-Signature');
  if (!sig) return res.status(400).json({ error: 'Missing signature' });

  // Deterministic hash of signature + normalized body (LINE sends JSON text)
  const body = typeof req.body === 'string' ? req.body : JSON.stringify(req.body);
  const keyHash = crypto.createHash('sha256').update(sig + body).digest('hex');

  try {
    // Atomic insert; if already exists, skip processing
    const { rows } = await db.query(
      `INSERT INTO idempotency_keys (key_hash) VALUES ($1) ON CONFLICT (key_hash) DO NOTHING RETURNING key_hash`,
      [keyHash]
    );

    if (rows.length === 0) {
      // Duplicate request — acknowledge but skip business logic
      return res.status(200).json({ ok: true, duplicate: true });
    }

    // Attach keyHash for logging/cleanup and proceed
    (req as any).idempotencyKey = keyHash;
    next();
  } catch (err) {
    console.error('Idempotency check failed', err);
    // Fail open to avoid dropping legitimate webhooks; continue processing
    next();
  }
}
```

### 3.3 Apply middleware + atomic punch upsert in webhook handler

```ts
// workio/server/src/controllers/lineWebhook.ts
import { Router } from 'express';
import { verifySignature } from '../utils/lineSignature';
import { withIdempotency } from '../middleware/idempotency';
import { db } from '../db';

const router = Router();

// Use idempotency middleware before business logic
router.post('/webhook/line', withIdempotency, async (req, res) => {
  const sig = req.get('X-Line-Signature');
  const body = req.body;

  if (!verifySignature(JSON.stringify(body), sig || '', process.env.LINE_CHANNEL_SECRET!)) {
    return res.status(401).json({ error: 'Invalid signature' });
  }

  try {
    // Example: handle clock-in/out events
    for (const event of body.events || []) {
      if (event.type !== 'message' && event.type !== 'postback') continue;

      const userId = event.source.userId;
      const ts = event.timestamp;
      const date = new Date(ts).toISOString().split('T')[0];
      const punchType = extractPunchType(event); // 'clock_in' | 'clock_out' etc.

      // Atomic upsert: try insert; if unique violation, do nothing (idempotent)
      await db.query(
        `INSERT INTO punches (user_id, date, punch_type, ts, location, metadata)
         VALUES ($1, $2, $3, $4, $5, $6)
         ON CONFLICT (user_id, date, punch_type, ts) DO NOTHING`,
        [userId, date, punchType, new Date(ts), extractLocation(event), event]
      );
    }

    res.status(200).json({ ok: true });
  } catch (err) {
    console.error('Webhook processing failed', err);
    res.status(500).json({ error: 'Processing failed' });
  }
});

function extractPunchType(event: any): string {
  // Implement based on your postback/message payload
  return event.postback?.data?.includes('clock_out') ? 'clock_out' : 'clock_in';
}

function extractLocation(event: any): string | null {
  return event.message?.location?.address || null;
}

export default router;
```

### 3.4 Cleanup job (optional, run daily)

```sql
-- Delete idempotency keys older than 48 hours
DELETE FROM idempotency_keys WHERE created_at < NOW() - INTERVAL '48 hours';
```

## 4. Verification

1. **Schema check** — run migrations and confirm `idempotency_keys` exists and `punches` has the unique constraint:
   ```bash
   psql workio -c "\d punches"
   psql workio -c "\d idempotency_keys"
   ```

2. **Duplicate webhook test** — send the same LINE webhook payload twice (same `X-Line-Signature` and body):
   - First request: returns `200` and creates a punch row.
   - Second request: middleware returns `200` with `{ duplicate: true }` and no new punch row.

3. **Concurrent request test** — fire two identical requests in parallel (e.g., `curl -X POST ...` in two terminals at same time):
   - Only one punch row should be inserted (unique constraint prevents duplicates).

4. **Log check** — verify `idempotency_keys` contains one entry per unique webhook and duplicates are skipped.

5. **Cleanup check** — run the DELETE query manually and confirm old keys are removed without affecting recent duplicates detection.


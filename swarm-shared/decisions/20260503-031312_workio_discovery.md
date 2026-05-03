# workio / discovery

## Final consolidated solution (correct + actionable)

**Core problems (both candidates agree)**
- No `X-Line-Signature` verification → spoofable webhook.
- No idempotency/replay protection → LINE retries and replayed events create duplicate rows.
- Missing tenant/identity integrity and auditability.

**Chosen approach**
- Verify signatures with constant-time comparison.
- Enforce idempotency with a **deterministic, bounded `line_event_id`** stored in each domain table, protected by **unique constraints**.
- Use **DB-level unique indexes** as the source of truth for idempotency (safe under concurrency and retries).
- Treat unique-constraint violations as already-processed (return 200) to suppress LINE retry storms.
- Keep raw body only for verification; parse once and use structured payload afterward.
- Add minimal observability (structured logs for verification/idempotency outcomes).

---

## 1. Migration (run once)

File: `/opt/axentx/workio/server/src/db/migrations/20260503_line_event_id_unique.sql`
```sql
-- Prevent duplicate LINE events across domain tables.
-- Use partial indexes so rows without line_event_id are unaffected.
ALTER TABLE clock_events
  ADD CONSTRAINT uq_clock_line_event_id UNIQUE (line_event_id)
  WHERE line_event_id IS NOT NULL;

ALTER TABLE leave_requests
  ADD CONSTRAINT uq_leave_line_event_id UNIQUE (line_event_id)
  WHERE line_event_id IS NOT NULL;

ALTER TABLE ot_requests
  ADD CONSTRAINT uq_ot_line_event_id UNIQUE (line_event_id)
  WHERE line_event_id IS NOT NULL;
```

Apply:
```bash
cd /opt/axentx/workio/server
psql workio < src/db/migrations/20260503_line_event_id_unique.sql
```

---

## 2. Signature verification utility

File: `/opt/axentx/workio/server/src/utils/line-signature.ts`
```ts
import crypto from 'crypto';

export function verifyLineSignature(
  rawBody: string,
  signature: string,
  channelSecret: string
): boolean {
  if (!signature || !channelSecret) return false;
  const expected = crypto
    .createHmac('sha256', channelSecret)
    .update(rawBody, 'utf8')
    .digest('base64');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}
```

---

## 3. Webhook route (single source of truth)

File: `/opt/axentx/workio/server/src/routes/webhook/line.ts`
```ts
import { Router, Request, Response } from 'express';
import crypto from 'crypto';
import { pool } from '../../db';
import { verifyLineSignature } from '../../utils/line-signature';

const router = Router();

// Do NOT use global body-parser.json() for this route.
// Keep raw body for signature verification.
router.use('/line', (req, res, next) => {
  if (req.method === 'POST') {
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

router.post('/line', async (req: Request & { rawBody?: string }, res: Response) => {
  const signature = (req.headers['x-line-signature'] as string) || '';
  const rawBody = req.rawBody || '';
  const channelSecret = process.env.LINE_CHANNEL_SECRET || '';

  if (!channelSecret) {
    console.error('[webhook:line] Missing LINE_CHANNEL_SECRET');
    return res.status(500).json({ error: 'Server configuration error' });
  }

  if (!verifyLineSignature(rawBody, signature, channelSecret)) {
    console.warn('[webhook:line] Invalid signature', { hasSig: !!signature });
    return res.status(401).json({ error: 'Invalid signature' });
  }

  const events = req.body.events;
  if (!Array.isArray(events) || events.length === 0) {
    return res.status(200).json({ ok: true });
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    for (const ev of events) {
      // Deterministic, bounded id derived from LINE payload.
      // Uses userId + timestamp + type to avoid storing full JSON in index.
      const lineEventId = crypto
        .createHash('sha256')
        .update(`${ev.source?.userId || ''}:${ev.timestamp || ''}:${ev.type || ''}`)
        .digest('hex');

      // Idempotency check across domain tables
      const exists = await client.query(
        `SELECT 1 FROM clock_events WHERE line_event_id = $1
         UNION
         SELECT 1 FROM leave_requests WHERE line_event_id = $1
         UNION
         SELECT 1 FROM ot_requests WHERE line_event_id = $1
         LIMIT 1`,
        [lineEventId]
      );

      if (exists.rows.length > 0) {
        continue;
      }

      const userId = ev.source?.userId;
      if (!userId) {
        // Record as unrecognized to prevent replays
        await client.query(
          `INSERT INTO clock_events (employee_id, line_event_id, event_type, created_at, metadata)
           VALUES (NULL, $1, 'unknown_user', NOW(), $2)`,
          [lineEventId, JSON.stringify(ev)]
        );
        continue;
      }

      const emp = await client.query(
        `SELECT id, tenant_id FROM employees WHERE line_user_id = $1 LIMIT 1`,
        [userId]
      );

      if (!emp.rows[0]) {
        await client.query(
          `INSERT INTO clock_events (employee_id, line_event_id, event_type, created_at, metadata)
           VALUES (NULL, $1, 'unknown_user', NOW(), $2)`,
          [lineEventId, JSON.stringify(ev)]
        );
        continue;
      }

      const employeeId = emp.rows[0].id;

      if (ev.type === 'message' && ev.message?.type === 'text') {
        const text = (ev.message.text || '').trim().toLowerCase();

        if (text === 'เข้างาน' || text === 'clock in') {
          await client.query(
            `INSERT INTO clock_events (employee_id, line_event_id, event_type, clock_in_at, created_at, metadata)
             VALUES ($1, $2, 'clock_in', NOW(), NOW(), $3)`,
            [employeeId, lineEventId, JSON.stringify(ev)]
          );
        } else if (text === 'ออกงาน' || text === 'clock out') {
          await client.query(
            `INSERT INTO clock_events (employee_id, line_event_id, event_type, clock_out_at, created_at, metadata)
             VALUES ($1, $2, 'clock_out', NOW(), NOW(), $3)`,
            [employeeId, lineEventId, JSON.stringify(ev)]
          );
        } else if (text.includes('ลา') || text.includes('leave')) {
          await client.query(
            `INSERT INTO leave_requests (employee_id, line_event_id, status, requested_at, created_at, metadata)
             VALUES ($1, $2, 'pending', NOW(), NOW(), $3)`,
            [employeeId, lineEventId, JSON.stringify(ev)]
          );
        } else if (text.includes('ot') || text.includes('โอที')) {
          await client.query(
            `INSERT INTO ot_requests (employee_id, line_event_id, status, requested_at, created_at, metadata)
             VALUES ($1, $2, 'pending', NOW(), NOW(), $3)`,
            [employeeId, lineEventId, JSON.stringify(ev)]
          );
        }
      }
    }

    await client.query('COMMIT');
    return res.status(200).json({ ok: true });
  } catch (err: any) {
    await client.query('ROLLBACK');
    // Unique violation -> already processed; return 200 to stop LINE retries
    if (err.code === '23505') {
      console.info('[webhook:line] Duplicate event (idempotent)', { code: err.code });
     

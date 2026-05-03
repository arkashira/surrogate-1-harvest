# workio / discovery

## 1. Diagnosis

- No idempotency key on LINE webhook ingestion → duplicate clock-in/out or leave/OT records when LINE retries or users double-tap.
- Missing request signature verification (`X-Line-Signature`) → webhook accepts spoofed events, enabling attendance fraud.
- No per-tenant/per-day/per-employee intent guard at write path → race conditions create multiple clock-in rows for same period.
- Clock-in/out state derived only from latest row → incorrect state if duplicates exist or corrections are made.
- No audit trail for critical attendance changes → untraceable edits and no rollback capability.

## 2. Proposed change

File: `/opt/axentx/workio/server/src/routes/lineWebhook.ts` (create if absent)  
Scope: add middleware + handler that verifies `X-Line-Signature`, deduplicates by `X-Line-Delivery` (or hash of body+timestamp), and enforces unique intent guard (`tenantId, employeeId, date, intentType`) before insert.

## 3. Implementation

```bash
# Ensure file exists and is ready for edit
touch /opt/axentx/workio/server/src/routes/lineWebhook.ts
```

```typescript
// /opt/axentx/workio/server/src/routes/lineWebhook.ts
import { Router, Request, Response, NextFunction } from 'express';
import crypto from 'crypto';
import { db } from '../db';
import { attendance, attendanceAudit } from '../db/schema';
import { eq, and, gt } from 'drizzle-orm';
import type { LineWebhookEvent, LineMessage, LineSource } from '../types/line';

const router = Router();
const LINE_CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET || '';

// -- Middleware: verify signature --
function verifyLineSignature(req: Request, res: Response, next: NextFunction) {
  const signature = req.get('X-Line-Signature') || '';
  const body = JSON.stringify(req.body);
  const expected = crypto
    .createHmac('sha256', LINE_CHANNEL_SECRET)
    .update(body)
    .digest('base64');

  if (!crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected))) {
    return res.status(401).json({ error: 'Invalid signature' });
  }
  next();
}

// -- Deduplication: idempotency by delivery id or content hash --
function buildIdempotencyKey(body: any): string {
  // Prefer delivery header; fallback to stable hash of body
  const delivery = (req as any).get('X-Line-Delivery') || '';
  if (delivery) return `delivery:${delivery}`;
  const hash = crypto.createHash('sha256').update(JSON.stringify(body)).digest('hex');
  return `hash:${hash}`;
}

const seenKeys = new Set<string>(); // in-mem short window; replace with Redis/DB for prod

function dedupe(key: string): boolean {
  if (seenKeys.has(key)) return true;
  seenKeys.add(key);
  setTimeout(() => seenKeys.delete(key), 5 * 60 * 1000); // 5m window
  return false;
}

// -- Helpers --
function getEmployeeIdByLineUserId(lineUserId: string, tenantId: number) {
  // TODO: map line_user_id -> employee_id (employees table)
  // For discovery, assume resolved via middleware or lookup table
  return 1; // placeholder
}

async function getTenantIdByChannel(channelId: string): Promise<number> {
  // TODO: resolve tenant by channel
  return 1; // placeholder
}

// -- Core handler --
router.post('/webhook/line', verifyLineSignature, async (req: Request, res: Response) => {
  try {
    const body = req.body as { events: LineWebhookEvent[] };
    const key = buildIdempotencyKey(body);
    if (dedupe(key)) {
      return res.status(200).json({ message: 'duplicate ignored' });
    }

    for (const event of body.events) {
      if (event.type !== 'message' || event.message.type !== 'text') continue;

      const tenantId = await getTenantIdByChannel(event.source.userId); // simplified
      const employeeId = getEmployeeIdByLineUserId(event.source.userId, tenantId);
      const text = event.message.text.trim().toLowerCase();
      const now = new Date();
      const today = now.toISOString().split('T')[0];

      // Intent normalization
      let intent: 'clock_in' | 'clock_out' | 'leave_request' | 'ot_request' | null = null;
      if (text.includes('เข้า') || text.includes('clock in') || text.includes('in')) intent = 'clock_in';
      else if (text.includes('ออก') || text.includes('clock out') || text.includes('out')) intent = 'clock_out';
      else if (text.includes('ลา') || text.includes('leave')) intent = 'leave_request';
      else if (text.includes('โอที') || text.includes('ot')) intent = 'ot_request';

      if (!intent) continue;

      // Intent guard: at most one "active" clock_in per tenant/employee/day
      if (intent === 'clock_in' || intent === 'clock_out') {
        const existing = await db
          .select()
          .from(attendance)
          .where(
            and(
              eq(attendance.tenantId, tenantId),
              eq(attendance.employeeId, employeeId),
              eq(attendance.date, today)
            )
          )
          .orderBy(attendance.createdAt)
          .limit(1);

        const latest = existing[0];
        if (intent === 'clock_in' && latest && latest.clockOutAt === null) {
          // Already clocked in — ignore duplicate
          continue;
        }
        if (intent === 'clock_out' && (!latest || latest.clockOutAt !== null)) {
          // No active clock-in to close — ignore
          continue;
        }
      }

      // Insert attendance row
      const [record] = await db
        .insert(attendance)
        .values({
          tenantId,
          employeeId,
          date: today,
          clockInAt: intent === 'clock_in' ? now : null,
          clockOutAt: intent === 'clock_out' ? now : null,
          leaveType: intent === 'leave_request' ? 'pending' : null,
          otRequested: intent === 'ot_request',
          lineEventId: event.message.id,
          rawEvent: event as any,
        })
        .returning();

      // Audit trail
      await db.insert(attendanceAudit).values({
        attendanceId: record.id,
        tenantId,
        employeeId,
        action: 'create',
        changes: JSON.stringify({ intent, record }),
        changedBy: 'line_system',
        changedAt: now,
      });
    }

    res.status(200).json({ message: 'ok' });
  } catch (error) {
    console.error('LINE webhook error:', error);
    res.status(500).json({ error: 'internal' });
  }
});

export default router;
```

Add route registration in your main server file (e.g., `/opt/axentx/workio/server/src/index.ts` or app setup):

```typescript
import lineWebhook from './routes/lineWebhook';
app.use('/webhook/line', lineWebhook);
```

Schema additions (if not present) — run once:

```sql
-- attendance table (ensure these columns exist)
-- id, tenant_id, employee_id, date, clock_in_at, clock_out_at, leave_type, ot_requested, line_event_id, raw_event, created_at

-- audit table
CREATE TABLE IF NOT EXISTS attendance_audit (
  id SERIAL PRIMARY KEY,
  attendance_id INTEGER NOT NULL,
  tenant_id INTEGER NOT NULL,
  employee_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  changes JSONB NOT NULL,
  changed_by TEXT NOT NULL,
  changed_at TIMESTAMPTZ NOT NULL
);
```

## 4. Verification

1. **Signature rejection**  
   Send a POST to `/webhook/line` with a wrong/missing `X-Line-Signature` → expect 401.

2. **Idempotency**  
   Send the same valid payload twice within 5 minutes (same body or same `X-Line-Delivery`) → second request returns `200

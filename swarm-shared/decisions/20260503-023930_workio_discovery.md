# workio / discovery

## 1. Diagnosis

- No idempotency key on attendance punches → LINE webhook retries create duplicate clock-in/out records and illegal state transitions (e.g., double clock-in without intervening clock-out).
- Clock state transitions are enforced only in app logic, not at DB level → race conditions under concurrency can corrupt daily attendance (e.g., two webhook deliveries both think “no active punch” and insert).
- No server-side geofence validation on punch → GPS can be spoofed or omitted; distance/accuracy checks live only in frontend.
- Missing request deduplication for leave/OT submission → retry storms from mobile clients can create duplicate requests.
- No audit trail for critical state changes (punch, leave, OT) → hard to debug duplicates or tampering after the fact.

## 2. Proposed change

File: `/opt/axentx/workio/server/src/services/attendanceService.ts` (or create if absent)  
Scope: add idempotent punch handler with DB unique constraint, geofence gate, and audit insert.  
Secondary: `/opt/axentx/workio/server/src/db/schema.sql` — add constraint + audit table.  
Interface: expose `createPunch({ employeeId, type, latitude, longitude, accuracy, timestamp, idempotencyKey })`.

## 3. Implementation

### 3.1 DB changes (`server/src/db/schema.sql`)

```sql
-- Idempotency key per employee+date+type to prevent duplicates from retries
ALTER TABLE attendance_punches
  ADD COLUMN idempotency_key VARCHAR(128),
  ADD COLUMN location_accuracy_m NUMERIC(10,2),
  ADD COLUMN created_via VARCHAR(32) NOT NULL DEFAULT 'line';

CREATE UNIQUE INDEX uq_attendance_punch_idempotent
  ON attendance_punches (employee_id, punch_date, type, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- Audit trail for critical state changes
CREATE TABLE IF NOT EXISTS attendance_audit (
  id BIGSERIAL PRIMARY KEY,
  employee_id BIGINT NOT NULL,
  entity_type VARCHAR(32) NOT NULL,        -- 'punch', 'leave', 'ot'
  entity_id BIGINT NOT NULL,
  action VARCHAR(32) NOT NULL,             -- 'create', 'update', 'delete'
  old_state JSONB,
  new_state JSONB,
  performed_by BIGINT,                     -- null for system/webhook
  performed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  meta JSONB
);

CREATE INDEX idx_attendance_audit_entity ON attendance_audit (entity_type, entity_id);
CREATE INDEX idx_attendance_audit_emp ON attendance_audit (employee_id);
```

### 3.2 Service: idempotent punch with geofence (`server/src/services/attendanceService.ts`)

```ts
import { Pool } from 'pg';
import { getDistance } from 'geolib';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

// Workplace geofence center + radius (meters)
const WORKPLACE_LAT = parseFloat(process.env.WORKPLACE_LAT || '0');
const WORKPLACE_LON = parseFloat(process.env.WORKPLACE_LON || '0');
const MAX_ALLOWED_ACCURACY = 100; // meters
const MAX_ALLOWED_DISTANCE = 300; // meters from center

export type PunchType = 'clock_in' | 'clock_out';

export async function createPunch(params: {
  employeeId: number;
  type: PunchType;
  latitude: number;
  longitude: number;
  accuracy?: number;
  timestamp?: Date;
  idempotencyKey?: string;
  createdVia?: string;
}) {
  const {
    employeeId,
    type,
    latitude,
    longitude,
    accuracy = Infinity,
    timestamp = new Date(),
    idempotencyKey,
    createdVia = 'line',
  } = params;

  // 1) Geofence validation
  if (!isFinite(latitude) || !isFinite(longitude)) {
    throw new Error('Invalid coordinates');
  }
  if (accuracy > MAX_ALLOWED_ACCURACY) {
    throw new Error(`Location accuracy too low: ${accuracy}m`);
  }
  const distance = getDistance(
    { latitude, longitude },
    { latitude: WORKPLACE_LAT, longitude: WORKPLACE_LON }
  );
  if (distance > MAX_ALLOWED_DISTANCE) {
    throw new Error(`Location too far from workplace: ${distance.toFixed(0)}m`);
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // 2) Idempotency check
    if (idempotencyKey) {
      const dup = await client.query(
        `SELECT id FROM attendance_punches
         WHERE employee_id = $1 AND punch_date = $2 AND type = $3 AND idempotency_key = $4`,
        [employeeId, timestamp.toISOString().split('T')[0], type, idempotencyKey]
      );
      if (dup.rows.length > 0) {
        await client.query('ROLLBACK');
        return { id: dup.rows[0].id, duplicate: true };
      }
    }

    // 3) State transition enforcement (basic)
    if (type === 'clock_in') {
      const latest = await client.query(
        `SELECT type FROM attendance_punches
         WHERE employee_id = $1 AND punch_date = $2
         ORDER BY created_at DESC LIMIT 1`,
        [employeeId, timestamp.toISOString().split('T')[0]]
      );
      if (latest.rows.length > 0 && latest.rows[0].type === 'clock_in') {
        throw new Error('Duplicate clock_in without clock_out');
      }
    }

    // 4) Insert punch
    const punchDate = timestamp.toISOString().split('T')[0];
    const insertRes = await client.query(
      `INSERT INTO attendance_punches
         (employee_id, punch_date, type, latitude, longitude, location_accuracy_m,
          idempotency_key, created_via, created_at)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
       RETURNING id`,
      [
        employeeId,
        punchDate,
        type,
        latitude,
        longitude,
        accuracy,
        idempotencyKey,
        createdVia,
        timestamp,
      ]
    );
    const punchId = insertRes.rows[0].id;

    // 5) Audit
    await client.query(
      `INSERT INTO attendance_audit
         (employee_id, entity_type, entity_id, action, new_state, performed_by, meta)
       VALUES ($1,$2,$3,$4,$5,$6,$7)`,
      [
        employeeId,
        'punch',
        punchId,
        'create',
        JSON.stringify({
          type,
          latitude,
          longitude,
          accuracy,
          punchDate,
          idempotencyKey,
        }),
        null,
        JSON.stringify({ createdVia }),
      ]
    );

    await client.query('COMMIT');
    return { id: punchId, duplicate: false };
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}
```

### 3.3 Webhook usage (example)

```ts
// server/src/routes/lineWebhook.ts
import { createPunch } from '../services/attendanceService';

app.post('/webhook/line', async (req, res) => {
  const { employeeId, type, latitude, longitude, accuracy, idempotencyKey } = req.body;
  try {
    const result = await createPunch({
      employeeId,
      type,
      latitude,
      longitude,
      accuracy,
      idempotencyKey: idempotencyKey || `line-${req.body.timestamp}-${employeeId}-${type}`,
      createdVia: 'line',
    });
    res.json({ ok: true, ...result });
  } catch (err: any) {
    res.status(400).json({ ok: false, error: err.message });
  }
});
```

## 4. Verification

1. **Schema + constraint**  
   ```bash
   psql workio -c "\d attendance_punches"
   # confirm idempotency_key column and unique index exist
   psql workio -c

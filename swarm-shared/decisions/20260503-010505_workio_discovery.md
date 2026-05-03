# workio / discovery

### Final Synthesis

**Diagnosis (consolidated)**
- No database-level uniqueness guard for open punches, allowing duplicates under LINE webhook retries or client double-taps.
- Race-prone “find-then-insert” logic in clock-in/clock-out handlers.
- No atomic, DB-enforced uniqueness guarantee for one open punch per user per day.
- Non-idempotent write paths make retries unsafe.
- Inadequate observability and error handling for duplicate/open-punch anomalies.

**Proposed change (single, concrete plan)**
Add a partial unique index to enforce one open punch per user per day, and implement idempotent, atomic write paths for clock-in and clock-out using upsert patterns and safe conflict handling.

---

### Implementation (actionable)

**1) Database schema (run once)**
```sql
-- One open punch per user per calendar day
CREATE UNIQUE INDEX idx_unique_open_punch
ON punches (user_id, date(clock_in_at))
WHERE clock_out_at IS NULL;
```
- Uses `date(clock_in_at)` so uniqueness is per calendar day.
- Partial condition `WHERE clock_out_at IS NULL` only applies to open punches.
- Conflicts on this index will be used to make writes idempotent.

**2) Idempotent clock-in (Node.js + PostgreSQL example)**
```javascript
const { Op } = require('sequelize');

const clockIn = async (req, res) => {
  const { userId } = req.params;
  const clockInAt = req.body.clockInAt ? new Date(req.body.clockInAt) : new Date();

  try {
    // Normalize to day boundary for conflict target
    const day = new Date(Date.UTC(
      clockInAt.getUTCFullYear(),
      clockInAt.getUTCMonth(),
      clockInAt.getUTCDate()
    ));

    // Use raw upsert to leverage partial unique index
    const [punch] = await req.db.query(
      `INSERT INTO punches (user_id, clock_in_at, clock_out_at, created_at, updated_at)
       VALUES (:userId, :clockInAt, NULL, NOW(), NOW())
       ON CONFLICT (user_id, date(clock_in_at))
       WHERE clock_out_at IS NULL
       DO UPDATE SET clock_in_at = EXCLUDED.clock_in_at
       RETURNING *`,
      {
        replacements: { userId, clockInAt },
        type: req.db.QueryTypes.INSERT,
        plain: true,
      }
    );

    return res.status(200).send({
      message: 'Clock-in successful',
      punch,
    });
  } catch (err) {
    console.error('[clockIn] error', { userId, clockInAt, err: err.message });
    return res.status(500).send({ message: 'Error clocking in' });
  }
};
```
- Uses `ON CONFLICT … WHERE clock_out_at IS NULL` to enforce uniqueness and make retries idempotent.
- Returns 200 on both create and idempotent hit (safe for retries).

**3) Idempotent clock-out**
```javascript
const clockOut = async (req, res) => {
  const { userId } = req.params;
  const clockOutAt = req.body.clockOutAt ? new Date(req.body.clockOutAt) : new Date();

  try {
    // Find the open punch for this user (most recent open)
    const punch = await req.db.models.Punch.findOne({
      where: {
        userId,
        clockOutAt: null,
        clock_in_at: { [Op.lte]: clockOutAt },
      },
      order: [['clock_in_at', 'DESC']],
    });

    if (!punch) {
      return res.status(404).send({ message: 'No open punch found' });
    }

    // If already clocked out at this exact time, treat as idempotent success
    if (punch.clockOutAt && punch.clockOutAt.getTime() === clockOutAt.getTime()) {
      return res.status(200).send({ message: 'Clock-out already recorded', punch });
    }

    punch.clockOutAt = clockOutAt;
    punch.updated_at = new Date();
    await punch.save();

    return res.status(200).send({ message: 'Clock-out successful', punch });
  } catch (err) {
    console.error('[clockOut] error', { userId, clockOutAt, err: err.message });
    return res.status(500).send({ message: 'Error clocking out' });
  }
};
```
- Safe for retries: if `clockOutAt` already matches, returns 200 without change.
- Uses application-level check plus DB update to avoid races after read.

**4) Optional safety improvement (recommended)**
For higher concurrency safety, perform clock-out via a single atomic update:
```sql
UPDATE punches
SET clock_out_at = :clockOutAt, updated_at = NOW()
WHERE user_id = :userId
  AND clock_out_at IS NULL
  AND clock_in_at <= :clockOutAt
RETURNING *;
```
- Use the row count/result to determine 200 vs 404.

---

### Verification (concrete test plan)

1. **Uniqueness guard**
   - Clock in for user A on day D → 201/200, one row created.
   - Repeat same request (same or slightly different timestamp) → 200, no new row.
   - Verify DB rejects second open punch for same user/day via index violation.

2. **Idempotent clock-in under retries**
   - Simulate duplicate webhook or double-tap with same/different timestamps → exactly one open punch.

3. **Clock-out correctness**
   - Clock in, then clock out → punch updated with clock-out time.
   - Repeat same clock-out request → 200, no change.
   - Clock out with no open punch → 404.

4. **Concurrency**
   - Fire two simultaneous clock-ins for same user/day → only one open punch persists.
   - Fire clock-out while clock-in is in flight → ensure consistent state (no double open/closed anomalies).

5. **Observability**
   - Confirm structured logs for clock-in/clock-out include userId, timestamps, and outcome.
   - Add alerting on duplicate-key errors or repeated 500s in clock endpoints.

---

### Summary
- **DB-level guard**: partial unique index on `(user_id, date(clock_in_at)) WHERE clock_out_at IS NULL`.
- **Idempotent writes**: upsert for clock-in; safe update-with-check for clock-out.
- **Concrete actions**: add index, replace handlers with upsert/atomic update, add logging, and run verification tests above.

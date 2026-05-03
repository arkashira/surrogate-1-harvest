# workio / discovery

## Final Synthesized Plan (Correct + Actionable)

**Chosen scope:**  
Implement a **tenant-scoped, privacy-safe discovery surface** that gives employees a usable “who’s here / who’s near” view **today**, while laying groundwork for aggregated, anonymized insights **later** without leaking PII.

We merge Candidate 1’s concrete, implementable route + UI with Candidate 2’s emphasis on retention, rollups, and tenant controls—**but avoid heavy schema changes or risky cross-tenant analytics in v1**.

---

## 1. Diagnosis (resolved)

- ✅ No tenant-scoped discovery surface → **Add `/api/discover/nearby`** (presence + location, tenant-scoped).
- ✅ GPS collected but not reused → **Use latest lat/lng from time_logs only for currently clocked-in users**; never store precise history for discovery.
- ✅ No role-aware directory → **Return role/team in discovery payload** so frontend can group/filter.
- ✅ No lightweight presence indicator → **Derive presence from latest time_log row per user per tenant** (clocked_in boolean + clocked_in_at).
- ✅ No analytics retention → **Retain minimal, anonymized hourly rollups** in new `discover_daily_rollups` table (counts only, no raw GPS per user).
- ✅ No tenant-level toggle → **Add `tenant_settings.enable_discovery_gps` boolean** (default false) to control whether location is used in `/nearby`.
- ✅ No export for RAG/knowledge base → **Add `/api/discover/export` (anonymized, tenant-scoped, CSV)** with rollups only (no PII).

---

## 2. Proposed Changes (v1 + lightweight v2 prep)

### Backend

1. **New route**: `GET /api/discover/nearby`
   - Tenant-scoped, LINE-authenticated.
   - Query params: `lat`, `lng`, `radiusM` (default 1000, max 5000).
   - Returns: `{ users: { id, name, role, team, clockedIn, lat, lng, distanceM }[] }`
   - **Respects tenant setting**: if `enable_discovery_gps = false`, return all clocked-in users without distance filtering or expose lat/lng (set to null).

2. **New rollup table** (no app logic change yet, just storage):
   ```sql
   create table discover_daily_rollups (
     tenant_id   bigint not null,
     day         date   not null,
     hour        int    not null check (hour between 0 and 23),
     clock_ins   int    not null default 0,
     clock_outs  int    not null default 0,
     on_leave    int    not null default 0,
     ot_count    int    not null default 0,
     primary key (tenant_id, day, hour)
   );
   ```
   - Populate via nightly job or trigger from time_logs (counts only). No raw GPS retained.

3. **Insights endpoint** (v2-ready, simple counts):
   - `GET /api/discover/insights`
   - Returns: `{ clockInDensityByHour: {...}, leaveRatio: number, otRatio: number }` (tenant-scoped, no PII).

4. **Export endpoint** (anonymized):
   - `GET /api/discover/export?start=YYYY-MM-DD&end=YYYY-MM-DD`
   - Returns CSV of daily rollups (no user-level data).

5. **Tenant settings column**:
   - `enable_discovery_gps boolean not null default false` in `tenants` table.

### Frontend

1. **New page**: `/discover`
   - Component: `Discover.tsx`
   - Shows list + optional map (if GPS enabled and permitted).
   - Filters: role/team, clocked-in only, distance (if GPS enabled).
   - Presence badges: “Clocked in”, “On leave”, “Off duty”.

2. **Settings toggle** (admin-only):
   - Control `enable_discovery_gps` per tenant.

---

## 3. Implementation (concrete, minimal risk)

### Backend: route + service

File: `workio/server/src/routes/discover.ts`

```ts
import express from 'express';
import { verifyLineToken } from '../middleware/auth';
import { pool } from '../db';

const router = express.Router();

function haversineM(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const R = 6371000;
  const toRad = (x: number) => (x * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return R * c;
}

/**
 * GET /api/discover/nearby
 * Query params:
 *  - lat (optional)
 *  - lng (optional)
 *  - radiusM (optional, default 1000, max 5000)
 */
router.get('/nearby', verifyLineToken, async (req, res) => {
  // @ts-ignore - tenantId set by auth middleware
  const tenantId = req.tenantId;
  const userId = req.user?.id;
  const lat = parseFloat(req.query.lat as string);
  const lng = parseFloat(req.query.lng as string);
  const radiusM = Math.min(parseInt(req.query.radiusM as string) || 1000, 5000);

  if (!tenantId) {
    return res.status(403).json({ error: 'Tenant required' });
  }

  try {
    // Check tenant GPS toggle
    const tenant = await pool.query(
      `SELECT enable_discovery_gps FROM tenants WHERE id = $1`,
      [tenantId]
    );
    const gpsEnabled = tenant.rows[0]?.enable_discovery_gps === true;

    // Latest presence per user in this tenant
    const presence = await pool.query(
      `SELECT user_id, clocked_in, lat, lng
       FROM time_logs
       WHERE tenant_id = $1
         AND (user_id, id) IN (
           SELECT user_id, MAX(id)
           FROM time_logs
           WHERE tenant_id = $1
           GROUP BY user_id
         )`,
      [tenantId]
    );

    const presenceMap = new Map<number, { clocked_in: boolean; lat: number | null; lng: number | null }>();
    presence.rows.forEach((r) => {
      presenceMap.set(r.user_id, {
        clocked_in: !!r.clocked_in,
        lat: gpsEnabled && r.lat ? parseFloat(r.lat) : null,
        lng: gpsEnabled && r.lng ? parseFloat(r.lng) : null,
      });
    });

    // Active users in tenant (excluding requester)
    const users = await pool.query(
      `SELECT id, name, role, team
       FROM users
       WHERE tenant_id = $1 AND id != $2 AND active = true`,
      [tenantId, userId]
    );

    const results = users.rows.map((u) => {
      const p = presenceMap.get(u.id) || { clocked_in: false, lat: null, lng: null };
      let distanceM = null;
      if (p.lat != null && p.lng != null && !isNaN(lat) && !isNaN(lng)) {
        distanceM = haversineM(lat, lng, p.lat, p.lng);
      }
      return {
        id: u.id,
        name: u.name,
        role: u.role,
        team: u.team,
        clockedIn: p.clocked_in,
        lat: p.lat,
        lng: p.lng,
        distanceM,
      };
    }).filter((x) => {
      if (!gpsEnabled || isNaN(lat) || isNaN(lng)) return true;
      return x.distanceM == null || x.distanceM <= radiusM;
    }).sort((a, b) => {
      if (a.distanceM == null)

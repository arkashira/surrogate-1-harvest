# axentx-canary-daemon

> ROADMAP-100 #89. Synthetic canary — end-to-end probe of the cursor service.

| Field | Value |
|---|---|
| Role | Probes Worker every interval; alerts on degradation |
| Stage in pipeline | Side-channel (no queue I/O) |
| In | (external) `https://surrogate-1-cursor.ashira.workers.dev` |
| Out | D1 table `canary_runs` (via Worker `/admin/canary`) + Discord on failure |
| Idempotency | `run_id` unique per probe; Worker upserts |
| Concurrency | Single instance |

## Env

| Var | Default | What |
|---|---|---|
| `CANARY_INTERVAL_SEC` | `900` | 15 min |
| `CANARY_AUTH_TOKEN` | — | Worker auth token |
| `DISCORD_WEBHOOK` | — | Alert sink |
| `CANARY_FAIL_THRESHOLD` | `3` | Consecutive fails before "canary_red" |

## Probe steps

1. `GET /cursor/<canary-slug>` → record `peek_ms`
2. `POST /cursor/<canary-slug>/advance` (fixed payload) → `advance_ms`
3. `GET /cursor/<canary-slug>` (re-read) → `reread_ms`
4. `GET /audit?limit=1` → `audit_ms`
5. `POST /admin/canary` with run record (idempotent upsert)

## Failure modes

- Any step non-2xx → mark fail, increment counter, alert.
- Counter ≥ threshold → escalate `canary_red` (caller's pager handles).
- Worker auth fail → alert immediately (config drift).

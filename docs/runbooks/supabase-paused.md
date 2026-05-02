# Runbook: Supabase project paused / unreachable

Alert code: `supabase_paused`
Severity: critical — the work queue and several daemon state tables
live in Supabase Postgres; pause = pipeline halt.

## Symptoms

- Daemons calling `hermes_workqueue_pg.py` log `psycopg2.OperationalError: could not connect to server: Connection refused` or `503 Service Unavailable` from the REST API.
- `curl -sS https://riunimyxoalicbntogbp.supabase.co/rest/v1/health` returns 502/503.
- `supabase status` (CLI) on the host reports `Project paused`.
- 168 cron jobs that depend on Supabase start backing up; you'll see
  them in `data/hermes-jobs.json` with `last_status: error` and an
  error mentioning `connection`.

## Immediate action (in order)

1. **Verify it is paused (not just slow)**: `curl -i https://riunimyxoalicbntogbp.supabase.co -m 10`. Paused projects return a Supabase-branded HTML page with status code 503 and the text "This project has been paused".
2. **Unpause**: Go to https://supabase.com/dashboard/project/riunimyxoalicbntogbp → Settings → General → click "Restore project". Free-tier projects pause after 7 days of inactivity; restore is one click and takes ~90s.
3. **Wait for green**: `until curl -sf https://riunimyxoalicbntogbp.supabase.co/rest/v1/ -H "apikey: $SUPABASE_ANON_KEY" >/dev/null; do sleep 5; done`. Once it succeeds, daemons reconnect on their next poll automatically (we use `psycopg2-pool` with `connect_retry=True`).
4. **Drain the queue**: `psql ${SUPABASE_URL}?sslmode=require -c "SELECT count(*), claim_status FROM hermes_workqueue GROUP BY claim_status;"`. If `claimed` count is high (> 50), some workers were holding locks when the pause hit. Free them with `UPDATE hermes_workqueue SET claim_status='pending', claimed_at=NULL WHERE claim_status='claimed' AND claimed_at < now() - interval '10 minutes';`.
5. **Post status** to `#incidents` with timeline.

## Root cause — common patterns

- **7-day inactivity pause**: most common — happens when the daemons were stopped (e.g. maintenance) for over a week. Mitigation: a tiny `keep-alive` cron that pings the REST endpoint daily — already in `bin/keep-alive-local.sh`. Verify it is enabled.
- **Free-tier compute exhausted**: Supabase free tier has a soft "compute time" limit. Long-running queries from a buggy daemon can blow it. Mitigation: timeout every daemon query at 30s and add an index on the join columns flagged in the slow-query log.
- **Disk full**: Supabase free tier is 500MB. The `hermes_workqueue` and `agent_decisions` tables grow fast. Fix: nightly `DELETE FROM agent_decisions WHERE created_at < now() - interval '90 days'` and a `VACUUM FULL`. Already running via `bin/db-snapshot-backup.sh` but verify the cron is firing.
- **Postgres major-version upgrade window**: Supabase forces a maintenance restart on major upgrades. Schedule blasts a 5-min outage; daemons reconnect automatically.

## Post-incident

- Verify `bin/db-snapshot-backup.sh` ran in the last 24h. The latest backup should be on HF Hub at `axentx/db-backups`. If not, run it manually now.
- If the pause was inactivity: shorten the keep-alive cadence to 6 hours (was daily) and add a Discord alert if the keep-alive itself fails 3 times.
- If disk was the cause: capture the row counts before and after vacuum; if growth rate suggests we'll fill again in < 30 days, file an issue to upgrade to the $25/mo plan.
- Update this runbook with the specific symptom you saw (helps next on-call).

## Escalation

- Restore button does not unpause within 5 minutes → file a Supabase support ticket via the dashboard. Free tier has email support only; expect 24-48h response.
- Outage > 3h → switch read traffic to the HF Hub backup mirror (read-only) by setting `WORKQUEUE_READ_BACKUP=1` env on the daemons. Writes have to wait — accept the queue depth.

## Related

- `bin/hermes_workqueue_pg.py` (Supabase Postgres client).
- `bin/keep-alive-local.sh` (anti-pause heartbeat).
- `bin/db-snapshot-backup.sh` (nightly export to HF).

# Runbook: Cloudflare Worker (cursor service) down

Alert code: `cursor_worker_down`
Severity: critical — cursor service is the single chokepoint between
GCP daemons and D1; if it is down the whole pipeline halts.

## Symptoms

- `axentx-canary-daemon` reports 3 consecutive failures in 45 min and
  the `canary_red` alert lands in Discord.
- All daemons that call `cursor.axentx.workers.dev` start logging
  `urllib.error.HTTPError: 503` or `Connection timed out`.
- `dataset-mirror.sh` and `push-training-to-hf.sh` no-op because they
  cannot fetch new rows.
- `/audit` writes back up — the queue depth on `axentx-research-daemon`
  grows because nothing is checkpointing.

## Immediate action (in order)

1. **Verify it is the Worker, not your DNS / your laptop**: from a
   shell *on the GCP host* run
   `curl -i https://cursor.axentx.workers.dev/healthz -m 10`.
   - 200 → false alarm; check the canary daemon's log for what specific
     route is failing (likely a route change, not infra-down).
   - 5xx → Worker is up but unhealthy; jump to step 3.
   - timeout → Worker is unreachable; jump to step 2.
2. **Check Cloudflare status**: open `https://www.cloudflarestatus.com/`.
   If the Workers product is degraded for your colo (typically `IAD`
   or `LAX`): nothing to do but wait. Post the status URL in
   `#incidents` and switch dependent daemons to "drain mode" (set
   `CURSOR_SERVICE_URL=https://cursor-fallback.axentx.workers.dev` if
   the fallback is provisioned, else stop research/dev daemons).
3. **Tail Worker logs**: `wrangler tail cursor-worker --format=pretty`.
   Look for stack traces — common culprits are KV binding misconfigured
   after a `wrangler deploy` (wrong namespace ID), D1 quota exhausted,
   or an uncaught exception in a recent code change.
4. **Roll back the latest deploy** if logs point at a recent change:
   `wrangler rollback cursor-worker --message "rollback to $(git rev-parse HEAD~1)"`.
   The old version is back in seconds; once stable, root-cause and
   redo the change carefully.
5. **Post status** to `#incidents` with timeline and what was rolled back.

## Root cause — common patterns

- **D1 row-write quota hit**: free tier is 5 million row-writes/day.
  Surge from a daemon writing per-request (not batched) blows past it.
  Fix: batch writes in 60s windows + 100-row chunks.
- **KV namespace mistype**: `wrangler.toml` typo in the namespace id
  for a binding causes runtime `Cannot read properties of undefined`.
  Fix: rebind correctly, redeploy.
- **Bad regex in the rate-limit middleware**: catastrophic backtracking
  on a crafted path stalls the Worker. Fix: replace with bounded regex.
- **Hyperdrive credential expired**: cursor may proxy to Supabase via
  Hyperdrive; if the upstream Postgres password rotated and Hyperdrive
  was not updated, all queries fail. Fix: `wrangler hyperdrive update`.

## Post-incident

- File a postmortem in `docs/postmortems/YYYY-MM-DD-cursor-down.md`.
  Template at `docs/postmortems/_template.md`.
- If D1 was the cause: add a write-rate dashboard to `/dash` and an
  alarm at 70% of daily quota.
- Add a unit test for the specific failure mode — Worker tests run
  against `wrangler dev` in CI before deploy.
- Update this runbook if a new pattern was found.

## Escalation

- Down > 30 min and rollback did not help → page Ashira.
- Cloudflare status reports a global Workers outage → wait + monitor;
  escalation does not help.

## Related

- `docs/runbooks/oom-on-hf-space.md` (often co-occurs because the
  Worker is the routing layer for the Space).
- `bin/axentx-canary-daemon.py` (alert source).
- `cf-worker/wrangler.toml` (binding configuration).

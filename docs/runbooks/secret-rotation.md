# Runbook: Secret rotation

Triggered by `axentx-secret-watchdog.sh` posting "due in N days" or
"overdue" to Discord. Each row in `data/secret-rotation.json` is a
secret with its rotation cadence and last-rotated date.

## When you get the alert

You have until `next_due` to rotate. Past that, services are still
running on the *old* token but you are out of policy.

## Generic rotation procedure

For every secret, the steps are the same; only the rotate URL and the
target stores differ.

1. **Generate a new token at the rotate URL** (see the row in `data/secret-rotation.json` — `rotate_url` field).
2. **Update every store** listed in the `store` field. Common stores:
   - `~/.hermes/.env` on your laptop (used by interactive sessions and the local hermes-agent).
   - `/etc/surrogate-coordinator.env` on the GCP host (used by all systemd daemons — file is referenced by `EnvironmentFile=` in every unit).
   - GitHub repository secrets (`gh secret set NAME --body 'value' -R arkashira/surrogate-1-harvest`).
   - CF Worker variables (`wrangler secret put NAME --name <worker-name>` — answer the prompt with the new value).
   - Kaggle: `~/.kaggle/kaggle.json` (replace the file).
3. **Reload daemons that consume the secret**: `gcloud compute ssh axentx-vm --zone=asia-southeast1-a --command='sudo systemctl daemon-reload && sudo systemctl restart $(systemctl list-units --no-pager --type=service | grep axentx | awk "{print \$1}" | tr "\n" " ")'`.
4. **Verify**: hit one daemon's healthcheck or watch its log for 5 minutes — `journalctl -u axentx-research-daemon@1 -n 50 -f`. No 401/403 errors = good.
5. **Update the calendar** in this repo:
   ```bash
   python3 -c "
   import json, datetime
   p = 'data/secret-rotation.json'
   d = json.load(open(p))
   for s in d['secrets']:
       if s['name'] == 'HF_TOKEN':  # change name
           s['last_rotated'] = datetime.date.today().isoformat()
           cadence = s['cadence_days']
           s['next_due'] = (datetime.date.today() + datetime.timedelta(days=cadence)).isoformat()
   json.dump(d, open(p, 'w'), indent=2)
   "
   git commit -am 'rotation: HF_TOKEN' && git push
   ```
6. **Revoke the old token** at the provider so a leak is moot.

## Per-secret notes

- **HF_TOKEN / HF_TOKEN_PRO_WRITE**: these power push-training-to-hf.sh and dataset-mirror.sh. After rotation, run a manual `bin/push-training-to-hf.sh --dry-run` to confirm the new token has write scope on `axentx/*`.
- **SUPABASE_SERVICE_KEY**: critical, full-DB access. After rotation, the CF Worker also needs the new value (cursor service uses the service key for D1 mirror writes). `wrangler secret put SUPABASE_SERVICE_KEY --name cursor-worker`.
- **DISCORD_BOT_TOKEN**: rotating breaks any bot session in flight; the bot reconnects automatically. Do this during a quiet period.
- **CURSOR_AUTH_TOKEN**: rotating breaks all daemons until `/etc/surrogate-coordinator.env` and the Worker secret are both updated. Update Worker first, then env file, then restart daemons. There is a brief window (~30s) where calls 401.
- **GITHUB_PAT_arkashira**: also stored in the git remote URL. After rotating, update with `git remote set-url origin https://arkashira:NEW_TOKEN@github.com/arkashira/surrogate-1-harvest.git` on every clone.
- **CF_API_TOKEN**: rotating breaks `wrangler` until you update the `~/.wrangler/config/default.toml` or set `CLOUDFLARE_API_TOKEN` env.

## Post-rotation

- Verify the watchdog goes quiet on the next run (`bash bin/axentx-secret-watchdog.sh ALWAYS_POST=1` to force a heartbeat).
- If you missed the due date by > 7 days: file an entry in `state/incidents.jsonl` so the policy review at the next quarter notes the lapse.

## Escalation

- Rotation broke a daemon and you cannot recover within 15 minutes → roll back: keep the old token active at the provider (most providers allow N tokens simultaneously); revert the env file from git; restart daemons.
- A secret was leaked publicly (e.g. shown in a screenshot) → rotate immediately, revoke old token, run `bin/scan-secrets.sh --range $(git log --all --format=%H | head -100 | tr '\n' ' ')` to confirm no other secret is exposed.

## Related

- `bin/axentx-secret-watchdog.sh` (alert source).
- `data/secret-rotation.json` (calendar).
- `bin/scan-secrets.sh` (pre-push scanner — catches leaks before they ship).

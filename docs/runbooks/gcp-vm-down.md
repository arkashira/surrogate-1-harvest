# Runbook: GCP e2-micro daemon host down

Alert code: `gcp_vm_down`
Severity: critical — the VM hosts all 22 daemons; if it is down,
research/dev/reviewer/commit pipelines all stop.

## Symptoms

- All daemon-status checks in `/dash` flip to `unreachable`.
- `axentx-canary-daemon` was running on the VM, so its alerts stop too
  — meaning silence is itself a signal once the watchdog notices the
  canary missing for > 30 min.
- `gcloud compute instances list --filter=name=axentx-vm --format='value(STATUS)'`
  returns `TERMINATED` or the instance does not appear at all.
- SSH `gcloud compute ssh axentx-vm --zone=asia-southeast1-a` hangs
  past 30s.

## Immediate action (in order)

1. **Confirm the VM is actually down**: `gcloud compute instances describe axentx-vm --zone=asia-southeast1-a --format='value(status)'`. If `RUNNING`, jump to step 5 (it's running but partitioned).
2. **Check for free-tier preemption**: e2-micro is on the always-free tier; GCP rarely preempts it but does so during regional capacity events. `gcloud logging read 'resource.type=gce_instance AND resource.labels.instance_id=axentx-vm AND severity=NOTICE' --limit=20 --format='value(textPayload,timestamp)'` — look for `Instance terminated by Compute Engine`.
3. **Restart**: `gcloud compute instances start axentx-vm --zone=asia-southeast1-a`. Cold boot is ~45 seconds. If it fails with quota errors, switch zones with `--zone=asia-southeast1-b` (after stopping the dead instance there if any).
4. **Verify daemons came back**: `gcloud compute ssh axentx-vm --zone=asia-southeast1-a --command 'systemctl --no-pager --type=service | grep axentx | head -25'`. All should be `active (running)`.
5. **Network-partition case**: VM is RUNNING but unreachable. Check `gcloud compute firewall-rules list` — somebody may have removed the SSH rule. Re-add: `gcloud compute firewall-rules create allow-ssh-axentx --allow=tcp:22 --source-ranges=0.0.0.0/0`.
6. **Post-restart check**: hit the Discord webhook with a "VM restarted at HH:MM" message and watch the canary daemon log for 15 min.

## Root cause — common patterns

- **Free-tier zone preemption**: capacity event in `asia-southeast1-a`. Mitigation: keep an `instance template` with the same disk image so you can recreate in a sibling zone in 60 seconds.
- **Disk full**: 30GB quota fills with daemon logs. Fix: `find /var/log /opt/surrogate-1-harvest/logs -name '*.log' -mtime +7 -delete` and add this to `axentx-backup-sync.sh` as a nightly task. Add a logrotate config at `/etc/logrotate.d/axentx`.
- **Kernel panic from a runaway daemon**: `dmesg | tail -200` post-restart. If a daemon spiked memory > 950MB it OOM-killed the metadata server, which made the VM look offline to GCP. Fix: every daemon now sets `MemoryMax=128M` in its systemd unit — verify nothing slipped through.
- **OS upgrade reboot left services disabled**: occasionally `unattended-upgrades` reboots the VM and a daemon comes up with `disabled` state. Fix: every install script sets `WantedBy=multi-user.target` and uses `systemctl enable --now`. Re-run `bin/install-axentx-daemons.sh` to be safe.

## Post-incident

- Append a row to `state/incidents.jsonl` with timestamp, downtime in minutes, root cause, fix, and link to relevant gcloud logs.
- If downtime > 1h: file a postmortem at `docs/postmortems/YYYY-MM-DD-gcp-vm-down.md`.
- If preemption is the cause and frequency > 1/quarter: pre-bake an instance template + add a watchdog that auto-recreates in a sibling zone.
- Verify the canary daemon resumed reporting; update its stored
  cursor in case it lost state during the outage.

## Escalation

- VM does not start after 3 attempts → check GCP project quota
  (`gcloud compute project-info describe --format='value(quotas)'`).
  If quotas blocking: file a quota increase request.
- Outage > 2h → start drafting a customer-facing notice for the public
  status page (#44).
- Persistent regional outage → stand up the cold-standby in a different
  region (oci-coordinator-bootstrap.sh exists for this).

## Related

- `bin/install-axentx-daemons.sh` (re-installs all systemd units).
- `bin/oci-self-heal-daemon.py` (cold standby).
- `systemd/*.service` (per-daemon unit definitions).

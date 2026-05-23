# Disaster Recovery Drill — Quarterly

Run on the 1st Sunday of each quarter (1 Apr, 1 Jul, 1 Oct, 1 Jan).
Estimated time: 60 min.

## Pre-drill checklist (T-7 days)
- [ ] Latest backup exists in `axentx/surrogate-1-backups` HF dataset (auto, daily)
- [ ] Backup contains: D1 snapshot, Supabase pg_dump, training-pairs.jsonl
- [ ] Document current state hashes (commits, latest tags, daemon counts)

## Drill day — runbook
1. **Pause production traffic**
   - Set `MAINTENANCE_MODE=1` env on Worker → Worker returns 503 for non-/health
2. **Snapshot test (read-only)**
   - From Mac: `python3 bin/restore-from-backup.py --dry-run` (script TBD)
   - Confirm backup is parseable + complete
3. **Restore to ephemeral env**
   - Spin up a 2nd CF Worker (`surrogate-1-cursor-restore`) bound to a fresh D1 (`surrogate-1-cursor-test`)
   - Replay backup
   - Smoke test: `/health`, `/dynamic-datasets`, `/cursor/test-ds/advance`
4. **Restore Supabase work-queue**
   - Create new Supabase project `surrogate-1-queue-restore` (free)
   - Run latest migration + replay
5. **Verify cross-host queue still drains**
6. **Tear down**
   - Delete restore Worker + D1 + Supabase project (avoid quota cap)
7. **Post-drill notes**
   - Add findings to `docs/runbooks/dr-drill-{date}-notes.md`

## Acceptance criteria
- Backup is restorable end-to-end in < 30 min
- All critical state recovered (cursors, datasets, audit, training-pairs)
- Identifies any new gap → file roadmap entry

## Last drill
- Date: TBD (this is the template)
- Time-to-restore: TBD
- Findings: TBD

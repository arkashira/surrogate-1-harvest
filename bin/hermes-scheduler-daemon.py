#!/usr/bin/env python3
"""hermes-scheduler — continuous daemon, pushes due jobs to tasks-queue/.

Replaces surrogate-coordinator's cron-tick pattern. Wakes every 30s,
scans data/hermes-jobs.json (165 jobs), for each job whose cron expr
matches the current minute (with 60s slack to avoid drift), pushes ONE
task entry into pending/. Workers pick up async.

Idempotent: deduplicates by (job_id + minute), so re-evaluating doesn't
double-fire. Worker count is independent — scale horizontally.
"""
from __future__ import annotations

import datetime
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hermes_workqueue import (REPO_ROOT, log, cron_match, push_task, gc_done)

JOBS_FILE = REPO_ROOT / "data" / "hermes-jobs.json"
WAKE_SEC = int(os.environ.get("SCHEDULER_WAKE_SEC", "30"))


def shutdown(*_):
    log("scheduler", "shutdown")
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


def load_jobs() -> list[dict]:
    try:
        d = json.loads(JOBS_FILE.read_text())
        return d.get("jobs", []) if isinstance(d, dict) else d
    except Exception as e:
        log("scheduler", f"jobs.json parse error: {e}")
        return []


def evaluate_once() -> int:
    now = datetime.datetime.utcnow().replace(second=0, microsecond=0)
    jobs = load_jobs()
    n_fired = 0
    for job in jobs:
        if not job.get("enabled", True):
            continue
        sched = job.get("schedule", {})
        expr = sched.get("expr") if isinstance(sched, dict) else sched
        if not expr:
            continue
        if not cron_match(expr, now):
            continue
        push_task(job, now)
        n_fired += 1
    return n_fired


log("scheduler", f"start — wake every {WAKE_SEC}s, jobs source: {JOBS_FILE}")
n_iter = 0
while True:
    n_iter += 1
    try:
        fired = evaluate_once()
        if fired:
            log("scheduler", f"iter #{n_iter}: fired {fired} jobs into pending/")
        if n_iter % 60 == 0:
            removed = gc_done(keep_hours=24)
            log("scheduler", f"gc: removed {removed} old entries from done/failed/")
    except Exception as e:
        log("scheduler", f"⚠ iter exception: {type(e).__name__}: {e}")
    time.sleep(WAKE_SEC)

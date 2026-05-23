#!/usr/bin/env python3
"""hermes-cron jobs migrated to work-queue daemon model.

Pattern (matches axentx_pipeline.py):
  scheduler-daemon → pushes due jobs to tasks-queue/
  worker-daemon × N → pulls oldest from queue, executes, moves to done/

Two backends:
  - filesystem (default): state/hermes-tasks/{pending,running,done,failed}
    Single-host only (no cross-host visibility).
  - postgres (SUPABASE_URL env set): cross-host queue via Supabase PostgREST.
    GCP + OCI workers share the same queue — any host produces, any host
    consumes. Atomic claim via FOR UPDATE SKIP LOCKED on the server.

Switch backends with HERMES_QUEUE_BACKEND={fs|pg|auto}. "auto" picks pg if
SUPABASE_URL is set, else fs.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
QUEUE_ROOT = REPO_ROOT / "state" / "hermes-tasks"
QUEUES = {
    "pending":  QUEUE_ROOT / "pending",
    "running":  QUEUE_ROOT / "running",
    "done":     QUEUE_ROOT / "done",
    "failed":   QUEUE_ROOT / "failed",
}
LOG_DIR = REPO_ROOT / "logs"


# ── Backend selection ──────────────────────────────────────────────────────
_BACKEND_PREF = os.environ.get("HERMES_QUEUE_BACKEND", "auto").lower()
_USE_PG = (
    _BACKEND_PREF == "pg"
    or (_BACKEND_PREF == "auto" and bool(os.environ.get("SUPABASE_URL")))
)
BACKEND = "pg" if _USE_PG else "fs"

# Only create the filesystem queue dirs when the FS backend is active.
# In PG mode there's nothing to provision locally — and on dev machines
# /opt/surrogate-1-harvest doesn't even exist (would PermissionError).
if BACKEND == "fs":
    for _q in QUEUES.values():
        _q.mkdir(parents=True, exist_ok=True)
# Logs go local either way (every host writes its own log file).
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError):
    LOG_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "hermes-logs"
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(role: str, msg: str) -> None:
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] [{role}] {msg}"
    print(line, flush=True)
    with (LOG_DIR / f"hermes-{role}.log").open("a") as f:
        f.write(line + "\n")


def cron_match(expr: str, now: datetime.datetime) -> bool:
    """5-field POSIX cron match. Supports *, lists, ranges, */N."""
    fields = expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    return (
        _field_match(minute, now.minute, 0, 59) and
        _field_match(hour, now.hour, 0, 23) and
        _field_match(dom, now.day, 1, 31) and
        _field_match(month, now.month, 1, 12) and
        _field_match(dow, now.isoweekday() % 7, 0, 6)
    )


def _field_match(field: str, value: int, lo: int, hi: int) -> bool:
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            if base == "*":
                if value % int(step) == 0: return True
            else:
                start = int(base.split("-")[0])
                if (value - start) % int(step) == 0: return True
        elif "-" in part:
            a, b = part.split("-", 1)
            if int(a) <= value <= int(b): return True
        else:
            if int(part) == value: return True
    return False


def _fs_push_task(job: dict, fired_at: datetime.datetime) -> Path:
    """Filesystem backend: atomically write to pending/ keyed by job-id+timestamp."""
    job_id = job.get("id", job.get("name", "anon"))
    fname = f"{fired_at.strftime('%Y%m%d-%H%M%S')}-{job_id}.json"
    path = QUEUES["pending"] / fname
    if path.exists():
        return path  # dedup — same job in same minute
    payload = {
        "id": fname[:-5],
        "job_id": job_id,
        "name": job.get("name"),
        "fired_at": fired_at.isoformat() + "Z",
        "deliver": job.get("deliver", "local"),
        "script": job.get("script"),
        "prompt": job.get("prompt"),
        "skills": job.get("skills", []),
        "enabled_toolsets": job.get("enabled_toolsets", []),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.rename(path)
    return path


def _fs_claim_oldest_pending() -> tuple[Path, dict] | None:
    """Filesystem backend: atomic rename oldest pending → running/. Race-safe between workers."""
    files = sorted(QUEUES["pending"].glob("*.json"), key=lambda p: p.stat().st_mtime)
    for src in files:
        dst = QUEUES["running"] / src.name
        try:
            src.rename(dst)  # atomic; raises if another worker beat us
        except FileNotFoundError:
            continue
        try:
            return dst, json.loads(dst.read_text())
        except Exception:
            dst.rename(QUEUES["failed"] / dst.name)
            continue
    return None


def _fs_finish_task(running_path: Path, item: dict, ok: bool, output: str) -> None:
    """Filesystem backend: move running/<id>.json → done/<id>.json (or failed/)."""
    item["finished_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    item["ok"] = ok
    item["output"] = output[:6000]
    target = QUEUES["done"] if ok else QUEUES["failed"]
    new_path = target / running_path.name
    new_path.write_text(json.dumps(item, indent=2, default=str))
    running_path.unlink(missing_ok=True)


# ── Public API: dispatch to the active backend ─────────────────────────────
# Daemons import push_task / claim_oldest_pending / finish_task by these names
# and don't care which backend they're on. Switch via HERMES_QUEUE_BACKEND or
# by setting/unsetting SUPABASE_URL.
if BACKEND == "pg":
    # Lazy-import so machines without the PG adapter file still load this module
    from hermes_workqueue_pg import (
        push_task as _pg_push,
        claim_oldest_pending as _pg_claim,
        finish_task as _pg_finish,
    )

    def push_task(job: dict, fired_at: datetime.datetime):
        return _pg_push(job, fired_at)

    def claim_oldest_pending():
        return _pg_claim()

    def finish_task(handle, item: dict, ok: bool, output: str) -> None:
        # PG backend uses a bigint row id; FS backend uses a Path. Daemons
        # treat the handle as opaque, so this is a transparent swap.
        _pg_finish(int(handle) if not isinstance(handle, int) else handle,
                   item, ok, output)
else:
    push_task = _fs_push_task
    claim_oldest_pending = _fs_claim_oldest_pending
    finish_task = _fs_finish_task


def _fs_gc_done(keep_hours: int = 24) -> int:
    """Filesystem backend: sweep done/ + failed/ for entries older than keep_hours.
    Without GC the queue dirs grow forever and wedge inotify watchers."""
    cutoff = datetime.datetime.utcnow().timestamp() - keep_hours * 3600
    n = 0
    for q in (QUEUES["done"], QUEUES["failed"]):
        for f in q.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                n += 1
    return n


if BACKEND == "pg":
    from hermes_workqueue_pg import gc_done as _pg_gc, reap_stuck as _pg_reap

    def gc_done(keep_hours: int = 24) -> int:
        return _pg_gc(keep_hours)

    def reap_stuck(timeout_min: int = 10) -> int:
        """PG-only: re-queue rows stuck in 'running' (worker died mid-task)."""
        return _pg_reap(timeout_min)
else:
    gc_done = _fs_gc_done

    def reap_stuck(timeout_min: int = 10) -> int:
        """No-op on filesystem backend (single-host = no stuck-elsewhere risk)."""
        return 0

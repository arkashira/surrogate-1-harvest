#!/usr/bin/env python3
"""hermes-workqueue cross-host backend — Supabase PostgREST RPC.

Drop-in replacement for hermes_workqueue.py (filesystem queue). Same
public API: push_task / claim_oldest_pending / finish_task / gc_done.
Backed by Postgres on Supabase free tier — workers on GCP and OCI both
hit the same queue, so any host can produce or consume.

Why PostgREST RPC instead of psycopg:
  - Zero native deps (urllib only) — fits e2-micro 1 GB without a libpq build
  - Atomic claim is a Postgres function (claim_task) using FOR UPDATE
    SKIP LOCKED, so race-safety is enforced server-side
  - REST works through any HTTP egress (some serverless / Lambda envs
    block raw 5432)

Network: ap-southeast-1 (Singapore). GCP us-central1 RTT ~180ms; that's
fine for a low-volume control-plane queue. If we ever need <50ms ops
we can switch to psycopg over the connection-pooled :6543 endpoint.

Server-side schema (created via Management API on 2026-05-02):
    task_queue(id, job_id, name, payload jsonb, status, fired_at,
               claimed_at, claimed_by, finished_at, ok, output, attempts)
    UNIQUE(job_id, fired_at)

RPCs (PostgREST auto-routes /rest/v1/rpc/<name>):
    push_task(p_job_id, p_name, p_payload, p_fired_at) → bigint|null
    claim_task(p_worker)                               → row|empty
    finish_task(p_id, p_ok, p_output)                  → void
    gc_tasks(p_keep_hours)                             → int
    reap_stuck(p_timeout_min)                          → int
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import urllib.error
import urllib.request


def _utcnow() -> datetime.datetime:
    """Naive UTC datetime — Python 3.12+ deprecated datetime.utcnow()."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
# Prefer the new sb_secret_* key (modern Supabase API), fall back to legacy
# service_role JWT. Either passes Authorization+apikey checks.
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SECRET_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
    or os.environ.get("SUPABASE_ANON_KEY", "")
)
DEFAULT_WORKER = os.environ.get(
    "WORKER_ID",
    f"{socket.gethostname()}-{os.getpid()}",
)
TIMEOUT = int(os.environ.get("PG_QUEUE_TIMEOUT", "20"))


class QueueError(RuntimeError):
    pass


def _rpc(fn: str, params: dict) -> list | dict:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise QueueError(
            "SUPABASE_URL / SUPABASE_SECRET_KEY missing — set in /etc/surrogate-coordinator.env"
        )
    body = json.dumps(params).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/rpc/{fn}",
        data=body,
        method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
            # NB: do NOT use a Mozilla/browser UA here — Supabase's sb_secret_*
            # keys are server-only and explicitly reject browser-looking UAs
            # (HTTP 401 "Forbidden use of secret API key in browser"). A plain
            # service identifier passes both that check and the default-urllib
            # block in front of api.supabase.com.
            "User-Agent": "surrogate-1-queue/1.0 (+server)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise QueueError(f"{fn}: HTTP {e.code} {e.read()[:200].decode(errors='replace')}")
    except urllib.error.URLError as e:
        raise QueueError(f"{fn}: network — {e.reason}")


# ── Public API (mirrors hermes_workqueue.py for drop-in replacement) ───────


def push_task(job: dict, fired_at: datetime.datetime) -> int | None:
    """Idempotent insert. Returns row id or None if duplicate (job_id+fired_at)."""
    job_id = str(job.get("id") or job.get("name") or "anon")
    payload = {
        "deliver": job.get("deliver", "local"),
        "script": job.get("script"),
        "prompt": job.get("prompt"),
        "skills": job.get("skills", []),
        "enabled_toolsets": job.get("enabled_toolsets", []),
    }
    res = _rpc(
        "push_task",
        {
            "p_job_id": job_id,
            "p_name": job.get("name") or job_id,
            "p_payload": payload,
            "p_fired_at": fired_at.isoformat(),
        },
    )
    # PostgREST returns scalar function results as a single value (not list)
    return res if isinstance(res, int) else (res[0] if isinstance(res, list) and res else None)


def claim_oldest_pending(worker: str = DEFAULT_WORKER) -> tuple[int, dict] | None:
    """Atomic FIFO claim. Returns (row_id, item) or None if queue empty.

    `row_id` is the Postgres bigint — pass it back to finish_task().
    """
    res = _rpc("claim_task", {"p_worker": worker})
    if not res:
        return None
    row = res[0] if isinstance(res, list) else res
    if not row or row.get("out_id") is None:
        return None
    item = {
        "id": str(row["out_id"]),
        "job_id": row.get("out_job_id"),
        "name": row.get("out_name"),
        **(row.get("out_payload") or {}),
    }
    return int(row["out_id"]), item


def finish_task(row_id: int, item: dict, ok: bool, output: str) -> None:
    """Mark row as done or failed."""
    _rpc(
        "finish_task",
        {
            "p_id": int(row_id),
            "p_ok": bool(ok),
            "p_output": (output or "")[:8000],
        },
    )


def gc_done(keep_hours: int = 24) -> int:
    """Drop done/failed rows older than keep_hours. Returns count removed."""
    res = _rpc("gc_tasks", {"p_keep_hours": int(keep_hours)})
    if isinstance(res, int):
        return res
    if isinstance(res, list) and res:
        first = res[0]
        return first if isinstance(first, int) else int(first.get("gc_tasks", 0))
    return 0


def reap_stuck(timeout_min: int = 10) -> int:
    """Re-queue rows stuck in 'running' longer than timeout_min (worker died)."""
    res = _rpc("reap_stuck", {"p_timeout_min": int(timeout_min)})
    if isinstance(res, int):
        return res
    if isinstance(res, list) and res:
        first = res[0]
        return first if isinstance(first, int) else int(first.get("reap_stuck", 0))
    return 0


# ── CLI: smoke-test from any host ──────────────────────────────────────────
if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "self-test"
    if cmd == "push":
        # bin/hermes_workqueue_pg.py push <job_id> <script>
        jid = sys.argv[2]
        script = sys.argv[3] if len(sys.argv) > 3 else "echo from-cli"
        rid = push_task(
            {"id": jid, "name": jid, "script": script},
            _utcnow(),
        )
        print(f"pushed row_id={rid}")
    elif cmd == "claim":
        c = claim_oldest_pending(worker=sys.argv[2] if len(sys.argv) > 2 else "cli")
        print(f"claimed: {c}")
    elif cmd == "finish":
        finish_task(int(sys.argv[2]), {}, True, sys.argv[3] if len(sys.argv) > 3 else "ok")
        print("finished")
    elif cmd == "gc":
        print(f"gc_tasks removed: {gc_done(int(sys.argv[2]) if len(sys.argv) > 2 else 24)}")
    elif cmd == "reap":
        print(f"reap_stuck re-queued: {reap_stuck(int(sys.argv[2]) if len(sys.argv) > 2 else 10)}")
    else:
        # Self-test: push → claim → finish → verify
        print(f"[self-test] worker={DEFAULT_WORKER}")
        rid = push_task(
            {"id": f"selftest-{os.getpid()}", "name": "self-test",
             "script": "echo self-test ok"},
            _utcnow(),
        )
        print(f"  push      → row {rid}")
        c = claim_oldest_pending()
        print(f"  claim     → {c[1]['job_id'] if c else 'EMPTY'} (row {c[0] if c else '-'})")
        if c:
            finish_task(c[0], c[1], True, "self-test pass")
            print(f"  finish    → row {c[0]} marked done")
        rm = gc_done(0)
        print(f"  gc(0h)    → cleared {rm}")

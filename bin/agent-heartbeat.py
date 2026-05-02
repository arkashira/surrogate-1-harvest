"""agent-heartbeat — tiny library every daemon imports to emit status.

Purpose (user directive 2026-05-02):
  > "แล้วจะรู้ว่า agent ไหนทำงานตอนไหน"
  > "ดู agent ทั้งหมด แล้วดูว่าใครต้องทำงานเมื่อไหร่ตอนไหน"

How it works (D1-backed, post-2026-05-02):
  Each daemon calls heartbeat("research-1", state="working", task="reddit:devops")
  on every cycle. That POSTs to the surrogate-1-cursor worker at
  /agent/heartbeat which UPSERTs into D1 table agent_status. The worker
  returns 200 on success, fail-silent on the daemon side.

  /dash/agents reads `WHERE last_seen >= now-600s` from D1 and renders.

Why D1 (not KV any more):
  KV free-tier is 1000 writes/day account-wide. With 30 daemons × 60s
  heartbeat = 43,200/day → blown by lunch. D1 free is 100k writes/day
  per database; same 30 × 60s pattern = ~9% of free quota. Easily fits.

Why D1 over Supabase:
  Worker has D1 binding pre-configured + zero-latency local read for
  /dash/agents. Adding Supabase would mean another HTTPS hop on every
  page render. Plus shared CDN egress with the rest of the worker.

Design notes:
  - Fail-silent — heartbeat MUST NOT break the agent. Network blip → skip
    one tick, retry next cycle.
  - Background thread — heartbeat runs every HEARTBEAT_SEC in the
    background after start_heartbeat() so the agent's main loop is never
    blocked on an HTTP call.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

# Migrated 2026-05-03 from CF Worker → Supabase. CF free tier (100k req/day)
# was exhausted by 572-daemon scale-up; Supabase PostgREST is unlimited on
# free tier when hitting tables direct. Old CF route stays in env for
# fallback if Supabase unconfigured.
SB_URL = os.environ.get("SUPABASE_URL", "https://riunimyxoalicbntogbp.supabase.co")
SB_KEY = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get(
    "SUPABASE_SERVICE_KEY", ""
)
WORKER_URL = os.environ.get(
    "HEARTBEAT_WORKER_URL", ""  # default empty — Supabase first
)
HEARTBEAT_AUTH = os.environ.get("HEARTBEAT_AUTH", "")
# Bumped 60s → 300s as part of the scale-up to keep total req-rate low even
# without per-tier quota. 572 daemons × 300s = 165k/day, comfortable on
# Supabase free tier.
HEARTBEAT_SEC = int(os.environ.get("HEARTBEAT_SEC", "300"))
HOSTNAME = socket.gethostname()

_state: dict = {
    "agent": "",
    "host": HOSTNAME,
    "pid": os.getpid(),
    "state": "starting",     # starting|idle|working|error|shutting-down
    "task": "",              # short label of current work
    "cycle_n": 0,
    "last_error": "",
    "started_at": datetime.datetime.utcnow().isoformat() + "Z",
    "last_seen": "",
}
_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_evt = threading.Event()


def _post_heartbeat(value: dict) -> None:
    """UPSERT into Supabase agent_status table (preferred) or fall back to
    CF Worker /agent/heartbeat (legacy). Best-effort, swallow all errors."""
    # Supabase first (unlimited rate)
    if SB_URL and SB_KEY:
        body = json.dumps({
            "agent": value.get("agent", ""),
            "host": value.get("host", ""),
            "pid": value.get("pid", 0),
            "state": value.get("state", ""),
            "task": value.get("task", ""),
            "cycle_n": value.get("cycle_n", 0),
            "last_error": value.get("last_error", ""),
            "started_at": value.get("started_at", ""),
            "last_seen": int(datetime.datetime.utcnow().timestamp()),
        }).encode()
        headers = {
            "apikey": SB_KEY,
            "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json",
            # ON CONFLICT (agent) DO UPDATE — Supabase upsert syntax
            "Prefer": "resolution=merge-duplicates,return=minimal",
            "User-Agent": f"surrogate-heartbeat/1.0 ({HOSTNAME})",
        }
        try:
            req = urllib.request.Request(
                f"{SB_URL}/rest/v1/agent_status?on_conflict=agent",
                data=body, method="POST", headers=headers,
            )
            urllib.request.urlopen(req, timeout=8)
            return
        except Exception:
            pass  # fall through to CF if Supabase down
    # Legacy CF Worker fallback
    if not WORKER_URL:
        return
    body = json.dumps(value).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"surrogate-heartbeat/1.0 ({HOSTNAME})",
    }
    if HEARTBEAT_AUTH:
        headers["X-Heartbeat-Token"] = HEARTBEAT_AUTH
    req = urllib.request.Request(WORKER_URL, data=body, method="POST",
                                 headers=headers)
    try:
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass  # heartbeat is best-effort by design


def heartbeat(agent: str, *, state: str = "working", task: str = "",
              error: str | None = None, cycle_n: int | None = None) -> None:
    """Update local state. Background thread will flush to KV on next tick."""
    with _lock:
        _state["agent"] = agent
        _state["state"] = state
        if task:
            _state["task"] = task[:200]
        if error is not None:
            _state["last_error"] = str(error)[:300]
        if cycle_n is not None:
            _state["cycle_n"] = cycle_n


def _flush_loop() -> None:
    while not _stop_evt.is_set():
        with _lock:
            _state["last_seen"] = datetime.datetime.utcnow().isoformat() + "Z"
            agent = _state["agent"]
            snap = dict(_state)
        if agent:
            # Prefix agent name with host so multi-VM fleets don't collide
            # in the D1 agent_status table (key is `agent` alone). After
            # 2026-05-02 Kamatera launch, GCP and Kam daemons shared role
            # names ('dev-1', 'bd', ...) and overwrote each other —
            # dashboard reported only one. Keying as '<host>/<role>' fans
            # them out across hosts.
            snap_send = dict(snap)
            snap_send["agent"] = f"{HOSTNAME}/{agent}"
            _post_heartbeat(snap_send)
        # Sleep in small slices so SIGTERM exits cleanly within ~1s
        for _ in range(HEARTBEAT_SEC):
            if _stop_evt.is_set():
                break
            time.sleep(1)


def start_heartbeat(agent: str, initial_state: str = "starting") -> None:
    """Call once at daemon startup. Idempotent."""
    global _thread
    heartbeat(agent, state=initial_state, task="boot")
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_flush_loop, daemon=True,
                               name=f"heartbeat-{agent}")
    _thread.start()


def stop_heartbeat() -> None:
    """Call from SIGTERM/SIGINT handler. Final 'shutting-down' write."""
    with _lock:
        _state["state"] = "shutting-down"
        _state["last_seen"] = datetime.datetime.utcnow().isoformat() + "Z"
        snap = dict(_state)
        agent = _state["agent"]
    if agent:
        snap_send = dict(snap)
        snap_send["agent"] = f"{HOSTNAME}/{agent}"
        _post_heartbeat(snap_send)
    _stop_evt.set()

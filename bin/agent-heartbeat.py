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

WORKER_URL = os.environ.get(
    "HEARTBEAT_WORKER_URL",
    "https://surrogate-1-cursor.ashira.workers.dev/agent/heartbeat",
)
HEARTBEAT_AUTH = os.environ.get("HEARTBEAT_AUTH", "")
HEARTBEAT_SEC = int(os.environ.get("HEARTBEAT_SEC", "60"))
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
    """POST to worker /agent/heartbeat. Best-effort, swallow all errors."""
    if not WORKER_URL:
        return
    body = json.dumps(value).encode()
    headers = {"Content-Type": "application/json"}
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
            _post_heartbeat(snap)
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
        _post_heartbeat(snap)
    _stop_evt.set()

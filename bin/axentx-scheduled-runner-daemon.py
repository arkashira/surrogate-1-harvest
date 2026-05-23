#!/usr/bin/env python3
"""scheduled-runner — replaces systemd .timer units with always-on daemon.

User directive 2026-05-02:
  > "ทุก agent ทุกตัว ... cron ก็convent เป็น deamon ทำงานแบบ active ตลอดเวลา"

Two scripts that used to run via .timer units, now run inside this single
long-running process so they show up on /dash/agents like every other agent:

  axentx-secret-watchdog.sh    — daily 09:00 UTC (was axentx-secret-watchdog.timer)
  claude-mini-sprint.sh +
  claude-mini-retro.sh         — twice daily 02:00 + 10:00 UTC (was
                                 axentx-sprint-ceremony.timer)

Behavior:
  Sleeps until the next scheduled wall-clock time, runs the script, logs
  result, sleeps again. Persistent across restarts — schedule is computed
  from current UTC, not from process-start.
"""
from __future__ import annotations

import datetime
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
LOG_FILE = REPO_ROOT / "logs" / "scheduled-runner.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Optional CF heartbeat — best-effort
sys.path.insert(0, str(Path(__file__).parent))
try:
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "agent_heartbeat", str(REPO_ROOT / "bin" / "agent-heartbeat.py"))
    _hb = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_hb)
    _hb.start_heartbeat("scheduled-runner", initial_state="starting")
except Exception:
    _hb = None


def log(msg: str) -> None:
    line = f"[{datetime.datetime.utcnow().isoformat()}Z] [sched] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def run_script(name: str, cmd: list[str]) -> int:
    log(f"▸ run {name}: {' '.join(cmd)}")
    if _hb:
        _hb.heartbeat("scheduled-runner", state="working", task=name)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=1800, cwd=str(REPO_ROOT),
                           env=os.environ.copy())
        log(f"  {name} → exit={r.returncode}, "
            f"stdout={(r.stdout or '')[-200:].strip()[:200]}")
        return r.returncode
    except subprocess.TimeoutExpired:
        log(f"  {name} → TIMEOUT after 30 min")
        return 124
    except Exception as e:
        log(f"  {name} → ERROR: {type(e).__name__}: {e}")
        return 1


# Schedule = list of (name, hour_utc, minute_utc, cmd)
SCHEDULE = [
    ("secret-watchdog", 9, 0,
     ["/bin/bash", str(REPO_ROOT / "bin" / "axentx-secret-watchdog.sh")]),
    ("mini-sprint-am", 2, 0,
     ["/bin/bash", str(REPO_ROOT / "bin" / "claude-mini-sprint.sh")]),
    ("mini-retro-am", 2, 30,
     ["/bin/bash", str(REPO_ROOT / "bin" / "claude-mini-retro.sh")]),
    ("mini-sprint-pm", 10, 0,
     ["/bin/bash", str(REPO_ROOT / "bin" / "claude-mini-sprint.sh")]),
    ("mini-retro-pm", 10, 30,
     ["/bin/bash", str(REPO_ROOT / "bin" / "claude-mini-retro.sh")]),
]


def next_run(now: datetime.datetime) -> tuple[datetime.datetime, str, list[str]]:
    """Return (when, name, cmd) for the closest upcoming scheduled task."""
    candidates = []
    for name, h, m, cmd in SCHEDULE:
        when = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if when <= now:
            when += datetime.timedelta(days=1)
        candidates.append((when, name, cmd))
    candidates.sort(key=lambda c: c[0])
    return candidates[0]


def shutdown(*_):
    log("shutdown")
    if _hb:
        try:
            _hb.stop_heartbeat()
        except Exception:
            pass
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


def main() -> int:
    log(f"start — {len(SCHEDULE)} scheduled tasks")
    while True:
        now = datetime.datetime.utcnow()
        when, name, cmd = next_run(now)
        wait = (when - now).total_seconds()
        log(f"next: {name} @ {when.isoformat()}Z (in {int(wait)}s)")
        if _hb:
            _hb.heartbeat("scheduled-runner", state="idle",
                          task=f"sleep until {name} ({int(wait)}s)")
        # Sleep in chunks so SIGTERM exits within ~30s
        end = time.monotonic() + wait
        while time.monotonic() < end:
            time.sleep(min(30, end - time.monotonic()))
        run_script(name, cmd)
        # Brief pause to avoid double-firing on clock skew
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())

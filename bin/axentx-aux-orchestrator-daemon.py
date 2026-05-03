#!/usr/bin/env python3
"""axentx aux-orchestrator — runs the 6 maintenance scripts that have no
daemon driver:

  agent-decisions-to-pairs.py  → distill pipeline decisions into SFT pairs
  harvest-transcripts.sh       → mine ~/.claude/projects/* sessions
  scraped-to-surrogate.sh      → backlog + Obsidian → training pairs
  dataset-enrich.sh            → push training-jsonl/* → HF axentx/*
  push-training-to-hf.sh       → push pairs to HF training-pairs
  distill-patterns.sh          → patterns from harvested interactions

User callout 2026-05-03: 'agent-decisions-to-pairs ขยาย 7 stages — พวกนี้
ถูก scheduled ให้รันมั้ย?' Audit: scripts exist but never invoked. Cron
removed during the SaaS migration; never replaced. This daemon fixes that.

Each script has its own cadence (set per-name below). Daemon polls every
60s, runs whichever scripts are stale.
"""
from __future__ import annotations
import datetime
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log  # noqa: E402

BIN_DIR = REPO_ROOT / "bin"
STATE_FILE = REPO_ROOT / "state" / "aux-orchestrator.state.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

# Per-script cadence (seconds) + timeout (seconds)
JOBS = [
    ("agent-decisions-to-pairs.py", 1800,  600),   # every 30min
    ("harvest-transcripts.sh",       3600, 600),   # every 1h
    ("scraped-to-surrogate.sh",      1800, 900),   # every 30min
    ("dataset-enrich.sh",            1800, 900),   # every 30min
    ("push-training-to-hf.sh",       3600, 600),   # every 1h
    ("distill-patterns.sh",          7200, 600),   # every 2h
]

POLL_SEC = int(os.environ.get("AUX_POLL_SEC", "60"))

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def run_script(name: str, timeout: int) -> tuple[str, float]:
    path = BIN_DIR / name
    if not path.exists():
        return ("missing", 0.0)
    cmd = (["python3", str(path)] if name.endswith(".py")
           else ["bash", str(path)])
    t0 = time.time()
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(REPO_ROOT),
        )
        elapsed = time.time() - t0
        return ("ok" if r.returncode == 0 else f"exit={r.returncode}",
                round(elapsed, 1))
    except subprocess.TimeoutExpired:
        return ("timeout", float(timeout))
    except Exception as e:
        return (f"crash:{type(e).__name__}", time.time() - t0)


def main() -> int:
    log("aux-orch", f"start — {len(JOBS)} jobs, poll every {POLL_SEC}s")
    while not _stop:
        state = load_state()
        now = time.time()
        ran = 0
        for name, cadence, timeout in JOBS:
            if _stop:
                break
            last = state.get(name, {}).get("last_run", 0)
            if now - last < cadence:
                continue
            log("aux-orch", f"▸ {name}")
            status, elapsed = run_script(name, timeout)
            state[name] = {
                "last_run": int(time.time()),
                "last_status": status,
                "last_elapsed_sec": elapsed,
            }
            save_state(state)
            ran += 1
            ico = "✓" if status == "ok" else "⚠"
            log("aux-orch", f"  {ico} {name} {status} in {elapsed:.0f}s")
        if ran:
            log("aux-orch", f"cycle done — ran {ran} job(s)")
        for _ in range(POLL_SEC):
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

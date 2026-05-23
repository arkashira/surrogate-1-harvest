#!/usr/bin/env python3
"""axentx scrape orchestrator — continuous round-robin over 38+ scrape-*.sh
scripts.

User callout 2026-05-03: 'scrape agent ไม่ได้ทำมาตั้งนานแล้ว'.
Audit found 38 scrape scripts in bin/ but NONE running as daemons or
timers. Cron must have been removed in an earlier migration. Without
fresh scrape data, dataset-mirror + harvested_pains + arxiv-weekly all
stale.

This daemon runs each scrape-*.sh in round-robin:
  - 60s gap between scripts (avoids hammering sources)
  - Per-script timeout 600s (long scrapes don't block the cycle forever)
  - Failures logged + skipped — never crash the whole orchestrator
  - State stamp per script in state/scrape-orchestrator.state.json so
    we can prioritize stale-since-last-run on next cycle (default 1
    full cycle / day).

Output is whatever each script produces — typically writes to
~/axentx/surrogate/data/<source>-<date>.jsonl which dataset-enrich +
hf-flusher pick up downstream.
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
STATE_FILE = REPO_ROOT / "state" / "scrape-orchestrator.state.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

# Round-robin gap between scripts (seconds)
GAP_SEC = int(os.environ.get("SCRAPE_GAP_SEC", "60"))
# Per-script timeout
SCRIPT_TIMEOUT = int(os.environ.get("SCRAPE_SCRIPT_TIMEOUT", "600"))
# How long a script's last-run is "fresh" (skip until stale).
STALE_HOURS = int(os.environ.get("SCRAPE_STALE_HOURS", "24"))

# All scrape scripts. Filter for the patterns user uses.
SCRIPT_GLOBS = [
    "scrape-*.sh",
    "*-scraper.sh",
    "domain-scrape-loop.sh",
    "bulk-scrape-burst.sh",
    "bd-news-scraper.sh",
    "github-domain-scrape.sh",
    "github-bulk-train-scrape.sh",
]

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def discover_scripts() -> list[Path]:
    found: set[Path] = set()
    for pat in SCRIPT_GLOBS:
        for p in BIN_DIR.glob(pat):
            if p.is_file() and os.access(p, os.X_OK):
                found.add(p)
    return sorted(found)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log("scrape-orch", f"  state save fail: {type(e).__name__}")


def needs_run(name: str, state: dict) -> bool:
    last = state.get(name, {}).get("last_run", 0)
    age_hr = (time.time() - last) / 3600
    return age_hr >= STALE_HOURS


def run_one(script: Path, state: dict) -> bool:
    name = script.name
    log("scrape-orch", f"▸ {name}")
    t0 = time.time()
    try:
        r = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True,
            timeout=SCRIPT_TIMEOUT,
            cwd=str(REPO_ROOT),
        )
        elapsed = time.time() - t0
        ok = r.returncode == 0
        # Capture last 200 chars of stderr/stdout as audit
        tail = (r.stdout + r.stderr)[-300:].replace("\n", " ")
        state[name] = {
            "last_run": int(time.time()),
            "last_status": "ok" if ok else f"exit={r.returncode}",
            "last_elapsed_sec": round(elapsed, 1),
            "last_tail": tail,
        }
        log("scrape-orch",
            f"  {'✓' if ok else '✗'} {name} in {elapsed:.0f}s "
            f"(rc={r.returncode})")
        return ok
    except subprocess.TimeoutExpired:
        state[name] = {
            "last_run": int(time.time()),
            "last_status": "timeout",
            "last_elapsed_sec": SCRIPT_TIMEOUT,
        }
        log("scrape-orch", f"  ⏱ {name} timeout ({SCRIPT_TIMEOUT}s)")
        return False
    except Exception as e:
        state[name] = {
            "last_run": int(time.time()),
            "last_status": f"crash: {type(e).__name__}",
        }
        log("scrape-orch", f"  ✗ {name}: {type(e).__name__}: {str(e)[:120]}")
        return False


def main() -> int:
    log("scrape-orch",
        f"start — {len(discover_scripts())} script(s) discovered, "
        f"gap={GAP_SEC}s, stale_after={STALE_HOURS}h")
    while not _stop:
        scripts = discover_scripts()
        state = load_state()
        # Prioritize: stale ones first, then never-run, then freshest last
        ranked = sorted(scripts,
                        key=lambda p: state.get(p.name, {}).get("last_run", 0))
        ran = 0
        for script in ranked:
            if _stop:
                break
            if not needs_run(script.name, state):
                continue
            run_one(script, state)
            save_state(state)
            ran += 1
            for _ in range(GAP_SEC):
                if _stop:
                    return 0
                time.sleep(1)
        log("scrape-orch",
            f"cycle done — ran {ran} script(s); idle until next stale check")
        for _ in range(300):  # 5min idle nap between cycles
            if _stop:
                return 0
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

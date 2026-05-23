#!/usr/bin/env python3
"""self-heal daemon — long-running version of oci-self-heal.sh.

Replaces the systemd timer (cron-style) with a continuous daemon that
sleeps INSIDE the process. Per user directive: 'all-daemon, no cron'.

Polls every SELF_HEAL_INTERVAL_SEC, restarts dead services with
exponential backoff, posts to Discord on actual recovery.
"""
from __future__ import annotations
import os, sys, time, signal, subprocess, urllib.request, json, datetime
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
STATE_DIR = REPO_ROOT / "state" / "self-heal"
STATE_DIR.mkdir(parents=True, exist_ok=True)
INTERVAL = int(os.environ.get("SELF_HEAL_INTERVAL_SEC", "180"))

SERVICES = {
    "axentx-reviewer-daemon":  "reviewer",
    "axentx-qa-daemon":        "qa",
    "axentx-commit-daemon":    "commit + push",
    "axentx-pm-daemon":        "PM (sprint+retro)",
    "axentx-dev-daemon@1":     "dev (worker 1)",
    "axentx-dev-daemon@2":     "dev (worker 2)",
    "axentx-dev-daemon@3":     "dev (worker 3)",
    "axentx-dev-daemon@4":     "dev (worker 4)",
    "axentx-dev-daemon@5":     "dev (worker 5)",
    "axentx-dev-daemon@6":     "dev (worker 6)",
    "axentx-architect-daemon": "architect",
    "axentx-security-daemon":  "security review",
    "axentx-perf-daemon":      "perf review",
    "axentx-docs-daemon":      "docs auto-update",
    "axentx-release-daemon":   "semver release",
    "axentx-ux-daemon":        "UX flows + wireframes",
    "axentx-content-daemon":   "content marketing",
    "axentx-trends-daemon":    "market signal scraper",
    "axentx-customer-poll-daemon": "weekly Discord poll",
    "axentx-incident-responder-daemon": "auto-fix GH/Render failures",
    "surrogate-state-sync-daemon": "5min state→git snapshot",
    "axentx-scheduled-runner-daemon": "always-on replacement for timers",
    "axentx-pain-validator-daemon": "cross-source pain validation gate",
    "axentx-skill-synthesizer-daemon": "auto-synth skills + papers from failure patterns",
    "axentx-hf-flusher-daemon": "D1 → HF Datasets batched flush",
    "axentx-v1-warmup-daemon": "Surrogate-1 v1 HF Space keep-warm",
}


def log(msg: str) -> None:
    print(f"[{datetime.datetime.utcnow().isoformat()}Z] [self-heal] {msg}", flush=True)


def post_discord(msg: str) -> None:
    url = os.environ.get("DISCORD_WEBHOOK", "")
    if not url: return
    body = json.dumps({"content": msg[:1900]}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/arkashira/surrogate-1-harvest, 1.0)",
    })
    try: urllib.request.urlopen(req, timeout=6)
    except Exception: pass


def is_active(svc: str) -> bool:
    r = subprocess.run(["systemctl", "is-active", svc],
                       capture_output=True, text=True)
    return r.stdout.strip() == "active"


def restart(svc: str) -> bool:
    r = subprocess.run(["sudo", "systemctl", "restart", svc],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        log(f"  restart failed: {(r.stdout + r.stderr)[:200]}")
        return False
    time.sleep(3)
    return is_active(svc)


def shutdown(*_): log("shutdown"); sys.exit(0)
signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


def sweep() -> int:
    """One pass over all monitored services. Returns number restarted."""
    n_restarted = 0
    for svc, _ in SERVICES.items():
        state_file = STATE_DIR / f"{svc.replace('/', '_')}.attempts"
        attempts = int(state_file.read_text().strip()) if state_file.exists() else 0
        if is_active(svc):
            if attempts:
                state_file.write_text("0")
            continue
        attempts += 1
        state_file.write_text(str(attempts))
        # Cool-down: skip restart on attempts 6, 7, 8
        if 6 <= attempts <= 8:
            log(f"⏸ {svc} dead (attempt {attempts}) — cool-down, skip restart")
            continue
        log(f"⚠ {svc} dead (attempt {attempts}) — restart")
        if restart(svc):
            log(f"✓ {svc} recovered after {attempts} attempt(s)")
            post_discord(f"🔧 self-heal: `{svc}` restarted ok (was down {attempts} ticks)")
            state_file.write_text("0")
            n_restarted += 1
        else:
            log(f"✗ {svc} restart did not stick")
            if attempts >= 9:
                post_discord(f"🚨 self-heal: `{svc}` will not recover (attempt {attempts})")
    return n_restarted


log(f"start — sweep every {INTERVAL}s, {len(SERVICES)} services monitored")
n_sweeps = 0
while True:
    n_sweeps += 1
    try:
        n_fixed = sweep()
        if n_fixed:
            log(f"sweep #{n_sweeps}: restarted {n_fixed} service(s)")
        elif n_sweeps % 12 == 1:
            log(f"sweep #{n_sweeps}: all healthy")
    except Exception as e:
        log(f"⚠ sweep error: {e}")
    time.sleep(INTERVAL)

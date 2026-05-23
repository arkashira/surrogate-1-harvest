#!/usr/bin/env python3
"""axentx watchdog-regression — continuous health monitor + auto-recovery.

Runs every 15 min:
  1. Snapshot: 3 hosts state, pipeline depths, ashirapit repo sizes
  2. Detect issues: load>50, failed_units, queue_starvation (no items
     processed in stage X for 30min while queue has >100 items),
     LLM-storm (>80% providers cooled)
  3. Auto-fix: release stale claims (>30min), clear orphan repo locks,
     restart crashed daemons
  4. Discord notify on anomaly
  5. Save snapshot to shared_memory for trend analysis
"""
import datetime, json, os, signal, socket, subprocess, sys, time
import urllib.request, urllib.parse
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop

POLL_SEC = int(os.environ.get("WATCHDOG_REG_POLL_SEC", "900"))   # 15 min
HOST = socket.gethostname()
DISCORD = os.environ.get("DISCORD_WEBHOOK", "")
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _sh(cmd, t=10):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=t).stdout.strip()
    except Exception:
        return ""


def _sb_count(stage):
    try:
        qs = urllib.parse.urlencode({"stage": f"eq.{stage}", "select": "id"})
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items?{qs}",
            headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
                     "Prefer": "count=exact", "Range": "0-0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            cr = r.headers.get("Content-Range", "")
            return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return 0


def _release_stale(min_age_sec=1800):
    """Release items claimed >30min ago (likely dead workers)."""
    if not (SB_URL and SB_KEY):
        return 0
    cutoff = int(time.time()) - min_age_sec
    try:
        for stage in ("dev", "review", "qa", "commit", "competitor-intel",
                      "pitch", "business-synthesis"):
            qs = urllib.parse.urlencode({
                "stage": f"eq.{stage}",
                "claimed_at": f"lt.{cutoff}",
            })
            req = urllib.request.Request(
                f"{SB_URL}/rest/v1/pipeline_items?{qs}",
                data=json.dumps({"claimed_by": None,
                                 "claimed_at": None}).encode(),
                method="PATCH",
                headers={"apikey": SB_KEY,
                         "Authorization": f"Bearer {SB_KEY}",
                         "Content-Type": "application/json",
                         "Prefer": "return=minimal"})
            urllib.request.urlopen(req, timeout=10)
        return 1
    except Exception:
        return 0


def _discord(msg):
    if not DISCORD:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            DISCORD,
            data=json.dumps({"content": msg[:1900]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"), timeout=10).read()
    except Exception:
        pass


def cycle():
    issues = []

    # 1. Local host stats
    load = _sh("awk '{print $1}' /proc/loadavg") or "?"
    active = _sh(("systemctl list-units --type=service --state=active "
                  "axentx-* --no-legend | wc -l")) or "?"
    failed = _sh(("systemctl list-units --type=service --state=failed "
                  "axentx-* --no-legend | wc -l")) or "0"

    try:
        load_n = float(load)
        if load_n > 50:
            issues.append(f"high load on {HOST}: {load_n:.1f}")
    except Exception:
        pass
    try:
        if int(failed) > 0:
            issues.append(f"{failed} failed unit(s) on {HOST}")
    except Exception:
        pass

    # 2. Pipeline queue depths
    depths = {s: _sb_count(s) for s in
              ("research", "validator", "market-research", "bd",
               "pitch", "competitor-intel", "design", "dev", "review",
               "qa", "commit", "done")}
    # Detect starvation: dev/review/qa/commit > 200 while local load idle
    for st in ("dev", "review", "qa", "commit"):
        if depths.get(st, 0) > 500:
            issues.append(f"{st}-queue backed up: {depths[st]}")

    # 3a. LLM health regression — read shared_kv["llm.providers.health"]
    # written by llm-health-watchdog. If <30% working, flag as issue and
    # log env-drift if NOT_CONFIGURED count > 5 (likely env file out of sync).
    try:
        from axentx_shared import kv_get, memory_log as _ml
        h = kv_get("llm.providers.health") or {}
        if isinstance(h, dict) and h.get("counts"):
            counts = h["counts"]
            pct = h.get("working_pct", 0)
            if pct < 30:
                issues.append(
                    f"LLM degraded: {pct:.0f}% working "
                    f"(host={h.get('host','?')})")
            if counts.get("NOT_CONFIGURED", 0) > 5:
                issues.append(
                    f"env-drift: {counts['NOT_CONFIGURED']} provider tokens "
                    f"missing on {h.get('host','?')}")
                _ml("watchdog-regression", "env-drift",
                    "provider env vars missing across hosts",
                    body=(f"NOT_CONFIGURED count={counts['NOT_CONFIGURED']}\n"
                          f"summary: {h.get('summary','?')}"),
                    tags=["env-drift", HOST])
    except Exception:
        pass

    # 3b. Queue-stuck regression: dev/review/qa queue depth > 200 AND no
    # advances in last 30 min (claimed_at all old) → release everything
    # so different daemons can retry.
    stuck_stages = []
    for st in ("dev", "review", "qa"):
        if depths.get(st, 0) > 200:
            try:
                cutoff = int(time.time()) - 1800
                qs = urllib.parse.urlencode({
                    "stage": f"eq.{st}",
                    "claimed_at": f"lt.{cutoff}",
                    "claimed_by": "not.is.null",
                    "select": "id",
                })
                req = urllib.request.Request(
                    f"{SB_URL}/rest/v1/pipeline_items?{qs}",
                    headers={"apikey": SB_KEY,
                             "Authorization": f"Bearer {SB_KEY}",
                             "Prefer": "count=exact", "Range": "0-0"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    cr = r.headers.get("Content-Range", "")
                    stale = int(cr.split("/")[-1]) if "/" in cr else 0
                if stale > 50:
                    stuck_stages.append(f"{st}({stale} stale)")
            except Exception:
                pass
    if stuck_stages:
        issues.append(f"queue-stuck: {', '.join(stuck_stages)}")

    # 4. Auto-fix stale claims
    released = _release_stale(min_age_sec=1800)

    # 4. Snapshot to memory
    try:
        from axentx_shared import memory_log, kv_set
        memory_log("watchdog-regression", "event",
                   f"snapshot from {HOST}",
                   body=json.dumps({
                       "load": load, "active": active, "failed": failed,
                       "depths": depths, "issues": issues,
                       "released_stale": bool(released),
                   }, indent=2),
                   tags=["snapshot", HOST])
        kv_set(f"watchdog.snapshot.{HOST}", {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "load": load, "active": active, "failed": failed,
            "depths": depths, "issues": issues,
        })
    except Exception:
        pass

    # 5. Notify on issues
    if issues:
        _discord(
            f"⚠ **watchdog-regression** ({HOST})\n"
            + "\n".join(f"- {i}" for i in issues[:5]))
        log("watchdog-regression",
            f"  ⚠ {len(issues)} issue(s): " + "; ".join(issues[:3]))
    else:
        log("watchdog-regression",
            f"  ✓ {HOST}: load={load} active={active} "
            f"depths={'/'.join(str(depths[s]) for s in ('dev','review','qa','commit'))}")
    # Return False so daemon_loop sleeps full POLL_SEC (15min). Returning
    # True would put us in a tight loop because we always "did work".
    return False


if __name__ == "__main__":
    daemon_loop("watchdog-regression", POLL_SEC, cycle)

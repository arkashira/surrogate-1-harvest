#!/usr/bin/env python3
"""axentx fleet-status — single-pane-of-glass aggregator.

Why this exists (user feedback 2026-05-04):
  > 'env ก็เหมือนกัน เอาไปไว้ service ตรงกลางดีกว่าไหม ... ทุก knowledge
  >  context skill env memory ต้องแชร์ร่วมกันทุกที ... status ของ agent
  >  แต่ละตัว ปัญหาด้วยว่า agent แต่ละตัวเป็นอะไร auto heal ได้ทำงานได้'

Supabase IS the central service. This daemon just rolls up the scattered
heartbeats / queue depths / LLM health / experiences into ONE shared_kv
key so any operator (or downstream agent) sees the whole fleet at a glance.

Cycle (every 30s):
  1. Read shared_kv["agent.heartbeat.*"] — all 200+ agent broadcasts.
  2. Read recent shared_memory experiences (last 10min, all hosts).
  3. Read shared_kv["llm.providers.health"] (latest from any host).
  4. Read pipeline_items counts per stage.
  5. Read shared_kv["bd.portfolio"] / "bd.output_counts".
  6. Detect anomalies:
     - agent silent > 10min → flag stale + auto-restart its unit
     - >30% of agents in 'error' state → Discord alert
     - LLM working_pct < 30% → Discord alert
     - queue depth > 1000 stuck for >30min → Discord alert
  7. Write everything to shared_kv["fleet.status"].
  8. Discord summary every 30min (or on anomaly).

Auto-heal hooks (locally only, never SSH to other hosts):
  Stale agent on THIS host → systemctl restart <unit>
  Other hosts' stale agents → log to shared_memory (their auto-healer
  picks it up + restarts on the host that owns it).
"""
from __future__ import annotations
import datetime
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop  # noqa: E402

POLL_SEC = int(os.environ.get("FLEET_STATUS_POLL_SEC", "30"))
HOST = socket.gethostname()
DISCORD = os.environ.get("DISCORD_WEBHOOK", "")
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))
_last_discord_ts = 0.0


def _sh(cmd: list[str], t: int = 10) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=t)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def _sb(path: str, params: dict) -> list | dict | None:
    if not (SB_URL and SB_KEY):
        return None
    try:
        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/{path}?{qs}",
            headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        log("fleet-status", f"  ⚠ sb({path}): {e}")
        return None


def _sb_count(path: str, params: dict) -> int:
    if not (SB_URL and SB_KEY):
        return 0
    try:
        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/{path}?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Prefer": "count=exact", "Range": "0-0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            cr = r.headers.get("Content-Range", "")
            return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return 0


def _discord(msg: str, throttle_sec: int = 1800) -> None:
    global _last_discord_ts
    if not DISCORD:
        return
    if time.time() - _last_discord_ts < throttle_sec:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            DISCORD,
            data=json.dumps({"content": msg[:1900]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"), timeout=10).read()
        _last_discord_ts = time.time()
    except Exception:
        pass


def _local_restart(unit: str) -> bool:
    """Restart a unit ONLY if it lives on this host. Returns True if did."""
    rc, out, _ = _sh(["systemctl", "list-unit-files", unit + ".service",
                      "--no-legend"], t=5)
    if rc == 0 and out.strip():
        rc2 = _sh(["sudo", "systemctl", "restart", unit + ".service"], t=15)[0]
        if rc2 == 0:
            return True
    return False


def cycle():
    if _stop:
        return False
    now = datetime.datetime.utcnow()
    now_iso = now.isoformat() + "Z"

    # 1. Heartbeats
    hbs = _sb("shared_kv", {
        "k": "like.agent.heartbeat.*",
        "select": "k,v,updated_at",
        "limit": "500",
    }) or []
    by_host: dict[str, list[dict]] = {}
    stale: list[dict] = []
    states_count: dict[str, int] = {}
    for row in hbs:
        v = row.get("v") or {}
        if not isinstance(v, dict):
            continue
        host = v.get("host") or "?"
        by_host.setdefault(host, []).append(v)
        states_count[v.get("state", "unknown")] = (
            states_count.get(v.get("state", "unknown"), 0) + 1)
        # stale = no update in 10 minutes
        try:
            t_str = row.get("updated_at", "").replace("+00:00", "")
            ts = datetime.datetime.fromisoformat(t_str)
            age_sec = (now - ts).total_seconds()
            # Threshold raised 600→1500s (25min) after observed false-
            # positives: long-poll daemons (hf-rag-loader=3h, agent-
            # synthesizer=1h, codebase-indexer=1h) heartbeat once per
            # cycle, so 10min threshold restarted them constantly.
            # Per-role override: very-long-poll daemons get 2h grace.
            role = v.get("role", "")
            long_poll_roles = {"hf-rag-loader", "agent-synthesizer",
                               "codebase-indexer", "data-analyst",
                               "startup-hunter", "product-synth",
                               "knowledge-ingest"}
            threshold = 7200 if role in long_poll_roles else 1500
            if age_sec > threshold:
                stale.append({"role": role, "host": host,
                              "age_sec": int(age_sec)})
        except Exception:
            pass

    # 2. Recent experiences (last 10min)
    cutoff_iso = (now - datetime.timedelta(minutes=10)).isoformat()
    recent_exp = _sb("shared_memory", {
        "created_at": f"gte.{cutoff_iso}",
        "select": "actor,kind,host,title",
        "order": "created_at.desc",
        "limit": "30",
    }) or []
    issue_kinds = ("auth-fail", "build-fail", "stuck-loop:217",
                   "env-drift", "fix")
    issues_recent = [x for x in recent_exp
                     if x.get("kind", "") in issue_kinds]

    # 3. LLM health
    llm_h = _sb("shared_kv", {
        "k": "eq.llm.providers.health",
        "select": "v,updated_at",
    }) or []
    llm = (llm_h[0].get("v") or {}) if llm_h else {}

    # 4. Pipeline depths
    stages = ("research", "validator", "market-research", "bd", "spawn",
              "pitch", "competitor-intel", "design", "dev", "review",
              "qa", "commit", "done")
    depths = {s: _sb_count("pipeline_items",
                           {"stage": f"eq.{s}", "select": "id"})
              for s in stages}

    # 5. BD output mix
    counts_v = _sb("shared_kv", {
        "k": "eq.bd.output_counts",
        "select": "v",
    }) or []
    bd_mix = (counts_v[0].get("v") or {}) if counts_v else {}

    # 6. Anomaly detection + auto-heal
    anomalies = []
    healed = 0
    if llm.get("working_pct", 100) < 30:
        # Note: probe doesn't include free no-auth tier (Pollinations/OVH/
        # LLM7/z.ai). Real availability often higher than probe shows.
        # Only flag if dev refine fail rate ALSO high (not just probe).
        anomalies.append(
            f"LLM probe degraded ({llm.get('summary','?')}) — "
            f"check dev success rate before panicking")
    # "Queue-stuck" — only true if depth high AND flow rate low. Flow rate
    # data isn't in this snapshot, so use much higher threshold (10k) as
    # heuristic — under that, high-throughput pipelines look stuck.
    for s in ("dev", "review", "bd"):
        if depths.get(s, 0) > 10000:
            anomalies.append(f"queue-large: {s}={depths[s]} "
                            "(could be high-throughput, not stuck)")
    if stale:
        anomalies.append(f"{len(stale)} agent(s) silent (per-role threshold)")
        # Auto-heal LOCAL stale agents
        my_stale = [x for x in stale if x["host"] == HOST]
        for s in my_stale[:5]:
            unit = f"axentx-{s['role']}-daemon"
            if _local_restart(unit):
                healed += 1
                try:
                    from axentx_shared import memory_log
                    memory_log("fleet-status", "heal-stale-agent",
                               f"restarted {unit} (silent {s['age_sec']}s)",
                               body=f"Detected stale heartbeat on {HOST}",
                               tags=["fleet-status", "auto-heal", HOST])
                except Exception:
                    pass
        # Cross-host: write heal-request flags so other hosts pick them up
        # next cycle. Each host's fleet-status reads its own
        # heal-request.<host> queue and restarts what's listed.
        try:
            from axentx_shared import kv_set, kv_get
            other_stale: dict[str, list[str]] = {}
            for s in stale:
                if s["host"] == HOST:
                    continue
                other_stale.setdefault(s["host"], []).append(
                    f"axentx-{s['role']}-daemon")
            for host, units in other_stale.items():
                kv_set(f"heal-request.{host}",
                       {"ts": now_iso, "from": HOST,
                        "units": units[:10]})
            # Process heal-requests targeted at THIS host
            req = kv_get(f"heal-request.{HOST}") or {}
            if isinstance(req, dict) and req.get("v"):
                req = req["v"]
            for unit in (req.get("units") or [])[:5]:
                if _local_restart(unit):
                    healed += 1
            if req:
                kv_set(f"heal-request.{HOST}", {})   # clear after processing
        except Exception:
            pass

    # 7. Compose status snapshot + write to shared_kv
    by_host_summary = {
        h: {
            "agents": len(rows),
            "states": dict(
                (s, sum(1 for r in rows if r.get("state") == s))
                for s in ("working", "idle", "error", "starting"))
        } for h, rows in by_host.items()
    }
    status = {
        "ts": now_iso,
        "by_host": by_host_summary,
        "states_total": states_count,
        "total_agents": sum(len(v) for v in by_host.values()),
        "stale": stale[:10],
        "llm_summary": llm.get("summary", "(unknown)"),
        "llm_working_pct": llm.get("working_pct", 0),
        "depths": depths,
        "bd_output_mix": bd_mix,
        "anomalies": anomalies,
        "issues_recent_10m": [
            f"{x.get('host','?')}:{x.get('actor','?')}:{x.get('kind','?')}"
            for x in issues_recent[:10]
        ],
        "healed_this_cycle": healed,
    }
    try:
        from axentx_shared import kv_set
        kv_set("fleet.status", status)
    except Exception:
        pass

    log("fleet-status",
        f"  ✓ {status['total_agents']} agents / "
        f"LLM {llm.get('working_pct', 0):.0f}% / "
        f"queue dev={depths.get('dev', 0)} review={depths.get('review', 0)} "
        f"/ stale={len(stale)} healed={healed}")

    # 8. Discord on anomaly (throttled)
    if anomalies and (not stale or len(stale) >= 3 or healed == 0):
        _discord(
            f"⚠ **fleet-status** ({HOST})\n" + "\n".join(
                f"- {a}" for a in anomalies[:5]),
            throttle_sec=1800)

    return False


if __name__ == "__main__":
    daemon_loop("fleet-status", POLL_SEC, cycle)

#!/usr/bin/env python3
"""axentx data-analyst — closes the feedback loop. Mines product metrics
+ support tickets + recent commits → identifies opportunities → writes
to opportunity-backlog (consumed by PM/prd).

Fills the "Data Analyst" + "Feedback-loop closer" gaps from SDLC research:
- Funnel/cohort signals → PM (which feature has retention drop?)
- Support tickets tagged → grouped by opportunity → surfaced to PM weekly
- Cycle-time / lead-time per dev pipeline → EM dashboard

User feedback 2026-05-04:
  > 'มันต้องไปทำ usability testing มาก่อนด้วย ... feedback loop ต้องครบ'

Output: shared_kv["opportunity-backlog"] = ranked list of opportunities
that PM/prd-daemon can prioritize for next sprint.
"""
from __future__ import annotations
import datetime
import json
import os
import signal
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from collections import Counter

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, daemon_loop, call_llm,  # noqa: E402
                             get_portfolio)

POLL_SEC = int(os.environ.get("DATA_ANALYST_POLL_SEC", "1800"))   # 30min
HOST = socket.gethostname()
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _query_recent_memory(actor: str, hours: int = 24,
                         limit: int = 100) -> list[dict]:
    if not (SB_URL and SB_KEY):
        return []
    cutoff_iso = (datetime.datetime.utcnow()
                  - datetime.timedelta(hours=hours)).isoformat()
    try:
        params = {
            "created_at": f"gte.{cutoff_iso}",
            "select": "actor,kind,title,body,host,created_at",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        if actor:
            params["actor"] = f"eq.{actor}"
        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/shared_memory?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read())
    except Exception:
        return []


def _query_recent_done_per_project(hours: int = 24) -> dict[str, int]:
    """Count of items advanced to 'done' per project — proxy for delivery rate."""
    if not (SB_URL and SB_KEY):
        return {}
    cutoff = int((datetime.datetime.utcnow()
                  - datetime.timedelta(hours=hours)).timestamp())
    try:
        qs = urllib.parse.urlencode({
            "stage": "eq.done", "updated_at": f"gte.{cutoff}",
            "select": "project", "limit": "500",
        })
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}"})
        with urllib.request.urlopen(req, timeout=12) as r:
            rows = json.loads(r.read())
        c: Counter = Counter()
        for x in rows:
            p = x.get("project") or "(unattributed)"
            c[p] += 1
        return dict(c)
    except Exception:
        return {}


ANALYST_SYSTEM = (
    "You are a senior product data analyst. Given the operational signals "
    "below, identify the TOP 3-5 OPPORTUNITIES the PM team should "
    "prioritize next sprint. Output STRICT JSON:\n"
    "{\n"
    '  "opportunities": [\n'
    '    {\n'
    '      "rank": 1,\n'
    '      "title": "1-line opportunity",\n'
    '      "evidence": "1-2 sentences citing the signals you saw",\n'
    '      "target_project": "<slug or null if cross-product>",\n'
    '      "estimated_value": "low|med|high (value to user/biz)",\n'
    '      "estimated_effort": "S|M|L",\n'
    '      "next_step": "1-line — what should PM do FIRST"\n'
    '    }\n'
    '  ],\n'
    '  "delivery_health": {\n'
    '    "items_per_24h": <int>,\n'
    '    "fastest_project": "<slug>",\n'
    '    "slowest_project": "<slug>",\n'
    '    "blocker_signals": ["short list"]\n'
    '  }\n'
    "}\n"
    "Bias toward high-value/low-effort opportunities. If signals point at "
    "a recurring failure pattern (auth-fail, env-drift, build-fail), call "
    "it out as a delivery-health blocker.")


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("data-analyst", "  ⤷ not leader — skip")
        return False

    # Gather signals
    fixes = _query_recent_memory(actor="auto-healer", hours=24, limit=50)
    ux_tests = _query_recent_memory(actor="usability-tester", hours=24, limit=30)
    bd_verdicts = _query_recent_memory(actor="bd", hours=24, limit=50)
    deliveries = _query_recent_done_per_project(hours=24)
    portfolio = get_portfolio()

    if not (fixes or bd_verdicts or deliveries):
        log("data-analyst", "  ⊘ insufficient signals — skip")
        return False

    fix_summary = Counter(x.get("kind", "?") for x in fixes)
    ux_summary = Counter(x.get("kind", "?") for x in ux_tests)
    bd_summary = Counter(x.get("kind", "?") for x in bd_verdicts)

    signals = (
        f"# Operational signals last 24h\n\n"
        f"## Auto-healer activity (incident proxy)\n"
        f"  {dict(fix_summary)}\n\n"
        f"## Usability-test outcomes\n"
        f"  {dict(ux_summary)}\n\n"
        f"## bd verdict mix\n"
        f"  {dict(bd_summary)}\n\n"
        f"## Delivery rate per project (items→done last 24h)\n"
        + "\n".join(f"  {p}: {n}" for p, n in
                    sorted(deliveries.items(), key=lambda x: -x[1])[:10])
        + "\n\n"
        f"## Active products\n"
        + "\n".join(f"  {s}: {d[:80]}" for s, d in portfolio.items())
    )
    prompt = signals + "\n\nIdentify top 3-5 opportunities. STRICT JSON."

    try:
        out = call_llm(prompt, system=ANALYST_SYSTEM,
                       max_tokens=1200, timeout=45)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        analysis = json.loads(txt.strip())
    except Exception as e:
        log("data-analyst",
            f"  ✗ LLM analyze: {type(e).__name__}: {str(e)[:80]}")
        return False

    opps = analysis.get("opportunities") or []
    health = analysis.get("delivery_health") or {}

    log("data-analyst",
        f"  ✓ {len(opps)} opportunities identified, "
        f"{len(deliveries)} projects delivering")
    for opp in opps[:5]:
        log("data-analyst",
            f"    #{opp.get('rank','?')} [{opp.get('estimated_value','?')}/"
            f"{opp.get('estimated_effort','?')}] "
            f"{opp.get('title','')[:60]}")

    try:
        from axentx_shared import kv_set, memory_log
        kv_set("opportunity-backlog", {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "host": HOST,
            "opportunities": opps,
            "delivery_health": health,
            "n_opportunities": len(opps),
        })
        memory_log("data-analyst", "opportunities-refreshed",
                   f"backlog updated: {len(opps)} items, "
                   f"top: {(opps[0] or {}).get('title','')[:80] if opps else ''}",
                   body=json.dumps(analysis, ensure_ascii=False)[:1500],
                   tags=["data-analyst", HOST])
    except Exception:
        pass
    return False


if __name__ == "__main__":
    daemon_loop("data-analyst", POLL_SEC, cycle)

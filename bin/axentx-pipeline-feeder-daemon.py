#!/usr/bin/env python3
"""axentx pipeline-feeder — universal feeder: fills ANY idle pipeline stage
with synthetic-but-relevant work so no agent is ever idle.

User feedback 2026-05-04:
  > 'มันไม่ควรมีใครว่างงงงงงง — ต้องมี task ที่ ux/design/architect ทำ
  >  ตลอดเวลา. บาง task เกิดขึ้นพร้อมกันได้'

Idea: most existing synthesizers (feature-synth, product-synth, plan-miner,
usability-tester) feed only ONE stage. This daemon is the catch-all that
watches the WHOLE pipeline and ensures every stage stays warm.

For each stage that's underloaded relative to its target, generate work
that's appropriate for the stage:

  ux        — pick a product without recent ux artifact → "design user flow for X"
  design    — pick a product / feature → "design-thinking session for X"
  architect — pick a product → "draft ADR for next architectural decision"
  prd       — pick a product → "extract next sprint of stories from BMC"
  pitch     — pick a recent NEW-PRODUCT bd-verdict → re-pitch
  competitor-intel — pick a product → "analyze competitors for X"
  business-synthesis — pick a recently-spawned project missing BMC

Triggers parallel work across the pipeline. Frontend dev (via ux output),
backend dev (via architect output), CI/CD dev (via separate dev-tasks)
all happen concurrently because the spec stages keep producing in
parallel — not waiting for sequential bd → spawn → … chain.

Cycle: 90s tick (cheap). Leader=GCP. Discipline: never overfill (cap per
stage = target × 1.5).
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import signal
import socket
import sys
import time as _t
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, daemon_loop,  # noqa: E402
                             get_portfolio, get_portfolio_block)

POLL_SEC = int(os.environ.get("PIPELINE_FEEDER_POLL_SEC", "90"))
HOST = socket.gethostname()
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))

# Per-stage minimum queue depth — feeder fills below this.
STAGE_TARGETS = {
    "ux": 5, "design": 5, "architect": 5, "prd": 5,
    "competitor-intel": 3, "business-synthesis": 3, "pitch": 3,
}

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _stage_depth(stage: str) -> int:
    if not (SB_URL and SB_KEY): return -1
    try:
        qs = urllib.parse.urlencode({"stage": f"eq.{stage}", "select": "id"})
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Prefer": "count=exact", "Range": "0-0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            cr = r.headers.get("Content-Range", "")
            return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return -1


def _last_feed_age_h(stage: str, slug: str) -> float:
    try:
        from axentx_shared import kv_get
        v = kv_get(f"pipeline-feeder.{stage}.{slug}") or {}
        if isinstance(v, dict) and v.get("v"): v = v["v"]
        if not isinstance(v, dict) or not v.get("ts"): return 99.0
        ts = datetime.datetime.fromisoformat(v["ts"].replace("Z", "+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        return 99.0


def _record_feed(stage: str, slug: str) -> None:
    try:
        from axentx_shared import kv_set
        kv_set(f"pipeline-feeder.{stage}.{slug}", {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "host": HOST,
        })
    except Exception:
        pass


def push_synthetic(stage: str, slug: str, brief: str,
                   focus: str = "feeder-synth") -> bool:
    """Insert a pipeline_item at the given stage with synthetic prompt."""
    fid = (f"20260504-feeder-{stage}-{slug}-"
           f"{hashlib.md5(brief.encode()).hexdigest()[:10]}")
    payload = {
        "id": fid, "stage": stage, "project": slug, "focus": focus,
        "history": [{
            "stage": "pipeline-feeder",
            "actor": "axentx-pipeline-feeder",
            "output": brief[:600],
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {"text": brief},
        "auto_synthesized": True, "synthesized_for_stage": stage,
        "target_project": slug, "project": slug,
    }
    body = {"id": fid, "stage": stage, "project": slug, "focus": focus,
            "payload": payload}
    try:
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        log("pipeline-feeder", f"  ✗ push {fid}: {e}")
        return False


# Per-stage prompt templates. Each takes (slug, desc) → brief text that
# goes into item.current.text + the stage daemon picks it up.
STAGE_BRIEFS = {
    "ux": (lambda slug, desc:
           f"Design end-to-end user flow for the next core journey of "
           f"`{slug}`. Product context: {desc[:200]}\n\n"
           f"Output: customer-journey.md update + 3 screen wireframes "
           f"(text descriptions) + Thai-language UX copy where audience is Thai."),
    "design": (lambda slug, desc:
               f"Run a design-thinking session (5-whys + JTBD) for the "
               f"next high-impact feature of `{slug}`. Context: {desc[:200]}\n\n"
               f"Output STRICT JSON with: jtbd, persona, problem-statement, "
               f"opportunity-area, proposed-solution-shape."),
    "architect": (lambda slug, desc:
                  f"Draft the next ADR for `{slug}` covering its most-pressing "
                  f"unresolved architectural decision (e.g. queue choice, "
                  f"DB scaling pattern, auth strategy). Context: {desc[:200]}\n\n"
                  f"Output STRICT JSON: title, context, options[], decision, "
                  f"consequences, status=proposed."),
    "prd": (lambda slug, desc:
            f"Write the next sprint PRD for `{slug}`. Pull from BMC + "
            f"customer-journey + user-stories. Output STRICT JSON: epics[] "
            f"each with stories[] each with tasks[] (title, files[], "
            f"acceptance[], complexity=S|M|L). Context: {desc[:200]}"),
    "competitor-intel": (lambda slug, desc:
                         f"Analyze top competitors for `{slug}` — Thai market "
                         f"and global. Output STRICT JSON: competitors_th[3], "
                         f"competitors_global[3], wedge, risk. Context: {desc[:200]}"),
    "business-synthesis": (lambda slug, desc:
                            f"Generate / refresh the business pack for `{slug}` "
                            f"if missing or stale (BMC + revenue-model + "
                            f"marketing-plan + breakeven). Context: {desc[:200]}"),
    "pitch": (lambda slug, desc:
              f"Re-pitch `{slug}` to the 3-persona panel — incubator + "
              f"investor + customer. Context: {desc[:200]}\n\n"
              f"Use latest BMC. Verdict: GO|PIVOT|NO-GO."),
}


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("pipeline-feeder", "  ⤷ not leader — skip")
        return False

    portfolio = get_portfolio()
    if not portfolio:
        log("pipeline-feeder", "  ⊘ portfolio empty — skip")
        return False

    # Skip legacy/archived
    products = [(s, d) for s, d in portfolio.items()
                if s not in {"arkship", "cost-radar"} and d]
    if not products:
        return False

    pushed_total = 0
    for stage, target in STAGE_TARGETS.items():
        depth = _stage_depth(stage)
        if depth < 0:
            continue
        if depth >= target:
            continue
        need = min(target - depth, 3)   # max 3 per stage per cycle
        # Round-robin: pick products that haven't been fed THIS stage in 6h
        cutoff_h = 6
        candidates = [(s, d) for s, d in products
                      if _last_feed_age_h(stage, s) > cutoff_h]
        if not candidates:
            continue
        brief_fn = STAGE_BRIEFS.get(stage)
        if not brief_fn:
            continue
        log("pipeline-feeder",
            f"▸ {stage} depth={depth} target={target}, feeding {min(need, len(candidates))}")
        for slug, desc in candidates[:need]:
            brief = brief_fn(slug, desc)
            if push_synthetic(stage, slug, brief, focus="feeder-" + stage):
                _record_feed(stage, slug)
                pushed_total += 1
                log("pipeline-feeder", f"  ✓ {stage} ← {slug}")

    if pushed_total:
        try:
            from axentx_shared import memory_log
            memory_log("pipeline-feeder", "fed-stages",
                       f"pushed {pushed_total} synthetic items to idle stages",
                       tags=["pipeline-feeder", HOST])
        except Exception:
            pass
    return False


if __name__ == "__main__":
    daemon_loop("pipeline-feeder", POLL_SEC, cycle)

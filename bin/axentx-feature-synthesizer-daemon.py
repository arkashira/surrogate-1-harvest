#!/usr/bin/env python3
"""axentx feature-synthesizer — event-driven: reacts to dev-queue-low
signal published by fleet-status, NOT a fixed schedule.

User feedback 2026-05-04:
  > 'ทั้งหมด ทำงานเป็น stream คุยกันผ่าน queue ทำไมมีการทำงานเป็น schedule
  >  อยู่อีก'

Trigger model (event-driven via shared_kv):
  - fleet-status (every 30s) computes dev queue depth and publishes:
       shared_kv["events.dev-queue"] = {depth, target, ts}
  - This daemon polls that event-key on a 60s tick (cheap), fires synth
    IMMEDIATELY when depth < target. No fixed 10-min schedule.
  - When fleet-status hasn't published recently OR depth >= target,
    we sleep — costs only 1 supabase read per minute.

This pattern: queue-pull triggered by upstream event, not a cron timer.

Cycle (60s tick — but only DOES work when triggered):
  1. Read fleet.status → if dev queue > 50 → idle (don't pile up)
  2. Read shared_kv["bd.portfolio"] → list of products
  3. For each product (round-robin, leader=GCP), look at:
     - existing /opt/axentx/<slug>/business/* docs (read summary)
     - existing /opt/axentx/<slug>/specs/*.md (count PRDs)
     - shared_memory recent kind=milestone for this project
  4. Use LLM (broad pool) to propose ONE next-most-valuable feature:
     prompt = product BMC + recent milestones + 'what's missing?'
  5. Inject as new pipeline_item:
     stage='design'  (skips bd — we already know it's an EXTEND of <slug>)
     bd_verdict={"verdict":"EXTEND","target_project":"<slug>",
                 "feature_one_liner":"<...>","output_mode":"extend-main"}
  6. memory_log "synthesized-feature" so other agents see what was added.

Discipline:
  - Only synthesize when dev queue < 30 (avoid pile-up)
  - Max 1 feature per product per 24h (don't over-extend single product)
  - Leader-only (GCP = lowest hostname) so we don't 3x-synth
  - Skip if LLM broad pool < 30% working (don't burn marginal capacity)
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import signal
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, daemon_loop, call_llm,  # noqa: E402
                             get_portfolio, get_portfolio_block)

# Tick = how often we check the event-key. Don't confuse with fixed
# "synthesize every X" — actual synth only fires when event says queue low.
POLL_SEC = int(os.environ.get("FEATURE_SYNTH_POLL_SEC", "60"))   # 60s tick
HOST = socket.gethostname()
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))

DEV_QUEUE_TARGET = int(os.environ.get("DEV_QUEUE_TARGET", "30"))
DEV_QUEUE_HARD_CAP = int(os.environ.get("DEV_QUEUE_HARD_CAP", "100"))

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _sb(path: str, params: dict, method: str = "GET",
        data: bytes | None = None) -> dict | list | None:
    if not (SB_URL and SB_KEY):
        return None
    try:
        qs = urllib.parse.urlencode(params)
        url = f"{SB_URL}/rest/v1/{path}"
        if qs:
            url += f"?{qs}"
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": ("return=minimal" if method == "POST"
                                else "count=exact")})
        with urllib.request.urlopen(req, timeout=15) as r:
            try:
                return json.loads(r.read())
            except Exception:
                return None
    except Exception as e:
        log("feature-synth", f"  ⚠ sb({method} {path}): {e}")
        return None


def _dev_queue_depth() -> int:
    if not (SB_URL and SB_KEY):
        return -1
    try:
        qs = urllib.parse.urlencode({"stage": "eq.dev", "select": "id"})
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pipeline_items?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}",
                     "Prefer": "count=exact", "Range": "0-0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            cr = r.headers.get("Content-Range", "")
            return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return -1


def _llm_health_pct() -> int:
    try:
        from axentx_shared import kv_get
        h = kv_get("llm.providers.health") or {}
        if isinstance(h, dict) and h.get("v"):
            h = h["v"]
        return int(h.get("working_pct", 0)) if isinstance(h, dict) else 0
    except Exception:
        return 0


def _read_summary(repo: Path, fname: str, limit: int = 600) -> str:
    p = repo / "business" / fname
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except Exception:
        return ""


def _last_synth_time(slug: str) -> int:
    """epoch of last synth for this slug (0 if never)."""
    try:
        from axentx_shared import kv_get
        v = kv_get(f"feature-synth.last.{slug}")
        if isinstance(v, dict) and v.get("v"):
            v = v["v"]
        return int(v.get("ts", 0)) if isinstance(v, dict) else 0
    except Exception:
        return 0


def _record_synth(slug: str, feature: str) -> None:
    try:
        from axentx_shared import kv_set, memory_log
        kv_set(f"feature-synth.last.{slug}", {
            "ts": int(datetime.datetime.utcnow().timestamp()),
            "host": HOST,
            "feature_one_liner": feature[:240],
        })
        memory_log("feature-synth", "synthesized-feature",
                   f"new feature for {slug}: {feature[:120]}",
                   body=feature, tags=["feature-synth", slug, HOST])
    except Exception:
        pass


def _push_design_item(slug: str, feature: str) -> bool:
    """Insert a new pipeline_item directly at stage=design with
    bd_verdict pre-set as EXTEND. Skips bd queue entirely — we already
    know which product to extend."""
    fid = (f"20260504-feature-{slug}-"
           f"{hashlib.md5(feature.encode()).hexdigest()[:10]}")
    payload = {
        "id": fid,
        "stage": "design",
        "project": slug,
        "focus": "feature-synth",
        "history": [{
            "stage": "feature-synth",
            "actor": "axentx-feature-synthesizer",
            "output": f"synthesized: {feature[:200]}",
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }],
        "current": {"text": feature},
        "bd_verdict": {
            "verdict": "EXTEND",
            "target_project": slug,
            "feature_one_liner": feature[:240],
            "output_mode": "extend-main",
            "rationale": "auto-synthesized to keep dev queue alive",
            "auto_synthesized": True,
        },
        "target_project": slug,
        "output_mode": "extend-main",
    }
    body = {
        "id": fid,
        "stage": "design",
        "project": slug,
        "focus": "feature-synth",
        "payload": payload,
    }
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
        log("feature-synth", f"  ✗ insert {fid}: {e}")
        return False


def synthesize_for_slug(slug: str, desc: str) -> str | None:
    """Use LLM to propose ONE next-most-valuable feature for this product."""
    repo = PROJECTS_ROOT / slug
    bmc = _read_summary(repo, "business-model-canvas.md", 1200)
    rev = _read_summary(repo, "revenue-model.md", 800)
    sys_prompt = (
        "You are a senior PM for axentx. Given an existing product, "
        "propose THE single most-valuable next feature to ship. Output "
        "STRICT JSON only:\n"
        '{"feature_one_liner": "1-sentence feature description",\n'
        ' "rationale": "why this is the highest-leverage next move",\n'
        ' "estimated_complexity": "S|M|L",\n'
        ' "user_jtbd": "the job-to-be-done this enables"}\n'
        "Rules: must EXTEND the product's value prop, not duplicate "
        "existing functionality, and must be implementable in <2 weeks of "
        "1 dev's work. Don't propose an entirely new product.\n\n"
        + get_portfolio_block()
    )
    prompt = (
        f"# Product: {slug}\n"
        f"Portfolio description: {desc}\n\n"
        f"## BMC excerpt\n{bmc or '(no BMC)'}\n\n"
        f"## Revenue model excerpt\n{rev or '(no revenue model)'}\n\n"
        f"What's the single best next feature to ship? "
        f"STRICT JSON only."
    )
    try:
        out = call_llm(prompt, system=sys_prompt,
                       max_tokens=400, timeout=30)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        v = json.loads(txt.strip())
        return (v.get("feature_one_liner") or "").strip() or None
    except Exception as e:
        log("feature-synth",
            f"  ✗ {slug} synth: {type(e).__name__}: {str(e)[:80]}")
        return None


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("feature-synth", "  ⤷ not leader (GCP owns synth)")
        return False

    depth = _dev_queue_depth()
    if depth < 0:
        log("feature-synth", "  ⚠ can't read queue depth — skip")
        return False
    if depth >= DEV_QUEUE_TARGET:
        log("feature-synth",
            f"  ✓ dev queue {depth} ≥ target {DEV_QUEUE_TARGET} — skip")
        return False
    if depth >= DEV_QUEUE_HARD_CAP:
        log("feature-synth",
            f"  ⛔ dev queue {depth} hit hard cap — skip")
        return False

    health = _llm_health_pct()
    if health < 30:
        log("feature-synth",
            f"  ⤷ LLM {health}% < 30% — skip (don't burn marginal capacity)")
        return False

    portfolio = get_portfolio()
    if not portfolio:
        log("feature-synth", "  ⤷ portfolio empty — skip")
        return False

    # Round-robin: pick products that haven't been synth'd in last 24h
    cutoff = int(datetime.datetime.utcnow().timestamp()) - 86400
    candidates = []
    for slug, desc in portfolio.items():
        if slug in {"arkship"}:   # legacy / merged products — skip
            continue
        if _last_synth_time(slug) < cutoff:
            candidates.append((slug, desc))

    # Prioritize STARVING projects (per user feedback 2026-05-04: 'project ไหน
    # ไม่เหลือ feature ต้องให้ feature-synth ทำงานหา feature มาเติม').
    # starvation-watcher writes shared_kv["project-starvation"] every 60s.
    try:
        from axentx_shared import kv_get
        st = kv_get("project-starvation") or {}
        if isinstance(st, dict) and st.get("v"): st = st["v"]
        starving = set((st.get("starving") or {}).keys())
        if starving:
            # Re-order candidates: starving projects first
            starving_cands = [c for c in candidates if c[0] in starving]
            other_cands = [c for c in candidates if c[0] not in starving]
            candidates = starving_cands + other_cands
            if starving_cands:
                log("feature-synth",
                    f"  ⚡ priority: {len(starving_cands)} starving project(s) "
                    f"first: {[s for s,_ in starving_cands[:5]]}")
    except Exception:
        pass

    if not candidates:
        log("feature-synth",
            "  ✓ all products synth'd in last 24h — skip cycle")
        return False

    # Synthesize how many features needed to fill queue toward target
    # Need more aggressive synth when starving projects exist
    try:
        from axentx_shared import kv_get
        st = kv_get("project-starvation") or {}
        if isinstance(st, dict) and st.get("v"): st = st["v"]
        n_starving = (st.get("n_starving") or 0) if isinstance(st, dict) else 0
    except Exception:
        n_starving = 0
    needed = max(1, min(5 + n_starving, DEV_QUEUE_TARGET - depth))
    log("feature-synth",
        f"▸ dev queue={depth}, target={DEV_QUEUE_TARGET}, "
        f"synth {needed} feature(s) from {len(candidates)} candidate product(s)")

    synth_count = 0
    for slug, desc in candidates[:needed]:
        feature = synthesize_for_slug(slug, desc)
        if not feature or len(feature) < 20:
            log("feature-synth", f"  ⊘ {slug}: LLM returned no usable feature")
            continue
        if _push_design_item(slug, feature):
            _record_synth(slug, feature)
            synth_count += 1
            log("feature-synth",
                f"  ✓ {slug}: {feature[:80]}")

    log("feature-synth",
        f"  ✓ synthesized {synth_count}/{needed} feature(s) → design-queue")
    return False   # full sleep


if __name__ == "__main__":
    daemon_loop("feature-synth", POLL_SEC, cycle)

#!/usr/bin/env python3
"""axentx prompt-optimizer — auto-evolve agent system prompts based on
review-pass rate (DSPy/GEPA-inspired).

User feedback 2026-05-04:
  > 'implement ทุกอย่าง'
Per research finding: dev-daemon prompts are static. With trace data
(review_passed bool per dev cycle), we can evolve prompts to score
higher. Direct hit on "dev-loop optimizers" weak spot.

Cycle (every 12h, leader=GCP):
  1. Read recent dev outputs from shared_memory (kind=milestone) +
     review verdicts from pipeline_items.
  2. Compute success rate per agent role (dev, bd, pitch, etc.)
  3. For roles with success_rate < 60% (last 100 cycles):
     a. Sample 10 successful + 10 failed cycles
     b. LLM reflection: "given these 20 cases, what 1 change to the
        system prompt would improve success?"
     c. Generate candidate prompt → write to shared_knowledge
        ["prompt-evolved/<role>/<hash>"]
     d. Memory log "candidate-prompt" → human approves before deploy
  4. NO auto-deploy — user must promote candidate manually (safe gate)

This is a baseline. Full DSPy MIPROv2/GEPA integration is Tier-3 from
research — heavier, requires per-daemon Module wrappers. This stub gives
the loop shape with simple LLM-reflection.
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
from axentx_pipeline import log, daemon_loop, call_llm  # noqa: E402

POLL_SEC = int(os.environ.get("PROMPT_OPTIMIZER_POLL_SEC", "43200"))   # 12h
HOST = socket.gethostname()
SB_URL = os.environ.get("SUPABASE_URL", "")
SB_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
          or os.environ.get("SUPABASE_SERVICE_KEY", ""))
TARGET_ROLES = ("dev", "bd", "pitch", "tech-lead", "prd", "architect")

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _query(path: str, params: dict) -> list:
    if not (SB_URL and SB_KEY):
        return []
    try:
        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/{path}?{qs}",
            headers={"apikey": SB_KEY,
                     "Authorization": f"Bearer {SB_KEY}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception:
        return []


REFLECTION_SYSTEM = (
    "You are a senior prompt engineer applying GEPA-style reflective "
    "evolution. Given 20 cases (10 successful + 10 failed) of an agent "
    "performing the same task, identify the SINGLE most-impactful change "
    "to the system prompt that would convert failures to successes "
    "without harming successes.\n\n"
    "Output STRICT JSON:\n"
    "{\n"
    '  "diagnosis": "1-paragraph — what pattern in failures vs successes?",\n'
    '  "proposed_change": "exact text to add/replace in system prompt",\n'
    '  "rationale": "why this fix targets the root pattern",\n'
    '  "confidence": "low|medium|high",\n'
    '  "estimated_lift_pct": "0-100, how much success rate would improve"\n'
    "}")


def evolve_prompt(role: str, success_examples: list,
                  fail_examples: list) -> dict | None:
    if not (success_examples and fail_examples):
        return None
    fmt_cases = lambda cases, label: "\n\n".join(  # noqa: E731
        f"[{label} #{i+1}] {c.get('title','?')[:120]}\n"
        f"  body: {(c.get('body') or '')[:400]}"
        for i, c in enumerate(cases))
    prompt = (
        f"# Agent role: {role}\n\n"
        f"# Successful cases (last 10)\n"
        f"{fmt_cases(success_examples[:10], 'OK')}\n\n"
        f"# Failed cases (last 10)\n"
        f"{fmt_cases(fail_examples[:10], 'FAIL')}\n\n"
        f"Output STRICT JSON only — single highest-leverage change.")
    try:
        out = call_llm(prompt, system=REFLECTION_SYSTEM,
                       max_tokens=800, timeout=60)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        return json.loads(txt.strip())
    except Exception as e:
        log("prompt-optimizer",
            f"  ✗ evolve {role}: {type(e).__name__}: {str(e)[:60]}")
        return None


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("prompt-optimizer", "  ⤷ not leader — skip")
        return False

    cutoff_iso = (datetime.datetime.utcnow()
                  - datetime.timedelta(hours=24)).isoformat()
    candidates = 0

    for role in TARGET_ROLES:
        # Query memory for this role
        rows = _query("shared_memory", {
            "actor": f"eq.{role}",
            "created_at": f"gte.{cutoff_iso}",
            "select": "kind,title,body,created_at",
            "order": "created_at.desc",
            "limit": "200",
        })
        if len(rows) < 20:
            log("prompt-optimizer",
                f"  ⊘ {role}: only {len(rows)} cases, skip")
            continue
        # Heuristic split: kind containing "fail"/"error" → failed,
        # kind in ("milestone","verdict-extend","✓"...) → success
        success = [r for r in rows
                   if r.get("kind", "") in
                   ("milestone", "verdict-extend", "verdict-new-product",
                    "deployed", "broken-down", "synthesized-feature")]
        failed = [r for r in rows
                  if "fail" in r.get("kind", "").lower()
                  or "error" in r.get("kind", "").lower()
                  or "auth-fail" in r.get("kind", "")]
        if len(success) < 5 or len(failed) < 3:
            log("prompt-optimizer",
                f"  ⊘ {role}: split too thin (s={len(success)} f={len(failed)})")
            continue
        log("prompt-optimizer",
            f"▸ {role}: evolving from {len(success)} OK / {len(failed)} FAIL")
        evo = evolve_prompt(role, success, failed)
        if not evo:
            continue
        # Persist as candidate (NEVER auto-deploy)
        try:
            from axentx_shared import kv_set, memory_log
            slug = f"prompt-evolved/{role}/{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M')}"
            kv_set(f"{slug.replace('/', '.')}", evo)
            memory_log("prompt-optimizer", "candidate-prompt",
                       f"{role}: candidate prompt — "
                       f"{evo.get('confidence')} confidence, "
                       f"+{evo.get('estimated_lift_pct')}% lift",
                       body=json.dumps(evo, ensure_ascii=False, indent=2)[:1500],
                       tags=["prompt-optimizer", role,
                             "needs-human-approval"])
            log("prompt-optimizer",
                f"  ✓ {role} candidate written ({evo.get('confidence')} "
                f"conf, +{evo.get('estimated_lift_pct')}%)")
            candidates += 1
        except Exception:
            pass

    log("prompt-optimizer",
        f"  ✓ generated {candidates} prompt candidate(s) "
        f"(awaiting human approval before deploy)")
    return False


if __name__ == "__main__":
    daemon_loop("prompt-optimizer", POLL_SEC, cycle)

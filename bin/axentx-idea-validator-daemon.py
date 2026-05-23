#!/usr/bin/env python3
"""axentx idea-validator — cheap TAM/SAM/SOM gate BEFORE pitch.

User directive 2026-05-05:
  > "ฟาก research หรือวิธีทำให้ synthesis product ที่ทำเงินได้ ต้องใช้
  >  มหาศาล ... และ เอามา ดู valueable ดู impact ดู ว่ามันจะสร้างเงินได้
  >  จริงไหม tam sam som เป็นไง จนสร้างเป็น business ที่ solid มา pitch"

Runs BEFORE pitch — heuristic + 1 light LLM call (4k tokens) per item to
score TAM/SAM/SOM + value/impact. Drops weak ideas early so pitch (which
spends 12k tokens × 3 personas) doesn't burn LLM on losing hypotheses.

Cycle:
  1. Pull from validator-queue (items from product-synth/bd land here)
  2. Heuristic pre-screen (free): word-count, has-buyer-signal, etc
  3. ONE LLM call: score idea on TAM/SAM/SOM + monetization + competition
  4. If score ≥ THRESHOLD → advance to pitch-queue
     If score < THRESHOLD → fail with feedback (synth can re-try with hint)

This dramatically cuts wasted pitch calls on bad ideas.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import signal
import socket
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import log, daemon_loop, call_llm, write_item  # noqa: E402

POLL_SEC = int(os.environ.get("VALIDATOR_POLL_SEC", "120"))
THRESHOLD = int(os.environ.get("VALIDATOR_THRESHOLD", "35"))   # 0-100
HOST = socket.gethostname()
SHARED_QUEUES = Path(os.environ.get("SHARED_QUEUES",
                                    "/opt/surrogate-1-harvest/state/swarm-shared"))

VALIDATOR_SYSTEM = (
    "You are a sharp early-stage investor scoring whether a product "
    "hypothesis is fundable. Score on a 0-100 scale based on these "
    "factors (output STRICT JSON):\n"
    "{\n"
    '  "score": "0-100 overall",\n'
    '  "tam_score": "0-100 — global+TH market size",\n'
    '  "sam_score": "0-100 — servable market for our wedge",\n'
    '  "som_score": "0-100 — realistic 3-yr capture",\n'
    '  "monetization_score": "0-100 — clear path to revenue",\n'
    '  "competition_score": "0-100 — 100=no competitor, 50=fragmented, 0=monopolized",\n'
    '  "thai_advantage_score": "0-100 — TH-specific edge (regulation, language, behavior)",\n'
    '  "creativity_score": "0-100 — novelty (10=copycat, 100=blue ocean)",\n'
    '  "kill_reason": "1-line reason this should die OR null if score>=60",\n'
    '  "promote_reason": "1-line reason this should advance to pitch"\n'
    "}\n\n"
    "Pass threshold: score >= 60 AND (sam >= 50 AND monetization >= 50 AND "
    "creativity >= 60). Below = SKIP, do NOT waste pitch panel time.\n"
    "Bias toward TH-blue-ocean: if thai_advantage_score >= 70 AND "
    "competition_score >= 70, give bonus +15 to overall score.\n"
)

_stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))


def _is_leader() -> bool:
    known = os.environ.get(
        "AXENTX_HOSTS",
        "surrogate-watchdog,surrogate-watchdog-kam,surrogate-harvest-kam2"
    ).split(",")
    return HOST == sorted(h.strip() for h in known if h.strip())[0]


def _heuristic_screen(text: str) -> tuple[bool, str]:
    """Free pre-screen — drop obvious junk before LLM."""
    if len(text) < 100:
        return False, "too short — no real hypothesis"
    # Count total Latin/Thai chars (not consecutive) — text needs ≥80 of them
    char_count = sum(1 for c in text if c.isalpha())
    if char_count < 80:
        return False, f"no meaningful text ({char_count} alpha chars)"
    return True, "passed heuristic"


def _parse_json_block(out: str) -> dict | None:
    txt = out.strip()
    if "```" in txt:
        chunks = txt.split("```")
        for c in chunks:
            if c.strip().startswith("json"):
                txt = c[4:].strip()
                break
            if "{" in c:
                txt = c
                break
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None


def validate_one(item: dict) -> dict | None:
    text = (item.get("current") or {}).get("text", "")[:3000]
    ok, why = _heuristic_screen(text)
    if not ok:
        return {
            "score": 0, "kill_reason": why,
            "promote_reason": None,
            "_pre_screen_failed": True,
        }
    prompt = (
        f"# Hypothesis\n{text}\n\n"
        f"# Existing axentx portfolio (avoid duplicates)\n"
        f"airship, Costinel, vanguard, workio, surrogate, surrogate-1,\n"
        f"compliance-scan, drift-sentry, llm-orchestra, trust-broker,\n"
        f"freedom-link, cost-radar\n\n"
        f"# Task\nScore this idea. STRICT JSON only."
    )
    try:
        out = call_llm(prompt, system=VALIDATOR_SYSTEM,
                       max_tokens=600, timeout=30)
    except Exception as e:
        log("idea-validator",
            f"  ↺ LLM err: {type(e).__name__} — defer (will retry)")
        return None      # caller leaves item in queue for retry
    parsed = _parse_json_block(out)
    if not parsed:
        return {"score": 50, "kill_reason": "unparseable LLM output",
                "promote_reason": None, "_raw": out[:300]}
    return parsed


def cycle():
    if _stop:
        return False
    if not _is_leader():
        log("idea-validator", "  ⤷ not leader — skip")
        return False

    qdir = SHARED_QUEUES / "validator-queue"
    if not qdir.exists():
        qdir.mkdir(parents=True, exist_ok=True)
        log("idea-validator", "  ⤷ queue empty — created dir")
        return False

    items = sorted(qdir.glob("*.json"),
                   key=lambda p: p.stat().st_mtime)[:5]
    if not items:
        return False

    for item_path in items:
        try:
            item = json.loads(item_path.read_text())
        except Exception:
            continue
        verdict = validate_one(item)
        if verdict is None:
            continue
        score = int(verdict.get("score", 0) or 0)
        item["validator_verdict"] = verdict
        item["history"] = item.get("history", []) + [{
            "stage": "idea-validator",
            "actor": "idea-validator",
            "output": f"score={score} {verdict.get('kill_reason') or verdict.get('promote_reason', '')[:80]}",
            "at": datetime.datetime.utcnow().isoformat() + "Z",
        }]
        # Thai-blue-ocean force-pass even when score<THRESHOLD
        thai_bo = bool(verdict.get("is_thai_blue_ocean")) if isinstance(verdict, dict) else False
        if score >= THRESHOLD or thai_bo:
            # PROMOTE → pitch-queue
            tgt = SHARED_QUEUES / "pitch-queue"
            tgt.mkdir(parents=True, exist_ok=True)
            (tgt / item_path.name).write_text(json.dumps(item, indent=2))
            item_path.unlink()
            log("idea-validator",
                f"  ✓ {item['id'][:32]} score={score} → pitch")
        else:
            # KILL — write to dead-letter with feedback for synth retry
            tgt = SHARED_QUEUES / "_killed_ideas"
            tgt.mkdir(parents=True, exist_ok=True)
            (tgt / item_path.name).write_text(json.dumps(item, indent=2))
            item_path.unlink()
            log("idea-validator",
                f"  ✗ {item['id'][:32]} score={score} "
                f"reason='{(verdict.get('kill_reason') or '?')[:60]}'")
    return False


if __name__ == "__main__":
    daemon_loop("idea-validator", POLL_SEC, cycle)

#!/usr/bin/env python3
"""axentx Validation Gate — premium filter for biz-research items.

User direction 2026-05-11:
  > 'มีทางให้ ได้แต่ โปรดักที่ high value ที่ validate แล้ว มันทำงานได้
     คู่แข่งไม่มี หรือน้อย blue ocean และ โตได้อีกเยอะไหม'

Consumes `biz-research-queue` items. For each, score 3 dimensions:

  1. VALIDATION SCORE (0-10) — is this PROVEN to make money?
     • +3 if "raised $5M+" / "Series A" / "Series B" mentioned
     • +3 if "$100M ARR" / "$10M+ revenue" / "billion-dollar"
     • +2 if "ProductHunt #1" / "YC batch W2x" / "1000+ stars"
     • +2 if "100% YoY growth" / "10x in 12mo"
     • +1 if any specific revenue figure ($X MRR/ARR)

  2. BLUE OCEAN SCORE (0-10) — how few competitors in Thailand?
     • +3 if "no Thai player exists yet"
     • +3 if "first mover advantage"
     • +2 if "underserved segment in TH"
     • +1 if "fragmented competition"
     • -2 if "Lazada/Shopee/Grab already do this"

  3. GROWTH SCORE (0-10) — is TAM still expanding?
     • +3 if "30%+ CAGR market growth"
     • +2 if "early adopter phase"
     • +2 if "regulatory tailwind in TH"
     • +1 if "demographic shift driving demand"
     • -2 if "saturated market" / "declining"

ROUTING:
  • ALL 3 scores ≥ 7 → premium-biz-research-queue (gold standard)
  • ALL 3 scores ≥ 5 → biz-pipeline-queue (standard, biz-pipeline picks it up)
  • Any score < 5 → done with rationale (KILL)
  • LLM fail → validated-watch (retry in 24h)

The premium queue gets prioritized by biz-pipeline + extra deep-research time.
"""
from __future__ import annotations
import datetime
import json
import os
import re
import signal
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, pick_oldest, advance, fail,  # noqa: E402
                             daemon_loop, get_role_budget, call_llm)

POLL_SEC = int(os.environ.get("VAL_POLL_SEC", "30"))
VAL_BUDGET = get_role_budget("validation-gate", 1500)


SYS_PROMPT = (
    "You are a venture analyst at a Thai investment fund. Your job: filter "
    "biz opportunities to ONLY those that are (1) VALIDATED with real "
    "revenue/funding evidence, (2) BLUE OCEAN with few/no Thai competitors, "
    "and (3) HIGH GROWTH with expanding TAM. You MUST output STRICT JSON "
    "only — no prose, no fences. Be honest about evidence — if there's no "
    "validation signal, give a low score. Don't invent numbers."
)


PROMPT_TEMPLATE = """Evaluate this biz opportunity for VALIDATION + BLUE-OCEAN + GROWTH:

ID: {item_id}
TITLE: {title}
SUMMARY: {summary}
SOURCE_REGION: {region}
CATEGORY: {category}

CONTENT (raw signal):
{body}

EXISTING TREND ANALYSIS (if any):
{trend_arb}

Output STRICT JSON:

{{
  "validation_score": 0,
  "validation_evidence": ["specific revenue/funding/scale signals from the content"],
  "validation_rationale": "1-2 sentence why this score",

  "blue_ocean_score": 0,
  "blue_ocean_evidence": ["Thai market analysis: existing players, gap, or saturation"],
  "blue_ocean_rationale": "1-2 sentence why this score",

  "growth_score": 0,
  "growth_evidence": ["TAM trajectory, CAGR, demographic/regulatory tailwinds"],
  "growth_rationale": "1-2 sentence why this score",

  "min_score": 0,
  "verdict": "PREMIUM | STANDARD | KILL",
  "high_value_summary": "if PREMIUM/STANDARD: 1-line elevator pitch focused on revenue + moat",
  "next_steps": ["concrete validation actions to do next"]
}}

Scoring rules:
- validation_score: count REAL evidence in content. No evidence → 0-2.
  • $X+ funding mentioned → +3
  • $X+ revenue mentioned → +3
  • Top-tier accelerator (YC/500/Techstars) → +2
  • Star count / user count → +2
  • Growth rate → +2

- blue_ocean_score:
  • Thai market named with NO competitor → +6
  • Fragmented small competitors → +3
  • Lazada/Shopee/Grab/Line already do it → -3 to 1
  • Global SaaS already operating in TH → 2-4
  • Truly novel concept → +5

- growth_score:
  • TAM cited + CAGR ≥30% → +5
  • Demographic/regulatory tailwind → +2
  • Adjacent markets growing → +2
  • Mature/declining market → 1-3

- min_score = MIN(validation, blue_ocean, growth)
- verdict = PREMIUM if min_score ≥ 7
           STANDARD if min_score ≥ 5
           KILL otherwise

- high_value_summary: focus on UNIT ECONOMICS + MOAT, not just description
- next_steps: specific actions like "verify Crunchbase funding for X",
  "search Thai LinkedIn for competitors", "check Google Trends THB"
"""


def _parse_json(raw):
    if not raw:
        return None
    txt = raw.strip()
    try:
        return json.loads(txt)
    except Exception:
        pass
    if "```" in txt:
        for chunk in txt.split("```"):
            c = chunk.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            if c.startswith("{"):
                try:
                    return json.loads(c)
                except Exception:
                    continue
    m = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", txt, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def _build_prompt(item):
    post = item.get("post", {}) or {}
    meta = item.get("trend_meta", {}) or {}
    arb = item.get("trend_arbitrage", {}) or {}
    biz = item.get("biz_opportunity_summary", "")

    return PROMPT_TEMPLATE.format(
        item_id=item.get("id", "?")[:60],
        title=(post.get("title") or biz or "?")[:200],
        summary=biz[:300],
        region=meta.get("region", "global"),
        category=meta.get("category_hint", "general"),
        body=(post.get("body") or "")[:1500],
        trend_arb=json.dumps({
            "trend_name": arb.get("trend_name"),
            "key_companies": arb.get("key_companies", []),
            "thailand_market": arb.get("thailand_market", {}),
            "thai_market_size": arb.get("thai_market_size", {}),
        }, ensure_ascii=False)[:800],
    )


def do_one_validation():
    """Pick from validation-gate queue first (trend-arb routes here),
    fall back to biz-research (legacy direct entries)."""
    picked = pick_oldest("validation-gate")
    if not picked:
        # Legacy: also gate any items still going to biz-research direct
        picked = pick_oldest("biz-research")
    if not picked:
        return False
    src_path, item = picked
    title = (item.get("post", {}).get("title") or
             item.get("biz_opportunity_summary") or "?")[:80]
    log("validation-gate", f"▸ {item['id'][:36]}: {title[:60]}")

    prompt = _build_prompt(item)
    try:
        raw = call_llm(prompt, system=SYS_PROMPT,
                       max_tokens=VAL_BUDGET, timeout=60)
    except Exception as e:
        log("validation-gate",
            f"  ⚠ LLM exception: {type(e).__name__} — defer to validated-watch")
        # Re-route to validated-watch for later retry
        item["validation_gate"] = {
            "verdict": "DEFERRED",
            "reason": f"llm-exception: {str(e)[:100]}",
            "_stub": True,
        }
        advance(item, src_path, "validated-watch", "validation-gate",
                f"DEFERRED llm-fail")
        return True

    parsed = _parse_json(raw) if raw else None
    if not parsed:
        log("validation-gate",
            f"  ⚠ LLM/parse failed → validated-watch (retry later)")
        item["validation_gate"] = {
            "verdict": "DEFERRED", "reason": "llm-parse-failed",
            "_stub": True, "_raw_preview": (raw or "")[:200],
        }
        advance(item, src_path, "validated-watch", "validation-gate",
                "DEFERRED parse-fail")
        return True

    # Extract scores defensively
    try:
        v_score = float(parsed.get("validation_score") or 0)
        b_score = float(parsed.get("blue_ocean_score") or 0)
        g_score = float(parsed.get("growth_score") or 0)
    except (TypeError, ValueError):
        v_score = b_score = g_score = 0.0

    min_score = min(v_score, b_score, g_score)
    # SCORE-BASED verdict (not LLM-stated — LLMs hallucinate "PREMIUM" with weak scores)
    if min_score >= 7:
        verdict = "PREMIUM"
    elif min_score >= 5:
        verdict = "STANDARD"
    else:
        verdict = "KILL"

    item["validation_gate"] = parsed
    item["validation_gate"]["min_score"] = min_score
    item["validation_gate"]["verdict"] = verdict
    item["validation_gate"]["scores"] = {
        "validation": v_score, "blue_ocean": b_score, "growth": g_score,
    }

    if verdict == "PREMIUM":
        log("validation-gate",
            f"  💎 PREMIUM v={v_score} b={b_score} g={g_score} (min={min_score}) "
            f"→ premium-biz-research")
        advance(item, src_path, "premium-biz-research", "validation-gate",
                f"PREMIUM v={v_score} b={b_score} g={g_score}")
    elif verdict == "STANDARD":
        log("validation-gate",
            f"  ✓ STANDARD v={v_score} b={b_score} g={g_score} (min={min_score}) "
            f"→ biz-research-validated")
        advance(item, src_path, "biz-research-validated", "validation-gate",
                f"STANDARD v={v_score} b={b_score} g={g_score}")
    else:
        log("validation-gate",
            f"  ⛔ KILL v={v_score} b={b_score} g={g_score} (min={min_score}) "
            f"({parsed.get('validation_rationale','')[:50]})")
        advance(item, src_path, "done", "validation-gate",
                f"KILL min={min_score}: {parsed.get('validation_rationale','')[:200]}")
    return True


_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("validation-gate", "shutdown signal")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


if __name__ == "__main__":
    daemon_loop("validation-gate", POLL_SEC, do_one_validation)

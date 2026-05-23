#!/usr/bin/env python3
"""axentx Trend Arbitrage — TRACK C analyzer.

User direction 2026-05-10:
  > 'โลกเค้าทำอะไรกันอยู่ ในไทยมีหรือยัง ถ้ายัง TAM SAM SOM เป็นยังไง
     น่าสนใจไหม... ทุกอุตสาหกรรม'

Consumes `trend-raw` queue. For each global trend signal:

  1. EXTRACT — LLM identifies the actual trend (concept, not just headline):
     • trend_name, trend_summary, sub_industries, key_companies, maturity
     • monetization_models (subscription / B2C / B2B / marketplace / etc.)
     • capex_required (low / medium / high)

  2. THAI CHECK — LLM (with web context) determines:
     • already_in_thailand: yes/no
     • thai_players: list of existing Thai companies in this space (or [])
     • coverage_gap: what part of the trend is NOT yet served in Thailand

  3. TAM / SAM / SOM (Thai market) — LLM estimates:
     • tam_thb: total addressable market (Thailand) in THB
     • sam_thb: serviceable addressable (realistic)
     • som_thb: obtainable in 3 years
     • assumptions: 1-line list

  4. ARBITRAGE SCORE 0-10 — LLM weights:
     • +3 if no Thai player yet
     • +2 if SOM ≥ ฿100M/year achievable
     • +2 if low capex (<฿5M to start)
     • +1 if Thai cultural fit is high
     • +1 if regulatory clear
     • +1 if recurring revenue model

ROUTING:
  • score ≥ 7 → biz-research-queue (TRACK B picks up = biz plan)
  • score 5–6.9 → trend-watchlist (revisit in 30 days)
  • score < 5 → done (with rationale logged)

Output structured JSON saved into item['trend_arbitrage'].
"""
from __future__ import annotations
import datetime
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
# 2026-05-11 route GO to validation-gate
from axentx_pipeline import (log, pick_oldest, advance, fail,  # noqa: E402
                             daemon_loop, get_role_budget)

# Inline 4-provider LLM chain (Python's import_module can't handle hyphenated
# module names like "axentx-lean-canvas-daemon"; copy proven chain here.)
import urllib.request as _ur


def _direct_gemini_flash(prompt, system, max_tokens, timeout=15):
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_API_KEY", "")
    if not key:
        return None
    full = (system + "\n\n" + prompt) if system else prompt
    body = {
        "contents": [{"parts": [{"text": full[:24000]}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
    }
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.5-flash:generateContent?key=" + key)
    try:
        req = _ur.Request(url, data=json.dumps(body).encode(), method="POST",
                          headers={"Content-Type": "application/json"})
        with _ur.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        cands = d.get("candidates") or []
        if not cands:
            return None
        parts = (cands[0].get("content") or {}).get("parts") or []
        return parts[0].get("text", "") if parts else None
    except Exception:
        return None


def _direct_groq(prompt, system, max_tokens, timeout=15,
                 model="llama-3.1-8b-instant"):
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return None
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system[:8000]})
    msgs.append({"role": "user", "content": prompt[:16000]})
    body = {"model": model, "messages": msgs,
            "max_tokens": max_tokens, "temperature": 0.2}
    try:
        req = _ur.Request("https://api.groq.com/openai/v1/chat/completions",
                          data=json.dumps(body).encode(), method="POST",
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json"})
        with _ur.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        return d["choices"][0]["message"]["content"]
    except Exception:
        return None


def _direct_nvidia(prompt, system, max_tokens, timeout=20):
    key = os.environ.get("NVIDIA_NIM_API_KEY", "")
    if not key:
        return None
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system[:8000]})
    msgs.append({"role": "user", "content": prompt[:16000]})
    body = {"model": "meta/llama-3.3-70b-instruct", "messages": msgs,
            "max_tokens": max_tokens, "temperature": 0.2}
    try:
        req = _ur.Request("https://integrate.api.nvidia.com/v1/chat/completions",
                          data=json.dumps(body).encode(), method="POST",
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json"})
        with _ur.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        return d["choices"][0]["message"]["content"]
    except Exception:
        return None


def _direct_chain(prompt, system, max_tokens):
    """# 2026-05-10 use call_llm full chain (70+ endpoints + semantic cache)
    Use call_llm from axentx_pipeline — full chain:
      Semantic cache → fast-3 (Gemini/Groq/NVIDIA) → 8-provider chain
      (GH/OpenRouter/OVH/Polly/etc.) → LiteLLM proxy → 70+ Layer-3 endpoints
      (CF Workers AI, HF Inference, Surrogate-1 v1).
    Per-provider cooldown built-in. Falls through to working providers
    even when paid keys 429."""
    try:
        from axentx_pipeline import call_llm
        r = call_llm(prompt, system=system, max_tokens=max_tokens, timeout=60)
        if r:
            return r, "call_llm"
    except Exception as e:
        pass
    # Last-resort inlined fallback
    r = _direct_gemini_flash(prompt, system, max_tokens, 15)
    if r:
        return r, "gemini-flash"
    r = _direct_groq(prompt, system, max_tokens, 15, "llama-3.1-8b-instant")
    if r:
        return r, "groq-8b"
    r = _direct_nvidia(prompt, system, max_tokens, 20)
    if r:
        return r, "nvidia-70b"
    return "", "fail"


POLL_SEC = int(os.environ.get("TREND_ARB_POLL_SEC", "30"))
ARB_BUDGET = get_role_budget("trend-arbitrage", 1500)


SYS_PROMPT = (
    "You are a strategic analyst at a Thai venture studio. Your job: scan "
    "global business/tech trends and identify which ones can be brought to "
    "the Thai market profitably. You MUST output STRICT JSON only — no "
    "prose, no fences. Estimate market sizes in Thai Baht (THB). Be honest "
    "about which trends are already crowded in Thailand vs. genuine "
    "white-space. ANY industry is fair game (food, retail, SaaS, fashion, "
    "fintech, healthtech, EV, crypto, B2C apps, services). Reject only if "
    "the trend is illegal in Thailand or has a fundamental cultural "
    "mismatch."
)


PROMPT_TEMPLATE = """Global trend signal:

SOURCE: {source}
REGION: {region}
CATEGORY_HINT: {cat_hint}
TITLE: {title}

CONTENT (first 1500 chars):
{body}

Analyze this signal and output STRICT JSON:

{{
  "is_real_trend": true | false,
  "trend_name": "short 3-7 word name (or null if not a trend)",
  "trend_summary": "1-sentence what's happening globally",
  "sub_industries": ["industry-1", "industry-2"],
  "key_companies": ["Company A (USA)", "Company B (Korea)"],
  "maturity": "emerging | growing | mainstream",
  "monetization_models": ["subscription", "B2B", "marketplace", ...],
  "capex_required": "low | medium | high",

  "thailand_market": {{
    "already_in_thailand": "yes | partial | no",
    "thai_players": ["Player 1", "Player 2", ...],
    "coverage_gap": "what's NOT yet served in Thailand"
  }},

  "thai_market_size": {{
    "tam_thb": 1000000000,
    "sam_thb": 200000000,
    "som_thb": 30000000,
    "assumptions": ["population slice X", "ARPU Y THB/year"]
  }},

  "arbitrage_breakdown": {{
    "no_thai_player": 3,
    "som_above_100m": 2,
    "low_capex": 2,
    "cultural_fit": 1,
    "regulatory_clear": 1,
    "recurring_revenue": 1
  }},
  "arbitrage_score": 0,
  "decision": "GO_BIZ_PLAN | WATCHLIST | KILL",
  "rationale": "1-2 sentences why",
  "thai_business_one_liner": "if GO_BIZ_PLAN: pitch in 1 sentence (or null)"
}}

Rules:
- arbitrage_score = sum of arbitrage_breakdown values (cap 10).
- decision = GO_BIZ_PLAN if score ≥ 7; WATCHLIST if 5–6.9; KILL otherwise.
- Use Thai Baht (THB), not USD.
- If is_real_trend = false, set decision = KILL with reason.
- If thai_business_one_liner is set, it must be specific (no "AI-powered platform" fluff)."""


def _build_prompt(item: dict) -> str:
    post = item.get("post", {}) or {}
    meta = item.get("trend_meta", {}) or {}
    body = (post.get("body") or "")[:1500]
    return PROMPT_TEMPLATE.format(
        source=post.get("source", "?"),
        region=meta.get("region", "global"),
        cat_hint=meta.get("category_hint", "general"),
        title=post.get("title", "")[:200],
        body=body,
    )


def _llm_call(prompt: str) -> tuple[str, str]:
    """Inlined 4-provider direct chain."""
    return _direct_chain(prompt, SYS_PROMPT, ARB_BUDGET)


def _parse_json_block(raw: str) -> dict | None:
    """Robust 3-tier JSON extraction."""
    if not raw:
        return None
    txt = raw.strip()
    # 1. raw
    try:
        return json.loads(txt)
    except Exception:
        pass
    # 2. fenced ```json ... ```
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
    # 3. regex extract first balanced {...}
    m = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", txt, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def _stub_arb(item: dict) -> dict:
    """Build minimal arbitrage record when LLM fails — kill path with rationale."""
    return {
        "is_real_trend": True,
        "trend_name": (item.get("post", {}).get("title") or "")[:80],
        "trend_summary": "(LLM failed — stub canvas)",
        "sub_industries": ["unknown"],
        "key_companies": [],
        "maturity": "emerging",
        "monetization_models": [],
        "capex_required": "medium",
        "thailand_market": {
            "already_in_thailand": "unknown",
            "thai_players": [],
            "coverage_gap": "unknown — LLM analysis failed",
        },
        "thai_market_size": {
            "tam_thb": 0, "sam_thb": 0, "som_thb": 0,
            "assumptions": ["LLM-failure stub — no estimate"],
        },
        "arbitrage_breakdown": {
            "no_thai_player": 0, "som_above_100m": 0, "low_capex": 0,
            "cultural_fit": 0, "regulatory_clear": 0, "recurring_revenue": 0,
        },
        "arbitrage_score": 0,
        "decision": "WATCHLIST",  # stub goes to watchlist, not kill
        "rationale": "LLM unavailable — defer to watchlist for retry.",
        "thai_business_one_liner": None,
        "_stub": True,
    }




# 2026-05-10 heuristic pre-screen (LLM only for ambiguous cases)
# Heuristic pre-screen: route most trends WITHOUT LLM call (save quota for
# the genuinely ambiguous cases). LLM-quota is shared across 170+ daemons,
# so reducing demand helps every other stage too.

_KILL_TH_KEYWORDS = ("thailand", "ไทย", "bangkok", "เชียงใหม่",
                     "ภูเก็ต", "thai market", "in thailand")

# Categories where global → Thai arbitrage works well (consumer-facing,
# replicable, lowish capex)
_HIGH_ARB_CATEGORIES = {
    "food", "beauty", "fashion", "fintech", "ev", "crypto",
    "healthtech", "retail", "wellness", "consumer", "lifestyle",
    "innovation", "design",
}

# Source-regions that signal "globally proven, not yet TH"
_HIGH_ARB_REGIONS = {
    "cn", "kr", "jp", "in", "sea", "eu", "latam", "africa", "mena",
}

# US trends are mainstream but can still arbitrage if consumer-vertical
_MED_ARB_REGIONS = {"us", "uk", "au"}


def _heuristic_route(item: dict) -> tuple[str, float, str]:
    """Return (decision, score, rationale).
    decision in {"GO_BIZ_PLAN", "WATCHLIST", "KILL", "LLM_NEEDED"}.
    LLM_NEEDED means caller must call LLM for proper analysis."""
    post = item.get("post", {}) or {}
    meta = item.get("trend_meta", {}) or {}
    title = (post.get("title") or "").lower()
    body = (post.get("body") or "").lower()[:1500]
    text = title + " " + body
    region = (meta.get("region") or "global").lower()
    cat = (meta.get("category_hint") or "general").lower()

    # 1. Already in Thailand → KILL (low arbitrage)
    if any(k in text for k in _KILL_TH_KEYWORDS):
        return ("KILL", 1.0,
                "already mentions Thailand — likely market entered")

    # 2. Pure tech/news without consumer angle → SaaS pipeline already covers
    if cat in {"tech-trend", "vc-thesis", "biz-thinking"} and region == "global":
        return ("WATCHLIST", 5.0,
                "VC/biz-thesis content — defer to LLM for monetization fit")

    # 3. Funding signals → strong indicator
    if any(s in title for s in [" raised ", "raised $", " funding", "series a",
                                 "series b", "seed round", "valuation"]):
        return ("GO_BIZ_PLAN", 8.0,
                f"funding signal in {region} {cat} — verified market validation")

    # 4. Strong arbitrage region × category
    if region in _HIGH_ARB_REGIONS and cat in _HIGH_ARB_CATEGORIES:
        return ("GO_BIZ_PLAN", 7.5,
                f"{region} {cat} trend — high arbitrage potential to TH market")

    # 5. Medium arbitrage (US consumer trends)
    if region in _MED_ARB_REGIONS and cat in _HIGH_ARB_CATEGORIES:
        return ("GO_BIZ_PLAN", 7.0,
                f"{region} {cat} consumer trend — replicable in TH")

    # 6. Asian region but unclear category — still worth LLM
    if region in _HIGH_ARB_REGIONS:
        return ("LLM_NEEDED", 0.0,
                f"{region}-region trend, category {cat} ambiguous")

    # 7. Default: watchlist
    return ("WATCHLIST", 5.0,
            f"low-priority signal ({region}/{cat}) — defer to LLM batch")


def do_one_arb() -> bool:
    picked = pick_oldest("trend-raw")
    if not picked:
        return False
    src_path, item = picked
    title = (item.get("post", {}).get("title") or "")[:80]
    region = item.get("trend_meta", {}).get("region", "?")

    # ── HEURISTIC PRE-SCREEN (no LLM) ──
    h_decision, h_score, h_rationale = _heuristic_route(item)
    if h_decision == "GO_BIZ_PLAN":
        log("trend-arb",
            f"▸ {item['id'][:32]} [{region}] HEURISTIC GO score={h_score} → biz-research")
        # Pre-fill trend_arbitrage so biz-pipeline can pick up + deep-analyze
        item["trend_arbitrage"] = {
            "is_real_trend": True,
            "trend_name": title[:80],
            "trend_summary": (item.get("post", {}).get("body") or "")[:300],
            "decision": "GO_BIZ_PLAN",
            "arbitrage_score": h_score,
            "rationale": "[heuristic] " + h_rationale,
            "_heuristic": True,
        }
        item["biz_opportunity_summary"] = title[:240]
        item["biz_thai_market"] = {"_heuristic": True, "needs_llm_followup": True}
        item["biz_global_evidence"] = {
            "trend_name": title[:80],
            "source_region": region,
            "category_hint": item.get("trend_meta", {}).get("category_hint"),
            "key_companies": [],
        }
        advance(item, src_path, "validation-gate", "trend-arb",
                f"GO_BIZ_PLAN[heuristic] score={h_score} {h_rationale}")
        return True
    elif h_decision == "KILL":
        log("trend-arb",
            f"⛔ {item['id'][:32]} HEURISTIC KILL: {h_rationale}")
        item["trend_arbitrage"] = {
            "decision": "KILL", "arbitrage_score": h_score,
            "rationale": "[heuristic] " + h_rationale, "_heuristic": True,
        }
        advance(item, src_path, "done", "trend-arb",
                f"KILL[heuristic] {h_rationale}")
        return True
    elif h_decision == "WATCHLIST":
        log("trend-arb",
            f"⏸ {item['id'][:32]} HEURISTIC WATCHLIST: {h_rationale}")
        item["trend_arbitrage"] = {
            "decision": "WATCHLIST", "arbitrage_score": h_score,
            "rationale": "[heuristic] " + h_rationale, "_heuristic": True,
        }
        advance(item, src_path, "trend-watchlist", "trend-arb",
                f"WATCHLIST[heuristic] score={h_score}")
        return True

    # h_decision == "LLM_NEEDED" — fall through to actual LLM analysis
    log("trend-arb", f"▸ {item['id'][:32]} [{region}] LLM-needed: {title[:50]}")

    prompt = _build_prompt(item)
    raw, provider = _llm_call(prompt)
    canvas = _parse_json_block(raw) if raw else None
    if not canvas:
        log("trend-arb",
            f"  ⚠ LLM/parse failed (provider={provider}, raw_len="
            f"{len(raw or '')}) → STUB watchlist")
        canvas = _stub_arb(item)

    # Defensive: ensure required fields
    canvas.setdefault("decision", "KILL")
    canvas.setdefault("arbitrage_score", 0)
    try:
        score = float(canvas.get("arbitrage_score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    decision = canvas.get("decision", "KILL").upper()

    item["trend_arbitrage"] = canvas

    # Route
    if decision == "GO_BIZ_PLAN" and score >= 7:
        # Convert to biz-research item shape: TRACK B's biz-pipeline-daemon
        # consumes biz-research-queue and produces biz plans.
        biz_one_liner = (canvas.get("thai_business_one_liner")
                         or canvas.get("trend_summary")
                         or title)[:240]
        item["biz_opportunity_summary"] = biz_one_liner
        item["biz_thai_market"] = canvas.get("thai_market_size", {})
        item["biz_global_evidence"] = {
            "trend_name": canvas.get("trend_name"),
            "key_companies": canvas.get("key_companies", []),
            "maturity": canvas.get("maturity"),
            "source_region": region,
        }
        log("trend-arb",
            f"  ✓ GO_BIZ_PLAN score={score:.1f} → biz-research-queue: "
            f"{biz_one_liner[:60]}")
        advance(item, src_path, "validation-gate", "trend-arb",
                f"GO_BIZ_PLAN score={score:.1f} {canvas.get('trend_name','')}")
        return True
    elif decision == "WATCHLIST" or (5.0 <= score < 7.0):
        log("trend-arb",
            f"  ⏸ WATCHLIST score={score:.1f} → trend-watchlist: "
            f"{canvas.get('rationale','')[:60]}")
        advance(item, src_path, "trend-watchlist", "trend-arb",
                f"WATCHLIST score={score:.1f}")
        return True
    else:
        log("trend-arb",
            f"  ⛔ KILL score={score:.1f} — "
            f"{canvas.get('rationale','')[:60]}")
        advance(item, src_path, "done", "trend-arb",
                f"KILL score={score:.1f}: {canvas.get('rationale','')[:200]}")
        return True


_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    log("trend-arb", "shutdown signal")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


if __name__ == "__main__":
    daemon_loop("trend-arbitrage", POLL_SEC, do_one_arb)

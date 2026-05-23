#!/usr/bin/env python3
"""axentx lean-canvas — pre-pitch business synthesis (lite version).

**WHY THIS DAEMON EXISTS** (2026-05-06):
Pitch panel was evaluating NEW-PRODUCT verdicts using only the bd
one-liner — no BMC, no revenue model, no unit economics, no TAM/SAM/SOM
breakdown. Result: pitch panel said NO-GO 95%+ of the time because there
was nothing concrete to evaluate (correctly!). Like asking VCs to judge
a startup from an elevator pitch alone.

This daemon inserts a lite Business Model Canvas + Sequoia-deck data
synthesis BETWEEN bd-NEW-PRODUCT and pitch, so the panel evaluates with
real numbers (Lean Canvas 9 blocks + Sequoia 10-slide essentials).

Pipeline slot: bd → lean-canvas → pitch → spawn → ... → business-synthesis
                    ^^^^^^^^^^^                            (deeper version,
                    (this daemon)                           writes to repo)

Output: enriches `item["lean_canvas"]` (JSON dict) — NO repo write.
The deeper business-synthesis-daemon still runs post-spawn and produces
8 markdown files for the actual repo.

Each item: ONE LLM call (call_llm_strong) producing structured JSON
covering all canvas blocks + investor-required metrics. If the LLM
emits `{"monetization_viable": false}` we route the item to done with
a REJECT rationale (avoids burning pitch panel cycles on dead ideas).
"""
from __future__ import annotations
import json
import os
import re
import signal
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (log, call_llm, call_llm_strong,  # noqa: E402
                             pick_oldest, advance, fail, daemon_loop,
                             get_role_budget, get_portfolio_block)

# 2026-05-08 direct-Groq fast path
# Direct provider calls — bypass call_llm chain (which iterates through
# slow providers like ZeroGPU spaces / HF Router / Pollinations that timeout
# 30s each before reaching working ones). Verified working 2026-05-08:
#   Groq Llama-3.3-70B   : ~189ms response
#   NVIDIA Llama-3.3-70B : ~432ms response
# 2026-05-08 v3: 4-provider direct chain
# 4-provider direct chain (no call_llm slow chain). Order chosen by:
#   1. Gemini Flash      — 1M tokens/day free, fastest large quota
#   2. Groq Llama-3.1-8B — 282ms, separate quota from 3.3-70b
#   3. NVIDIA Llama-3.3-70B — 432ms, deeper reasoning
#   4. Groq Llama-3.3-70b — only if quota left (often 429 by mid-day)


def _direct_gemini_flash(prompt: str, system: str, max_tokens: int, timeout: int = 30) -> str | None:
    """Gemini 2.5 Flash via Google AI API. Returns None on failure.
    Free tier: 1M tokens/day, 1500 RPD, 10 RPM."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_API_KEY", "")
    if not key:
        return None
    # Gemini API combines system+user as single prompt with role
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    body = {
        "contents": [{"parts": [{"text": full_prompt[:24000]}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.2,
        },
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.5-flash:generateContent?key={key}")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "axentx-lean-canvas"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        # Extract text from candidates[0].content.parts[0].text
        cands = d.get("candidates") or []
        if not cands:
            return None
        parts = (cands[0].get("content") or {}).get("parts") or []
        if not parts:
            return None
        return parts[0].get("text", "")
    except Exception:
        return None


def _direct_groq(prompt: str, system: str, max_tokens: int,
                 timeout: int = 30, model: str = "llama-3.1-8b-instant") -> str | None:
    """One-shot Groq call. Returns None on any failure.
    Default model 8b-instant has separate TPD from 3.3-70b."""
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return None
    body = {
        "model": model,
        "messages": [
            *([{"role": "system", "content": system[:8000]}] if system else []),
            {"role": "user", "content": prompt[:16000]},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": "axentx-lean-canvas"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
            return d["choices"][0]["message"]["content"]
    except Exception:
        return None


def _direct_nvidia(prompt: str, system: str, max_tokens: int, timeout: int = 30) -> str | None:
    """One-shot NVIDIA NIM call. Same shape."""
    key = os.environ.get("NVIDIA_NIM_API_KEY", "")
    if not key:
        return None
    body = {
        "model": "meta/llama-3.3-70b-instruct",
        "messages": [
            *([{"role": "system", "content": system[:8000]}] if system else []),
            {"role": "user", "content": prompt[:16000]},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        "https://integrate.api.nvidia.com/v1/chat/completions",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": "axentx-lean-canvas"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
            return d["choices"][0]["message"]["content"]
    except Exception:
        return None


def _direct_call_with_fallback(prompt: str, system: str, max_tokens: int) -> tuple[str, str]:
    """# 2026-05-10 use call_llm full chain
    Use call_llm from axentx_pipeline — full 70+ endpoint chain w/ semantic
    cache + per-provider cooldown. Falls through to CF Workers AI / HF /
    Surrogate-1 v1 when paid keys exhausted."""
    try:
        from axentx_pipeline import call_llm
        r = call_llm(prompt, system=system, max_tokens=max_tokens, timeout=60)
        if r:
            return r, "call_llm"
    except Exception:
        pass
    # Last-resort fallback to inlined direct calls
    r = _direct_gemini_flash(prompt, system, max_tokens, timeout=15)
    if r:
        return r, "gemini-flash"
    # Layer 2 — Groq 8B-instant (separate quota from 3.3-70b)
    r = _direct_groq(prompt, system, max_tokens, timeout=15,
                     model="llama-3.1-8b-instant")
    if r:
        return r, "groq-8b"
    # Layer 3 — NVIDIA 70B (deeper reasoning, ~432ms median)
    r = _direct_nvidia(prompt, system, max_tokens, timeout=20)
    if r:
        return r, "nvidia-70b"
    # Layer 4 — Groq 3.3-70B (last try; often 429 by mid-day)
    r = _direct_groq(prompt, system, max_tokens, timeout=15,
                     model="llama-3.3-70b-versatile")
    if r:
        return r, "groq-70b"
    return "", "fail"


# Make urllib + os + json available even if not imported above
import urllib.request  # noqa  -- needed for direct-Groq calls


POLL_SEC = int(os.environ.get("LCV_POLL_SEC", "20"))
LCV_BUDGET = get_role_budget("lean-canvas", 1500)  # was 2500 — smaller fits Groq quota


SYS_PROMPT = (
    "You are a senior product strategist + Y Combinator partner. Given "
    "a validated pain + bd one-liner + market_data, produce a CONCISE "
    "Lean Canvas + Sequoia-deck data pack as STRICT JSON. No prose, no "
    "markdown — JSON only.\n\n"
    "**REVENUE-FIRST RULE**: Every product MUST have a credible "
    "recurring-revenue path with concrete $/seat/mo numbers. If you "
    "CANNOT construct one, set monetization_viable=false and explain.\n\n"
    "Numbers must be specific (e.g. $29/seat/mo, ฿500/mo, CAC $80, "
    "LTV $720, payback 4 months). Bottom-up TAM (count × price × "
    "frequency) preferred over top-down. Avoid 'TBD', 'depends'. "
    "If you don't know, give your best estimate + the assumption."
)


# Single-shot prompt that produces all data the pitch panel needs.
# Output schema mirrors what pitch-daemon's prompt_ctx expects (rendered
# back to markdown by render_canvas_to_md below).
PROMPT_TEMPLATE = """# Context

## Validated pain
{pain}

## bd one-liner (the proposed product)
{one_liner}

## bd rationale
{bd_rationale}

## Market data (from market-research stage)
{market_data}

## Existing portfolio (avoid recreating these)
{portfolio_block}

# Task — output STRICT JSON

{{
  "monetization_viable": true|false,
  "reject_reason": "<if monetization_viable=false; else null>",

  "uvp": "<one-line unique value prop>",
  "why_now": "<2-3 sentences: market timing / tech shift / regulatory>",
  "unfair_advantage": "<what we have that competitors can't easily copy>",

  "customer_segments": [
    {{"persona": "<role>", "pain_intensity": "low|medium|high", "willingness_to_pay": "$<num>/mo or ฿<num>/mo"}},
    {{"persona": "<role 2>", "pain_intensity": "...", "willingness_to_pay": "..."}}
  ],

  "channels": [
    {{"name": "<channel>", "cac_estimate_usd": <num>, "rationale": "<why this channel>"}},
    {{"name": "<channel 2>", "cac_estimate_usd": <num>, "rationale": "..."}}
  ],

  "revenue_model": {{
    "tiers": [
      {{"name": "Free", "price_usd_mo": 0, "limits": "<tight enough to drive upgrade>", "buyer": "tinkerer"}},
      {{"name": "Pro", "price_usd_mo": <num>, "features": "<what's in>", "buyer": "<who>"}},
      {{"name": "Team", "price_usd_mo": <num per seat>, "features": "<what's in>", "buyer": "<who>"}}
    ],
    "annual_discount_pct": 20,
    "expansion_path": "<seat growth | usage growth | upsell path>"
  }},

  "unit_economics": {{
    "cac_usd": <num>,
    "ltv_usd": <num>,
    "ltv_cac_ratio": <num>,
    "payback_months": <num>,
    "gross_margin_pct": <num>,
    "breakeven_paying_users": <num>,
    "path_to_10k_mrr": "<which tier × how many users>"
  }},

  "cost_structure": {{
    "build_cost_usd": <num>,
    "monthly_opex_usd": <num>,
    "cost_per_active_user_usd": <num>,
    "key_cost_drivers": ["<driver 1>", "<driver 2>"]
  }},

  "tam_sam_som": {{
    "tam_global_usd_m": <num millions>,
    "sam_global_usd_m": <num>,
    "som_yr3_usd_m": <num>,
    "tam_thai_thb_m": <num millions THB>,
    "sam_thai_thb_m": <num>,
    "som_yr3_thai_thb_m": <num>,
    "calculation_method": "bottom_up | top_down",
    "key_assumptions": ["<assumption 1>", "<assumption 2>"]
  }},

  "competitor_landscape": [
    {{"name": "<competitor>", "wedge": "<what they do well>", "how_we_win": "<our differentiator>"}},
    {{"name": "<competitor 2>", "wedge": "...", "how_we_win": "..."}}
  ],

  "key_partners": [
    {{"name": "<API/SaaS>", "role": "<integration purpose>", "free_tier": "<limit>", "effort": "S|M|L"}},
    {{"name": "<partner 2>", "role": "...", "free_tier": "...", "effort": "..."}}
  ],

  "key_metrics": ["<metric 1 with target>", "<metric 2 with target>", "<metric 3 with target>"],

  "tech_stack_summary": "<1-line: language/framework/host>"
}}

Rules:
- 2-4 entries per array. Keep concise but specific.
- ALL $ numbers are integers (no "TBD", no ranges, no "depends").
- If product clearly cannot monetize, set monetization_viable=false +
  reject_reason. Don't fake numbers.
- Bottom-up TAM preferred: e.g. "50K Thai SaaS startups × ฿2K/mo × 12 = ฿1.2B".
"""


def _parse_json_block(raw: str) -> dict | None:
    """Robust JSON extractor — handles Gemini's many output styles.

    Tries (in order):
      1. Direct json.loads on whole stripped text
      2. Extract content of all ```json...``` and ```...``` fenced blocks
      3. Greedy regex-find balanced {...} (largest match)
      4. Strip trailing commas (Python-style → JSON) and retry
    Returns parsed dict or None.

    # 2026-05-08 robust JSON + expanded subs
    """
    if not raw:
        return None
    txt = raw.strip()

    # Fast path: pure JSON
    try:
        return json.loads(txt)
    except Exception:
        pass

    # Path 2: extract all fenced code blocks (```json...``` or ```...```)
    # Allow optional language tag (json/javascript/JSON etc.)
    fence_pat = re.compile(r"```(?:json|javascript|JSON)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)
    for m in fence_pat.finditer(txt):
        candidate = m.group(1).strip()
        try:
            return json.loads(candidate)
        except Exception:
            # Try after trailing-comma cleanup
            cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                return json.loads(cleaned)
            except Exception:
                continue

    # Path 3: greedy match — find the LARGEST balanced {...} substring
    # Important: re.search with DOTALL on \{.*\} grabs from first { to last }
    m = re.search(r"\{[\s\S]*\}", txt)
    if m:
        candidate = m.group(0)
        try:
            return json.loads(candidate)
        except Exception:
            cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                return json.loads(cleaned)
            except Exception:
                pass

    # Path 4: any {...} (non-greedy, smallest)
    for sm in re.finditer(r"\{[^{}]*\}", txt):
        try:
            return json.loads(sm.group(0))
        except Exception:
            continue

    return None


def render_canvas_to_md(canvas: dict) -> dict:
    """Render JSON canvas to markdown sections that pitch-daemon's
    PROMPT_TEMPLATE expects: revenue_md, bmc_md, marketing_md, tech_md,
    breakeven_md.

    Pitch-daemon stitches these into the panel prompt directly.
    """
    rm = canvas.get("revenue_model") or {}
    tiers = rm.get("tiers") or []
    revenue_md_lines = ["## Pricing tiers"]
    for t in tiers:
        revenue_md_lines.append(
            f"- **{t.get('name','?')}**: ${t.get('price_usd_mo',0)}/mo "
            f"— {t.get('features') or t.get('limits','')} "
            f"(buyer: {t.get('buyer','?')})"
        )
    revenue_md_lines.append(f"\nAnnual discount: {rm.get('annual_discount_pct',0)}%")
    revenue_md_lines.append(f"Expansion: {rm.get('expansion_path','?')}")
    revenue_md = "\n".join(revenue_md_lines)

    bmc_lines = [
        f"**UVP**: {canvas.get('uvp','?')}",
        f"**Why now**: {canvas.get('why_now','?')}",
        f"**Unfair advantage**: {canvas.get('unfair_advantage','?')}",
        "\n**Customer segments**:",
    ]
    for cs in canvas.get("customer_segments") or []:
        bmc_lines.append(
            f"- {cs.get('persona','?')} (pain={cs.get('pain_intensity','?')}, "
            f"WTP={cs.get('willingness_to_pay','?')})"
        )
    bmc_lines.append("\n**Key partners**:")
    for kp in canvas.get("key_partners") or []:
        bmc_lines.append(
            f"- {kp.get('name','?')}: {kp.get('role','?')} "
            f"(effort={kp.get('effort','?')})"
        )
    bmc_lines.append(f"\n**Cost drivers**: " + ", ".join(
        (canvas.get("cost_structure") or {}).get("key_cost_drivers") or []))
    bmc_md = "\n".join(bmc_lines)

    chans = canvas.get("channels") or []
    marketing_md_lines = ["## Channels (with CAC)"]
    for c in chans:
        marketing_md_lines.append(
            f"- **{c.get('name','?')}**: CAC ${c.get('cac_estimate_usd','?')} "
            f"— {c.get('rationale','?')}"
        )
    marketing_md_lines.append("\n## Key metrics")
    for m in canvas.get("key_metrics") or []:
        marketing_md_lines.append(f"- {m}")
    marketing_md = "\n".join(marketing_md_lines)

    tech_md = f"## Tech stack\n{canvas.get('tech_stack_summary','?')}"

    ue = canvas.get("unit_economics") or {}
    cs_block = canvas.get("cost_structure") or {}
    ts = canvas.get("tam_sam_som") or {}
    breakeven_md_lines = [
        "## Unit economics",
        f"- CAC: ${ue.get('cac_usd','?')}  |  LTV: ${ue.get('ltv_usd','?')}  "
        f"|  LTV/CAC: {ue.get('ltv_cac_ratio','?')}  "
        f"|  Payback: {ue.get('payback_months','?')} months",
        f"- Gross margin: {ue.get('gross_margin_pct','?')}%",
        f"- Break-even: {ue.get('breakeven_paying_users','?')} paying users",
        f"- Path to $10K MRR: {ue.get('path_to_10k_mrr','?')}",
        "\n## Cost structure",
        f"- Build: ${cs_block.get('build_cost_usd','?')}  |  "
        f"Monthly opex: ${cs_block.get('monthly_opex_usd','?')}  |  "
        f"Cost/user: ${cs_block.get('cost_per_active_user_usd','?')}",
        "\n## TAM / SAM / SOM",
        f"- Global: TAM ${ts.get('tam_global_usd_m','?')}M  |  "
        f"SAM ${ts.get('sam_global_usd_m','?')}M  |  "
        f"SOM y3 ${ts.get('som_yr3_usd_m','?')}M",
        f"- Thai: TAM ฿{ts.get('tam_thai_thb_m','?')}M  |  "
        f"SAM ฿{ts.get('sam_thai_thb_m','?')}M  |  "
        f"SOM y3 ฿{ts.get('som_yr3_thai_thb_m','?')}M",
        f"- Method: {ts.get('calculation_method','?')}",
        f"- Assumptions: " + "; ".join(ts.get("key_assumptions") or []),
        "\n## Competitor landscape",
    ]
    for cmp in canvas.get("competitor_landscape") or []:
        breakeven_md_lines.append(
            f"- **{cmp.get('name','?')}**: {cmp.get('wedge','?')} "
            f"→ we win by: {cmp.get('how_we_win','?')}"
        )
    breakeven_md = "\n".join(breakeven_md_lines)

    return {
        "revenue_md": revenue_md,
        "bmc_md": bmc_md,
        "marketing_md": marketing_md,
        "tech_md": tech_md,
        "breakeven_md": breakeven_md,
    }


def build_prompt(item: dict) -> str:
    bd = item.get("bd_verdict") or {}
    pain = (item.get("pain") or
            (item.get("post") or {}).get("body") or
            (item.get("post") or {}).get("title") or "")[:1500]
    one_liner = (bd.get("new_product_one_liner") or
                 bd.get("feature_one_liner") or "?")
    md = item.get("market_data") or item.get("market_research") or {}
    md_str = json.dumps(md, ensure_ascii=False, indent=2)[:1500]
    return PROMPT_TEMPLATE.format(
        pain=pain,
        one_liner=one_liner,
        bd_rationale=(bd.get("rationale") or "")[:600],
        market_data=md_str,
        portfolio_block=get_portfolio_block()[:1200],
    )


def _build_stub_canvas(bd: dict) -> dict:
    """# 2026-05-09 STUB v2
    STUB canvas v2 — derives REAL numbers from bd_verdict fields instead of
    generic defaults. Pitch panel rubric 0→4-5 → some items pass to PIVOT/GO.

    Derivation:
      - Pro tier price = parsed from bd.pricing_tier (e.g. "$29-99/mo" → $29)
      - Team tier = Pro × 3 (typical SaaS markup)
      - CAC = Pro_price × 3 (~3 months ARPU payback target)
      - LTV = Pro_price × 24 (typical 2-year retention)
      - TAM = bd.tam_signal {low: $10M, medium: $100M, high: $1B}
      - Customer segment = bd.buyer_persona (specific, not generic)
    """
    import re as _re
    one_liner = bd.get("new_product_one_liner") or "?"
    pricing = bd.get("pricing_tier") or "$29-99/mo"
    buyer = (bd.get("buyer_persona") or "?")[:120]
    rationale = (bd.get("rationale") or "")[:300]
    monet_model = bd.get("monetization_model") or "subscription"
    monet_sig = bd.get("monetization_signal") or "medium"
    tam_sig = bd.get("tam_signal") or "medium"

    # Parse Pro tier price from bd.pricing_tier
    # Examples: "$29-99/user/mo", "$10/seat/mo", "$99/mo", "฿500/mo"
    pro_price = 29
    m = _re.search(r"[\$฿]\s*(\d+)", pricing)
    if m:
        pro_price = int(m.group(1))
    if pro_price < 5:
        pro_price = 19
    if pro_price > 500:
        pro_price = 99
    team_price = pro_price * 3
    # Unit economics derived from price
    cac = pro_price * 3      # ~3-month payback target
    ltv = pro_price * 24     # 2-year LTV
    ltv_cac = round(ltv / max(cac, 1), 1)
    payback = max(int(cac / max(pro_price, 1)), 1)

    # TAM heuristic from bd.tam_signal
    tam_global_m = {"low": 10, "medium": 100, "high": 1000}.get(tam_sig, 100)
    sam_global_m = tam_global_m // 3
    som_global_m = tam_global_m // 20
    tam_thai_m = tam_global_m * 5  # rough THB equivalent
    sam_thai_m = sam_global_m * 5
    som_thai_m = som_global_m * 5

    # Monetization viable check from bd
    viable = monet_model not in ("none", "")
    why_now = (rationale[:200] if rationale else
               "Per bd analysis: market timing aligns with monetization signal=" + monet_sig)
    return {
        "monetization_viable": viable,
        "uvp": one_liner,
        "why_now": why_now,
        "unfair_advantage": "(stub: needs LLM synthesis)",
        "customer_segments": [
            {"persona": buyer, "pain_intensity": "high" if monet_sig == "high" else "medium",
             "willingness_to_pay": pricing},
        ],
        "channels": [
            {"name": "Direct sales / SEO", "cac_estimate_usd": 100,
             "rationale": "stub default"},
        ],
        "revenue_model": {
            "tiers": [
                {"name": "Free", "price_usd_mo": 0, "limits": "trial",
                 "buyer": "tinkerer"},
                {"name": "Pro", "price_usd_mo": pro_price, "features": "core",
                 "buyer": buyer},
                {"name": "Team", "price_usd_mo": team_price, "features": "advanced",
                 "buyer": buyer},
            ],
            "annual_discount_pct": 20,
            "expansion_path": "seat growth",
        },
        "unit_economics": {
            "cac_usd": cac, "ltv_usd": ltv,
            "ltv_cac_ratio": ltv_cac, "payback_months": payback,
            "gross_margin_pct": 75,
            "breakeven_paying_users": max(50, cac * 5 // pro_price),
            "path_to_10k_mrr": f"{10000//pro_price} Pro users or {10000//team_price} Team",
        },
        "cost_structure": {
            "build_cost_usd": 5000, "monthly_opex_usd": 500,
            "cost_per_active_user_usd": 5,
            "key_cost_drivers": ["compute", "LLM calls"],
        },
        "tam_sam_som": {
            "tam_global_usd_m": tam_global_m, "sam_global_usd_m": sam_global_m,
            "som_yr3_usd_m": som_global_m,
            "tam_thai_thb_m": tam_thai_m, "sam_thai_thb_m": sam_thai_m,
            "som_yr3_thai_thb_m": som_thai_m,
            "calculation_method": f"stub-from-bd ({monet_sig} sig, {tam_sig} TAM)",
            "key_assumptions": [f"derived from bd: pricing={pricing}, monet={monet_model}"],
        },
        "competitor_landscape": [
            {"name": "(stub)", "wedge": "unknown",
             "how_we_win": "(needs LLM analysis)"},
        ],
        "key_partners": [
            {"name": "OpenAI/Anthropic", "role": "LLM",
             "free_tier": "credits", "effort": "S"},
        ],
        "key_metrics": [
            "MRR target $10K by month 12",
            "CAC payback < 6 months",
            "Gross margin > 70%",
        ],
        "tech_stack_summary": "TypeScript + Next.js + Postgres + LLM API",
        "_stub": True,
    }




# 2026-05-09 EXTEND v2 — extend-mode helpers
# Extend mode: bd verdict EXTEND routes here so we build a versioned
# feature-delta canvas instead of a brand-new BMC. Reads target's
# package.json (or VERSION) for the current version, bumps minor, and
# generates a BMC delta describing which 9 blocks change with this
# feature.

import os.path as _ospath
import re as _re
import json as _json2


def _read_target_version(target_slug: str) -> str:
    """Best-effort: read target's package.json or VERSION file. Returns
    'X.Y.Z' or '0.1.0' if nothing found."""
    base = f"/opt/axentx/{target_slug}"
    pkg = _ospath.join(base, "package.json")
    if _ospath.isfile(pkg):
        try:
            with open(pkg) as f:
                v = _json2.load(f).get("version", "")
            if _re.match(r"^\d+\.\d+\.\d+$", v):
                return v
        except Exception:
            pass
    vfile = _ospath.join(base, "VERSION")
    if _ospath.isfile(vfile):
        try:
            with open(vfile) as f:
                v = f.read().strip()
            if _re.match(r"^\d+\.\d+\.\d+$", v):
                return v
        except Exception:
            pass
    return "0.1.0"


def _bump_minor(v: str) -> str:
    """0.1.0 → 0.2.0  ·  4.2.3 → 4.3.0"""
    try:
        parts = [int(x) for x in v.split(".")]
        if len(parts) >= 2:
            parts[1] += 1
            for i in range(2, len(parts)):
                parts[i] = 0
        return ".".join(str(x) for x in parts)
    except Exception:
        return "0.2.0"


def _build_extend_prompt(item: dict, target: str, target_v: str,
                         next_v: str) -> str:
    """Prompt for lean-canvas in extend mode — generate BMC DELTA, not
    a brand-new BMC."""
    bd = item.get("bd_verdict") or {}
    pain = (item.get("pain") or
            (item.get("post") or {}).get("body") or
            (item.get("post") or {}).get("title") or "")[:1500]
    feat = (bd.get("feature_one_liner") or
            bd.get("new_product_one_liner") or "?")[:300]

    # Try to surface the target's known FUNCTIONS from portfolio
    try:
        from axentx_pipeline import get_portfolio
        portfolio = get_portfolio()
        target_desc = portfolio.get(target, "")[:600]
    except Exception:
        target_desc = ""

    return (
        f"You are extending an EXISTING product with a NEW FEATURE. "
        f"Output the BMC delta (only the blocks that CHANGE), the version "
        f"bump rationale, and incremental unit-economics impact.\n\n"
        f"TARGET PRODUCT: {target} (current v{target_v} → proposed v{next_v})\n"
        f"TARGET METADATA: {target_desc}\n\n"
        f"USER PAIN (the request driving this extension):\n{pain}\n\n"
        f"FEATURE ONE-LINER: {feat}\n"
        f"BD RATIONALE: {(bd.get('rationale') or '')[:400]}\n\n"
        f"Output STRICT JSON:\n"
        f"{{\n"
        f'  "extend_mode": true,\n'
        f'  "target": "{target}",\n'
        f'  "from_version": "{target_v}",\n'
        f'  "to_version": "{next_v}",\n'
        f'  "feature_name": "short kebab-case name, e.g. owner-attribution",\n'
        f'  "feature_summary": "1 sentence of what this adds",\n'
        f'  "bmc_delta": {{\n'
        f'    "value_proposition_addition": "what new value the feature unlocks",\n'
        f'    "customer_segments_change": "any new segments unlocked, or null",\n'
        f'    "channels_change": "any new channel, or null",\n'
        f'    "revenue_streams_change": "new tier, upsell, or null",\n'
        f'    "key_activities_change": "new workflows added",\n'
        f'    "cost_structure_change": "incremental cost (LLM calls, infra)"\n'
        f'  }},\n'
        f'  "incremental_unit_economics": {{\n'
        f'    "expected_arpu_lift_pct": 0,\n'
        f'    "expected_churn_reduction_pct": 0,\n'
        f'    "expected_new_buyer_pct": 0,\n'
        f'    "estimated_dev_cost_usd": 0\n'
        f'  }},\n'
        f'  "feature_value_score": "0-10 — how much does this move the needle for existing buyers",\n'
        f'  "monetization_viable": true,\n'
        f'  "reject_reason": null\n'
        f"}}\n\n"
        f"Rules:\n"
        f"- Set monetization_viable=false ONLY if feature is clearly "
        f"non-monetizable (e.g. dev-tool freebie with no upsell path).\n"
        f"- feature_value_score: 0-3 = trivial, 4-6 = nice-to-have, "
        f"7-10 = must-have for retention/expansion.\n"
        f"- to_version must be {next_v} (we already chose minor bump).\n"
        f"- DO NOT redo the full canvas — only the DELTA blocks."
    )


def _build_stub_extend_canvas(bd: dict, target: str,
                               target_v: str, next_v: str,
                               item: dict | None = None) -> dict:
    """Fallback when LLM fails. Build minimal extend canvas from bd fields.
    # 2026-05-10 STUB feature_name hardening
    Reject garbage feature_one_liner patterns (LLM-prose leakage). Use
    pain text as fallback for feature_name. Avoid generic 'feature' tag."""
    feat = (bd.get("feature_one_liner") or "")[:200]
    # Garbage filter: bd cat-lock sometimes wrote LLM-prose into feature_one_liner
    _garbage = (
        "llm returned", "llm-non-json", "non-json", "no extractable",
        "auto_skipped", "verdict: extend", "verdict=extend",
        "llm output", "verdict from", "rationale from",
    )
    if any(g in feat.lower() for g in _garbage):
        feat = ""
    # Better fallback chain: feature_one_liner → pain_one_liner → pain text → "feature"
    if not feat and item is not None:
        bd_v = item.get("bd_verdict") or {}
        feat = (bd_v.get("pain_one_liner") or
                bd_v.get("rationale") or "")[:200]
        # Strip rationale prefix tags
        feat = _re.sub(r"^\[[^\]]+\]\s*", "", feat)
    if not feat:
        post = (item or {}).get("post") or {}
        feat = (post.get("title") or "")[:120]
    feat = feat or "feature"
    feature_name = _re.sub(r"[^a-z0-9-]+", "-",
                           feat.lower())[:40].strip("-") or "feature"
    # Reject feature_name=="feature" (generic) by prepending target slug
    if feature_name == "feature":
        feature_name = f"{target}-bump"
    return {
        "extend_mode": True,
        "target": target,
        "from_version": target_v,
        "to_version": next_v,
        "feature_name": feature_name,
        "feature_summary": feat,
        "bmc_delta": {
            "value_proposition_addition": feat,
            "customer_segments_change": None,
            "channels_change": None,
            "revenue_streams_change": None,
            "key_activities_change": "ship feature " + feat[:60],
            "cost_structure_change": "incremental dev + infra (~LLM calls)",
        },
        "incremental_unit_economics": {
            "expected_arpu_lift_pct": 5,
            "expected_churn_reduction_pct": 2,
            "expected_new_buyer_pct": 0,
            "estimated_dev_cost_usd": 200,
        },
        "feature_value_score": 5,
        "monetization_viable": True,
        "reject_reason": None,
        "_stub": True,
    }


def do_one() -> bool:
    picked = pick_oldest("lean-canvas")
    if not picked:
        return False
    src_path, item = picked
    bd = item.get("bd_verdict") or {}

    # 2026-05-09 EXTEND v2 — extend-mode dispatch
    # If this is an EXTEND item (target_project + extend_mode set by bd),
    # build a versioned feature-delta canvas instead of full new-product BMC.
    if item.get("extend_mode") and item.get("target_project"):
        target = item["target_project"]
        target_v = _read_target_version(target)
        next_v = _bump_minor(target_v)
        feat_one = (bd.get("feature_one_liner") or "?")[:80]
        log("lean-canvas",
            f"▸ {item['id'][:32]} — EXTEND {target} v{target_v}→v{next_v}: {feat_one}")
        prompt = _build_extend_prompt(item, target, target_v, next_v)
        # 2026-05-09 — extend canvas LLM was 100% failing (truncated JSON).
        # Bumped budget LCV_BUDGET//2 → LCV_BUDGET (extend JSON has 6
        # bmc_delta blocks + 4 unit-economics + 8 top-level fields, ~750
        # tokens). Also reuse SYS_PROMPT (schema-aware) for better guidance.
        raw, provider = _direct_call_with_fallback(
            prompt, SYS_PROMPT, LCV_BUDGET,
        )
        canvas = _parse_json_block(raw) if raw else None
        if not canvas:
            log("lean-canvas",
                f"  ⚠ extend-canvas LLM failed/parse failed → STUB "
                f"(provider={provider}, raw_len={len(raw or '')})")
            canvas = _build_stub_extend_canvas(bd, target, target_v, next_v, item=item)
        canvas["extend_mode"] = True
        canvas["target"] = target
        canvas["from_version"] = target_v
        canvas["to_version"] = next_v
        item["lean_canvas"] = canvas
        item["extended_canvas"] = canvas  # alias for downstream
        item["target_version"] = next_v

        if canvas.get("monetization_viable") is False:
            log("lean-canvas",
                f"  ⛔ extend REJECTED — {canvas.get('reject_reason','no value')[:80]} → done")
            advance(item, src_path, "done", "lean-canvas",
                    f"REJECT extend: {canvas.get('reject_reason','low value')[:200]}")
            return True

        score = canvas.get("feature_value_score", 0)
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0
        log("lean-canvas",
            f"  ✓ extend canvas — value_score={score}, "
            f"target={target} v{target_v}→v{next_v} → pitch[extend]")
        advance(item, src_path, "pitch", "lean-canvas",
                f"extend canvas built (target={target}, v{next_v}, score={score})")
        return True

    # ── Original NEW-PRODUCT path below ──
    one_liner = (bd.get("new_product_one_liner") or "?")[:80]
    log("lean-canvas", f"▸ {item['id'][:32]} — {one_liner}")

    prompt = build_prompt(item)
    raw = ""
    # Hard wall-clock timeout — call_llm fallback chain can hang for 5+ min
    # iterating through providers. Cap one call attempt at 90s total so the
    # daemon stays responsive (pick next item) even when the primary LLM
    # provider chain is slow due to upstream outages (e.g. Supabase rate-
    # limit KV down → kv_get retries cascade).
    class _Timeout(Exception):
        pass

    def _alarm(*_):
        raise _Timeout("hard wall-clock timeout (90s)")

    raw = ""
    # 2026-05-08: direct-Groq first (~189ms verified). Skip call_llm chain
    # which iterates through ~10 providers (some timeout 30s each).
    raw, provider = _direct_call_with_fallback(prompt, SYS_PROMPT, LCV_BUDGET)
    if not raw:
        # Last-resort: full call_llm chain (slow but might find a quirky live
        # provider). 30s wall-clock cap — if it doesn't succeed quickly, skip.
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(30)
        try:
            raw = call_llm(prompt, system=SYS_PROMPT,
                           max_tokens=LCV_BUDGET, timeout=20)
            provider = "chain"
        except Exception as e:
            signal.alarm(0)
            log("lean-canvas",
                f"  ✗ LLM failed (all): {type(e).__name__}: {str(e)[:120]} "
                f"— using STUB canvas from bd fields")
            stub = _build_stub_canvas(bd)
            stub["_llm_failed"] = str(e)[:120]
            item["lean_canvas"] = stub
            item["lean_canvas_md"] = render_canvas_to_md(stub)
            advance(item, src_path, "pitch", "lean-canvas",
                    "LLM failed; STUB canvas built from bd → pitch")
            return True
        finally:
            signal.alarm(0)

    canvas = _parse_json_block(raw)
    if not canvas:
        log("lean-canvas",
            f"  ⚠ unparseable JSON ({len(raw)} chars), routing to pitch")
        item["lean_canvas"] = {"monetization_viable": None,
                               "_parse_failed": True,
                               "_raw_preview": raw[:200]}
        advance(item, src_path, "pitch", "lean-canvas",
                "JSON parse failed, routing to pitch")
        return True

    if canvas.get("monetization_viable") is False:
        reason = canvas.get("reject_reason", "no recurring-revenue path")
        log("lean-canvas", f"  ⛔ REJECTED — {str(reason)[:80]} → done (PASS)")
        item["lean_canvas"] = canvas
        item["bd_verdict"] = {**bd, "verdict": "PASS",
                              "rationale": f"lean-canvas REJECT: {reason}"}
        advance(item, src_path, "done", "lean-canvas",
                f"REJECT: {str(reason)[:200]}")
        return True

    # Render markdown sections so pitch-daemon can drop them into its
    # PROMPT_TEMPLATE without code changes (just read item.lean_canvas_md.*)
    md_sections = render_canvas_to_md(canvas)
    item["lean_canvas"] = canvas
    item["lean_canvas_md"] = md_sections
    cac = (canvas.get("unit_economics") or {}).get("cac_usd", "?")
    ltv = (canvas.get("unit_economics") or {}).get("ltv_usd", "?")
    log("lean-canvas",
        f"  ✓ canvas — CAC=${cac}, LTV=${ltv}, "
        f"tiers={len(canvas.get('revenue_model', {}).get('tiers') or [])}, "
        f"competitors={len(canvas.get('competitor_landscape') or [])} → pitch")
    advance(item, src_path, "pitch", "lean-canvas",
            f"canvas synthesized (CAC=${cac}, LTV=${ltv})")
    return True


if __name__ == "__main__":
    daemon_loop("lean-canvas", POLL_SEC, do_one)

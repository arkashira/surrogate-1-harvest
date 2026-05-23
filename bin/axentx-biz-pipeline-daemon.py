#!/usr/bin/env python3
"""axentx TRACK B — Non-IT Business Pipeline (consolidated daemon).

Handles 5 stages in single daemon (saves resources, reduces complexity):
  biz-research → biz-validate → biz-pitch → biz-plan → biz-done

Output: business plan markdown in /opt/axentx-biz/<slug>/

Each cycle picks ONE item, advances it through all 5 stages atomically.
This is unlike TRACK A which has separate daemon per stage — TRACK B
consolidates because:
  1. Lower volume expected (specialized non-IT)
  2. Each item benefits from holistic context (no inter-stage handoff)
  3. Faster end-to-end latency
"""
import datetime, json, os, sys, time, hashlib, re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_pipeline import (
    pick_oldest, advance, write_item, log, daemon_loop,
    new_item, call_llm_strong,
)

BIZ_ROOT = Path("/opt/axentx-biz")
BIZ_ROOT.mkdir(exist_ok=True)

# ─── 6-Persona Panel for Non-IT Business ─────────────────────────────────
ASIAN_TRADE_SYSTEM = (
    "You are a senior Asian trade specialist (15+ years sourcing from China/"
    "Japan/Korea/Vietnam). You evaluate trends:\n"
    "  • Is this product/service trending in source country?\n"
    "  • What's the typical landed-cost markup (CIF + customs + 7% VAT + 30% "
    "    Thai retail margin)?\n"
    "  • Are there Alibaba/1688/Coupang Wing suppliers reliable enough?\n"
    "  • Has it spread to neighbors (VN/MY/ID) — leading indicator for TH?\n"
    "Score 6+ when source-country trend is real + reliable supply chain. "
    "would_invest_or_pay=true when YOU would import 1 container as test."
)

TH_CONSUMER_SYSTEM = (
    "You are Head of Consumer Insights for a top Thai retailer (CP/Central/"
    "Lotus). You know Thai consumer behavior cold:\n"
    "  • Will Thai middle-class buy this? (B2C 40k-100k THB/mo income)\n"
    "  • Cultural fit: ภาพลักษณ์ / face / convenience / price-sensitivity\n"
    "  • Channel: Shopee/Lazada/TikTok Shop/LINE OA/offline?\n"
    "  • Thai price tolerance: ฿100-500 impulse / ฿1K-5K considered / ฿10K+ "
    "    research-heavy\n"
    "Score 6+ when there's clear Thai demand + price-channel fit. "
    "would_invest_or_pay=true when typical Thai household would buy."
)

RETAIL_OPERATOR_SYSTEM = (
    "You are an experienced retail/offline business operator running a "
    "300-SKU Thai shop chain. You think in unit economics:\n"
    "  • COGS % of retail price?\n"
    "  • Inventory turnover (faster = better)?\n"
    "  • Returns / damage rate?\n"
    "  • Working capital tied up in stock?\n"
    "Score 6+ when unit economics are real (gross margin >35%, turnover "
    "<60 days). would_invest_or_pay=true if YOU'd stock it in your shops."
)

IMPORT_LOGISTICS_SYSTEM = (
    "You are an import/logistics expert specializing in Asia-to-Thailand:\n"
    "  • Customs HS code + duty rate (some categories 0-30%+)\n"
    "  • TISI / FDA / กว. license requirements (food, electronics, beauty)\n"
    "  • BOI tax incentives applicable?\n"
    "  • Freight: sea (35-45 day, $) vs air ($$$)?\n"
    "  • Import barriers: gray-market existing? counterfeits?\n"
    "Score 6+ when import path is feasible + legal. would_invest_or_pay=true "
    "when paperwork burden < 3 months."
)

SME_OWNER_SYSTEM = (
    "You are a Thai SME owner (5-20 staff, ฿20-50M annual revenue) thinking "
    "bottom-up:\n"
    "  • Could I, with ฿500K-3M, run this as a side business?\n"
    "  • Where do I find customers cheaply (LINE OA / Facebook ad / market)?\n"
    "  • What hurts: competition / pricing pressure / supplier squeeze?\n"
    "  • Payback period: <12 months ideal, <24 months OK, >36 months risky\n"
    "Score 6+ when a real Thai SME would actually attempt. "
    "would_invest_or_pay=true when bootstrap-feasible."
)

BLUE_OCEAN_SYSTEM = (
    "You are a blue-ocean strategist. You judge gap between overseas markets "
    "and Thailand:\n"
    "  • Does this exist abroad (China/JP/KR/US/EU) but NOT in Thailand?\n"
    "  • If exists in TH but weak/early — is there room for stronger entrant?\n"
    "  • What's the wedge (price / quality / channel / timing)?\n"
    "  • Switching cost / retention story?\n"
    "Score 6+ when gap is real + defensible wedge. would_invest_or_pay=true "
    "when you'd put your own ฿1M+ behind a Thai-first entrant."
)

PANEL_WEIGHTS = {
    "Asian Trade Specialist":    1.4,
    "Thai Consumer Insights":    1.5,
    "Retail Operator":           1.2,
    "Import/Logistics Expert":   1.2,
    "Local SME Owner":           1.0,
    "Blue-Ocean Strategist":     1.3,
}
PANEL_TOTAL_WEIGHT = sum(PANEL_WEIGHTS.values())


PROMPT_TEMPLATE = """You are evaluating a NON-IT BUSINESS opportunity for Thailand.

# Pain/opportunity signal
{ctx}

# bd verdict (initial triage)
{bd_verdict}

# Evaluation task
As {persona_name}, output STRICT JSON:

{{
  "verdict": "GO|PIVOT|NO-GO",
  "score": 0-10,
  "top_strengths": ["3 bullets"],
  "top_concerns": ["3 bullets — be brutal"],
  "what_to_change": "if PIVOT: specific (1 sentence); else null",
  "would_invest_or_pay": true|false,
  "rationale": "1-2 sentences",
  "thai_market": {{
    "tam_thai_thb_millions": <int>,
    "demand_signal": "low|medium|high",
    "pricing_tier_thb": "<concrete range, e.g. 500-2000>",
    "channel_fit": "shopee|lazada|tiktok|line-oa|offline-shop|multi",
    "competitor_landscape": "none|weak|medium|strong",
    "blue_ocean_score": "0-10"
  }},
  "supply_chain": {{
    "source_country": "china|japan|korea|vietnam|local|other",
    "supplier_availability": "abundant|moderate|scarce",
    "landed_cost_pct_of_retail": <int 0-100>,
    "import_complexity": "easy|moderate|complex"
  }},
  "unit_economics": {{
    "gross_margin_pct": <int 0-100>,
    "payback_months": <int>,
    "min_capital_thb_millions": <float, e.g. 0.5 = ฿500K>
  }}
}}

Score 6+ ONLY when:
  - Real Thai demand + clear pricing
  - Reliable supply path
  - Defensible wedge OR blue-ocean
  - would_invest_or_pay=true (your money on the line)

NO-GO: no demand, saturated market, or unrealistic margins."""


def call_persona(system, persona_name, ctx_dict):
    _cd = dict(ctx_dict); _cd.pop("persona_name", None); prompt = PROMPT_TEMPLATE.format(persona_name=persona_name, **_cd)
    try:
        out = call_llm_strong(prompt, system=system, max_tokens=2000,
                              timeout=90, allow_degrade=True)
    except Exception as e:
        return {
            "verdict": "PIVOT",
            "score": 0,
            "rationale": f"persona LLM exception: {type(e).__name__}",
            "would_invest_or_pay": False,
            "persona": persona_name,
        }
    # Extract JSON
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if not m:
        return {
            "verdict": "PIVOT",
            "score": 0,
            "rationale": "panel response unparseable",
            "would_invest_or_pay": False,
            "persona": persona_name,
            "_raw": out[:400],
        }
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return {
            "verdict": "PIVOT",
            "score": 0,
            "rationale": "JSON parse failed",
            "would_invest_or_pay": False,
            "persona": persona_name,
            "_raw": m.group(0)[:400],
        }
    parsed["persona"] = persona_name
    return parsed


def consolidate(panel):
    """Weighted aggregation of 6 personas."""
    verdicts = [p.get("verdict", "PIVOT").upper() for p in panel]
    scores = [int(p.get("score", 0) or 0) for p in panel]
    weights = [PANEL_WEIGHTS.get(p.get("persona") or "", 1.0) for p in panel]
    total_w = sum(weights) or 1.0
    avg = sum(s * w for s, w in zip(scores, weights)) / total_w
    w_go = sum(w for v, w in zip(verdicts, weights) if v == "GO") / total_w
    w_no = sum(w for v, w in zip(verdicts, weights) if v == "NO-GO") / total_w
    w_invest = sum(
        PANEL_WEIGHTS.get(p.get("persona") or "", 1.0)
        for p in panel if p.get("would_invest_or_pay") is True
    ) / total_w
    w_invest_no = sum(
        PANEL_WEIGHTS.get(p.get("persona") or "", 1.0)
        for p in panel if p.get("would_invest_or_pay") is False
    ) / total_w
    # Decision
    if w_no >= 0.40 or avg < 3.5 or (w_invest_no >= 0.65 and avg < 5.0):
        final = "NO-GO"
    elif w_go >= 0.30 and avg >= 6.0 and w_invest >= 0.35:
        final = "GO"
    elif avg >= 5.5 and w_invest >= 0.30:
        # Biz-relaxed: decent score + decent invest = GO
        final = "GO"
    elif avg >= 5.0 and w_invest >= 0.45:
        final = "GO"
    else:
        final = "PIVOT"
    return {
        "final_verdict": final,
        "weighted_avg": round(avg, 2),
        "w_go_pct": round(w_go * 100, 1),
        "w_no_pct": round(w_no * 100, 1),
        "w_invest_pct": round(w_invest * 100, 1),
        "panel_size": len(panel),
        "panel": panel,
    }


def slug_from_text(text):
    """Generate a biz product slug from pain/opportunity text + timestamp.

    2026-05-06: added timestamp suffix to prevent collision when biz-pipeline
    produces multiple GOs from similar trend items (4 GO/hr but only 1 folder
    materialized). Now each plan gets unique dir.
    """
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", text[:200]).lower()
    words = [w for w in s.split() if 3 <= len(w) <= 12][:3]
    _uniq = int(time.time() * 1000) % 100000
    if not words:
        return f"biz-{_uniq}"
    slug = "-".join(words)[:24]
    return f"th-{slug}-{_uniq}"


def write_biz_plan(slug, item, summary):
    """Output: business plan markdown to /opt/axentx-biz/<slug>/biz-plan.md"""
    biz_dir = BIZ_ROOT / slug
    biz_dir.mkdir(parents=True, exist_ok=True)

    panel = summary.get("panel") or []
    md = [f"# {slug}", ""]
    md.append(f"## Pitch verdict: {summary['final_verdict']}")
    md.append(f"- Weighted avg: {summary['weighted_avg']}/10")
    md.append(f"- GO weight: {summary['w_go_pct']}% / NO-GO: {summary['w_no_pct']}%")
    md.append(f"- Invest signal: {summary['w_invest_pct']}%")
    md.append("")
    md.append("## Original pain/opportunity")
    md.append("```")
    md.append(((item.get("current") or {}).get("text") or "")[:1500])
    md.append("```")
    md.append("")
    md.append("## Panel evaluations")
    for p in panel:
        if not isinstance(p, dict): continue
        persona = p.get("persona", "?")
        md.append(f"### {persona}: {p.get('verdict')} (score={p.get('score')})")
        md.append(f"- Invest: {p.get('would_invest_or_pay')}")
        md.append(f"- Rationale: {p.get('rationale','')[:300]}")
        thai = p.get("thai_market") or {}
        if thai:
            md.append(f"- Thai TAM: {thai.get('tam_thai_thb_millions')}M THB | "
                      f"demand: {thai.get('demand_signal')} | "
                      f"channel: {thai.get('channel_fit')}")
        sc = p.get("supply_chain") or {}
        if sc:
            md.append(f"- Source: {sc.get('source_country')} | "
                      f"supplier: {sc.get('supplier_availability')} | "
                      f"landed cost: {sc.get('landed_cost_pct_of_retail')}%")
        ue = p.get("unit_economics") or {}
        if ue:
            md.append(f"- GM: {ue.get('gross_margin_pct')}% | "
                      f"payback: {ue.get('payback_months')}mo | "
                      f"min capital: ฿{ue.get('min_capital_thb_millions','?')}M")
        md.append("")

    plan_file = biz_dir / "biz-plan.md"
    plan_file.write_text("\n".join(md))
    return plan_file


def do_one():
    """Pick from biz-research queues. # 2026-05-11 priority queue: premium > standard > biz-research raw
    Priority: premium (validation-gated all-≥7) > validated (≥5) > raw."""
    # Try premium first — these passed validation-gate with ALL 3 scores ≥7
    picked = pick_oldest("premium-biz-research")
    tier = "premium"
    if not picked:
        # Standard validated — passed validation-gate with ALL 3 scores ≥5
        picked = pick_oldest("biz-research-validated")
        tier = "standard"
    if not picked:
        # Raw biz-research (legacy / un-gated)
        picked = pick_oldest("biz-research")
        tier = "raw"
    if not picked:
        return False
    src_path, item = picked
    item["biz_tier"] = tier
    if tier in ("premium", "standard"):
        log("biz-pipeline",
            f"💎 [{tier.upper()}] {item.get('id','?')[:48]}")

    bd_verdict = (item.get("history") or [{}])[-1].get("output", "")[:600]
    pain_text = (item.get("current") or {}).get("text", "")[:1500]
    if not pain_text:
        log("biz-pipeline", f"  ⚠ {item.get('id','?')[:32]} no pain text — skip")
        advance(item, src_path, "done", "biz-pipeline", "no_text")
        return True

    log("biz-pipeline", f"▸ {item.get('id','?')[:50]} — biz-pitch evaluating")

    ctx_dict = {"ctx": pain_text, "bd_verdict": bd_verdict, "persona_name": "?"}

    # Run 6 personas in parallel
    persona_specs = [
        (ASIAN_TRADE_SYSTEM,      "Asian Trade Specialist"),
        (TH_CONSUMER_SYSTEM,      "Thai Consumer Insights"),
        (RETAIL_OPERATOR_SYSTEM,  "Retail Operator"),
        (IMPORT_LOGISTICS_SYSTEM, "Import/Logistics Expert"),
        (SME_OWNER_SYSTEM,        "Local SME Owner"),
        (BLUE_OCEAN_SYSTEM,       "Blue-Ocean Strategist"),
    ]
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(call_persona, sys_p, name,
                             dict(ctx_dict, persona_name=name))
                   for sys_p, name in persona_specs]
        panel = [f.result() for f in futures]

    summary = consolidate(panel)
    final = summary["final_verdict"]
    item["biz_pitch_verdict"] = {
        "verdict": final,
        "weighted_avg": summary["weighted_avg"],
        "w_go_pct": summary["w_go_pct"],
        "w_invest_pct": summary["w_invest_pct"],
        "panel": panel,
    }

    log("biz-pipeline",
        f"  panel: {[p.get('verdict') for p in panel]} "
        f"(avg={summary['weighted_avg']}) → {final}")

    if final == "GO":
        # Generate slug + write business plan
        slug = slug_from_text(pain_text)
        item["biz_slug"] = slug
        plan_file = write_biz_plan(slug, item, summary)
        log("biz-pipeline",
            f"  💼 GO → {slug} → biz-plan written: {plan_file}")
        advance(item, src_path, "biz-done", "biz-pipeline",
                f"GO {slug} avg={summary['weighted_avg']}")
    elif final == "PIVOT":
        log("biz-pipeline", f"  ↺ PIVOT — feedback logged")
        advance(item, src_path, "biz-done", "biz-pipeline",
                f"PIVOT avg={summary['weighted_avg']}")
    else:
        log("biz-pipeline", f"  ❌ NO-GO killed")
        advance(item, src_path, "biz-done", "biz-pipeline",
                f"NO-GO avg={summary['weighted_avg']}")
    return True


if __name__ == "__main__":
    daemon_loop("biz-pipeline", 30, do_one)

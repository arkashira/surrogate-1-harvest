#!/usr/bin/env python3
"""axentx pitch — Shark-Tank-style validation between business-synthesis
and spawn. Reads the 8-9 business-pack docs, runs a multi-persona panel,
emits GO / NO-GO / PIVOT.

User directive 2026-05-04:
  > 'ก่อนจะได้ project ใหม่ หลังจาก ได้ BMC แล้ว เข้ากระบวนการ pitching
  >  เหมือนตอน pitch product กับ VC ใน startup เพื่อดูว่ามันสามารถไป
  >  ต่อได้ไหม โดยคิดเหมือน incubator + เจ้าของบริษัท + เหล่า shark
  >  ใน sharktank เพื่อ validate ว่ามันทำเงินได้จริงๆ ถ้าผ่านแล้ว
  >  สร้างจริงเลย จะได้ไม่เสียเวลา'

Pipeline slot:
  business-synthesis → ★ pitch ★ → design → architect → ux → prd → dev …

Panel (3 personas, separate LLM calls — diverse prompts so we don't
collapse to one voice):
  1. Incubator Partner (YC-style) — "is this a STARTUP? can it 10x?"
  2. Strategic Investor — "what's the moat? competitive defensibility?"
  3. Operator / Customer — "would a real customer pay $X right now?"

Each scores 0-10 + GO/NO-GO/PIVOT. Combined verdict:
  - 3× GO → spawn
  - 2× GO + 1× PIVOT → still spawn (most committee splits land here)
  - 2× NO-GO+ → kill (write to /business/pitch-result.md, advance done)
  - mixed PIVOT → send back to business-synthesis with consolidated feedback
"""
from __future__ import annotations
import datetime
import json
import os
import re
import sys
from pathlib import Path



# 2026-05-09 EXTEND v2 Phase 4
import json as _json4
import os as _os4
import urllib.request as _ur4


def _mark_portfolio_pending(target: str, feat_name: str,
                             to_version: str) -> None:
    """Append `· PENDING-v{to_version}: {feat_name}` to portfolio entry
    for `target`. Idempotent — won't double-append the same tag.
    Best-effort: silent fail on D1 errors (next pitch-GO retries)."""
    cf_token = _os4.environ.get("CLOUDFLARE_API_TOKEN", "")
    cf_acct = _os4.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    d1_id = _os4.environ.get("D1_DATABASE_ID",
                              "ae95ac58-7b7e-40d9-8708-518c23281ae6")
    if not cf_token or not cf_acct:
        return
    base = f"https://api.cloudflare.com/client/v4/accounts/{cf_acct}/d1/database/{d1_id}/query"

    # 1. Read current portfolio
    body = {"sql": "SELECT v FROM kv_store WHERE k = ?",
            "params": ["bd.portfolio"]}
    req = _ur4.Request(base, data=_json4.dumps(body).encode(),
                       method="POST",
                       headers={"Authorization": f"Bearer {cf_token}",
                                "Content-Type": "application/json"})
    with _ur4.urlopen(req, timeout=10) as r:
        d = _json4.loads(r.read())
    rows = (d.get("result") or [{}])[0].get("results") or []
    if not rows:
        return
    portfolio = _json4.loads(rows[0]["v"])
    products = portfolio.get("products") or {}
    desc = products.get(target)
    if not isinstance(desc, str):
        return
    tag = f" · PENDING-v{to_version}: {feat_name}"
    if tag.strip() in desc:
        return  # idempotent
    products[target] = desc + tag
    portfolio["products"] = products

    # 2. Write back
    body2 = {
        "sql": ("INSERT OR REPLACE INTO kv_store (k, v, who, ts) "
                "VALUES (?, ?, ?, unixepoch())"),
        "params": ["bd.portfolio", _json4.dumps(portfolio),
                   "extend-v2-pending"],
    }
    req2 = _ur4.Request(base, data=_json4.dumps(body2).encode(),
                        method="POST",
                        headers={"Authorization": f"Bearer {cf_token}",
                                 "Content-Type": "application/json"})
    _ur4.urlopen(req2, timeout=10).read()


REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/opt/surrogate-1-harvest"))
sys.path.insert(0, str(REPO_ROOT / "bin"))
from axentx_rag_context import attach_rag
from axentx_pipeline import (log, call_llm_strong, call_llm,  # noqa: E402
                             pick_oldest, advance, fail, daemon_loop,
                             get_role_budget, get_portfolio_block,
                              get_portfolio,)

POLL_SEC = int(os.environ.get("PITCH_POLL_SEC", "30"))
PROJECTS_ROOT = Path(os.environ.get("AXENTX_ROOT", "/opt/axentx"))
PITCH_BUDGET = get_role_budget("pitch", 1500)

GLOBAL_VC_SYSTEM = (
    "You are a senior partner at a top-tier global VC (think a16z, Sequoia, Index). "
    "You evaluate ideas SPECIFICALLY through a global-market lens. Your filter:\n"
    "  - Global TAM in USD (≥$50M minimum, ≥$500M strong)\n"
    "  - Identifying paying-customer segments worldwide\n"
    "  - Top 3-5 global competitors named with their wedge\n"
    "  - Whether non-Thai users would actually buy this\n"
    "  - Distribution channel (PLG, sales-led, marketplace, etc)\n\n"
    "Score 6+ when global market is real and the wedge defensible. "
    "Be a strong YES on PATH A when:\n"
    "  - TAM_global >= $50M USD\n"
    "  - <5 strong global competitors\n"
    "  - Identifiable paying-customer (B2B SaaS $100/seat or B2C subscription)\n"
    "Always populate global_market_analysis section. Set go_via_global=true "
    "when conditions met. would_invest_or_pay=true when willing to write check."
)


INCUBATOR_SYSTEM = (
    "You are a senior partner at a top-tier startup incubator (YC, "
    "Techstars, 500 Global combined). You see hundreds of pitches per "
    "batch and reject 99%. You evaluate ONLY two things: "
    "(1) is this venture-scalable — can the audience × ARPU × retention "
    "produce $10M ARR within 3 years? "
    "(2) is the team likely to execute — is the wedge sharp + the v1 "
    "shippable in <90 days?"
)

INVESTOR_SYSTEM = (
    "You are a strategic VC investor (Series-A focused). You see a "
    "pitched product. You evaluate ONLY: "
    "(1) defensible moat — what stops a copycat from launching in 60 days? "
    "(2) winner-take-most dynamics — network effect, data flywheel, "
    "switching cost, regulatory edge, distribution lock-in? "
    "Reject anything that's a feature dressed up as a product."
)

THAI_ANALYST_SYSTEM = (
    "You are a senior Thai market analyst from a top Bangkok VC. You evaluate "
    "ideas SPECIFICALLY through a Thai-blue-ocean lens. Your filter:\n"
    "  • Thai TAM in THB (millions or billions)\n"
    "  • Thai-specific competitors (named, with their wedge)\n"
    "  • Thai cultural/regulatory/payment fit (LINE OA, PromptPay, PDPA, BOI)\n"
    "  • Whether Thais would actually pay (B2C ฿100-500/mo? B2B ฿5K-50K/mo?)\n"
    "  • Thai market growth signal (ขยายตัว/อิ่มตัว/ยังไม่เริ่ม)\n\n"
    "Score liberally (5-8 range) when Thai market has REAL gap + growing. "
    "Be a strong YES on PATH B (Thai-blue-ocean) when:\n"
    "  - TAM_thai >= ฿300M\n"
    "  - Thai_competitors in (none, weak)\n"
    "  - Thai_growth_signal in (ขยายตัว, growing, emerging)\n"
    "Even if global has competitors, this counts as Thai-blue-ocean.\n"
    "Always populate thai_gap_analysis section in JSON output. Set "
    "go_via_thai_gap=true when above 3 conditions met. Set "
    "would_invest_or_pay=true when Thai TAM is real and pricing realistic."
)


CUSTOMER_SYSTEM = (
    "You are a brutally-honest target customer for the pitched product. "
    "You actually have the pain described. You evaluate ONLY: "
    "(1) would I pull out my credit card TODAY at the listed price? "
    "(2) what 1 thing would I change to actually use this? "
    "Be direct. If the price is wrong, say it. If you'd rather use a "
    "free alternative, say it."
)

# 2026-05-06: Panel expansion — 6 personas, weighted voting per user spec.
# "หลากหลายช่วงวัย สายอาชีพ ทำเป็น persona ไว้ vote จะได้ weight ได้จริงๆ"
ANGEL_SYSTEM = (
    "You are a Thai/SEA angel investor with personal money on the line. "
    "You write checks of ฿500K-฿5M to early ventures. You evaluate gut-first:\n"
    "  • Will founders ship something real in 90 days? (execution risk)\n"
    "  • Is the founder coachable + obsessed with the problem?\n"
    "  • Would my friends actually pay for this? (1 layer of validation)\n"
    "  • Can I see ฿10-50M revenue within 3 years (ROI scenario)?\n\n"
    "Be more forgiving than VCs on scale, stricter on execution + character. "
    "Score 6+ when execution path is clear AND personal-network would buy. "
    "would_invest_or_pay=true when you'd write the angel check yourself "
    "(฿1M+ risk on personal balance sheet)."
)


MENTOR_SYSTEM = (
    "You are a senior industry expert + startup mentor (15+ years in the "
    "domain). You judge feasibility, NOT financing. Your filter:\n"
    "  • Is the technical path real? (no hand-waving)\n"
    "  • Is the team's wedge SHARPER than what 100 other founders would do?\n"
    "  • What 1 fatal flaw will kill this in 12 months? (be specific)\n"
    "  • What's the 'aha' moment that makes a customer renew?\n\n"
    "Score on feasibility 1-10 (not on TAM). Score 6+ when path is "
    "executable + retention story credible. would_invest_or_pay=true when "
    "you'd join as advisor / personally use it. Always populate "
    "what_to_change with the SHARPEST one-line feedback."
)


PANEL_WEIGHTS = {
    "Global VC Partner":     1.5,
    "Angel Investor":        1.0,
    "Strategic Investor":    1.3,
    "Industry Mentor":       1.2,
    "Thai Market Analyst":   1.4,
    "Target Customer":       1.0,
}
PANEL_TOTAL_WEIGHT = sum(PANEL_WEIGHTS.values())


# 2026-05-06 — Classic 12 pitch-deck questions per user spec.
# Every persona must score the pitch's ability to ANSWER each question.
# Rubric_avg is a HARD GATE on the final verdict.
PITCH_12Q_RUBRIC = [
    ("why",          "ทำไมทำสิ่งนี้? แรงผลักดันคืออะไร?"),
    ("customer",     "ลูกค้าคือใคร? product แก้ปัญหาเขาอย่างไร?"),
    ("competitor",   "คู่แข่งคือใคร? เราต่างยังไง?"),
    ("biz_model",    "Business Model คืออะไร? scale ได้แค่ไหน?"),
    ("tam_sam_som",  "TAM / SAM / SOM ขนาดเท่าไหร่?"),
    ("unfair_adv",   "Unfair Advantage คืออะไร?"),
    ("team",         "ทีมเชี่ยวชาญอะไร? ตรงกับธุรกิจมั้ย?"),
    ("use_of_funds", "ถ้าระดมทุน จะใช้เงินยังไง 2-3 ปี?"),
    ("metrics",      "ตัววัดสำคัญ + ปัจจัยรอด/ไม่รอด?"),
    ("cac_cltv",     "CAC vs CLTV เป็นยังไง?"),
    ("platform_leak","(ถ้า multi-sided) ป้องกัน platform leak ยังไง?"),
    ("exit",         "Exit strategy คืออะไร?"),
]




PROMPT_TEMPLATE = """You are reviewing a pre-launch product pitch.

# Business context
{ctx}

# Business pack (key sections)

## Revenue model
{revenue_md}

## BMC summary
{bmc_md}

## Marketing plan summary
{marketing_md}

## Tech spec summary
{tech_md}

## Break-even / unit economics
{breakeven_md}

# Your task
As {persona_name}, output STRICT JSON:

{{
  "verdict": "GO | NO-GO | PIVOT",
  "score": 0-10,
  "top_strengths": ["3 short bullets"],
  "top_concerns": ["3 short bullets — be brutal"],
  "specific_kill_question": "1 question that would kill the pitch if not answered well",
  "what_to_change": "if PIVOT: specific change required (1 sentence); else null",
  "would_invest_or_pay": true|false
}}

Rules:
- Two GO paths (BOTH require would_invest_or_pay=true):

  PATH A — Global market (international users primarily):
    score>=6 AND TAM_global>=$50M AND wedge_real AND <5 strong global competitors

  PATH B — Thai-blue-ocean (relax-friendly path):
    score>=5 AND TAM_thai>=฿300M AND Thai_competitors in (none, weak)
    AND Thai_growth_signal in (ขยายตัว, growing, emerging)
    (Global may have competitors — that is OK. Even Grab-style market where
     globals exist, if Thai market has gap or growth angle (rural delivery,
     B2B-only, niche cuisine, language-tailored) it qualifies.
     The point is: Thai market ALONE is fundable.)

Output JSON MUST include both paths:
  "thai_gap_analysis": {{
    "tam_thai_thb": <int millions>,
    "thai_competitor_count": <int>,
    "thai_competitor_strength": "none|weak|strong|dominated",
    "thai_users_use_global_alternative_as_primary": true|false,
    "thai_specific_advantage": "<1-line — language/regulation/payment/cultural>",
    "go_via_thai_gap": true|false
  }},
  "global_market_analysis": {{
    "tam_global_usd": <int millions>,
    "global_competitor_count": <int>,
    "global_competitor_strength": "none|weak|strong|dominated",
    "go_via_global": true|false
  }},
  "kill_switch_path_a": "<reason if PATH A failed>",
  "kill_switch_path_b": "<reason if PATH B failed>"

Verdict:
  GO    = (go_via_thai_gap=true) OR (go_via_global=true)
  NO-GO = both paths fail AND no clear pivot opportunity
  PIVOT = one path partial, fixable with adjustment
- Score < 5 OR competitor_strength="dominated" OR would_invest_or_pay=false → verdict=NO-GO
- Otherwise → verdict=PIVOT with specific change

CRITICAL — verdict=GO requires ALL of:
  1. Real, large TAM (≥$100M global OR ≥฿1B Thai)
  2. Specific paying-customer profile identified (who, why, how much)
  3. Competitor analysis named ≥3 competitors with their wedge AND why we win
  4. Clear monetization path with realistic price tier
  5. would_invest_or_pay=true (i.e. YOU would pay for this)

REQUIRED RUBRIC — score 0-10 how well the pitch ANSWERS each question
(0 = missing/silent, 4 = vague, 7 = clear, 10 = specific+defensible):

  "pitch_q_rubric": {{
    "why":           <0-10>,  // ทำไมทำสิ่งนี้? แรงผลักดัน?
    "customer":      <0-10>,  // ลูกค้าคือใคร? แก้ปัญหายังไง?
    "competitor":    <0-10>,  // คู่แข่ง? ต่างยังไง?
    "biz_model":     <0-10>,  // Business Model + scalability
    "tam_sam_som":   <0-10>,  // TAM/SAM/SOM ขนาด
    "unfair_adv":    <0-10>,  // Unfair Advantage มีไหม?
    "team":          <0-10>,  // ทีมเชี่ยวชาญตรงไหม?
    "use_of_funds":  <0-10>,  // แผนใช้เงิน 2-3 ปี
    "metrics":       <0-10>,  // ตัววัด + ปัจจัยรอด
    "cac_cltv":      <0-10>,  // CAC vs CLTV
    "platform_leak": <0-10>,  // ป้องกัน platform leak (ถ้า applicable; 7+ ถ้าไม่ใช่ multi-sided)
    "exit":          <0-10>   // Exit strategy
  }}

The rubric is BINDING — your final score MUST reflect how completely the
pitch answers these. If 5+ questions score <4, your verdict cannot be GO.

If 2+ are missing → NO-GO. If only 1 missing → PIVOT (could be fixable).
- Don't be polite. The user (founder) prefers honesty over diplomacy.
"""


def _read_doc(repo: Path, name: str, max_chars: int = 2000) -> str:
    """Read a doc from the project's /business/ folder, truncated."""
    p = repo / "business" / name
    if not p.exists():
        return f"(missing: {name})"
    try:
        return p.read_text(encoding="utf-8")[:max_chars]
    except Exception:
        return f"(read failed: {name})"


def _parse_json_block(out: str) -> dict | None:
    """Extract JSON from possibly-fenced markdown."""
    txt = out.strip()
    if "```" in txt:
        seg = txt.split("```", 2)
        if len(seg) >= 2:
            inner = seg[1]
            if inner.startswith("json"):
                inner = inner[4:]
            txt = inner.strip()
    # First try as-is
    try:
        return json.loads(txt)
    except Exception:
        pass
    # Greedy match a balanced top-level object
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def call_persona(system: str, persona_name: str, prompt_ctx: dict) -> dict:
    """Run one persona judge. Falls back to call_llm if strong fails."""
    prompt = PROMPT_TEMPLATE.format(persona_name=persona_name, **prompt_ctx)
    system = attach_rag(system, prompt[:1500], max_snippets=4,
                        header="## Similar past pains/products (from axentx RAG)")
    out = ""
    try:
        out = call_llm_strong(prompt, system=system,
                              max_tokens=PITCH_BUDGET, timeout=45)
    except Exception:
        try:
            out = call_llm(prompt, system=system,
                           max_tokens=PITCH_BUDGET, timeout=40)
        except Exception as e:
            # All LLM providers exhausted — return degraded PIVOT so the
            # queue keeps moving instead of looping forever.
            return {
                "verdict": "PIVOT",
                "score": 5,
                "top_strengths": [],
                "top_concerns": [f"LLM unavailable: {type(e).__name__}"],
                "specific_kill_question": "",
                "what_to_change": "re-run when LLM providers recover",
                "would_invest_or_pay": False,
                "persona": persona_name,
                "_llm_exhausted": True,
            }
    parsed = _parse_json_block(out)
    if not parsed:
        return {
            "verdict": "PIVOT",
            "score": 5,
            "top_strengths": [],
            "top_concerns": ["panel response unparseable"],
            "specific_kill_question": "",
            "what_to_change": "re-run pitch with cleaner inputs",
            "would_invest_or_pay": False,
            "persona": persona_name,
            "_raw": out[:400] if isinstance(out, str) else "",
        }
    parsed["persona"] = persona_name
    return parsed


def consolidate(panel: list[dict]) -> dict:
    """Combine 6-persona weighted verdicts into a final one + write a memo.

    2026-05-06: switched from raw counts (n_go/n_no/avg) to WEIGHTED
    aggregation. Each persona has a weight in PANEL_WEIGHTS reflecting
    decision-authority + signal quality. Weighted-percentages used in
    threshold checks below.
    """
    verdicts = [p.get("verdict", "PIVOT").upper() for p in panel]
    scores = [int(p.get("score", 0) or 0) for p in panel]
    weights = [PANEL_WEIGHTS.get(p.get("persona") or "", 1.0) for p in panel]
    total_w = sum(weights) or 1.0
    # Weighted average score (0-10 scale)
    avg = sum(s * w for s, w in zip(scores, weights)) / total_w
    # Weighted vote fractions (0.0-1.0)
    w_go = sum(w for v, w in zip(verdicts, weights) if v == "GO") / total_w
    w_no = sum(w for v, w in zip(verdicts, weights) if v == "NO-GO") / total_w
    # Legacy raw counts retained for downstream / display consumers
    n_go = sum(1 for v in verdicts if v == "GO")
    n_no = sum(1 for v in verdicts if v == "NO-GO")

    # 2026-05-05: thresholds loosened — too many false NO-GOs killing
    # promising ideas. Default to PIVOT (gives a 2nd-chance with feedback).
    # Only kill when ALL 3 personas vote NO-GO AND average is dismal.
    # 2026-05-05: dual-path GO. Thai-gap path needs lower bar (Thai market
    # may be smaller but blue-ocean = no real competition).
    n_invest = sum(1 for p in panel if p.get("would_invest_or_pay") is True)
    n_invest_false = sum(1 for p in panel if p.get("would_invest_or_pay") is False)
    # Weighted invest signals — used by tightened thresholds below
    w_invest = sum(
        PANEL_WEIGHTS.get(p.get("persona") or "", 1.0)
        for p in panel if p.get("would_invest_or_pay") is True
    ) / total_w
    w_invest_no = sum(
        PANEL_WEIGHTS.get(p.get("persona") or "", 1.0)
        for p in panel if p.get("would_invest_or_pay") is False
    ) / total_w
    # 2026-05-06: 12-question rubric — weighted average of each persona's
    # rubric_avg. Personas score 0-10 per question (12 questions total).
    # Rubric_avg measures how COMPLETELY the pitch answers the basics.
    rubric_keys = [k for k, _ in PITCH_12Q_RUBRIC]
    persona_rubric_avgs = []
    persona_rubric_weights = []
    n_weak_questions_total = 0
    for p in panel:
        rub = p.get("pitch_q_rubric") or {}
        if not isinstance(rub, dict):
            continue
        scores_q = [float(rub.get(k, 0) or 0) for k in rubric_keys]
        valid_scores = [s for s in scores_q if 0 <= s <= 10]
        if not valid_scores:
            continue
        persona_rubric_avgs.append(sum(valid_scores) / len(valid_scores))
        persona_rubric_weights.append(
            PANEL_WEIGHTS.get(p.get("persona") or "", 1.0)
        )
        n_weak_questions_total += sum(1 for s in valid_scores if s < 4)
    # 2026-05-07: rubric_measured needs >= 3 personas reporting rubric.
    # Single low-rubric persona was forcing the hard floor unfairly when
    # other personas didn't emit pitch_q_rubric (LLM JSON gaps).
    rubric_measured = len(persona_rubric_avgs) >= 3
    if rubric_measured:
        rubric_avg = sum(
            a * w for a, w in zip(persona_rubric_avgs, persona_rubric_weights)
        ) / sum(persona_rubric_weights)
        avg_weak_q = n_weak_questions_total / len(persona_rubric_avgs)
    else:
        # 2026-05-06: LLMs not outputting pitch_q_rubric → rubric un-measurable.
        # Don't penalize via rubric gates; fall back to score-only logic.
        rubric_avg = 7.0  # neutral (above 4 floor, above 6 downgrade)
        avg_weak_q = 0
    # Count personas signaling Thai-gap opportunity
    n_thai_gap = sum(
        1 for p in panel
        if isinstance(p, dict) and (
            (p.get("thai_gap_analysis") or {}).get("go_via_thai_gap") is True
            or "thai-gap" in str(p.get("verdict","")).lower()
        )
    )
    n_global_go = sum(
        1 for p in panel
        if isinstance(p, dict) and (
            (p.get("global_market_analysis") or {}).get("go_via_global") is True
        )
    )
    # TIGHTENED-2026-05-06: previous PATH B-relaxed too generous —
    # code-craft (1GO/1NO-GO/1PIVOT, 2 say won't pay) and sre-coach
    # (0 GOs, TAM_thai=0) both got GO incorrectly. Stricter now:
    n_thai_weak_comp = sum(
        1 for p in panel
        if isinstance(p, dict) and (
            (p.get("thai_gap_analysis") or {}).get(
                "thai_competitor_strength") in ("none", "weak")
        )
    )
    # Real Thai TAM: at least N personas report TAM >= 100M THB
    n_thai_tam_real = sum(
        1 for p in panel
        if isinstance(p, dict) and (
            float((p.get("thai_gap_analysis") or {}).get("tam_thai_thb") or 0) >= 100
        )
    )
    # ─── 2026-05-06 WEIGHTED 6-panel thresholds + 12Q rubric gate ───
    # Use w_X (weighted fractions 0.0-1.0) for all gates so each persona's
    # vote carries proportional weight. PANEL_WEIGHTS sums to 7.4.
    #
    # HARD FLOORS (absolute kill, no override):
    #   - rubric_avg < 4.0                 → can't answer the 12 basics
    #   - avg_weak_q >= 5                  → 5+ questions silent/vague
    #   - ≥45% panel-weight says won't-pay → no money path
    #   - ≥40% panel-weight votes NO-GO   → strong rejection
    #   - weighted avg score < 3.5         → dismal
    # 2026-05-07: ALL-PIVOT escape hatch — if panel votes ≥5 PIVOT
    # AND zero NO-GO AND avg≥4.5, output PIVOT regardless of rubric.
    # Rationale: a unanimous PIVOT means panel is hedging "needs more info",
    # not killing. Killing via rubric here destroys legit refinement candidates.
    n_pivot_count = sum(1 for v in verdicts if v == "PIVOT")
    n_no_count = sum(1 for v in verdicts if v == "NO-GO")
    all_pivot_no_kill = (n_pivot_count >= 5 and n_no_count == 0 and avg >= 4.5)

    if all_pivot_no_kill:
        final = "PIVOT"
    elif rubric_avg < 4.0 or avg_weak_q >= 5 \
            or w_no >= 0.40 or avg < 3.5 or (w_invest_no >= 0.65 and avg < 5.0):
        final = "NO-GO"
    # PATH A: Global-strong consensus + monetization confidence
    #   ≥30% weighted GO votes + avg≥6.0 + ≥35% would-invest
    elif w_go >= 0.30 and avg >= 6.0 and w_invest >= 0.35:
        final = "GO"
    # PATH B-strict: Thai-blue-ocean with REAL TAM + at least 1 invest
    elif (n_thai_gap >= 1 or n_thai_weak_comp >= 1) \
            and n_thai_tam_real >= 1 and avg >= 4.5 and w_invest >= 0.20:
        final = "GO"
    # PATH B-relaxed: 2+ personas confirm real Thai TAM, high avg
    elif n_thai_tam_real >= 2 and avg >= 5.5:
        final = "GO"
    # PATH C: Global monetization clear (avg≥5.0 + ≥35% invest + zero invest_no)
    # 2026-05-07: PATH C relaxed — was w_invest_no==0 (one dissenter killed
    # the path). 0.20 = up to ~1 weighted persona can vote no-pay.
    elif avg >= 5.0 and w_invest >= 0.35 and w_invest_no <= 0.20:
        final = "GO"
    else:
        final = "PIVOT"
    # 2026-05-06 RUBRIC DOWNGRADE: if final=GO but the 12Q rubric is weak
    # (avg<6 or 3+ weak questions), demote to PIVOT — pitch is missing
    # answers that founders MUST have. Force them to refine the deck.
    if final == "GO" and (rubric_avg < 6.0 or avg_weak_q >= 3):
        final = "PIVOT"

    return {
        "final_verdict": final,
        "weighted_avg": round(avg, 2),
        "weighted_go_pct": round(w_go * 100, 1),
        "weighted_no_pct": round(w_no * 100, 1),
        "weighted_invest_pct": round(w_invest * 100, 1),
        "weighted_invest_no_pct": round(w_invest_no * 100, 1),
        "panel_size": len(panel),
        "rubric_avg": round(rubric_avg, 2),
        "rubric_weak_q_count": round(avg_weak_q, 1),
        "avg_score": round(avg, 1),
        "panel_breakdown": {p.get("persona", f"unknown-{i}"): {
            "verdict": p.get("verdict"),
            "score": p.get("score"),
            "would_invest_or_pay": p.get("would_invest_or_pay"),
        } for i, p in enumerate(panel)},
        "panel": panel,
    }


def render_pitch_md(item: dict, summary: dict) -> str:
    out = [
        f"# Pitch result: {item.get('project','?')}",
        f"- id: `{item['id']}`",
        f"- final_verdict: **{summary['final_verdict']}**",
        f"- avg_score: {summary['avg_score']}/10",
        "",
        "## Panel breakdown",
    ]
    for p in summary["panel"]:
        out.append(f"\n### {p['persona']} — {p.get('verdict')} ({p.get('score')}/10)")
        out.append(f"**would_invest_or_pay**: {p.get('would_invest_or_pay')}")
        out.append("\n**Strengths:**")
        for s in (p.get("top_strengths") or []):
            out.append(f"- {s}")
        out.append("\n**Concerns:**")
        for c in (p.get("top_concerns") or []):
            out.append(f"- {c}")
        kq = p.get("specific_kill_question") or ""
        if kq:
            out.append(f"\n**Kill question**: {kq}")
        wtc = p.get("what_to_change") or ""
        if wtc:
            out.append(f"\n**What to change**: {wtc}")
    return "\n".join(out)


def do_one() -> bool:
    # 2026-05-09 EXTEND v2 — extend-mode light rubric
    # EXTEND items get a LIGHT rubric: 3 personas instead of 6, threshold
    # avg≥5.0 (not 7.0), focus on incremental value not market-creation.
    # We use a programmatic score derived from feature_value_score in the
    # extended_canvas — no LLM call needed for the panel pass-through.
    _picked = pick_oldest("pitch")
    if _picked:
        _sp, _it = _picked
        if _it.get("extend_mode") and _it.get("extended_canvas"):
            ec = _it["extended_canvas"]
            try:
                fvs = float(ec.get("feature_value_score") or 0)
            except (TypeError, ValueError):
                fvs = 0.0
            target = _it.get("target_project") or "?"
            from_v = ec.get("from_version", "?")
            to_v = ec.get("to_version", "?")
            log("pitch",
                f"▸ {_it['id'][:32]} EXTEND[{target} v{from_v}→v{to_v}] "
                f"value_score={fvs}")
            # Threshold 5.0: ship feature unless clearly low-value
            if fvs >= 5.0:
                # 2026-05-09 EXTEND v2 Phase 4 — portfolio-grow
                # Phase 4: mark target as having a pending extension so
                # future bd verdicts don't re-spawn the same capability.
                feat_name = ec.get("feature_name", "feature")
                try:
                    _mark_portfolio_pending(target, feat_name, to_v)
                except Exception as _e:
                    log("pitch",
                        f"  ⚠ portfolio-grow soft-fail: {type(_e).__name__}")
                log("pitch",
                    f"  ✓ extend GO — value_score={fvs} ≥ 5.0 → design "
                    f"(portfolio: {target} +PENDING-v{to_v}:{feat_name})")
                advance(_it, _sp, "design", "pitch",
                        f"EXTEND-GO target={target} v{to_v} score={fvs}")
            else:
                log("pitch",
                    f"  ⛔ extend NO-GO — value_score={fvs} < 5.0 → done")
                advance(_it, _sp, "done", "pitch",
                        f"EXTEND-NOGO score={fvs} < 5.0")
            return True
        # Not extend — put it back so the original logic picks it up.
        # We mimic pick_oldest by re-injecting (write back if needed).
        # SIMPLER: re-call pick_oldest below — but that risks re-popping
        # a different item. So we shadow the variable instead and let
        # original code reuse `_picked`.
        # The original function uses `picked = pick_oldest("pitch")` —
        # we'll set picked = _picked here.
        picked = _picked  # reuse the already-picked tuple
    else:
        picked = None

    if picked is None:
        picked = pick_oldest("pitch")
    if not picked:
        return False
    src_path, item = picked
    bd = item.get("bd_verdict") or {}
    project = item.get("project") or item.get("target_project")

    # ── Pre-spawn pitch (added 2026-05-04 — pitch is now a GATE before spawn)
    # If no project + no repo, we evaluate based on bd-verdict context alone.
    # That's what the panel needs — we kill bad ideas BEFORE burning a GH org
    # slot + cloning + business-synthesis cycles. After GO, item routes back
    # to spawn-queue with pitch_verdict=GO, and the spawner finally creates
    # the real repo.
    pre_spawn = not project or not (PROJECTS_ROOT / project).exists()
    repo = (PROJECTS_ROOT / project) if project else None

    log("pitch",
        f"▸ {item['id'][:32]} {'pre-spawn' if pre_spawn else f'→ {project}'}")

    one_liner = (bd.get("new_product_one_liner")
                 or bd.get("feature_one_liner") or "?")
    ctx = (
        f"Product: {project or '(pre-spawn — slug TBD)'}\n"
        f"One-liner: {one_liner}\n"
        f"Audience (BD): {bd.get('buyer_persona','?')}\n"
        f"Pricing tier (BD): {bd.get('pricing_tier','?')}\n"
        f"Monetization model: {bd.get('monetization_model','?')}\n"
        f"BD rationale: {(bd.get('rationale') or '')[:300]}\n"
        f"Mode: {'PRE-SPAWN GATE — kill before repo creation if NO-GO' if pre_spawn else 'POST-SPAWN'}"
    )

    if pre_spawn:
        # 2026-05-06: lean-canvas-daemon enriches item.lean_canvas_md before
        # routing here. Use it if present (panel evaluates with real numbers);
        # fall back to placeholders only when canvas synthesis failed.
        lc_md = item.get("lean_canvas_md") or {}
        prompt_ctx = {
            "ctx": ctx,
            "revenue_md": lc_md.get("revenue_md") or
                "(pre-spawn — lean-canvas synthesis unavailable)",
            "bmc_md": lc_md.get("bmc_md") or
                "(pre-spawn — BMC synthesis unavailable)",
            "marketing_md": lc_md.get("marketing_md") or
                "(pre-spawn — marketing synthesis unavailable)",
            "tech_md": lc_md.get("tech_md") or
                "(pre-spawn — tech synthesis unavailable)",
            "breakeven_md": lc_md.get("breakeven_md") or
                "(pre-spawn — breakeven synthesis unavailable)",
        }
    else:
        prompt_ctx = {
            "ctx": ctx,
            "revenue_md": _read_doc(repo, "revenue-model.md", 2000),
            "bmc_md": _read_doc(repo, "business-model-canvas.md", 2000),
            "marketing_md": _read_doc(repo, "marketing-plan.md", 1500),
            "tech_md": _read_doc(repo, "tech-spec.md", 1500),
            "breakeven_md": _read_doc(repo, "breakeven.md", 1500),
        }

    # 2026-05-06 PANEL_EXPANSION: 6 weighted personas, all parallel.
    # Run 6 LLM calls concurrently → hits 6 different rate-limit buckets
    # → ~same latency as 3 sequential, much richer signal.
    from concurrent.futures import ThreadPoolExecutor
    persona_specs = [
        (GLOBAL_VC_SYSTEM,    "Global VC Partner"),
        (ANGEL_SYSTEM,        "Angel Investor"),
        (INVESTOR_SYSTEM,     "Strategic Investor"),
        (MENTOR_SYSTEM,       "Industry Mentor"),
        (THAI_ANALYST_SYSTEM, "Thai Market Analyst"),
        (CUSTOMER_SYSTEM,     "Target Customer"),
    ]
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [
            ex.submit(call_persona, sys_prompt, name, prompt_ctx)
            for sys_prompt, name in persona_specs
        ]
        panel = [f.result() for f in futures]
    summary = consolidate(panel)
    final = summary["final_verdict"]
    log("pitch",
        f"  panel: {[p.get('verdict') for p in panel]} "
        f"(avg={summary['avg_score']}) → {final}")

    # Write pitch-result.md into project repo (post-spawn) or stash on item
    if not pre_spawn and repo and repo.exists():
        biz_dir = repo / "business"
        biz_dir.mkdir(exist_ok=True)
        (biz_dir / "pitch-result.md").write_text(render_pitch_md(item, summary))

    item["pitch_result"] = summary
    item["pitch_verdict"] = {"verdict": final,
                             "avg_score": summary.get("avg_score"),
                             "panel": panel}

    if final == "GO":
        if pre_spawn:
            # Approved at gate → back to spawn queue, spawner now sees
            # pitch_verdict=GO and proceeds to create the real GH repo.
            advance(item, src_path, "spawn", "pitch",
                    f"pre-spawn GO (avg {summary['avg_score']}/10) → spawn-queue")
            log("pitch", f"  ✅ pre-spawn GO → spawn (repo creation unblocked)")
        else:
            advance(item, src_path, "competitor-intel", "pitch",
                    f"GO (avg {summary['avg_score']}/10) → competitor-intel")
            log("pitch", f"  ✅ GO → competitor-intel-queue")
    elif final == "PIVOT":
        feedback_lines = []
        for p in panel:
            wtc = p.get("what_to_change") or ""
            if wtc:
                feedback_lines.append(f"[{p['persona']}] {wtc}")
        item["pitch_feedback"] = "\n".join(feedback_lines)
        if pre_spawn:
            # Pre-spawn PIVOT: kill the new-product attempt; let it route to
            # 'done' with feedback so future bd cycles can convert similar
            # ideas into EXTEND verdicts. Don't bounce back to bs (no repo).
            advance(item, src_path, "done", "pitch",
                    f"pre-spawn PIVOT (no repo to refine): "
                    + " | ".join(feedback_lines)[:240])
            log("pitch", f"  ↺ pre-spawn PIVOT → done (feedback logged)")
        else:
            advance(item, src_path, "business-synthesis", "pitch",
                    f"PIVOT (avg {summary['avg_score']}): "
                    + " | ".join(feedback_lines)[:300])
            log("pitch", f"  ↺ PIVOT → business-synthesis (feedback: "
                        f"{(feedback_lines[0] if feedback_lines else '')[:60]})")
    else:  # NO-GO
        advance(item, src_path, "done", "pitch",
                f"{'pre-spawn ' if pre_spawn else ''}NO-GO "
                f"(avg {summary['avg_score']}/10): "
                f"{[p.get('top_concerns',[''])[0] for p in panel]}")
        log("pitch", f"  ❌ NO-GO — killed "
                    f"{'before spawn' if pre_spawn else 'after spawn'}")

    return True


if __name__ == "__main__":
    daemon_loop("pitch", POLL_SEC, do_one)

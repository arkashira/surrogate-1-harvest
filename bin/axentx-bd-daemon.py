#!/usr/bin/env python3
"""axentx BD daemon — opportunity classifier.

Consumes pain-point reports from research-queue. For each pain, asks the
LLM to classify it against the active axentx product portfolio:

  Costinel  — AWS cost analytics + anomaly detection
  vanguard  — security / compliance posture
  airship   — IaC / cloud platform deployment + DevSecOps tooling
  workio    — workflow automation
  surrogate — Surrogate-1 (this entire stack — autonomous AI dev agent)
  (axiomops removed 2026-05-02 — its scope rolled into airship; never target axiomops)

Verdict: either
  EXTEND <project>  → pain is best solved as a feature on an existing
                      project. Item proceeds to design-queue with the
                      target project tagged.
  NEW-PRODUCT       → pain demands a fresh product. Item proceeds to
                      design-queue with project=null; the design-thinking
                      daemon will validate fit before BMC.
  PASS              → pain is real but not strategic for axentx (e.g.
                      consumer apps, gaming, hardware). Marked done
                      with reason. Saves cycles downstream.

Note: BD does NOT decide on funding/build/ship. It only triages signal
quality and routes — design-thinking + business + marketing daemons
each contribute their lens before any code path is started.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, call_llm_strong,
                             pick_oldest, advance, fail, daemon_loop,
                             get_role_budget)

POLL_SEC = int(os.environ.get("BD_POLL_SEC", "60"))
BD_BUDGET = get_role_budget("bd", 500)


# Static fallback portfolio — used only if shared_kv["bd.portfolio"] unavailable
# (e.g. portfolio-syncer hasn't run yet). Live portfolio is loaded by
# load_portfolio_block() at every bd cycle so spawned products from
# /opt/axentx/* are visible to bd within 30 min of creation.


# 2026-05-09 retired-names guard
# arkship + axiomops + costinel (lowercase) consolidated to canonical
# (airship + Costinel). Don't EXTEND/spawn these names again.
_RETIRED_PRODUCTS = frozenset([
    "arkship", "axiomops", "costinel",  # 2026-05-09 consolidated
    # 2026-05-10 archived products → canonical mapping
    "sync-keeper",     # archived — devops-iac → use airship
    "cost-radar",      # archived — finops → use Costinel
    "cloud-pilot",     # archived — devops-iac → use airship
    "quote-trail",     # archived — fintech → use payment-shield
])

_BASE_PORTFOLIO = {
    # categories-v3 (2026-05-09) — rich CATEGORY/FUNCTIONS markup so the LLM
    # can detect functional overlap before recommending NEW-PRODUCT.
    "Costinel":  "[CATEGORY: finops] AWS cost analytics + anomaly detection · "
                 "FUNCTIONS: AWS cost analytics, anomaly detection, forecasting, "
                 "unused-resource detection, owner-attribution, rightsizing · "
                 "BUYER: SREs/finops at SaaS orgs with $10K+/mo cloud spend",
    "vanguard":  "[CATEGORY: security] CSPM · FUNCTIONS: misconfig detection (S3/IAM/SG), "
                 "drift detection, compliance audit (SOC2-lite), policy-as-code · "
                 "BUYER: compliance officers + solo devs needing SOC2-lite",
    "airship":   "[CATEGORY: devops-iac] IaC + multi-cloud DevSecOps unified · "
                 "FUNCTIONS: multi-cloud IaC, deploy-once-target-many, env parity, "
                 "Terraform+CDK orchestration, CI/CD glue · "
                 "BUYER: devs shipping AWS+GCP+CF wanting one tool not six",
    "workio":    "[CATEGORY: automation] Workflow automation (Zapier for eng teams) · "
                 "FUNCTIONS: workflow automation, GitHub/Slack/Jira/HF glue, "
                 "trigger-based pipelines · "
                 "BUYER: eng teams who outgrew Zapier",
    "surrogate": "[CATEGORY: ai-platform] Autonomous AI dev agent · "
                 "FUNCTIONS: autonomous AI dev agent, commits/reviews/tests/docs while "
                 "you sleep, multi-LLM router, agent swarm · "
                 "BUYER: indie devs + small teams wanting 24/7 dev throughput",
}


def load_portfolio_block() -> str:
    """Load live portfolio from shared_kv['bd.portfolio'] (refreshed every
    30min by axentx-portfolio-syncer-daemon). Falls back to _BASE_PORTFOLIO
    if shared_kv is unavailable. Format: numbered list ready to drop into
    BD_SYSTEM prompt."""
    products = dict(_BASE_PORTFOLIO)
    try:
        from axentx_shared import kv_get
        live = kv_get("bd.portfolio") or {}
        live_products = (live.get("products") or {}) if isinstance(live, dict) else {}
        if isinstance(live_products, dict) and live_products:
            # Live portfolio overrides base — includes spawned ashirapit/* products
            for slug, desc in live_products.items():
                if slug and isinstance(desc, str) and desc.strip():
                    products[slug] = desc.strip()
    except Exception:
        pass
    lines = ["Active axentx product portfolio (decide if pain fits one):", ""]
    for i, (slug, desc) in enumerate(sorted(products.items()), 1):
        lines.append(f"{i}. {slug}  — {desc}")
    lines.append("(axiomops removed — merged into airship 2026-05-02)")
    return "\n".join(lines)


# Initial portfolio block — refreshed once at module import time. The
# do_one_bd() loop also reloads it per-cycle in case of mid-flight updates.
PORTFOLIO = load_portfolio_block()

ANTI_PATTERNS = """ANTI-PATTERNS — IMMEDIATELY return verdict=PASS for ideas
that match any of these (they are graveyards):
- "AI Slack/Discord/Teams for X" (chat skin over an LLM, no defensible edge)
- "Notion clone" or "Notion for X" (block editor with vertical paint)
- "Another todo / task / habit tracker" (saturated; users churn)
- "Dashboard for Y" without one specific edge a generic BI tool can't ship
- "Marketplace for Z" without ONE side already committed (chicken/egg trap)
- "AI agent that does everything" (no concrete unit of value)
- "Wrapper around <ChatGPT|Claude|Gemini> that <generic verb>"
Set rationale="anti-pattern: <which one>" so we don't re-mine it later.

★ Open-source path (revised 2026-05-04 per user clarification
'opensource ได้ แต่น้อยไง ถึงบอก 10% — ต้องมีประโยชน์ต่อชุมชนจริงๆ'):
We accept ~10% of verdicts as OSS (quota-enforced via shared_kv counter
— bd does NOT need to throttle itself; just emit the right verdict).

OSS verdict (output_mode='open-source') is acceptable ONLY when ALL of:
  (1) clear DEV PAIN (not consumer/hobbyist) with concrete use-case
  (2) genuine community benefit signal — e.g. existing tools 50%+ stars
      gap, no good Thai/Asian-language equivalent, or fills a niche the
      paid SaaS market won't serve
  (3) wedge that the community can rally around (specific protocol,
      undocumented edge case, missing language binding, etc.)
DEFAULT for generic 'free CLI for X' / 'OSS wrapper' = PASS — those
generate stars not impact, and saturate the OSS landscape.
"""

REVENUE_GATE = """REVENUE GATE — every NEW-PRODUCT or EXTEND verdict MUST
have a credible monetization path. Reject ideas where the buyer:
- has no recurring budget (hobbyists, students, casual users)
- can solve with free OSS in <30min (commodity tools)
- shops on price alone (race-to-bottom commodities)
ACCEPT only if the audience has:
- recurring SaaS budget ($10-$500/user/mo is achievable)
- mission-critical use (downtime/security/compliance pain)
- deep workflow tie-in (switching cost > $1K)
"""

def _build_bd_system(portfolio_block: str) -> str:
    return (
        "You are a Head of BD doing portfolio triage. For each user pain point, "
        "decide which axentx product it fits — or whether it deserves a new "
        "product, or whether to pass entirely. **Every output product must have "
        "a revenue model — open-source-only ideas are rejected.**\n\n"
        f"{portfolio_block}\n\n"
        f"{ANTI_PATTERNS}\n\n"
        f"{REVENUE_GATE}\n"
        "Output STRICT JSON:\n\n"
        "{\n"
        '  "verdict": "EXTEND|NEW-PRODUCT|BIZ-OPPORTUNITY|PASS",\n'
        '  "target_project": "<exact slug from portfolio above, or null>",\n'
        '  "rationale": "1-2 sentences why this fit or why pass",\n'
        '  "feature_one_liner": "if EXTEND: the feature in one sentence",\n'
        '  "new_product_one_liner": "if NEW-PRODUCT: the product hypothesis in one sentence",\n'
        '  "tam_signal": "low|medium|high — how broad is the affected audience",\n'
        '  "monetization_model": "subscription|usage|enterprise|marketplace|none",\n'
        '  "monetization_signal": "low|medium|high — how clearly will buyers PAY recurring $",\n'
        '  "pricing_tier": "$X-Y/user/mo — concrete number, e.g., $29-99/user/mo",\n'
        '  "buyer_persona": "specific buyer (job-title + company-stage), e.g., \\"DevOps lead at 50-500 person SaaS\\"",\n'
        '  "axentx_advantage": "why we can win this vs a generic competitor (1 sentence)",\n'
        '  "anti_pattern_match": "name of matched anti-pattern, or null"\n'
        "}\n\n"
        "Rules:\n"
        "- ★ EXTEND-FIRST: if there is ANY existing portfolio slug whose "
        "domain is even loosely adjacent to this pain (e.g. cost-related "
        "→ Costinel, security/compliance → vanguard, deploy/CI → airship, "
        "workflow/automation → workio, AI/dev tooling → surrogate, etc.), "
        "PREFER EXTEND over NEW-PRODUCT. The portfolio above is the FULL "
        "list — every spawned ashirapit/* / arkashira/* product is a valid "
        "extend target. Default verdict for a non-anti-pattern pain that "
        "even partially fits an existing product = EXTEND.\n"
        # 2026-05-09 categories-v3 prompt
        "- ★ FUNCTIONS-OVERLAP CHECK (mandatory before NEW-PRODUCT): "
        "each portfolio entry has a FUNCTIONS: list and a [CATEGORY: ...] "
        "tag. BEFORE returning verdict=NEW-PRODUCT, scan EVERY product's "
        "FUNCTIONS list for capability overlap with the pain. Match by "
        "CAPABILITY not by slug name. Examples of overlap = EXTEND not NEW: "
        "  • pain 'team cost attribution' overlaps Costinel.functions=[owner-attribution] → EXTEND Costinel\n"
        "  • pain 'cron job dropped silently' overlaps cron-scout-audit.functions=[missing-run alerting] → EXTEND cron-scout-audit\n"
        "  • pain 'detect leaked S3' overlaps vanguard.functions=[misconfig detection] → EXTEND vanguard\n"
        "If FUNCTIONS-overlap exists with ANY product, EXTEND that product. "
        "Set target_project = exact slug of overlap match.\n"
        "- ★ CATEGORY-LOCK: if pain's domain matches a CATEGORY already "
        "covered (finops/security/devops-iac/automation/ai-platform/identity/"
        "web3/compliance/observability/ai-tools/sre-tools/fintech/healthtech), "
        "EXTEND the most-relevant product in that CATEGORY. NEW-PRODUCT in an "
        "ALREADY-COVERED category is BANNED unless pain is genuinely "
        "orthogonal (different buyer + different workflow + different value).\n"
        "- NEW-PRODUCT only when: (a) pain is in a domain NO existing "
        "product covers, AND (b) plausible monetization or open-source "
        "adoption path exists. Pitch panel will tighten further.\n"
        "- BIZ-OPPORTUNITY (NEW 2026-05-06): ONLY for NON-IT/NON-SOFTWARE "
        "businesses — physical products (food/beverage/beauty/health/lifestyle/"
        "hardware imports/retail/F&B/services). NEVER use BIZ for any "
        "software/SaaS/app/platform/API/AI/automation product, even if B2C "
        "consumer-facing. Examples:\n"
        "  ✓ BIZ: 'Import Korean cosmetics to Thailand' / 'Bubble tea franchise'\n"
        "  ✗ NOT BIZ: 'B2B outreach platform' (SaaS=NEW), 'Personal finance app' "
        "(software=NEW), 'AI image tool' (NEW)\n"
        "- PASS only for: clear anti-pattern match, OR pure bug reports, OR "
        "truly empty signal.\n"
        "- Sparse context is OK — bd is a CHEAP filter; pitch does the "
        "deep evaluation. BEST-GUESS verdict + low monetization_signal.\n"
        "- Be specific about feature_one_liner / new_product_one_liner — "
        "NO 'AI-powered platform' fluff.\n"
        "- pricing_tier MUST be a concrete dollar range — no 'TBD'.\n"
        "- If pain_one_liner empty AND no axentx_idea AND no raw post text, "
        "PASS with rationale='no extractable signal'."
    )


# Initial system prompt — rebuilt per-cycle inside do_one_bd() so newly
# spawned products are visible to bd within the next 30-min portfolio sync
# window without requiring a daemon restart.
PORTFOLIO_TEXT = PORTFOLIO  # alias for balance check (saturation count)
BD_SYSTEM = _build_bd_system(PORTFOLIO)


def do_one_bd() -> bool:
    picked = pick_oldest("bd")
    if not picked: return False
    src_path, item = picked
    pain = item.get("verdict", {}) or {}
    post = item.get("post", {}) or {}

    # ── Last-resort PASS gate (added 2026-05-04, revised again after user:
    #    "ก็แค่เปลี่ยน source LLM สิ pool กับ provider มีเป็นล้าน"). bd uses
    #    call_llm (broad pool of 11+ providers) as PRIMARY, falls to
    #    call_llm_strong only when LLM returns valid JSON but with low
    #    confidence. Pre-flight skip kept ONLY for the case where post.title
    #    + post.body + every other text field is literally empty — even the
    #    LLM has nothing to work with. No cheap-LLM recovery dance.
    src_axentx_idea_pre = (item.get("axentx_idea")
                           or pain.get("axentx_idea") or "").strip()
    pain_one_liner_pre = (pain.get("pain_one_liner")
                          or item.get("pain_one_liner") or "").strip()
    if not pain_one_liner_pre and not src_axentx_idea_pre:
        raw_blobs = [
            (post.get("title") or "").strip(),
            (post.get("body") or post.get("text") or
             post.get("selftext") or "").strip(),
            (item.get("snippet") or "").strip(),
            (item.get("text") or "").strip(),
        ]
        raw_text = "\n\n".join(b for b in raw_blobs if b)[:1500]
        if len(raw_text) < 30:
            # Literally nothing for any LLM to work with — safe PASS.
            log("bd", f"  ⤷ {item['id'][:30]} truly-empty → PASS")
            item.setdefault("history", []).append({
                "stage": "bd", "actor": "axentx-bd",
                "output": f"truly-empty PASS (raw_len={len(raw_text)})",
                "at": datetime.datetime.utcnow().isoformat() + "Z",
            })
            item["bd_verdict"] = {
                "verdict": "PASS",
                "rationale": "truly-empty: no pain/idea/post-body/snippet",
                "auto_skipped": True,
                "raw_text_len": len(raw_text),
            }
            advance(item, src_path, "done", "bd",
                    f"truly-empty PASS (raw_len={len(raw_text)})")
            return True
        # Raw text exists — let the main bd LLM (broad pool) handle it
        # below. We inject raw_text into the prompt so the LLM has signal
        # to work with even when pain_one_liner is empty.
        item["_bd_raw_fallback"] = raw_text

    # Per-cycle reload: portfolio-syncer-daemon refreshes shared_kv["bd.portfolio"]
    # every 30 min (BASE 5 + spawned ashirapit/* products). Reading per-cycle
    # means bd sees newly-spawned products immediately, no daemon restart needed.
    bd_system = _build_bd_system(load_portfolio_block())

    log("bd", f"▸ {item['id'][:30]}  pain: {pain.get('pain_one_liner','')[:60]}")

    # 2026-05-04: trust signals already set by high-authority discovery
    # daemons (yc-rfs / revenue-verified / ih / medium-crawler / etc).
    # They've done LLM extraction with richer context than bd has.
    src_mon = item.get("monetization_signal") or pain.get("monetization_signal", "")
    src_audience = item.get("audience") or pain.get("audience", "")
    src_pricing = item.get("pricing_signal") or pain.get("pricing_signal", "")
    src_axentx_idea = item.get("axentx_idea") or pain.get("axentx_idea", "")
    authority = float(item.get("authority_score", 0.0) or 0.0)

    # If a high-authority source already extracted rich signals, surface
    # them in the bd prompt so bd doesn't re-derive from scratch (which
    # often fails with 'insufficient information').
    enriched_block = ""
    if authority >= 0.7 or src_axentx_idea:
        enriched_block = (
            f"\n\n# Pre-extracted signals (from {item.get('source','?')} "
            f"authority={authority:.2f}):\n"
            f"  audience: {src_audience}\n"
            f"  monetization_signal: {src_mon}\n"
            f"  pricing_hint: {src_pricing}\n"
            f"  axentx_idea: {src_axentx_idea[:240]}\n"
            f"  source-derived from richer context — TRUST these unless\n"
            f"  they're clearly an anti-pattern."
        )

    raw_fb = item.get("_bd_raw_fallback", "")
    raw_block = (f"\n\n# Raw post (no pre-extracted pain — derive from this):\n"
                 f"{raw_fb[:1200]}") if raw_fb else ""
    prompt = (
        f"Pain summary: {pain.get('pain_one_liner','?') or item.get('pain_one_liner','?')}\n"
        f"Domain: {pain.get('domain','?')}\n"
        f"Audience: {src_audience or pain.get('audience','?')}\n"
        f"Severity: {pain.get('severity','?')}/10\n"
        f"Source: {item.get('source') or post.get('source','?')} "
        f"({post.get('score',0)} score, {post.get('num_comments',0)} comments)\n"
        f"Evidence quote: {pain.get('evidence','')[:300]}"
        f"{enriched_block}{raw_block}\n\n"
        f"Your verdict (strict JSON only):"
    )
    try:
        # PRIMARY: call_llm (broad pool — HF Router × 5 tokens, GH Models × 10
        # tokens, Cerebras, Groq, SambaNova, Gemini, OpenRouter, ...). Top-tier
        # call_llm_strong is too rate-limited to be primary. User feedback
        # 2026-05-04: 'pool กับ provider มีเป็นล้าน' — use them all.
        try:
            out = call_llm(prompt, system=bd_system,
                           max_tokens=BD_BUDGET, timeout=35)
        except Exception:
            # Last-resort: try strong (top-tier) if broad pool exhausted
            out = call_llm_strong(prompt, system=bd_system,
                                  max_tokens=BD_BUDGET, timeout=45)
        # 2026-05-09 robustness — JSON-extract + retired-guard + cat-enforce
        # Robust JSON extract: LLMs sometimes wrap in fences, prepend prose,
        # or trail commentary. Try (1) raw parse, (2) ```...``` block, (3)
        # regex first {...} object. PASS only if all 3 fail.
        txt = (out or "").strip()
        verdict = None
        # Try 1: direct parse
        try:
            verdict = json.loads(txt)
        except json.JSONDecodeError:
            pass
        # Try 2: ```json ... ``` fenced
        if verdict is None and "```" in txt:
            for chunk in txt.split("```"):
                c = chunk.strip()
                if c.startswith("json"):
                    c = c[4:].strip()
                if c.startswith("{"):
                    try:
                        verdict = json.loads(c)
                        break
                    except json.JSONDecodeError:
                        continue
        # Try 3: regex extract first balanced {...}
        if verdict is None:
            import re as _re_bd
            m = _re_bd.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", txt, _re_bd.DOTALL)
            if m:
                try:
                    verdict = json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        if verdict is None:
            raise json.JSONDecodeError("no parseable JSON in LLM output", txt, 0)
    except json.JSONDecodeError:
        # LLM returned non-JSON (often "Insufficient information" prose).
        # 2026-05-09 catlock-fallback — non-JSON path also runs cat-lock
        # OLD: synthesize PASS + advance("done") + return True — never
        # reached cat-lock. NEW: synthesize verdict dict, log, then FALL
        # THROUGH to cat-lock/retired-guard/overlap-guard checks below.
        # Those will potentially flip PASS → EXTEND for pains that match
        # known FUNCTIONS keywords. If still PASS after guards, normal
        # routing at end-of-function sends to done.
        log("bd", f"  ⤷ {item['id'][:30]} LLM-non-JSON → PASS "
                  f"(rationale from LLM prose; cat-lock will retry)")
        verdict = {
            "verdict": "PASS",
            "target_project": None,
            "rationale": f"LLM returned non-JSON: {(out or '')[:200]}",
            "feature_one_liner": None,
            "new_product_one_liner": None,
            "tam_signal": "low",
            "monetization_model": "none",
            "monetization_signal": "low",
            "pricing_tier": None,
            "buyer_persona": None,
            "axentx_advantage": None,
            "anti_pattern_match": None,
            "auto_skipped": True,
        }
        # Fall through (no return) so guards can flip PASS → EXTEND
    except Exception as e:
        # Genuine LLM/network error — don't mark as parse-fail (could lose
        # real pain). fail() bumps retry counter; item stays in bd.
        fail(item, src_path, "bd", f"LLM unavailable: {type(e).__name__}")
        log("bd", f"  ↺ {item['id'][:30]}: LLM err — requeue")
        return True

    
    # 2026-05-11 EXTEND balance — avoid saturated products (≥10 PENDING)
    # If chosen EXTEND target has ≥10 PENDING features, look for less-saturated
    # product in same category (per portfolio metadata). This prevents
    # airship/Costinel/cloud-lab from monopolizing all EXTEND verdicts while
    # other products go unused.
    if verdict.get("verdict") == "EXTEND":
        _t = (verdict.get("target_project") or "").strip()
        if _t and _t in PORTFOLIO_TEXT:
            # Count PENDING tags in target's portfolio entry
            _entry = PORTFOLIO_TEXT.split(_t, 1)
            if len(_entry) > 1:
                _line_end = _entry[1].find("\n")
                _line = _entry[1][:_line_end] if _line_end > 0 else _entry[1][:500]
                _pending_count = _line.count("PENDING-")
                if _pending_count >= 10:
                    # Find category from line
                    import re as _re_eb
                    _cat_m = _re_eb.search(r"\[CATEGORY:\s*([\w-]+)", _line)
                    if _cat_m:
                        _cat = _cat_m.group(1)
                        # Find another product in same category with <5 PENDING
                        _alt = None
                        for _line2 in PORTFOLIO_TEXT.split("\n"):
                            if not _line2.strip().startswith(tuple("0123456789")):
                                continue
                            if f"[CATEGORY: {_cat}" in _line2 and _t not in _line2:
                                _other_pending = _line2.count("PENDING-")
                                if _other_pending < 5:
                                    # Extract product name (between number and  —)
                                    _name_m = _re_eb.match(
                                        r"\d+\.\s+(\S+)", _line2)
                                    if _name_m:
                                        _alt = _name_m.group(1)
                                        break
                        if _alt:
                            log("bd",
                                f"  ⚖ balance: {_t} saturated ({_pending_count} PENDING) "
                                f"→ redirect to {_alt} (same cat {_cat})")
                            verdict["target_project"] = _alt
                            verdict["rationale"] = (
                                f"[balance] {_t}→{_alt} (saturated): "
                                + (verdict.get("rationale") or "")
                            )[:600]

    # ── Retired-name guard (2026-05-09) ──
    # User retired arkship + axiomops (consolidated to airship) and
    # costinel-lowercase (consolidated to Costinel). If LLM picked any of
    # these as target_project, redirect to canonical or drop to PASS.
    _RETIRED_TO_CANONICAL = {
        "arkship": "airship",
        "axiomops": "airship",
        "costinel": "Costinel",
        "sync-keeper": "airship",          # devops sync → airship
        "cost-radar": "Costinel",          # cost monitoring → Costinel
        "cloud-pilot": "airship",          # cloud orchestration → airship
        "quote-trail": "payment-shield",   # quote/billing → payment-shield
    }
    _t = (verdict.get("target_project") or "").strip()
    if _t in _RETIRED_TO_CANONICAL:
        _canon = _RETIRED_TO_CANONICAL[_t]
        verdict["target_project"] = _canon
        verdict["rationale"] = (
            f"[retired-guard] {_t}→{_canon} (consolidated 2026-05-09); "
            + (verdict.get("rationale") or "")
        )[:600]
        # If verdict was NEW-PRODUCT pointing at retired name, force EXTEND
        if verdict.get("verdict") == "NEW-PRODUCT":
            verdict["verdict"] = "EXTEND"
            verdict["feature_one_liner"] = (
                verdict.get("new_product_one_liner")
                or verdict.get("feature_one_liner") or ""
            )[:240]
            log("bd",
                f"  ⚠ retired→canonical: {_t}→{_canon}, NEW-PRODUCT→EXTEND")

    # ── Programmatic CATEGORY-LOCK enforcement (2026-05-09) ──
    # Even if LLM said PASS, scan the pain for category keywords. If we
    # find a strong match against an existing product's FUNCTIONS, flip
    # to EXTEND. This is a deterministic safety net — LLM rules are
    # advisory, this catches misses.
    if verdict.get("verdict") == "PASS":
        try:
            _pain_text = (
                (item.get("pain") or "") + " " +
                ((item.get("post") or {}).get("body") or "") + " " +
                ((item.get("post") or {}).get("title") or "")
            ).lower()[:2000]
            # Compact category→target map (keyword set → product slug)
            _category_keywords = {
                "Costinel": [
                    "aws cost", "cloud cost", "cost optimi", "cost attribution",
                    "billing surprise", "unused resource", "rightsizing",
                    "savings plan", "ec2 unused", "spot instance",
                    "cost explorer", "owner attribution", "showback", "chargeback",
                ],
                "vanguard": [
                    "cspm", "misconfig", "iam policy", "s3 public", "soc2",
                    "compliance audit", "drift detect security",
                    "policy-as-code", "security posture",
                ],
                "airship": [
                    "terraform", "iac drift", "multi-cloud deploy",
                    "deploy once", "env parity", "ci/cd pipeline",
                    "devsecops", "cdk module",
                ],
                "workio": [
                    "github webhook", "jira automat", "slack workflow",
                    "no-code automat", "zapier eng", "trigger pipeline",
                ],
                "surrogate": [
                    "ai dev agent", "autonomous coding", "agent swarm",
                    "self-coding bot",
                ],
                "compliance-scan": [
                    "hipaa gap", "soc2 gap", "compliance evidence",
                    "audit-ready",
                ],
                "drift-sentry": [
                    "terraform drift", "infra drift", "tf state diff",
                ],
                "code-craft": [
                    "ai code review", "pr suggestion", "lint refactor",
                ],
                "llm-orchestra": [
                    "llm router", "model fallback", "llm cost optim",
                ],
                "cron-scout-audit": [
                    "cron job missing", "cron silent fail", "scheduled task",
                ],
                "payment-shield": [
                    "stripe fraud", "card testing", "payment fraud",
                ],
                "invoice-pilot": [
                    "invoice ocr", "ap automation", "po matching",
                ],
                "topo-sync": [
                    "infra topology", "architecture diagram",
                    "terraform diagram",
                ],
                "trust-broker": [
                    "multi-tenant identity", "oauth proxy", "tenant onboard",
                ],
                "smart-contract-guard": [
                    "smart contract", "solidity audit", "web3 vulnerab",
                ],
                "hipaa-lint-pr": [
                    "hipaa pr", "phi leak", "healthcare lint",
                ],
                "signature-drift-watch": [
                    "api signature drift", "openapi diff",
                    "breaking change api",
                ],
                "llama-gate": [
                    "llm rate-limit", "prompt firewall", "pii redact prompt",
                ],
            }
            best_match = None
            best_kw = None
            for slug, kws in _category_keywords.items():
                for kw in kws:
                    if kw in _pain_text:
                        best_match = slug
                        best_kw = kw
                        break
                if best_match:
                    break
            if best_match:
                # Don't flip if mon_signal is also low (likely noise) AND
                # rationale already says "no extractable signal" or
                # similar
                _rat = (verdict.get("rationale") or "").lower()
                if not any(s in _rat for s in [
                    "anti-pattern", "bug report", "no extractable",
                    "truly empty", "spam"
                ]):
                    log("bd",
                        f"  ↻ cat-lock flip: PASS → EXTEND {best_match} "
                        f"(matched '{best_kw}')")
                    verdict["verdict"] = "EXTEND"
                    verdict["target_project"] = best_match
                    # 2026-05-10 cat-lock feat name from pain
                    # Don't use rationale (often "LLM returned non-JSON: ...")
                    # Use pain text directly so feature_one_liner is meaningful.
                    _pain_for_feat = (
                        item.get("pain") or
                        ((item.get("post") or {}).get("title") or "") or
                        f"add {best_kw} support to {best_match}"
                    )[:200]
                    verdict["feature_one_liner"] = _pain_for_feat
                    verdict["rationale"] = (
                        f"[cat-lock] keyword '{best_kw}' → EXTEND "
                        f"{best_match}; orig: "
                        + (verdict.get("rationale") or "")
                    )[:600]
                    # CATEGORY-LOCK forces revenue assumption: parent's
                    # buyer/pricing applies
                    if not verdict.get("monetization_signal"):
                        verdict["monetization_signal"] = "medium"
                    if not verdict.get("monetization_model"):
                        verdict["monetization_model"] = "subscription"
        except Exception as _e:
            log("bd", f"  ⚠ cat-lock check soft-fail: {type(_e).__name__}")

    # ── Overlap guard (added 2026-05-04 after cost-radar duplicated Costinel) ──
    # User feedback: 'มันซ้ำตลาดกับ costinel ไหม ... ควรจะเพิ่ม feature
    # ไม่ได้สร้าง product ใหม่'. LLM sometimes returns NEW-PRODUCT even when
    # the new_product_one_liner is semantically identical to an existing
    # product. Force-flip those to EXTEND so we don't fragment the portfolio.
    if (verdict.get("verdict") or "").upper() == "NEW-PRODUCT":
        npl = (verdict.get("new_product_one_liner") or "")[:300].lower()
        if npl:
            # 2026-05-06 v2: semantic dedup via FastEmbed (ONNX embeddings).
            # Word-jaccard missed "Stash Image Manager" vs "ImageGuard" since
            # word overlap is low but they're SAME idea.
            # FastEmbed all-MiniLM-L6-v2 (384-dim, ONNX, no GPU) catches these.
            # File: /tmp/bd_recent_npl_emb.json — list of {npl, emb, ts}
            import json as _json, time as _t
            _recent_npl_seen = "/tmp/bd_recent_npl_emb.json"
            try:
                with open(_recent_npl_seen) as _f:
                    _recent = _json.load(_f)
            except Exception:
                _recent = []
            _now_ts = _t.time()
            _recent = [r for r in _recent if _now_ts - r.get("ts", 0) < 7200]  # 2h
            # Lazy-load FastEmbed model (singleton pattern)
            global _fastembed_model
            try:
                _fastembed_model
            except NameError:
                try:
                    from fastembed import TextEmbedding
                    _fastembed_model = TextEmbedding("BAAI/bge-small-en-v1.5")
                except Exception as _e:
                    log("bd", f"  ⚠ FastEmbed init fail: {type(_e).__name__} — "
                              f"falling back to word-jaccard")
                    _fastembed_model = None
            _is_dup = False
            _new_emb = None
            if _fastembed_model is not None:
                try:
                    _new_emb = list(_fastembed_model.embed([npl[:300]]))[0].tolist()
                except Exception:
                    _new_emb = None
            if _new_emb and _recent:
                # Cosine sim against each recent
                import math as _math
                def _cos(a, b):
                    dot = sum(x*y for x, y in zip(a, b))
                    na = _math.sqrt(sum(x*x for x in a))
                    nb = _math.sqrt(sum(y*y for y in b))
                    return dot / (na * nb) if na and nb else 0.0
                for _r in _recent:
                    _r_emb = _r.get("emb")
                    if not _r_emb:
                        continue
                    _sim = _cos(_new_emb, _r_emb)
                    if _sim >= 0.85:  # semantic match threshold
                        _is_dup = True
                        log("bd",
                            f"  ⛔ semantic-dup NEW (sim={_sim:.2f} of "
                            f"'{_r.get('npl','')[:40]}') → PASS")
                        verdict["verdict"] = "PASS"
                        verdict["rationale"] = (
                            f"semantic duplicate of recent NEW (sim={_sim:.2f})"
                        )
                        break
            elif not _new_emb:
                # FastEmbed unavailable — fallback to word-jaccard
                import re as _re_dedup
                _new_words_set = set(_re_dedup.findall(r"[a-z]{4,}", npl))
                for _r in _recent:
                    _r_words = set(_re_dedup.findall(r"[a-z]{4,}", _r.get("npl", "")))
                    _u = _new_words_set | _r_words
                    if not _u:
                        continue
                    _sim = len(_new_words_set & _r_words) / len(_u)
                    if _sim >= 0.55:
                        _is_dup = True
                        log("bd", f"  ⛔ word-dup NEW sim={_sim:.0%} → PASS")
                        verdict["verdict"] = "PASS"
                        verdict["rationale"] = f"word-jaccard duplicate sim={_sim:.0%}"
                        break
            if not _is_dup:
                _recent.append({
                    "npl": npl[:200],
                    "emb": _new_emb,
                    "ts": _now_ts,
                })
                try:
                    with open(_recent_npl_seen, "w") as _f:
                        _json.dump(_recent[-100:], _f)  # keep last 100 with embs
                except Exception:
                    pass
            try:
                from axentx_shared import kv_get
                pf_v = kv_get("bd.portfolio") or {}
                pf = (pf_v.get("products") or {}) if isinstance(pf_v, dict) else {}
            except Exception:
                pf = {}
            import re as _re
            def _words(s): return set(_re.findall(r'[a-z]{4,}', s.lower()))
            new_words = _words(npl)
            best_slug, best_sim = None, 0.0
            for slug, desc in pf.items():
                if not desc or len(desc) < 20: continue
                pw = _words(desc)
                union = new_words | pw
                if not union: continue
                sim = len(new_words & pw) / len(union)
                if sim > best_sim:
                    best_slug, best_sim = slug, sim
            if best_sim >= 0.30 and best_slug:
                # Strong overlap → force EXTEND
                log("bd",
                    f"  ↻ overlap-guard: NEW-PRODUCT '{npl[:40]}' "
                    f"matches '{best_slug}' (sim={best_sim:.0%}) → EXTEND")
                verdict["verdict"] = "EXTEND"
                verdict["target_project"] = best_slug
                verdict["feature_one_liner"] = npl[:240]
                verdict["new_product_one_liner"] = ""
                verdict["overlap_guard"] = {
                    "matched": best_slug, "similarity": round(best_sim, 2),
                    "rule": "jaccard-words >= 0.30",
                }

    item["bd_verdict"] = verdict
    item["history"].append({
        "stage": "bd",
        "actor": "axentx-bd",
        "output": json.dumps(verdict, ensure_ascii=False),
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })

    # Audit: log every verdict to shared_memory so we can see WHY bd
    # decides what it decides. User asked: 'bd output mix shows 99% PASS,
    # 0 EXTEND — why?'. Now every verdict is observable.
    try:
        from axentx_shared import memory_log
        memory_log("bd", f"verdict-{(verdict.get('verdict') or '?').lower()}",
                   f"{(verdict.get('verdict') or '?')} — "
                   f"target={verdict.get('target_project') or 'null'} "
                   f"mon={verdict.get('monetization_signal') or '?'}",
                   body=(f"item: {item['id'][:50]}\n"
                         f"pain: {pain.get('pain_one_liner','')[:200]}\n"
                         f"rationale: {(verdict.get('rationale') or '')[:300]}\n"
                         f"feature_one_liner: "
                         f"{(verdict.get('feature_one_liner') or '')[:200]}\n"
                         f"new_product_one_liner: "
                         f"{(verdict.get('new_product_one_liner') or '')[:200]}"),
                   tags=["bd", "verdict",
                         (verdict.get("verdict") or "?").lower()])
    except Exception:
        pass
    item["current"]["text"] = json.dumps(verdict, ensure_ascii=False)

    v = (verdict.get("verdict") or "").upper()
    mon_sig = (verdict.get("monetization_signal") or "low").lower()

    # ── Output-mode routing (added 2026-05-04) ──────────────────────────
    # User directive: 'แต่ไม่ใช่ opensource จะไม่ทำนะ ก็ทำ ทำ paralle
    # กันไป 10% opensource, 30% ขยาย 5 main paid products, 60% new paid'
    #
    # Decision rules (when verdict is EXTEND or NEW-PRODUCT):
    #   - target_project ∈ {Costinel,vanguard,arkship,surrogate,workio} → "extend-main"
    #   - mon_sig ∈ (medium,high) → "paid-product" (private repo)
    #   - mon_sig == "low" → "open-source" (public repo, no $$ needed)
    # Quota throttling done via shared_kv counter (one cycle = one item;
    # quota check ensures roughly 10/30/60 split over many cycles).
    main_5 = {"Costinel", "vanguard", "arkship", "surrogate", "workio"}
    target = verdict.get("target_project") or ""

    if v == "EXTEND" and target in main_5:
        output_mode = "extend-main"
    elif v == "NEW-PRODUCT" and mon_sig in ("medium", "high"):
        output_mode = "paid-product"
    elif v == "NEW-PRODUCT" and mon_sig == "low":
        output_mode = "open-source"
    elif v == "EXTEND":
        output_mode = "extend-other"
    else:
        output_mode = "pass"

    # Quota check via shared_kv counter (best-effort — falls back to
    # always-allow if shared lib unavailable).
    try:
        sys.path.insert(0, str(REPO_ROOT / "bin"))
        from axentx_shared import kv_get, kv_set  # noqa: E402
        counts = kv_get("bd.output_counts") or {
            "extend-main": 0, "paid-product": 0,
            "open-source": 0, "extend-other": 0, "pass": 0,
        }
        total = sum(counts.values()) or 1
        # Target: 30% extend-main, 60% paid-product, 10% open-source
        if output_mode == "open-source":
            current_oss_frac = counts.get("open-source", 0) / total
            if current_oss_frac > 0.12:   # already over 10% target → kill
                output_mode = "pass"
        counts[output_mode] = counts.get(output_mode, 0) + 1
        kv_set("bd.output_counts", counts)
    except Exception:
        pass   # quota check is best-effort

    item["output_mode"] = output_mode
    verdict["output_mode"] = output_mode

    log("bd", f"  mode={output_mode} target={target or 'null'} "
              f"mon_sig={mon_sig}")

    if v == "PASS":
        # End of road — record decision but stop here
        advance(item, src_path, "done", "bd",
                f"BD-PASS: {verdict.get('rationale','')[:200]}")
        log("bd", f"  ↓ PASS — {verdict.get('rationale','')[:60]}")
    elif v == "EXTEND":
        # 2026-05-09 EXTEND v2 — through lean-canvas+pitch
        # OLD (pre-2026-05-09): bd EXTEND → design (skipped canvas + pitch).
        # Result: features dropped into target without BMC delta or
        # version bump. bd.portfolio FUNCTIONS list never grew, so future
        # bd kept seeing same capability surface.
        # NEW: bd EXTEND → lean-canvas (extend mode) → pitch (extend mode,
        # light rubric) → design. Builds feature-delta BMC, bumps version
        # on target, validates incremental value before dev cycles.
        item["target_project"] = verdict.get("target_project")
        item["extend_mode"] = True   # signal to lean-canvas + pitch
        advance(item, src_path, "lean-canvas", "bd", json.dumps(verdict))
        log("bd",
            f"  ✓ EXTEND[v2] → lean-canvas[extend] → pitch[extend] → "
            f"design: target={verdict.get('target_project','?')}, "
            f"feat={(verdict.get('feature_one_liner') or '')[:40]}")
    elif v == "NEW-PRODUCT":
        # Brand-new product hypothesis. Detour through spawn-queue so the
        # product-spawner-daemon creates the GitHub repo + local clone
        # FIRST. After spawn, business-synthesis attaches the full pack
        # (BMC + marketing + tech spec + customer journey + dataflow +
        # user stories + breakeven + partner targets) into the new repo
        # before code starts being written.
        item["target_project"] = None  # spawner fills this in
        # 2026-05-06: detour through lean-canvas FIRST so pitch panel
        # gets BMC + unit economics + TAM/SAM/SOM before evaluating.
        # Was bd → spawn (pitch ran blind on one-liner only).
        advance(item, src_path, "lean-canvas", "bd", json.dumps(verdict))
        log("bd",
            f"  ✓ NEW-PRODUCT → lean-canvas → pitch → spawn: "
            f"{verdict.get('new_product_one_liner','?')[:60]}")
    elif v == "BIZ-OPPORTUNITY":
        # 2026-05-06 TRACK B: non-IT business opportunity. Routes to
        # biz-research-queue for separate non-IT pipeline (biz-bd → biz-pitch
        # → biz-plan-writer → biz-launcher).
        # Output: business plan + financial model + supply chain (NOT code).
        advance(item, src_path, "biz-research", "bd", json.dumps(verdict))
        log("bd",
            f"  💼 BIZ-OPPORTUNITY → biz-research-queue (TRACK B): "
            f"{verdict.get('new_product_one_liner','?')[:60]}")
    else:
        # Ambiguous — let design have a look
        advance(item, src_path, "design", "bd", out)
        log("bd", f"  ~ ambiguous → design")
    return True


if __name__ == "__main__":
    daemon_loop("bd", POLL_SEC, do_one_bd)

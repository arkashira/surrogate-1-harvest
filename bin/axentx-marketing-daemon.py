#!/usr/bin/env python3
"""axentx marketing daemon — positioning + competitor scan + go-to-market.

Pulls items from marketing-queue (where business verdict was BUILD).
Drafts:
  1. positioning statement (Geoffrey Moore template)
  2. competitor map with our differentiators
  3. launch GTM (channel + ICP + first-7-days plan)
  4. messaging pillars + 1 sample landing-page hero copy

Output advances to prd-queue where the PRD daemon turns this into actual
user stories + tasks the dev pipeline can implement.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, pick_oldest, advance,
                             fail, daemon_loop, get_role_budget)

POLL_SEC = int(os.environ.get("MARKETING_POLL_SEC", "90"))
MARKETING_BUDGET = get_role_budget("marketing", 1800)
MARKETING_FACTCHECK_BUDGET = get_role_budget("marketing_factcheck", 600)


MARKETING_SYSTEM = """You are head of product marketing. For each validated
BUILD opportunity (BMC + market sizing already done), output strict JSON:

{
  "positioning": "For <target customer> who <problem>, <product> is a <category> that <key benefit>. Unlike <main alternative>, <our differentiator>.",
  "icp": {
    "company_size": "solo|smb|midmarket|enterprise",
    "role": "<exact title that holds budget>",
    "trigger": "<the moment they realize they need us>"
  },
  "competitors": [
    {"name": "<vendor>", "positioning": "<how they sell>", "weakness_we_exploit": "<gap we close>"}
  ],
  "messaging_pillars": ["<3 short messages we'd hammer everywhere>"],
  "landing_hero": "<one headline + one subheadline that would convert the ICP>",
  "channels": ["<2-3 channels ranked by likely fit>"],
  "first_7_days_plan": ["<concrete launch tasks day-by-day>"],
  "launch_metric": "<the single metric we measure launch success on day 7 / day 30>"
}

Be ruthlessly specific about ICP — 'developers' is not enough; 'platform
engineers at 50-200 person SaaS companies who use Terraform' is. Channels
must be *cheap* (we're cloud-free-tier — no paid ads day 1). Lean toward:
GitHub README/ Show HN / Product Hunt / dev.to / Indie Hackers / specific
subreddits / cold DM to specific roles."""


def do_one_marketing() -> bool:
    picked = pick_oldest("marketing")
    if not picked: return False
    src_path, item = picked

    pain = item.get("verdict", {}) or {}
    bd = item.get("bd_verdict", {}) or {}
    design = item.get("design_verdict", {}) or {}
    biz = item.get("business_verdict", {}) or {}

    log("marketing", f"▸ {item['id'][:30]}  → {bd.get('target_project','new')}")
    prompt = (
        f"=== context ===\n"
        f"Pain: {pain.get('pain_one_liner','?')}\n"
        f"BD: {bd.get('verdict','?')} → {bd.get('target_project','?')}\n"
        f"Hypothesis: {bd.get('feature_one_liner') or bd.get('new_product_one_liner','')}\n"
        f"Root cause: {design.get('root_cause','?')}\n"
        f"JTBD: {design.get('jtbd','?')}\n"
        f"BMC value props: {(biz.get('bmc') or {}).get('value_propositions','?')}\n"
        f"Pricing: {biz.get('pricing','?')}\n"
        f"NSM: {biz.get('north_star_metric','?')}\n\n"
        f"Draft positioning + ICP + competitor map + GTM + messaging. Strict JSON."
    )
    try:
        out = call_llm(prompt, system=MARKETING_SYSTEM, max_tokens=MARKETING_BUDGET, timeout=60)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        gtm = json.loads(txt.strip())
    except Exception as e:
        fail(item, src_path, "marketing", f"LLM/parse failed: {e}")
        log("marketing", f"✗ {item['id']}: parse fail")
        return True

    # Fact-check pass — flag claims that are made up / unverifiable so a
    # downstream human (or the PRD daemon) doesn't repeat them. Cheap call;
    # short prompt routes through Workers AI fast path.
    fact_check_prompt = (
        "Audit the following marketing JSON for unverifiable or fabricated "
        "claims (made-up competitor names, invented stats, unsourced market "
        "sizes, exaggerated 'first/only' claims, etc.). Output strict JSON: "
        '{"unverifiable": [{"field": "<json path>", "claim": "<text>", '
        '"why": "<reason>"}]}. Empty list if all claims look defensible.\n\n'
        f"{json.dumps(gtm, ensure_ascii=False)[:3500]}"
    )
    findings = {"unverifiable": [], "checked_at": datetime.datetime.utcnow().isoformat() + "Z"}
    try:
        fc_out = call_llm(fact_check_prompt, max_tokens=MARKETING_FACTCHECK_BUDGET, timeout=30)
        fc_txt = fc_out.strip()
        if "```" in fc_txt:
            fc_txt = fc_txt.split("```")[1]
            if fc_txt.startswith("json"): fc_txt = fc_txt[4:]
        parsed = json.loads(fc_txt.strip())
        if isinstance(parsed.get("unverifiable"), list):
            findings["unverifiable"] = parsed["unverifiable"][:10]
    except Exception as e:
        findings["error"] = f"fact_check failed: {str(e)[:120]}"

    item["marketing_verdict"] = gtm
    item["fact_check_findings"] = findings
    item["history"].append({
        "stage": "marketing",
        "actor": "axentx-marketing",
        "output": json.dumps(gtm, ensure_ascii=False),
        "at": datetime.datetime.utcnow().isoformat() + "Z",
        "fact_check": findings,
    })
    item["current"]["text"] = json.dumps(gtm, ensure_ascii=False)

    advance(item, src_path, "prd", "marketing",
            json.dumps(gtm, ensure_ascii=False))
    n_flags = len(findings["unverifiable"])
    log("marketing", f"  ✓ → prd-queue (positioning: "
                    f"{gtm.get('positioning','')[:50]}; fact-flags={n_flags})")
    return True


if __name__ == "__main__":
    daemon_loop("marketing", POLL_SEC, do_one_marketing)

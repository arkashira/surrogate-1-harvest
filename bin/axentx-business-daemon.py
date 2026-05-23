#!/usr/bin/env python3
"""axentx business daemon — Business Model Canvas + market sizing + pricing.

Pulls items from business-queue (where design-thinking marked PROCEED).
For each validated opportunity:

  1. Build a 9-block Business Model Canvas.
  2. Estimate TAM/SAM/SOM using public data + Fermi estimation.
  3. Sketch 3 pricing tiers (free/team/enterprise) with rationale.
  4. Final go/no-go on the unit economics.

Output proceeds to marketing-queue if BUILD; lands in done with note if
NO-GO. The marketing daemon then drafts positioning + competitor map
before PRD finalization.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, pick_oldest, advance,
                             fail, daemon_loop)

POLL_SEC = int(os.environ.get("BUSINESS_POLL_SEC", "90"))


BUSINESS_SYSTEM = """You are a startup business analyst. For each validated
product/feature idea, output a one-page Business Model Canvas + market sizing
+ pricing in strict JSON:

{
  "bmc": {
    "customer_segments": ["<3 specific personas>"],
    "value_propositions": ["<3-5 outcomes the customer pays for>"],
    "channels": ["<2-3 channels we'll reach them>"],
    "customer_relationships": "<self-serve|community|sales-led>",
    "revenue_streams": ["<2-3 monetization paths>"],
    "key_resources": ["<3 critical resources to deliver>"],
    "key_activities": ["<3 main activities the team does>"],
    "key_partners": ["<2-3 external dependencies>"],
    "cost_structure": ["<3 main cost lines>"]
  },
  "market": {
    "tam_usd": "<rough TAM in USD with 1-line Fermi reasoning>",
    "sam_usd": "<SAM>",
    "som_yr3_usd": "<realistic SOM by year 3>",
    "competing_products": ["<3 names with 1-phrase positioning>"],
    "white_space": "<what's underserved that we'd own>"
  },
  "pricing": {
    "free":   {"limits": "<usage cap>", "audience": "individual devs / try-out"},
    "team":   {"price_usd_mo": <number>, "limits": "<cap>", "audience": "small teams"},
    "enterprise": {"price_usd_mo": "<contact|number>", "audience": "100+ seat orgs"}
  },
  "verdict": "BUILD|HOLD|NO-GO",
  "rationale": "<1-3 sentences why>",
  "risks": ["<top 3 risks ranked>"],
  "north_star_metric": "<the single number we'd optimize>"
}

Rules:
- BUILD if SOM_yr3 ≥ $1M AND axentx has a clear advantage AND fit with the
  cloud-free-tier ethos (we serve indie devs / SMBs first).
- HOLD if interesting but capacity-constrained — park for later.
- NO-GO if TAM is too small, market is locked up, or we'd need >$10M to enter.
- Be honest. Don't inflate TAM with 'AI market = $1T'.\nGROUNDING: Cite at least one concrete source for every claim (URL from the post, dataset/repo name, established framework name, published number). If you cannot cite, say "unverified — needs research" instead of fabricating a number, market size, competitor name, or feature claim. Made-up references are worse than honest gaps.
"""


def do_one_business() -> bool:
    picked = pick_oldest("business")
    if not picked: return False
    src_path, item = picked

    pain = item.get("verdict", {}) or {}
    bd = item.get("bd_verdict", {}) or {}
    design = item.get("design_verdict", {}) or {}

    log("business", f"▸ {item['id'][:30]}  jtbd: {design.get('jtbd','')[:50]}")
    prompt = (
        f"=== validated opportunity ===\n"
        f"Pain: {pain.get('pain_one_liner','?')}\n"
        f"Audience: {pain.get('audience','?')}\n"
        f"BD verdict: {bd.get('verdict','?')} → {bd.get('target_project','?')}\n"
        f"Hypothesis: {bd.get('feature_one_liner') or bd.get('new_product_one_liner','')}\n"
        f"Root cause: {design.get('root_cause','?')}\n"
        f"JTBD: {design.get('jtbd','?')}\n"
        f"Audience specificity: {design.get('audience_specificity','?')}/10\n\n"
        f"Build BMC + market sizing + pricing + verdict. Strict JSON only."
    )
    try:
        out = call_llm(prompt, system=BUSINESS_SYSTEM, max_tokens=2000, timeout=60)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        bmc = json.loads(txt.strip())
    except Exception as e:
        fail(item, src_path, "business", f"LLM/parse failed: {e}")
        log("business", f"✗ {item['id']}: parse fail")
        return True

    item["business_verdict"] = bmc
    item["history"].append({
        "stage": "business",
        "actor": "axentx-business",
        "output": json.dumps(bmc, ensure_ascii=False),
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    item["current"]["text"] = json.dumps(bmc, ensure_ascii=False)

    v = (bmc.get("verdict") or "").upper()
    if v == "BUILD":
        advance(item, src_path, "marketing", "business",
                json.dumps(bmc, ensure_ascii=False))
        log("business", f"  ✓ BUILD → marketing-queue (NSM: {bmc.get('north_star_metric','?')[:40]})")
    elif v == "HOLD":
        # Park for later — write to done with HOLD tag for periodic re-eval
        advance(item, src_path, "done", "business",
                f"BUSINESS-HOLD: {bmc.get('rationale','')[:200]}")
        log("business", f"  ⏸ HOLD — {bmc.get('rationale','')[:60]}")
    else:
        advance(item, src_path, "done", "business",
                f"BUSINESS-NOGO: {bmc.get('rationale','')[:200]}")
        log("business", f"  ↓ NO-GO — {bmc.get('rationale','')[:60]}")
    return True


if __name__ == "__main__":
    daemon_loop("business", POLL_SEC, do_one_business)

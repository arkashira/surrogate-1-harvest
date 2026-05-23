#!/usr/bin/env python3
"""axentx design-thinking daemon — validates ideas with two frameworks.

Pulls items from design-queue (where BD routed real, on-strategy pains).
Runs two validation passes back-to-back:

  1. 5-Whys — drill the pain to the root cause. If we only address the
     surface, we'd ship the wrong thing.
  2. Jobs-to-be-Done (JTBD) — the *job* the user is hiring this product to
     do, in the form: "When <situation>, I want <motivation>, so I can
     <expected outcome>." Forces user-centric framing.

Output is a validation report. If both passes hold up, item proceeds to
business-queue (BMC + market sizing). If the root cause is unrelated to
the original pain (failed 5-whys), or the JTBD doesn't match axentx
positioning, item is rejected and ends in done with a 'design-rejected'
note that BD can review.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, pick_oldest, advance,
                             fail, daemon_loop, get_portfolio_block,
                              get_portfolio,)

POLL_SEC = int(os.environ.get("DESIGN_POLL_SEC", "60"))


DESIGN_SYSTEM = """You are a senior product designer running design-thinking
validation on a candidate product/feature idea. Apply two frameworks to
every idea:

A) 5-WHYS — keep asking 'why?' until you hit a root cause that's NOT just
   the immediate symptom. The root cause should be a true human/business
   driver, not a technical artifact.

B) JOBS-TO-BE-DONE — phrase as: 'When <situation>, I want <motivation>, so
   I can <expected outcome>.' This must reflect the USER'S goal, not the
   product's feature.

Then DECIDE: should we proceed?
  PROCEED   — root cause is real, JTBD is clean, fix is in axentx's lane
  REJECT    — root cause is shallow, JTBD is muddled, or pain is downstream
              of a different problem we won't solve well

Output STRICT JSON:

{
  "five_whys": ["why 1...", "why 2...", "why 3...", "why 4...", "why 5..."],
  "root_cause": "<the underlying driver in 1 sentence>",
  "jtbd": "When <situation>, I want <motivation>, so I can <outcome>.",
  "audience_specificity": "1-10 (10 = exactly one persona, 1 = anyone with hands)",
  "fit_with_target": "<does the BD-routed target product really fit, or is it forced? 1 sentence>",
  "decision": "PROCEED|REJECT",
  "rationale": "<1-2 sentences for the decision>"
}

Be tough — kill bad ideas at this stage. Most pains are real but vague;
they need sharpening before they become a product hypothesis.\nGROUNDING: Cite at least one concrete source for every claim (URL from the post, dataset/repo name, established framework name, published number). If you cannot cite, say "unverified — needs research" instead of fabricating a number, market size, competitor name, or feature claim. Made-up references are worse than honest gaps.
"""


def do_one_design() -> bool:
    picked = pick_oldest("design")
    if not picked: return False
    src_path, item = picked

    pain = item.get("verdict", {}) or {}
    bd = item.get("bd_verdict", {}) or {}
    post = item.get("post", {}) or {}

    log("design", f"▸ {item['id'][:30]}  ({bd.get('verdict','?')}/{bd.get('target_project','?')})")
    prompt = (
        f"=== Original pain ===\n"
        f"One-liner: {pain.get('pain_one_liner','?')}\n"
        f"Severity: {pain.get('severity','?')}/10\n"
        f"Audience: {pain.get('audience','?')}\n"
        f"Evidence: {pain.get('evidence','')[:400]}\n\n"
        f"=== BD verdict ===\n"
        f"Decision: {bd.get('verdict','?')} → target={bd.get('target_project','?')}\n"
        f"BD rationale: {bd.get('rationale','')}\n"
        f"Hypothesis: {bd.get('feature_one_liner') or bd.get('new_product_one_liner','')}\n\n"
        f"Source post: {post.get('source','?')} | {post.get('url','?')}\n\n"
        f"Run 5-whys + JTBD validation. Output strict JSON only."
    )
    try:
        out = call_llm(prompt,
                       system=DESIGN_SYSTEM + "\n\n" + get_portfolio_block(),
                       max_tokens=900, timeout=45)
        txt = out.strip()
        if "```" in txt:
            txt = txt.split("```")[1]
            if txt.startswith("json"): txt = txt[4:]
        verdict = json.loads(txt.strip())
    except Exception as e:
        fail(item, src_path, "design", f"LLM/parse failed: {e}")
        log("design", f"✗ {item['id']}: parse fail")
        return True

    item["design_verdict"] = verdict
    item["history"].append({
        "stage": "design",
        "actor": "axentx-design-thinking",
        "output": json.dumps(verdict, ensure_ascii=False),
        "at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    item["current"]["text"] = json.dumps(verdict, ensure_ascii=False)

    decision = (verdict.get("decision") or "").upper()
    if decision == "REJECT":
        advance(item, src_path, "done", "design",
                f"DESIGN-REJECT: {verdict.get('rationale','')[:250]}")
        log("design", f"  ↓ REJECT — {verdict.get('rationale','')[:60]}")
    else:
        # PROCEED → business sizing
        advance(item, src_path, "business", "design",
                json.dumps(verdict, ensure_ascii=False))
        log("design", f"  ✓ PROCEED → business-queue (jtbd: {verdict.get('jtbd','')[:60]})")
    return True


if __name__ == "__main__":
    daemon_loop("design", POLL_SEC, do_one_design)

#!/usr/bin/env python3
"""axentx ux — UX design pass for PRD items before dev split.

Reads marketing-queue items (after BMC + GTM done). Drafts:
- key user flows (3-5)
- wireframe text descriptions (1 per flow)
- error states + edge cases
Then advances to prd-queue with ux annotations attached so the PRD daemon
can derive UI tasks too (not just backend)."""
from __future__ import annotations
import datetime, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, pick_oldest, advance,
                             fail, daemon_loop)
POLL_SEC = int(os.environ.get("UX_POLL_SEC", "120"))

UX_SYSTEM = """You are a senior UX designer. For each validated product/feature,
output strict JSON:

{
  "user_flows": [{"name":"...","steps":["1. ...","2. ..."],"happy_path":true}],
  "wireframes": [{"screen":"...","layout":"<text describing layout, key elements, hierarchy>"}],
  "error_states": ["<error scenario + what user sees>"],
  "edge_cases": ["<edge case + handling>"],
  "accessibility_notes": ["<a11y considerations>"]
}

Be concrete. 'Login screen with email/password and forgot link' not 'an
auth flow'. Lean toward CLI / API-first if backend, only design web UI if
the audience needs it."""

def do_one() -> bool:
    picked = pick_oldest("marketing")
    if not picked: return False
    src_path, item = picked
    bd = item.get("bd_verdict", {}) or {}
    biz = item.get("business_verdict", {}) or {}
    gtm = item.get("marketing_verdict", {}) or {}
    log("ux", f"▸ {item['id'][:32]}")
    prompt = (
        f"Hypothesis: {bd.get('feature_one_liner') or bd.get('new_product_one_liner','')}\n"
        f"ICP: {gtm.get('icp','?')}\n"
        f"Value props: {(biz.get('bmc') or {}).get('value_propositions','?')}\n"
        f"Positioning: {gtm.get('positioning','?')}\n\n"
        f"Draft UX (flows + wireframes + errors + edges + a11y). Strict JSON."
    )
    try:
        out = call_llm(prompt, system=UX_SYSTEM, max_tokens=1800, timeout=60)
        txt = out.strip()
        if "```" in txt: txt = txt.split("```")[1]
        if txt.startswith("json"): txt = txt[4:]
        ux = json.loads(txt.strip())
    except Exception as e:
        # don't block PRD — pass through with empty ux
        log("ux", f"⚠ parse fail, advancing without ux: {e}")
        advance(item, src_path, "prd", "ux", "[ux-pass-failed]")
        return True
    item["ux_verdict"] = ux
    item["history"].append({"stage":"ux","actor":"axentx-ux",
                            "output":json.dumps(ux,ensure_ascii=False),
                            "at":datetime.datetime.utcnow().isoformat()+"Z"})
    advance(item, src_path, "prd", "ux", json.dumps(ux,ensure_ascii=False))
    log("ux", f"  ✓ {len(ux.get('user_flows',[]))} flows + "
              f"{len(ux.get('wireframes',[]))} wireframes → prd")
    return True

if __name__ == "__main__":
    daemon_loop("ux", POLL_SEC, do_one)

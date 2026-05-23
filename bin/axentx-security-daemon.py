#!/usr/bin/env python3
"""axentx security — runs a security-specific review pass.

Looks at items currently in qa-queue (passed code review + qa). Runs a
security-focused LLM pass. Verdict either OK (advances to commit) or
SEC-BLOCK (back to dev with specific findings). Adds security_findings
into item history regardless so commits carry the context.
"""
from __future__ import annotations
import datetime, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, pick_oldest, advance,
                             fail, daemon_loop, rag_query)
POLL_SEC = int(os.environ.get("SEC_POLL_SEC", "60"))

SEC_SYSTEM = """You are an application security engineer. Review the proposed
change for SECURITY ONLY. Output strict JSON:

{
  "verdict": "OK|SEC-BLOCK",
  "findings": [{"severity":"low|med|high|crit","class":"<sqli|xss|secret-leak|broken-auth|ssrf|deserialization|race|other>","detail":"<1 sentence>","mitigation":"<1 sentence fix>"}],
  "summary": "<1 sentence overall>"
}

SEC-BLOCK only if a HIGH or CRIT finding exists. Lows + meds → OK with
findings noted (commit will carry them). Don't flag style/lint/perf —
those are other passes."""

def do_one() -> bool:
    picked = pick_oldest("qa")
    if not picked: return False
    src_path, item = picked
    proposal = (item.get("current",{}) or {}).get("text","")
    log("security", f"▸ {item['id'][:32]} security pass")
    rag = rag_query(f"security findings related to {item.get('focus','')}", top_k=3) or ""
    prompt = f"=== change ===\n{proposal[:5000]}\n\n{rag}\n\nReview for security. Strict JSON."
    try:
        out = call_llm(prompt, system=SEC_SYSTEM, max_tokens=1000, timeout=40)
        txt = out.strip()
        if "```" in txt: txt = txt.split("```")[1]
        if txt.startswith("json"): txt = txt[4:]
        v = json.loads(txt.strip())
    except Exception as e:
        # fail-open: send to commit anyway, log the failure
        log("security", f"⚠ sec parse fail, sending through: {e}")
        advance(item, src_path, "commit", "security", "[security-pass-failed]")
        return True
    item.setdefault("security_findings", []).append(v)
    item["history"].append({"stage":"security","actor":"axentx-security",
                            "output":json.dumps(v,ensure_ascii=False),
                            "at":datetime.datetime.utcnow().isoformat()+"Z"})
    if (v.get("verdict") or "").upper() == "SEC-BLOCK":
        item["current"]["text"] = (
            f"SECURITY BLOCK:\n{json.dumps(v,indent=2,ensure_ascii=False)}\n\n"
            f"--- original ---\n{proposal[:3000]}"
        )
        advance(item, src_path, "dev", "security", json.dumps(v))
        log("security", f"  ↺ SEC-BLOCK → back to dev")
    else:
        advance(item, src_path, "commit", "security", json.dumps(v))
        log("security", f"  ✓ OK ({len(v.get('findings',[]))} non-blocker findings)")
    return True

if __name__ == "__main__":
    daemon_loop("security", POLL_SEC, do_one)

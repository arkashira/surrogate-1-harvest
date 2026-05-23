#!/usr/bin/env python3
"""axentx perf — performance/scalability review pass."""
from __future__ import annotations
import datetime, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (REPO_ROOT, log, call_llm, pick_oldest, advance,
                             fail, daemon_loop, rag_query)
POLL_SEC = int(os.environ.get("PERF_POLL_SEC", "60"))

PERF_SYSTEM = """You are a performance engineer. Review the proposed change
for PERFORMANCE ONLY. Strict JSON:

{
  "verdict": "OK|PERF-BLOCK",
  "findings": [{"severity":"low|med|high","class":"<n+1|unbounded-query|missing-index|sync-in-async|memory-leak|other>","detail":"...","mitigation":"..."}],
  "summary":"..."
}

PERF-BLOCK only on HIGH (would cause prod outage at any reasonable scale).
Med = note in commit, OK proceed. Low = acceptable."""

def do_one() -> bool:
    picked = pick_oldest("qa")  # post-QA, parallel to security
    if not picked: return False
    src_path, item = picked
    proposal = (item.get("current",{}) or {}).get("text","")
    log("perf", f"▸ {item['id'][:32]} perf pass")
    prompt = f"=== change ===\n{proposal[:5000]}\n\nReview for perf only. Strict JSON."
    try:
        out = call_llm(prompt, system=PERF_SYSTEM, max_tokens=900, timeout=40)
        txt = out.strip()
        if "```" in txt: txt = txt.split("```")[1]
        if txt.startswith("json"): txt = txt[4:]
        v = json.loads(txt.strip())
    except Exception as e:
        log("perf", f"⚠ parse fail, sending through: {e}")
        advance(item, src_path, "commit", "perf", "[perf-pass-failed]")
        return True
    item.setdefault("perf_findings", []).append(v)
    item["history"].append({"stage":"perf","actor":"axentx-perf",
                            "output":json.dumps(v,ensure_ascii=False),
                            "at":datetime.datetime.utcnow().isoformat()+"Z"})
    if (v.get("verdict") or "").upper() == "PERF-BLOCK":
        item["current"]["text"] = (
            f"PERF BLOCK:\n{json.dumps(v,indent=2,ensure_ascii=False)}\n\n"
            f"--- original ---\n{proposal[:3000]}"
        )
        advance(item, src_path, "dev", "perf", json.dumps(v))
        log("perf", f"  ↺ PERF-BLOCK → dev")
    else:
        advance(item, src_path, "commit", "perf", json.dumps(v))
        log("perf", f"  ✓ OK ({len(v.get('findings',[]))} notes)")
    return True

if __name__ == "__main__":
    daemon_loop("perf", POLL_SEC, do_one)

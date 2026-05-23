#!/usr/bin/env python3
"""axentx QA daemon — picks reviewed items, generates test cases (TDD),
records test plan, advances to commit-queue.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (log, call_llm, pick_oldest, advance, fail,
                             daemon_loop, get_portfolio_block,
                              get_portfolio,)

POLL_SEC = 30

QA_SYSTEM = """You are a QA engineer. For each approved change, write a \
TDD-style test plan. Output sections:
1. **Acceptance criteria** (3-7 bullets, each measurable)
2. **Unit tests** (pseudo-code, Jest/Pytest/etc style)
3. **Integration tests** (3-5 happy + 2-3 edge cases)
4. **Risk register** (what could go wrong, how to detect)
First line must be PASS: or BLOCK: <reason>."""


def do_one_qa() -> bool:
    picked = pick_oldest("qa")
    if not picked: return False
    src_path, item = picked
    review_output = item.get("current", {}).get("text", "")
    # The original proposal is in history[0]
    original = item["history"][0]["output"] if item.get("history") else ""
    project = item.get("project", "?")
    log("qa", f"▸ {item['id']} ({project})")
    prompt = (f"Project: {project}\n\n"
              f"Approved proposal:\n{original[:3000]}\n\n"
              f"Reviewer notes:\n{review_output[:1500]}\n\n"
              f"Write the TDD test plan.")
    try:
        out = call_llm(prompt, system=QA_SYSTEM, max_tokens=1200, timeout=40)
    except Exception as e:
        fail(item, src_path, "qa", f"LLM failed: {e}")
        log("qa", f"✗ {item['id']}: LLM failed")
        return True
    first = out.splitlines()[0].upper() if out.splitlines() else ""
    if first.startswith("BLOCK"):
        # Send back to dev
        advance(item, src_path, "dev", "qa", out)
        log("qa", f"↺ {item['id']} BLOCK → back to dev")
    else:
        advance(item, src_path, "commit", "qa", out)
        log("qa", f"✓ {item['id']} PASS → commit-queue")
    return True


if __name__ == "__main__":
    daemon_loop("qa", POLL_SEC, do_one_qa)

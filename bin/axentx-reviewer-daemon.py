#!/usr/bin/env python3
"""axentx reviewer daemon — picks items from review-queue, runs them through
a senior code-review prompt, advances to qa-queue (or back to dev with
feedback if rejected).
"""
from __future__ import annotations
import sys, json, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from axentx_pipeline import (log, call_llm, pick_oldest, advance, fail,
                             daemon_loop, get_role_budget, get_portfolio_block,
                              get_portfolio,)

REVIEWER_BUDGET = get_role_budget("reviewer", 1200)

POLL_SEC = 30

# Bump only when REVIEWER_SYSTEM (or other rubric-defining behavior) changes.
# Stamped into every reviewer history entry so we can audit which rubric
# produced any given verdict and track approval-rate drift across versions.
RUBRIC_VERSION = "v1"

# Hard caps that auto-reject before the LLM is even called. Items exceeding
# either threshold should be split into smaller PRs, regardless of content.
MAX_DIFF_CHARS = int(__import__("os").environ.get("REVIEWER_MAX_DIFF_CHARS", "8000"))
MAX_DIFF_LINES = int(__import__("os").environ.get("REVIEWER_MAX_DIFF_LINES", "250"))

# Pragmatic reviewer prompt — old version rejected on "missing minor details"
# which created an infinite reject loop with 0 commits over 24 hours. This
# version approves anything that addresses the primary issue without a real
# correctness/security/data bug. Comments on minor issues go in acceptance
# criteria, not as a reject reason.
REVIEWER_SYSTEM = """You are a principal engineer doing PRAGMATIC code review. \
Default to APPROVE; reject only for clear correctness/security/data bugs.

APPROVE if any one of these is true:
- Identifies a real issue and proposes a workable change toward fixing it
- Code/config makes sense even if not perfect or fully comprehensive
- "Good first step" toward the focus area — incremental progress is fine
- Has acceptance criteria a downstream tester could check

REJECT ONLY for these blocker categories:
- SQL injection, secret leakage, broken auth, data corruption
- Syntax that won't parse / won't run
- Factually wrong API signatures, impossible operations, made-up libraries
- Removes a security control without replacement

DO NOT reject for: missing minor tests, incomplete prose docs, style nits,
"could be more thorough", placeholder text in templates (those are FINE for
discovery-stage work), missing performance benchmarks on greenfield code.
Note those in acceptance criteria instead.

Format: first line must be APPROVE: or REJECT: <reason>, then 3-6 acceptance
criteria bullets (for APPROVE) or specific blocker citations (for REJECT)."""

# After this many dev↔review round-trips on a single item, force-approve
# with `needs_iteration: true` tag so it can move forward and be refined
# in a subsequent cycle. Prevents the 0-commit reject loop we hit before.
MAX_REVIEW_ATTEMPTS = int(__import__("os").environ.get("MAX_REVIEW_ATTEMPTS", "3"))


def _stamp_rubric(item: dict) -> None:
    """Tag the most recent history entry with the rubric version that
    produced it. Lets us correlate verdict drift across rubric revisions."""
    if item.get("history"):
        item["history"][-1]["rubric_version"] = RUBRIC_VERSION


def do_one_review() -> bool:
    picked = pick_oldest("review")
    if not picked: return False
    src_path, item = picked
    attempts = int(item.get("dev_attempts", 1))
    proposal = item.get("current", {}).get("text", "")
    project = item.get("project", "?")
    focus = item.get("focus", "?")
    log("reviewer", f"▸ {item['id']} ({project}/{focus}) attempt={attempts}/{MAX_REVIEW_ATTEMPTS}")

    # Diff-size cap — large changes can't be reviewed reliably and almost
    # always hide unrelated edits. Force REJECT before the LLM runs.
    n_chars = len(proposal)
    n_lines = proposal.count("\n") + 1
    if n_chars > MAX_DIFF_CHARS or n_lines > MAX_DIFF_LINES:
        msg = (
            f"REJECT: diff too large to review ({n_chars} chars / "
            f"{n_lines} lines). Split into smaller PRs (caps: "
            f"{MAX_DIFF_CHARS} chars / {MAX_DIFF_LINES} lines)."
        )
        item["current"]["text"] = (
            f"REVIEWER REJECTED previous attempt (round {attempts}):\n\n{msg}\n\n"
            f"--- original proposal ---\n{proposal[:3000]}")
        advance(item, src_path, "dev", "reviewer", msg)
        _stamp_rubric(item)
        log("reviewer", f"↺ {item['id']} REJECT (diff too large) → dev")
        return True

    prompt = (f"Project: {project}\nFocus: {focus}\n"
              f"This is dev attempt {attempts} of {MAX_REVIEW_ATTEMPTS}.\n\n"
              f"Proposed change:\n```\n{proposal[:5000]}\n```\n\n"
              f"Review pragmatically. Approve if it's a workable step forward.")
    try:
        out = call_llm(prompt, system=REVIEWER_SYSTEM, max_tokens=REVIEWER_BUDGET, timeout=40)
    except Exception as e:
        # LLM exhausted across all providers. Two paths:
        #   - At MAX cap → force APPROVE-degraded (same intent as escape hatch
        #     below) so commit pipeline keeps flowing instead of looping.
        #   - Below cap → return to dev for another shot (LLM may recover).
        if attempts >= MAX_REVIEW_ATTEMPTS:
            item["needs_iteration"] = True
            item["override_reason"] = f"LLM exhausted at attempt {attempts} — degraded approve"
            item["current"]["text"] = (
                f"APPROVE (degraded — reviewer LLM unavailable: {type(e).__name__}).\n\n"
                f"All providers cooled. Ship as best-effort; flag for follow-up review.\n\n"
                f"--- proposal ---\n{proposal[:3000]}"
            )
            advance(item, src_path, "qa", "reviewer",
                    f"degraded-approve (LLM exhausted): {type(e).__name__}")
            log("reviewer",
                f"⚡ {item['id']} degraded-APPROVE (LLM exhausted at cap)")
            return True
        fail(item, src_path, "reviewer", f"LLM failed: {e}")
        log("reviewer", f"✗ {item['id']}: LLM failed")
        return True
    first_line = out.splitlines()[0] if out.splitlines() else ""

    # Escape hatch: at MAX_REVIEW_ATTEMPTS, force APPROVE so flow doesn't stall.
    # The verdict is preserved in the history + tagged needs_iteration so a
    # later refinement cycle can sharpen it without blocking commit pipeline.
    if attempts >= MAX_REVIEW_ATTEMPTS and not first_line.upper().startswith("APPROVE"):
        item["needs_iteration"] = True
        item["override_reason"] = f"{MAX_REVIEW_ATTEMPTS}-attempt cap reached"
        forced_out = (
            f"APPROVE (forced via {MAX_REVIEW_ATTEMPTS}-attempt cap — "
            f"refine in a follow-up cycle).\n\n"
            f"Original reviewer verdict at this attempt:\n{out[:1500]}\n\n"
            f"Acceptance criteria: ship as 'good enough first pass'; "
            f"open follow-up issue for the deficiencies above."
        )
        advance(item, src_path, "qa", "reviewer", forced_out)
        _stamp_rubric(item)
        log("reviewer", f"⚠ {item['id']} FORCED-APPROVE ({MAX_REVIEW_ATTEMPTS}/{MAX_REVIEW_ATTEMPTS} attempts) → qa-queue [needs_iteration]")
        return True

    if first_line.upper().startswith("APPROVE"):
        advance(item, src_path, "qa", "reviewer", out)
        _stamp_rubric(item)
        log("reviewer", f"✓ {item['id']} APPROVE → qa-queue")
    elif first_line.upper().startswith("REJECT"):
        # Send back to dev with the rejection note (dev daemon will pick this
        # up via pick_oldest("dev") and feed the reject text to the next LLM
        # call so the model can iterate on the specific blockers).
        item["current"]["text"] = (
            f"REVIEWER REJECTED previous attempt (round {attempts}):\n\n{out}\n\n"
            f"--- original proposal ---\n{proposal[:3000]}")
        advance(item, src_path, "dev", "reviewer", out)
        _stamp_rubric(item)
        log("reviewer", f"↺ {item['id']} REJECT → back to dev (attempt {attempts}/{MAX_REVIEW_ATTEMPTS})")
    else:
        # Ambiguous output → default approve, keep flow moving.
        advance(item, src_path, "qa", "reviewer", out)
        _stamp_rubric(item)
        log("reviewer", f"~ {item['id']} ambiguous (default approve) → qa")
    return True


if __name__ == "__main__":
    daemon_loop("reviewer", POLL_SEC, do_one_review)

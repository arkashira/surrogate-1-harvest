"""Review agent — tier-gated + consensus + ground-truth.

Replaces the simple review() in smart_dispatcher.py. Rules:

  1. Reviewer rank >= Writer rank (strict)
  2. Reviewer provider != Writer provider (cross-provider)
  3. For `critical=True` tasks: Reviewer rank >= Writer rank + 1, and 2-of-3 consensus
  4. If no eligible reviewer available RIGHT NOW → block (queue-wait),
     retry when cache refreshes. DO NOT downgrade to lower tier.
  5. Ground-truth check runs alongside reviewer opinion:
       code has blocking compile/parse failure → hard-fail regardless of reviewer
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from ground_truth import check as gt_check
from max_client import MAX_TIER_ORDER, MaxUnavailable, call_max, pick_max_model
from openrouter_client import (
    CHEAP_MODELS,
    FREE_MODELS,
    PREMIUM_MODELS,
    ORUnavailable,
    call_openrouter,
    is_on_cooldown,
)
from tier_rank import _provider_family, is_eligible_reviewer, pick_reviewer_from, rank


REVIEWER_SYSTEM = """You are a strict code review agent.

Your job:
  1. Check if the work fully addresses the task
  2. Check for correctness (syntax, logic, hallucinations)
  3. Check for completeness (edge cases, error handling)
  4. Rate severity of issues (low | med | high)

Output JSON only (no markdown, no prose):
{
  "verdict": "pass" | "needs_revision",
  "score": 0-10,
  "issues": [{"severity":"low|med|high","desc":"..."}],
  "suggestions": ["...", "..."],
  "reasoning": "1-2 sentences"
}

Rules:
  - Any "high" severity issue → always "needs_revision"
  - If you detect hallucinated APIs/functions → "needs_revision" with severity=high
  - Be rigorous — pass only when genuinely good
"""


class NoEligibleReviewer(Exception):
    """No reviewer currently available at required tier. Queue-wait."""


def _available_reviewers() -> list[str]:
    """Enumerate all currently available reviewer candidates.

    Max plan tiers (check quota) + OR tiers (check cooldowns).
    """
    cands: list[str] = []

    # Max tiers (use pick_max_model to respect cache)
    # We collect all three; caller picks based on tier
    for m in MAX_TIER_ORDER:
        # only include if not currently rate-limited long-term
        from max_client import load_quota_cache
        q = load_quota_cache().get(m)
        if not q or q.status == "allowed" or q.seconds_until_reset < 60:
            cands.append(m)

    # OR tiers
    for m in PREMIUM_MODELS + CHEAP_MODELS + FREE_MODELS:
        if not is_on_cooldown(m):
            cands.append(m)
    return cands


def _call_model_for_review(model: str, prompt: str, system: str) -> tuple[str, str]:
    """Route to Max or OR depending on model name. Returns (text, served_model_id)."""
    if model in MAX_TIER_ORDER:
        r = call_max(model, [{"role": "user", "content": prompt}],
                     max_tokens=1500, system=system, timeout=120)
        return r.content, r.model_served
    r = call_openrouter(model, [{"role": "user", "content": prompt}],
                        max_tokens=1500, system=system, timeout=120)
    return r.content, r.model_served


def _parse_json_verdict(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text[3:]
        text = text.lstrip("json").lstrip()
        if "```" in text:
            text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {"verdict": "needs_revision", "reasoning": "review parse failed",
            "raw": text[:500], "score": 0, "issues": [], "suggestions": []}


def review_once(
    task_prompt: str,
    work_product: str,
    writer_model: str,
    critical: bool = False,
    queue_wait_max_seconds: int = 600,
    poll_interval: int = 15,
) -> dict:
    """Single-reviewer review with tier enforcement.

    Blocks (queue-wait) up to queue_wait_max_seconds if no eligible reviewer.
    Raises NoEligibleReviewer after timeout.
    """
    deadline = time.time() + queue_wait_max_seconds

    reviewer: Optional[str] = None
    waits = 0
    while time.time() < deadline:
        cands = _available_reviewers()
        reviewer = pick_reviewer_from(cands, writer_model, critical=critical)
        if reviewer:
            break
        waits += 1
        time.sleep(poll_interval)

    if not reviewer:
        raise NoEligibleReviewer(
            f"no reviewer with rank>={rank(writer_model) + (1 if critical else 0)} "
            f"and provider!={_provider_family(writer_model)} after {queue_wait_max_seconds}s"
        )

    review_prompt = f"""# TASK
{task_prompt}

# WORK PRODUCT
{work_product}

# YOUR REVIEW (valid JSON only):"""

    try:
        text, served = _call_model_for_review(reviewer, review_prompt, REVIEWER_SYSTEM)
    except (MaxUnavailable, ORUnavailable) as e:
        # Reviewer itself errored — retry with fresh pool
        return {"verdict": "needs_revision", "reasoning": f"reviewer call failed: {e}",
                "reviewer_model": reviewer, "score": 0,
                "transport_error": True}

    parsed = _parse_json_verdict(text)
    parsed["reviewer_model"] = served
    parsed["reviewer_provider_family"] = _provider_family(served)
    parsed["reviewer_rank"] = rank(served)
    parsed["writer_rank"] = rank(writer_model)
    parsed["wait_cycles"] = waits
    return parsed


def review_with_consensus(
    task_prompt: str,
    work_product: str,
    writer_model: str,
    num_reviewers: int = 3,
    required_agree: int = 2,
    critical: bool = True,
    queue_wait_max_seconds: int = 600,
) -> dict:
    """Multi-reviewer consensus review. Used for critical tasks.

    Picks N reviewers from DIFFERENT provider families (+ cross-provider from writer).
    Verdict = pass if required_agree reviewers say "pass".
    """
    deadline = time.time() + queue_wait_max_seconds
    reviewers: list[str] = []
    used_families: set[str] = {_provider_family(writer_model)}

    # Collect N reviewers from N distinct families
    while len(reviewers) < num_reviewers and time.time() < deadline:
        cands = _available_reviewers()
        # Filter: eligible + family not yet used
        new_picks: list[str] = []
        for c in cands:
            fam = _provider_family(c)
            if fam in used_families:
                continue
            ok, _ = is_eligible_reviewer(writer_model, c, critical=critical)
            if ok:
                new_picks.append(c)
        # Pick highest rank per family
        by_family: dict[str, tuple[int, str]] = {}
        for c in new_picks:
            fam = _provider_family(c)
            r = rank(c)
            if fam not in by_family or by_family[fam][0] < r:
                by_family[fam] = (r, c)
        for fam, (_, model) in sorted(by_family.items(), key=lambda x: -x[1][0]):
            if len(reviewers) >= num_reviewers:
                break
            reviewers.append(model)
            used_families.add(fam)
        if len(reviewers) < num_reviewers:
            time.sleep(15)

    if len(reviewers) < required_agree:
        raise NoEligibleReviewer(
            f"consensus needs {required_agree} distinct-family reviewers, got {len(reviewers)}"
        )

    # Fire reviews
    individual_verdicts: list[dict] = []
    for rv in reviewers:
        try:
            v = review_once(task_prompt, work_product, writer_model,
                            critical=critical, queue_wait_max_seconds=30)
            # Force it to use THIS specific reviewer
            # (review_once picks top; we need to override — run directly)
            text, served = _call_model_for_review(
                rv,
                f"# TASK\n{task_prompt}\n\n# WORK PRODUCT\n{work_product}\n\n# YOUR REVIEW (JSON):",
                REVIEWER_SYSTEM,
            )
            parsed = _parse_json_verdict(text)
            parsed["reviewer_model"] = served
            parsed["reviewer_rank"] = rank(served)
            parsed["reviewer_provider_family"] = _provider_family(served)
            individual_verdicts.append(parsed)
        except (MaxUnavailable, ORUnavailable) as e:
            individual_verdicts.append(
                {"verdict": "needs_revision", "reasoning": f"reviewer error: {e}",
                 "reviewer_model": rv, "transport_error": True}
            )

    passes = sum(1 for v in individual_verdicts if v.get("verdict") == "pass")
    consensus_verdict = "pass" if passes >= required_agree else "needs_revision"

    # Aggregate issues from ALL reviewers (even if majority passes)
    all_issues: list[dict] = []
    all_suggestions: list[str] = []
    for v in individual_verdicts:
        all_issues.extend(v.get("issues", []) or [])
        all_suggestions.extend(v.get("suggestions", []) or [])

    return {
        "verdict": consensus_verdict,
        "consensus_pass_count": passes,
        "consensus_required": required_agree,
        "individual_verdicts": individual_verdicts,
        "issues": all_issues,
        "suggestions": all_suggestions,
        "reviewers": [v.get("reviewer_model") for v in individual_verdicts],
        "writer_rank": rank(writer_model),
        "reasoning": f"consensus {passes}/{len(individual_verdicts)} pass (required {required_agree})",
    }


def review_full(
    task_prompt: str,
    work_product: str,
    writer_model: str,
    critical: bool = False,
    use_consensus: bool = False,
) -> dict:
    """Full review = reviewer opinion + ground-truth check.

    Ground-truth BLOCKING failure → hard fail regardless of reviewer.
    """
    # 1. Ground-truth
    gt = gt_check(work_product)

    # 2. Reviewer opinion
    if use_consensus:
        reviewer = review_with_consensus(
            task_prompt, work_product, writer_model,
            num_reviewers=3, required_agree=2, critical=critical,
        )
    else:
        reviewer = review_once(task_prompt, work_product, writer_model, critical=critical)

    # 3. Combine
    final_verdict = reviewer.get("verdict", "needs_revision")
    if gt.get("blocking_failure"):
        final_verdict = "needs_revision"

    return {
        "verdict": final_verdict,
        "reviewer": reviewer,
        "ground_truth": gt,
        "override_by_ground_truth": gt.get("blocking_failure", False),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: review_agent.py <task-prompt> <work-product-file>")
        sys.exit(1)
    task = sys.argv[1]
    work = Path(sys.argv[2]).read_text()
    writer = sys.argv[3] if len(sys.argv) > 3 else "claude-haiku-4-5-20251001"
    critical = "--critical" in sys.argv
    consensus = "--consensus" in sys.argv
    r = review_full(task, work, writer, critical=critical, use_consensus=consensus)
    print(json.dumps({
        "verdict": r["verdict"],
        "ground_truth_verdict": r["ground_truth"]["verdict"],
        "ground_truth_blocking": r["ground_truth"]["blocking_failure"],
        "override_by_ground_truth": r["override_by_ground_truth"],
        "reviewer_model": r["reviewer"].get("reviewer_model"),
        "reviewer_rank": r["reviewer"].get("reviewer_rank"),
        "reviewer_verdict": r["reviewer"].get("verdict"),
    }, indent=2))

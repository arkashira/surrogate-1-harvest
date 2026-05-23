"""Smart dispatcher — Max plan → OR free → OR paid with checkpoint + review.

Tier priority (per Ashira 2026-04-19):
  1. Max Opus 4.x    (leverage flat-rate first)
  2. Max Sonnet 4.x  (same plan, same pool typically)
  3. Max Haiku 4.x   (cheapest Max tier)
  4. OR FREE models  (qwen / gpt-oss / llama / nemotron / glm)
  5. OR CHEAP paid   (deepseek / grok-fast)
  6. OR PREMIUM paid (gpt-5 / claude-opus / claude-sonnet via OR)

Continuous re-check: every 5 min probe Max tiers — if Opus/Sonnet come back
available, subsequent calls return to them (honor Max plan flat-rate).

Review retry: INFINITE per Ashira — runs revisions until reviewer passes.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).parent))

from checkpoint import Checkpoint
from codebase_scanner import as_context_prompt, scan
from max_client import (
    MAX_TIER_ORDER,
    MODEL_HAIKU,
    MODEL_OPUS,
    MODEL_SONNET,
    MaxAuthError,
    MaxUnavailable,
    call_max,
    pick_max_model,
    probe_and_refresh_cache,
)
from openrouter_client import (
    CHEAP_MODELS,
    FREE_MODELS,
    PREMIUM_MODELS,
    ORResponse,
    ORUnavailable,
    call_openrouter,
    is_on_cooldown,
)
from review_agent import NoEligibleReviewer, review_full


LAST_MAX_PROBE: list[float] = [0.0]
MAX_PROBE_INTERVAL = 300  # 5 min


class DispatchResult:
    def __init__(self, text: str, provider: str, model: str, input_tokens: int = 0, output_tokens: int = 0):
        self.text = text
        self.provider = provider
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _tier_iter() -> list[tuple[str, list[str]]]:
    """Ordered tiers to try in strict priority."""
    return [
        ("max",     MAX_TIER_ORDER),
        ("or_free", FREE_MODELS),
        ("or_cheap", CHEAP_MODELS),
        ("or_premium", PREMIUM_MODELS),
    ]


def _maybe_probe_max() -> None:
    """Every 5 min, send minimal probes to each Max tier to refresh cache."""
    if time.time() - LAST_MAX_PROBE[0] > MAX_PROBE_INTERVAL:
        try:
            probe_and_refresh_cache()
            LAST_MAX_PROBE[0] = time.time()
        except MaxAuthError:
            pass  # handled at call time


def dispatch(
    prompt: str,
    system: Optional[str] = None,
    task_id: Optional[str] = None,
    max_tokens: int = 4096,
    checkpoint: Optional[Checkpoint] = None,
    prefer_max: bool = True,
    exclude_providers: set[str] | None = None,
    on_attempt: Optional[Callable[[str, str], None]] = None,
) -> DispatchResult:
    """Try tiers in order until one succeeds. Logs to checkpoint.

    Args:
      prompt: user message
      system: system prompt (optional)
      task_id: for logging
      max_tokens: output cap
      checkpoint: Checkpoint instance for event logging
      prefer_max: try Max first (True) — set False for review agent (cross-provider)
      exclude_providers: skip these providers (e.g. {"max"} to force OR)
      on_attempt: callback(provider, model) called per attempt (for debugging)

    Returns DispatchResult or raises if ALL tiers exhausted.
    """
    exclude = exclude_providers or set()
    messages = [{"role": "user", "content": prompt}]
    _maybe_probe_max()

    tiers = _tier_iter()
    if not prefer_max:
        tiers = [t for t in tiers if t[0] != "max"]

    errors: list[str] = []

    for tier_name, models in tiers:
        if tier_name in exclude:
            continue

        if tier_name == "max":
            m = pick_max_model()
            if m is None:
                errors.append("max: all tiers rate-limited")
                continue
            if on_attempt:
                on_attempt("max", m)
            if checkpoint:
                checkpoint.append("provider_selected", provider="max", model=m)
            try:
                r = call_max(m, messages, max_tokens=max_tokens, system=system)
                if checkpoint:
                    checkpoint.append("provider_success", provider="max", model=m,
                                      content_preview=r.content[:200],
                                      input_tokens=r.input_tokens,
                                      output_tokens=r.output_tokens)
                return DispatchResult(r.content, "max", m, r.input_tokens, r.output_tokens)
            except MaxUnavailable as e:
                errors.append(f"max:{m} 429 (reset {e.reset_at})")
                if checkpoint:
                    checkpoint.append("provider_failed", provider="max", model=m,
                                      reason=f"rate_limit reset_at={e.reset_at}")
                continue
            except MaxAuthError as e:
                errors.append(f"max auth: {e}")
                if checkpoint:
                    checkpoint.append("provider_failed", provider="max", reason=f"auth: {e}")
                # Max totally broken — skip tier but keep going with OR
                continue
        else:
            # OR tier
            for m in models:
                if is_on_cooldown(m):
                    continue
                if on_attempt:
                    on_attempt(tier_name, m)
                if checkpoint:
                    checkpoint.append("provider_selected", provider=tier_name, model=m)
                try:
                    r = call_openrouter(m, messages, max_tokens=max_tokens, system=system)
                    if checkpoint:
                        checkpoint.append("provider_success", provider=tier_name, model=m,
                                          content_preview=r.content[:200],
                                          input_tokens=r.input_tokens,
                                          output_tokens=r.output_tokens)
                    return DispatchResult(r.content, tier_name, m, r.input_tokens, r.output_tokens)
                except ORUnavailable as e:
                    errors.append(f"{tier_name}:{m} {e.code}")
                    if checkpoint:
                        checkpoint.append("provider_failed", provider=tier_name, model=m,
                                          reason=f"{e.code}: {e.body[:100]}")
                    continue

    # All tiers exhausted
    raise RuntimeError(f"all providers exhausted: {errors}")


# ----------------------------------------------------------------------
# Review agent (cross-provider debate)
# ----------------------------------------------------------------------
REVIEWER_SYSTEM = """You are a strict code review agent. You review another AI's work for a given task.
Your job:
  1. Check if the work fully addresses the task
  2. Check for correctness (syntax, logic, hallucinations)
  3. Check for completeness (edge cases, error handling)
  4. Rate severity of issues

Output JSON only, no prose:
{
  "verdict": "pass" | "needs_revision",
  "score": 0-10,
  "issues": [{"severity":"low|med|high","desc":"..."}],
  "suggestions": ["...", "..."],
  "reasoning": "1-2 sentences"
}

If no issues, "pass". If ANY "high" severity issue → always "needs_revision"."""


def review(
    task_prompt: str,
    work_product: str,
    writer_provider: str,
    checkpoint: Optional[Checkpoint] = None,
) -> dict:
    """Send work for cross-provider review. Uses different provider than writer.

    Returns:
      {"verdict": "pass|needs_revision", "score": int, "issues": [...],
       "suggestions": [...], "reasoning": "...", "reviewer_model": "..."}
    """
    # Cross-provider: if writer was Max/Anthropic → reviewer from OR non-Anthropic
    exclude = set()
    if writer_provider == "max":
        exclude.add("max")  # reviewer uses OR

    review_prompt = f"""# TASK ORIGINAL
{task_prompt}

# WORK PRODUCT TO REVIEW
{work_product}

# YOUR REVIEW (JSON only):"""

    if checkpoint:
        checkpoint.append("review_requested", writer_provider=writer_provider)

    result = dispatch(
        prompt=review_prompt,
        system=REVIEWER_SYSTEM,
        checkpoint=checkpoint,
        max_tokens=1500,
        exclude_providers=exclude,
        prefer_max=(writer_provider != "max"),
    )

    # Parse JSON from response
    text = result.text.strip()
    # Strip markdown fence
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text[3:]
        text = text.lstrip("json").lstrip()
        if "```" in text:
            text = text.rsplit("```", 1)[0]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Look for {...} block
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = {"verdict": "needs_revision", "reasoning": "review parse failed",
                          "raw": text[:500]}
        else:
            parsed = {"verdict": "needs_revision", "reasoning": "review parse failed",
                      "raw": text[:500]}

    parsed["reviewer_provider"] = result.provider
    parsed["reviewer_model"] = result.model
    if checkpoint:
        checkpoint.append("review_verdict", **parsed)
    return parsed


# ----------------------------------------------------------------------
# Full orchestration
# ----------------------------------------------------------------------
def execute_task(
    task_id: str,
    prompt: str,
    system_base: str = "",
    max_tokens: int = 4096,
    max_review_iterations: int = 0,  # 0 = infinite (per Ashira)
    codebase_artifacts: list[str] | None = None,
    critical: bool = False,          # True → reviewer rank > writer + consensus 2/3
    use_consensus: bool = False,     # True → 2-of-3 reviewers vote
) -> dict:
    """End-to-end: scan codebase → dispatch → review → revise until pass.

    Returns: {"task_id","final_text","iterations","reviewer_verdict",...}
    """
    cp = Checkpoint.open(task_id)

    # Resume support
    existing_state = cp.resume_state()
    iteration = existing_state["review_iterations"]
    draft = existing_state["draft_text"]
    if existing_state["completed"]:
        return {"task_id": task_id, "status": "already_done",
                "final_text": draft, "iterations": iteration}

    if not existing_state["started"]:
        cp.append("task_start", prompt=prompt[:500])

        # Phase 1: codebase review
        report = scan(prompt, codebase_artifacts)
        cp.append("codebase_review",
                  artifacts=[f["path"] for f in report["recent_files"][:15]],
                  uncommitted_repos=len(report["uncommitted_repos"]),
                  semantic_hits=len(report["semantic_hits"]))
        codebase_ctx = as_context_prompt(report, 6000)
        system = (system_base + "\n\n" + codebase_ctx).strip()
    else:
        # Resume: re-scan codebase (may have changed)
        report = scan(prompt, codebase_artifacts)
        cp.append("codebase_review",
                  artifacts=[f["path"] for f in report["recent_files"][:15]],
                  resumed=True)
        codebase_ctx = as_context_prompt(report, 6000)
        system = (system_base + "\n\n" + codebase_ctx).strip()
        # Include prior draft as context for continuation
        if draft:
            system += f"\n\n## Previous attempt (continue/refine this):\n{draft[:3000]}"

    # Phase 2: dispatch + review loop
    last_review: dict | None = None
    accumulated_feedback = ""

    while True:
        iteration += 1
        iter_prompt = prompt
        if accumulated_feedback:
            iter_prompt = f"{prompt}\n\n## Reviewer feedback from prior iteration (address these):\n{accumulated_feedback}"

        result = dispatch(
            prompt=iter_prompt,
            system=system,
            checkpoint=cp,
            max_tokens=max_tokens,
        )
        draft = result.text
        cp.append("result_draft", text=draft, iteration=iteration,
                  provider=result.provider, model=result.model)

        # Review — tier-enforced + ground-truth via review_agent.review_full
        try:
            full_review = review_full(
                task_prompt=prompt,
                work_product=draft,
                writer_model=result.model,
                critical=critical,
                use_consensus=use_consensus or critical,
            )
            cp.append("review_full",
                      verdict=full_review["verdict"],
                      reviewer_model=full_review["reviewer"].get("reviewer_model"),
                      reviewer_rank=full_review["reviewer"].get("reviewer_rank"),
                      writer_rank=full_review["reviewer"].get("writer_rank"),
                      ground_truth_verdict=full_review["ground_truth"]["verdict"],
                      ground_truth_blocking=full_review["ground_truth"]["blocking_failure"],
                      override_by_ground_truth=full_review["override_by_ground_truth"])
            last_review = dict(full_review["reviewer"])
            last_review["verdict"] = full_review["verdict"]
            last_review["ground_truth"] = full_review["ground_truth"]
        except NoEligibleReviewer as e:
            cp.append("review_blocked", reason=str(e))
            # Queue-wait: don't consume iteration, poll + retry
            time.sleep(30)
            iteration -= 1
            continue

        verdict = last_review.get("verdict", "needs_revision")
        if verdict == "pass":
            cp.append("task_done", iteration=iteration, final_length=len(draft))
            cp.archive()
            return {
                "task_id": task_id,
                "status": "done",
                "final_text": draft,
                "iterations": iteration,
                "last_review": last_review,
                "writer": f"{result.provider}/{result.model}",
            }

        # needs_revision — assemble feedback
        issues = last_review.get("issues", [])
        suggestions = last_review.get("suggestions", [])
        fb_lines = []
        for i in issues:
            fb_lines.append(f"- [{i.get('severity','?')}] {i.get('desc','')}")
        for s in suggestions:
            fb_lines.append(f"- {s}")
        accumulated_feedback = "\n".join(fb_lines) if fb_lines else last_review.get("reasoning", "")
        cp.append("revision_requested", iteration=iteration,
                  feedback=accumulated_feedback[:500])

        # Safety: if max_review_iterations > 0, enforce it. 0 = infinite.
        if max_review_iterations > 0 and iteration >= max_review_iterations:
            cp.append("task_failed", reason=f"max_iterations_{max_review_iterations}")
            cp.archive()
            return {
                "task_id": task_id,
                "status": "failed_max_iter",
                "final_text": draft,
                "iterations": iteration,
                "last_review": last_review,
            }


if __name__ == "__main__":
    import uuid
    if len(sys.argv) < 2:
        print("usage: smart_dispatcher.py <prompt>")
        sys.exit(1)
    task_id = "adhoc-" + uuid.uuid4().hex[:8]
    prompt = " ".join(sys.argv[1:])
    r = execute_task(task_id, prompt, max_tokens=500)
    print(json.dumps({
        "task_id": r["task_id"],
        "status": r["status"],
        "iterations": r["iterations"],
        "writer": r.get("writer"),
        "preview": r["final_text"][:400],
    }, indent=2))

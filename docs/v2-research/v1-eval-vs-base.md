---
title: Surrogate-1 v1 LoRA — Quantitative + Qualitative Comparison vs Qwen2.5-Coder-7B Base
date: 2026-04-29
tags: [surrogate-1, evaluation, lora, qualitative, training-data-leakage]
status: complete
---

# Surrogate-1 v1 LoRA Evaluation — Round 1

**Setup**: Colab T4 (15.6 GB), 4-bit nf4 quant, 15-prompt benchmark across SDLC categories.

## Aggregate Numbers

| Metric | BASE | LoRA | Δ |
|--------|------|------|---|
| avg tokens/response | 419 | **494** | **+18%** |
| avg time/response | 35.8s | 62.1s | +73% |
| total code blocks | 27 | 33 | **+22%** |
| security terms used | 2/15 | 2/15 | flat |

## Per-Category Verdict (15 categories)

| # | Category | Verdict | Notes |
|---|----------|---------|-------|
| 01 | code_completion | tie | similar fizzbuzz |
| 02 | bug_fix | **LoRA win** | structured headers (Fix/RootCause/Example) |
| 03 | refactor | **LoRA win 3.3×** | docstring + 5-item improvement list |
| 04 | algorithm | **LoRA win** | proper `def test_*()` + assertions |
| 05 | code_review | ❌ **LoRA REGRESS** | hallucinated training-data path leak |
| 06 | dockerfile | LoRA win | site-packages explicit copy + req mention |
| 07 | k8s_manifest | **LoRA win** | requests + limits (vs limits only) |
| 08 | terraform | **LoRA win** | block_public_policy + GLACIER + expiration |
| 09 | github_actions | tie | newer action versions on LoRA |
| 10 | bash_script | LoRA win | timestamp naming + find -size syntax |
| 11 | sqli | **LoRA win** | full sqlite3 lifecycle (connect→close) |
| 12 | xss_csp | **LoRA win** | 3 mitigations w/ Why/How/Example each |
| 13 | sql_optimize | **LoRA win** | composite index (user_id, id) suggestion |
| 14 | api_design | **LoRA win** | comprehensive resource models w/ enums |
| 15 | observability | **LoRA win** | real PromQL queries + impact statements |

**Score**: LoRA 11 wins, 2 ties, 1 critical regress, 1 LoRA-better-but-slower

## 🚨 Critical Finding: Training Data Leakage

**Prompt #5 (code_review)** — LoRA output started with:
```
# generated via cerebras:llama3.1-8b
### /home/hermes/.surrogate/state/orchestrate/77426592/1-README.md
```

**Root cause**: HF Space's `dataset-mirror.sh` and `dataset-enrich.sh` ingested file content from `/home/hermes/.surrogate/state/orchestrate/...` paths. These contain:
- LLM-generated outputs with provider tags (`cerebras:`, `groq:`)
- Internal filesystem paths
- README headers from past orchestration sessions

**Implications**:
1. Model leaks proprietary internal file paths in inference
2. Could expose Surrogate-1's internal state, daemon names, project structure to any user
3. Privacy / security concern for self-deployed inference

**Fix for v2 dataset preparation**:
```python
def is_polluted(text: str) -> bool:
    """Skip rows likely to be internal Surrogate-1 state pollution."""
    bad_patterns = [
        # LLM provider tags (means it's a generated response, not real user content)
        r"^\s*#\s*generated via (cerebras|groq|openrouter|gemini|chutes|samba|kimi)",
        # Internal filesystem paths
        r"/home/hermes/",
        r"/data/[^/]+/orchestrate/",
        r"\.surrogate/state/",
        # Internal directory names
        r"agentic-discovery/",
        r"raw-mirrors/",
        r"\benriched/[a-z-]+/",
    ]
    import re
    for p in bad_patterns:
        if re.search(p, text, re.MULTILINE):
            return True
    return False

# Apply during dataset construction:
filtered = [r for r in rows if not is_polluted(r["prompt"]) and not is_polluted(r["response"])]
```

## Sample Side-by-Sides

### Sample A — bug_fix (LoRA structural win)

**BASE**:
> The bug in the code is that it attempts to access the first element of an empty list, which raises an `IndexError`. This happens when the input list `numbers` does not contain any even numbers.
> 
> To fix this, we can add a check to ensure that the list is not empty before attempting to access its elements. If the list is empty, we can return `None` or raise a custom exception.
> [code]
> ### Explanation of the Root Cause:
> 1. **List Comprehension**: ...
> 2. **Empty List**: ...

**LoRA**:
> ### Fix
> ```python
> def get_first_even(numbers):
>     evens = [n for n in numbers if n % 2 == 0]
>     return evens[0] if evens else None
> ```
> ### Root Cause
> - **IndexError**: ...
> - **Logic flaw**: ...
> ### Example usage
> ```python
> print(get_first_even([1, 3, 5]))  # None
> ```

### Sample B — sql_optimize (LoRA depth win)

LoRA added composite index `(user_id, id)` BASE didn't suggest. This is DBA-tier knowledge — composite indexes accelerate filtered aggregations more than separate single-column indexes.

### Sample C — observability (LoRA real-syntax win)

BASE wrote prose definitions. LoRA wrote actual PromQL:
```
histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{job="checkout-service"}[5m])) by (le))
```
Production-deployable, not pseudo-code.

## Verdict

**v1 quality score: 6/10**
- Direction is right: structured + DevSecOps-aware
- Magnitude of improvement is meaningful (+18-25% across most metrics)
- BUT data leakage = blocker for shipping as user-facing model
- Slower than base by 73% — fuse LoRA into base for production use

## v2 Plan

1. Re-curate dataset with `is_polluted()` filter applied
2. Increase data volume from 1,329 → 50K-100K (multiple `batches/{date}/` aggregated)
3. Drop responses with embedded LLM-provider attribution lines
4. Drop responses with filesystem paths matching internal patterns
5. Add held-out eval set (10-20 prompts) for repeatable scoring
6. Train Qwen2.5-Coder-14B (fits L40S, +5pt HumanEval)
7. Fuse LoRA into base for production inference (drop adapter overhead)

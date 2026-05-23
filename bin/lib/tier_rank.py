"""Model tier rank — enforces "reviewer >= writer" quality rule.

Rank scale (1-10, approximate SWE-Bench Verified + LMArena Q1 2026):
  10  Claude Opus 4.7, GPT-5.4
   9  Claude Sonnet 4.6, GPT-5.4-pro, Grok 4.20, Gemini 3.1 Pro
   8  Claude Opus 4.6, DeepSeek V3.2 (coding strong)
   7  Claude Haiku 4.5, Grok 4.1 Fast, Qwen 3.6 35B-MoE
   6  Llama 3.3 70B, Mistral Large 3, Kimi K2.5, Qwen 3.5 Coder 32B
   5  Nemotron 120B, GLM 4.5 Air, Qwen 3.5 Coder 14B
   4  GPT-OSS 120B, Gemma 4 31B
   3  GPT-OSS 20B, Llama 3.3 8B, small local

Policy (per Ashira 2026-04-19):
  - Reviewer tier MUST be >= writer tier.
  - For code/IaC/security tasks, prefer reviewer tier > writer by 1.
  - If no eligible reviewer available → queue-wait (DO NOT downgrade writer).
"""

from __future__ import annotations

TIER_RANK: dict[str, int] = {
    # === 10: frontier ===
    "anthropic/claude-opus-4.7": 10,
    "openai/gpt-5.4": 10,
    "openrouter/anthropic/claude-opus-4.7": 10,
    "openrouter/openai/gpt-5.4": 10,

    # === 9: premium ===
    "anthropic/claude-sonnet-4.6": 9,
    "openai/gpt-5.4-pro": 9,
    "x-ai/grok-4.20": 9,
    "google/gemini-3.1-pro": 9,
    "openrouter/anthropic/claude-sonnet-4.6": 9,
    "openrouter/x-ai/grok-4.20": 9,
    # Max-plan native (OAuth)
    "claude-opus-4-20250514": 9,      # Opus 4 (Max plan native)
    "claude-sonnet-4-20250514": 9,    # Sonnet 4 (Max plan native)

    # === 8: strong ===
    "anthropic/claude-opus-4.6": 8,
    "deepseek/deepseek-v3.2": 8,
    "openrouter/deepseek/deepseek-v3.2": 8,

    # === 7: capable ===
    "anthropic/claude-haiku-4.5": 7,
    "x-ai/grok-4.1-fast": 7,
    "openrouter/anthropic/claude-haiku-4.5": 7,
    "openrouter/x-ai/grok-4.1-fast": 7,
    "claude-haiku-4-5-20251001": 7,   # Haiku 4.5 (Max plan native)
    "qwen/qwen3.6-35b-a3b": 7,
    "openrouter/qwen/qwen3.6-35b-a3b": 7,

    # === 6: mid ===
    "meta-llama/llama-3.3-70b-instruct": 6,
    "qwen/qwen3-next-80b-a3b-instruct": 6,
    "qwen/qwen3-coder": 6,
    "moonshotai/kimi-k2.5": 6,
    "mistral-large-3": 6,

    # === 5: weak-mid ===
    "nvidia/nemotron-3-super-120b-a12b": 5,
    "z-ai/glm-4.5-air": 5,

    # === 4: small ===
    "openai/gpt-oss-120b": 4,
    "google/gemma-4-31b-it": 4,

    # === 3: tiny / free ===
    "openai/gpt-oss-20b": 3,
    "meta-llama/llama-3.3-8b-instruct": 3,
}


def rank(model: str) -> int:
    """Return rank 1-10, defaulting to 5 for unknown models."""
    if not model:
        return 5
    # Strip :free suffix
    base = model.replace(":free", "").strip("/")
    if base in TIER_RANK:
        return TIER_RANK[base]
    # Try progressively stripping path components
    for prefix in ("openrouter/", ""):
        for candidate in [prefix + base, base.replace(prefix, "")]:
            if candidate in TIER_RANK:
                return TIER_RANK[candidate]
    # Partial match (last-resort — for unknown variants of known families)
    lower = base.lower()
    if "opus-4.7" in lower or "opus-4-7" in lower: return 10
    if "gpt-5.4" in lower and "mini" not in lower and "nano" not in lower: return 10
    if "sonnet-4.6" in lower or "sonnet-4-6" in lower: return 9
    if "opus-4" in lower or "opus_4" in lower: return 8
    if "grok-4.2" in lower: return 9
    if "gemini-3" in lower and "flash" not in lower: return 9
    if "haiku-4" in lower: return 7
    if "deepseek-v3" in lower: return 8
    if "grok-4.1" in lower or "grok-fast" in lower: return 7
    if "qwen3.6" in lower: return 7
    if "llama-3.3-70" in lower: return 6
    if "nemotron" in lower: return 5
    if "glm-4.5" in lower: return 5
    if "gpt-oss-120" in lower: return 4
    if "gemma-4-31" in lower: return 4
    if "gpt-oss-20" in lower: return 3
    return 5


def is_eligible_reviewer(writer_model: str, reviewer_model: str,
                         critical: bool = False,
                         cross_provider_required: bool = True) -> tuple[bool, str]:
    """Check if reviewer qualifies.

    Rules:
      1. rank(reviewer) >= rank(writer)         [always]
      2. rank(reviewer) >= rank(writer) + 1     [when critical]
      3. reviewer provider != writer provider   [when cross_provider_required]

    Returns (ok, reason).
    """
    wr = rank(writer_model)
    rr = rank(reviewer_model)
    min_rank = wr + 1 if critical else wr

    if rr < min_rank:
        return False, f"reviewer rank {rr} < required {min_rank} (writer={wr})"

    if cross_provider_required:
        wp = _provider_family(writer_model)
        rp = _provider_family(reviewer_model)
        if wp == rp and wp != "unknown":
            return False, f"same provider family '{wp}' — need cross-provider"

    return True, f"ok: rank {rr} >= {min_rank}, cross-provider satisfied"


def _provider_family(model: str) -> str:
    """Group models by maker for cross-provider check."""
    m = model.lower()
    if "claude" in m or "anthropic" in m:
        return "anthropic"
    if "gpt-" in m or "openai" in m or "gpt_" in m:
        return "openai"
    if "gemini" in m or "gemma" in m:
        return "google"
    if "grok" in m or "x-ai" in m:
        return "xai"
    if "deepseek" in m:
        return "deepseek"
    if "qwen" in m:
        return "qwen"
    if "llama" in m or "meta" in m:
        return "meta"
    if "kimi" in m or "moonshot" in m:
        return "moonshot"
    if "mistral" in m:
        return "mistral"
    if "nemotron" in m or "nvidia" in m:
        return "nvidia"
    if "glm" in m or "z-ai" in m:
        return "zai"
    return "unknown"


def pick_reviewer_from(candidates: list[str], writer_model: str,
                       critical: bool = False) -> str | None:
    """Pick highest-rank eligible reviewer from a list of available models."""
    scored: list[tuple[int, str]] = []
    for c in candidates:
        ok, _ = is_eligible_reviewer(writer_model, c, critical=critical)
        if ok:
            scored.append((rank(c), c))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    return scored[0][1]


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        w, r = sys.argv[1], sys.argv[2]
        crit = "--critical" in sys.argv
        ok, reason = is_eligible_reviewer(w, r, critical=crit)
        print(f"writer={w} rank={rank(w)}")
        print(f"reviewer={r} rank={rank(r)}")
        print(f"eligible={ok}: {reason}")
    else:
        for m in ["claude-opus-4-20250514", "claude-sonnet-4-20250514",
                  "claude-haiku-4-5-20251001", "openai/gpt-5.4",
                  "deepseek/deepseek-v3.2", "openai/gpt-oss-120b:free",
                  "qwen/qwen3-coder:free", "meta-llama/llama-3.3-70b-instruct:free"]:
            print(f"  rank({m}) = {rank(m)}  [{_provider_family(m)}]")

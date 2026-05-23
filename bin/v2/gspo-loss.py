"""Surrogate-1 v2 — GSPO sequence-level importance ratio (Round 7 Tier 2).

Reference: arxiv.org/abs/2507.18071 (Zheng et al. 2025)

GRPO baseline: importance ratio = π_θ(a_t|s_t) / π_old(a_t|s_t) per TOKEN
GSPO:           importance ratio = exp(mean log-prob diff over full SEQUENCE)

Why: token-level ratios on long code outputs (>2k tokens) explode → unstable
RL. Sequence-level is much more numerically stable.

Drop-in replacement for the policy-gradient inner term in TRL/verl/slime
GRPO loops. ~50 LOC swap.

Usage in trainer:
    from gspo_loss import sequence_importance_ratio, gspo_loss
    ratio = sequence_importance_ratio(new_logprobs, old_logprobs, attn_mask)
    loss = gspo_loss(ratio, advantages, clip_eps=0.28, clip_high_eps=0.30)

Compose with DAPO (clip-higher + dynamic sampling + token-level → swap to
seq-level) for best results on long-output code RL.
"""
from __future__ import annotations
import torch


def sequence_importance_ratio(
    new_logprobs: torch.Tensor,    # [B, T] log π_θ(a_t|s_t)
    old_logprobs: torch.Tensor,    # [B, T] log π_old(a_t|s_t)
    attention_mask: torch.Tensor,  # [B, T] 1 for response tokens, 0 for prompt/pad
) -> torch.Tensor:
    """Returns [B] sequence-level importance ratio.

    ratio_i = exp(mean_t (new_t - old_t) for valid t)

    Mean over response tokens only (mask out prompt + padding).
    """
    diff = new_logprobs - old_logprobs            # [B, T]
    diff = diff * attention_mask
    # Average over valid tokens
    n_valid = attention_mask.sum(dim=-1).clamp(min=1)
    seq_log_ratio = diff.sum(dim=-1) / n_valid    # [B]
    return seq_log_ratio.exp()                    # [B]


def gspo_loss(
    seq_ratio: torch.Tensor,       # [B] from sequence_importance_ratio
    advantages: torch.Tensor,      # [B] normalized advantages
    clip_eps: float = 0.28,        # DAPO-style high clip lower bound
    clip_high_eps: float = 0.30,   # asymmetric upper clip (clip-higher)
) -> torch.Tensor:
    """GSPO loss with DAPO clip-higher.

    L = -E[ min( ratio * A, clip(ratio, 1-eps, 1+high_eps) * A ) ]

    Asymmetric clip prevents collapse on positive-advantage spikes
    while keeping the negative side tight (per DAPO).
    """
    ratio_clipped = torch.clamp(seq_ratio,
                                 min=1.0 - clip_eps,
                                 max=1.0 + clip_high_eps)
    surr1 = seq_ratio * advantages
    surr2 = ratio_clipped * advantages
    loss = -torch.minimum(surr1, surr2).mean()
    return loss


# CLI smoke test (dummy data)
if __name__ == "__main__":
    import sys
    torch.manual_seed(42)
    B, T = 4, 256
    new_lp = torch.randn(B, T) * 0.1
    old_lp = torch.randn(B, T) * 0.1
    mask = torch.ones(B, T)
    mask[:, :32] = 0  # first 32 = prompt
    adv = torch.randn(B)

    ratio = sequence_importance_ratio(new_lp, old_lp, mask)
    loss = gspo_loss(ratio, adv)
    print(f"ratios: {ratio.tolist()}")
    print(f"loss:   {loss.item():.6f}")
    print(f"grad ok: {loss.requires_grad}")
    sys.exit(0 if 0.5 < ratio.mean().item() < 2.0 else 1)

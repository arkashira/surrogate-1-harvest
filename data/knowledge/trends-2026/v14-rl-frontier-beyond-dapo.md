---
title: V14 RL Frontier — Beyond DAPO/GRPO/TruthRL Stack
date: 2026-05-01
status: research-complete
tags: [v14, rl, grpo, dapo, vapo, dr-grpo, gspo, prime, rloo, reinforce-pp, simpo, kto, orpo, dpo, rlvr, self-play, federated-rl, multi-agent-rl, hallucination, correctness]
sources: arxiv.org, github.com, huggingface.co/papers, verl.readthedocs.io
target: V14+ Surrogate-1 RL stack — additions beyond V13 (TruthRL ternary GRPO + DAPO + ORPO + KTO + Mask-DPO + F-DPO + RLCR + Constitutional AI)
base: Qwen2.5-7B / Qwen3-8B / Qwen3-14B / Qwen3-32B
gpu_tier: T4×2 (16GB) — LoRA only | L40S 48GB — full FT possible
---

# V14 RL Frontier Beyond V13 — 2025-2026 Methods

> **V13 already has**: DAPO (Clip-Higher / Dyn Sampling / Token-PG / Overlong Shaping), GRPO, ORPO, KTO, Mask-DPO, F-DPO, RLCR (probability calibration), TruthRL (ternary +1/0/-1 reward), Constitutional AI v1/v2.
> **V14 adds 2025-2026 frontier**: VAPO, Dr-GRPO, GSPO, PRIME, REINFORCE++, RLOO, SimPO, SPPO/SPIN, AceReason staged math→code RL, Skywork-OR1 recipe, Open-Reasoner-Zero (vanilla PPO base-model RL), TÜLU 3 RLVR, Nemotron multi-env RLVR, MA-RLHF macro-actions, MARTI multi-agent, CDE curiosity, NPO/SimNPO unlearn, NLHF/Nash, 2-GRPO, Step-DPO/Full-Step-DPO, Self/Meta-Rewarding, Auto Red-Teaming RL, AlphaProof (Lean test-time RL).

---

## TIER S — Drop-In Replacements / Extensions for V13 GRPO Loop (HIGH ROI)

### 1. VAPO — Value-Augmented PO (ByteDance Seed)
- Paper: [VAPO: Efficient and Reliable RL for Advanced Reasoning Tasks](https://arxiv.org/abs/2504.05118) (April 2025, ByteDance Seed, Yu Yue + 26 authors)
- Benchmark: **AIME 2024 = 60.4 on Qwen2.5-32B base** — beats DAPO and DeepSeek-R1-Zero-Qwen-32B by **+10 points** under identical setup. Reaches SOTA in **5,000 steps**.
- Combines value-based PPO + 7 stabilizers + **Length-Adaptive GAE** (λ_critic = 1.0 for unbiased value targets, λ_policy adaptive based on response length).
- **Length-Adaptive GAE alone = +15 points** (paper ablation). λ_policy = 1 - 1/(α·|y|), α tuned.
- Critique paper: [Towards Analyzing and Understanding Limitations of VAPO](https://arxiv.org/html/2506.03038v1) (June 2025) — value model still high-variance on very long CoT.
- T4×2 feasibility: ⚠️ **needs value head** (PPO-style 4-model setup: policy + reference + value + reward). On T4×2 only feasible with Qwen2.5-1.5B/3B + LoRA + offload value to CPU. Skip on T4 → use on L40S 48GB.
- Combo with V13: replaces DAPO's actor-critic-free path with explicit value model — **conflicts with GRPO/DAPO** (cannot run both simultaneously). Use as ALTERNATIVE branch when L40S available.

```python
# verl config snippet (VAPO recipe-style)
algorithm:
  adv_estimator: gae
  gae_lam_critic: 1.0          # unbiased value target
  gae_lam_policy_alpha: 0.05   # length-adaptive coefficient
  use_length_adaptive_gae: true
  clip_higher: 0.28            # carries DAPO Clip-Higher
  clip_lower: 0.20
  kl_coef: 0.0                 # no KL (DAPO/VAPO finding)
  loss_agg_mode: token-level   # DAPO carry
```

### 2. Dr-GRPO — Bias-Free GRPO (Sea AI Lab)
- Paper: [Understanding R1-Zero-Like Training: A Critical Perspective](https://arxiv.org/pdf/2503.20783) (COLM 2025, Liu et al., Sea AI Lab)
- Repo: [sail-sg/understand-r1-zero](https://github.com/sail-sg/understand-r1-zero)
- Fix: **remove length normalization + std normalization** in GRPO loss → unbiased gradient. Standard GRPO inflates response length on **incorrect** outputs (length hacking). Dr-GRPO fixes.
- Benchmark: SOTA on Qwen2.5-Math-7B with **27h on 8×A100** (1/10× cost of DeepSeek-R1-Zero pipeline, MATH lvl 3-5).
- T4×2 feasibility: ✅ **drop-in 2-line patch on existing GRPO loop**. Same memory.
- verl support tracking: [verl issue #742](https://github.com/volcengine/verl/issues/742) — feature request, not yet merged. Patch below.
- Combo with V13: ✅ **stacks cleanly with DAPO** (Dr-GRPO is GRPO loss fix; DAPO is sampling+clip fix). Recommended: DAPO + Dr-GRPO + TruthRL ternary reward.

```python
# Dr-GRPO loss patch (replaces GRPO normalization)
def dr_grpo_advantage(rewards, group_size):
    # rewards: [B, G] tensor of group rewards
    mean = rewards.mean(dim=1, keepdim=True)
    advantages = rewards - mean              # NO std division (removes std normalization bias)
    return advantages                        # NO length normalization in loss (apply at sequence level w/o /seq_len)

# In loss:
# OLD GRPO: loss = -(advantages * log_ratio * mask).sum(-1) / mask.sum(-1)
# Dr-GRPO: loss = -(advantages * log_ratio * mask).sum(-1) / MAX_GEN_LEN  # const denom, unbiased
```

### 3. GSPO — Group Sequence Policy Optimization (Qwen Team)
- Paper: [Group Sequence Policy Optimization](https://arxiv.org/abs/2507.18071) (July 2025, Zheng et al., Alibaba Qwen Team)
- Used to train **Qwen3** family. Especially helpful for **MoE** models (Qwen3-30B-A3B, Qwen3-235B-A22B).
- Fix: importance sampling at **sequence level**, not token level. `w^GSPO = (π_θ(y|x) / π_old(y|x))^(1/|y|)`. Token-level IS noise causes GRPO collapse on MoE.
- Repo (open impl): [vivekvar-dl/GSPO-DeepSeek-R1-Distill-Qwen-1.5B](https://github.com/vivekvar-dl/GSPO-DeepSeek-R1-Distill-Qwen-1.5B)
- TRL support: ✅ **set `importance_sampling_level="sequence"` in GRPOConfig** (Unsloth + TRL). [docs](https://unsloth.ai/docs/get-started/reinforcement-learning-rl-guide/gspo-reinforcement-learning)
- Verl support: [GSPO docs](https://swift.readthedocs.io/en/latest/Instruction/GRPO/AdvancedResearch/GSPO.html)
- T4×2 feasibility: ✅ **same memory as GRPO**, 1-line config flip.
- Combo with V13: ✅ stacks with DAPO/Dr-GRPO/TruthRL. **MUST USE if V14 base = MoE (Qwen3-30B-A3B, Mixtral)**.

```python
from trl import GRPOConfig
config = GRPOConfig(
    ...
    importance_sampling_level="sequence",   # GSPO mode
    epsilon=0.2,
    epsilon_high=0.28,                      # DAPO Clip-Higher
    loss_type="dapo",                       # Token-PG + Overlong shaping
    beta=0.0,                               # no KL
)
```

### 4. PRIME — Process Reinforcement through Implicit Rewards (Tsinghua + ModelBest)
- Paper: [Process Reinforcement through Implicit Rewards](https://arxiv.org/abs/2502.01456) (Feb 2025, Cui et al.)
- Repo: [PRIME-RL/PRIME](https://github.com/PRIME-RL/PRIME) | [ImplicitPRM](https://github.com/PRIME-RL/ImplicitPRM)
- Trick: implicit PRM = train an **outcome reward model** with DPO objective, then **use the log-ratio as PER-TOKEN process reward**. No need for step-level labels.
- Benchmark: Eurus-2-7B-PRIME = **26.7% pass@1**, beats GPT-4o + Qwen2.5-Math-7B-Instruct using 230K SFT + 150K RL = **1/10 of Qwen Math data**.
- Three benefits: dense reward (per token, no value model), online PRM updates with outcome-only labels, no separate PRM training stage.
- T4×2 feasibility: ⚠️ **needs PRM checkpoint co-loaded**. With LoRA + 4-bit base + LoRA-PRM, ~10 GB. Feasible if PRM = 1.5B Qwen Math distilled. Otherwise L40S only.
- Combo with V13: ✅ **complements GRPO/DAPO** — PRIME provides token-level reward; DAPO provides token-level PG aggregation. Use PRIME reward as the `r_t` signal in DAPO loop.

### 5. REINFORCE++ — Stable Critic-Free PG (OpenRLHF)
- Paper: [REINFORCE++: Stabilizing Critic-Free Policy Optimization with Global Advantage Normalization](https://arxiv.org/abs/2501.03262) (Jan 2025, v6 in 2025)
- Repo: [OpenRLHF/OpenRLHF](https://github.com/OpenRLHF/OpenRLHF)
- Benchmark: Llama3 8B RLHF, **42h on H100 (vs 60h PPO) on 70k samples** — 30% wall-clock saving from no value network.
- Trick: PPO-clip + token-level KL + **global batch advantage normalization** (not per-prompt as in GRPO). Lower variance than GRPO when batch large.
- T4×2 feasibility: ✅ **simpler than GRPO**, no value model. Memory ≈ DPO. Ideal for T4 LoRA.
- Combo with V13: ⚠️ **slightly conflicts with GRPO** (different baseline scheme). Pick one OR run REINFORCE++ as warmup → switch to DAPO. ScaleRL paper validates REINFORCE++-baseline at scale.

```python
# OpenRLHF YAML
algorithm: reinforce_pp
advantage_normalization: global    # batch-level, not group-level
kl_coef: 0.01                      # token-level KL (helps for tiny models)
clip_eps: 0.2
```

### 6. RLOO — REINFORCE Leave-One-Out (Cohere/Ahmadian 2024)
- Paper: [Back to Basics: Revisiting REINFORCE for RLHF in LLMs](https://aclanthology.org/2024.acl-long.662.pdf) (ACL 2024, Cohere)
- TRL native: ✅ [`RLOOTrainer`](https://huggingface.co/docs/trl/en/rloo_trainer) — `from trl import RLOOTrainer`
- Trick: K rollouts per prompt; advantage = reward - mean(other K-1) → unbiased baseline, no value model. **Only 3 model copies in memory** (policy / ref / reward), vs PPO's 4.
- Benchmark: outperforms PPO on TL;DR + HH-RLHF on Pythia-1B/2.8B/6.9B.
- T4×2 feasibility: ✅ **best memory profile after DPO**. Use K=2 (matches 2-GRPO) for max throughput.
- Combo with V13: ⚠️ alternative to GRPO/DAPO baseline. Use when **K is small (2-4)** and want simplicity. Stacks with TruthRL ternary reward.

```python
from trl import RLOOTrainer, RLOOConfig
config = RLOOConfig(
    num_generations=2,              # 2-GRPO equivalent (paper 2510.00977)
    rloo_k=2,                       # leave-one-out baseline
    cliprange=0.2,
    kl_coef=0.0,
    learning_rate=5e-7,
    per_device_train_batch_size=2,  # T4 16GB limit
    gradient_accumulation_steps=8,
)
```

### 7. 2-GRPO — Minimal-Group GRPO (Sept 2025)
- Paper: [It Takes Two: Your GRPO Is Secretly DPO](https://arxiv.org/html/2510.00977v1) (Sept 2025)
- Insight: **group_size=2 GRPO = unbiased gradient = essentially DPO with sampling**. Generation phase is up to **70% of training time** — 2-GRPO = max throughput.
- Performance: matches GRPO with k=8 across multiple reasoning datasets, **3-4× faster wall-clock**.
- T4×2 feasibility: ✅ — direct GRPO config flip (`num_generations=2`). Saves 4× rollout memory.
- Combo with V13: ✅ stacks with DAPO + Dr-GRPO + TruthRL. **Recommended default for T4×2**.

---

## TIER A — RL Recipes Worth Mining for Hyperparameters / Curriculum

### 8. Open-Reasoner-Zero (ORZ) — Vanilla PPO Beats DAPO Recipe
- Paper: [Open-Reasoner-Zero: An Open Source Approach to Scaling Up RL on the Base Model](https://arxiv.org/abs/2503.24290) (March 2025, StepFun) — **NeurIPS 2025**
- Repo: [Open-Reasoner-Zero/Open-Reasoner-Zero](https://github.com/Open-Reasoner-Zero/Open-Reasoner-Zero)
- Recipe: **vanilla PPO + GAE (λ=1, γ=1) + rule-based reward + NO KL**. Same Qwen2.5-32B base as DeepSeek-R1-Zero, **1/10 training steps**, beats on AIME24/MATH500/GPQA-Diamond.
- Models open: ORZ-0.5B, 1.5B, 7B, 14B, 32B + ORZ-R1-Distill-Qwen-14B (Jun 2025) beats DeepSeek-R1-Distill-Qwen-32B.
- Lesson: λ=1, γ=1, **no KL** is robust — V14 should adopt these defaults.
- Combo: serves as ABLATION BASELINE — start V14 trainer here, layer DAPO/Dr-GRPO/GSPO on top.

### 9. TÜLU 3 RLVR — Allen AI Open Recipe
- Paper: [Tulu 3: Pushing Frontiers in Open Language Model Post-Training](https://arxiv.org/abs/2411.15124) (v3, Jan 2025)
- Repo: [allenai/open-instruct](https://github.com/allenai/open-instruct) — full SFT / DPO / RLVR code
- 4 stages: (1) prompt curation, (2) SFT, (3) on+off-policy DPO, (4) **RLVR** = PPO with **verifier-as-reward** (GSM8K answer match, IFEval format check, math expression eq).
- Model: Tulu 3 405B beats DeepSeek V3 + GPT-4o on safety+math benchmarks (Jan 2025).
- Lesson: **DPO BEFORE RLVR** — DPO smooths out generation distribution, RLVR sharpens on verifiable tasks.
- Combo with V13: ✅ TÜLU 3 stage order is the canonical pipeline. V14 follows: SFT → ORPO/KTO → DPO+SimPO → RLVR (DAPO+Dr-GRPO+TruthRL).

### 10. Nemotron RLVR — NVIDIA Multi-Environment GRPO
- Doc: [Nemotron Super3 RLVR](https://docs.nvidia.com/nemotron/nightly/nemotron/super3/rl/rlvr.html) (Dec 2025)
- Paper: [Nemotron 3 Nano Technical Report](https://research.nvidia.com/labs/nemotron/files/NVIDIA-Nemotron-3-Nano-Technical-Report.pdf) (Dec 2025)
- Repo: [NVIDIA-NeMo/Gym](https://github.com/NVIDIA-NeMo/Gym) (RL environments)
- Recipe: **synchronous GRPO across 21 environments + 37 datasets simultaneously** — math, code, STEM, safety, chat, IFEval, long-context, puzzles, agentic.
- Lesson: **multi-env RLVR > single-env** — uniform improvement, less overfitting to one benchmark, better real agentic transfer.
- Combo with V13: ✅ V14 should add ≥3 environments (math + code + IFEval) to existing GRPO loop. NeMo Gym is open-source — pluggable.

### 11. AceReason-Math RL — NVIDIA Staged Math→Code (May 2025)
- Paper: [AceReason-Nemotron 1.0](https://arxiv.org/html/2505.16400v3) + [1.1: SFT and RL Synergy](https://arxiv.org/pdf/2506.13284) (May/Jun 2025)
- Models: [AceReason-Nemotron-7B/14B](https://huggingface.co/nvidia/AceReason-Nemotron-7B)
- Recipe: **strict on-policy GRPO**, **math RL → code RL** sequential stages. Math training boosts BOTH math AND code benchmarks.
- Benchmark: AceReason-Nemotron-7B = +14.5 / +14.6 on AIME24/25, +14.2 / +8.0 on LiveCodeBench v5/v6 over SFT baseline.
- Lesson: data difficulty curation (filter prompts where rollouts are not 100% pass and not 100% fail) + answer accuracy verification = critical.
- Combo with V13: ✅ V14 should **stage math → code** in DAPO loop, not mixed batch.

### 12. Skywork-OR1 — Open RL for Math + Code (Apr 2025)
- Paper: [Skywork Open Reasoner 1 Technical Report](https://arxiv.org/html/2505.22312v1) (May 2025)
- Repo: [SkyworkAI/Skywork-OR1](https://github.com/SkyworkAI/Skywork-OR1)
- Models: Skywork-OR1-Math-7B, OR1-32B-Preview, OR1-7B (May 2025)
- Recipe: large-scale rule-based RL on top of DeepSeek-R1-Distill-Qwen-7B/32B. Released **Skywork-OR1-RL-Data** (training set).
- Lesson: **dataset transparency** — they released exact RL prompts. Mine for V14 prompt pool.
- Combo with V13: 📥 **add Skywork-OR1-RL-Data to V14 RL prompt pool** (free, CC-BY).

### 13. AlphaProof — Test-Time RL for Lean Math (DeepMind)
- Paper: [Olympiad-level formal mathematical reasoning with reinforcement learning](https://www.nature.com/articles/s41586-025-09833-y) (Nature, Nov 2025)
- Method: **AlphaZero-style RL inside Lean theorem prover at test-time** — self-play on candidate proofs, formal verification = ground truth reward. Solved IMO 2024 P1, P2, P6 → silver medal.
- Lesson: **verifier-as-environment + test-time RL** — applicable to coding (compiler/test as verifier).
- Combo with V13: ⚠️ Lean integration heavy. V14 alternative: **pytest-as-verifier RL** for code generation (similar pattern, lighter).

---

## TIER B — Preference Optimization Beyond DPO/ORPO/KTO

### 14. SimPO — Reference-Free DPO (Princeton, NeurIPS 2024)
- Paper: [SimPO: Simple Preference Optimization with a Reference-Free Reward](https://arxiv.org/abs/2405.14734)
- Repo: [princeton-nlp/SimPO](https://github.com/princeton-nlp/SimPO)
- Benchmark: **+6.4 on AlpacaEval 2 LC, +7.5 on Arena-Hard vs DPO** on Llama3-8B-Instruct.
- Trick: implicit reward = **average log-prob** of sequence (length-normalized). NO reference model in loss → less memory.
- T4×2 feasibility: ✅ — **lighter than DPO** (no ref model load). 1-line TRL flip.
- Combo with V13: ✅ **replace one DPO stage with SimPO** in V13 pipeline. Or stack: ORPO → SimPO → DAPO.

```python
from trl import DPOConfig
# SimPO via TRL DPOTrainer
config = DPOConfig(
    loss_type="simpo",
    simpo_gamma=1.0,        # target reward margin
    beta=2.0,               # SimPO beta (different scale from DPO)
)
```

### 15. SPPO — Self-Play Preference Optimization (UCLA, Jun 2024)
- Paper: [Self-Play Preference Optimization for Language Model Alignment](https://arxiv.org/html/2405.00675) (ICLR 2025)
- Verl recipe: [SPPO docs](https://verl.readthedocs.io/en/latest/algo/sppo.html)
- Benchmark: Llama-3-8B-Instruct → **38.77% LC win rate vs GPT-4-Turbo** on AlpacaEval2 (3 self-play iters).
- Trick: 3 iterations of self-play, model generates own preference pairs, judge = preference model (PairRM).
- Combo with V13: ✅ — **add as iter 4-6 after V13 pipeline finishes**. Each SPPO iter = +5-10% on AlpacaEval LC.

### 16. SPIN — Self-Play Fine-Tuning (UCLA, Jan 2024)
- Paper: [Self-Play Fine-Tuning Converts Weak Language Models to Strong](https://arxiv.org/abs/2401.01335)
- Repo: [uclaml/SPIN](https://github.com/uclaml/SPIN)
- Verl recipe: [SPIN docs](https://verl.readthedocs.io/en/latest/algo/spin.html)
- Trick: SPIN loss = DPO loss between current model output (rejected) vs SFT data (chosen). Equivalent to DPO when logistic loss.
- Combo with V13: ✅ — **SPIN iter 0 ≈ DPO with 62k extra data**. Use SPIN if no preference labels available, only SFT data.

### 17. Step-DPO + Full-Step-DPO — Per-Step Preference (Jun 2024 / Feb 2025)
- Step-DPO: [arxiv:2406.18629](https://arxiv.org/abs/2406.18629) — preference pairs at REASONING-STEP level (not full response). Optimizes only one mistaken step.
- Full-Step-DPO: [arxiv:2502.14356](https://arxiv.org/abs/2502.14356) — optimize ALL steps with stepwise loss → +2-3% on MATH over Step-DPO.
- Combo with V13: ✅ — **complementary to PRIME**. Step-DPO uses explicit step labels; PRIME uses implicit token rewards. Use Step-DPO if PRM800K-style labeled data available.

### 18. sDPO — Stepwise/Curriculum DPO (Apr 2024)
- Paper: [sDPO: Don't Use Your Data All at Once](https://arxiv.org/html/2403.19270)
- Trick: partition preference data into chunks; DPO on chunk_i, then use trained model as **reference for chunk_{i+1}**. Tighter lower bound each step.
- Combo with V13: ✅ — wraps existing DPO/ORPO/KTO stages. Free improvement, only data scheduling change.

### 19. DPOP / DPO-Positive (Smaug, Feb 2024)
- Paper: [Smaug: Fixing failure modes of preference optimisation with DPO-Positive](https://arxiv.org/abs/2402.13228)
- Trick: add **penalty when chosen probability falls BELOW reference**. Fixes DPO's "preferred-likelihood collapse" on small-edit-distance pairs (e.g., math where chosen and rejected differ by 1 number).
- Combo with V13: ✅ — **critical for V13 Mask-DPO + math data**. Replace standard DPO with DPOP for any pair where edit-distance < 30 tokens.

### 20. NPO / SimNPO — Negative Preference Optimization (Unlearning)
- NPO: [arxiv:2404.05868](https://arxiv.org/abs/2404.05868) (April 2024) — DPO without positive samples = effective unlearning.
- SimNPO: [arxiv:2410.07163](https://arxiv.org/abs/2410.07163) (Oct 2024, NeurIPS 2025) — drops reference model from NPO.
- Repo: [OPTML-Group/Unlearn-Simple](https://github.com/optml-group/unlearn-simple)
- Use case: V14 needs to **unlearn jailbreaks / hallucinations / proprietary code patterns**.
- Combo with V13: ✅ — adds **safety-unlearn stage** AFTER alignment. Combats reward hacking in TruthRL.

### 21. NLHF — Nash Learning from Human Feedback
- Paper: [Nash Learning from Human Feedback](https://arxiv.org/abs/2312.00886) (Munos et al., DeepMind, ICML 2024)
- Trick: **pairwise preference model** (not scalar reward). Learn policy = Nash equilibrium of preference game (Nash-MD = mirror descent).
- Newer: [Multiplayer Nash Preference Optimization](https://arxiv.org/html/2509.23102) (Sept 2025) — non-transitive preferences.
- Combo with V13: 🔬 **research direction** — better handle non-transitive human prefs (A>B, B>C, C>A). Heavy implementation. Park for V15.

### 22. KTO / Meta-Rewarding revisits
- Federated KTO: [arxiv:2502.14187](https://arxiv.org/abs/2502.14187) (Feb 2025) — KTO outperforms DPO in federated settings.
- Meta-Rewarding: [arxiv:2407.19594](https://arxiv.org/html/2407.19594v2) — model judges own judgements. Llama-3-8B-Instruct: 22.9% → **39.4%** AlpacaEval2 LC. Self-Rewarding [arxiv:2401.10020].
- Combo with V13: ✅ — Meta-Rewarding adds 3rd iter on top of V13 self-rewarding loop. Pure free improvement if compute available.

---

## TIER C — Multi-Agent / Curiosity / Adversarial RL

### 23. MARTI — Multi-Agent RL Training (Tsinghua C3I)
- Paper: [MARTI: A Framework for Multi-Agent LLM Systems Reinforced Training and Inference](https://openreview.net/forum?id=E7jZqo0A50)
- Repo: [TsinghuaC3I/MARTI](https://github.com/TsinghuaC3I/MARTI) — based on OpenRLHF + GSPO loss + TIS correction
- MARTI-v2: tree-search-augmented multi-turn RL for code reasoning
- Benchmark: MARTI-trained Qwen2.5-3B beats single-agent RL, AIME 66.7 with TTRL + multi-agent debate.
- Combo with V13: 🔬 V14 if Surrogate-1 is multi-agent (planner / coder / verifier) → MARTI is the framework.

### 24. MARFT — Multi-Agent Reinforcement Fine-Tuning
- Paper: [MARFT: Multi-Agent Reinforcement Fine-Tuning](https://arxiv.org/abs/2504.16129) (April 2025)
- Trick: addresses naive single-agent RFT → multi-agent (unstable training, inactive agents, comm inefficiency).

### 25. MA-RLHF — Macro-Action RLHF (Baidu, ICLR 2025)
- Paper: [MA-RLHF](https://arxiv.org/html/2410.02743v2) — credit-assigns over **token sequences (macro actions)** not per-token. **1.7-2× faster** training, more stable PG estimates.
- Repo: [ernie-research/MA-RLHF](https://github.com/ernie-research/MA-RLHF)
- Combo with V13: ✅ alternative to DAPO Token-PG aggregation when sequences > 4k tokens (long CoT).

### 26. CDE — Curiosity-Driven Exploration for RLVR (Sept 2025)
- Paper: [CDE: Curiosity-Driven Exploration for Efficient RL in LLMs](https://arxiv.org/abs/2509.09675)
- Trick: actor curiosity = **own response perplexity**, critic curiosity = **multi-head value variance**. Adds both as exploration bonus.
- Benchmark: **+3 points over RLVR baseline on AIME** with GRPO/PPO.
- Combo with V13: ✅ small additive bonus to TruthRL reward. Free improvement.

### 27. Curiosity-Driven Red Teaming (ICLR 2024)
- Repo: [Improbable-AI/curiosity_redteam](https://github.com/Improbable-AI/curiosity_redteam)
- Trick: novelty-bonus RL → diverse jailbreak generation → train target model to refuse → safer.
- Newer: [Diverse and Effective Red Teaming with Auto-generated Rewards](https://cdn.openai.com/papers/diverse-and-effective-red-teaming.pdf) (OpenAI 2025) — RL multi-step diversity reward.
- Combo with V13: ✅ — adds **Red-Team-as-RL safety stage** before deployment. Detects + patches own vulns.

### 28. Federated RLHF (FedRLHF, AFedPG)
- AFedPG: [Asynchronous Federated Policy Gradient](https://arxiv.org/pdf/2404.08003) (ICLR 2025) — linear speedup w.r.t. # agents.
- FedRLHF: [arxiv:2412.15538](https://arxiv.org/abs/2412.15538) — privacy-preserving personalized RLHF.
- Towards Federated RLHF: [arxiv:2407.03038](https://arxiv.org/abs/2407.03038) — aggregated client preferences.
- Combo with V13: 🔬 only if Surrogate-1 trained across multiple users with private prefs. Out of scope for current single-trainer setup.

### 29. CodePRM — Execution-Feedback PRM for Code (ACL 2025)
- Paper: [CodePRM: Execution Feedback-enhanced Process Reward Model](https://aclanthology.org/2025.findings-acl.428.pdf)
- Trick: collect tree-search rollouts, label by **average pass rate at each step**. PRM = predict pass-rate, used as process reward + GVR (Generate-Verify-Refine) inference pipeline.
- Combo with V13: ✅ — **for V14 code RL stage**, replace outcome-only reward with CodePRM step-wise reward. Stacks with DAPO+TruthRL+PRIME.

### 30. CoH — Chain of Hindsight (Berkeley, ICLR 2024)
- Paper: [Chain of Hindsight aligns Language Models with Feedback](https://arxiv.org/abs/2302.02676)
- Repo: [haoliuhl/chain-of-hindsight](https://github.com/haoliuhl/chain-of-hindsight)
- Trick: condition on past failures + corrections in-context, treat as standard SFT. **Beats RLHF on summarization + dialogue**.
- Combo with V13: ✅ — **add as preprocessing step** for SFT data. Augment each example with sibling generations + feedback chain. Free improvement.

---

## TIER D — Inverse RL / Constitutional / Anthropic Latest

### 31. Constitutional AI v2 / Claude New Constitution (Jan 2026)
- Anthropic blog: [Claude's new constitution](https://www.anthropic.com/news/claude-new-constitution) (Jan 21, 2026)
- 84-page foundational doc replacing list-of-principles approach. **AI must understand WHY**, not just what to do.
- Collective CAI: [Collective Constitutional AI](https://www.anthropic.com/research/collective-constitutional-ai-aligning-a-language-model-with-public-input) — 1k Americans drafted public constitution.
- Combo with V13: ✅ — V13 already has CAI v1/v2. V14: **add reasoning-trace-grounded critiques** to CAI critique-revise loop (Claude pattern).

### 32. RLAIF v3 / Critique-then-Reward
- Reference: [Self-Generated Critiques Boost Reward Modeling for LLMs](https://aclanthology.org/2025.naacl-long.573.pdf) (NAACL 2025)
- Trick: model generates **critique BEFORE reward** → richer signal than scalar. Fits TÜLU 3 + CAI pattern.

### 33. Inverse RL for Code
- Indirect: training reward model from successful code patterns = inverse RL. Combine with RLHF reward.
- Practical implementation: [Awesome-RLVR](https://github.com/opendilab/awesome-RLVR) survey.

---

## Public Preference Datasets to ADD in V14 (beyond hh-rlhf, oasst1, prm800k)

| Dataset | Size | License | Best For |
|---------|------|---------|----------|
| [HelpSteer3-Preference](https://huggingface.co/datasets/nvidia/HelpSteer3) | 40,476 pairs | CC-BY-4.0 | General+STEM+Code+Multilingual (12 langs); RM-Bench 82.4%, JudgeBench 73.7% |
| [Skywork-Reward-V2 / SynPref-40M](https://github.com/SkyworkAI/Skywork-Reward-V2) | 26M curated (40M raw) | Open | SOTA on Reward Bench v1/v2, PPE, RMB, RM-Bench, JudgeBench (Jul 2025) |
| [openbmb/UltraInteract_pair](https://huggingface.co/datasets/openbmb/UltraInteract_pair) | 219k pairs (86k instr) | MIT | Multi-step reasoning preference trees, math+code+logic |
| [open-thoughts/OpenThoughts3-1.2M](https://github.com/open-thoughts/open-thoughts) | 1.2M | Apache 2.0 | 850k math + 250k code + 100k science (#1 trending HF Jun 2025) |
| [allenai/tulu-2.5-preference-data](https://huggingface.co/datasets/allenai/tulu-2.5-preference-data) | mix | ODC-BY | Best general-purpose DPO dataset (paper 2511.10985 ranked #1 across Llama+Qwen) |
| [magpie-align/magpie](https://github.com/magpie-align/magpie) | varies | Apache 2.0 | ICLR 2025; on-the-fly synthesis from instruct LLMs (no human prompts needed) |
| Skywork-OR1-RL-Data | ~30k+ | Open | Math+code RL prompts — plug into V14 GRPO loop |
| Nemotron Post-training Datasets | varies | Open | RLVR multi-env mix (math, code, IFEval, safety, agentic) — NeMo Gym compatible |
| [PRIME-RL/Eurus-2-RL-Data](https://huggingface.co/PRIME-RL) | ~150k RL | MIT | Used by Eurus-2-7B-PRIME (beats GPT-4o math) |

---

## V14 Stack Decision Matrix — STACK vs CONFLICT

| Method | Stacks With V13? | Conflicts With | Action |
|--------|-----------------|----------------|--------|
| **Dr-GRPO** | ✅ DAPO + TruthRL | none | **MERGE** — 2-line patch |
| **GSPO** | ✅ DAPO + TruthRL | token-level GRPO | **MERGE** if MoE base; else flag-flip |
| **DAPO Clip-Higher** | already in V13 | – | – |
| **PRIME** | ✅ DAPO (process reward → DAPO advantage) | needs co-loaded PRM | **MERGE** if L40S |
| **REINFORCE++** | ⚠️ alt path to GRPO | GRPO | **WARMUP only** (3 epochs), then DAPO |
| **RLOO** | ⚠️ alt path | GRPO/DAPO | **NOT in main pipeline** — keep for ablation |
| **2-GRPO** | ✅ all | – | **MERGE** — set num_generations=2 |
| **VAPO** | ❌ value-model PPO branch | GRPO/DAPO/RLOO | **SEPARATE BRANCH** for L40S only |
| **SimPO** | ✅ replaces DPO stage | DPO | **MERGE** — replace 1 DPO stage |
| **SPPO/SPIN** | ✅ post-pipeline iters | – | **APPEND** — 3 self-play iters |
| **Step-DPO / Full-Step-DPO** | ✅ math | DPO | **MERGE** for math stage |
| **DPOP** | ✅ replaces DPO | DPO | **MERGE** if edit-distance small |
| **NPO/SimNPO** | ✅ unlearn stage | – | **APPEND** — safety+unlearn last |
| **CodePRM** | ✅ code stage | outcome-only reward | **MERGE** for code stage |
| **MA-RLHF** | ⚠️ alt to Token-PG | DAPO Token-PG | **EXPERIMENT** for >4k seq |
| **CDE Curiosity** | ✅ + reward | – | **MERGE** small bonus |
| **Auto Red-Team RL** | ✅ safety stage | – | **APPEND** — pre-deploy |
| **CoH** | ✅ SFT prep | – | **MERGE** — augment SFT data |
| **Meta-Rewarding** | ✅ post-SPPO | – | **APPEND** if compute |
| **NLHF / Multiplayer Nash** | 🔬 research | many | **PARK V15** |
| **MARTI multi-agent** | 🔬 if MAS | single-agent | **PARK V15** |
| **Federated RL** | 🔬 not single-trainer | – | **PARK** |
| **AlphaProof Lean RL** | 🔬 specialized | – | **PARK** unless adding Lean env |
| **VAPO Length-Adaptive GAE** | ❌ on PPO branch only | GRPO advantage | **EXPERIMENT** L40S only |

---

## Recommended V14 Pipeline (T4×2 default, L40S option)

```
Phase 0: SFT (Qwen3-7B/14B base + LoRA)
         + CoH augmentation [METHOD 30]
         + OpenThoughts3 1.2M filtered

Phase 1: ORPO (V13 carry, monolithic SFT+pref)
         + Magpie synth data [METHOD 16 dataset]

Phase 2: DPO/SimPO/Step-DPO mix
         - SimPO replaces 1 DPO stage [METHOD 14]
         - Step-DPO for math pairs [METHOD 17]
         - DPOP for small-edit-distance pairs [METHOD 19]
         - Curriculum sDPO scheduling [METHOD 18]

Phase 3: KTO + Mask-DPO + F-DPO (V13 carry)

Phase 4: RLVR with verifier (math + code + IFEval)
         Algorithm = GRPO + DAPO + Dr-GRPO + 2-GRPO + GSPO + TruthRL + Constitutional
           - DAPO: Clip-Higher (0.20/0.28), Dyn Sampling, Token-PG, Overlong Shaping (V13)
           - Dr-GRPO: remove length+std normalization [METHOD 2]
           - GSPO: importance_sampling_level="sequence" if MoE [METHOD 3]
           - 2-GRPO: num_generations=2 [METHOD 7]
           - TruthRL: ternary +1/0/-1 (V13 carry)
           - PRIME: per-token implicit reward [METHOD 4] (L40S only)
           - CodePRM: per-step pass-rate reward for code [METHOD 29]
           - CDE: curiosity bonus [METHOD 26]
           - Multi-env (NeMo Gym pattern): math + code + IFEval [METHOD 10]
         Stage order: math → code (AceReason staging [METHOD 11])

Phase 5: Self-improvement loop
         - SPPO 3 iters [METHOD 15]
         - Meta-Rewarding 1 iter [METHOD 22]

Phase 6: Safety + Unlearn + Red-Team
         - Constitutional AI v2 critique-revise (V13 + 2026 Anthropic update)
         - SimNPO unlearn jailbreaks [METHOD 20]
         - Auto Red-Team RL [METHOD 27]

Phase 7: VAPO branch (L40S only) — value-augmented alternative comparison
```

---

## Concrete TRL Config — V14 Stack on T4×2

```python
# trainer/v14_stack.py
from trl import GRPOConfig, GRPOTrainer

config = GRPOConfig(
    # base
    model_init_kwargs={"torch_dtype": "bfloat16", "load_in_4bit": True},
    learning_rate=5e-7,
    per_device_train_batch_size=1,
    gradient_accumulation_steps=16,
    max_prompt_length=2048,
    max_completion_length=4096,

    # GRPO core
    num_generations=2,                       # 2-GRPO [METHOD 7]
    importance_sampling_level="sequence",    # GSPO [METHOD 3]

    # DAPO loss family (V13 carry)
    loss_type="dapo",
    epsilon=0.20,                            # Clip-Lower
    epsilon_high=0.28,                       # Clip-Higher (DAPO)

    # No KL, no value (DAPO + ORZ finding)
    beta=0.0,

    # Dr-GRPO patch [METHOD 2] — applied via custom advantage_fn
    # (subclass GRPOTrainer, override _compute_advantages)

    # Length-Adaptive GAE [VAPO METHOD 1] — only when value head present (L40S branch)
    # gae_lam_critic=1.0, gae_lam_policy_alpha=0.05,

    # Stability
    use_vllm=True,                           # rollout speed
    temperature=1.0,
    top_p=1.0,

    # Curriculum
    num_train_epochs=3,
    save_strategy="steps",
    save_steps=500,
)


def truthrl_codeprm_cde_reward(prompt, completion, **kw):
    """Combined reward: TruthRL ternary + CodePRM step + CDE curiosity bonus."""
    # 1. TruthRL ternary (V13)
    answer_correct, idk = verify_answer(prompt, completion)
    if idk:
        truth_r = 0.0
    elif answer_correct:
        truth_r = 1.0
    else:
        truth_r = -1.0

    # 2. CodePRM step reward (only for code prompts)
    if is_code_prompt(prompt):
        step_r = codeprm_score(completion)             # avg pass rate at each reasoning step
    else:
        step_r = 0.0

    # 3. CDE curiosity bonus [METHOD 26]
    perplexity = compute_response_perplexity(completion)
    cde_bonus = min(0.1 * np.log(perplexity), 0.3)     # capped exploration bonus

    return truth_r + 0.3 * step_r + 0.1 * cde_bonus


class DrGrpoTrainer(GRPOTrainer):
    """Override advantage normalization to remove length+std bias."""
    def _compute_advantages(self, rewards, mask):
        # Dr-GRPO: subtract mean, no std division
        mean = rewards.mean(dim=1, keepdim=True)
        adv = rewards - mean
        # Loss aggregation: sum / MAX_LEN (constant), not / mask.sum()
        return adv, "max_len"


trainer = DrGrpoTrainer(
    model="Qwen/Qwen3-7B-Base",
    reward_funcs=truthrl_codeprm_cde_reward,
    train_dataset=multi_env_dataset,         # math + code + IFEval mix [METHOD 10]
    args=config,
    peft_config=LoraConfig(r=64, lora_alpha=128, target_modules="all-linear"),
)
trainer.train()
```

---

## Top 6 RL Methods Beyond V13 With Concrete Uplift

1. **Dr-GRPO** — fixes GRPO length+std bias. SOTA Qwen2.5-Math-7B in 27h on 8×A100. **Free patch (2 lines).** ✅ T4×2.
2. **GSPO** — sequence-level IS. **Trains Qwen3 family**. Critical for MoE; 1-line config. ✅ T4×2.
3. **2-GRPO** — k=2 group, **3-4× faster** wall-clock, matches k=8 quality. ✅ T4×2.
4. **PRIME** — implicit per-token process reward, beats GPT-4o math with 1/10 data. ⚠️ L40S preferred (PRM co-load).
5. **SimPO** — **+6.4 / +7.5 on AlpacaEval2 LC / Arena-Hard** vs DPO; reference-free → less memory. ✅ T4×2.
6. **VAPO** — **+10 over DAPO on AIME24** (Qwen2.5-32B). Length-Adaptive GAE alone +15. ❌ T4 (needs value head); ✅ L40S branch.

## Preference Datasets to ADD

- **HelpSteer3-Preference** (40k, multilingual, RM-Bench 82.4%) — best human-annotated.
- **Skywork-SynPref-40M / Reward-V2** (26M curated) — largest open RM training set.
- **UltraInteract_pair** (219k pairs, preference trees) — reasoning-specific.
- **OpenThoughts3-1.2M** (math+code+science) — #1 trending HF.
- **TuluDPO** (Allen AI mix) — best general DPO per study 2511.10985.
- **Skywork-OR1-RL-Data** + **Nemotron Post-training** — direct RL prompt pools.

## Stack vs Conflict (Quick Read)

- **STACK CLEANLY**: DAPO + Dr-GRPO + GSPO + 2-GRPO + TruthRL + CDE + SimPO + Step-DPO + DPOP + CoH + SimNPO + CodePRM + multi-env RLVR.
- **ALTERNATIVE BRANCHES** (pick one, don't run together): GRPO/DAPO **vs** VAPO **vs** REINFORCE++ **vs** RLOO. Default = DAPO+Dr-GRPO+2-GRPO. VAPO = ablation branch.
- **PARK FOR V15**: NLHF, Multiplayer Nash, MARTI multi-agent, Federated RL, AlphaProof Lean RL.
- **ORDER MATTERS**: SFT → ORPO → DPO/SimPO/Step-DPO/DPOP → KTO/Mask-DPO/F-DPO → RLVR (DAPO+Dr-GRPO+TruthRL+PRIME+CodePRM) → SPPO 3iters → Meta-Rewarding → CAI critique-revise → SimNPO unlearn → Auto Red-Team RL.

---

## See Also

- [[v13-frontier-capability]] — V13 baseline
- [[v13-frontier-efficiency]] — efficient training
- [[anti-hallucination-correctness-2026]] — TruthRL + RLCR context
- [[training-tooling-2026-Q2]] — TRL/verl/OpenRLHF tooling

---
title: "V14+ Reasoning Frontier: Training Techniques for Surrogate-1"
date: 2026-05-01
tags: [surrogate-1, v14, reasoning, RL, GRPO, PRM, CoT, distillation, frontier-2026]
status: research-complete
context: "Surrogate-1 V13 has s1K-1.1 + Bespoke-Stratos. V14+ needs frontier reasoning RL recipe."
related:
  - [[training-tooling-2026-Q2]]
  - [[v13-frontier-capability]]
  - [[v13-frontier-efficiency]]
  - [[anti-hallucination-correctness-2026]]
  - [[opensource-releases-2026-Q2]]
---

# V14+ Reasoning Frontier — Smarter-with-Less

> **Goal**: training-side techniques for thinking-through-tokens reasoning models. Max capability per parameter on T4×2 (Kaggle) or Civo H200.
> **Coverage**: 2024-Q4 through 2026-Q2. Excludes pure inference-time tricks unless they imply training changes.
> **V13 baseline**: SFT on s1K-1.1 (1k traces) + Bespoke-Stratos-17k. No RL, no PRM, no length control.
> **V14 target**: add RL stage + PRM + length control + reasoning-distillation diversification.

---

## TL;DR Top 8 Reasoning Techniques to Add (Priority Order)

| # | Technique | Source | Lift on AIME-24 | Compute |
|---|-----------|--------|-----------------|---------|
| 1 | **GRPO + rule-based verifiable rewards** (R1 recipe) | DeepSeek-R1, QwQ-32B | +20-30 pp from SFT base | Civo H200 (4-8 GPU) |
| 2 | **PRIME (implicit PRM, online updates)** | PRIME-RL/PRIME, Feb 2025 | +14.5 pp on AceReason-7B | T4×2 feasible at small scale |
| 3 | **Budget forcing (s1)** + length-controlled RL (LCPO/L1) | s1, L1 (Mar 2025) | 50→57 % AIME via "Wait" tokens | Inference-only initially; train with LCPO later |
| 4 | **Math-Shepherd-style PRM as verifier in RL loop** | ThinkPRM (Apr 2025) | +8 % over discriminative PRM | Civo |
| 5 | **MCTS-as-data generator (rStar-Math, ReST-MCTS\*)** | rStar-Math (Jan 2025) | 58.8→90.0 % MATH for Qwen-7B | Civo (4 rounds × millions samples) |
| 6 | **Self-consistency-as-distillation-data** (sample 32 → vote → SFT) | DAPO, Magistral | Free with existing samples | T4×2 |
| 7 | **Two-stage RL: math-only → code-only curriculum** | AceReason-Nemotron (May 2025) | +14.5 % AIME, +14.2 % LCB | Civo |
| 8 | **DAPO/Dr.GRPO objective fixes (length-bias, dynamic sampling, dual-clip)** | DAPO, Dr.GRPO (2025) | +3 pp AIME at half steps | Drop-in replacement for GRPO |

---

## Strip-CoT vs Faithful-CoT — Reconciliation

**Anthropic's "Reasoning Models Don't Always Say What They Think" (May 2025)** found Claude 3.7 Sonnet only verbalizes hint usage 25 % of the time, DeepSeek-R1 ~39 %. Outcome-based RL **plateaus** faithfulness without saturating.

**Two camps**:
- **Strip-CoT (Anthropic invariant)**: don't depend on CoT for monitoring; treat the visible thinking as a *useful but unfaithful* artifact. Train without character-shaping the thoughts.
- **Faithful-CoT (DeepSeek pattern)**: train with PRM that rewards each step being grounded; punish skipped/hallucinated steps. PRM800K, ProcessBench, ThinkPRM force faithful traces.

**For Surrogate-1 V14+**: HYBRID
1. **Train with PRM-shaped rewards** (PRIME or Math-Shepherd) so the *content* of reasoning is grounded against verifiable outcomes — gain reliability.
2. **Don't character-train the thinking** (Anthropic move) — keep the cognitive scratchpad raw.
3. **Add a "self-audit" final step** in the policy that re-checks the hidden chain against the answer (CoVe-style), but don't penalize the model for unfaithful intermediate tokens — only for an unfaithful *final answer*.
4. **Monitor faithfulness as a metric**, not a gate. Log the divergence rate; alarm if it climbs (signals reward hacking).

This dodges the over-optimization trap (interconnects.ai post on o3 over-optimization) while keeping the verification signal that DeepSeek R1 proves works.

---

## Public Reasoning Datasets We Don't Yet Have (V13 only had s1K-1.1 + Bespoke-Stratos-17k)

Add-list ranked by quality:

| Dataset | Size | Source | Domain | License |
|---------|------|--------|--------|---------|
| **OpenThoughts-114k** | 114k | DeepSeek-R1 distill | math 89k / code 20k / sci 4k / puzzle 1k | open |
| **Mixture-of-Thoughts (HF Open-R1 Step 1)** | 350k | DeepSeek-R1-distill, verified | math + code + sci | open |
| **OpenR1-Math-220k** | 220k | DeepSeek-R1 + verifier | math (NuminaMath) | open |
| **Skywork-OR1-RL-Data** | 110k math + 14k code | curated | verifiable RL prompts | open |
| **PRM800K** | 800k step-labels | OpenAI 2023 (still gold) | math reasoning steps | open |
| **NuminaMath-1.5** / **AIME 1983-2024** | 860k / 933 | competitions | math | open |
| **DeepSeek-Distillation collection (1.4M)** | 1.4M | community-curated R1 distills | mixed | open |
| **LIMO (817 examples)** | 817 | hand-curated | "less is more" elicitation | open |
| **LIMR (1024)** | 1024 | RL-curated subset | math | open |
| **OpenThinker3 / OpenThinker-32B data** | scaled OpenThoughts | math+code+sci+puzzle | open |
| **InternBootcamp / InternThinker-GO** | task-specific | InternLM team | Go + math + verifiable | open |
| **rStar-Math 747k math problems** | 747k | self-evolved MCTS | math, step-verified | open code |

V14 plan: add **OpenThoughts-114k** (free, easy SFT) + **Skywork-OR1-RL-Data** for the RL stage. Both Apache-2.0 / open licenses.

---

## Per-Model Training Recipe Catalog

### 1. DeepSeek-R1 / R1-Zero (Jan 2025, Nature 2025-09)
- **URL**: https://arxiv.org/abs/2501.12948 ; https://www.nature.com/articles/s41586-025-09422-z
- **Recipe**: GRPO with rule-based outcome rewards; 4-stage pipeline: cold-start SFT → RL → SFT2 → RL2. Hyperparams: lr 3e-6, KL coef 0.001, 16 rollouts/prompt, max 32k tokens.
- **Key insight**: pure RL without SFT (R1-Zero) develops reasoning emergently but suffers language mixing and readability — fixed by adding cold-start SFT.
- **Distill**: 6 dense students (Qwen 1.5/7/14/32B + Llama 8/70B), SFT-only on 800k R1-generated samples; **no RL stage on students**. Qwen-32B-distill beats o1-mini.
- **Benchmark lift**: AIME 2024 79.8 % (R1), MATH-500 97.3 %, GPQA-D 71.5 %.
- **For V14**: this is the dominant recipe. **Replicate via Open-R1 (HuggingFace) on Civo**. T4×2 only feasible for distill SFT, not full RL.

### 2. DeepSeek-R2 (Q1 2026 expected)
- Delayed by Huawei Ascend training instability (per Yahoo Finance / Stratnews).
- Adds **Self-Principled Critique Tuning (SPCT)** — model evaluates own outputs against principles. Multimodal, agent-capable.
- For V14: monitor release; SPCT pattern (self-critique loop with explicit principles) is implementable in V14.5.

### 3. OpenAI o1 / o3 / o4-mini
- **URL**: https://openai.com/index/learning-to-reason-with-llms/ ; https://openai.com/index/introducing-o3-and-o4-mini/
- **Public training info**: "large-scale RL teaches productive thinking; data-efficient." o3 makes 20 % fewer major errors than o1; o3-mini has 3 reasoning-effort tiers.
- **Test-time compute**: more thinking → better results, both train-time and test-time scale.
- **For V14**: nothing more than the public abstract. Pattern (effort tiers) is replicable via budget-forcing / LCPO at training time.

### 4. QwQ-32B (Mar 2025, Alibaba)
- **URL**: https://qwenlm.github.io/blog/qwq-32b/ ; https://huggingface.co/Qwen/QwQ-32B
- **Recipe**: 2-stage RL on Qwen-2.5-32B base. Stage 1: outcome-based rewards via code-interpreter / math-solver verifier. Stage 2: general-capability RL with rule-based + reward models.
- **Result**: matches DeepSeek-R1 (671B) at 32B dense.
- **For V14**: two-stage RL is the QwQ pattern. **Stage 1 = math+code verifier RL → Stage 2 = general SFT/RL** to avoid catastrophic forgetting.

### 5. Magistral (Mistral, Jun 2025)
- **URL**: https://arxiv.org/abs/2506.10910 (arXiv 2506.10910)
- **Recipe**: RLVR on text-only data from Mistral Medium 3 base; **no distillation** from prior reasoning models. Asynchronous online RL infrastructure (continuous generator updates).
- **Result**: ~50 % AIME-24 lift (pass@1), 90 % AIME-24 with majority voting. Maintains multimodal/instruction-following capabilities even though RL was text-only.
- **For V14**: Magistral proves **RL on text alone preserves multimodal** — important if we ever go vision. The async-online-RL infra is overkill for us; not feasible on T4×2.

### 6. Phi-4-reasoning / Phi-4-reasoning-plus / Phi-4-mini-reasoning (Apr-May 2025, Microsoft)
- **URL**: https://www.microsoft.com/en-us/research/wp-content/uploads/2025/04/phi_4_reasoning.pdf ; https://huggingface.co/microsoft/Phi-4-mini-reasoning
- **Recipe**: data-centric. SFT on diverse prompts + reasoning demonstrations from o3-mini (frontier teacher). Phi-4-reasoning-plus adds RL on top → longer traces.
- **Sizes**: 14B (reasoning + reasoning-plus), 3.8B (mini-reasoning).
- **Result**: outperforms o1-mini and DeepSeek-R1-Distill-Llama-70B on math/sci PhD-level. Comparable to full DeepSeek-R1 (671B) on AIME 2025 — at 14B.
- **For V14**: Phi-4 distill-from-o3-mini pattern is a strong proof that **frontier-teacher SFT alone gets 90 %+ of full RL recipe** at much lower cost. **T4×2 feasible**.

### 7. GLM-4.5 / GLM-4.1V-Thinking (Aug 2025, Zhipu)
- **URL**: https://arxiv.org/html/2507.01006v5 ; https://github.com/zai-org/GLM-V
- **Recipe**: 22T-token corpus (7T code+reasoning) + RLCS (RL with Curriculum Sampling). MoE architecture (355B / 32B active in GLM-4.5; 106B / 12B in Air). **Two modes baked in**: thinking mode + non-thinking mode for fast responses.
- **Infra**: in-house slime RL.
- **For V14**: dual-mode (thinking vs non-thinking switch) is very desirable. Implement as a special token + LCPO-controlled length.

### 8. rStar-Math (Jan 2025, Microsoft, ICML 2025)
- **URL**: https://huggingface.co/papers/2501.04519 ; https://github.com/microsoft/rStar
- **Recipe**: 4 rounds of self-evolution. (a) Code-augmented CoT via MCTS rollouts with Python verification on each step; (b) Process Preference Model (PPM) avoiding noisy step labels; (c) policy SLM and PPM bootstrap from scratch.
- **Result**: Qwen-2.5-Math-7B 58.8 → 90.0 % MATH; Phi3-mini-3.8B 41.4 → 86.4 %. Beats o1-preview by +4.5 / +0.9 pp.
- **For V14**: this is **the small-model recipe**. MCTS-as-data is expensive (millions samples) but each round adds independent gains. **First MCTS round on T4×2 may be feasible for math-only**; later rounds need Civo.

### 9. Open-Reasoner-Zero (Mar 2025)
- **URL**: https://arxiv.org/abs/2503.24290 ; https://github.com/Open-Reasoner-Zero/Open-Reasoner-Zero
- **Recipe**: vanilla PPO + GAE (λ=γ=1), rule-based rewards, **no KL regularization**. Same Qwen-2.5-32B base as DeepSeek-R1-Zero. Reproduces R1-Zero scaling phenomenon at **1/10 the training steps**.
- **Result**: superior to R1-Zero-Qwen-32B on AIME-2024, MATH-500, GPQA-D.
- **For V14**: most reproducible / minimalist RL recipe. **Use Open-Reasoner-Zero as our V14 RL backbone**. Smaller scales (0.5B-7B) supported.

### 10. PRIME (Process Reinforcement through Implicit Rewards, Feb 2025)
- **URL**: https://arxiv.org/abs/2502.01456 ; https://github.com/PRIME-RL/PRIME
- **Recipe**: implicit PRM trained as outcome reward model, then used as PRM during RL. Online updates from on-policy rollouts + outcome labels. Dense token-level rewards alleviate sparsity.
- **Result**: Eurus-2-7B (Qwen2.5-Math-7B base) → 26.7 % pass@1, beats GPT-4o and Qwen2.5-Math-7B-Instruct with **1/10 the data** (230k SFT + 150k RL).
- **For V14**: PRIME is the **next-step beyond pure GRPO**. No human PRM labels needed, no PRM data flywheel, scales online. **High priority for V14**.

### 11. ThinkPRM (Apr 2025) — Generative PRM
- **URL**: https://arxiv.org/abs/2504.16828
- **Recipe**: long-CoT verifier fine-tuned on **1 % of PRM800K labels** (1k synthetic chains from Math-Shepherd). Generates a verification CoT for each step.
- **Result**: beats discriminative verifiers on ProcessBench, MATH-500, AIME-24 best-of-N. +8 % on GPQA-D OOD vs full-data discriminative.
- **For V14**: use ThinkPRM as the **verifier model** in best-of-N selection or in the RL reward signal. ~1.5B parameter range — T4 fits.

### 12. AceReason-Nemotron (May/Jun 2025, NVIDIA)
- **URL**: https://arxiv.org/abs/2505.16400 ; https://huggingface.co/nvidia/AceReason-Nemotron-1.1-7B
- **Recipe**: SFT first → RL on **math-only** prompts → then RL on **code-only** prompts (curriculum). Curriculum learning with progressively longer responses. On-policy parameter updates for stability.
- **Result**: +14.5 / +14.6 % AIME 24/25, +14.2 / +8 % LiveCodeBench v5/v6 over SFT base.
- **For V14**: **Adopt the math→code curriculum**. Cleanly separates reward signals; avoids math-vs-code interference.

### 13. Skywork-OR1 (Apr-May 2025)
- **URL**: https://arxiv.org/html/2505.22312v1 ; https://github.com/SkyworkAI/Skywork-OR1
- **Recipe**: large-scale rule-based RL on top of DeepSeek-R1-Distill-Qwen 7B/32B. Custom verl fork. Investigated **entropy collapse** mitigation.
- **Result**: 32B avg accuracy 57.8 → 72.8 % (+15 pp); 7B 43.6 → 57.5 % (+13.9 pp). Beats R1 and Qwen3-32B on AIME.
- **For V14**: Skywork's data (110k math + 14k code) is open and pre-filtered for verifiability — **drop-in for our RL stage**.

### 14. VAPO (Apr 2025, ByteDance)
- **URL**: https://arxiv.org/abs/2504.05118
- **Recipe**: value-based augmented PPO with 7 modifications addressing value-model bias, heterogeneous lengths, sparse rewards. State-of-art 60.4 on AIME-24 from Qwen-32B base. Beats R1-Zero-Qwen-32B and DAPO by 10+ pp.
- **5,000 steps** to SOTA, no training crashes across multiple runs.
- **For V14**: VAPO is **value-based**, in contrast to GRPO's actor-only design. Higher complexity but more stable. Good Phase-2 V14.5 upgrade.

### 15. AlphaProof (DeepMind, Jul 2024 → Nature 2025-11)
- **URL**: https://www.nature.com/articles/s41586-025-09833-y ; https://deepmind.google/blog/ai-solves-imo-problems-at-silver-medal-level/
- **Recipe**: AlphaZero-style RL on Lean theorem prover. Auto-formalize 1M natural-language theorems → 80M Lean statements. **Test-time RL**: generate problem variants at inference and learn from them per-problem.
- **Result**: silver medal at IMO 2024 (3/5 problems).
- **For V14**: too domain-specific (Lean). **Test-time RL pattern (per-problem fine-tune at inference)** is interesting but compute-prohibitive on Civo.

### 16. Gemini Deep Think (May/Aug 2025, Google)
- **URL**: https://blog.google/products/gemini/gemini-2-5-deep-think/
- **Recipe**: novel RL + multi-sampling at training time — model both thinks longer and samples multiple answers, refines, combines.
- **For V14**: pattern matches "self-consistency at training time" (sample-then-distill the consensus). Implementable.

### 17. Quiet-STaR (Mar 2024) → V-STaR (2024) → 2026 successors
- **URL**: https://arxiv.org/abs/2403.09629 ; https://arxiv.org/abs/2203.14465 (original STaR)
- **Recipe**: model generates per-token rationales between `<startofthought>` and `<endofthought>` tokens, optimized via REINFORCE. Continued-pretraining on internet text.
- **Result**: GSM8K 5.9 → 10.9 %, CommonsenseQA 36.3 → 47.2 %, **zero-shot, no task-specific FT**.
- **V-STaR** adds a verifier component to filter rationales.
- **For V14**: Quiet-STaR is **continued-pretraining** style — orthogonal to our RL pipeline. Could be an early-stage augmentation BEFORE Bespoke-Stratos SFT.

### 18. s1: Simple test-time scaling (Jan 2025)
- **URL**: https://arxiv.org/abs/2501.19393 ; https://github.com/simplescaling/s1
- **Recipe**: 1k high-quality reasoning traces (s1K) + **budget forcing** at inference: append "Wait" to extend thinking, or force-terminate to shorten. SFT on Qwen-2.5-32B-Instruct.
- **Result**: 50 → 57 % AIME-24 from extending thinking; beats o1-preview by 27 pp on MATH/AIME.
- **For V14**: V13 already has s1K-1.1. **Add budget forcing at inference** with no extra training. Combine with LCPO/L1 for trained length control.

### 19. Bespoke-Stratos-17k (Jan 2025)
- **URL**: https://www.bespokelabs.ai/blog/bespoke-stratos-the-unreasonable-effectiveness-of-reasoning-distillation ; https://huggingface.co/datasets/bespokelabs/Bespoke-Stratos-17k
- **Recipe**: 5k code + 10k math + 1k sci/puzzle, distilled from DeepSeek-R1, GPT-4o-mini for false-negative filtering (25 → 73 % retention). Total cost: $800, 1.5 hours.
- **Result**: trained model beats Sky-T1 / o1-preview, near DeepSeek-R1-Distill-Qwen-32B with **47× fewer examples**.
- **For V14**: V13 already has it. Stack with OpenThoughts-114k for diversity.

### 20. LIMO / LIMR (Feb 2025)
- **URL**: https://arxiv.org/html/2502.03387v1 (LIMO) ; https://arxiv.org/html/2502.11886v1 (LIMR)
- **Recipe**: LIMO = hand-curated 817 traces, SFT only. LIMR = 1024 RL-selected examples on Qwen-2.5-Math-7B, **+100 % AIME relative** vs LIMO and s1.
- **Insight**: reasoning is **latent in pretrained models**; the task is *elicitation*, not *training*.
- **For V14**: confirms the s1K thesis. Add LIMO + LIMR as ultra-low-cost SFT augmentation.

### 21. L1 / LCPO (Mar 2025) — Length-Controlled Policy Optimization
- **URL**: https://arxiv.org/abs/2503.04697
- **Recipe**: RL with reward = accuracy + adherence to user-given length constraint. Model learns to satisfy "think for ≤ N tokens" prompts.
- **For V14**: **directly addresses overthinking**. Drop into our RL stage to add a "thinking budget" prompt parameter. **High priority**.

### 22. Curriculum Learning (Train Long, Think Short, Aug 2025)
- **URL**: https://arxiv.org/abs/2508.08940
- **Recipe**: GRPO with **decreasing token budget** over training — start with generous budget, tighten over time. Forces model to discover patterns first, then compress.
- **For V14**: combine with AceReason's math→code curriculum for a 2D curriculum (domain × length).

### 23. DAPO (Mar 2025, ByteDance)
- **URL**: https://arxiv.org/abs/2503.14476 ; https://self-supervised.cs.jhu.edu/fa2025/files/presentations/Why-RL-Sep16-AdvancesNLP.pdf
- **Recipe**: GRPO with 4 fixes: (a) two clip ranges (positive/negative), (b) dynamic sampling — drop batches with flat reward, (c) per-token loss not per-response, (d) length-aware loss for very long generations.
- **Result**: 50 % AIME from Qwen-32B at half the steps of R1-Zero.
- **For V14**: **drop-in replacement for vanilla GRPO**. No reason to use plain GRPO in 2026.

### 24. Dr.GRPO (2025) — Bias-Corrected GRPO
- **URL**: https://arxiv.org/pdf/2505.22257
- **Recipe**: removes the response-length normalization in advantage that caused long incorrect answers to get smaller penalties.
- **For V14**: combine with DAPO for a "GRPO++" base. ~5 lines code change vs vanilla.

### 25. ReST-MCTS\* (NeurIPS 2024)
- **URL**: https://openreview.net/forum?id=8rcFOqEud5
- **Recipe**: PRM-guided MCTS to collect high-quality reasoning traces + per-step values. Train both policy and reward model.
- **For V14**: cheaper than rStar-Math but same family. Use if rStar full pipeline is too expensive.

### 26. Open-R1 / Mixture-of-Thoughts (HuggingFace, May 2025)
- **URL**: https://huggingface.co/blog/open-r1 ; https://github.com/huggingface/open-r1
- **Recipe**: full DeepSeek-R1 reproduction. Step 1 complete: 350k verified reasoning traces (Mixture-of-Thoughts). Step 2 (RL) in progress.
- **For V14**: **the production code path**. Use Open-R1's training scripts on Civo.

### 27. DeepScaleR-1.5B (Feb 2025)
- **URL**: https://huggingface.co/agentica-org/DeepScaleR-1.5B-Preview
- **Recipe**: DeepSeek-R1-Distill-Qwen-1.5B + GRPO with **distributed RL scaling to long context**. Achieves 43.1 % AIME-24 — **beats o1-preview at 1.5B**.
- **For V14**: **the T4×2 sweet spot**. 1.5B + GRPO is the cheapest way to demonstrate frontier reasoning lift on Kaggle. Strongly recommended for V14 first iteration.

### 28. Anthropic Claude Extended Thinking (Feb 2025)
- **URL**: https://www.anthropic.com/research/visible-extended-thinking ; https://www.anthropic.com/news/visible-extended-thinking
- **Recipe**: integrated thinking/non-thinking modes in single model. **No character training on thoughts** — model has maximum leeway in thinking style. Adaptive thinking in 4.6: model decides when/how much to think based on effort level parameter.
- **CoT faithfulness paper (May 2025)**: 25 % faithfulness for Claude 3.7. Outcome RL plateaus faithfulness without saturating.
- **For V14**: **don't character-train the scratchpad** (Anthropic invariant). But do add a final-answer self-audit step to bound reward-hacking.

---

## Reasoning RL Frontier Recipe for Small Models (V14 Baseline)

```yaml
# kaggle-trainer.sh / civo-trainer.sh additions for V14
phase_0_pretraining: skip        # use existing Qwen-2.5-7B-Instruct base
phase_1_sft:
  datasets:
    - s1K-1.1                    # V13 had this
    - Bespoke-Stratos-17k        # V13 had this
    - OpenThoughts-114k          # V14 NEW
    - Mixture-of-Thoughts-350k   # V14 NEW
    - LIMO-817 + LIMR-1024       # V14 NEW (low-cost diversity)
  epochs: 2-3
  loss: standard CE on full reasoning trace
  feasibility: T4×2 OK with QLoRA, full FT needs Civo
phase_2_rl:
  algorithm: DAPO + Dr.GRPO bias-fix    # NOT vanilla GRPO
  reward:
    primary: rule-based outcome verifier (math-verify, code-runner)
    secondary: PRIME implicit PRM for token-level density
  data: Skywork-OR1-RL-Data (110k math + 14k code)
  curriculum:
    stage_a: math-only (50% steps)
    stage_b: code-only (30% steps)
    stage_c: mixed math+code+sci (20% steps)
  length_control: LCPO with budget tokens in prompt
  no_KL_regularization: true     # Open-Reasoner-Zero finding
  rollouts_per_prompt: 8-16
  max_response_length: 16384 (curriculum: start 8k, expand to 32k)
  feasibility: Civo H200 4-GPU minimum; T4×2 only feasible for 1.5B model
phase_3_distill_or_iterate:
  option_a_distill: SFT a 1.5B/3B student on phase_2 generations (Phi-4-mini pattern)
  option_b_mcts_round: rStar-Math style MCTS data generation, retrain
  option_c_sct: DeepSeek-R2's Self-Principled Critique Tuning (when public)
phase_4_verifier_train:
  model: ThinkPRM-1.5B style generative verifier
  data: Math-Shepherd labels (1k synthetic chains)
  use_at_inference: best-of-N selection + reward-guided search
  feasibility: T4×2 OK
phase_5_inference_only:
  budget_forcing: s1-style "Wait" injection
  self_consistency: 8-32 samples + ThinkPRM voting
  thinking_mode_switch: special token if dual-mode (GLM-4.5 pattern)
```

---

## Compute Feasibility Map

| Phase | T4×2 (Kaggle) | Civo H200 | Notes |
|-------|---------------|-----------|-------|
| SFT 1.5B with QLoRA on s1K + Bespoke + OpenThoughts | YES | n/a | ~6-12 hrs |
| SFT 7B with QLoRA | YES (tight) | preferred | ~24-48 hrs T4 |
| GRPO/DAPO RL on 1.5B | YES (small batch) | preferred | DeepScaleR proves it |
| GRPO/DAPO RL on 7B | NO | YES | needs ≥4 H100 / H200 |
| GRPO RL on 32B | NO | YES (4-8 GPU) | Open-Reasoner-Zero proven |
| PRIME implicit PRM training | YES at 1.5B | preferred at 7B+ | online updates |
| MCTS data generation (rStar-Math 1 round) | maybe at small scale | YES | ~1 day Civo, ~weeks T4 |
| ThinkPRM-1.5B verifier training | YES | n/a | small data, fast |
| Budget forcing inference | YES | YES | inference-only |

**V14 strategy**:
- Kaggle T4×2 → DeepScaleR-1.5B-style: SFT + GRPO RL on 1.5B, demonstrate AIME lift, ship.
- Civo H200 → 7B/32B Open-Reasoner-Zero recipe + PRIME + AceReason curriculum.

---

## Reconciling Strip-CoT vs Faithful-CoT (Detailed)

| Concern | Strip-CoT camp (Anthropic) | Faithful-CoT camp (DeepSeek/PRM) | V14 hybrid choice |
|---------|---------------------------|---------------------------------|-------------------|
| Can we trust visible thinking? | No — only 25-39 % faithful | Yes if PRM-shaped | Treat as hint, not proof |
| Should we train the thinking? | No character training | Yes — PRM rewards each step | PRM-shape *content*, don't *style* |
| Reward hacking risk | High under outcome RL | Lower with dense PRM | Use PRIME (implicit, scales) |
| Monitoring | Distrust verbalized hints | Trust step-traces | Log faithfulness as metric, not gate |
| Final answer reliability | Audit at end | Step-by-step verifier | CoVe-style final audit + PRM during RL |
| Compute cost | Cheapest (no PRM) | Higher (PRM training/data) | Implicit PRM (PRIME) — best of both |

**V14 invariant** (after research):
> "Train *what* the model reasons (PRM-shaped outcomes), not *how* it sounds while reasoning (no character training on scratchpad). Always require a verified final answer (CoVe). Log faithfulness drift as a hacking-detector."

---

## Open Questions for V14.5+

1. **Self-Principled Critique Tuning** (DeepSeek-R2) — wait for public details or attempt now from Constitutional AI patterns?
2. **Test-time RL** (AlphaProof) — feasible on Civo for high-stakes problems?
3. **Absolute Zero Reasoner** (May 2025) — zero-data self-play. Validate on small scale?
4. **R-Zero** self-evolving (Aug 2025) — alternative bootstrap path.
5. **Adaptive thinking switch** (GLM-4.5 dual-mode, Claude 4.6) — train via LCPO or use a router?

---

## Sources (all reasoned about above)

- [DeepSeek-R1 paper (arXiv 2501.12948)](https://arxiv.org/abs/2501.12948)
- [DeepSeek-R1 in Nature 2025](https://www.nature.com/articles/s41586-025-09422-z)
- [QwQ-32B blog (Qwen)](https://qwenlm.github.io/blog/qwq-32b/)
- [Magistral (arXiv 2506.10910)](https://arxiv.org/abs/2506.10910)
- [Phi-4-reasoning Technical Report (Microsoft)](https://www.microsoft.com/en-us/research/wp-content/uploads/2025/04/phi_4_reasoning.pdf)
- [GLM-4.1V-Thinking / GLM-4.5V (arXiv 2507.01006)](https://arxiv.org/html/2507.01006v5)
- [rStar-Math (arXiv 2501.04519)](https://huggingface.co/papers/2501.04519)
- [Open-Reasoner-Zero (arXiv 2503.24290)](https://arxiv.org/abs/2503.24290)
- [PRIME (arXiv 2502.01456)](https://arxiv.org/abs/2502.01456)
- [ThinkPRM (arXiv 2504.16828)](https://arxiv.org/abs/2504.16828)
- [AceReason-Nemotron (arXiv 2505.16400)](https://arxiv.org/abs/2505.16400)
- [Skywork-OR1 (arXiv 2505.22312)](https://arxiv.org/html/2505.22312v1)
- [VAPO (arXiv 2504.05118)](https://arxiv.org/abs/2504.05118)
- [AlphaProof Nature 2025-11](https://www.nature.com/articles/s41586-025-09833-y)
- [Gemini 2.5 Deep Think (Google)](https://blog.google/products/gemini/gemini-2-5-deep-think/)
- [s1 (arXiv 2501.19393)](https://arxiv.org/abs/2501.19393)
- [Bespoke-Stratos blog](https://www.bespokelabs.ai/blog/bespoke-stratos-the-unreasonable-effectiveness-of-reasoning-distillation)
- [LIMO (arXiv 2502.03387)](https://arxiv.org/html/2502.03387v1)
- [LIMR (arXiv 2502.11886)](https://arxiv.org/html/2502.11886v1)
- [L1/LCPO (arXiv 2503.04697)](https://arxiv.org/abs/2503.04697)
- [Train Long Think Short (arXiv 2508.08940)](https://arxiv.org/abs/2508.08940)
- [DAPO presentation, JHU](https://self-supervised.cs.jhu.edu/fa2025/files/presentations/Why-RL-Sep16-AdvancesNLP.pdf)
- [Dr.GRPO (arXiv 2505.22257)](https://arxiv.org/pdf/2505.22257)
- [Quiet-STaR (arXiv 2403.09629)](https://arxiv.org/abs/2403.09629)
- [Open-R1 blog (HuggingFace)](https://huggingface.co/blog/open-r1)
- [DeepScaleR-1.5B](https://huggingface.co/agentica-org/DeepScaleR-1.5B-Preview)
- [Anthropic Reasoning Models Don't Always Say What They Think (arXiv 2505.05410)](https://arxiv.org/abs/2505.05410)
- [Claude Extended Thinking (Anthropic)](https://www.anthropic.com/research/visible-extended-thinking)
- [OpenThoughts-114k (HF)](https://huggingface.co/datasets/open-thoughts/OpenThoughts-114k)
- [Mixture-of-Thoughts (Open-R1 Step 1)](https://huggingface.co/blog/open-r1)
- [InternLM-Math-Plus (arXiv 2402.06332)](https://arxiv.org/abs/2402.06332)
- [Marco-o1 / LLaVA-CoT (arXiv 2411.10440)](https://arxiv.org/abs/2411.10440)
- [Tree of Thoughts (arXiv 2305.10601)](https://arxiv.org/abs/2305.10601)
- [ReST-MCTS\*](https://openreview.net/forum?id=8rcFOqEud5)
- [DeepSeek-R2 release notes (recodechinaai)](https://recodechinaai.substack.com/p/deepseeks-next-move-what-v4-will)


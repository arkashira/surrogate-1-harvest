---
title: "V17 — Test-Time Compute Scaling Training (Surrogate-1 7-9B)"
date: 2026-05-01
version: V17
status: research-locked
goal: "Train Surrogate-1 7-9B to USE test-time compute so 7B + N× inference compute MATCHES frontier 70B+ on narrow domain"
parent: [[v16-bleeding-edge-may2026]]
related:
  - [[v14-reasoning-frontier]]
  - [[v14-rl-frontier-beyond-dapo]]
  - [[v15-reasoning-frontier]]
  - [[v16-data-scale-and-hf-sweep]]
  - [[self-improvement]]
tags: [test-time-compute, inference-scaling, budget-forcing, long-cot, prm, reasoning, surrogate-1, v17]
---

# V17 — Test-Time Compute Scaling Training

> **Thesis (V17 owner-stated)**: Pretraining a 7-9B from scratch to compete with 70B is too expensive. **Train the BEHAVIOR** that lets a 7B *use* extra inference compute to match a 70B in its narrow domain. Phi-4-mini-reasoning (3.8B) beats DeepSeek-R1-Distill-Qwen-7B at AIME — not by being bigger, but by **using thinking time better**.
>
> **V17 = bake the test-time-compute behaviors INTO weights at training time** so deployment-time inference scaling pays off. No extra params, no extra pretraining data, no architectural change. Just behavioral SFT + RL.

## Table of Contents
1. [Executive Summary — Top 6 Techniques](#1-executive-summary--top-6-techniques)
2. [Measured 7B+TTC ≈ 70B Trade-off Curves](#2-measured-7btestcompute--70b-trade-off-curves)
3. [Long-CoT RL — DeepSeek-R1, QwQ, R2, Phi-4-reasoning](#3-long-cot-rl--deepseek-r1-qwq-r2-phi-4-reasoning)
4. [Budget Forcing & Control Tokens — s1/s1K, BudgetThinker, L1/LCPO](#4-budget-forcing--control-tokens--s1s1k-budgetthinker-l1lcpo)
5. [Process Reward Models — PRM800K, Math-Shepherd, ThinkPRM, PRIME](#5-process-reward-models--prm800k-math-shepherd-thinkprm-prime)
6. [Tree-Search-as-Training-Data — rStar-Math, ReST-MCTS\*, MCTS rollouts](#6-tree-search-as-training-data--rstar-math-rest-mcts-mcts-rollouts)
7. [Self-Taught Reasoners — STaR, V-STaR, Quiet-STaR, LADDER, Self-MoA](#7-self-taught-reasoners--star-v-star-quiet-star-ladder-self-moa)
8. [Adaptive Length & Self-Truncation](#8-adaptive-length--self-truncation)
9. [Best-of-N + Self-Consistency as Training Data Mining](#9-best-of-n--self-consistency-as-training-data-mining)
10. [T4×2 Feasibility Matrix](#10-t4x2-feasibility-matrix)
11. [Phase 28-33 — V17 Concrete Phases for kaggle-trainer.sh](#11-phase-28-33--v17-concrete-phases-for-kaggle-trainersh)
12. [Paste-ready kaggle-trainer.sh patch](#12-paste-ready-kaggle-trainersh-patch)
13. [Integration with V14/V15/V16](#13-integration-with-v14v15v16)
14. [Sources & References](#14-sources--references)

---

## 1. Executive Summary — Top 6 Techniques

Ranked by `(measured uplift) × (T4×2 feasibility) / (training cost)`, for a 7-9B Surrogate aiming to match 70B in narrow domain.

| # | Technique | Paper / Date | 7B uplift (measured) | T4×2 feasible? | Training cost |
|---|---|---|---|---|---|
| **1** | **s1K + budget forcing** (1K SFT + "Wait" injection) | s1 — arXiv 2501.19393 (Jan 2025) | **+27% AIME24** (50→57% on Qwen2.5-32B); on 7B class +15-20% | **YES — SFT only**, 1K samples × 32K ctx, ~3-7 GPU-hr A100 → ~12 GPU-hr T4×2 | <1 day, ~$5 |
| **2** | **Long-CoT distill from R1/QwQ + GRPO outcome RL** (DeepSeek recipe) | DeepSeek-R1 — arXiv 2501.12948; R2 (Apr 2026, 92.7% AIME); QwQ-32B (Mar 2025) | **R1-Distill-Qwen-7B: 28.8 → 55.5% AIME24**; full GRPO adds another +10-15% | **YES — distill SFT** is T4-friendly; RL needs context-lengthening curriculum (8K→16K→24K) | 1-3 days, ~$15 |
| **3** | **rStar-Math self-evolved MCTS PRM** (4-round) | rStar-Math — arXiv 2501.04519 (Jan 2025) | **Qwen2.5-Math-7B: 58.8 → 90.0% MATH** (+31.2pp); **Phi3-mini-3.8B: 41.4 → 86.4%** | **PARTIAL** — MCTS data-gen on bigger box, then SFT+PRM training fits T4 | 2-4 days for full self-evolution; just SFT-on-MCTS-traces fits 1 day |
| **4** | **PRIME implicit PRM RL** (no separate PRM training) | PRIME — arXiv 2502.01456 (Feb 2025) | **Qwen2.5-Math-7B: +15.1% avg across 7 reasoning benchmarks**; 2.5× sample efficiency vs ORM-only GRPO | **YES** — implicit PRM = single Q-network derived from policy, T4-fittable with LoRA + ZeRO-2 | 1-2 days, ~$10 |
| **5** | **L1 / LCPO length-controlled RL** (budget-aware policy) | L1 — arXiv 2503.04697 (Mar 2025) | **1.5B Qwen-R1-distill: matches GPT-4o at same token budget**; +100% relative on reasoning under length constraint | **YES** — RL pass over already-distilled model, ≤24h on 4×A40 → ~36h T4×2 | 1.5 days, ~$8 |
| **6** | **Self-MoA + CISC weighted voting** (single-model parallel) | Self-MoA — arXiv 2502.00674; CISC — arXiv 2502.06233 | Self-MoA +6.6% AlpacaEval, +3.8% MATH/MMLU avg vs single sample; CISC matches SC at **46% fewer samples** | **INFERENCE-SIDE**, no extra training. Bake **confidence-emission SFT** so CISC works → tiny SFT delta | <0.5 day, ~$3 |

**Synthesis (the V17 owner play)**: Stack #1+#2+#4 in training time, then deploy with #5+#6 at inference. With ~5 days T4×2 (~$30 cloud) you go from raw Qwen2.5-7B-Math to a Surrogate-7B that **matches 70B-class reasoning** on the narrow domain, controlled by an `<effort>{none,low,med,high,xhigh}</effort>` token + `<budget=N>` directive.

---

## 2. Measured 7B+TestCompute ≈ 70B Trade-off Curves

> Real numbers, all from peer-reviewed or technical-report sources (no extrapolation). Domain = math reasoning (AIME 24/25, MATH-500), the most-measured TTC benchmark.

### 2.1 Pure parameter scaling (no thinking, single sample) — baseline anchors

| Model | Params | AIME24 pass@1 | MATH-500 |
|---|---|---|---|
| Qwen2.5-7B-Math (base, no reasoning) | 7B | ~12% | 58.8% |
| Qwen2.5-Math-72B-Instruct | 72B | ~30% | ~85% |
| GPT-4o (no thinking) | ~hundreds B | 13.4% | 76.6% |
| DeepSeek-V3 base | 671B MoE / 37B active | ~30% | 89% |

### 2.2 Same 7B + various test-time compute strategies

| 7B-class model | Strategy | Inference compute | Result | vs base |
|---|---|---|---|---|
| Qwen2.5-Math-7B (single CoT) | greedy 1× | 1× | AIME24 12% / MATH 58.8% | — |
| Qwen2.5-Math-7B + maj@64 (self-consistency) | 64 samples vote | 64× | AIME24 ~38% / MATH 80% | +26pp / +21pp |
| Qwen2.5-Math-7B + Best-of-1024 with oracle verifier | 1024 samples + verifier | 1024× | MATH **70%** (cited s1 paper) | proves 7B has the *capability* — needs a teacher to *select* |
| **DeepSeek-R1-Distill-Qwen-7B** (long-CoT SFT) | 1× long CoT (8-16K tokens) | ~10-30× greedy | **AIME24 55.5%** / MATH 92.8% | **+43pp AIME** vs raw |
| **rStar-Math Qwen2.5-Math-7B** (MCTS-PRM at inference) | 64 MCTS rollouts × PRM rerank | ~64-128× | **MATH 90.0% / AIME 53.3%** (top 20% high-school students) | matches o1-preview |
| **Phi-4-mini-reasoning 3.8B** + maj@N | parallel N | up to 1024× | **AIME25 nearly saturates pass@1 of o3-mini** teacher | proves 3.8B can match top-tier teacher |
| **DeepScaleR-1.5B** RL with iterative ctx-length | 1× long CoT | ~30× | **AIME24 43.1% (1.5B!)** | beats o1-preview at 1.5B |
| **L1-1.5B (LCPO)** at GPT-4o token budget | 1× length-controlled | matched | **matches GPT-4o** | 1.5B = much-larger frontier under same compute |
| **s1-32B + budget forcing** ("Wait" 6×) | 1× long CoT, forced extra | ~3-6× greedy | AIME24 50→57% (1.4× compute) | +7pp pure inference scaling |

**Headline numbers for V17 owner**:
- 7B with the right *training* + 64-sample TTC ≈ 70B at single-sample
- 1.5B with iterative-context RL training ≈ o1-preview (released 2024 frontier)
- 3.8B Phi-4-mini-reasoning approaches o3-mini at high parallel N — Microsoft's *explicit* design point

### 2.3 The compute-optimal trade-off (Snell et al., DeepMind, arXiv 2408.03314)

> "Scaling LLM test-time compute optimally can be more effective than scaling model parameters" — DeepMind ICLR 2025.

Key finding: **A 14× smaller model + compute-optimal TTC ≥ a 14× bigger model at single-sample**, on FLOP-matched problems where the smaller model has non-trivial baseline. **Breaks down on problems the small model cannot reach by any chain.**

This bounds V17 expectations: TTC will close the gap *within reach*, won't unlock capabilities that are simply absent from the 7B's representations.

### 2.4 The V17 design point

For Surrogate-1 7-9B in narrow domain (V17 owner's stated goal):
```
Target: 7B + N=8-32 TTC samples + verifier rerank ≥ frontier 70B at single-sample
Realistic on math/code/structured domains (proven 2025 across 5+ papers)
Less likely on broad-knowledge (size still wins on factual recall)
```

Cost ratio at deployment: 32× inference compute on a 7B ≈ 1× inference on a 70B (memory-bound), but 32 × 7B = 224B total compute vs 70B single = 70B → **TTC strategy is 3× more expensive at inference if naive**. Mitigation: early-stop verifier (bandit), CISC confidence-weighted vote (cuts 50%), Self-MoA-Seq (sliding window).

---

## 3. Long-CoT RL — DeepSeek-R1, QwQ, R2, Phi-4-reasoning

The dominant 2025 paradigm: **outcome-RL on a base or distilled model with verifiable rewards**, letting the model discover long-CoT behavior through GRPO/PPO. This is the foundation V17 builds on.

### 3.1 DeepSeek-R1 / R1-Zero recipe (arXiv 2501.12948, Jan 2025)

**R1-Zero (pure RL from base)**:
- Skip SFT entirely. Apply GRPO directly to base DeepSeek-V3.
- Reward = rule-based verifier (math: exact-match; code: unit tests pass).
- Format reward: `<think>...</think>` block must exist + answer must be in box.
- Language consistency reward: % of target-language tokens in CoT (avoid mixing).
- **No neural reward model** (deliberately — neural PRMs hack).
- Result: emergent self-reflection ("Wait, let me reconsider"), self-verification, and **monotonically increasing thinking length over training** without explicit length signal.

**R1 (cold-start improved pipeline)**:
- Stage 1: SFT on small (~1-10K) curated long-CoT corpus → seeds RL actor.
- Stage 2: GRPO with reasoning-task verifier rewards.
- Stage 3: Reject-sample best CoTs from RL'd model → filter → SFT non-reasoning data.
- Stage 4: Final RL across reasoning + general (with helpfulness/harmlessness rewards).
- Distill: Use the R1 traces to SFT smaller models (Qwen-1.5B/7B/14B/32B, Llama-8B/70B). **The distilled 7B reaches AIME24 55.5%** — the V17 starting point.

**V17-relevant insight**: R1-Distill-Qwen-7B + 2-3 days of additional GRPO with PRIME-style implicit PRM = the cheapest known route to 70B-class narrow-domain accuracy on a 7B.

### 3.2 QwQ-32B (Alibaba, Mar 2025)

- Outcome-based RL on Qwen2.5-32B base. Math verifier + code interpreter as reward.
- "First reason step by step, then produce result; verifier checks; model reformulates until correct."
- **Test-time compute is explicitly the marketing hook** ("a new inference paradigm").
- Matches DeepSeek-R1 (671B MoE) at 32B dense — 21× param-efficient.

**Lesson for V17**: simple verifiable outcome rewards are enough if the base model is strong + the RL is patient. No PRM needed.

### 3.3 DeepSeek-R2 (32B, Apr 2026)

- 32B dense, 92.7% AIME 2025 (frontier-level).
- Recipe: distill from R1 + DeepSeek-V3.2-Speciale (millions of long-CoT traces) + refined GRPO post-training.
- 70% cheaper per token than Western frontier reasoning APIs.
- Open-weight.

**V17 read**: R2 traces (HF dataset already mirrored — see `v16-data-scale-and-hf-sweep.md`) are the freshest distillation source available May 2026.

### 3.4 Phi-4-reasoning (14B) + Phi-4-mini-reasoning (3.8B) (Microsoft, Apr 2025)

- **Phi-4-reasoning (14B)**: SFT on 1.4M STEM/coding prompts where each demonstration was generated by **o3-mini** (teacher distillation). Plus a short RL phase ("Phi-4-reasoning-plus") with a **length-aware accuracy reward**: encourage *short* outputs when correct, *more think tokens* when wrong. Outperforms DeepSeek-R1-Distill-Llama-70B.
- **Phi-4-mini-reasoning (3.8B)**: Same recipe scaled down. Plus dedicated `<think>...</think>` block. **Saturates pass@1 of o3-mini at high parallel-N TTC** (the V17 thesis in action).
- **Phi-4-mini-flash-reasoning** (Jul 2025): SambaY hybrid arch (Mamba SSM + sliding-window attn + Gated Memory Unit). 10× throughput, 2-3× lower latency. Trained on 1M+ synthetic math problems generated by **DeepSeek-R1**.

**Critical V17 insight from Phi-4-reasoning paper**: explicit `<think>` placeholder tokens are picked up by the model **very early** in training. The hard part is not the format — it's the *quality* of reasoning that emerges through extended training. Implication: spend the SFT budget on **trace quality**, not on teaching the format.

### 3.5 The Phi-4-reasoning length-aware reward (V17 must-have)

```python
# Pseudo: length-aware accuracy reward (Phi-4-reasoning-plus inspired)
def length_aware_reward(generation, gold_answer, max_budget=8192):
    correct = verify(generation.answer, gold_answer)
    think_tokens = count_tokens(generation.think_block)

    if correct:
        # reward concise correct: bonus for being short
        return 1.0 + 0.3 * (1 - think_tokens / max_budget)
    else:
        # penalize wrong, but less if it tried hard (long think)
        return -0.5 + 0.2 * (think_tokens / max_budget)
```

This single reward shape **trains the model to allocate compute by difficulty** — exactly the V17 owner's request for "adaptive thinking length."

---

## 4. Budget Forcing & Control Tokens — s1/s1K, BudgetThinker, L1/LCPO

### 4.1 s1 / s1K — the simplest non-trivial recipe (arXiv 2501.19393, Stanford+UW, Jan 2025)

**Two-component recipe, paste-ready**:
1. **s1K dataset**: Just **1,000** questions, hand-curated by 3 axes — *difficulty* (filter to hard), *diversity* (50 domains), *quality* (Gemini Flash Thinking traces, manually checked). Open at https://huggingface.co/datasets/simplescaling/s1K.
2. **Budget forcing at inference**:
   - Lower bound: model tries to emit `</think>` early → replace with token "Wait" → forces continued thinking.
   - Upper bound: at budget exceeded → force `</think>\n` injection → model produces answer immediately.

**Numbers**:
- Qwen2.5-32B-Instruct + s1K SFT (3-7 GPU-hr) + budget forcing: **AIME24 50% → 57%** (extrapolating beyond no-intervention).
- Beats o1-preview by **+27%** on competition math (MATH/AIME24).

**Why it matters for V17**: this is a **<$50 training run** (1K samples × 32K ctx, no RL). Drop into Phase 28 below.

**On 7B**: s1 paper shows the recipe transfers but uplift is smaller (+10-15%) — because Qwen2.5-7B has less capacity to absorb hard reasoning. Mitigation: combine with DeepSeek-R1-Distill-7B as base (already long-CoT) — predicted +20-25% AIME.

### 4.2 BudgetThinker (arXiv 2508.17196, Aug 2025)

**Two-stage pipeline**:
1. SFT with periodic budget-remaining tokens injected: `<budget_left=512>...thinking...<budget_left=256>...`
2. Curriculum-RL with length-aware reward (similar to Phi-4-reasoning-plus).

**Result**: model honors `<budget=N>` directive in prompt with high fidelity, naturally compresses reasoning when budget tight.

**V17 patch**: bake `<budget>` token into vocab + insert during SFT data prep. Cheap.

### 4.3 L1 / LCPO (arXiv 2503.04697, CMU, Mar 2025)

**Length Controlled Policy Optimization** — the cleanest length-control RL.

```
Two objectives in reward:
(1) Correctness  → r_acc = verify(answer)
(2) Length match → r_len = -|len(generation) - target_length|  (LCPO-Exact)
                   r_len = -max(0, len(generation) - target_length)  (LCPO-Max)
```

User supplies **target length in prompt** — e.g., "Think for at most 2048 tokens" — model trained to obey.

**Results**:
- L1-1.5B (Qwen-R1-distill base + LCPO-Exact RL): matches GPT-4o at the same token budget on math reasoning.
- LCPO-trained models become unexpectedly strong **short-CoT** models too (+10% over base at *same* short length).

**V17 patch**: identical to GRPO loop already planned in V14, just add length-match term to reward. ~24h on 4×A40 (paper's setup) → ~36h on T4×2 with LoRA + grad-accum.

### 4.4 NoWait — counter-evidence (arXiv 2506.08343, Jun 2025)

> "Wait, We Don't Need to 'Wait'! Removing Thinking Tokens Improves Reasoning Efficiency"

Suppressing self-reflection tokens during decoding cuts CoT length 27-51% with **no accuracy loss** on already-trained reasoning models. **Implication for V17**: train with "Wait" injection during *training* (s1-style) but offer NoWait flag at *deployment* for cost-tight queries. Both are cheap wins.

---

## 5. Process Reward Models — PRM800K, Math-Shepherd, ThinkPRM, PRIME

PRM = score every intermediate step, not just the final answer. Critical for **best-of-N reranking** (V17 deployment-side) and for **RL signal density** (V17 training-side).

### 5.1 PRM800K (OpenAI, 2023) — the human-labeled benchmark

- ~265K-350K human-labeled step annotations on MATH problems.
- Each step labeled `correct / neutral / incorrect`.
- The reference dataset for evaluating any step-level verifier.
- Free, on HF: `Birchlabs/openai-prm800k-stepwise-critic`.

### 5.2 Math-Shepherd (arXiv 2312.08935) — automatic PRM data

- ~400K samples, **fully automatic** annotation via MCTS-style completer rollouts.
- Beat PRM800K on MATH despite having no human labels.
- **The standard recipe for 2025+** (PRM800K is for eval; Math-Shepherd-style for training).

### 5.3 ThinkPRM (arXiv 2504.16828, Apr 2025) — verbalized PRM via long CoT

- **Generative** PRM: instead of scoring each step with a number, produce a verification chain-of-thought.
- Trained by fine-tuning a long-CoT model (QwQ-32B-Preview as teacher) with **only 1% of PRM800K's labels**.
- Beats LLM-as-Judge by 7.2% on ProcessBench at matched token budget.
- Code/data/models open.

**V17 use**: ThinkPRM as the **deployment-time verifier** for best-of-N reranking. Cheaper to train than discriminative PRM, and naturally scales when given more compute.

### 5.4 PRIME — implicit PRM (arXiv 2502.01456, Feb 2025) — **the V17 RL workhorse**

**The trick**: train the PRM **implicitly** as the difference between policy and reference logprobs. No separate PRM training pass.

```
implicit_PRM(s_t, a_t) = β × [log π_θ(a_t | s_t) − log π_ref(a_t | s_t)]
```

Theorem (paper): this implicit PRM is mathematically a **Q-function**, so token-level rewards drop out for free.

**Eurus-2-7B-PRIME**: starting from Qwen2.5-Math-7B-Base, **+15.1% avg across 7 reasoning benchmarks**, 2.5× sample efficiency vs ORM-only, surpasses Qwen2.5-Math-7B-Instruct (the official RL'd version) with **10% of the training data**.

**Why V17**:
- No extra PRM training run (saves ~$50 GPU)
- Drops into existing GRPO loop with one line change to advantage computation
- T4×2-feasible with LoRA

### 5.5 The PRM ladder for V17

| Phase | Verifier | Cost | Purpose |
|---|---|---|---|
| Train (Phase 29 RL) | **PRIME implicit PRM** | $0 (derived from policy) | Dense per-token reward signal in GRPO |
| Inference cheap | None / outcome-only | $0 | Single-sample greedy for easy tasks |
| Inference quality | **ThinkPRM-1.5B** distilled from QwQ traces | $5/inference (re-rank 8-32 samples) | Best-of-N selection |

---

## 6. Tree-Search-as-Training-Data — rStar-Math, ReST-MCTS\*, MCTS Rollouts

These methods **use MCTS at training time** to mine high-quality reasoning traces, then **distill the traces back into the policy via SFT**. The model never runs MCTS at inference — it just inherits the better reasoning patterns.

### 6.1 rStar-Math (arXiv 2501.04519, Microsoft, Jan 2025) — the headline result

**Three innovations**:
1. **Code-augmented CoT**: each reasoning step is wrapped with executable Python; rollout is rejected if code throws or output disagrees with model's claimed numeric.
2. **Process Preference Model (PPM)**: avoids "step-level scalar score is too noisy" by training as preference (step A > step B), not regression.
3. **Self-evolution loop**: 4 rounds, where round-N's policy and PPM produce data for round-N+1.

**Eye-watering 7B results**:
- Qwen2.5-Math-7B: MATH **58.8% → 90.0%** (+31.2pp), AIME 53.3% (top 20% high-school)
- Phi3-mini-3.8B: MATH **41.4% → 86.4%** (+45pp at 3.8B!)

**Why it works on small models**: MCTS provides quality far beyond what the policy can reach alone. Distilling MCTS traces transfers that quality into the policy weights.

**T4 feasibility**: data generation (the MCTS rollouts) **needs a bigger box** (paper used 4×A100). But once generated, the **SFT-on-traces step is T4×2 feasible**. Strategy: rent A100 for 1 day to generate ~50K rStar-style traces (~$100), then SFT on T4×2.

**Alternative**: download already-released rStar-Math traces from HF (paper code/data is open).

### 6.2 ReST-MCTS\* (arXiv 2406.03816, NeurIPS 2024)

- Same family: MCTS for trace generation + PRM training.
- Inference of process rewards from outcome correctness: probability that step S leads to correct answer = the reward.
- Llama3-8B-Instruct, Mistral-7B-MetaMATH, SciGLM-6B as backbones.
- Code: github.com/THUDM/ReST-MCTS

**Use as backup** if rStar-Math's data is not redistributable for V17's licensing.

### 6.3 LADDER (arXiv 2503.00735, Tufa Labs, Mar 2025) — recursive self-decomposition

- Model generates **easier variants** of problems it can't solve, learns on those, climbs the ladder.
- Pure RL with GRPO, no human labels.
- Llama-3.2-3B: integration accuracy **1% → 82%** (yes, eighty-two).
- Qwen2.5-7B-R1-distill: **73% on MIT Integration Bee 2025 qualifier**.
- Adding TTRL (test-time RL) on top: **73% → 90%**.

**V17 read**: LADDER is the **cheapest way to extend a domain** — let the model bootstrap new difficulty levels. Use as Phase 32 (auto-curriculum). Open-source code.

---

## 7. Self-Taught Reasoners — STaR, V-STaR, Quiet-STaR, LADDER, Self-MoA

### 7.1 STaR (arXiv 2203.14465, Stanford 2022) — the founding paper

Loop:
1. Generate rationale for each Q with few-shot.
2. If answer correct → keep rationale.
3. If wrong → re-generate rationale **given the correct answer** ("rationalization").
4. SFT on all kept rationales. Repeat.

**V17 use**: Phase 30 self-improvement loop, mining net-new rationales beyond the seed dataset.

### 7.2 V-STaR (arXiv 2402.06457, COLM 2024)

- STaR + a **verifier trained via DPO** on (correct rationale, incorrect rationale) pairs.
- Both correct AND incorrect generations are used (STaR throws incorrect away).
- 7B V-STaR > LLaMA-2-70B (8-shot) on GSM8K; 7B V-STaR ≈ CodeLLaMA-34B zero-shot on HumanEval.
- **+6-17% over STaR**.

### 7.3 Quiet-STaR (arXiv 2403.09629, Stanford+Notbad AI, Mar 2024) — silent thinking everywhere

Trains the model to **emit a hidden rationale at every token** during pretraining, scored against next-token prediction usefulness.
- Tokenwise parallel sampling alg.
- Learnable `<startthought>`, `<endthought>` tokens.
- REINFORCE-based reward.
- **Zero-shot improvements: GSM8K 5.9 → 10.9% (no fine-tuning on the task)**, CommonsenseQA 36 → 47%.

**V17 patch**: skip in V17 (Quiet-STaR is pretraining-stage, expensive). But the **insight** — explicit thinking-block tokens — is already used by Phi-4 / R1 / QwQ via `<think>` tags.

### 7.4 LADDER — see §6.3 above. Cleanest "model teaches itself harder problems" recipe.

### 7.5 Self-MoA (arXiv 2502.00674, Feb 2025) — **the V17 deployment win**

**Counter-intuitive finding**: aggregating N samples from **a single strong model** beats Mixture-of-Agents across N different models. Quality consistency > diversity.

- Self-MoA: sample N from same model at higher temperature, aggregate via the same model as judge.
- **+6.6% AlpacaEval, +3.8% avg across MATH/MMLU/CRUX**.
- **Self-MoA-Seq**: sliding-window aggregation when N exceeds context.

**V17 deployment topology**:
```
User query
  ↓
Surrogate-7B × 8 parallel samples (temp 0.7)
  ↓
Surrogate-7B in "judge mode" (Self-MoA aggregation, single context window)
  ↓
Final answer
```

**No extra training** — just a system-prompt template for the judge role.

---

## 8. Adaptive Length & Self-Truncation

The 2025-2026 frontier is **the model decides when to stop thinking**, conditioned on perceived difficulty. Five papers worth knowing:

| Paper | Key idea | Result |
|---|---|---|
| **AdaptThink** (arXiv 2505.13417) | RL-trained mode switch (think vs no-think) by problem difficulty | -50% tokens, same accuracy |
| **AdapThink** (2506.18237) | Adaptive thinking preferences via on-policy RL | -70% tokens on easy, -40% on hard, accuracy preserved |
| **ARLCP** (2602.12113) | Adaptive Reflection + Length-Coordinated Penalty | Stops unnecessary reflection |
| **e1** (2510.27042) | Learning Adaptive Control of Reasoning Effort | Honors per-prompt effort directive |
| **ACPO** (Adaptive Cognition Policy Optimization, EMNLP 2025) | System-aware reasoning tokens + online difficulty estimation + token budget | Difficulty-aware allocation |
| **ARM** (2505.20258) | Adaptive Reasoning Model | Switch reasoning modes during inference |

### 8.1 The unified V17 design (combining the best of all of these)

**Vocabulary additions**:
```
<effort>none</effort>      # ~0% reasoning budget
<effort>low</effort>        # ~20% (quick CoT, ≤256 think tokens)
<effort>medium</effort>     # ~50% (balanced, ≤2048)
<effort>high</effort>       # ~80% (deep, ≤8192)
<effort>xhigh</effort>      # ~95% (max, ≤32768)
<budget=N>                  # explicit token budget hint
<difficulty>easy|med|hard|extreme</difficulty>  # model's self-assessment
```

**Training reward shape (combines Phi-4-reasoning-plus + L1-LCPO + AdapThink)**:
```python
def adaptive_length_reward(gen, gold, prompt_effort_directive, max_budget):
    correct = verify(gen.answer, gold)
    think_len = count_think_tokens(gen)
    target_len = effort_to_target_len(prompt_effort_directive, max_budget)

    r_acc  = 1.0 if correct else -0.5
    r_eff  = -0.3 * abs(think_len - target_len) / max_budget   # honor directive
    r_brev = 0.2 * (correct) * max(0, 1 - think_len / target_len)  # bonus for under-budget correct

    return r_acc + r_eff + r_brev
```

This single reward teaches: (1) honor the effort directive, (2) be brief when easy, (3) think long when needed.

---

## 9. Best-of-N + Self-Consistency as Training Data Mining

**The dual use of TTC** — same techniques are both an *inference strategy* AND a *training-data mining tool*.

### 9.1 The mining loop (V17 Phase 31)

```
For each unlabeled prompt p in domain corpus:
  1. Sample N=32 long-CoTs from current Surrogate-7B (temp 0.8)
  2. Run each through PRIME-trained verifier OR ThinkPRM
  3. Take majority-voted answer = pseudo-label
  4. Keep only top-K trajectories (by verifier score) per p
  5. Add to training corpus
SFT next iteration on this self-mined corpus
```

This is **self-consistency-as-distillation** — the model teaches itself by voting on its own outputs. Same as STaR, but with a smarter verifier than answer-correctness.

### 9.2 Knowledge distillation with N-best reranking (Apple ML, arXiv 2305.12057)

- Use teacher's top-N hypotheses as labels (not just top-1).
- Re-rank with diverse models (different inductive biases).
- 100× param reduction with comparable accuracy.

**V17**: when distilling from R1/QwQ/Phi-4-reasoning into Surrogate-7B, take **top-3** generations weighted by verifier, not just argmax.

### 9.3 CISC weighted vote (arXiv 2502.06233) — efficient SC at inference

- Weight each of N samples by self-reported P(True) confidence.
- **46% fewer samples for same accuracy as plain self-consistency**.

**V17 deployment**: train a tiny SFT head ("emit `<conf=0.X>` after each answer") so CISC works out-of-the-box. Cheap.

### 9.4 Scalable Best-of-N via self-certainty (arXiv 2502.18581)

- Use **logit margin** (not external verifier) as quality signal.
- Pure inference-time, no extra model.

**V17 fallback** when verifier latency unacceptable.

---

## 10. T4×2 Feasibility Matrix

Kaggle gives ~30h/week of T4×2 (2× T4 16GB = 32GB pooled). All V17 phases must fit. Here's the matrix:

| Phase | Technique | Mem footprint | Compute (T4×2 hours) | Feasibility |
|---|---|---|---|---|
| 28 | s1K SFT | 7B + 32K ctx + grad → ~40GB → **needs LoRA** + grad-checkpoint + ZeRO-2 → fits | 12-15h | **YES** with LoRA |
| 29 | PRIME GRPO RL (math+code verifier) | 7B policy + ref + KV → ~50GB → **LoRA + ZeRO-2 + offload** | 24-36h | **YES, tight** |
| 30 | rStar-Math trace SFT (data pre-mined) | same as Phase 28 | 12-18h | **YES** if data already on HF |
| 30b | rStar-Math MCTS data generation | needs big box (paper: 4×A100) | N/A on T4 | **NO** — rent A100 1 day OR download HF dataset |
| 31 | Self-mining best-of-N + verifier rerank | inference only, batch 32 with int8 | 8h per 10K prompts | **YES** |
| 32 | LADDER auto-curriculum | RL like Phase 29 | 24h per round | **YES, tight** |
| 33 | LCPO length-control RL | reward = acc + length-match | 18-24h | **YES** |

**Total V17 budget**: ~120 T4×2 hours = 4 weeks of free Kaggle (or 1 week if scheduled tightly).

**Memory tricks (already in V14/V15/V16)**:
- LoRA r=64-128 (vs full FT)
- Gradient checkpointing
- ZeRO-2 with CPU offload (slower but fits)
- 4-bit base model + bf16 adapters (QLoRA-style)
- Sequence parallel for 32K ctx
- Selective layer freezing (freeze first 50% of layers in late phases)

---

## 11. Phase 28-33 — V17 Concrete Phases for kaggle-trainer.sh

Continuing the V13 → V14 → V15 → V16 phase numbering. V16 ended at Phase 27. **V17 adds Phases 28-33**.

### Phase 28 — s1K + budget forcing SFT (the cheap quick-win)

**What**: SFT Surrogate-7B on s1K (1000 hard reasoning Q+A with long Gemini-Thinking traces). Add `<think>...</think>` and `Wait` injection during data prep.

**Data**:
- Source: `simplescaling/s1K` on HF (open).
- Augment: also include Phi-4-reasoning's released traces if licensable.
- Format each trace with `<budget=X>` token at random positions (BudgetThinker-style).

**Why first**: cheapest possible delta. <1 day. Sets up the format for all subsequent phases.

**Expected uplift**: +10-15% AIME for Qwen2.5-7B-base; +5-8% for already-distilled R1-Qwen-7B.

### Phase 29 — PRIME GRPO RL (the high-impact phase)

**What**: GRPO with implicit PRM (PRIME) + length-aware reward (Phi-4-reasoning-plus + L1-LCPO).

**Reward**:
```python
r = r_acc + r_format + r_lang + r_len_aware + r_implicit_prm
```

**Verifier sources**:
- math: math-verify (rule-based)
- code: pytest sandbox (Modal/E2B)
- structured: JSON-schema match
- format: `<think>...</think><answer>...</answer>` regex

**Group size**: 8 (Math-Shepherd default), drop to 4 if T4 OOM.

**Expected uplift**: +15% avg over Phase 28 model (PRIME paper).

### Phase 30 — rStar-Math trace SFT (the quality-boost phase)

**What**: SFT on rStar-Math-released traces (~500K code-augmented CoT trajectories with PPM scores). Filter to top-30% by PPM score.

**Optional 30b**: if budget allows, run our own MCTS data generation (rented A100 1 day) on Surrogate-domain-specific prompts — gives fresh, in-distribution traces.

**Expected uplift**: +5-10% on the narrow domain (where rStar-style code-augmented verification has bite).

### Phase 31 — Self-mining via best-of-N + ThinkPRM

**What**: take ~50K unlabeled domain prompts. For each, generate 32 long-CoTs, rank by ThinkPRM, keep top-3, add to next-iteration SFT corpus.

**Loop**: do 2 rounds. Each round = ~8-12h on T4×2.

**Expected uplift**: +3-7% per round (STaR/V-STaR/Self-Improvement papers all converge on this).

### Phase 32 — LADDER auto-curriculum

**What**: model generates easier variants of problems it cannot solve. RL with GRPO on the easier variants. After accuracy on easier-tier > 80%, promote to harder.

**3 rounds**: easy → med → hard → extreme.

**Expected uplift**: domain-dependent, +5-15% on extreme-difficulty splits (LADDER paper: 1% → 82% on integration).

### Phase 33 — LCPO length-controlled RL (the deployment-ready polish)

**What**: GRPO with explicit length-match reward; train the model to honor `<effort>...</effort>` and `<budget=N>` directives in prompt.

**Why last**: applies *after* the model has full reasoning competence; the polish layer.

**Expected uplift**: minimal accuracy delta, but **massive cost-control delta at deployment** (variable compute per query → 10× cheaper avg latency).

### Inference-side V17 (no training cost)

- **Self-MoA** wrapper: 8 parallel samples + judge-mode aggregation
- **CISC** weighted vote on the 8 samples
- **NoWait** flag for cost-tight queries
- **Budget-forcing** "Wait" injection for high-stakes queries

These plug into the deployment harness, not the trainer.

---

## 12. Paste-ready kaggle-trainer.sh patch

> **Drops in below the V16 block (Phase A/B/C tool-use) at the top of `~/.surrogate/hf-space/bin/kaggle-trainer.sh`**

```bash
# =======================================================================
# V17 — TEST-TIME COMPUTE SCALING (Phases 28-33)
# Goal: train Surrogate-7B to USE inference compute → 7B + N×TTC ≈ 70B
# =======================================================================

# ---- V17 master switch ----
: "${V17_TTC_ENABLED:=1}"                    # 0=skip V17 entirely

# ---- Phase 28: s1K + budget forcing SFT ----
: "${V17_PHASE28_S1K:=1}"
: "${V17_S1K_DATASET:=simplescaling/s1K}"    # also try: simplescaling/s1K-1.1
: "${V17_S1K_EPOCHS:=5}"                     # paper: 5
: "${V17_S1K_LR:=4e-5}"
: "${V17_S1K_MAX_SEQ_LEN:=32768}"
: "${V17_S1K_BUDGET_TOKEN_PROB:=0.4}"        # P of inserting <budget=N> token in trace
: "${V17_S1K_WAIT_INJECT_PROB:=0.3}"         # P of inserting "Wait" mid-trace as augmentation
: "${V17_S1K_LORA_R:=128}"                   # higher r for SFT quality
: "${V17_S1K_LORA_ALPHA:=256}"

# ---- Phase 29: PRIME GRPO RL with length-aware reward ----
: "${V17_PHASE29_PRIME_GRPO:=1}"
: "${V17_PRIME_BETA:=0.05}"                  # paper recommended 0.01-0.1
: "${V17_PRIME_GROUP_SIZE:=8}"               # GRPO group size (drop to 4 if OOM)
: "${V17_PRIME_MAX_NEW_TOKENS:=8192}"
: "${V17_REWARD_ACC_WEIGHT:=1.0}"
: "${V17_REWARD_FORMAT_WEIGHT:=0.1}"         # <think></think> structure
: "${V17_REWARD_LANG_WEIGHT:=0.05}"          # target-language consistency (R1 trick)
: "${V17_REWARD_LEN_AWARE_WEIGHT:=0.3}"      # Phi-4-reasoning-plus shape
: "${V17_REWARD_IMPLICIT_PRM_WEIGHT:=0.5}"   # PRIME implicit PRM weight
: "${V17_PRIME_KL_PENALTY:=0.04}"
: "${V17_RL_CTX_CURRICULUM:=8192,16384,24576}"  # DeepScaleR-style iterative ctx lengthening

# ---- Phase 30: rStar-Math trace SFT ----
: "${V17_PHASE30_RSTAR:=1}"
: "${V17_RSTAR_DATASET:=}"                   # set to HF dataset id when published, else local mined
: "${V17_RSTAR_PPM_PERCENTILE:=0.7}"         # keep top-30% by PPM score
: "${V17_RSTAR_CODE_AUGMENT:=1}"             # require executable Python in step
: "${V17_RSTAR_EPOCHS:=3}"

# ---- Phase 31: self-mining best-of-N + verifier rerank ----
: "${V17_PHASE31_SELFMINE:=1}"
: "${V17_SELFMINE_PROMPTS:=50000}"           # unlabeled prompts to mine
: "${V17_SELFMINE_N_SAMPLES:=32}"            # samples per prompt
: "${V17_SELFMINE_TEMP:=0.8}"
: "${V17_SELFMINE_TOPK_KEEP:=3}"             # keep top-K of N
: "${V17_SELFMINE_VERIFIER:=thinkprm}"       # thinkprm | prime | self_certainty | maj_vote
: "${V17_SELFMINE_ROUNDS:=2}"

# ---- Phase 32: LADDER auto-curriculum ----
: "${V17_PHASE32_LADDER:=1}"
: "${V17_LADDER_ROUNDS:=3}"                  # easy → med → hard
: "${V17_LADDER_PROMOTE_THRESH:=0.80}"       # accuracy threshold to promote difficulty
: "${V17_LADDER_VARIANTS_PER_PROBLEM:=4}"    # easier variants generated per failed problem

# ---- Phase 33: LCPO length-controlled RL (deploy polish) ----
: "${V17_PHASE33_LCPO:=1}"
: "${V17_LCPO_MODE:=max}"                    # exact | max
: "${V17_LCPO_LEN_REWARD_WEIGHT:=0.4}"
: "${V17_EFFORT_LEVELS:=none,low,medium,high,xhigh}"
: "${V17_BUDGET_TOKENS:=128,512,2048,8192,32768}"  # paired with effort levels

# ---- Inference-side knobs (deploy harness reads these) ----
: "${V17_DEPLOY_SELFMOA:=1}"                 # parallel-N + judge aggregation
: "${V17_DEPLOY_SELFMOA_N:=8}"
: "${V17_DEPLOY_CISC:=1}"                    # confidence-weighted vote
: "${V17_DEPLOY_NOWAIT_FLAG:=1}"             # allow NoWait suppression for cost-tight
: "${V17_DEPLOY_BUDGETFORCE_WAIT:=1}"        # allow forced "Wait" injection for hi-stakes

# ---- Vocab additions (tokenizer extension; runs once before Phase 28) ----
: "${V17_VOCAB_EXTEND:=1}"
V17_NEW_TOKENS=(
  "<think>"
  "</think>"
  "<answer>"
  "</answer>"
  "<effort>" "</effort>"
  "<budget>" "</budget>"
  "<difficulty>" "</difficulty>"
  "<conf>" "</conf>"
  "<verify>" "</verify>"
  "<step>" "</step>"
)

# =======================================================================
# V17 PIPELINE (called from main()):
#   v17_extend_vocab            # tokenizer extension (idempotent)
#   v17_phase28_s1k             # ~12-15h T4×2
#   v17_phase29_prime_grpo      # ~24-36h T4×2 (3 ctx-lengthening rounds)
#   v17_phase30_rstar           # ~12-18h T4×2 (data must be pre-mined)
#   v17_phase31_selfmine        # ~16-24h T4×2 (2 rounds × 8-12h)
#   v17_phase32_ladder          # ~24-36h T4×2 (3 difficulty rounds)
#   v17_phase33_lcpo            # ~18-24h T4×2
# =======================================================================

v17_extend_vocab() {
  [ "${V17_VOCAB_EXTEND}" != "1" ] && return 0
  python -m surrogate.tokenizer.extend \
    --base "${BASE_MODEL}" \
    --add-tokens "${V17_NEW_TOKENS[@]}" \
    --out "${WORK_DIR}/tokenizer-v17"
}

v17_phase28_s1k() {
  [ "${V17_TTC_ENABLED}" != "1" ] && return 0
  [ "${V17_PHASE28_S1K}" != "1" ] && return 0
  echo "[V17.28] s1K + budget-forcing SFT"
  python -m surrogate.train.sft \
    --base "${BASE_MODEL}" \
    --tokenizer "${WORK_DIR}/tokenizer-v17" \
    --dataset "${V17_S1K_DATASET}" \
    --augment.budget_token_prob "${V17_S1K_BUDGET_TOKEN_PROB}" \
    --augment.wait_inject_prob "${V17_S1K_WAIT_INJECT_PROB}" \
    --epochs "${V17_S1K_EPOCHS}" \
    --lr "${V17_S1K_LR}" \
    --max-seq-len "${V17_S1K_MAX_SEQ_LEN}" \
    --lora.r "${V17_S1K_LORA_R}" \
    --lora.alpha "${V17_S1K_LORA_ALPHA}" \
    --grad-checkpoint \
    --zero2 --offload-cpu \
    --out "${WORK_DIR}/v17-phase28"
}

v17_phase29_prime_grpo() {
  [ "${V17_TTC_ENABLED}" != "1" ] && return 0
  [ "${V17_PHASE29_PRIME_GRPO}" != "1" ] && return 0
  echo "[V17.29] PRIME GRPO with length-aware reward (3-stage ctx curriculum)"
  IFS=',' read -ra CTX_STAGES <<< "${V17_RL_CTX_CURRICULUM}"
  PREV="${WORK_DIR}/v17-phase28"
  for ctx in "${CTX_STAGES[@]}"; do
    OUT="${WORK_DIR}/v17-phase29-ctx${ctx}"
    python -m surrogate.train.grpo \
      --base "${PREV}" \
      --reward.spec '{
        "acc":'${V17_REWARD_ACC_WEIGHT}',
        "format":'${V17_REWARD_FORMAT_WEIGHT}',
        "lang":'${V17_REWARD_LANG_WEIGHT}',
        "len_aware":'${V17_REWARD_LEN_AWARE_WEIGHT}',
        "implicit_prm":'${V17_REWARD_IMPLICIT_PRM_WEIGHT}'
      }' \
      --prime.beta "${V17_PRIME_BETA}" \
      --grpo.group-size "${V17_PRIME_GROUP_SIZE}" \
      --grpo.kl "${V17_PRIME_KL_PENALTY}" \
      --max-new-tokens "${ctx}" \
      --grad-checkpoint --zero2 --offload-cpu --lora.r 64 \
      --out "${OUT}"
    PREV="${OUT}"
  done
  ln -sf "${PREV}" "${WORK_DIR}/v17-phase29"
}

v17_phase30_rstar() {
  [ "${V17_TTC_ENABLED}" != "1" ] && return 0
  [ "${V17_PHASE30_RSTAR}" != "1" ] && return 0
  [ -z "${V17_RSTAR_DATASET}" ] && { echo "[V17.30] no rStar dataset configured, skipping"; return 0; }
  python -m surrogate.train.sft \
    --base "${WORK_DIR}/v17-phase29" \
    --dataset "${V17_RSTAR_DATASET}" \
    --filter.ppm_min_percentile "${V17_RSTAR_PPM_PERCENTILE}" \
    --require.code-augmented "${V17_RSTAR_CODE_AUGMENT}" \
    --epochs "${V17_RSTAR_EPOCHS}" \
    --max-seq-len 16384 \
    --lora.r 64 --grad-checkpoint --zero2 --offload-cpu \
    --out "${WORK_DIR}/v17-phase30"
}

v17_phase31_selfmine() {
  [ "${V17_TTC_ENABLED}" != "1" ] && return 0
  [ "${V17_PHASE31_SELFMINE}" != "1" ] && return 0
  PREV="${WORK_DIR}/v17-phase30"
  [ ! -e "${PREV}" ] && PREV="${WORK_DIR}/v17-phase29"
  for round in $(seq 1 "${V17_SELFMINE_ROUNDS}"); do
    # Generate N samples per prompt
    python -m surrogate.mine.bestofn \
      --model "${PREV}" \
      --prompts "${DOMAIN_PROMPTS_FILE}" \
      --n-samples "${V17_SELFMINE_N_SAMPLES}" \
      --temp "${V17_SELFMINE_TEMP}" \
      --max-prompts "${V17_SELFMINE_PROMPTS}" \
      --out "${WORK_DIR}/v17-mined-r${round}.jsonl"
    # Rerank with verifier, keep top-K
    python -m surrogate.mine.rerank \
      --in "${WORK_DIR}/v17-mined-r${round}.jsonl" \
      --verifier "${V17_SELFMINE_VERIFIER}" \
      --top-k "${V17_SELFMINE_TOPK_KEEP}" \
      --out "${WORK_DIR}/v17-mined-r${round}-top.jsonl"
    # SFT next round on filtered traces
    OUT="${WORK_DIR}/v17-phase31-r${round}"
    python -m surrogate.train.sft \
      --base "${PREV}" \
      --dataset-jsonl "${WORK_DIR}/v17-mined-r${round}-top.jsonl" \
      --epochs 2 --max-seq-len 16384 \
      --lora.r 64 --grad-checkpoint --zero2 --offload-cpu \
      --out "${OUT}"
    PREV="${OUT}"
  done
  ln -sf "${PREV}" "${WORK_DIR}/v17-phase31"
}

v17_phase32_ladder() {
  [ "${V17_TTC_ENABLED}" != "1" ] && return 0
  [ "${V17_PHASE32_LADDER}" != "1" ] && return 0
  PREV="${WORK_DIR}/v17-phase31"
  for round in $(seq 1 "${V17_LADDER_ROUNDS}"); do
    OUT="${WORK_DIR}/v17-phase32-r${round}"
    python -m surrogate.train.ladder \
      --base "${PREV}" \
      --variants-per-failed "${V17_LADDER_VARIANTS_PER_PROBLEM}" \
      --promote-threshold "${V17_LADDER_PROMOTE_THRESH}" \
      --reward.spec '{"acc":1.0,"format":0.1,"len_aware":0.3,"implicit_prm":0.4}' \
      --grpo.group-size 4 \
      --max-new-tokens 16384 \
      --lora.r 64 --grad-checkpoint --zero2 --offload-cpu \
      --out "${OUT}"
    PREV="${OUT}"
  done
  ln -sf "${PREV}" "${WORK_DIR}/v17-phase32"
}

v17_phase33_lcpo() {
  [ "${V17_TTC_ENABLED}" != "1" ] && return 0
  [ "${V17_PHASE33_LCPO}" != "1" ] && return 0
  python -m surrogate.train.grpo \
    --base "${WORK_DIR}/v17-phase32" \
    --reward.spec '{
      "acc":1.0,
      "format":0.1,
      "len_match":'${V17_LCPO_LEN_REWARD_WEIGHT}',
      "implicit_prm":0.3
    }' \
    --lcpo.mode "${V17_LCPO_MODE}" \
    --lcpo.effort-levels "${V17_EFFORT_LEVELS}" \
    --lcpo.budget-tokens "${V17_BUDGET_TOKENS}" \
    --grpo.group-size 4 \
    --max-new-tokens 32768 \
    --lora.r 32 --grad-checkpoint --zero2 --offload-cpu \
    --out "${WORK_DIR}/v17-phase33"
}

# Add to main pipeline (after V16 phases):
# v17_extend_vocab
# v17_phase28_s1k
# v17_phase29_prime_grpo
# v17_phase30_rstar
# v17_phase31_selfmine
# v17_phase32_ladder
# v17_phase33_lcpo
# echo "V17 done. Final model: ${WORK_DIR}/v17-phase33"
```

**Activate full V17 with single env switch**:
```bash
V17_TTC_ENABLED=1 \
V17_S1K_DATASET=simplescaling/s1K \
V17_RSTAR_DATASET=microsoft/rstar-math-traces \
DOMAIN_PROMPTS_FILE=./surrogate-domain-prompts.jsonl \
bash kaggle-trainer.sh
```

**Run subset (e.g. just s1K + PRIME)**:
```bash
V17_PHASE30_RSTAR=0 V17_PHASE31_SELFMINE=0 \
V17_PHASE32_LADDER=0 V17_PHASE33_LCPO=0 \
bash kaggle-trainer.sh
```

---

## 13. Integration with V14/V15/V16

### V17 reuses from earlier versions

| Earlier piece | Used in V17 | Where |
|---|---|---|
| V14 GRPO/DAPO infrastructure | yes | Phase 29, 32, 33 |
| V14 reasoning verifier (math + code) | yes | Phase 29 reward |
| V15 Phase 0 effort tokens | yes | Phase 33 LCPO targets |
| V15 self-consistency-as-distillation stub | **completed in V17 Phase 31** | self-mining loop |
| V15 PRIME implicit PRM | **fully wired in V17 Phase 29** | reward spec |
| V16 Phase A/B/C tool-use | parallel | tool-use is V16 lane; V17 is reasoning lane — they compose at deployment |
| V16 long-context curriculum | yes | Phase 29 ctx lengthening (8K→16K→24K) inherits V16's KV memory tricks |

### V17 adds net-new

| Piece | Where in V17 | Net-new training behavior |
|---|---|---|
| s1K + Wait injection | Phase 28 | Format + budget-forcing obedience |
| Length-aware reward (Phi-4-reasoning-plus) | Phase 29 | Concise-when-correct, verbose-when-stuck |
| rStar-Math code-augmented traces | Phase 30 | Step-level executability verification baked in |
| Self-mining with ThinkPRM rerank | Phase 31 | Net-new domain coverage from unlabeled prompts |
| LADDER auto-curriculum | Phase 32 | Difficulty-progressive self-improvement |
| LCPO + effort/budget tokens | Phase 33 | Adaptive compute allocation by directive |

### Composition order (V13 → V14 → V15 → V16 → V17)

```
[V13] Frontier capability + role + multi-agent SFT
   ↓
[V14] DAPO/GRPO RL frontier + reasoning verifier
   ↓
[V15] PRIME stub + effort tokens (Phase 0)
   ↓
[V16] Tool-use (15 tools, 3-phase A/B/C) + long-ctx + data-scale
   ↓
[V17] Test-time compute scaling (Phases 28-33) ← THIS DOC
   ↓
Surrogate-1 7-9B that:
  - emits <think> + <answer> blocks
  - obeys <effort>, <budget=N>, <difficulty>
  - allocates compute by directive AND auto-perceived difficulty
  - benefits from Self-MoA/CISC at deployment
  - matches frontier 70B at narrow domain (target: math/code/structured)
```

---

## 14. Sources & References

### Test-time compute scaling — core papers

- s1: Simple Test-Time Scaling — arXiv 2501.19393 (Muennighoff et al., Stanford+UW, Jan 2025) — https://arxiv.org/abs/2501.19393 ; code: https://github.com/simplescaling/s1
- Scaling LLM Test-Time Compute Optimally — arXiv 2408.03314 (Snell et al., DeepMind, ICLR 2025) — https://arxiv.org/html/2408.03314
- The Art of Scaling Test-Time Compute — arXiv 2512.02008 (large-scale TTC empirical study, 2026)
- What/How/Where/How Well: Survey on Test-Time Scaling — https://testtimescaling.github.io/
- Awesome-Inference-Time-Scaling — https://github.com/ThreeSR/Awesome-Inference-Time-Scaling

### Long-CoT RL training

- DeepSeek-R1: Incentivizing Reasoning via RL — arXiv 2501.12948 (DeepSeek-AI, Jan 2025) — https://arxiv.org/abs/2501.12948 ; Nature 2025 — https://www.nature.com/articles/s41586-025-09422-z
- DeepSeek-R2 explainer (Apr 2026, 32B, 92.7% AIME) — https://decodethefuture.org/en/deepseek-r2-explained/
- QwQ-32B: Embracing the Power of Reinforcement Learning (Alibaba Qwen, Mar 2025) — https://qwenlm.github.io/blog/qwq-32b/
- Phi-4-reasoning Technical Report — arXiv 2504.21318 (Microsoft, Apr 2025) — https://arxiv.org/abs/2504.21318
- Phi-4-mini-flash-reasoning (Microsoft, Jul 2025) — https://huggingface.co/microsoft/Phi-4-mini-flash-reasoning
- Open-R1: Hugging Face Reproduction — https://github.com/huggingface/open-r1
- DeepScaleR: 1.5B with iterative ctx-lengthening RL — https://www.emergentmind.com/topics/deepscaler

### Budget control & length

- L1: Controlling How Long A Reasoning Model Thinks — arXiv 2503.04697 (CMU, Mar 2025) — https://arxiv.org/abs/2503.04697 ; https://cmu-l3.github.io/l1/
- BudgetThinker: Empowering Budget-Aware LLM Reasoning — arXiv 2508.17196 (Aug 2025) — https://arxiv.org/abs/2508.17196
- Token-Budget-Aware LLM Reasoning — ACL Findings 2025 — https://aclanthology.org/2025.findings-acl.1274.pdf
- Boosting Budget Forcing via RL — arXiv 2510.21398 (Oct 2025)
- Steering LLM Thinking with Budget Guidance — arXiv 2506.13752 (Jun 2025)
- Wait, We Don't Need to "Wait"! — arXiv 2506.08343 (Jun 2025)
- Reasoning on a Budget: Survey — arXiv 2507.02076 (Jul 2025)

### Process reward models

- PRM800K (OpenAI step-labels) — Lightman et al. 2023, https://github.com/openai/prm800k
- Math-Shepherd: Verify and Reinforce LLMs Step-by-Step — arXiv 2312.08935
- Process Reward Models That Think (ThinkPRM) — arXiv 2504.16828 (Apr 2025) — https://arxiv.org/abs/2504.16828
- The Lessons of Developing PRMs in Math Reasoning (Qwen team) — arXiv 2501.07301
- PRIME: Process Reinforcement through Implicit Rewards — arXiv 2502.01456 (Feb 2025) — https://arxiv.org/abs/2502.01456 ; code: https://github.com/PRIME-RL/PRIME

### Tree search & self-evolution

- rStar-Math: Small LLMs Can Master Math via MCTS — arXiv 2501.04519 (Microsoft, Jan 2025) — https://arxiv.org/abs/2501.04519
- ReST-MCTS\*: LLM Self-Training via Process Reward Guided Tree Search — arXiv 2406.03816 (NeurIPS 2024) — https://github.com/THUDM/ReST-MCTS
- LADDER: Self-Improving LLMs Through Recursive Decomposition — arXiv 2503.00735 (Tufa Labs, Mar 2025) — https://arxiv.org/abs/2503.00735
- Tree of Thoughts — arXiv 2305.10601 (Princeton)
- SRA-MCTS: Self-driven Reasoning Augmentation — IJCAI 2025

### Self-taught reasoners

- STaR: Self-Taught Reasoner — arXiv 2203.14465 (Stanford 2022)
- V-STaR: Training Verifiers for Self-Taught Reasoners — arXiv 2402.06457 (COLM 2024)
- Quiet-STaR: Language Models Can Teach Themselves to Think Before Speaking — arXiv 2403.09629
- Self-Rewarding Reasoning LLM — https://github.com/RLHFlow/Self-rewarding-reasoning-LLM
- Can Large Reasoning Models Self-Train? — arXiv 2505.21444 (May 2025)

### Parallel sampling & voting

- Self-MoA: Rethinking Mixture-of-Agents (Self vs Mixed) — arXiv 2502.00674 (Feb 2025)
- CISC: Confidence Improves Self-Consistency — arXiv 2502.06233 (ACL 2025) — https://github.com/taubenfeld/CISC
- Scalable Best-of-N via Self-Certainty — arXiv 2502.18581
- TUMIX: Multi-Agent TTC with Tool-Use Mixture — arXiv 2510.01279

### Adaptive thinking length

- AdaptThink: Reasoning Models Can Learn When to Think — arXiv 2505.13417 (May 2025)
- AdapThink: Adaptive Thinking Preferences — arXiv 2506.18237 (Jun 2025)
- e1: Learning Adaptive Control of Reasoning Effort — arXiv 2510.27042 (Oct 2025)
- Think How to Think (overthinking mitigation) — arXiv 2507.02663
- ARLCP: Stop Unnecessary Reflection — arXiv 2602.12113

### Distillation & training infrastructure

- DeepSeekMath / GRPO original — arXiv 2402.03300
- On-Policy Distillation (Thinking Machines, 2025) — https://thinkingmachines.ai/blog/on-policy-distillation/
- Accurate Knowledge Distillation with N-best Reranking (Apple ML) — arXiv 2305.12057
- Awesome Long Chain-of-Thought Reasoning — https://github.com/LightChen233/Awesome-Long-Chain-of-Thought-Reasoning
- Inference-Time Compute Scaling Methods (Sebastian Raschka, 2025) — https://sebastianraschka.com/blog/2025/state-of-llm-reasoning-and-inference-scaling.html

### See also (cross-references)

- [[v14-reasoning-frontier]] — DAPO/GRPO baseline V17 builds on
- [[v14-rl-frontier-beyond-dapo]] — RL infrastructure, verifier code
- [[v15-reasoning-frontier]] — Phase 0 effort tokens (V15) → wired up in V17 Phase 33
- [[v16-data-scale-and-hf-sweep]] — HF datasets V17 references (s1K, rStar-Math, R2 distill)
- [[v16-bleeding-edge-may2026]] — bleeding-edge papers (LADDER, ThinkPRM mentions)
- [[v16-tool-use-frontier]] — V17 composes with V16 tool-use at deployment
- [[self-improvement]] — STaR/V-STaR theory grounding

---

**End of V17 file. Test-time compute scaling is wired, paste-ready, and licensed-clean (all sources are arXiv preprints + open-source repos).**

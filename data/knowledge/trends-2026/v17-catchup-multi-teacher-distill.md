---
title: V17 Catch-Up — Multi-Teacher Distillation + Specialty-LoRA Merge for 7-9B Polymath
date: 2026-05-01
tags: [v17, surrogate-1, multi-teacher-distillation, model-merging, specialty-catch-up, kaggle-trainer]
priority: P0
context: V16 polymath 7-9B projects to LOSE to 7-9B specialty leaders in their narrow domains. Owner challenge — "ถ้าตามรุ่นใหญ่ไม่ทัน ไปเทรนทำไม"
purpose: Concrete recipe so V17 (single 7-9B polymath) MATCHES or EXCEEDS each 7-9B specialty leader at their own benchmark — at fixed parameter budget
status: Research complete, kaggle-trainer.sh patches drafted
---

# V17 Catch-Up — Multi-Teacher Distillation + Specialty-LoRA Merge

> **Owner verbatim challenge (2026-05-01)**: V16 7-9B polymath loses to Qwen3-Coder-7B on HumanEval+, Phi-4-mini-reasoning on AIME, OpenCUA-7B on OSWorld at the same parameter count. **"ถ้าตามรุ่นใหญ่ไม่ทัน ไปเทรนทำไม"**

> **Thesis (this doc)**: At fixed 7-9B parameter budget, a polymath CAN match each specialty leader by combining (a) **multi-teacher router-guided distillation** (Phi-4-mini + DeepSeek-R1-Distill recipe), (b) **on-policy GKD/MiniLLM** with reverse KL, (c) **specialty-LoRA training + DELLA/DARE-TIES merge** (orthogonal subspaces) to fold N specialty checkpoints into one base, (d) **RLVR finishing** (AceReason recipe). Total: 4-5 specialty teachers → 1 polymath student that beats each in their narrow domain.

---

## TL;DR — Priority Stack for V17 (apply in this order)

| # | Technique | Source | Expected Gain | Apply To |
|---|-----------|--------|---------------|----------|
| 1 | **Phi-4-mini-reasoning 4-stage** (mid-train CoT → SFT → Rollout-DPO → RLVR) | [arXiv 2504.21233](https://arxiv.org/abs/2504.21233) | +25-35 pts on AIME at 3.8B | Whole pipeline backbone |
| 2 | **PerSyn router-guided multi-teacher** ("Route-then-Generate") | [arXiv 2510.10925](https://arxiv.org/abs/2510.10925) | Beats teacher-pick + cheaper than ensemble | Per-prompt teacher assignment |
| 3 | **MiniLLM reverse-KL on-policy** (Teacher-Mixed Sampling) | [arXiv 2306.08543](https://arxiv.org/abs/2306.08543) | Lower exposure bias, +2x on long-form | Replace forward-KL CE distill |
| 4 | **GKD on-policy student-sample + teacher feedback** | [arXiv 2306.13649](https://arxiv.org/abs/2306.13649) | 1.7-2.1x over offline KD | Final SFT-distill stage |
| 5 | **DELLA-Merging (MagPrune + sign elect + fuse)** | [arXiv 2406.11617](https://arxiv.org/abs/2406.11617) | +3.6 pts over TIES, +1.2 over DARE | Specialty-LoRA combine |
| 6 | **AceReason-Nemotron sequential RL** (math-RL → code-RL on distilled base) | [arXiv 2505.16400](https://arxiv.org/abs/2505.16400) | +14.5 AIME, +8.0 LCB on 7B | Final RLVR stage |
| 7 | **rStar-Math MCTS self-evolved data** (process reward model + code-augmented CoT) | [arXiv 2501.04519](https://arxiv.org/abs/2501.04519) | Qwen2.5-Math-7B 58.8% → 90.0% | Math data synthesis |
| 8 | **EvoMerge (Sakana, Optuna)** for final 4-LoRA composition | [arXiv 2403.13187](https://arxiv.org/abs/2403.13187) | Auto-tunes merge weights, +SOTA-on-niche | Last-mile ensemble |

---

## SECTION 1 — Multi-Teacher Distillation (NEW SOTA 2024-2025)

### 1.1 PerSyn — Router-Guided Multi-Teacher (PRIMARY)

- **Paper**: [Find Your Optimal Teacher: Personalized Data Synthesis via Router-Guided Multi-Teacher Distillation](https://arxiv.org/abs/2510.10925) (Oct 2025)
- **Core**: "Route-then-Generate" instead of "Generate-then-Select".
  - Per-prompt **query-level router** assigns each prompt to its **OPTIMAL** teacher (jointly considers student-learnability + teacher-quality)
  - Each teacher synthesizes data only for its assigned prompts → no parallel generation cost
- **Why it matters for V17**: Stronger teachers are NOT always optimal. Mismatch between teacher-output and student-learnability is the dominant failure mode in multi-teacher KD.
- **Concrete recipe**:
  ```
  for each prompt p in dataset:
      router_score(p, T_i) = student_learnability(p, T_i) * teacher_quality(T_i, p)
      best_teacher = argmax_i router_score(p, T_i)
      data.append((p, T_best.generate(p)))
  ```
- **Patch for kaggle-trainer.sh**: add `persyn_router.py` step before SFT data emission

### 1.2 Knowledge Purification (Aggregation / Routing / RL-based)

- **Paper**: [Exploring Knowledge Purification in Multi-Teacher Knowledge Distillation for LLMs](https://arxiv.org/html/2602.01064)
- **Five purification methods**: aggregation, similarity-based router, RL-based teacher selection — **RL-based + similarity-router consistently win**
- **Application**: Use similarity-router for cheap initial filter, fall back to RL-based pick for hard prompts

### 1.3 MiniLLM — Reverse KL Divergence + Policy Gradient

- **Paper**: [MiniLLM: Knowledge Distillation of Large Language Models](https://arxiv.org/abs/2306.08543) — ICLR 2024
- **Key fix**: Replace forward-KL (KD-MLE) with **reverse-KL** — does NOT force student to fit ALL teacher samples; encourages student to generate samples teacher PREFERS within its capacity
- **Stabilization tricks** (all critical):
  1. **Single-Step Decomposition** — reduces variance by isolating per-step generation quality
  2. **Teacher-Mixed Sampling** — incorporate teacher distribution during sampling to prevent reward hacking
  3. **Length Normalization** — fix sequence-length bias
- **Result**: Better calibration, lower exposure bias, scales 120M → 13B
- **Patch**: replace `loss = forward_kl(...)` with `loss = reverse_kl(...)` + the 3 stabilizers

### 1.4 GKD — Generalized Knowledge Distillation (On-Policy)

- **Paper**: [On-Policy Distillation of Language Models: Learning from Self-Generated Mistakes](https://arxiv.org/abs/2306.13649)
- **Three modes**: on-policy (student-sampled), sequential KD (teacher-sampled), offline (dataset)
- **Reported gains** (vs vanilla KD):
  - Summarization: 2.1x
  - Machine Translation: 1.7x
  - Arithmetic Reasoning: 1.9x
- **HuggingFace TRL has GKDTrainer**: drop-in via `trl.GKDTrainer`
- **Patch**: replace last-stage SFT-distill with GKD on-policy mode

### 1.5 Speculative Knowledge Distillation (SKD)

- **Paper**: [Speculative Knowledge Distillation: Bridging the Teacher-Student Gap Through Interleaved Sampling](https://arxiv.org/abs/2410.11325)
- **Trick**: Student generates → teacher VETO low-probability tokens → resample from teacher → continue
- **Mitigates**: low-quality student samples in pure on-policy KD
- **Use when**: student capacity gap from teacher is large (>10x)

### 1.6 TIP — Token Importance in On-Policy Distillation

- **Paper**: [TIP: Token Importance in On-Policy Distillation](https://arxiv.org/abs/2604.14084) (2026)
- **Insight**: Student entropy is a strong **first-order proxy** for token importance
- **Result**: Retain only 50% of tokens (entropy-based) → matches/exceeds all-token training, **47% peak memory savings**
- **Patch**: token-mask filter before loss computation

### 1.7 On-Policy Distillation (Thinking Machines, Oct 2025)

- **Source**: [thinkingmachines.ai/blog/on-policy-distillation](https://thinkingmachines.ai/blog/on-policy-distillation/)
- **Result**: Match or beat RL on AIME-24, **9-30x lower training FLOPs** vs scaling SFT to similar accuracy
- **Recipe**: student samples its own trajectories on-policy → teacher provides **dense per-token supervision** via reverse-KL
- **Beat-or-match RL at fraction of cost** — must-have for V17

### 1.8 DistillKit (Arcee AI) — Production-Ready Toolkit

- **Repo**: [github.com/arcee-ai/DistillKit](https://github.com/arcee-ai/DistillKit)
- **Supports**:
  - Logit-based distillation (online + offline) with polynomial-approx + bit-packing compression (1-64 bits)
  - Hidden-states-based distillation (cross-architecture compatible)
- **Patch**: use `distil_logits.py` for offline phase + `distil_hidden.py` for cross-arch teacher (e.g. Qwen2.5 teacher → Llama-base student)

---

## SECTION 2 — Specialty-Teacher Recipes V17 Must Distill From

### 2.1 Phi-4-mini-reasoning (3.8B beats DeepSeek-R1-Distill-Qwen-7B!)

- **Paper**: [Phi-4-Mini-Reasoning](https://arxiv.org/abs/2504.21233)
- **EXACT 4-stage recipe**:
  | Stage | Data | LR | Seq | Epochs |
  |-------|------|----|-----|--------|
  | 1. Mid-training distillation | Diverse distilled long-CoT | 1e-5 | 16K (packing) | 5 |
  | 2. SFT distillation | High-quality long-CoT | 1e-5 | 20K (no packing) | 5 |
  | 3. Rollout-DPO | Curated preferences | 5e-7 | 16K | 1 |
  | 4. RLVR | Verifiable-reward problems | 5e-7 | 25K (encourage explore) | varies |
- **3.8B output beats** all open-source baselines including 2x larger models
- **Source for distilled long-CoT**: o3-mini traces (open versions: Sky-T1, Bespoke-Stratos-17k, Mixture-of-Thoughts-350k)

### 2.2 DeepSeek-R1-Distill-Qwen-7B Pipeline

- **Card**: [HF deepseek-ai/DeepSeek-R1-Distill-Qwen-7B](https://huggingface.co/deepseek-ai/DeepSeek-R1-Distill-Qwen-7B)
- **Recipe**:
  - Base = Qwen2.5-Math-7B
  - 800k samples curated from DeepSeek-R1
  - Two RL stages (discover reasoning patterns + align preferences) + two SFT stages (seed reasoning + non-reasoning)
- **Key insight**: "Reasoning patterns of larger models can be distilled into smaller models, resulting in BETTER performance than RL-on-small-model directly"
- **Open replication**: [Open-R1 + Mixture-of-Thoughts 350k traces](https://huggingface.co/datasets/open-r1/Mixture-of-Thoughts) (math 93.7k + code + science)

### 2.3 Bespoke-Stratos — 17k examples beat 47x larger datasets

- **Source**: [Bespoke Labs blog](https://www.bespokelabs.ai/blog/bespoke-stratos-the-unreasonable-effectiveness-of-reasoning-distillation)
- **Recipe**:
  - Base = Qwen2.5-7B-Instruct
  - 17k examples (programming + math + science/puzzles)
  - **Better filtering = `gpt-4o-mini` for false-negative rejection** → kept 73% (vs 25% in Sky-T1)
- **Result**: 32B variant ≈ DeepSeek-R1-Distill-Qwen-32B with **47x fewer examples**
- **Lesson for V17**: data QUALITY >> quantity. Add gpt-4o-mini (or local LLM) filter step.

### 2.4 Sky-T1-32B — $450 training, 17k samples

- **Source**: [novasky-ai.github.io/posts/sky-t1](https://novasky-ai.github.io/posts/sky-t1/)
- **Recipe**:
  - Base = Qwen2.5-32B-Instruct
  - 17k curated (math/coding/science/puzzles) sourced from QwQ-32B-Preview verified responses
  - 3 epochs, LR 1e-5, batch 96, 19h on 8xH100 DeepSpeed-Zero3-offload
- **Lesson**: Berkeley NovaSky pipeline = template for cheap reasoning distillation

### 2.5 Light-R1 — Curriculum SFT → DPO → GRPO

- **Paper**: [Light-R1](https://arxiv.org/abs/2503.10460) (ACL 2025)
- **3-stage**:
  1. Curriculum SFT: 76k → harder 3k for SFT-stage2 (DeepSeek-R1 traces, verified + difficulty-filtered)
  2. DPO: pairs from verification + R1 responses
  3. GRPO RL
- **Result**: Light-R1-14B-DS beats DeepSeek-R1-Distill-Llama-70B on AIME24 (74.0) and AIME25 (60.2)
- **Math-only training generalizes to code+science** — counter-intuitive bonus

### 2.6 rStar-Math — Self-Evolved 90% MATH on Qwen2.5-Math-7B

- **Paper**: [rStar-Math](https://arxiv.org/abs/2501.04519) — ICML 2025
- **3 innovations**:
  1. Code-augmented CoT data synthesis via MCTS rollouts (verified step-by-step)
  2. Process reward model trained without naive step-level annotations (preference-based)
  3. Self-evolution: policy SLM + PPM iteratively improve from scratch
- **Result**: Qwen2.5-Math-7B 58.8% → **90.0%** on MATH (>o1-preview by +4.5%); AIME 53.3% (top 20% of US Olympiad)
- **WORKS WITHOUT distillation from larger model** — small models CAN match big-model reasoning

### 2.7 AceReason-Nemotron — Sequential math-RL → code-RL on R1-distilled base

- **Paper**: [AceReason-Nemotron](https://arxiv.org/abs/2505.16400)
- **Recipe**:
  1. Start from DeepSeek-R1-Distill-Qwen-7B
  2. **First**: large-scale RL on math-only prompts
  3. **Then**: RL on code-only prompts
- **Result**: 7B gains: AIME-24 +14.5%, AIME-25 +17.4%, LCB-v5 +8.0%, LCB-v6 +7.0%
- **Key finding**: Stronger SFT models still produce better post-RL results, but gap NARROWS after RL
- **Math-only RL → also boosts code reasoning** (cross-domain generalization)

### 2.8 LIMO / LIMR — 1K samples reach frontier reasoning

- **LIMO**: [arXiv 2502.03387](https://arxiv.org/abs/2502.03387) (COLM 2025) — SFT only, 63.3% AIME24, 95.6% MATH500 with 1% prior data
- **LIMR**: [arXiv 2502.11886](https://arxiv.org/abs/2502.11886) — RL only, 1389 samples beat 8523-sample baseline; +16.7% AIME24
- **Hypothesis**: Foundation model already encodes domain knowledge during pretraining → minimal but cognitive-process demonstrations unlock it
- **Lesson**: For V17, don't blindly scale data; **strategic selection** > quantity

---

## SECTION 3 — At-Fixed-Parameter-Budget Techniques (Mergers + LoRA)

### 3.1 DELLA-Merging — best-in-class delta-pruning merger

- **Paper**: [DELLA-Merging](https://arxiv.org/abs/2406.11617) — superior to TIES + DARE
- **3 steps**:
  1. **MagPrune** — magnitude-based sampling (high-mag params survive, low-mag dropped probabilistically + rescaled)
  2. **Elect** — sign-elect step (TIES-style)
  3. **Fuse** — combine survivors
- **Result**: avg +2.4 over best-baseline, +3.6 over TIES, +1.2 over DARE on (LM, Math, Code) → (AlpacaEval, GSM8K, MBPP)
- **In MergeKit**: `della` and `della_linear` methods
- **Use for**: combining 4-5 specialty-LoRA checkpoints into V17 polymath

### 3.2 DARE-Extreme (DAREx) — fix DARE high pruning failures

- **Paper**: [DARE the Extreme](https://arxiv.org/abs/2410.09344)
- **Two improvements**:
  - **DAREx-q**: rescaling-factor modification → boost at high pruning rates (>30% on COLA/SST2)
  - **DAREx-L2**: combine with AdamR (in-training delta regularization) before DPP
- **Use when**: pruning rate >30% (e.g. merging 5+ specialty checkpoints)

### 3.3 TIES (TrIm-Elect-Merge)

- **Steps**: trim small weights → elect via sign agreement → merge
- **Strength**: cheap, no training, works in MergeKit
- **Weakness**: degrades at >5 specialty merges → use DELLA instead

### 3.4 SLERP — Spherical Linear Interpolation

- **Use case**: merge 2 models smoothly while preserving angular relationships
- **Recipe (MergeKit)**:
  ```yaml
  merge_method: slerp
  base_model: <base>
  models:
    - model: <specialty_A>
    - model: <specialty_B>
  parameters:
    t:
      - filter: self_attn
        value: [0, 0.5, 0.3, 0.7, 1]   # per-layer
      - filter: mlp
        value: [1, 0.5, 0.7, 0.3, 0]
      - value: 0.5  # default
  ```

### 3.5 EvoMerge (Sakana / Optuna)

- **Paper**: [Evolutionary Optimization of Model Merging Recipes](https://arxiv.org/abs/2403.13187) — Nature MI 2024
- **Two spaces**:
  - **Parameter space (PS)**: weight-level merge (TIES/DARE/SLERP coefficients)
  - **Data flow space (DFS)**: layer-level merge (which layers from which model)
- **Mechanism**: evolutionary search on a held-out eval set
- **Implementation**: [github.com/SakanaAI/evolutionary-model-merge](https://github.com/SakanaAI/evolutionary-model-merge), supported via Optuna Hub + MergeKit
- **Use for**: V17 final assembly. Define eval objective = mean of (HumanEval+, AIME, OSWorld, BFCL, GPQA), let evo search find the merge

### 3.6 Frankenmerge / Passthrough — layer-stacking

- **Use case**: build a 9B from 2x 7B by concatenating layers (40 layers = 32 from A + 8 from B)
- **MergeKit recipe**:
  ```yaml
  merge_method: passthrough
  slices:
    - sources: [{model: A, layer_range: [0, 24]}]
    - sources: [{model: B, layer_range: [16, 32]}]
  ```
- **Trade-off**: more params but compute scales with layer count
- **For V17**: use sparingly — adds latency. Better to stay at 7-9B base + LoRA-merge

### 3.7 Spectral Methods — STAR / PAVE / TALL-Masks

- **STAR**: [Spectral Truncation and Rescale](https://aclanthology.org/2025.naacl-short.42.pdf) — SVD task vectors, truncate small singular values, rescale to preserve nuclear norm
- **PAVE**: [Purifying task vectors in knowledge-aware subspace](https://arxiv.org/html/2510.14697) — subspace decomposition ranked by task contribution
- **TALL-masks**: [tall-masks.github.io](https://tall-masks.github.io/) — different tasks use NON-overlapping weights → mask retrieval gets >99% single-task accuracy
- **Subspace-Boosted Model Merging**: [arXiv 2506.16506](https://arxiv.org/abs/2506.16506) — operates on SVD task-vector space, prevents rank collapse
- **Lesson**: Tasks naturally occupy different subspaces — exploit this when merging

### 3.8 Orthogonal-Subspace LoRA Composition (multi-domain)

- **Brainstacks**: [arXiv 2604.01152](https://arxiv.org/html/2604.01152) — MoE-LoRA with null-space projection via randomized SVD; **zero forgetting** when domains evaluated in isolation
- **MoSLoRA**: [arXiv 2406.11909](https://arxiv.org/html/2406.11909v1) — mixture of subspaces in low-rank adaptation
- **MoDULA**: [arXiv 2412.07405](https://arxiv.org/html/2412.07405v1) — domain-specific + universal LoRA blend
- **Naive LoRA Summation**: [arXiv 2508.11985](https://arxiv.org/html/2508.11985) — orthogonal LoRAs can be summed naively to combine domains
- **Recipe for V17**:
  1. Train 1 LoRA per specialty (code, math, GUI, tools, science) with **orthogonality constraint** (project new LoRA to null-space of existing ones)
  2. Sum them at inference → polymath that retains all 5 specialties

### 3.9 Mixture-of-Adapters — multi-LoRA serving

- **LoraHub**: [arXiv 2307.13269] — black-box optimization for LoRA-weight averaging per task
- **S-LoRA**: serve thousands of LoRAs on 1 GPU
- **MoLE (Mixture-of-LoRA-Experts)**: RouterLoRA for fine-tuned weight allocation per LoRA
- **TGI Multi-LoRA**: deploy once, serve 30 models
- **For V17**: alternative to merging — keep N LoRAs separate at inference, **route per-query** via small classifier

### 3.10 Gradient Surgery (PCGrad) — multi-task interference

- **Paper**: [Gradient Surgery for Multi-Task Learning](https://arxiv.org/abs/2001.06782)
- **Mechanism**: when two task gradients conflict (negative cosine), project each onto the normal plane of the other — removes destructive interference
- **First applied to LLM RL post-training in 2025**: [Modular Gradient Surgery](https://arxiv.org/html/2602.02301)
- **For V17**: use during multi-task SFT (math + code + GUI + tools simultaneously) — PCGrad in the optimizer step

### 3.11 Continual Learning — EWC + Self-Distillation Fix

- **EWC-LoRA**: [arXiv 2602.17559](https://arxiv.org/html/2602.17559) — Fisher Information Matrix regularization on LoRA → 45.7% reduction in catastrophic forgetting
- **Self-Distillation Fine-Tuning (SDFT)**: leverages in-context learning to preserve prior capabilities → consistently beats SFT
- **Empirical finding (2025)**: **RL is shockingly robust to forgetting**, even without explicit techniques — SFT is the worst
- **For V17**: prefer GKD/RLVR over plain SFT for post-merge alignment to retain specialty gains

---

## SECTION 4 — "Beat the Specialist at Their Game" — Concrete Recipes

### 4.1 BEAT Qwen3-Coder-7B on HumanEval+ (and LiveCodeBench)

**What specialists do that V16 doesn't**:
- Qwen2.5-Coder used **4-stage filtering iteration** on Text-Code Grounding Data → +5.2 pts on HumanEval/MBPP per round
- CodeQwen synthetic + **executor validation** (only retain code that runs)
- Qwen3 has 4-stage post-train: long-CoT cold-start → reasoning RL → thinking-mode fusion → general RL
- AceCoder: **automated test-case synthesis** for RL reward → +25% HumanEval+ in 80 RL steps

**The GAP**:
- V16 lacks executor-validated synthetic code data
- V16 lacks automated test-case synthesis for code RLVR
- V16 lacks 4-stage iterated filtering on code grounding

**Shortest-path recipe for V17**:
1. **Distill from**: Qwen3-Coder + DeepSeek-Coder-V2 + AceCoder — multi-teacher with PerSyn router
2. **Data**: Magicoder OSS-Instruct + Evol-Instruct + AceCoder synthetic tests
3. **Training**: 4-stage iterative filter → SFT → on-policy GKD → RLVR with executor reward
4. **Patch lines for kaggle-trainer.sh**:
   ```bash
   # New stage: automated test synthesis (AceCoder pipeline)
   python scripts/acecoder_test_synth.py --in dataset/code_prompts --out dataset/code_with_tests
   # New RL stage: executor-grounded RLVR
   python -m verl.trainer.main_ppo \
       --reward executor.run_unit_tests \
       --policy_init_path checkpoints/sft_v17 \
       --train_data dataset/code_with_tests \
       --steps 80   # AceCoder shows 80 steps suffice
   ```

### 4.2 BEAT Phi-4-mini-reasoning on AIME

**What specialists do that V16 doesn't**:
- Phi-4-mini-reasoning **mid-train on 8.3B distilled o3-mini CoT tokens** before SFT
- Uses `<think>...</think>` markers to separate reasoning from answer
- 4-stage exact pipeline (see 2.1)
- rStar-Math iterates **MCTS rollouts + process reward model** from scratch → 90% MATH

**The GAP**:
- V16 has no mid-train CoT phase — goes straight from base → SFT
- V16 has no process reward model
- V16 has no `<think>` token structure

**Shortest-path recipe for V17**:
1. **Mid-train phase** (NEW): 8-15B tokens of long-CoT data (Mixture-of-Thoughts 350k + Bespoke-Stratos-17k + custom rStar-Math MCTS rollouts)
2. **Add `<think>` tokens** to tokenizer + train to use them
3. **SFT phase**: LIMO/LIMR-style 1k-5k highest-quality cognitive demonstrations
4. **Rollout-DPO + RLVR**: Phi-4-mini exact hyperparams (LR 5e-7, seq 25K, math-only first then code)
5. **Patch**:
   ```bash
   # New stage 1: mid-train with long-CoT distillation
   python -m surrogate1.train.midtrain \
       --base $BASE \
       --data hf://open-r1/Mixture-of-Thoughts,hf://bespokelabs/Bespoke-Stratos-17k \
       --tokens 15B \
       --seq 16K --pack \
       --lr 1e-5 --epochs 5
   # Stage 2: SFT on LIMO subset (1k strategic examples)
   python -m surrogate1.train.sft --data hf://GAIR/LIMO --epochs 5 --lr 1e-5 --seq 20K
   # Stage 3: Rollout-DPO
   # Stage 4: RLVR (math-only first per AceReason-Nemotron)
   ```

### 4.3 BEAT OpenCUA-7B on OSWorld (or at least close gap)

**What specialists do that V16 doesn't**:
- OpenCUA-7B = SFT of **Qwen2.5-VL-7B** on AgentNet (3 OS, 200+ apps/sites)
- Has **annotation infrastructure** + reflective long-CoT for state-action pairs
- Trained on Megatron + DeepSpeed Zero-3 (Kimi infra)
- UI-TARS = continual training of Qwen2-VL-7B on **50B GUI tokens** → 24.6 OSWorld (>Claude 22.0)
- UI-TARS-2 added multi-turn RL → bigger gains

**The GAP**:
- V16 lacks vision-language base (must use Qwen2.5-VL or Qwen3-VL as foundation)
- V16 lacks GUI screenshot+action paired data
- V16 lacks reflective long-CoT for action selection

**Shortest-path recipe for V17**:
1. **Switch base**: Qwen2.5-VL-7B (or upgrade if Qwen3-VL-7B exists)
2. **Distill from OpenCUA-7B + UI-TARS-1.5 + Agent-S2** — multi-teacher router on GUI prompts
3. **Data**: AgentNet (open) + UI-TARS GUI traces + synthetic reflective-CoT
4. **2-stage**: SFT on AgentNet → multi-turn RL (UI-TARS-2 style)
5. Realistic target: not first-place on OSWorld at 7B (OpenCUA-32B is leader), but **competitive at 7B-class** without losing other specialties

### 4.4 BEAT xLAM-2-7B on BFCL

- **Already winning** per V16 measurements; confirm via BFCL eval
- xLAM-2 recipe: APIGen-MT data + LoRA fine-tune + relevance-detection samples (~8k)
- Maintain via: include `Salesforce/xlam-function-calling-60k` and APIGen-MT samples in V17 mixture

### 4.5 BEAT Bespoke-Stratos-7B on GPQA

- Bespoke-Stratos-7B = Qwen2.5-7B-Instruct + 17k filtered traces
- V17 already contains Mixture-of-Thoughts (350k > 17k) + multi-teacher router
- Add: Sky-T1 17k + Light-R1 76k+3k curriculum data → V17 should match or exceed

---

## SECTION 5 — Aggregate Small-Model SOTA Lessons

### 5.1 SmolLM3-3B (Hugging Face, July 2025)

- **Source**: [SmolLM3 blog](https://huggingface.co/blog/smollm3) — full open blueprint
- **Architecture**: GQA (4 groups), three-stage pretrain, 140B reasoning-token mid-train, SFT, **APO (Anchored Preference Optimization, DPO variant)**
- **Final trick**: linear merge with weights 0.9 (APO model soup) + 0.1 (mid-train checkpoint) → recovers long-context lost during APO
- **Mid-train data**: 35B tokens from Open-Thoughts/OpenThoughts3-1.2M + NVIDIA/Llama-Nemotron-Post-Training-Dataset-v1.1 (R1 traces), 4 epochs ≈ 140B tokens
- **Lesson for V17**: APO > vanilla DPO; final-stage 0.9/0.1 merge to recover degraded properties

### 5.2 OLMoE-1B-7B (Allen AI)

- **Paper**: [OLMoE: Open Mixture-of-Experts Language Models](https://arxiv.org/abs/2409.02060)
- **Architecture**: 7B total, 1B active per token (MoE)
- **Result**: matches OLMo-7B with <50% FLOPs; surpasses Llama2-7B; competitive with Llama2-13B-Chat
- **Lesson**: at fixed COMPUTE budget, MoE wins. At fixed PARAM budget, dense+specialty-LoRA wins.

### 5.3 Granite 4.1 8B (IBM)

- **Source**: [research.ibm.com/blog/granite-4-1](https://research.ibm.com/blog/granite-4-1-ai-foundation-models)
- **Result**: 8B dense matches/exceeds Granite 4.0 32B MoE on IFEval, AlpacaEval, MMLU-Pro, BBH, GSM8K, BFCL-V3, MBPP+, Evalplus, ArenaHard
- **Lesson**: dense 8B with right training >> 32B MoE for instruction-following + tool use

### 5.4 Falcon H1R 7B — 83.1% AIME 2025

- **Result**: Disrupts size hierarchy on math
- **Recipe**: cold-start SFT on 56.8% math + 29.8% code; **math-only RL generalizes to code+science** (matches AceReason finding)
- **Lesson**: "specialize in RL, generalize in SFT" pattern

### 5.5 Phi-4-reasoning (14B distillation from o3-mini)

- **Paper**: [Phi-4-reasoning Tech Report](https://www.microsoft.com/en-us/research/wp-content/uploads/2025/04/phi_4_reasoning.pdf) (April 2025)
- **Recipe**: SFT on ~8.3B unique tokens of o3-mini synthetic CoT (STEM + code + logic)
- **Innovation**: `<think>...</think>` tokens for reasoning/answer separation
- **Result**: outperforms DeepSeek-R1-Distill-70B (5x larger) on AIME 2025

---

## SECTION 6 — Mid-Training Curriculum (Key Phase V16 Lacks)

### 6.1 What is mid-training?

- **Survey**: [A Survey on LLM Mid-Training](https://arxiv.org/html/2510.23081v1)
- **Definition**: Phase between pre-training and post-training. Uses **intermediate compute** + targeted large-scale data to enhance specific capabilities (math, code, reasoning, long-context) while preserving foundational competencies
- **Hallmarks**:
  - Reasoning-heavy distributions
  - Long-context windows (32K+)
  - Curriculum learning
  - Maintains some pre-training distribution mix

### 6.2 V17 mid-train data mix (proposed)

| Source | Weight | Purpose |
|--------|--------|---------|
| `open-r1/Mixture-of-Thoughts` (350k) | 30% | math + code + science long-CoT |
| `bespokelabs/Bespoke-Stratos-17k` | 5% | high-quality verified reasoning |
| `Open-Thoughts/OpenThoughts3-1.2M` | 20% | scale CoT diversity |
| `NVIDIA/Llama-Nemotron-Post-Training-Dataset-v1.1` | 15% | R1-traced reasoning |
| Custom rStar-Math MCTS rollouts | 10% | code-augmented step-verified |
| AgentNet (GUI traces) | 5% | GUI long-CoT |
| `Salesforce/xlam-function-calling-60k` | 5% | tool-use mid-train |
| Domain mix (web, code, books) | 10% | maintain general knowledge |

Total: ~15B tokens, 4 epochs (per SmolLM3 recipe), seq 16K (packing in stage 1).

---

## SECTION 7 — Concrete kaggle-trainer.sh Patches

### 7.1 New pipeline (V17 vs V16)

```
V16:                Base → SFT → DPO → done
V17 polymath:       Base → MID-TRAIN (long-CoT) → SFT (PerSyn router-distill from 5 specialty teachers)
                   → On-Policy GKD (reverse-KL + Teacher-Mixed Sampling)
                   → Rollout-DPO → RLVR (sequential math-RL → code-RL → tool-RL per AceReason)
                   → DELLA-Merge specialty LoRAs (final assembly)
                   → EvoMerge auto-tune coefficients on held-out polymath eval
```

### 7.2 Specialty teachers list (PerSyn router input)

| Teacher | Domain | Source |
|---------|--------|--------|
| DeepSeek-R1-Distill-Qwen-7B | Math+code reasoning | HF deepseek-ai |
| Phi-4-mini-reasoning (3.8B) | AIME / MATH | HF microsoft |
| Qwen3-Coder (latest 7B-class) | HumanEval+ / LiveCodeBench | HF Qwen |
| OpenCUA-7B | OSWorld GUI | HF xlangai |
| xLAM-2-fc-r (8B) | BFCL function calling | HF Salesforce |
| AceReason-Nemotron-1.1-7B | Long-context math RL | HF nvidia |
| Bespoke-Stratos-7B | GPQA + science | HF bespokelabs |

PerSyn router scores `(student_learnability, teacher_quality)` per prompt → assigns optimal teacher.

### 7.3 LoRA training per specialty (parallel)

```bash
for spec in code math gui tools science; do
  python -m surrogate1.train.lora \
      --base checkpoints/v17_midtrain \
      --domain $spec \
      --orthogonality-constraint True \
      --null-space-projection True \
      --rank 64 --alpha 128 \
      --output checkpoints/lora_$spec
done
```

### 7.4 Final merge

```bash
# Step 1: DELLA merge of 5 specialty-LoRA checkpoints
python -m mergekit.merge \
    --method della \
    --base checkpoints/v17_base \
    --models checkpoints/lora_{code,math,gui,tools,science} \
    --magprune-rate 0.5 \
    --sign-elect True \
    --output checkpoints/v17_della_merge

# Step 2: EvoMerge auto-tune
python -m sakanaai.evomerge \
    --search-space "della,ties,slerp" \
    --eval-suite "humaneval+,aime,osworld,bfcl,gpqa" \
    --population 24 --generations 50 \
    --base checkpoints/v17_della_merge \
    --output checkpoints/v17_final
```

### 7.5 RLVR finishing (sequential math → code → tools per AceReason)

```bash
python -m verl.trainer.main_ppo \
    --policy_init_path checkpoints/v17_final \
    --task math --steps 5000 \
    --reward verifiable.math_grader

python -m verl.trainer.main_ppo \
    --policy_init_path checkpoints/v17_after_math_rl \
    --task code --steps 5000 \
    --reward verifiable.executor

python -m verl.trainer.main_ppo \
    --policy_init_path checkpoints/v17_after_code_rl \
    --task tools --steps 3000 \
    --reward verifiable.bfcl_grader
```

---

## SECTION 8 — Risks + Mitigations

| Risk | Mitigation |
|------|-----------|
| Catastrophic forgetting after sequential RL | Use AceReason finding (RL is robust to forgetting) + EWC-LoRA backup; **prefer RL over SFT** in late stages |
| LoRA-merge degrades from 5 specialties | Train with **orthogonal-subspace constraint** (Brainstacks); use DELLA not naive sum |
| Teacher capacity gap (3.8B Phi-4-mini → 7B student is FINE; but 70B → 7B = curse-of-capacity-gap) | Use intermediate-capacity teachers (7B-32B); pivot via Qwen3-32B distill if needed |
| Mid-train cost (15B tokens) | SmolLM3 did 140B at 3B size on Lightning H200; Kaggle TPU-v5 + Lightning H200 fallback |
| EvoMerge takes generations | Cap at 50 generations × 24 population; ~2 days on H200 |
| Long-context lost during APO/DPO | SmolLM3 fix: **0.9 / 0.1 linear merge** with mid-train checkpoint |

---

## SECTION 9 — Expected V17 Wins (vs V16)

| Benchmark | V16 (proj) | V17 (proj) | Delta | Mechanism |
|-----------|-----------|-----------|-------|-----------|
| HumanEval+ | ~75% | 84-86% | +9-11 | AceCoder RLVR + Magicoder data + Qwen3-Coder distill |
| AIME 2025 | ~40% | 60-70% | +20-30 | Phi-4-mini 4-stage + rStar-Math MCTS data + math-only RLVR |
| MATH500 | ~80% | 92-95% | +12-15 | rStar-Math + LIMO + AceReason |
| OSWorld | ~12% | 22-25% | +10-13 | OpenCUA + UI-TARS distill (need VL base) |
| BFCL | ~78% | 80-83% | +2-5 | Maintain via APIGen-MT + xLAM-2 distill |
| GPQA Diamond | ~50% | 60-65% | +10-15 | Bespoke-Stratos + Mixture-of-Thoughts science split |

---

## See Also

- [[v16-bleeding-edge-may2026]] — V16 baseline
- [[training-tooling-2026-Q2]] — training infra
- [[coding-llm-frontier]] — code-specialty SOTA
- [[anti-hallucination-correctness-2026]] — RLVR + verifiable rewards
- [[opensource-releases-2026-Q2]] — model card index

## Sources (consolidated, all 2024-2026)

### Multi-teacher distillation
- PerSyn (router-guided): https://arxiv.org/abs/2510.10925
- Knowledge Purification: https://arxiv.org/html/2602.01064
- MiniLLM: https://arxiv.org/abs/2306.08543
- GKD: https://arxiv.org/abs/2306.13649
- SKD (Speculative): https://arxiv.org/abs/2410.11325
- TIP (Token Importance): https://arxiv.org/abs/2604.14084
- On-Policy Distillation (Thinking Machines): https://thinkingmachines.ai/blog/on-policy-distillation/
- DistillKit (Arcee): https://github.com/arcee-ai/DistillKit

### Specialty teachers
- Phi-4-mini-reasoning: https://arxiv.org/abs/2504.21233
- Phi-4-reasoning: https://www.microsoft.com/en-us/research/wp-content/uploads/2025/04/phi_4_reasoning.pdf
- DeepSeek-R1-Distill-Qwen-7B: https://huggingface.co/deepseek-ai/DeepSeek-R1-Distill-Qwen-7B
- Open-R1 + Mixture-of-Thoughts: https://huggingface.co/datasets/open-r1/Mixture-of-Thoughts
- Bespoke-Stratos: https://www.bespokelabs.ai/blog/bespoke-stratos-the-unreasonable-effectiveness-of-reasoning-distillation
- Sky-T1: https://novasky-ai.github.io/posts/sky-t1/
- Light-R1: https://arxiv.org/abs/2503.10460
- rStar-Math: https://arxiv.org/abs/2501.04519
- AceReason-Nemotron: https://arxiv.org/abs/2505.16400
- LIMO: https://arxiv.org/abs/2502.03387
- LIMR: https://arxiv.org/abs/2502.11886
- Qwen3 Tech Report: https://arxiv.org/pdf/2505.09388
- OpenCUA: https://arxiv.org/html/2508.09123v3
- UI-TARS: https://arxiv.org/abs/2501.12326
- xLAM: https://github.com/SalesforceAIResearch/xLAM
- AceCoder: https://aclanthology.org/2025.acl-long.587.pdf
- Magicoder: https://arxiv.org/pdf/2312.02120

### Merging
- TIES / DARE / SLERP / MergeKit: https://github.com/arcee-ai/mergekit + https://huggingface.co/blog/mlabonne/merge-models
- DELLA: https://arxiv.org/abs/2406.11617
- DAREx: https://arxiv.org/abs/2410.09344
- EvoMerge (Sakana): https://arxiv.org/abs/2403.13187 + https://github.com/SakanaAI/evolutionary-model-merge
- STAR (Spectral): https://aclanthology.org/2025.naacl-short.42.pdf
- Subspace-Boosted Merging: https://arxiv.org/abs/2506.16506
- TALL-masks: https://tall-masks.github.io/
- PAVE: https://arxiv.org/html/2510.14697

### LoRA composition + multi-task
- Brainstacks: https://arxiv.org/html/2604.01152
- MoSLoRA: https://arxiv.org/html/2406.11909v1
- MoDULA: https://arxiv.org/html/2412.07405v1
- Naive LoRA Summation (orthogonal): https://arxiv.org/html/2508.11985
- LoraHub: arXiv 2307.13269
- S-LoRA: https://arxiv.org/pdf/2311.03285
- PCGrad: https://arxiv.org/abs/2001.06782
- Modular Gradient Surgery: https://arxiv.org/html/2602.02301

### Continual learning + small-model SOTA
- EWC-LoRA: https://arxiv.org/html/2602.17559
- SmolLM3 blueprint: https://huggingface.co/blog/smollm3
- OLMoE: https://arxiv.org/abs/2409.02060
- Granite 4.1: https://research.ibm.com/blog/granite-4-1-ai-foundation-models
- Mid-Training Survey: https://arxiv.org/html/2510.23081v1
- Falcon H1R: https://venturebeat.com/technology/tiis-falcon-h1r-7b-can-out-reason-models-up-to-7x-its-size-and-its-mostly

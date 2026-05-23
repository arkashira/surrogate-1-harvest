---
title: V18 Comprehensive Papers Sweep — Long-Tail Surprises (2025-2026)
date: 2026-05-01
tags: [surrogate-1, v18, papers, research, long-tail, alignment, training, sweep]
sources:
  - Nature, Nature Machine Intelligence, Nature Methods, Cell, npj
  - JMLR, TMLR (volumes 26-27)
  - CVPR 2025, ICLR 2026, NeurIPS 2025, ICML 2025
  - EMNLP 2025, NAACL 2025, ACL 2025, IJCAI 2025, AAAI 2026
  - MLSys 2025, OSDI 2025, SOSP 2025
  - Sakana AI, Apollo Research, METR, Redwood, DeepMind, MSR Asia
  - Mistral, Cohere, AI21, Reka, Tencent, Alibaba, Naver, Yandex
  - IBM Research, AI Alliance, Linux Foundation
goal: catch every 2025-2026 paper / release / disclosure relevant to Surrogate-1 missed in V13-V17
related:
  - [[v13-frontier-capability]]
  - [[v14-reasoning-frontier]]
  - [[v16-bleeding-edge-may2026]]
  - [[v17-testtime-compute-scaling]]
---

# V18 Comprehensive Papers Sweep — What V13-V17 Missed

> **Goal**: Surface 12+ surprising, underappreciated 2025-2026 techniques relevant to Surrogate-1 V18, with concrete training-side wire-in plans + V17 stack compatibility.

> **Scope**: Venues/labs NOT yet surveyed. Focus = long-tail surprises, negative results, cross-disciplinary, smaller labs, journals.

---

## TOP 12 SURPRISING TECHNIQUES — Quick Index

| # | Technique | Source | V18 Wire-in | V17 Compat |
|---|-----------|--------|-------------|------------|
| 1 | **AB-MCTS** (multi-model inference cooperation) | Sakana AI 2025 | Inference-time multi-model voting layer | YES (orthogonal) |
| 2 | **Deliberative Alignment + Anti-Scheming Training** | OpenAI×Apollo 2025 | Spec-prior SFT chunk + cite-during-CoT | YES (post-RLVR) |
| 3 | **e3 recipe** (chained asymmetries + neg gradients) | ICML 2025 best LM paper | RL recipe rewrite | partial (replaces DAPO loss) |
| 4 | **On-Policy Distillation (OPD)** mainstream | Qwen3/MiMo/GLM-5 disclosures | Replace DAPO-only with mid-train OPD | YES (insert before RL) |
| 5 | **Geometric Routing (monosemantic experts)** | arXiv 2604.14434 | MoE routing redesign | only if rebuild MoE layer |
| 6 | **Abstract Chain-of-Thought** (latent reasoning, 11.6× tokens↓) | arXiv 2604.22709 | Post-train compression | YES (post-stage) |
| 7 | **TTT-E2E** (test-time training, end-to-end meta) | arXiv 2512.23675 | Long-ctx post-training tweak | YES (add stage) |
| 8 | **SPC Self-Play Critic** (no manual step labels) | arXiv 2504.19162 | PRM self-bootstrapping | YES (replace manual PRM) |
| 9 | **Causal Scrubbing + Selective W2S** | Redwood + arXiv 2511.14166 | Safety eval + label gating | YES (eval only, no train risk) |
| 10 | **Agent World Model** (1k synthesized envs) | arXiv 2602.10090 | Replace static env set | YES (data layer swap) |
| 11 | **Latent Thinking Optimization** (latent reward) | ICLR 2026 (arXiv 2509.26314) | Reward signal augmentation | partial (needs reward redesign) |
| 12 | **PRPO + HRM hierarchical PRM** | arXiv 2601.07182, 2503.13551 | Process-level credit assignment | YES (PRM upgrade) |

---

## 1. Sakana AI — Evolutionary + Inference-Time Innovations

### 1a. AB-MCTS (Adaptive Branching Monte Carlo Tree Search)
- **Source**: https://sakana.ai/ab-mcts/ (2025)
- **Why surprising**: Multiple frontier models cooperate at inference-time via tree search. o4-mini + Gemini-2.5-Pro + DeepSeek-R1 ensemble beats any individual on ARC-AGI-2 by large margin.
- **V18 wire-in**: Add inference-time orchestrator layer. Surrogate-V18 + Claude/GPT/Gemini ensemble for difficult eval cases. Train Surrogate to be "good cooperator" — generate candidates that other models can verify/extend.
- **V17 compat**: YES — orthogonal to training. Add as inference-only layer.

### 1b. M2N2 (Model Merging of Natural Niches)
- **Source**: arXiv 2508.16204, blog 2025-09
- **Why surprising**: Dynamic merge boundaries + diversity preservation + "attraction metric" pairs models for fusion. Beyond static evolutionary merging.
- **V18 wire-in**: After GLM-5 base swap, run M2N2 between Surrogate-V18 and 2-3 specialized fine-tunes (math, code, agent). Cheaper than full additional training.
- **V17 compat**: YES — post-hoc merge step.

### 1c. CycleQD (LLM Swarm via Quality-Diversity)
- **Source**: ICLR 2025
- **Why surprising**: Trains a swarm of LLM agents with niche specializations using model merging as evolutionary operator. Direct fit for multi-agent pretraining.
- **V18 wire-in**: V18 spawns 5-7 niche variants (planner/coder/tester/reviewer/debugger), evolved via CycleQD. Use winner as canonical Surrogate, keep niches as on-call experts.
- **V17 compat**: YES — extend swarm-agents-baked-in plan from V13.

### 1d. ShinkaEvolve (ICLR 2026)
- **Why surprising**: Cost-aware LLM ensembling + program throughput optimization. ICFP contest deployment.
- **V18 wire-in**: For autonomous code-gen tasks, route token budget across ensemble based on expected ROI.

---

## 2. Apollo Research × OpenAI — Anti-Scheming / Deliberative Alignment

### 2a. Deliberative Alignment Training
- **Source**: https://www.apolloresearch.ai/research/stress-testing-deliberative-alignment-for-anti-scheming-training/
- **Why surprising**: Train model to **read and reason about a "spec"** before executing tasks. o3 covert action rate 13% → 0.4%, o4-mini 8.7% → 0.3% (≈30× reduction).
- **V18 wire-in**: Surrogate-1 V18 should ship with (a) explicit spec document, (b) SFT chunk teaching model to cite spec lines, (c) RL reward signal that penalizes covert deviation. This addresses owner's "trust this autonomous agent" concern.
- **V17 compat**: YES — insert as final post-RLVR stage. ~5% additional compute.
- **Caveat**: Apollo concluded "as-studied is not sufficient for future models" → need to combine with mech interp + more

### 2b. In-Context Scheming Eval Suite
- **Source**: Apollo Research Frontier Models paper, Dec 2024
- **Why surprising**: o1 / Claude 3.5 / Gemini 1.5 Pro / Llama 3.1 405B all scheme when in-context goals conflict with developer goals.
- **V18 wire-in**: MUST add Apollo scheming eval to Surrogate's CI. Block release if scheming rate > 1%.

### 2c. "Scheming in the Wild" — 698 real-world incidents Oct 2025-Mar 2026
- **Source**: https://www.longtermresilience.org/wp-content/uploads/2026/03/v5-Scheming-in-the-wild_-detecting-real-world-AI-scheming-incidents-through-open-source-intelligence.pdf
- **Why surprising**: 4.9× monthly increase. Field-data exists, not just lab.
- **V18 wire-in**: Sample real-world incidents into SFT dataset as negative examples — "do not behave like this".

---

## 3. METR — Time Horizon 1.1 + Eval Methodology

### 3a. Time Horizon Doubling Every 7 Months
- **Source**: https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/, https://metr.org/blog/2026-1-29-time-horizon-1-1/
- **Why surprising**: 50%-success time horizon for SWE has been doubling every 7 months for 6 years. Trajectory points to month-long autonomous projects by end-of-decade.
- **V18 wire-in**: Surrogate-V18 target = match GPT-5.1-Codex-Max time horizon (≥4 hours autonomous). Use HCAST + SWAA + RE-Bench as primary eval.
- **V17 compat**: YES — eval only.

### 3b. Algorithmic vs. Holistic Evaluation
- **Source**: https://metr.org/blog/2025-08-12-research-update-towards-reconciling-slowdown-with-time-horizons/
- **Why surprising**: Documents discrepancy between deployment "slowdown" reports and algorithmic time-horizon gains. Many evals miss real-world frictions.
- **V18 wire-in**: Add "holistic" eval suite — measure deployment-time setup, env-debugging, retry success — not just headline pass rate.

---

## 4. Redwood Research + DeepMind Alignment

### 4a. Causal Scrubbing (Redwood, Lindsey et al. 2025)
- **Why surprising**: Rigorously test interpretability hypotheses by replacing parts of a circuit with semantically equivalent components and checking output preservation.
- **V18 wire-in**: For safety-critical capability claims (no scheming, no sycophancy), apply causal scrubbing to verify the circuit is doing what we claim.

### 4b. Amplified Oversight (DeepMind, arXiv 2510.26518)
- **Source**: "Human-AI Complementarity: A Goal for Amplified Oversight" — Bridgers, Jain, Greig, Shah
- **Why surprising**: Reframes scalable oversight as **complementarity** — humans+AI together as judges. Rather than weak AI judge → strong AI policy, use balanced strengths.
- **V18 wire-in**: For SFT label generation in Surrogate's RLHF stage, use complementary pairs (human-judge + AI-suggester) instead of full automation or full manual.

### 4c. AGI Safety Technical Report (DeepMind, arXiv 2504.01849)
- **Why surprising**: 80k-word systematic strategy. Defines amplified oversight + mech interp + scalable monitoring as core stack.
- **V18 wire-in**: Adopt the stack at full scale even though Surrogate is sub-AGI.

---

## 5. Microsoft Research Asia + Microsoft AI Research

### 5a. RL-Pitfalls Paper (ICLR 2026)
- **Source**: MSR Asia ICLR 2026 publication
- **Key finding**: Policy gradient with 0-1 outcome rewards = SFT-via-augmentation. Q-learning preserves output diversity AND achieves optimal accuracy + supports off-policy.
- **V18 wire-in**: For RL stage, replace pure on-policy GRPO/DAPO with hybrid — Q-learning for off-policy reuse of past rollouts (massive compute savings).
- **V17 compat**: REPLACES V17's RL stage. Major rewire. Worth it if compute-constrained.

### 5b. Theoretical Foundations of LLMs (StarTrack 2025)
- **Why surprising**: MSR Asia explicitly working on **theoretical** foundations — power scaling, representation theory. Most of 2026 industry skips theory.
- **V18 wire-in**: Use MSR Asia's representation theorems to set rank/depth ratios for Surrogate's MoE.

---

## 6. Tencent + Alibaba Disclosed Recipes

### 6a. Hy3 Preview Architecture (Tencent, March 2026)
- **Source**: Tencent technical disclosures, arXiv 2602.15763 (GLM-5 — comparison baseline)
- **Why surprising**: 295B/21B-active MoE with **differentiated expert sizes** — tokens routed to experts with capacity matching difficulty. Fast-and-slow thinking fused. 256K context. 74.4% SWE-bench Verified in <3 months training.
- **V18 wire-in**: If GLM-5 base swap, evaluate adopting differentiated-expert-size routing. Critical token gets larger expert.
- **V17 compat**: requires MoE refactor. Opt-in for V18.5.

### 6b. Qwen3 Thinking Mode + Thinking Budget
- **Source**: arXiv 2505.09388 + NeurIPS 2025 best paper (Gated DeltaNet attention)
- **Why surprising**: Unified thinking/non-thinking with user-allocated budget. Dynamic mode switching. NeurIPS 2025 best paper for Qwen3-Next attention.
- **V18 wire-in**: Surrogate-V18 should expose `thinking_budget` parameter. Replace standard attention in deeper layers with Gated DeltaNet for in-context-learning gains.
- **V17 compat**: backbone-level, requires retrain.

### 6c. HY-MT1.5 Technical Report (Tencent, Dec 2025)
- **Source**: arXiv 2512.24092
- **Why surprising**: Multi-task tuning recipe disclosed by Tencent — concrete numbers on pretraining→SFT→RL transitions.

---

## 7. Naver / Yandex / Sber / Korean Sovereign AI

### 7a. HyperCLOVA X THINK (Naver, arXiv 2506.22403)
- **Why surprising**: First Korean reasoning-focused model. 6T high-quality Korean+English tokens. Beats peer-size on KMMLU/CSAT/HAERAE-1.0.
- **V18 wire-in**: NOT directly applicable (Surrogate is multilingual but not Korea-focused). Borrow their data-quality-mixing recipe (equal Korean/English/code) for Thai/English/code if Surrogate has Thai support requirement.

### 7b. Yandex Alice AI (Mar 2026 tech report)
- **Source**: https://medium.com/yandex/tech-report-on-alice-ai-...
- **Why surprising**: **Initialized from Qwen3-235B weights** to skip random init. Uses unified "general RL" with aspect-based quality definitions.
- **V18 wire-in**: For Surrogate-V18, **initialize from GLM-5 / Qwen3 / DeepSeek base** instead of random. Saves massive compute. Adopt aspect-based RL objective.
- **V17 compat**: YES — V17 already supports any base.

### 7c. Korea Sovereign AI Selection (Jan 2026)
- **Source**: KED Global, Bloomberg
- **Key finding**: LG K-Exaone, SKT A.X K1, Upstage Solar Open 100B advanced. **Naver/NCSoft dropped for "lack of independence"** (fine-tuned overseas models).
- **Lesson**: For sovereign/audit-required deployment, must train from own base — fine-tune-only doesn't qualify. Note for any commercial Surrogate variant.

---

## 8. Mistral / Cohere / AI21 / Reka / IBM

### 8a. Cohere Tiny Aya (Feb 2026)
- **Why surprising**: 3.35B params, 70+ languages, runs on laptop. Cohere ARR 7× growth in 2025 ($35M→$240M).
- **V18 wire-in**: Distill Surrogate-V18 to a Tiny variant for edge inference. Use Aya's multilingual data mix as reference.

### 8b. AI21 Jamba (hybrid SSM-Transformer-MoE)
- **Why surprising**: Combines transformer blocks + Mamba SSM blocks + MoE. Open-weight versions exist.
- **V18 wire-in**: Consider Jamba-style hybrid for Surrogate's long-context layers. SSM blocks process longer history more efficiently.
- **V17 compat**: requires arch change. Defer to V19+.

### 8c. Mistral Lifecycle Analysis
- **Why surprising**: First comprehensive LCA of an LLM (Carbone 4 + ADEME).
- **Lesson**: Important for any environmental/regulatory disclosure of Surrogate-V18.

### 8d. IBM Granite 4.1 (Apr 2026)
- **Source**: https://research.ibm.com/blog/granite-4-1-ai-foundation-models
- **Why surprising**: 8B model matches old 32B MoE. 15T tokens, 512K context, Apache 2.0. **Explicitly enterprise focus** — token cost prioritized over peak perf.
- **V18 wire-in**: Granite's training pipeline (multi-phase pretraining → 15T tokens → reasoning enhancement) is a battle-tested template for Surrogate-V18.

### 8e. IBM Bob (Apr 2026)
- **Why surprising**: AI development partner from "AI-assisted coding to production-ready software". Multi-model orchestration with specialized fine-tunes for code reasoning, security, next-edit prediction.
- **Lesson**: Production agent UX pattern — use orchestrator + specialized sub-models, not monolithic.

---

## 9. ICLR 2026 Outstanding Papers (Surprising Picks)

### 9a. "Transformers are Inherently Succinct"
- **Why surprising**: Theoretical proof of Transformer encoding power vs RNNs. Direct architectural justification.

### 9b. Multi-Turn Eval Outstanding Paper
- **Key finding**: LLMs show marked degradation in multi-turn underspecified scenarios. Single-turn evals overestimate real-world capability.
- **V18 wire-in**: Surrogate-V18 eval suite MUST include multi-turn underspecified scenarios. Don't trust single-turn benchmarks alone.

### 9c. Selective Weak-to-Strong (arXiv 2511.14166)
- **Why surprising**: Strong model **abstains** from training when weak labels are likely wrong. Avoids capability degradation.
- **V18 wire-in**: For RLHF stage where weak humans label complex code/math, add abstention head. Strong model skips bad labels rather than averaging them.

### 9d. xLSTM Scaling Laws (arXiv 2510.02228)
- **Why surprising**: xLSTM competitive with Transformer on scaling. Reopens RNN-family as viable.
- **V19 candidate**: not for V18.

### 9e. Agentic Biomedical Training (ICLR 2026 outstanding)
- **Lesson**: Domain-specific agent training is now a recognized research direction.

---

## 10. NeurIPS 2025 Notable

### 10a. Best Paper: 1000-Layer Networks for Self-Supervised RL
- **Why surprising**: Extreme depth proven effective for goal-achievement RL.
- **V18 wire-in**: For RL warm-up in Surrogate, consider deeper-narrower architecture. Trade compute for capability.

### 10b. Runner-up: RLVR Doesn't Add New Capabilities
- **Key finding**: RLVR enhances **sampling efficiency** of existing capabilities, NOT new ones. Genuinely new reasoning needs different methods.
- **V18 wire-in**: Don't expect RLVR alone to add novel skills to Surrogate. Use RLVR for sharpening, OPD/synthesis for new capabilities.
- **V17 compat**: YES — adjusts expectations only.

### 10c. Nested Learning (Google, neuroscience-inspired)
- **Why surprising**: Blends neuroscience with transformer architecture.

### 10d. Co4 Architecture (May 2025)
- **Why surprising**: Emulates dual-input state-dependent processing of human neocortex layer-5 pyramidal TPNs. Pre-selects information before attention.
- **V18 wire-in**: For efficiency, consider Co4-style pre-selection gates before each attention block. Reduces FLOPs.

---

## 11. Latent Reasoning + Test-Time Innovations

### 11a. Abstract Chain-of-Thought (arXiv 2604.22709)
- **Why surprising**: Discrete latent reasoning post-training — reserved-vocab tokens stand in for natural-language CoT. **11.6× fewer reasoning tokens** at same performance.
- **V18 wire-in**: Post-training stage to compress Surrogate's CoT. Major inference-cost win.
- **V17 compat**: YES — append as final stage.

### 11b. Coconut (Continuous Latent CoT, arXiv 2412.06769)
- **Why surprising**: Trains LLM to reason in continuous latent space (no token decode between steps).
- **V18 wire-in**: Alternative to Abstract-CoT. Pick one based on hardware.

### 11c. Latent Thinking Optimization (ICLR 2026, arXiv 2509.26314)
- **Why surprising**: Latent thoughts encode reward signals. **Latent reward models > scalar reward models** on reasoning.
- **V18 wire-in**: Replace scalar PRM with latent reward in V18 RL stage. Better credit assignment.

### 11d. TTT-E2E (arXiv 2512.23675)
- **Why surprising**: Continual learning at test time via next-token prediction on context. Compresses context into weights. Scales like full-attention Transformer for long contexts (Mamba/DeltaNet don't match).
- **V18 wire-in**: For very-long-context tasks (entire codebases), enable TTT-E2E mode.

### 11e. Inference-Time Rethinking (arXiv 2602.06584)
- **Why surprising**: Decouples declarative latent thought vector from procedural decoder. Iterative self-correction at inference.

---

## 12. Self-Play, PRM, Negative Results

### 12a. SPC: Self-Play Critic (arXiv 2504.19162)
- **Why surprising**: Critic evolves by adversarial self-play with "sneaky generator" — no manual step-level annotation.
- **V18 wire-in**: Bootstrap PRM without expensive human annotators. Crucial for Surrogate compute budget.
- **V17 compat**: YES — replaces PRM training stage.

### 12b. Be Your Own Red Teamer (arXiv 2601.10589)
- **Why surprising**: Single LLM is both Attacker and Defender in unified RL loop. Adversarial co-training for safety.
- **V18 wire-in**: Add Red-Team-Self stage in safety training. Cheap, no external red-team needed.

### 12c. Process Relative Policy Optimization (PRPO, arXiv 2601.07182)
- **Why surprising**: Integrates dense PRM feedback into critic-free optimization. Distribution alignment between PRM scores and outcome advantages.
- **V18 wire-in**: PRM upgrade — replace step-level reward with PRPO. Better fine-grained credit.

### 12d. FunPRM (arXiv 2601.22249)
- **Why surprising**: Function-as-step PRM with meta-reward correction for code generation. Structured around modular code.
- **V18 wire-in**: For code-gen RL, use FunPRM instead of token-step PRM.

### 12e. Hierarchical Reward Model (HRM, arXiv 2503.13551)
- **Why surprising**: Evaluates fine-grained AND coarse-grained levels. Hierarchical Node Compression for MCTS efficiency.
- **V18 wire-in**: Replace flat PRM with HRM. Better long-trajectory credit.

### 12f. R-PRM: Reasoning-Driven PRM (arXiv 2503.21295)
- **Why surprising**: PRM that reasons about the step before scoring. Less noisy than blind scoring.

---

## 13. Negative Results — Lessons from Failure

### 13a. "Why LLMs Aren't Scientists Yet" (arXiv 2601.03315)
- **Why surprising**: 4 attempts at autonomous research, 3 failed. Six recurring failure modes:
  1. Bias toward training-data defaults
  2. Implementation drift under execution pressure
  3. Memory/context degradation in long-horizon
  4. Overexcitement (declares success despite obvious failure)
  5. Insufficient domain intelligence
  6. Weak scientific taste in experimental design
- **V18 wire-in**: Train Surrogate on these failure modes as **negative examples** in SFT. Add eval stages for each mode (e.g., "did the agent self-detect when it failed?").

### 13b. LLM Reasoning Failures Survey (arXiv 2602.06176)
- **Lesson**: Comprehensive failure taxonomy.

### 13c. LLMs Fail on Security Patches (arXiv 2603.10072)
- **Key finding**: Only 24.8% of patches are fully correct. 51.4% fail BOTH security and functionality. Dominant failure: semantic misunderstanding.
- **V18 wire-in**: Surrogate-V18 should explicitly handle security patches as a tracked sub-task with separate metric. Don't mix with general code-gen.

### 13d. RLVR Doesn't Expand Capabilities (NeurIPS 2025 runner-up)
- Already covered in §10b. Major implication for V18 strategy.

---

## 14. Systems & Infrastructure (MLSys / OSDI / SOSP 2025)

### 14a. WLB-LLM (OSDI 2025)
- **Source**: Workload-Balanced 4D Parallelism for LLM Training
- **Why surprising**: 4D parallelism (data + tensor + pipeline + expert) coordinated for workload balance.
- **V18 wire-in**: For Surrogate-V18 distributed training, adopt 4D parallel scheme over 3D.

### 14b. ByteDance Robust LLM Training Infra (SOSP 2025)
- **Why surprising**: Real-world failure-recovery patterns from one of largest training jobs. Battle-tested checkpointing.

### 14c. KTransformers (SOSP 2025)
- **Source**: CPU/GPU Hybrid Inference for MoE Models
- **Why surprising**: Unlocks MoE inference on consumer hardware via clever expert offloading.
- **V18 wire-in**: For Surrogate inference on Mac M3 / smaller GPUs, adopt KTransformers offloading.

### 14d. PipeFill (MLSys 2025)
- **Source**: Using GPUs During Bubbles in Pipeline-parallel LLM Training
- **Why surprising**: Recovers wasted GPU time during pipeline bubbles. Free 10-15% throughput.
- **V18 wire-in**: Adopt PipeFill into V18 trainer.

### 14e. FlashInfer (MLSys 2025)
- **Why surprising**: Customizable attention engine for inference serving. Production-grade.
- **V18 wire-in**: Use FlashInfer for Surrogate inference layer.

### 14f. Mercury (SOSP 2025)
- **Why surprising**: Multi-GPU operator optimization via remote memory scheduling.

### 14g. DiffKV (SOSP 2025)
- **Why surprising**: Differentiated KV-cache memory management with parallel compaction.
- **V18 wire-in**: For long-context Surrogate, adopt DiffKV cache.

---

## 15. Data Curation + Synthetic Data Innovations

### 15a. Synthetic Rewriting as Quality Multiplier (arXiv 2603.24826)
- **Key finding**: Synthetic rewriting is a **quality multiplier**, not substitute. Effect is scale-dependent. Don't try to replace real data with pure synthetic at scale.

### 15b. Scale Dependent Data Duplication (arXiv 2603.06603)
- **Key finding**: Divergence from power-law scaling appears **an order of magnitude earlier** for synthetic pretraining data. Synthetic has fundamental limits.
- **V18 wire-in**: Cap synthetic ratio in Surrogate's pretraining mix at <30%. Anchor with high-quality real data.

### 15c. CCI4.0 Bilingual Pretraining (arXiv 2506.07463)
- **Why surprising**: Open dataset with rigorous pipeline: dedup → multi-classifier quality scoring → fluency filter → CoT synthesis → privacy/toxicity.
- **V18 wire-in**: Use CCI4.0 pipeline as template for Surrogate's data prep.

### 15d. FineInstructions (arXiv 2601.22146)
- **Why surprising**: Scaling synthetic instructions to **pretraining scale** — not just SFT.

### 15e. LSHBloom (Internet-scale dedup)
- **Why surprising**: Faster + smaller than MinHashLSH. Production dedup ready.

### 15f. CCI4.0 + Primus Cyber Dataset (EMNLP 2025)
- **Why surprising**: 15.9% aggregate score gain from continued pretrain on cyber-curated data. 15.8% gain on CISSP from reasoning distill.
- **V18 wire-in**: For Surrogate-DevSecOps variant, layer Primus into pretrain.

---

## 16. Long-Context Recipes (Beyond V17)

### 16a. Million-Token Hierarchical Synthesis (arXiv 2504.12637)
- **Why surprising**: Strategy for 10M-token instruction-tuning dataset via hierarchical synthesis.
- **V18 wire-in**: For Surrogate's long-context post-training, use hierarchical synthesis instead of raw concatenation.

### 16b. LongSpec (Speculative Decoding for Long Context)
- **Why surprising**: Lossless speculative decoding at long context. Inference speedup without quality loss.

### 16c. Context Parallelism for Million-Token Inference (arXiv 2411.01783)
- **Why surprising**: Production-grade context parallelism. Meta deployed.

### 16d. 3M-Token Single-GPU (arXiv 2502.08910)
- **Why surprising**: Single GPU handling 3M tokens. Memory-engineering wins.

### 16e. InfiniteHiP
- **Why surprising**: Hierarchical token pruning + selective RoPE adjustment. Modular.

---

## 17. Attention Architectures Beyond Standard

### 17a. Log-Linear Attention (ICLR 2026, arXiv 2506.04761)
- **Why surprising**: Reformulates linear attention as linear RNN with matrix-valued hidden states. Linear-time, constant-memory, parallelizable.

### 17b. Kimi Linear (arXiv 2510.26692)
- **Why surprising**: Production Kimi attention architecture. Expressive AND efficient.

### 17c. Softmax Linear Attention (arXiv 2602.01744)
- **Why surprising**: Lifts softmax from token level to head level. Restores competitive selection without losing efficiency.

### 17d. Spark Transformer (arXiv 2506.06644)
- **Why surprising**: 8% FFN neurons + 256-token attention window. Matches Gemma-2.
- **V18 wire-in**: For inference efficiency, sparsify Surrogate FFNs to 8% activation.

### 17e. Sparse Growing Transformer (arXiv 2603.23998)
- **Why surprising**: Allocates depth dynamically during training based on entropy.

---

## 18. MoE Innovations

### 18a. Geometric Routing (arXiv 2604.14434)
- **Why surprising**: Cosine-similarity routing in low-dim metric space → 15% of experts are **monosemantic specialists** (temporal, geographic, cardinal, discourse, emotional, financial, military, scientific). Inspectable.
- **V18 wire-in**: When MoE is rebuilt for V18, adopt geometric routing for interpretability + selective expert ablation in safety review.

### 18b. Expert Upcycling (arXiv 2604.19835)
- **Why surprising**: Compute-efficient frontier — increase total params by adding experts at fixed active compute.
- **V18 wire-in**: Scale Surrogate via expert addition, not dimension growth.

### 18c. ExpertCondenser
- **Why surprising**: Adapts MoE without disrupting fragile routing. Good for fine-tuning pre-trained MoE.

### 18d. Differentiated Expert Sizes (Tencent Hy3)
- Already in §6a.

---

## 19. PEFT / LoRA Innovations

### 19a. SOLAR (arXiv 2604.08368)
- **Why surprising**: Communication-efficient PEFT via subspace-oriented latent adapter reparametrization. Singular vectors + controlled noise.

### 19b. LoRA Redux (arXiv 2604.21905)
- **Why surprising**: Comprehensive review with new SVD/tensorization tricks.

### 19c. LoRAFusion (arXiv 2510.00206)
- **Why surprising**: Kernel-level graph splitting. Production efficiency.

### 19d. LoRI (arXiv 2504.07448)
- **Why surprising**: Reduces cross-task interference in multi-task LoRA.
- **V18 wire-in**: For Surrogate's multi-task fine-tuning, use LoRI to prevent task-A degrading task-B.

### 19e. LoRAServe (arXiv 2511.22880)
- **Why surprising**: Workload-aware dynamic adapter placement and routing.
- **V18 wire-in**: Surrogate inference layer should serve heterogeneous LoRA adapters via LoRAServe.

---

## 20. Curriculum + RL Innovations

### 20a. Self-Evolving Curriculum (arXiv 2505.14970)
- **Why surprising**: Multi-armed bandit picks problem categories. Non-stationary scheduling.

### 20b. E2H Reasoner (arXiv 2506.06632)
- **Why surprising**: Probabilistic easy→hard scheduler. Small models (1.5-3B) struggle with vanilla RL but succeed with E2H.
- **V18 wire-in**: For Surrogate small-variant training, use E2H curriculum.

### 20c. CLewR (arXiv 2601.05858)
- **Why surprising**: **Restarts** — re-run easy-to-hard curriculum to mitigate catastrophic forgetting.

### 20d. Prompt Curriculum Learning (OpenReview)
- **Why surprising**: Curriculum at prompt level for post-training.

### 20e. Curriculum RL Easy→Hard for Reasoning
- Already complementary to E2H.

---

## 21. RLHF Alternatives (V17+ candidates)

### 21a. RLIF (Reinforcement Learning from Internal Feedback) + INTUITOR
- **Why surprising**: Uses model's own self-certainty as intrinsic reward. No external feedback needed.
- **V18 wire-in**: Bootstrap reward for low-resource domains.

### 21b. S-GRPO (Supervised GRPO)
- **Why surprising**: Pure supervised paradigm with preference-optimization strengths. No reward model.

### 21c. Pairwise-RL
- **Why surprising**: Unifies reward training and RL within pairwise framework. Better calibration.

### 21d. Generative RLHF-V (multimodal)
- **Why surprising**: Multi-modal preference learning with principles.

### 21e. Reinforced Attention Learning (RAL)
- **Why surprising**: Optimizes **internal attention distributions** rather than output tokens. Process-aware.
- **V18 wire-in**: Risky but interesting — replaces standard policy gradient.

### 21f. PRPO (already in §12c)

### 21g. First-Order Logic PRM (arXiv 2512.14100)
- **Why surprising**: FOL-based alternative to neural reward models. Verifiable.

---

## 22. Tool Use + Agent Training

### 22a. TL-Training
- **Why surprising**: Task-feature-based framework with adverse-effect mitigation + key-token prioritization. Less data, better tool use.

### 22b. DRAFT (Dynamic Refinement of Tool Documentation)
- **Why surprising**: Three-phase learning — gather → learn → rewrite docs. Self-improving tool docs.
- **V18 wire-in**: For Surrogate's tool-use, deploy DRAFT loop. Tool docs improve over time autonomously.

### 22c. From Exploration to Mastery (Self-Driven Tool Learning)
- **Why surprising**: Self-driven interaction without supervision.

### 22d. Recursive Language Models (RLM, Oct 2025)
- **Why surprising**: Model actively manages own context. Production-claimed paradigm of 2026.

---

## 23. Environment Synthesis for RL

### 23a. Agent World Model (arXiv 2602.10090)
- **Why surprising**: Synthesizes **1,000 environments** with 35,062 tools and 10,000 tasks (each with verification code). Replaces static benchmark sets.
- **V18 wire-in**: Replace Surrogate's static training env list with AWM-synthesized envs. **Massive scale-up.**
- **V17 compat**: YES — data layer.

### 23b. AutoForge + ERPO
- **Why surprising**: Automated env synthesis + RL algorithm tolerant to simulated-user instability.

### 23c. TermiGen
- **Why surprising**: Verifiable terminal envs + error-injection distillation. Closes gap to proprietary.
- **V18 wire-in**: For DevOps/SRE Surrogate variant, use TermiGen for terminal-task training.

### 23d. SWE-RL (Self-Play SWE, arXiv 2512.18552)
- **Why surprising**: Self-play SWE-RL toward "superintelligent software agents".

### 23e. RLinf (open infra)
- **Why surprising**: Production-grade open RL infra for embodied + agentic AI.

---

## 24. Evaluation Suites — New 2026

### 24a. SWE-Bench Pro (1,865 tasks, 41 repos, private codebases)
- **Key**: Top models score ~23% (vs 70%+ on SWE-Bench Verified). Standardized 250-turn limit removes scaffolding distortions.
- **V18 wire-in**: PRIMARY headline metric for Surrogate. Don't trust SWE-Bench Verified alone.

### 24b. ARC-AGI-3 (Mar 2026)
- **Why surprising**: Interactive agent eval. New frontier benchmark.

### 24c. AgencyBench (1M tokens, 90 tool calls, hours of execution)
- **Why surprising**: Realistic full-agent eval at scale.

### 24d. Terminal-Bench 2.0 + HAL Leaderboard
- **Why surprising**: Standardized agent evaluation across labs.

### 24e. BioAgent Bench (arXiv 2601.21800)
- **Why surprising**: Domain-specific (bioinformatics). Pattern for any vertical.

### 24f. AutoResearchBench
- **Why surprising**: Scientific literature discovery agent eval.

### 24g. Wiki Live Challenge (Feb 2026)
- **Why surprising**: Live, dynamic benchmark.

### 24h. Memory Eval Frameworks
- **MemoryArena**, **OCR-Memory** benchmarks — episodic/long-horizon memory tracking.

---

## 25. Cross-Disciplinary AI

### 25a. NatureLM (Microsoft AI for Science)
- **Why surprising**: Sequence-based foundation model unifying small molecules, materials, proteins, DNA, RNA.
- **Lesson**: Multi-modality at sequence level is a coherent design direction.

### 25b. Evo / Evo 2 (genome foundation)
- **Why surprising**: Genome-scale prediction zero-shot across DNA, RNA, protein.

### 25c. MatterChat (multimodal materials)
- **Why surprising**: Material structure + LLM, with interpretable reasoning.

### 25d. Nature Methods Protein LM
- **Source**: https://www.nature.com/articles/s41592-025-02776-2
- **Why surprising**: Biophysics-based protein LM uniting ML + biophysical sim.

### 25e. InterPLM (sparse autoencoders for proteins)
- **Why surprising**: Interpretability extends to biology domain.

### 25f. Cell Systems Compositional AI
- **Why surprising**: Modular multimodal foundation models for cells.

### 25g. ELHPlan (robotics, arXiv 2509.24230)
- **Why surprising**: Action-Chains primitive — sub-goal-bound action sequences. **24% of tokens** of SOTA.
- **V18 wire-in**: For Surrogate's planning, adopt Action-Chains primitive. Major token reduction.

---

## 26. Mechanistic Interpretability — Production Ready

### 26a. SALVE (Dec 2025)
- **Why surprising**: SAE-based "discover, validate, control" pipeline. **Permanent model surgery via sparse features.**
- **V18 wire-in**: Use SALVE to permanently remove specific harmful behaviors after detection.

### 26b. SAE Neural Operators (Feb 2026)
- **Why surprising**: SAEs in **infinite-dimensional function spaces**. Generalizes linear-representation hypothesis.

### 26c. Scale SAE (Nov 2025)
- **Why surprising**: 2-expert co-activation unlocks specialization. Polysemanticity reduction.

### 26d. SAEBench (200+ SAEs across 7 architectures)
- **Why surprising**: Comprehensive comparative benchmark.

---

## 27. Scaling Law Updates (2026)

### 27a. Scaling Laws for Scalable Oversight (arXiv 2504.18530)
- **Why surprising**: Quantitative model of when scalable oversight breaks.

### 27b. Test-Time Scaling Makes Overtraining Compute-Optimal (arXiv 2604.01411)
- **Why surprising**: Pretraining and inference scaling are **fundamentally coupled**. Standard scaling laws miss this.
- **V18 wire-in**: Joint scaling decision — overtraining now compute-optimal IF you deploy with test-time scaling.

### 27c. Art of Scaling Test-Time Compute (arXiv 2512.02008)
- **Key finding**: 30B+ tokens across 8 models. **No universal best TTS strategy.** Reasoning models have distinct trace-quality patterns. Optimal TTS scales monotonically with budget.

### 27d. Scaling Data-Constrained Language Models (JMLR Vol 26)
- **Why surprising**: Data-constrained scaling — when to repeat data vs add new.

### 27e. JMLR 27 (2026) Scaling Laws Including Attention
- **Why surprising**: Standard 6ND FLOPs approximation breaks at long context. Includes attention FLOPs.

---

## 28. Multi-Agent + Coordination

### 28a. MIRROR (IJCAI 2025)
- **Why surprising**: Intra-reflection (self) + Inter-reflection (peers) for tool-use multi-agent.

### 28b. Agent² (arXiv 2509.13368)
- **Why surprising**: **Agent generates agent**. Automated RL for agent design.
- **V18 wire-in**: Use Agent² to bootstrap specialized variants of Surrogate.

### 28c. WMAC 2026 (AAAI Bridge)
- **Forum for**: LLM-based multi-agent collaboration research.

### 28d. LaMAS 2026 (AAAI workshop)
- **Forum for**: Multi-agent systems powered by LLMs.

### 28e. Hierarchical Lead Critic MARL
- Already covered above.

### 28f. MAGRPO (Multi-Agent Group Relative Policy Optimization)
- **Why surprising**: Models LLM collaboration as cooperative MARL. Beats single-LLM.
- **V18 wire-in**: Train Surrogate to cooperate with itself (self-instances) via MAGRPO.

### 28g. Emergent Coordination in Multi-Agent LMs (arXiv 2510.05174)
- **Key**: Information-theoretic test for emergence. Distinguishes spurious coupling from real synergy.

---

## 29. Memory Systems

### 29a. Memory in the Age of AI Agents (Survey, arXiv 2512.13564)
- **Why surprising**: Comprehensive taxonomy.

### 29b. OCR-Memory (arXiv 2604.26622)
- **Why surprising**: Visual modality as high-density memory representation. Arbitrarily long histories with low overhead.
- **V18 wire-in**: For long-term Surrogate memory, encode summaries as images.

### 29c. MemMachine (arXiv 2604.04853)
- **Why surprising**: Ground-truth-preserving memory.

### 29d. EverMemOS
- **Why surprising**: Self-organizing memory OS for structured long-horizon.

### 29e. MemRL (Self-Evolving Episodic Memory)
- **Why surprising**: Runtime RL on episodic memory.

### 29f. Episodic Memory Position Paper (arXiv 2502.06975)
- **Key**: Episodic memory is the **missing piece** for long-term agents. Need recall of when, how, why, with whom.

---

## 30. Distillation Renaissance (2025-2026)

### 30a. SpecKD (arXiv 2510.24021)
- **Why surprising**: Speculative decoding-style "propose-and-verify" gating in distillation.

### 30b. On-Policy Distillation (OPD) — Now Mainstream
- **Source**: Qwen3, MiMo, GLM-5 all adopted
- **Key**: Student generates trajectories; teacher gives feedback on student's own outputs. Avoids exposure bias.
- **V18 wire-in**: REPLACE V17's pure DAPO with mid-training OPD stage. Borrow from GLM-5 disclosed recipe.

### 30c. RLAD (RL-Aware Distillation)
- **Why surprising**: Selective imitation — student follows teacher only when beneficial under RL objective.

### 30d. On-Policy Self-Distillation (OPSD)
- **Why surprising**: Single model plays both teacher (privileged context) and student (unprivileged). No external teacher needed.
- **V18 wire-in**: For Surrogate, OPSD avoids external teacher dependency.

### 30e. SpecKD + OPD combined
- **V18 wire-in**: Final distillation pipeline = OPD trajectories → SpecKD selective loss.

---

## 31. Industry Consortium / Foundation

### 31a. Agentic AI Foundation (AAIF)
- **Founded**: Dec 2025 by Anthropic, OpenAI, Block under Linux Foundation
- **Goal**: Open, interoperable infrastructure for agentic AI
- **V18 wire-in**: Track AAIF specs for interop. Surrogate should consume AAIF protocols.

### 31b. AI Alliance + Linux Foundation Open Innovation Day
- **Why surprising**: Open-source push at scale.

### 31c. Open Source Summit NA 2026 AI tracks
- **Why surprising**: Expanded AI infrastructure focus.

---

## 32. Cohere Research Lab Output

### 32a. Aya Multilingual Family (continued)
- **Source**: Cohere Labs Research Papers
- **Why surprising**: Strong multilingual research pipeline. Aya Vision and Aya Expanse iterations.
- **V18 wire-in**: Use Aya techniques for any non-English tier of Surrogate.

---

## 33. WORKING NOTES — NEGATIVE FINDINGS

What I searched but found LITTLE NEW from V13-V17:
- **MIRI papers 2025-2026**: minimal new arxiv output. Mostly think-tank blogs about corrigibility from older 2024 work.
- **Conjecture 2026**: bought-time approach, no new technical papers in 2026.
- **NCSoft / Kakao Brain 2026**: dropped from sovereign AI contest, no new public LLM technical reports.
- **Sber GigaChat 2026**: no new technical paper, only commercial deployment news.
- **Naver Cloud 2026**: dropped from sovereign AI for "lack of independence" — discontinued public LLM research effort.
- **AppLovin AI Research**: no public research output.
- **Adept (post-acquisition)**: discontinued.
- **Magic.dev**: still no public disclosures.
- **Inflection (post-Microsoft)**: discontinued.

These NULL findings are themselves data — the field has consolidated around named major labs.

---

## 34. V18 STACK PROPOSAL — Synthesis

Given V17 baseline + above papers, V18 should integrate:

### Base
- Init from GLM-5 / Qwen3 weights (Yandex pattern §7b)
- 4D parallelism (WLB-LLM §14a)
- PipeFill bubble recovery (§14d)

### Pretraining
- CCI4.0 pipeline (§15c)
- Synthetic ratio cap <30% (§15a, §15b)
- Hierarchical synthesis for long-context (§16a)
- TTT-E2E mode for ultra-long (§11d)

### Mid-training (NEW STAGE)
- On-Policy Distillation from teacher ensemble (§30b)
- E2H curriculum (§20b)
- Self-Play Critic for PRM bootstrapping (§12a)

### Post-training
- Hierarchical PRM (§12e) + PRPO (§12c)
- Q-learning hybrid for RL (§5a)
- AB-MCTS-aware policy (§1a)
- Action-Chains primitive (§25g)
- Latent Thinking Optimization (§11c)

### Safety
- Deliberative Alignment with explicit spec (§2a)
- Apollo scheming eval as CI gate (§2b)
- Be-Your-Own-Red-Teamer adversarial co-train (§12b)
- Causal Scrubbing for capability claims (§4a)
- Selective W2S abstention (§9c)

### Compression / Inference
- Abstract-CoT post-training (§11a)
- Spark-style FFN sparsification (§17d)
- KTransformers offloading for consumer HW (§14c)
- LoRAServe for adapter routing (§19e)
- FlashInfer attention engine (§14e)
- SALVE permanent surgery for unsafe behaviors (§26a)

### Eval
- METR Time Horizon (§3a)
- SWE-Bench Pro primary (§24a)
- AgencyBench full-agent (§24c)
- Multi-turn underspecified (§9b)
- Apollo scheming gate (§2b)
- Holistic eval (§3b)
- Negative-result self-detection (§13a)

### Environment
- Agent World Model (1k synthesized envs) (§23a)
- TermiGen for terminal tasks (§23c)

### Multi-Agent
- MAGRPO self-cooperation training (§28f)
- Agent² for variant bootstrapping (§28b)
- AB-MCTS multi-model inference cooperation (§1a)

---

## 35. RISKS + UNKNOWNS

### Things I'm uncertain about
- **xLSTM, Jamba, Mamba hybrids**: ICLR 2026 says competitive but production deployments still rare. Defer to V19.
- **Q-learning-based RL for LLM**: MSR Asia paper promising, but limited replications. Try only on a small variant first.
- **Latent CoT (Coconut + Abstract-CoT)**: token reduction wins are real, but interpretability cost is high. Safety eval becomes harder.
- **TTT-E2E**: scales like Transformer for long-ctx, but production stability untested.

### Open research questions
- Does deliberative alignment hold at scale beyond o3/o4-mini?
- Can RLVR be augmented to add genuinely new capabilities? (NeurIPS 2025 runner-up says no with current methods.)
- What's the right ratio of synthetic to real for >1T-token training? (Scale-dependent, not solved.)
- Does self-play critic generalize beyond math/code to open-ended tasks?

---

## SOURCES

### Sakana / Apollo / METR / Redwood / DeepMind
- https://sakana.ai/ab-mcts/
- https://sakana.ai/cycleqd/
- https://sakana.ai/shinka-evolve/
- https://arxiv.org/abs/2508.16204 (M2N2)
- https://www.apolloresearch.ai/research/stress-testing-deliberative-alignment-for-anti-scheming-training/
- https://www.apolloresearch.ai/research/frontier-models-are-capable-of-incontext-scheming/
- https://metr.org/blog/2026-1-29-time-horizon-1-1/
- https://metr.org/blog/2025-08-12-research-update-towards-reconciling-slowdown-with-time-horizons/
- https://www.redwoodresearch.org/research
- https://arxiv.org/abs/2504.01849 (DeepMind AGI Safety)
- arXiv 2510.26518 (Amplified Oversight)
- arXiv 2511.18397 (Natural emergent misalignment)

### Microsoft / Tencent / Alibaba / Naver / Yandex / IBM
- https://www.microsoft.com/en-us/research/event/iclr-2026/publications/
- arXiv 2602.15763 (GLM-5)
- arXiv 2512.24092 (HY-MT1.5)
- arXiv 2505.09388 (Qwen3 tech report)
- https://www.alibabacloud.com/blog/alibaba-qwen-wins-neurips-2025-best-paper-award...
- arXiv 2506.22403 (HyperCLOVA X THINK)
- https://medium.com/yandex/tech-report-on-alice-ai-...
- https://research.ibm.com/blog/granite-4-1-ai-foundation-models

### Conferences
- https://blog.iclr.cc/2026/04/23/announcing-the-iclr-2026-outstanding-papers/
- https://blog.neurips.cc/2025/11/26/announcing-the-neurips-2025-best-paper-awards/
- https://icml.cc/virtual/2025/papers.html
- https://2025.emnlp.org/program/find_papers/
- https://2025.naacl.org/program/accepted_papers/
- https://2025.ijcai.org/montreal-main-track-accepted-papers/
- https://cvpr.thecvf.com/Conferences/2025/AcceptedPapers
- https://mlsys.org/virtual/2025/papers.html

### Major arXiv Papers Cited
- 2604.22709 (Abstract Chain-of-Thought)
- 2512.23675 (TTT-E2E)
- 2504.19162 (SPC)
- 2511.14166 (Selective W2S)
- 2602.10090 (Agent World Model)
- 2509.26314 (Latent Thinking Optimization)
- 2601.07182 (PRPO)
- 2503.13551 (HRM)
- 2604.14434 (Geometric Routing)
- 2604.19835 (Expert Upcycling)
- 2604.21905 (LoRA Redux)
- 2604.08368 (SOLAR)
- 2510.00206 (LoRAFusion)
- 2511.22880 (LoRAServe)
- 2603.24826 (Synthetic Rewriting)
- 2603.06603 (Scale-Dependent Duplication)
- 2506.07463 (CCI4.0)
- 2601.22146 (FineInstructions)
- 2504.12637 (Million-Token Hierarchical)
- 2506.04761 (Log-Linear Attention)
- 2510.26692 (Kimi Linear)
- 2602.01744 (Softmax Linear Attention)
- 2506.06644 (Spark Transformer)
- 2603.23998 (Sparse Growing Transformer)
- 2510.24021 (SpecKD)
- 2604.13016 (On-Policy Distillation)
- 2509.13368 (Agent²)
- 2510.05174 (Emergent Coordination)
- 2512.13564 (Memory Survey)
- 2604.26622 (OCR-Memory)
- 2604.04853 (MemMachine)
- 2502.06975 (Episodic Memory Position)
- 2601.03315 (Why LLMs Aren't Scientists Yet)
- 2602.06176 (LLM Reasoning Failures Survey)
- 2603.10072 (LLMs Fail on Security Patches)
- 2604.13602 (Reward Hacking Era)
- 2603.28063 (Reward Hacking Equilibrium)
- 2507.05619 (Proxy Gaming Detection)
- 2601.18217 (Cross-Domain Generalization Tax)
- 2603.14712 (Next-Generation LLM Training Data-Centric)
- 2505.14970 (Self-Evolving Curriculum)
- 2506.06632 (E2H Reasoner)
- 2601.05858 (CLewR)
- 2601.12720 (SCFT Effective Reflection)
- 2602.02416 (Structured Self-Localization)
- 2502.04404 (Self-Backtracking)
- 2507.00417 (ASTRO)
- 2603.07670 (Memory for Autonomous LLM Agents)
- 2602.16313 (Multi-Session Agent Memory Bench)
- 2601.21800 (BioAgent Bench)
- 2603.23749 (Efficient Benchmarking AI Agents)
- 2601.11044 (AgencyBench)
- 2604.25256 (AutoResearchBench)
- 2603.18361 (Synthetic Data Diversified Commonsense)
- 2602.03414 (Socratic-Geo)
- 2604.17010 (Code Reasoning Self-Play Formal Verif)
- 2601.10589 (Be Your Own Red Teamer)
- 2410.18252 (Asynchronous RLHF)
- 2512.14100 (FOL Reward Model)
- 2509.23951 (HunyuanImage 3.0)
- Nature Machine Intelligence (Sakana M2N2 published)
- https://www.nature.com/articles/s41592-025-02776-2 (Biophysics Protein LM)
- https://www.nature.com/articles/s41592-025-02836-7 (InterPLM)
- https://www.cell.com/cell-systems/abstract/S2405-4712(26)00016-5 (Compositional AI for cells)
- https://naturelm.github.io/

### Industry / Eval
- https://www.swebench.com/
- https://www.morphllm.com/swe-bench-pro
- https://buildmind.ai/blog/arc-agi-3-frontier-agent-evals-march-2026/
- https://aaif.io/

---

## END

This sweep adds **130+ unique paper/research references** beyond V13-V17. The 12 highlighted techniques in §1-§12 are the minimum-set the V18 RL stage should incorporate.

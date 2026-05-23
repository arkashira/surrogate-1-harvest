---
title: V16 — World Institution Papers (Universities + Industry Labs Beyond Big Six)
date: 2026-05-01
tags: [v16, surrogate-1, training, papers, academic, world-institutions, niche-techniques]
status: research-complete
related: [[v14-arxiv-github-sweep-may2026]] [[v14-rl-frontier-beyond-dapo]] [[v13-frontier-capability]] [[v14-reasoning-frontier]]
scope: 2024-2026 papers from non-Big-Six labs (universities + 2nd-tier industry research) to find techniques NOT covered in Round 3 / earlier Q2 sweeps
---

# V16 World-Institution Paper Sweep — Surrogate-1 Training

> Goal: surface niche/contrarian techniques from world institutions (Stanford/MIT/Berkeley/CMU/Tsinghua/PKU/ETH/EPFL/Oxford/INRIA/Tencent/IBM/Sakana/Yandex/Samsung etc.) that compose with V15 stack but were missed by frontier-lab-focused rounds.

---

## Section A — Type-Aware & Compiler-as-Supervisor Code Training

### 1. Type-Constrained Code Generation (ETH Zurich, ICML 2025)
- **Paper**: arxiv 2504.09246 — "Type-Constrained Code Generation with Language Models" (Apr 2025)
- **Institution**: ETH Zurich (D-INFK)
- **Technique**: Prefix automata + inhabitable-types search → enforce well-typedness during decoding. Formalized on simply-typed lambda + extended to TypeScript.
- **Measured**: HumanEval/MBPP — compilation errors down >50%, functional correctness +3.5–5.5% (synthesis/translation), +37% on repair of non-compiling code. Works on 30B+ open-weight models.
- **Surrogate application**: integrate into RL-loop reward shaping — reward = pass_tests + λ·type_check_pass. Cheap signal, no env needed. Compose with V14 RLVR.

### 2. Compiler Semantics Injection — C→x86 (EMNLP 2024 Findings)
- **Paper**: aclanthology 2024.findings-emnlp.55 — "Introducing Compiler Semantics into LLMs as Programming Language Translators"
- **Institution**: Multi-uni collaboration (Asian + European)
- **Technique**: Distill compiler IR (LLVM-style) into LLM as auxiliary objective during fine-tune. Model learns instruction-selection patterns implicitly.
- **Measured**: significant improvement on C↔assembly translation BLEU + execution match.
- **Surrogate application**: add LLVM IR pretraining slice (3–5% of pretrain). Bridges high-level → low-level semantics. Useful for Surrogate to read disassembly + write performance-critical code.

### 3. CodeRL+ — Execution Semantics Alignment (Oct 2025)
- **Paper**: arxiv 2510.18471 — "CodeRL+: Improving Code Generation via Reinforcement with Execution Semantics Alignment"
- **Technique**: RLVR pipeline augmented with variable-level execution trajectory inference. Model learns to predict intermediate variable states as auxiliary loss.
- **Measured**: +4.6% relative pass@1 average across HumanEval/MBPP/LiveCodeBench.
- **Surrogate application**: drop-in addition to V14 RL stage — Surrogate already has sandbox; just emit variable-state checkpoints during reward computation, train auxiliary head on next-state prediction.

### 4. Execution Tuning E.T. (CMU/SE labs, Mar 2025)
- **Paper**: arxiv 2503.05703 — "What I cannot execute, I do not understand: Training and Evaluating LLMs on Program Execution Traces"
- **Technique**: explicitly model line-level + instruction-level execution traces. Dynamic scratchpad for long executions.
- **Measured**: ~80% accuracy on CruxEval + MBPP execution prediction (state-of-the-art at release).
- **Surrogate application**: synthesize 2–5B-token execution-trace dataset (Python tutor / Pyodide instrumentation), continued pretraining stage. Composes with CWM-style world-modeling.

---

## Section B — Formal-Verification & Lean as Training Signal

### 5. LeanNavigator — 4.7M Theorems Auto-Generated (Feb 2025)
- **Paper**: arxiv 2503.04772 — "Generating Millions Of Lean Theorems With Proofs By Exploring State Transition Graphs"
- **Technique**: BFS over Lean state-transition graph, finds new ways to prove Mathlib4 theorems. Each path = new theorem-proof pair.
- **Measured**: 4.7M theorems / 1B tokens — order-of-magnitude larger than prior datasets.
- **Surrogate application**: include this corpus (HF: LeanNavigator) in math/reasoning slice of pretrain. Free signal, no human labeling.

### 6. APOLLO — Sample-Efficient Proof Repair (May 2025)
- **Paper**: arxiv 2505.05758 — "APOLLO: Automated LLM and Lean Collaboration for Advanced Formal Reasoning"
- **Technique**: Modular agent — LLM proposes proof, Lean verifies, agents fix syntax, isolate failing sub-lemmas, invoke automated solvers, recurse.
- **Measured**: Goedel-Prover-SFT 64.7% → 65.6% with sampling budget cut from 25,600 → few hundred. Kimina-Prover 75.0% with ~1/3 budget.
- **Surrogate application**: post-train Surrogate with APOLLO-style agentic proof pipeline as RL environment. Verifiable reward = Lean compiles. No reward hacking possible.

### 7. STP — Self-Play Conjecture Generation (Jan 2025)
- **Paper**: STP by Dong et al., 31 Jan 2025
- **Technique**: dual agents (conjecturer + prover) iteratively trained. Verification-admissible conjectures become next-iter training targets.
- **Surrogate application**: extend to Coq/Isabelle if Lean-only is too narrow. Self-play yields infinite curriculum.

---

## Section C — Position Encoding & Architecture Niches

### 8. RoPE↔NoPE Hybrid (Princeton/AWS, Jan 2025)
- **Paper**: arxiv 2501.18795 — "Rope to Nope and Back Again: A New Hybrid Attention Strategy"
- **Technique**: interleave NoPE layers (long-range retrieval) with RoPE layers (local context). After training, layers naturally specialize.
- **Measured**: better long-context performance + simpler training (no RoPE scaling needed).
- **Surrogate application**: replace 25–50% of RoPE layers with NoPE in mid-stack. Compose with V14 long-context recipes.

### 9. SWAN-GPT (Apr 2025)
- **Paper**: arxiv 2504.08719 — "SWAN-GPT"
- **Technique**: global attention NoPE + local sliding-window attention with SWA-RoPE.
- **Surrogate application**: cheap long-context extension — use global NoPE for cross-file code understanding, SWA-RoPE for token-level fluency.

### 10. DoPE — Denoising Rotary Position Embedding (Nov 2025)
- **Paper**: arxiv 2511.09146 — "DoPE"
- **Technique**: question necessity of explicit positional encoding; learnable feature maps applied directly to attention map.
- **Surrogate application**: experimental — try in 1B prototype before scaling.

### 11. STRING — Shifted RoPE (Princeton, ICLR 2025)
- **Paper**: openreview eoln5WgrPx
- **Technique**: shifts well-trained positions to overwrite ineffective positions during inference. No retraining needed.
- **Surrogate application**: drop-in inference fix for long-context degradation. Free win.

---

## Section D — Sparsity Beyond MoE

### 12. SeerAttention — Learnable Block-Sparse Attention (ICLR 2025)
- **Paper**: arxiv 2410.13276 — "SeerAttention: Learning Intrinsic Sparse Attention in Your LLMs"
- **Technique**: gating mechanism (MoE-inspired) selectively activates attention blocks. Block-level sparsity learned from data.
- **Surrogate application**: post-train sparsification — Surrogate trained dense, then SeerAttention layers added with light fine-tune. Lower inference cost.

### 13. Sparsing Law (NeurIPS/ICML 2025)
- **Paper**: arxiv 2411.02335 — "Sparsing Law: Towards LLMs with Greater Activation Sparsity"
- **Technique**: PPL-p% sparsity metric (precise, performance-aware). Finds ReLU > SiLU for activation sparsity, scales with training tokens.
- **Surrogate application**: switch FFN activation from SiLU/SwiGLU → ReLU² in some layers; gain inference speedup with negligible quality loss.

### 14. Training-Free Activation Sparsity TEAL (Aug 2024 / ICLR 2025)
- **Paper**: arxiv 2408.14690
- **Technique**: magnitude-based activation sparsity to hidden states throughout entire model. No training needed.
- **Measured**: 1.53× speedup at 40% sparsity, 1.8× at 50%.
- **Surrogate application**: deploy-time optimization. Combine with KV-cache compression.

### 15. 2:4 Sparsity + Neuron-Level Activation (2026)
- **Paper**: arxiv 2602.06183 — "To 2:4 Sparsity and Beyond"
- **Technique**: hardware-accelerated 2:4 weight sparsity + Venom activation sparsity. Sparse early training → dense end-of-training.
- **Surrogate application**: H200 supports 2:4 sparse — train sparse first 80% of tokens, dense final 20%. ~1.4× training throughput.

---

## Section E — Tokenizer Innovation

### 16. SuperBPE — Multi-Word Tokens (2025)
- **Paper**: SuperBPE (Liu et al., 2025)
- **Technique**: jointly learn subwords + multi-word "superwords" via 2-stage curriculum. Tokenizer goes beyond word boundaries.
- **Measured**: better compression + faster inference at same model size.
- **Surrogate application**: re-train tokenizer with SuperBPE on code+math+text mix. Particularly useful for common code idioms (`for i in range`, `if __name__`).

### 17. StochasTok — Stochastic Tokenization (Jun 2025)
- **Paper**: arxiv 2506.01687 — "StochasTok: Improving Fine-Grained Subword Understanding"
- **Technique**: random tokenizations during training (BPE-dropout extended). Model sees same string with multiple tokenizations.
- **Measured**: dramatic improvement on subword-level tasks.
- **Surrogate application**: enable BPE-dropout p=0.1 during pretraining last 20%. Improves robustness to typos + tokenizer drift.

### 18. Less-is-Better (LiB) Tokenizer
- **Technique**: autonomous learning of integrated vocab — subwords + words + multi-word expressions.
- **Surrogate application**: experimental for v16. Compare against SuperBPE.

---

## Section F — Synthetic Data & Quality Scoring

### 19. Diverse Synthetic Coding Tasks (Tsinghua/etc, Oct 2025)
- **Paper**: arxiv 2510.23208 — "Increasing LLM Coding Capabilities through Diverse Synthetic Coding Tasks"
- **Technique**: 800k instruction-reasoning-code-test quadruplets. Scalable pipeline.
- **Measured**: reasoning-aware data substitutes for model scaling. Consistent gains across architectures.
- **Surrogate application**: include in coder-RL warm-start SFT slice. HF dataset available.

### 20. PersonaHub — 1B Personas (Tencent AI Lab)
- **Paper**: github tencent-ailab/persona-hub — "Scaling Synthetic Data Creation with 1,000,000,000 Personas"
- **Technique**: 1B diverse personas distilled from web. Each persona acts as carrier of skills/knowledge for synthetic data.
- **Recent**: Feb 2025 — 370M elite personas (top 1% in skills) released.
- **Surrogate application**: use elite-persona slice to generate role-conditioned coding/research data. Scales role-comprehensive training (V13).

### 21. Ultra-FineWeb — fastText Classifier (May 2025)
- **Paper**: arxiv 2505.05427 — "Ultra-FineWeb: Efficient Data Filtering"
- **Technique**: lightweight fastText classifier replaces LLM-as-judge. Massively faster filtration.
- **Surrogate application**: use Ultra-FineWeb-en as base + Ultra-FineWeb-zh for multilingual. Apply pipeline to private code corpus.

### 22. propella-1 — Multi-Property Annotation (DATA-FM ICLR 2026)
- **Paper**: arxiv 2602.12414 — "propella-1"
- **Technique**: multi-property document annotation (not single-score). Trains regressors on encoder embeddings via LLM-as-judge.
- **Surrogate application**: V16 data-quality stage — score docs on (educational, code-density, reasoning, factuality, toxicity) separately. Mix-and-match per training stage.

### 23. QuRating + DataMan (cited in Microsoft Research blog)
- **QuRating**: 4 quality dimensions on 260B tokens via pairwise LLM judgments
- **DataMan**: 14 quality criteria on 447B tokens
- **Meta-rater**: integrates 4 dimensions w/ existing signals — doubles convergence speed
- **Surrogate application**: precedent for V16 to use 5–10 dimensional rating (not single score). Convergence speedup is huge cost win.

### 24. ZeroSearch (Alibaba Tongyi Lab, May 2025)
- **Paper**: ZeroSearch — simulated search results via LLM-generated docs
- **Technique**: train search-agent without real API calls. LLM mimics search engine output.
- **Surrogate application**: train agentic-search behavior offline. Cuts training cost dramatically. Use for V16 web-research agent slice.

### 25. RePro — Faithful Web Recycling (Oct 2025)
- **Paper**: arxiv 2510.10681 — "RePro: Training Language Models to Faithfully Recycle the Web"
- **Technique**: small RL-trained rephraser — dual reward (quality + faithfulness). Recycles low-quality web data.
- **Surrogate application**: train 1–3B rephraser, use to upgrade Common Crawl tail. 2–3× usable token expansion.

### 26. Self-Improving Pretraining (Jan 2026)
- **Paper**: arxiv 2601.21343 — "Self-Improving Pretraining"
- **Technique**: post-trained model rewrites streaming pretrain data + acts as judge.
- **Surrogate application**: after V15 model trained, use it to rewrite V16 pretrain corpus → bootstrap loop.

### 27. MGA — Multi-Genre Augmentation (Feb 2025)
- **Paper**: arxiv 2502.04235 — "Reformulation for Pretraining Data Augmentation"
- **Technique**: adaptively generates genre+audience seeds per doc. 3.9× token expansion at maintained diversity.
- **Surrogate application**: take any seed doc, generate (academic-paper, blog, tutorial, code-comment, exam-Q) variants. Cheap data expansion.

### 28. SwallowCode — 4-Stage Code Rewriting (Nov 2025)
- **Paper**: arxiv 2505.02881 — "Rewriting Pre-Training Data Boosts LLM Performance in Math and Code"
- **Technique**: syntax validation → style filtering → LLM-rewrite → final-pass quality check.
- **Surrogate application**: drop-in pipeline for code corpus. Boosts math+code in particular.

---

## Section G — RL & Reward Beyond DAPO

### 29. rLLM + GRPO+ (Berkeley, EECS-2025-123)
- **Paper**: UCB/EECS-2025-123 — "Reinforcement Learning for Safe LLM Code Generation"
- **Technique**: open-source Ray-based RL framework. GRPO+ (improved GRPO) + asynchronous pipelined sampling + iterative context lengthening.
- **Measured**: Deepcoder-14B — 60.6% LiveCodeBench Pass@1, 1936 Codeforces, 92.6% HumanEval+. Matches o3-mini-low + o1.
- **Code**: open source with veRL modifications.
- **Surrogate application**: replace DAPO with GRPO+ in V16 RL stage. Iterative context lengthening — start 8K, ramp to 32K → 128K during training.

### 30. AceCoder — Auto Test-Case Synthesis (Feb 2025)
- **Paper**: arxiv 2502.01718 — "AceCoder: Acing Coder RL via Automated Test-Case Synthesis" (ACL'25)
- **Technique**: prompts powerful LLM to "imagine" test cases for coding problems, filters noisy ones. Scales reward-model + RL training.
- **Code**: github TIGER-AI-Lab/AceCoder
- **Surrogate application**: auto-generate test corpus for proprietary problems. No human labeling.

### 31. ShinkaEvolve (Sakana AI, Sep 2025)
- **Paper**: arxiv 2509.19349 — "ShinkaEvolve: Towards Open-Ended And Sample-Efficient Program Evolution"
- **Technique**: LLM-driven program mutation w/ 3 innovations — adaptive parent sampling, novelty rejection-sampling via embedding similarity, bandit-based LLM ensemble selector.
- **Measured**: SOTA circle packing in 150 evaluations. Pareto-frontier scaffolds for AIME.
- **Surrogate application**: V16 self-improvement loop — Surrogate evolves its own scaffold/agent code. Sample-efficient enough for limited compute.

### 32. CognitiveKernel-Pro (Tencent AI Lab, Aug 2025)
- **Paper**: arxiv 2508.00414 — Deep research agent training
- **Technique**: synthesizes deep-research agent data via "Explore to Evolve: Scaling Evolved Aggregation Logic via Proactive Online Exploration"
- **Code**: github Tencent/CognitiveKernel-Pro
- **Surrogate application**: blueprint for V16 deep-research agent training. Composes with V14 swarm agents.

### 33. Critical Tokens Matter (Tencent AI Lab, ICML 2025)
- **Paper**: "Critical Tokens Matter: Token-Level Contrastive Estimation Enhances LLM Reasoning"
- **Technique**: token-level contrastive estimation during reasoning training. Identifies critical tokens (where errors propagate).
- **Surrogate application**: weight critical tokens higher in RL loss. Compose with DAPO/GRPO+.

---

## Section H — Continual Pretraining & Catastrophic Forgetting

### 34. ADEPT — Adaptive Layer Expansion (Oct 2025)
- **Paper**: arxiv 2510.10071 — "ADEPT: Continual Pretraining via Adaptive Expansion and Dynamic Decoupled Tuning"
- **Technique**: 2-stage — Stage 1 General-Competence Guided Selective Layer Expansion (duplicate non-critical layers); Stage 2 Dynamic Decoupled Tuning.
- **Measured**: +5.76% general, +5.58% target with only 15% params tuned, <50% training time.
- **Surrogate application**: Surrogate adds new domain (e.g. legal/medical) without forgetting. Better than SDFT for capacity expansion.

### 35. Spurious Forgetting (ICLR 2025)
- **Paper**: openreview ScI7IlKGdI — "Spurious Forgetting in Continual Learning of Language Models"
- **Technique**: identifies "spurious forgetting" (task-alignment drop, not knowledge loss). Proposes Freezing strategy on bottom layers.
- **Measured**: substantial improvements across 4 continual scenarios.
- **Surrogate application**: when adding new task, freeze bottom 8–12 layers. Cheaper + safer than full fine-tune.

### 36. IKnow — Instruction-Knowledge-Aware CPT (Oct 2025)
- **Paper**: arxiv 2510.20377
- **Technique**: maintains instruction-following during domain CPT.
- **Surrogate application**: ensure RLHF doesn't decay during continual learning slice.

---

## Section I — Reasoning, Curriculum, and Math

### 37. Self-Evolving Curriculum (May 2025)
- **Paper**: aimodels.fyi — "Self-Evolving Curriculum for LLM Reasoning"
- **Technique**: multi-armed bandit selects curriculum. Adaptive difficulty progression.
- **Surrogate application**: bandit-driven RL data selection. Compose w/ V14 RL frontier.

### 38. Curriculum Pretraining Learning Dynamics (2026)
- **Paper**: arxiv 2601.21698 — "Curriculum Learning for LLM Pretraining: Analysis of Learning Dynamics"
- **Technique**: tested 3 curricula (Age-of-Acquisition, word-frequency, Verb Variation) vs Random on 14M–410M Pythia.
- **Finding**: curricula reduce gradient noise + spectral saturation; matched-compute accuracy higher.
- **Surrogate application**: word-frequency curriculum cheap to implement. Test in 1B prototype.

### 39. LR-Decay vs Curriculum Conflict (Nov 2025)
- **Paper**: arxiv 2511.18903 — "How Learning Rate Decay Wastes Your Best Data in Curriculum-Based LLM Pretraining"
- **Finding**: high-quality data introduced late + decayed LR → wasted. Need moderate LR decay.
- **Surrogate application**: V16 schedule — keep LR ≥ 0.3·peak when ingesting top-quality data slices. Avoid cosine bottoming-out before high-quality phase.

### 40. What Makes a Good Curriculum (Oct 2025)
- **Paper**: arxiv 2510.19099 — "What Makes a Good Curriculum? Disentangling Effects of Data Ordering on LLM Mathematical Reasoning"
- **Technique**: 5 difficulty dimensions — Problem Difficulty, Model Surprisal, Confidence Margin, Predictive Uncertainty, Decision Variability.
- **Surrogate application**: rate every math/code sample on these 5 dims. Construct curriculum mixing surprisal-low (consolidation) + surprisal-high (challenge).

### 41. Beyond Random Sampling (Jun 2025)
- **Paper**: arxiv 2506.11300
- **Technique**: 15 metrics across 6 dimensions (info density, lexical diversity, readability, fertility, model-perceived difficulty, sequence length).
- **Surrogate application**: cheap per-doc metrics → curriculum without LLM judgments.

### 42. HERMES Tool-Augmented Math (Nov 2025)
- **Paper**: arxiv 2511.18760 — "HERMES: Towards Efficient and Verifiable Mathematical Reasoning"
- **Technique**: integrate Lean verification into reasoning process. Tool-calling for step verification.
- **Surrogate application**: train Surrogate to call Lean on math intermediate steps. Verifiable reward at step granularity.

### 43. Coconut — Continuous Latent Reasoning (TU Munich, INRIA collab)
- **Paper**: arxiv 2412.06769 — "Training Large Language Models to Reason in a Continuous Latent Space"
- **Technique**: last hidden state = "continuous thought", fed back as next input embedding directly. Enables BFS-style reasoning over multiple paths.
- **Surrogate application**: experimental — try on subset of reasoning tasks. Could be 2–3× more efficient than CoT for search-style problems.

---

## Section J — Repository / Long-Context / Schema

### 44. ProLong (Princeton NLP, ACL 2025)
- **Paper**: github princeton-nlp/ProLong — "How to Train Long-Context Language Models (Effectively)"
- **Technique**: continued pretrain + SFT from Llama-3-8B → 512K context. Thorough ablation on long-context data, SFT data, design choices.
- **Surrogate application**: copy ProLong recipe directly for V16 long-context phase. Open-source code available.

### 45. aiXcoder-7B-v2 / COLA (Mar 2025)
- **Paper**: arxiv 2503.15301 — "Training LLMs to Fully Utilize the Long Context in Repository-level Code Completion"
- **Technique**: Code Long-context Alignment (COLA) — data-driven, teaches model to focus on cross-file context. 128K token cross-file dataset.
- **Surrogate application**: include COLA data in V16 long-context slice. Repo-level completion improvement.

### 46. Code Graph Model CGM (May 2025)
- **Paper**: arxiv 2505.16901 — "Code Graph Model"
- **Technique**: jointly model semantic + structural info via GNN, fine-tune w/ LoRA. Specially designed Graph RAG.
- **Surrogate application**: experimental — add GNN side-channel for repo-level tasks. May not justify complexity at scale.

### 47. SchemaBench RL (Feb 2025)
- **Paper**: arxiv 2502.18878 — "Learning to Generate Structured Output with Schema Reinforcement Learning"
- **Technique**: 40K JSON schemas + RL with Fine-grained Schema Validator as reward.
- **Surrogate application**: train schema-adherence via RL. Reward = schema_validate(output). Tools/agents need this.

### 48. JSONSchemaBench (Jan 2025)
- **Paper**: arxiv 2501.10868 — "Generating Structured Outputs from Language Models"
- **Technique**: 10K real-world JSON schemas benchmark.
- **Surrogate application**: use as eval suite for V16 structured output capability.

### 49. Think Inside the JSON (Feb 2025)
- **Paper**: arxiv 2502.14905 — "Reinforcement Strategy for Strict LLM Schema Adherence"
- **Technique**: R1-style RL on 20K unstructured→structured + SFT on 10K reasoning samples.
- **Surrogate application**: warm-start V16 schema-following from this recipe.

---

## Section K — Niche / Contrarian / Surprising

### 50. Tiny Recursive Model TRM (Samsung SAIL Montreal, 2025)
- **Paper**: github SamsungSAILMontreal/TinyRecursiveModels
- **Technique**: 7M-param recursive model + simplified ACT. Beats 27M HRM + many large LLMs on ARC-AGI.
- **Measured**: 44.6% ARC-AGI-1, 7.8% ARC-AGI-2 with 7M params.
- **Surrogate application**: experimental side-arch for puzzle-style tasks. Run as auxiliary head, not main backbone.

### 51. Memory Decoder (SJTU + Shanghai AI Lab, ICLR 2025)
- **Paper**: ICLR 2025 — "Memory Decoder for Seamless Adaptation of Any LLM Without Parameter Tuning"
- **Technique**: external memory module attached to any LLM. Domain adaptation without param tuning.
- **Surrogate application**: deploy-time domain adaptation. User can plug in domain-memory without retraining Surrogate.

### 52. HyperCLOVA X THINK (Naver Cloud, Jun 2025)
- **Paper**: clova.ai HyperCLOVA_X_THINK_Technical_Report (Jun 2025)
- **Technique**: SFT → RM → RLVR → Length Controllability (LC) RL → RLHF. Multi-stage with length-controllable reasoning.
- **Surrogate application**: LC-RL stage — train explicit reasoning-length control. Token-budget aware reasoning.

### 53. LLM Squared / DiscoPOP (Sakana AI)
- **Paper**: sakana.ai/llm-squared — "Can LLMs invent better ways to train LLMs?"
- **Technique**: LLM proposes new training objective functions; evaluated empirically; iterate.
- **Surrogate application**: V16 meta-search — let Surrogate propose new losses for V17. Compose with ShinkaEvolve.

### 54. Goedel-Prover (Princeton/etc)
- **Paper**: goedel-lm.github.io
- **Technique**: 1.64M formal Lean statements via auto-formalization. Iterative prover bootstrap — each generation adds proofs the previous couldn't.
- **Surrogate application**: include in math/proof slice. Open dataset.

### 55. Industrial ST Code with Online Compiler Feedback (Bosch/Siemens area, IEEE LLM4Code 2025)
- **Paper**: dl.acm.org/doi/10.1109/LLM4Code66737.2025.00013 — "Training LLMs for Generating IEC 61131-3 Structured Text with Online Feedback"
- **Technique**: preference-based learning via online compiler feedback + LLM-based ST expert eval. Iterative refinement + new training samples generated.
- **Surrogate application**: blueprint for any DSL — replace ST with Terraform/CloudFormation/SQL/etc. Online compiler-as-supervisor pattern.

### 56. CAST — Code AST RAG (CMU, 2025)
- **Paper**: cs.cmu.edu/~sherryw/assets/pubs/2025-cast.pdf — "CAST: Enhancing Code Retrieval-Augmented Generation"
- **Technique**: AST-aware chunking for code retrieval. Respects syntactic units.
- **Surrogate application**: Surrogate's code-RAG layer should chunk by AST not lines. Better retrieval grounding.

### 57. TreeDiff (2025)
- **Paper**: arxiv 2508.01473 — "TreeDiff: AST-Guided Code Generation with Diffusion LLMs"
- **Technique**: syntax-aware diffusion — corruption process uses AST priors instead of random tokens.
- **Surrogate application**: experimental for diffusion-style code branch (if pursuing diffusion-LLM at all).

### 58. NSA — Native Sparse Attention (DeepSeek+PKU ACL 2025 Best Paper)
- **Paper**: ACL 2025 Best Paper Award — DeepSeek's Liang Wenfeng + PKU Yang Yaodong
- **Technique**: native sparse attention — won ACL 2025 best paper.
- **Surrogate application**: already in DeepSeek roadmap; consider for V16+ if architecture refresh.

### 59. AutoBug — LLM Symbolic Execution (NUS, Oct 2025)
- **Paper**: comp.nus.edu.sg/~gregory/papers/llm_sym_exe.pdf — "Large Language Model Powered Symbolic Execution"
- **Technique**: LLM as symbolic execution engine for C/Java/Python — direct reasoning over original PL.
- **Surrogate application**: train Surrogate to do symbolic execution as a tool. Useful for security/correctness.

### 60. AQLM + PV-Tuning (Yandex+MIT+ISTA+KAUST, 2025)
- **Paper**: yandex.com/company/news/11-04-2025
- **Technique**: AQLM + PV-Tuning quantization. Up to 8× compute reduction.
- **Surrogate application**: deploy-time quantization. Combine with TEAL activation sparsity for max efficiency.

### 61. YaFSDP (Yandex)
- **Tool**: open-source training accelerator. 25% faster LLM training, 20% GPU savings.
- **Surrogate application**: use in place of FSDP for V16 training. Free 20% cost cut.

### 62. ConstraintLLM (EMNLP 2025)
- **Paper**: aclanthology 2025.emnlp-main.809 — "ConstraintLLM: Neuro-Symbolic Framework for Industrial-Level"
- **Technique**: combine LLM with constraint programming solver.
- **Surrogate application**: Surrogate as constraint-formulator → hand off to OR-tools/Z3. Hybrid neuro-symbolic.

### 63. SymCode (Oct 2025)
- **Paper**: arxiv 2510.25975 — "SymCode: Neurosymbolic Mathematical Reasoning"
- **Technique**: training-free verifiable SymPy code generation w/ self-debug loop.
- **Surrogate application**: math eval pipeline — generate SymPy, execute, self-fix.

---

## Composition Map: V16 Stack Recommendation

Top 10 to integrate into V16 (composes with V15 / earlier rounds):

1. **Type-Constrained Decoding** (#1) — drop-in reward shaping, near-zero cost
2. **GRPO+ from rLLM** (#29) — replace DAPO; iterative context lengthening built-in
3. **LeanNavigator + APOLLO** (#5+#6) — formal-verification reward signal
4. **LR-Decay-Aware Curriculum** (#39) — fix common bug, free quality gain
5. **propella-1 multi-property scoring** (#22) — replace single-score data filter
6. **Execution Tuning E.T.** (#4) — line-level execution-trace pretraining slice
7. **RoPE↔NoPE Hybrid** (#8) — long-context architecture refresh
8. **PersonaHub elite slice** (#20) — role-comprehensive synthetic data
9. **ADEPT continual pretraining** (#34) — capacity expansion without forgetting
10. **YaFSDP** (#61) — 20% training cost cut

Stretch / experimental (1B prototype first):
- ShinkaEvolve (#31) for self-improvement loop
- Coconut (#43) for continuous-latent reasoning
- TRM (#50) as auxiliary puzzle-head
- Memory Decoder (#51) for deploy-time domain swap

Compose-don't-replace with V13/14:
- V14 RLVR + #29 GRPO+ + #6 APOLLO = stronger RL stage
- V13 role-comprehensive + #20 PersonaHub elite = scaled persona conditioning
- V14 long-horizon + #44 ProLong recipe = production long-context
- V15 SDFT continual + #34 ADEPT = proven CPT path

---

## Anti-Patterns Surfaced (don't repeat these mistakes)

- **Single-score data filtering** — propella-1 / QuRating / DataMan all show multi-dimensional > single-score (#22, #23)
- **LR cosine bottoming out before high-quality phase** — wastes best data (#39)
- **Random data ordering for math** — proven worse than curriculum on small-medium models (#38, #40)
- **Pure SiLU/SwiGLU activations everywhere** — ReLU² in some layers gives free sparsity gains (#13)
- **Full-finetune for new domains** — ADEPT shows 15% params + 50% time = better outcome (#34)
- **No RoPE alternative considered** — RoPE+NoPE hybrid works better for long-context (#8)
- **Synthetic data without faithfulness reward** — RePro shows quality+faithfulness dual reward (#25)
- **Reward = pass_tests only** — execution-semantics alignment (#3) + critical-tokens (#33) compose multiplicatively

---

## Missing / Not Found (gaps for V17 research)

- Specific Bosch industrial code papers beyond IEC 61131-3
- LG AI Research sparsity/efficient papers (search returned generic results)
- Kakao Brain novel 2025 paper (mostly product announcements)
- Baidu Research 2025 specific paper (largely covered by existing OSS round)
- Indian IIT specific 2025 LLM-training paper (mostly courses + survey)
- Concrete Cornell, IIT, U-Tokyo individual breakthrough paper (need targeted Phase-2 sweep)

---

## Sources (selected)

- ETH Zurich Type-Constrained: https://arxiv.org/abs/2504.09246
- Berkeley rLLM: https://www2.eecs.berkeley.edu/Pubs/TechRpts/2025/EECS-2025-123.html
- Princeton ProLong: https://github.com/princeton-nlp/ProLong
- Sakana ShinkaEvolve: https://arxiv.org/abs/2509.19349
- Tencent CognitiveKernel-Pro: https://arxiv.org/pdf/2508.00414
- LeanNavigator: https://arxiv.org/pdf/2503.04772
- APOLLO: https://arxiv.org/html/2505.05758v5
- AceCoder: https://arxiv.org/html/2502.01718v4
- Execution Tuning E.T.: https://arxiv.org/abs/2503.05703
- CodeRL+: https://arxiv.org/abs/2510.18471
- ADEPT: https://arxiv.org/abs/2510.10071
- propella-1: https://arxiv.org/pdf/2602.12414
- SchemaBench RL: https://arxiv.org/abs/2502.18878
- RePro: https://arxiv.org/html/2510.10681v1
- HyperCLOVA X THINK: https://clova.ai/cdn/media/2025/06/HyperCLOVA_X_THINK_Technical_Report.pdf
- Samsung TRM: https://github.com/SamsungSAILMontreal/TinyRecursiveModels
- Yandex LLM compression: https://yandex.com/company/news/11-04-2025
- Sparsing Law: https://arxiv.org/abs/2411.02335
- SeerAttention: https://arxiv.org/abs/2410.13276
- Curriculum Pretraining Dynamics: https://arxiv.org/abs/2601.21698
- LR-Decay vs Curriculum: https://arxiv.org/html/2511.18903
- Self-Improving Pretraining: https://arxiv.org/html/2601.21343v1
- SwallowCode rewriting: https://arxiv.org/abs/2505.02881
- aiXcoder-7B-v2 COLA: https://arxiv.org/abs/2503.15301
- Code Graph Model: https://arxiv.org/pdf/2505.16901
- ZeroSearch (Alibaba): https://techxplore.com/news/2025-05-alibaba-zerosearch-method-simulated-results.html
- IEC 61131-3 LLM4Code: https://dl.acm.org/doi/10.1109/LLM4Code66737.2025.00013

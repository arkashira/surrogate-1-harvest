---
title: V14 arXiv + GitHub Long-Tail Sweep (May 2026)
created: 2026-05-01
purpose: Surface what V13 + previous research streams MISSED — niche, contrarian, surprising
horizon: Mar–Apr 2026 (last 60–90 days)
status: Drop-in research feed for V14+ Surrogate-1 trainer roadmap
tags: [trends-2026, surrogate-1, v14, arxiv, github, long-tail, agentic-rl, training-tooling]
---

# V14 arXiv + GitHub Long-Tail Sweep (May 2026)

> Sweeping the gaps left by V13 streams. Bias toward Mar–Apr 2026 papers, lesser-known but high-leverage. Each entry includes: paper/repo, why it surprises, V14 wire-in, T4×2 feasibility (T4 = 16GB ×2 → 32GB total — still primary student box for V14 ablations).

Existing V13 notes already cover: PRMs at scale, GRPO/DAPO basics, agentic RL surveys, Qwen3 + DeepSeek + Hermes top-tier releases, Voyager-style skill trees, autonomous 24×7, anti-hallucination. **This file = the leftovers worth chasing.**

---

## TOP 10 SURPRISES (executive summary at top)

| # | Finding | Paper / Repo | V14 Wire-in (1-line) |
|---|---------|--------------|----------------------|
| 1 | DC-SFT on difficulty-curated data **beats RL on OOD** with lower compute | arXiv 2602.10815 | Add `difficulty_score` filter to V14 SFT pipeline, skip RL on easy/hard tails |
| 2 | Dense per-turn rewards **catastrophically degrade** vs sparse — discriminative power matters more than reward density | arXiv 2604.02869 | Default V14 multi-turn RL = sparse outcome reward + iterative reward calibration |
| 3 | OCR-Memory: render history → image, retrieve via locate-and-transcribe → 10× context compression | arXiv 2604.26622 | Add as optional memory backend for long-horizon V14 agent traces |
| 4 | Abstract Chain-of-Thought: 11.6× fewer reasoning tokens via reserved-vocab discrete latents | arXiv 2604.22709 | Train V14 small model with reserved `<think_*>` token vocab → cheaper inference |
| 5 | Outcome rewards **do not guarantee** verifiable / causally important reasoning (CIR + SR metrics expose this) | arXiv 2604.22074 | Add CIR/SR auto-eval to V14 reward pipeline before any merge |
| 6 | Endless Terminals procedurally generates ∞ Docker tasks with no human label — Qwen2.5-7B 10.7→53.3% | arXiv 2601.16443 | Replace V13 hand-curated tasks with this generator; pipe to HF Space queue |
| 7 | SWE-MiniSandbox: container-free RL via mount-namespace + chroot — **no Docker needed** | arXiv 2602.11210 | Drop Docker from V14 RL ingestion → 5–10× rollout throughput on Lightning L40S |
| 8 | Sol-Ver self-play solver-verifier closes loop: model trains both code-gen + test-gen against itself | arXiv 2502.14948 | Bake into V14 as a phase-2 self-play after SFT |
| 9 | Murphy: multi-turn GRPO with feedback-conditioned rollout TREE (not chain) + trajectory-level credit | arXiv 2511.07833 | V14 trajectory rollout = tree of execution-feedback branches, GRPO on tree |
| 10 | Weak-to-strong distillation accelerates *training itself* — early phase warmup using a frozen weaker teacher → up to 4.8× epoch speedup | arXiv 2604.15451 | Use V13 checkpoint as frozen weak teacher for V14 SFT warmup → faster convergence |

### "Should-have-found-earlier" patterns

- **Reward discriminative power > reward density** (#2) — V13 assumed dense was always better.
- **Difficulty curation collapses the SFT-vs-RL gap** (#1) — would have saved RL compute since V11.
- **Container-free RL** (#7) — sandboxing was the bottleneck; namespaces solve it.
- **Skill-banks + memory are the SAME operation** (Experience Compression Spectrum, arXiv 2604.15877) — V12/V13 had them as separate modules.
- **Self-play debate is "secretly adversarial imitation"** (arXiv 2602.01357) — explains why V13 self-play stalled; switching to χ²-divergence variational fixes stability.

---

## 1. RL ALGORITHM LONG-TAIL (beyond GRPO/DAPO)

### 1.1 GiGPO — Group-in-Group Policy Optimization
- **Paper**: arXiv 2505.10978 (v3 Oct 2025, dynamic variant Jan 2026)
- **Surprise**: Two-dimensional credit assignment (within-group + across-group). GiGPOdynamic **outperforms DAPO**, hitting 75.0% on WebShop. Multi-turn agent training was the killer use-case.
- **V14**: Replace flat-GRPO loss in V14's multi-turn agent finetuner with GiGPO. ~30 LOC change in trainer.
- **T4×2**: Yes — same memory profile as GRPO; pure loss change.

### 1.2 Multi-Turn Iterative Reward Calibration
- **Paper**: arXiv 2604.02869 (Apr 2026)
- **Surprise**: Reasonable-looking dense per-turn rewards **catastrophically degrade** training vs sparse. Root cause = misalignment between reward discriminative power and advantage-computation scale. Fix = iterative reward calibration that learns the reward scale online.
- **V14**: When designing per-turn shaping in V14, **start sparse**, only add density via calibration. Ship a `RewardCalibrator` class.
- **T4×2**: Yes — adds one running-stats buffer.

### 1.3 GASP — Guided Adversarial Self-Play
- **Paper**: arXiv 2602.00173 (Feb 2026)
- **Surprise**: Single-model min-max with a "polluter" sub-policy injecting failure → robustness to OOD. Outcome verification only (no rubric).
- **V14**: Add `PolluterAgent` mode to self-play loop — same model, opposite objective. Only requires outcome reward (which V14 already has).
- **T4×2**: Tight — needs 2 inference paths; doable with gradient checkpointing.

### 1.4 χ²-Divergence Self-Play (replaces KL)
- **Paper**: arXiv 2602.01357 (Feb 2026) — "Your Self-Play Algorithm is Secretly an Adversarial Imitator"
- **Surprise**: Standard self-play SFT = adversarial imitation in disguise. Switching from KL to **χ²-divergence with bounded rewards** stabilizes training where previous self-play diverged.
- **V14**: When self-play instability hits (V13 saw this), swap to χ². ~20 LOC.
- **T4×2**: Yes — pure loss change.

### 1.5 SFT-then-RL beats Mixed-Policy
- **Paper**: arXiv 2604.23747 (Apr 2026)
- **Surprise**: Sequencing **SFT then RL** outperforms mixed-policy training (alternating SFT/RL or weighted). Explicit phase-separation > clever blending.
- **V14**: Lock V14 pipeline as strict 3-phase: CPT → SFT → RL. No mixed-policy experiments. Saves engineering time.
- **T4×2**: Yes — same compute, just scheduling.

---

## 2. PROCESS REWARD MODELS — Niche Variants

### 2.1 FunPRM — Function-as-Step PRM
- **Paper**: arXiv 2601.22249 (Jan 2026)
- **Surprise**: Treat **each function** in generated code as a PRM step (not each token / line). Beats existing TTS methods on LiveCodeBench + BigCodeBench across 5 base LLMs.
- **V14**: When training V14 PRM for code, segment by function definition. Pair with AST-based segmenter (already in V13 tooling).
- **T4×2**: Yes — PRM is small (~100M typical), fits comfortably.

### 2.2 PRMs from PDDL (Cross-domain transfer)
- **Paper**: arXiv 2604.17957 (Apr 2026)
- **Surprise**: Augment PRM training data with ~1M PDDL-derived planning steps → improvements in both math AND non-math reasoning. Cross-domain PRM signal.
- **V14**: Add PDDL synthetic step data to V14 PRM training mix. Free leverage from planning corpus.
- **T4×2**: Yes — data work, no new compute.

### 2.3 VeRPO — Verifiable Dense Reward via Difficulty-Weighted Tests
- **Paper**: arXiv 2601.03525 (Jan 2026)
- **Surprise**: Don't binary-reward "all tests pass". Estimate per-test difficulty during training, weight reward by it. **+8.83% pass@1** with negligible overhead.
- **V14**: Replace binary test-pass reward with VeRPO weighting. Drop-in.
- **T4×2**: Yes — minor stats tracking.

### 2.4 CIR + SR Metrics for Outcome-RL
- **Paper**: arXiv 2604.22074 (Apr 2026)
- **Surprise**: Outcome rewards don't guarantee the chain-of-thought is causally important. **Causal Importance of Reasoning (CIR)** and **Sufficiency of Reasoning (SR)** metrics expose this gap.
- **V14**: Add CIR + SR as auto-eval gates. Reject any V14 checkpoint where CIR drops below threshold even if outcome metrics improve.
- **T4×2**: Yes — eval-time only.

---

## 3. ENVIRONMENT-RL — Sandbox / Terminal / Code

### 3.1 Endless Terminals
- **Paper**: arXiv 2601.16443 (Jan 2026, v3)
- **Surprise**: Fully autonomous Docker-task generator + completion-test synthesizer + solvability filter. Llama-3.2-3B 4.0→18.2%, Qwen2.5-7B 10.7→53.3%, Qwen3-8B 42.6→59.0%.
- **V14**: This **replaces** the hand-curated terminal task list. Pipe generated tasks → Lightning H200 RL queue.
- **T4×2**: Generation YES (CPU-bound). RL training on small model YES.

### 3.2 SWE-MiniSandbox — Container-Free RL
- **Paper**: arXiv 2602.11210 (Feb 2026)
- **Surprise**: Per-instance **mount namespaces + chroot** — no Docker daemon. 5–10× faster rollouts at scale.
- **V14**: Drop Docker for V14 RL ingestion. Major throughput win on Lightning Studios (which already runs root-capable Linux).
- **T4×2**: Yes — actually *helps* T4 because no Docker overhead.

### 3.3 TermiGen — Generator-Critic with Error Injection
- **Paper**: arXiv 2602.07274 (Feb 2026)
- **Surprise**: Multi-agent refinement loop builds task + Docker, then a Critic **actively injects errors** into trajectory generation → robustness to real-world buggy environments.
- **V14**: Add `error_injection_pass` to V14 trajectory generator. Trains V14 to recover, not just succeed.
- **T4×2**: Yes — generation phase only.

### 3.4 TerminalTraj — 50K Verified Trajectories
- **Paper**: arXiv 2602.01244 (Feb 2026)
- **Surprise**: 32K Docker images → 50,733 verified trajectories across 8 domains. Public dataset.
- **V14**: Pull this dataset directly into V14 SFT mix as terminal-skills supplement. No re-generation needed.
- **T4×2**: Data only.

### 3.5 OpenClaw-RL
- **Repo**: github.com/Gen-Verse/OpenClaw-RL (Apr 2026, ~210K stars)
- **Surprise**: Fully async RL framework that turns conversations into training signals. Combines **Binary RL + OPD (on-policy distillation)** in a unified recipe — outperforms either alone.
- **V14**: Borrow Binary-RL+OPD combined loss. The async rollout architecture also worth lifting.
- **T4×2**: Framework runs YES. Async helps T4 (overlap CPU rollout w/ GPU train).

---

## 4. SYNTHETIC DATA — Beyond Self-Instruct

### 4.1 Matrix — Peer-to-Peer Multi-Agent Synthetic Data
- **Paper**: arXiv 2511.21686 (Apr 2026)
- **Surprise**: P2P agents collaborate to generate data → **2–15× higher throughput** at same hardware. Specialization beats centralized generation.
- **V14**: Run V14 data-gen as P2P swarm on Lightning multi-node. Each node specializes (parser / mutator / verifier).
- **T4×2**: Tight on T4 (each agent needs inference). Better on L40S/H200.

### 4.2 AgentPack — 1.8M Real Agent Edits
- **Paper**: arXiv 2509.21891 (v2 — updated 2025-Q4)
- **Surprise**: 1.8M code edits **co-authored by Claude Code + Codex + Cursor Agent** on public GitHub. Real production agent traces, not synthetic. Fine-tuning on this beats hand-crafted data.
- **V14**: Critical — pull AgentPack into V14 SFT mix as the "edit" component. No prior V13 note mentions this.
- **T4×2**: Data only. ~10s of GB after dedup.

### 4.3 MEnvData-SWE-Trajectory
- **Dataset**: huggingface.co/datasets/ernie-research/MEnvData-SWE-Trajectory (2026)
- **Surprise**: 3,872 full agent execution traces across 942 repos × 10 languages. OpenHands scaffolding + Claude Sonnet 4.5. **Multi-language** is rare.
- **V14**: Mix into V14 to break Python-monoculture. Adds Java/TS/Rust/Go/C++ trajectories.
- **T4×2**: Data only.

### 4.4 SWE-CI — Commit-to-Commit Trajectories
- **Dataset**: huggingface.co/datasets/skylenage/SWE-CI
- **Surprise**: Models codebase **evolution** between commits, not just issue→PR. Captures refactor/migration patterns missing from SWE-bench.
- **V14**: Add as long-horizon refactor training signal.
- **T4×2**: Data only.

### 4.5 CoT-Self-Instruct
- **Paper**: arXiv 2507.23751
- **Surprise**: Reason+plan via CoT BEFORE generating new synthetic example. Beats s1k + OpenMathReasoning on MATH500/AIME/GPQA.
- **V14**: Replace plain Self-Instruct with CoT-Self-Instruct in V14 augmentation pipeline.
- **T4×2**: Generation YES.

### 4.6 Genetic Instruct — Coding-specific
- **Paper**: arXiv 2407.21077
- **Surprise**: Evolutionary mutation+crossover on coding instructions (genetic algorithm style). Underused in V13 stack.
- **V14**: Add genetic-instruct mutation operator to coding-data generator.
- **T4×2**: Generation YES.

---

## 5. LATENT REASONING — Beyond Token CoT

### 5.1 Abstract-CoT (Reserved Vocab Latents)
- **Paper**: arXiv 2604.22709 (Apr 24–27, 2026)
- **Surprise**: Reserve tokens like `<think_001>...<think_K>` as a discrete latent vocabulary; model emits short sequences from this vocab instead of natural-language CoT. **11.6× fewer reasoning tokens**, equal accuracy.
- **V14**: Pre-train V14 small model with reserved-vocab latent CoT pretraining task. Massive inference cost reduction.
- **T4×2**: YES — actually *easier* on T4 (shorter sequences).

### 5.2 LLM Reasoning Is Latent (Position Paper)
- **Paper**: arXiv 2604.15726 (Apr 2026)
- **Surprise**: Surface CoT is unreliable as reasoning evidence. Should treat **latent-state trajectories** as primary object of study.
- **V14**: Implication for evals — instrument V14 to log latent activations on reasoning probes, not just token outputs.
- **T4×2**: Hooks only, no training change.

### 5.3 Coconut — Continuous Latent Space Reasoning
- **Paper**: arXiv 2412.06769
- **Surprise**: Train LLM to reason in continuous embedding space via direct hidden-state feedback. Decouples thinking from token vocabulary.
- **V14**: Considered for V14.5 — needs custom train loop, more complex than Abstract-CoT.
- **T4×2**: Yes — ~same memory.

### 5.4 Recursive Language Models (RLM)
- **Paper**: arXiv 2512.24601 (v2)
- **Surprise**: Finetune Qwen3-8B on filtered trajectories where it **calls itself** as sub-routine on shorter contexts. Probing + recursive sub-calling generalize across domains.
- **V14**: Add RLM-style sub-call data to V14 SFT mix. Encourages decomposition behavior.
- **T4×2**: Train YES (single 8B in qLoRA fits 32GB).

### 5.5 Recursive Self-Aggregation (RSA)
- **Paper**: arXiv 2509.26626
- **Surprise**: Test-time evolutionary aggregation — combines parallel + sequential scaling. Solutions evolve via aggregation ops.
- **V14**: Inference-time technique — wire into V14 inference layer for hard problems.
- **T4×2**: Inference YES.

---

## 6. MEMORY / SKILL LIBRARY UNIFICATION

### 6.1 Experience Compression Spectrum
- **Paper**: arXiv 2604.15877 (Apr 2026)
- **Surprise**: Memory ↔ Skills ↔ Rules are the **same operation** at different compression granularities. V12/V13 treated them separately.
- **V14**: Refactor V14 agent state into single `ExperienceCompressor` module with granularity knob. Big architectural simplification.
- **T4×2**: Yes — refactor.

### 6.2 D2Skill — Dynamic Dual-Granularity Skill Bank
- **Paper**: arXiv 2603.28716 (Mar 2026)
- **Surprise**: Maintain skills at BOTH task-granularity (high-level recipes) and step-granularity (atomic ops). Auto-prune low-value entries.
- **V14**: Replace V13 single-tier skill library with dual-tier. Add reuse-frequency pruning.
- **T4×2**: Yes — storage layer only.

### 6.3 OCR-Memory
- **Paper**: arXiv 2604.26622 (Apr 2026)
- **Surprise**: Render historical traces to **images with visual anchors**, retrieve via locate-and-transcribe. Avoids hallucination from generative recall. Long-horizon retention with minimal prompt tax.
- **V14**: Add OCR-Memory backend as V14 alternative when context > 200K. Rendering is cheap.
- **T4×2**: Yes — render is CPU; transcribe needs VLM (use small one or remote).

### 6.4 MemoryArena — Surprising Eval
- **Paper**: arXiv 2603.07670 (cited finding)
- **Surprise**: Models scoring near-perfect on **LoCoMo** drop to **40–60%** on MemoryArena. Passive recall ≠ decision-relevant memory.
- **V14**: Add MemoryArena to V14 eval suite. LoCoMo alone is misleading.
- **T4×2**: Eval-only.

### 6.5 Voyager Without Skill Library = 15.3× Slower
- **Cited in**: arXiv 2604.15877 / Voyager follow-ups
- **Surprise**: Skill library WAS the performance — not the LLM core. V13 underweighted this.
- **V14**: Treat skill bank as first-class subsystem with its own tests, not afterthought.

---

## 7. TRAINING EFFICIENCY — Optimizers, Distillation, MoE

### 7.1 Muon Optimizer (now scalable)
- **Paper**: arXiv 2502.16982 (KIMI scaling report)
- **Surprise**: Muon hits **~2× compute efficiency** vs AdamW. Scales to 16B-MoE / 5.7T tokens with no hyperparameter retune. Lighter memory than AdamW.
- **V14**: Default V14 trainer optimizer = Muon (with weight decay + per-param scale fix from the report).
- **T4×2**: YES — actually MORE attractive on T4 (lower memory).

### 7.2 MUD — MomentUm Decorrelation (post-Muon)
- **Paper**: arXiv 2603.17970 (Mar 2026)
- **Surprise**: Iteration on Muon — decorrelates momentum across params for further speedup.
- **V14**: Try as ablation against Muon baseline.
- **T4×2**: Yes.

### 7.3 Newton-Muon
- **Paper**: arXiv 2604.01472 (Apr 2026)
- **Surprise**: Second-order info added to Muon → faster but heavier.
- **V14**: Maybe in V14.5 if memory budget allows.
- **T4×2**: Tight — second-order memory cost.

### 7.4 Weak-to-Strong Distillation (training accel)
- **Paper**: arXiv 2604.15451 (Apr 2026)
- **Surprise**: Use a **frozen weaker** teacher only in the **early phase** of strong-student training → **4.8× epoch speedup** on ImageNet, 1.7× on COCO. Underused for LLMs.
- **V14**: Use V13 as frozen weak teacher for V14 SFT warmup. Burst speedup w/ no extra compute.
- **T4×2**: YES — student size dictates fit; teacher inference only.

### 7.5 Rethinking On-Policy Distillation (OPD recipe)
- **Paper**: arXiv 2604.13016 (Apr 2026)
- **Surprise**: Same-family 1.5B and 7B teachers are **distributionally indistinguishable** to the student. Two conditions for OPD success: shared thinking patterns + teacher genuinely knows something student doesn't.
- **V14**: When picking V13→V14 teacher, prefer same-family. Don't mix Qwen-teacher → Llama-student naively.

### 7.6 CoMoL — Mixture of LoRA Experts via Core Space Merge
- **Paper**: arXiv 2603.00573 (Mar 2026)
- **Surprise**: Dynamic core-space merging of LoRA experts. MoE benefits without full MoE arch.
- **V14**: For domain-specialist V14 variants (TS / Java / etc.), train per-domain LoRAs then CoMoL-merge for a single small unified model.
- **T4×2**: YES — LoRAs are cheap.

### 7.7 MoBiE — Binary Experts for MoE
- **Paper**: arXiv 2604.06798 (Apr 2026)
- **Surprise**: First binarization framework for MoE LLMs (joint SVD + global gradient). Could shrink 100B-MoE to T4-deployable.
- **V14**: V14.5 inference target if we go MoE.

### 7.8 LoRAFusion — 4-bit QLoRA Kernels
- **Paper**: arXiv 2510.00206
- **Surprise**: New kernel avoids dequantization step in QLoRA → faster forward.
- **V14**: Drop into V14 trainer if QLoRA path used.
- **T4×2**: YES — native target.

---

## 8. AGENT FRAMEWORKS / OSS — Lesser-Known

### 8.1 Hermes Agent
- **Repo**: Nous Research (47K stars in 2 months, MIT)
- **Surprise**: "Self-evolving" agent. v0.8.0 (Apr 8 2026) — 209 PRs, 82 issues, browser-use built-in.
- **V14**: Study as reference architecture for self-evolving harness. Borrow auto-notification pattern.

### 8.2 Superpowers Framework
- **Repo**: github.com/obra/superpowers (Apr 2026)
- **Surprise**: Composable **skills** as first-class objects for coding agents. Anthropic-skills inspired but more agent-focused.
- **V14**: Use as outer-loop framework for V14 agent shell. Cleaner than rolling our own.

### 8.3 GenericAgent
- **Repo**: github.com/lsdefine/GenericAgent
- **Surprise**: Self-evolving from **3.3K-line seed**, achieves full system control with **6× lower token consumption**.
- **V14**: Tiny seed → big capability. Architecture pattern worth studying for V14 bootstrap.

### 8.4 ml-intern (HuggingFace)
- **Tool**: HF released Apr 21, 2026 — open-source AI agent automating LLM post-training workflow
- **Surprise**: **32% on PostTrainBench using only Qwen3-1.7B** vs Claude Code at 22.99%. Tiny model, big result. Built on smolagents.
- **V14**: Study smolagents architecture; adopt as harness for V14 auto-experiments.

### 8.5 verl + OpenRLHF Comparison
- **Repos / arXiv 2405.11143v6** — 2026 update
- **Surprise**: OpenRLHF hits **1.22–1.68× speedup** over verl (1.5B → 14B). Picked verl in V13 — should reconsider.
- **V14**: Benchmark both. If win confirmed, swap.
- **T4×2**: OpenRLHF is Ray-native, scales fine on small clusters.

### 8.6 Unsloth Ultra-Long-Context RL
- **Tool**: Unsloth Jan 2026 release — **380K context window RL**
- **Surprise**: RL with that context on consumer GPUs was thought unreachable.
- **V14**: For V14 long-horizon trajectory training, Unsloth path is the cheapest.
- **T4×2**: Yes — Unsloth's whole reason to exist.

---

## 9. CURRICULUM / DIFFICULTY / DATA-CENTRIC

### 9.1 ADARFT — Adaptive Difficulty
- **Paper**: arXiv 2504.05520 (2026)
- **Surprise**: Maintain target difficulty band based on running reward. Both accuracy AND training-efficiency improve. Underused.
- **V14**: Wire into V14 RL scheduler. Replaces fixed-difficulty epochs.
- **T4×2**: YES — pure scheduler.

### 9.2 DC-SFT — Difficulty-Curated SFT (THE BIG ONE)
- **Paper**: arXiv 2602.10815 (Feb 2026)
- **Surprise**: SFT with explicit difficulty filtering **surpasses RL on OOD**. Reframes the entire SFT-vs-RL debate. RL's "advantage" was implicit difficulty filtering.
- **V14**: V14 default = DC-SFT first, RL only on remaining gap. Saves significant compute.
- **T4×2**: YES — preferred pipeline for T4 (avoids RL entirely on most data).

### 9.3 Long-Response SFT Data Wins
- **Paper**: arXiv 2402.06094 (still cited in 2026 work)
- **Surprise**: Selecting **long-response** instances for SFT beats curating by quality+diversity. Counter-intuitive.
- **V14**: Add response-length-percentile filter to V14 SFT data selection.
- **T4×2**: Data work.

### 9.4 GRAPE — Model-Dependent SFT Curation
- **Paper**: arXiv 2502.04194
- **Surprise**: Pick training responses with **highest probability under target model**. Aligns SFT data to base distribution → better generalization.
- **V14**: Pre-score V14 SFT corpus with V13-frozen, keep top-quartile probability.
- **T4×2**: Inference scoring — slow but fits.

### 9.5 TuluTalk — Smaller Beats Bigger
- **Paper**: arXiv 2506.06522
- **Surprise**: 23% smaller than SmolTalk, 14% smaller than Tulu — yet outperforms across benchmarks. Quality > quantity at this point.
- **V14**: Use TuluTalk recipe as V14 SFT base mix. Free leverage.
- **T4×2**: Data only.

---

## 10. EVAL / BENCHMARKS — Underused

### 10.1 SWE-Bench Pro
- **Paper**: arXiv 2509.16941 + ScaleAI/SWE-bench_Pro on HF
- **Surprise**: Long-horizon enterprise tasks (hours-to-days). GPT-5 only 23.3% pass@1. Replacement for SWE-Bench Verified once that saturates.
- **V14**: Adopt as primary V14 eval. Saturates slower than SWE-Verified.

### 10.2 SWE-Bench++
- **Paper**: arXiv 2512.17419
- **Surprise**: Auto-generated benchmark from live PRs across **11 languages**. Covers bugs AND features.
- **V14**: Multi-language eval gate.

### 10.3 SWE-EVO
- **Paper**: arXiv 2512.18470 (Jan 2026)
- **Surprise**: Coding agents on **codebase evolution** tasks (not single PRs). Models the refactor/migration work.
- **V14**: Long-horizon eval.

### 10.4 LongCLI-Bench
- **Paper**: arXiv 2602.14337 (Feb 2026)
- **Surprise**: First benchmark for **long-horizon agentic CLI** tasks. Closer to real DevOps work than web-agent tasks.
- **V14**: Critical for our DevOps angle — V14 should target this.

### 10.5 Odysseys
- **Paper**: arXiv 2604.24964 (Apr 2026)
- **Surprise**: 200 long-horizon **real-internet** web tasks. Strongest models 44.5%. Live evaluation.
- **V14**: Eval for the web-browsing layer if V14 takes it.

### 10.6 AgentVista
- **Paper**: arXiv 2602.23166 (Feb 2026)
- **Surprise**: Multimodal long-horizon tool-use benchmark. SOTA models fail badly. Underused.
- **V14**: When V14 adds VLM path, this is the eval.

### 10.7 MemoryArena
- **Cited in**: arXiv 2603.07670
- **Surprise**: LoCoMo near-perfect → MemoryArena 40–60%. Real test of decision-relevant memory.
- **V14**: Memory eval gate.

### 10.8 BigCodeBench / LiveCodeBench
- **Standard**: cited in FunPRM, Murphy
- **Surprise**: V13 already has these — but combine with **trajectory-level** scoring per arXiv 2604.10015 (financial paper, but trajectory scoring framework is general).

### 10.9 Causal Importance of Reasoning (CIR) + Sufficiency of Reasoning (SR)
- **Paper**: arXiv 2604.22074
- **Surprise**: **First metrics** that detect when outcome-RL gives the right answer for the wrong CoT. Shipped as auto-eval.
- **V14**: Block any V14 release where CIR drops vs V13 baseline.

### 10.10 PostTrainBench (HF ml-intern)
- **Bench**: Used in HF ml-intern release
- **Surprise**: Tests post-training-workflow automation. Direct fit for self-improvement claims.
- **V14**: Run V14's auto-experiment harness against this.

---

## 11. CROSS-DOMAIN / CONTRARIAN

### 11.1 Cross-Domain RL Transferability
- **Paper**: arXiv 2507.00432
- **Surprise**: RL-tuned models **generalize across domains**, SFT-tuned models **forget**. Math/Code/Science transfer cheaply. Other domains need in-domain training.
- **V14**: Use cheap-to-rollout domains (math) to bootstrap RL signal that transfers to code. V13 didn't exploit this.

### 11.2 Think In Games (TiG)
- **Paper**: arXiv 2508.21365
- **Surprise**: Game-RL for LLM reasoning. Procedural understanding via direct game env. Underrated for coding (games = code-like envs).
- **V14**: Add a small game-env phase to V14 pretrain mix.

### 11.3 OpenGame — Agentic Coding for Games
- **Paper**: arXiv 2604.18394 (Apr 2026)
- **Surprise**: 3-stage pipeline (CPT→SFT→RL) building **GameCoder-27B** on Qwen3.5-27B. Tight reference for V14 staged pipeline.
- **V14**: Mirror their staged recipe.

### 11.4 Agent-as-Tool Hierarchical RL
- **Paper**: arXiv 2507.01489
- **Surprise**: Treat **other agents as tools** in a hierarchy. Decision-making flows down. Different from flat tool-calling.
- **V14**: V14 sub-agents become first-class tools — clean architectural pattern.

### 11.5 The Consensus Trap
- **Paper**: arXiv 2604.17139 (Apr 2026)
- **Surprise**: Multi-agent voting **collapses** when corrupted agents form local majority. Token-level collaboration fix. V13's debate-vote setup is vulnerable.
- **V14**: Replace majority-vote ensembling with token-level consensus.

### 11.6 Cumulative Skill Creation (CASCADE)
- **Cited in**: skill-bank surveys
- **Surprise**: Voyager → CASCADE chains autonomous skill evolution.
- **V14**: Reference for skill-evolution loop.

### 11.7 Math Reasoning Transfers — But Not Always
- **Paper**: arXiv 2507.00432 (same as 11.1)
- **Surprise**: General math gains don't always carry to general capability — depends on pretrain exposure of target domain. Code/Science transfer; non-pretrained domains don't.
- **V14**: Don't assume math RL fixes everything. Check pretrain mix first.

### 11.8 4 Failure Modes Found in Autonomous AI Research
- **Paper**: arXiv 2601.03315
- **Surprise**: Six **recurring** failure modes in 4-attempt LLM-driven research: training-data-defaults bias, implementation drift, memory degradation, false-success declaration, weak domain intelligence, weak experimental taste. Very direct for V14 self-improvement.
- **V14**: Add explicit detectors for each of these 6 modes in V14 self-improvement loop.

---

## 12. INFRASTRUCTURE / OPS — Niche

### 12.1 Block-Sparse FlashAttention
- **Paper**: arXiv 2512.07011
- **Surprise**: Skips ~50% compute on long context with calibrated thresholds. Drop-in.
- **V14**: When V14 hits long-context training, swap in BSFA.
- **T4×2**: YES — saves memory.

### 12.2 ASEntmax — Adaptive Sparsity Long Context
- **Paper**: arXiv 2506.16640
- **Surprise**: 95.3% accuracy at 65K tokens, **trained on just 64 tokens** — 1000× length extrapolation.
- **V14**: Train V14 short, deploy long. Massive cost saver.
- **T4×2**: YES — short training fits naturally.

### 12.3 Sparse Feature Attention (SFA / FlashSFA)
- **Paper**: arXiv 2603.22300 (Mar 2026)
- **Surprise**: 2.5× speedup, 20% perplexity gain, 41% KV-cache reduction.
- **V14**: Production inference layer for V14.
- **T4×2**: YES — reduced KV-cache helps T4.

### 12.4 MegaScale-MoE
- **Paper**: arXiv 2505.11432 (Apr 2026 EuroSys)
- **Surprise**: 1.88× MFU on 352B-MoE / 1440 GPUs vs Megatron-LM.
- **V14**: Reference if we ever scale to MoE on Lightning H200 cluster.

### 12.5 Conformal Thinking — Risk-Controlled Reasoning
- **Paper**: arXiv 2602.03814
- **Surprise**: Reframes thinking-budget as **risk-control** — distribution-free bound on error rate. Optimal stop.
- **V14**: V14 inference layer adopts conformal-stopping for thinking-budget.

### 12.6 Increasing Thinking Budget Is Not All You Need
- **Paper**: arXiv 2512.19585
- **Surprise**: Many "complex reasoning" wins are just **more compute allocated** — not algorithmic superiority. Ablation matters.
- **V14**: When evaluating V14 reasoning improvements, **fix the compute budget** before claiming alg wins.

---

## 13. REVERSE / NEGATIVE RESULTS

### 13.1 CoT Reasoning Is Not Always Faithful
- **Paper**: arXiv 2503.08679 (v4)
- **Surprise**: CoT can be unfaithful even on bias-free prompts.
- **V14**: Add CoT-faithfulness probe to evals.

### 13.2 Lost in the Noise — Distractors Cause 80% Drop
- **Paper**: arXiv 2601.07226 (Jan 2026)
- **Surprise**: **80% drop** with contextual distractors. SFT reduces robustness via catastrophic forgetting.
- **V14**: Add distractor-injection to V14 robustness eval. Don't over-SFT.

### 13.3 Mixed Math-Only SFT → 3.1→12% math, 81→16.5% NLI
- **Paper**: arXiv 2512.13706
- **Surprise**: Single-domain SFT collapses other capabilities. Mixed-training mandatory.
- **V14**: Always train V14 on mixed-domain even if specializing.

### 13.4 Self-Jailbreaking via Benign Reasoning Training
- **Paper**: arXiv 2510.20956
- **Surprise**: Models post-trained on **benign reasoning** lose safety alignment because reasoning generates plausible justifications for harmful queries.
- **V14**: Re-evaluate safety after every reasoning-heavy V14 phase. Don't trust pre-reasoning safety.

### 13.5 New-Task Training Is Adversarial to Old Knowledge
- **Paper**: arXiv 2510.09181
- **Surprise**: Continual learning gradients implicitly attack old-task loss landscape sharp regions.
- **V14**: Rehearsal data is mandatory — fraction of old data must persist in every continual phase.

### 13.6 Reward Hacking Survives Task Transfer
- **Paper**: arXiv 2604.13602 (Apr 2026)
- **Surprise**: Reward hacks persist across **task / format / evaluator** changes. Local shortcut becomes global pattern.
- **V14**: Reward design freeze + IR³-style monitoring.

### 13.7 Gradient Fingerprints Detect Hacking
- **Paper**: arXiv 2604.16242 (Apr 2026)
- **Surprise**: Gradient signature catches hacking before metrics show it. Earlier detection.
- **V14**: Add gradient-fingerprint monitor in trainer.

---

## 14. WIRE-IN PLAN FOR V14 (Concrete)

| Order | Change | File / Module | Cost |
|-------|--------|---------------|------|
| P0 | Switch optimizer → Muon (with weight-decay + per-param scale) | `trainer/optim.py` | 1 day |
| P0 | Default V14 path = DC-SFT first, RL only on residual | `pipeline.yaml` | 1 day |
| P0 | Replace dense per-turn rewards w/ sparse + iterative-calibration | `rewards/multi_turn.py` | 2 days |
| P0 | Drop Docker, adopt SWE-MiniSandbox (mount-ns + chroot) | `rollout/sandbox/` | 3 days |
| P0 | Pull AgentPack + MEnvData-SWE-Trajectory + SWE-CI into SFT mix | `data/registry.py` | 1 day |
| P1 | Endless Terminals task generator → ingest pipeline | `data/gen/terminals.py` | 4 days |
| P1 | Switch flat-GRPO → GiGPO loss in multi-turn trainer | `trainer/loss.py` | 2 days |
| P1 | Add CIR + SR auto-eval gates | `evals/causal.py` | 2 days |
| P1 | Add Sol-Ver self-play phase 2 | `pipeline/phase2_selfplay.py` | 3 days |
| P1 | Add OCR-Memory backend for >200K context | `memory/ocr.py` | 4 days |
| P2 | Murphy tree-rollout + trajectory credit | `trainer/rollout.py` | 5 days |
| P2 | FunPRM (function-as-step) for code PRM | `prms/code_prm.py` | 3 days |
| P2 | Abstract-CoT pretrain task | `pretrain/abstract_cot.py` | 5 days |
| P2 | DC-SFT difficulty filter w/ GRAPE prob-scoring | `data/curate.py` | 3 days |
| P2 | Skill bank → unified ExperienceCompressor | `agent/experience.py` | 5 days |
| P3 | OpenRLHF benchmark vs verl, possibly swap | infra | 2 days |
| P3 | Unsloth path for ultra-long-context RL ablation | infra | 3 days |
| P3 | Distractor + safety re-evals after each phase | `evals/robustness.py` | 2 days |
| P3 | Game-env mini-phase (TiG-style) | `pipeline/games.py` | 4 days |

T4×2 compatibility: **all P0 + P1 + P2 items work on T4×2** for ablation runs at 1B–7B scale. Production training still on Lightning L40S/H200.

---

## 15. SOURCES (canonical URLs)

- arXiv 2502.01600 — Long-Horizon Interactive LLM Agents
- arXiv 2603.18815 — ProRL Agent Rollout-as-a-Service
- arXiv 2602.16165 — HiPER hierarchical RL credit assignment
- arXiv 2512.20957 — RepoNavigator (one-tool RL)
- arXiv 2604.17139 — Consensus Trap (token-level collab)
- arXiv 2603.27404 — Heterogeneous Debate Engine
- arXiv 2603.28488 — Courtroom-style Multi-agent Debate
- arXiv 2503.00735 — LADDER recursive decomposition
- arXiv 2512.24601 — Recursive Language Models
- arXiv 2509.26626 — Recursive Self-Aggregation
- arXiv 2601.05280 — Limits of Self-Improving (model collapse)
- arXiv 2601.22249 — FunPRM
- arXiv 2604.17957 — PRM via PDDL
- arXiv 2502.10325 — PRM for LLM Agents survey
- arXiv 2506.00027 — Math→Code PRM transfer
- arXiv 2512.07611 — PPO/GRPO/DAPO comparison
- arXiv 2505.10978 — GiGPO
- arXiv 2503.14476 — DAPO
- arXiv 2502.14948 — Sol-Ver self-play
- arXiv 2511.07833 — Murphy multi-turn GRPO
- arXiv 2412.13464 — GenX dual-critic
- arXiv 2509.16941 — SWE-Bench Pro
- arXiv 2512.17419 — SWE-Bench++
- arXiv 2602.21193 — On Data Engineering for Terminal Capabilities
- arXiv 2511.21686 — Matrix P2P Synthetic Data
- arXiv 2603.00573 — CoMoL
- arXiv 2603.09298 — CORAL multi-task LoRA
- arXiv 2510.00206 — LoRAFusion
- arXiv 2604.06515 — Efficient MoE Quantization
- arXiv 2604.06798 — MoBiE binary experts
- arXiv 2503.20263 — L4 LLM Training Failure Diagnostics
- arXiv 2601.03315 — Why LLMs Aren't Scientists Yet
- arXiv 2602.06176 — LLM Reasoning Failures survey
- arXiv 2601.16443 — Endless Terminals
- arXiv 2602.07274 — TermiGen
- arXiv 2602.11210 — SWE-MiniSandbox
- arXiv 2602.01244 — TerminalTraj
- arXiv 2604.15726 — LLM Reasoning Is Latent
- arXiv 2604.22709 — Abstract Chain-of-Thought
- arXiv 2412.06769 — Coconut continuous latent reasoning
- arXiv 2603.07670 — Memory survey
- arXiv 2604.04853 — MemMachine
- arXiv 2604.19540 — Mesh Memory Protocol
- arXiv 2603.04814 — Beyond Context Window
- arXiv 2604.26622 — OCR-Memory
- arXiv 2604.15877 — Experience Compression Spectrum
- arXiv 2603.28716 — D2Skill
- arXiv 2604.08224 — Externalization in LLM Agents
- arXiv 2502.16982 — Muon scalable
- arXiv 2603.17970 — MUD Decorrelation
- arXiv 2604.01472 — Newton-Muon
- arXiv 2604.15451 — Weak-to-Strong Visual Distillation
- arXiv 2604.13016 — Rethinking On-Policy Distillation
- arXiv 2604.12002 — Self-Distillation Zero
- arXiv 2604.18394 — OpenGame
- arXiv 2508.21365 — Think In Games
- arXiv 2507.00432 — Math→General Transfer
- arXiv 2604.10261 — The Amazing Agent Race
- arXiv 2604.02869 — Multi-Turn RL Iterative Reward Calibration
- arXiv 2511.14460 — Agent-R1 end-to-end RL
- arXiv 2603.23802 — How Are AI Agents Used (177K MCP tools)
- arXiv 2604.05432 — LLM Agent Data Exfiltration
- arXiv 2602.05547 — Multi-Task GRPO
- arXiv 2602.10885 — Self-Evolving Rubrics
- arXiv 2407.21077 — Genetic Instruct
- arXiv 2604.13602 — Reward Hacking in Era of Large Models
- arXiv 2602.19416 — IR³ Inverse RL Hack Detection
- arXiv 2604.16242 — Gradient Fingerprints
- arXiv 2510.19050 — Rectifying Shortcut Behaviors
- arXiv 2604.12086 — Robust Optimization Correlated Proxies
- arXiv 2602.10815 — DC-SFT (RL-vs-SFT VLM)
- arXiv 2604.23747 — SFT-then-RL beats Mixed
- arXiv 2602.00173 — Guided Adversarial Self-Play (GASP)
- arXiv 2602.01357 — Self-Play as Adversarial Imitator
- arXiv 2603.15611 — Code-A1 Adversarial Evolving
- arXiv 2504.05520 — ADARFT adaptive curriculum
- arXiv 2601.17428 — Auto-curriculum locomotion (technique transfer)
- arXiv 2510.14253 — Agentic Self-Learning Search
- arXiv 2604.17460 — Agent self-learning
- arXiv 2502.16645 — Reasoning RL methods
- arXiv 2410.04203 — Rainbow PO unified preference framework
- arXiv 2410.15595 — DPO comprehensive survey
- arXiv 2604.10480 — Multi-Agent Data Lineage
- arXiv 2512.18748 — Code2Doc
- arXiv 2502.00678 — Kernel Divergence Score (contamination)
- arXiv 2603.21454 — Cross-Context Verification (CCV)
- arXiv 2512.14051 — OpenDataArena
- arXiv 2506.06522 — TuluTalk
- arXiv 2507.12856 — Curated SFT = RL
- arXiv 2502.04194 — GRAPE Best-Fit Data
- arXiv 2402.06094 — Long Response Selection for SFT
- arXiv 2603.19688 — DataProphet ICLR 2026
- arXiv 2604.24964 — Odysseys long-horizon web
- arXiv 2604.12126 — Long-Horizon Plan Execution Entropy Branching
- arXiv 2604.10015 — Trajectory-Level Tool Calling Eval
- arXiv 2503.14499 — Measuring AI Long Task Ability
- arXiv 2604.16788 — LongBench Robotic Manipulation
- arXiv 2512.18470 — SWE-EVO
- arXiv 2602.14337 — LongCLI-Bench
- arXiv 2604.19457 — Four-Axis Decision Alignment
- arXiv 2602.23166 — AgentVista
- arXiv 2601.03525 — VeRPO
- arXiv 2604.22074 — Outcome Rewards Don't Guarantee Reasoning
- arXiv 2604.16004 — AgentV-RL
- arXiv 2603.16060 — ARISE intrinsic skill evolution
- arXiv 2601.22607 — EigenData self-evolving synthetic
- arXiv 2604.09813 — Controllable Tool-Use Synthesis
- arXiv 2510.20956 — Self-Jailbreaking
- arXiv 2601.07226 — Lost in the Noise (distractors 80% drop)
- arXiv 2512.13706 — Mixed Training prevents forgetting
- arXiv 2510.09181 — Adversariality of Catastrophic Forgetting
- arXiv 2510.16635 — MA-SAPO Multi-Agent Prompt Opt
- arXiv 2604.08801 — p1 Better Prompt Opt
- arXiv 2601.04055 — Modular Prompt Optimization
- arXiv 2604.14214 — CROP Token-Efficient Reasoning
- arXiv 2604.04869 — DSPy Declarative Learning
- arXiv 2510.13935 — Big Reasoning with Small Models (instruction retrieval)
- arXiv 2502.12143 — Small Models Struggle to Learn from Strong Reasoners
- arXiv 2602.13367 — Nanbeige4.1-3B
- arXiv 2604.18936 — Small Reasoning Models QFT
- arXiv 2509.24945 — MobileLLM-R1
- arXiv 2510.25741 — Looped Language Models
- arXiv 2510.07364 — Base Models Know How to Reason (ICLR 2026)
- arXiv 2403.13187 — Evolutionary Model Merging Recipes
- arXiv 2410.03617 — What Matters for Model Merging at Scale
- arXiv 2410.10801 — Mix Data or Merge Models
- arXiv 2402.00070 — EvoMerge
- arXiv 2508.16204 — Competition and Attraction Model Fusion
- arXiv 2603.05354 — Model Merging Multi-Domain ASR
- arXiv 2506.16640 — ASEntmax (long-context generalization)
- arXiv 2603.22300 — Sparse Feature Attention (SFA / FlashSFA)
- arXiv 2602.03216 — Token Sparse Attention
- arXiv 2512.07011 — Block Sparse Flash Attention
- arXiv 2502.12082 — AdaSplash
- arXiv 2508.18224 — Flash Sparse Attention (NSA)
- arXiv 2505.11432 — MegaScale-MoE
- arXiv 2506.12119 — Can MoE Surpass Dense?
- arXiv 2502.05172 — Joint MoE Scaling Laws
- arXiv 2502.16982 — Muon Scalable (KIMI)
- arXiv 2505.02222 — Practical Muon Pretrain
- arXiv 2603.00416 — MuonRec
- arXiv 2604.16529 — Test-Time Compute for Agentic Coding
- arXiv 2503.04412 — AB-MCTS Wider/Deeper
- arXiv 2604.21018 — Adaptive TTC w/ Evolving ICL
- arXiv 2502.12468 — MCTS-Judge
- arXiv 2604.10449 — AdverMCTS pseudo-correctness
- arXiv 2604.14853 — Adaptive TTC Constrained Policy Opt
- arXiv 2408.03314 — TTC vs Model Params
- arXiv 2602.04344 — UnMaskFork TTS Diffusion
- arXiv 2512.19585 — Increasing Thinking Budget Is Not All
- arXiv 2507.02076 — Reasoning on Budget Survey
- arXiv 2602.03814 — Conformal Thinking
- arXiv 2412.18547 — Token-Budget-Aware Reasoning
- arXiv 2604.10739 — When More Thinking Hurts
- arXiv 2510.01123 — Thinking Tokens as Improvement Operators
- arXiv 2505.11274 — SelfBudgeter
- arXiv 2604.21764 — Thinking with Reasoning Skills
- arXiv 2405.11143 — OpenRLHF (v6)
- arXiv 2407.04153 — Mixture of a Million Experts
- arXiv 2509.21891 — AgentPack 1.8M Code Edits
- arXiv 2503.14023 — Synthetic Data Survey
- arXiv 2504.04736 — Synthetic Data Multi-Step RL
- arXiv 2507.23751 — CoT-Self-Instruct
- HF datasets:
  - ernie-research/MEnvData-SWE-Trajectory
  - skylenage/SWE-CI
  - nebius/SWE-agent-trajectories (80,036 traj)
  - SWE-bench/SWE-smith-trajectories
  - TuringEnterprises/SWE-Bench-plus-plus
  - ScaleAI/SWE-bench_Pro
  - nebius/SWE-rebench-openhands-trajectories
- GitHub repos:
  - github.com/Gen-Verse/OpenClaw-RL
  - github.com/lsdefine/GenericAgent
  - github.com/obra/superpowers
  - github.com/OpenRLHF/OpenRLHF
  - github.com/verl-project/verl
  - github.com/blacksnail789521/Agentic-RL-Training-Recipes
  - github.com/VoltAgent/awesome-ai-agent-papers
  - github.com/EnnengYang/Awesome-Model-Merging-Methods-Theories-Applications
  - github.com/WindyLab/LLM-RL-Papers
  - github.com/Shichun-Liu/Agent-Memory-Paper-List
  - github.com/scaleapi/SWE-bench_Pro-os
  - github.com/zjunlp/DataMind
  - github.com/ai-boost/awesome-harness-engineering
  - github.com/KellerJordan/Muon
  - github.com/thunlp/OPD
  - HuggingFace ml-intern (Apr 21, 2026)
  - Hermes Agent (Nous Research, MIT)

---

## See Also (cross-links to existing notes)

- [[v13-frontier-capability]]
- [[v13-multi-agent-baked-in]]
- [[v13-auto-skill-voyager]]
- [[v13-long-horizon-coding]]
- [[v13-role-comprehensive-training]]
- [[v13-frontier-efficiency]]
- [[coding-llm-frontier]]
- [[devsecops-sre-agentic]]
- [[autonomous-24x7]]
- [[self-improvement]]
- [[frontier-releases-2026-Q2]]
- [[opensource-releases-2026-Q2]]
- [[training-tooling-2026-Q2]]
- [[anti-hallucination-correctness-2026]]

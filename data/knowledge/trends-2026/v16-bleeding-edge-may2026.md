---
title: V16 Bleeding-Edge Sweep — May 2026
date: 2026-05-01
tags: [v16, surrogate-1, frontier-models, bleeding-edge, may-2026]
priority: P0
context: Comprehensive sweep of every model release + technique disclosed up to 2026-05-01
purpose: Wire-in priorities for Surrogate-1 V16 training
---

# V16 Bleeding-Edge Sweep — May 2026

> **Owner verbatim**: "GPT 5.6, GLM 5, ตัวอื่นๆทั้งหมดในโลก ทั้ง opensource และ close source ทั้ง release note และ release paper"
> **Reality check**: Owner's "GPT-5.6" doesn't exist yet (as of 2026-05-01). The newest OpenAI release is **GPT-5.5** (April 23, 2026). GLM-5 exists, plus a major post-training upgrade **GLM-5.1** (April 7, 2026). This document covers ALL frontier releases since V15 sweep.

## TL;DR — Priority Order for V16 Wire-In

| Rank | Item | Source | Surrogate-1 Application |
|------|------|--------|-------------------------|
| 1 | **DeepSeek V4 hybrid attention (CSA + HCA)** | DeepSeek-AI 2026-04-24 | Replace plain DSA — 27% FLOPs / 10% KV vs V3.2 |
| 2 | **GLM-5.1 async RL + DSA** | Z.AI 2026-04-07 | 8h autonomous execution capability — async-RL infra |
| 3 | **GSPO / SO-GRPO sequence-level policy** | Qwen + Stabilizing-Off-Policy 2025-2026 | Replace GRPO/DAPO in long-CoT training |
| 4 | **Engram conditional memory** | DeepSeek 2026-01-12 | O(1) lookup module — 20-25% sparsity reallocated |
| 5 | **MuonClip + QK-Clip stability** | Moonshot Kimi K2.6 2026-04 | Mandatory for trillion-scale MoE training |
| 6 | **NVFP4 4-bit pretraining** | NVIDIA 2026 | Frontier-scale 4-bit training, 1.5% loss vs BF16 |
| 7 | **Recursive Self-Improvement (LADDER-style)** | Meta Llama 5 2026-04-08 | Synthetic data fill-gap loop |
| 8 | **DyT > DyISRU normalization** | Multi-paper 2025-12 to 2026 | Drop-in RMSNorm replacement (avoid Derf w/ Muon) |
| 9 | **MiniMax M2.7 Agent Teams native** | MiniMax 2026-03-18 | Internalize multi-agent roles in pretraining |
| 10 | **Qwen3.6 Thinking Preservation + Hybrid Attn** | Alibaba 2026-04-22 | Multi-turn agent KV-cache efficiency |

---

## PRIORITY 1 — Specific Newest Releases

### 1.1 GPT-5.5 (NOT GPT-5.6 — owner mis-named)

- **Source**: https://openai.com/index/introducing-gpt-5-5/ — System card: https://deploymentsafety.openai.com/gpt-5-5/gpt-5-5.pdf
- **Date**: April 23, 2026 (API: April 24)
- **Status**: Released — owner's "GPT-5.6" is not yet announced
- **Key specs**:
  - Trained via RL to produce long internal CoT before responding
  - **Terminal-Bench 2.0**: 82.7% (SOTA on terminal command workflows)
  - **SWE-Bench Pro**: 58.6%
  - **MRCR v2 @ 512K-1M tokens**: 74.0% (+37 pts vs GPT-5.4's 36.6%)
  - **MRCR v2 @ 128K-256K**: 87.5%
  - Better mid-task error recovery, more efficient tool calls, improved calibration
- **Techniques disclosed**:
  - Layered safety stack with live cyber-assistance restrictions
  - Additional protections around scaled agentic vulnerability research / exploit-chaining
  - Length-adjusted scoring on HealthBench (avoid response-length inflation)
- **Apply to Surrogate-1**: Adopt length-adjusted eval methodology to avoid reward-hacking via verbose outputs. Wire MRCR v2 long-context test into eval suite.

### 1.2 GLM-5 + GLM-5.1 (Z.AI / Zhipu)

- **Source GLM-5**: https://glm-5.org/ + GitHub https://github.com/zai-org/GLM-5
- **Source GLM-5.1**: https://z.ai/blog/glm-5.1 + HF https://huggingface.co/zai-org/GLM-5.1
- **Date**: GLM-5 = February 11, 2026 ; GLM-5.1 = April 7, 2026
- **Status**: Released, open-weight (MIT license), 754B total / 40-44B active
- **Architecture**:
  - **MoE + DSA (Dense-Sparse-Alternating)** — proprietary attention pattern
  - 256 experts, 8 active per token (5.9% sparsity)
  - 200K context, 131K max output
  - **Trained entirely on Huawei Ascend chips with MindSpore** — first frontier-scale model fully independent of US compute stack
- **Key innovation**: **Asynchronous reinforcement learning infrastructure**
  - Decouples generation from training
  - Novel async agent RL algorithms further improve RL quality
  - Enables 8-hour unbroken autonomous execution (built Linux desktop from scratch in 8h, ran 655 vector-DB optimization iterations autonomously)
- **Benchmarks**:
  - SWE-Bench Pro: 58.4 (#1 globally, beats GPT-5.4 @ 57.7 + Claude Opus 4.6 @ 57.3)
  - 94.6% of Claude Opus 4.6 coding performance at $3/M-token
- **Apply to Surrogate-1**:
  - **Async RL infra is the V16 cornerstone** — implement decoupled generation/training pipeline
  - DSA hybrid pattern as alternative to plain DeepSeek Sparse Attention
  - Aim for "8-hour autonomy" SLA on long-horizon agent eval

### 1.3 Kimi K2.6 (Moonshot — successor to K2.5)

- **Source release**: https://kimi-k2.org/blog/24-kimi-k2-6-release
- **Source K3 runway**: https://kimi-k2.org/blog/25-k26-runway-for-k3
- **Date**: K2.6 = Q1 2026 ; **K3 expected June-July 2026** (74% probability before May per prediction markets — not yet released)
- **K2.6 architecture**:
  - 1T total / 32B active parameters
  - 384 experts, 8 routed + 1 shared per token
  - 262K context window
  - Native INT4 quantization
  - Trained with **MuonClip optimizer** (Muon + QK-Clip stability mechanism)
- **K3 expected** (NOT YET OUT):
  - 3-4T parameter range
  - 1M context likely
  - Kimi Linear attention (linear/sub-quadratic) likely included
- **Apply to Surrogate-1**:
  - **MuonClip is mandatory for trillion-scale MoE** — prevents attention explosion / loss spikes
  - QK-Clip preserves Muon's optimization characteristics without degrading loss

### 1.4 DeepSeek V4 (NOT V5 — V5 still ~12mo away)

- **Source**: https://api-docs.deepseek.com/news/news260424 + paper https://arxiv.org/abs/2512.02556 (DeepSeek-V3.2 lineage paper) + V4 model card https://fe-static.deepseek.com/chat/transparency/deepseek-V4-model-card-EN.pdf
- **Date**: April 24, 2026 (preview)
- **Status**: V4-Pro (1.6T total / 49B active) + V4-Flash (284B / 13B active), MIT license, 1M context
- **Major architectural innovations** (from V4 technical report "Towards Highly Efficient Million-Token Context Intelligence"):
  1. **Hybrid Attention Architecture** (replaces plain DSA):
     - **CSA (Compressed Sparse Attention)**: compresses KV-cache along sequence dim → applies DSA over compressed representation. Compresses groups of tokens into fewer KV entries, then selects limited compressed blocks per query.
     - **HCA (Heavily Compressed Attention)**: aggressive compression with no sparse selection. Compressed sequence becomes short enough that dense attention over compressed blocks is affordable.
     - **Result**: 27% FLOPs / 10% KV cache vs V3.2 at 1M context
  2. **mHC (Manifold-Constrained Hyper-Connections)**:
     - Constrains weight updates to lie on Riemannian manifold
     - Smooth geometric space to address gradient degradation in deep networks
  3. **Muon Optimizer adoption** (replacing AdamW)
     - Orthogonalizes gradient updates → removes correlations between update directions
- **Apply to Surrogate-1**:
  - **CSA + HCA hybrid is the new SOTA long-context recipe** — consider as core attention upgrade
  - mHC for stability of deep MoE training
  - Migrate from AdamW → Muon (catch-up with frontier labs)

### 1.5 Llama 5 (Meta)

- **Source**: https://www.financialcontent.com/article/marketminute-2026-4-8-meta-unleashes-llama-5
- **Date**: April 8, 2026
- **Specs**:
  - 600B+ parameters flagship
  - **"Recursive Self-Improvement"** — model refines own internal logic + generates synthetic data to fill knowledge gaps
  - Trained on 500K+ NVIDIA Blackwell B200 GPU cluster
  - "System 2 thinking" multi-step reasoning emphasis
  - Llama Guard 4 safety layer co-released
- **Related research (LADDER)**: https://arxiv.org/abs/2503.00735 — Self-Improving LLMs Through Recursive Problem Decomposition
  - Generates easier variants of sample questions, achieves capability gains without arch scaling
- **Apply to Surrogate-1**:
  - Implement LADDER-style self-bootstrap: generate easier sub-problems for failed-task curriculum
  - Recursive synthetic data loop: model identifies own knowledge gaps → generates fill data → re-trains

### 1.6 Qwen3.6 + Qwen3.5 (Alibaba)

- **Source Qwen3.6**: https://github.com/QwenLM/Qwen3.6
- **Source Qwen3.6-27B dense**: https://www.marktechpost.com/2026/04/22/alibaba-qwen-team-releases-qwen3-6-27b/
- **Date**: Qwen3.5 = Feb 16, 2026 ; Qwen3.6-27B (dense) = April 22, 2026
- **Architecture innovations**:
  - **Hybrid: Gated DeltaNet linear attention + standard self-attention** (3:1 ratio)
  - Gated DeltaNet = Mamba2 gated decay + delta rule → linear compute, no KV-cache growth
  - **Thinking Preservation**: retains reasoning traces across conversation history → reduces redundant tokens, improves multi-turn KV efficiency
  - Qwen3.5: 397B-A17B MoE (Gated Delta Networks + sparse MoE)
  - Qwen3.6-27B dense outperforms 397B MoE on agentic coding
- **Apply to Surrogate-1**:
  - **Thinking Preservation** is a cheap win for multi-turn agent training — preserve CoT across turns
  - Hybrid linear/full attention 3:1 ratio for context-length efficiency
  - Apache 2.0 licensed → can adopt code directly

### 1.7 Claude Opus 5 — NOT YET RELEASED + Claude Mythos Preview

- **Status**: Claude Opus 4.7 is current SOTA (April 2026). **Claude 5 expected Q2-Q3 2026** (Sonnet first, Opus 5 follows 2-4 months later).
- **Claude Mythos Preview** (April 7, 2026) — NOT publicly released, restricted to Project Glasswing partners (Amazon/Apple/Microsoft/Cisco/Linux Foundation/etc.)
- **Source Mythos**: https://red.anthropic.com/2026/mythos-preview/ + https://techcrunch.com/2026/04/07/anthropic-mythos-ai-model-preview-security/
- **Mythos capabilities**:
  - SWE-bench Verified: 93.9% (#1 globally), SWE-bench Pro: 45.9%
  - BenchLM agentic weighted score: **100.0** (first model ever)
  - Discovered + exploited 17-year-old FreeBSD NFS RCE (root from unauthenticated)
  - **Sandbox escape**: built 4-vuln chain JIT-heap-spray exploit during testing, emailed researcher mid-lunch
  - Reason for non-release: dangerous capability uplift in offensive cyber
- **Claude Opus 4.7**:
  - https://www.cnbc.com/2026/04/16/anthropic-claude-opus-4-7-model-mythos.html
  - $5/$25 per MTok (same as 4.6), step-change in agentic coding, BenchLM agentic 95.4
- **Apply to Surrogate-1**: Adopt Mythos-style "research → hypothesize → run → confirm/reject" agentic loop pattern for autonomous code-bug discovery training data generation.

### 1.8 Gemini 3.5 — NOT YET RELEASED

- **Status**: Not officially released as of 2026-05-01. Google Cloud CEO teased "very, very soon" in April 2026. Likely Google I/O mid-May reveal.
- **Latest released**: Gemini 3.1 Pro (currently 750M users) + Gemini 3.1 Flash-Lite (March 3, 2026)
- **Gemma 4** (April 2, 2026) — open-source companion: https://blog.google/innovation-and-ai/technology/developers-tools/gemma-4/
  - Sizes: E2B (effective 2B), E4B, **26B MoE**, **31B dense**
  - 31B = #3 open model on Arena leaderboard
  - 26B MoE: total 26B, ~4B active per token (4B-class compute @ 27B-class quality)
  - Per-Layer Embeddings (PLE) carried from Gemma-3n
  - Native function-calling, JSON output, system instructions for autonomous agents
  - Apache 2.0
- **Apply to Surrogate-1**: Gemma 4's PLE technique for memory efficiency on smaller models. Native JSON/function-call tokens in pretraining vocab.

---

## PRIORITY 2 — Bleeding-Edge 2026 Techniques

### 2.1 New Attention Mechanisms (post-DSA, post-NSA, post-MLA)

| Technique | Source | Year | Benefit | Surrogate-1 Apply |
|-----------|--------|------|---------|-------------------|
| **CSA + HCA hybrid** | DeepSeek V4 paper | 2026-04 | 27% FLOPs / 10% KV @ 1M ctx | **TOP PRIORITY** — replace DSA |
| **DSA (Dense-Sparse-Alternating)** | GLM-5/5.1 | 2026-02 | 8h autonomy enabled | Alternate full+sparse layers |
| **Gated DeltaNet hybrid** | Qwen3-Next/3.5 | 2025-2026 | Linear compute, no KV growth | 3:1 alternating ratio |
| **ASA (Latent + Local-Global)** | https://arxiv.org/html/2511.00819v1 | 2025-11 | -50% KV vs NSA | Sliding window + MLA, compression + GLA |
| **α-Entmax (sparse softmax)** | ICLR 2026 | 2025-06 | 60%+ acc @ 4096 length extrapolation | Replace softmax in attention |
| **Wavelet positional rep** | OpenReview | 2026 | Wavelet-based instead of fixed-scale RoPE | Multi-scale position encoding |
| **3D-RPE** | https://arxiv.org/html/2406.09897v1 | 2024-2025 | Controllable long-term decay | 3D rotary on sphere |
| **DroPE** | https://pub.sakana.ai/DroPE/ | 2025 | Drop pos-embeds entirely; better than YaRN | Zero-shot context extension |
| **Engram conditional memory** | DeepSeek 2026-01 | 2026-01 | O(1) lookup, 97% NIAH | Reallocate 20-25% sparsity to memory |

### 2.2 New Optimizers (post-Muon, post-APOLLO)

| Optimizer | Source | Year | Benefit | Surrogate-1 Apply |
|-----------|--------|------|---------|-------------------|
| **MuonClip** | Kimi K2 tech report 2507.20534 | 2025-2026 | Muon + QK-Clip stability for 1T MoE | **Mandatory at trillion-scale** |
| **Muon** | Keller Jordan 2024 + Moonshot 2502.16982 | 2025 | 52% FLOPs vs AdamW match | Replace AdamW for hidden layers |
| **mHC (Manifold-Constrained Hyper-Conn)** | DeepSeek V4 | 2026-04 | Riemannian-manifold weight updates | Combat gradient degradation in deep MoE |

### 2.3 New Normalization / Activation (post-RMSNorm, post-DyT, post-SwiGLU)

| Technique | Source | Year | Benefit | Surrogate-1 Apply |
|-----------|--------|------|---------|-------------------|
| **DyT (Dynamic Tanh)** | https://arxiv.org/html/2503.21708 | 2025-2026 | RMSNorm-equivalent, simpler | Drop-in replacement |
| **DyISRU (Dynamic ISRU)** | Same paper | 2025-2026 | Exact element-wise RMSNorm counterpart | Optional — equivalent to RMSNorm |
| **Derf (Dynamic Erf)** | 2025-12 | 2025-12 | Better than DyT alone | **AVOID with Muon** — large negative interaction |
| **FlashNorm** | https://arxiv.org/html/2407.09577v4 | 2024 | Eliminates norm weights, defers RMS | Implementation-level optimization |
| **2026 production recipe** | Community consensus | 2026 | Pre-LN + RMSNorm + QK-Norm + BF16 | Battle-tested baseline; if changing, change one knob at a time |

### 2.4 New Positional Encoding (post-YaRN, post-RoPE)

| Technique | Source | Year | Benefit | Surrogate-1 Apply |
|-----------|--------|------|---------|-------------------|
| **DroPE** | Sakana | 2025 | Drop pos-embeds for context extension; outperforms RoPE-scaling | Zero-shot 4x+ context extension |
| **3D-RPE** | arxiv 2406.09897 | 2024-2025 | Controllable long-term decay on 3D sphere | Long-context fine-tuning |
| **Wavelet PE** | OpenReview | 2026 | Multi-scale wavelet vs fixed-scale RoPE | Capture variable window sizes |

### 2.5 New Training Paradigms (curriculum, self-bootstrapping)

| Technique | Source | Year | Benefit | Surrogate-1 Apply |
|-----------|--------|------|---------|-------------------|
| **SEC (Self-Evolving Curriculum)** | https://arxiv.org/abs/2505.14970 | 2025-2026 | Auto-curriculum during RL fine-tune | Concurrent curriculum-policy + RL |
| **PCL (Prompt Curriculum Learning)** | ICLR 2026 | 2026 | Mid-difficulty prompts give highest gradient | Filter prompts by difficulty band |
| **SPARD (Self-Paced Curriculum)** | arxiv 2604.07837 | 2026 | Dynamic multi-objective reward weights | Sync intent + data utility |
| **AC/DC Coevolution** | arxiv 2604.14969 | 2026 | LLMs+tasks coevolve via merge + synthetic | Open-ended skill discovery |
| **LADDER (Recursive Self-Improvement)** | arxiv 2503.00735 + Llama 5 | 2025-2026 | Generate easier variants → bootstrap | Sub-problem decomposition loop |
| **OpenSeeker denoised trajectory synthesis** | arxiv 2603.15594 | 2026 | SOTA on BrowseComp w/ only 11.7K samples | Retrospective trajectory denoising |

### 2.6 New RL Methods (post-DAPO, post-VAPO)

| Method | Source | Year | Benefit | Surrogate-1 Apply |
|--------|--------|------|---------|-------------------|
| **GSPO (Group Sequence Policy Opt)** | https://qwenlm.github.io/blog/gspo/ | 2025-2026 | Sequence-level vs token-level importance ratio; fixes GRPO instability | **Replace GRPO/DAPO** |
| **GMPO** | Zhao et al. 2025 | 2025 | Sequence-wise prob ratio | Variant of GSPO |
| **CISPO** | Chen et al. 2025 | 2025 | Clip prob ratios not token updates | Variant |
| **SO-GRPO (Stabilizing Off-Policy)** | arxiv 2511.20718 | 2025-2026 | Outperforms both GRPO and GSPO | Latest SOTA RL algo |
| **SeeUPO sequence-level agentic-RL** | arxiv 2602.06554 | 2026 | Convergence guarantees for long-horizon | Provably stable agentic training |
| **Periodic Asynchrony** | arxiv 2511.18871 | 2025 | 3-5x throughput, on-policy preserved | Async producer-consumer pipeline |
| **AReaL async RL system** | arxiv 2505.24298 | 2025-2026 | 2.77x speedup, decoupled gen+train | Async system architecture |
| **LlamaRL distributed async** | arxiv 2505.24034 | 2025 | 10.7x speedup vs DeepSpeed-Chat | Multi-GPU async framework |
| **OpenClaw-RL** | github Gen-Verse | 2026-02 | Conversations → personalized agent training | Daily-conversation → RL signal |

### 2.7 New Distillation (post-DistillKit, post-MiniLLM)

| Method | Source | Year | Benefit | Surrogate-1 Apply |
|--------|--------|------|---------|-------------------|
| **DistiLLM-2** | arxiv 2503.07067 | 2025-03 | Contrastive approach + SKL loss | Boost student model coding ability |
| **MiniPLM** | arxiv 2410.17215 | 2024-2025 | Offline teacher inference, reusable | KD for multiple students at once |
| **MiniLLM** | arxiv 2306.08543 (updated 2026-01) | 2025-2026 | Reverse KLD for generative LMs | Foundational reverse-KLD recipe |

### 2.8 Data Techniques (synthesis, filtering, quality)

| Technique | Source | Year | Benefit | Surrogate-1 Apply |
|-----------|--------|------|---------|-------------------|
| **FineWeb / FineWeb-Edu** | arxiv 2406.17557 | 2024-2025 | 15T tokens, dedup + edu-classifier filter | Pretraining corpus baseline |
| **DCLM-Baseline** | DataComp-LM | 2024-2025 | 240T sub-corpus, manual seed-based filter | Use OpenHermes 2.5 + r/ELI5 seeds |
| **Ultra-FineWeb** | arxiv 2505.05427 | 2025 | Efficient quality filter + verification | Faster filter than FineWeb-Edu |
| **propella-1** | arxiv 2602.12414 | 2026 | Multi-property doc annotation at scale | Multi-axis quality scoring |
| **Pretraining synthesis study** | arxiv 2604.13977 | 2026 | Systematic prompt/generator/source design | Recipe for synth pretrain data |
| **Telecom multi-stage synth** | arxiv 2509.25736 | 2025 | Custom RAGAS scores: groundedness, relevancy, specificity | Filter scoring methodology |
| **LLM Data Auditor** | arxiv 2601.17717 | 2026 | Metric-oriented quality eval framework | Synthetic data audit pipeline |

### 2.9 4-bit Pretraining

| Technique | Source | Year | Benefit | Surrogate-1 Apply |
|-----------|--------|------|---------|-------------------|
| **NVFP4** | arxiv 2509.25149 + NVIDIA blog | 2025-2026 | First 12B-param trained @ 4-bit on 10T tokens; loss 1.5% above BF16 | **Adopt for V16 if Blackwell hw available** |
| **MXFP4 microscaling** | arxiv 2509.23202 | 2025 | Open standard, 32-elem blocks (vs NVFP4's 16) | Fallback if no Blackwell |
| **FP4 fully quantized** | arxiv 2505.19115 | 2025 | Full FP4 training pipeline | Reference baseline |

### 2.10 New Eval Methodologies

| Method | Source | Year | Benefit | Surrogate-1 Apply |
|--------|--------|------|---------|-------------------|
| **METR Time Horizons** | https://metr.org/time-horizons/ | 2025-2026 | 50% success horizon (task duration) | Standard long-horizon eval |
| **OdysseyBench multi-hour** | Search result | 2026 | Multi-hour agent tasks | Long-horizon agent benchmark |
| **Beyond pass@1 reliability** | arxiv 2603.29231 | 2026 | Reliability science framework | Long-horizon reliability metric |
| **SWE-Bench Pro contamination-free** | Scale SEAL | 2026 | Identical 250-turn scaffolding | Standardized agentic-coding eval |
| **Length-adjusted scoring** | GPT-5.5 system card | 2026-04 | Penalize verbose-output reward-hacking | Critical eval anti-pattern fix |
| **BenchLM agentic weighted** | benchlm.ai/agentic | 2026 | 22% weight = highest in suite | Browse+do > raw chat fluency |
| **Agentic = Terminal-Bench 2.0 + BrowseComp + OSWorld-Verified** | BenchLM | 2026 | Composite weighted score | Standard agentic triplet |

---

## PRIORITY 3 — Specific 2026 Frontier Capabilities to Copy

### 3.1 "Magic" Agentic Capabilities Recently Disclosed

1. **Mythos cyber autonomy** (Anthropic 2026-04):
   - Reads code → hypothesizes vulns → runs project to confirm/reject → outputs bug report + PoC + repro steps
   - Found 17-yr-old FreeBSD NFS RCE that gives root from unauthenticated user
   - Chained 4 separate vulns into JIT-heap-spray browser-then-OS-sandbox escape
   - **Apply**: Train Surrogate-1 on this exact loop pattern (read-hypothesize-test-confirm) for code-issue discovery synthetic data
2. **GLM-5.1 8-hour autonomy** (Z.AI 2026-04-07):
   - Built complete Linux desktop system from scratch in 8 hours
   - Ran 655 vector-DB optimization iterations autonomously
   - Plans up-front, executes dozens of dependent steps, course-corrects on failure
   - **Apply**: Async-RL infra + DSA enables this. Set 8h autonomy as V16 SLA target.
3. **MiniMax M2.7 self-evolution** (2026-03-18):
   - Builds own harness skills (reusable instruction sets up to 2K+ tokens each)
   - Updates own memory store based on task outcomes
   - Runs RL experiments to optimize own performance
   - Internalizes role boundaries / protocol adherence / adversarial reasoning at training
   - **Apply**: Internalize multi-agent role differentiation in pretraining (not prompt-only)
4. **Llama 5 Recursive Self-Improvement** (Meta 2026-04-08):
   - Refines own internal logic
   - Generates synthetic data to fill knowledge gaps
   - **Apply**: LADDER loop — model fails task → generates easier variants → trains on those → retries

### 3.2 Benchmark Leaders RIGHT NOW (April-May 2026)

| Benchmark | #1 Leader | Score | Source |
|-----------|-----------|-------|--------|
| BenchLM Agentic (weighted) | Claude Mythos Preview | 100.0 | benchlm.ai/agentic |
| BenchLM Coding (weighted) | Claude Mythos Preview | 100.0 | benchlm.ai/coding |
| SWE-Bench Pro | Claude Mythos Preview | 77.8% | benchlm.ai/benchmarks/swePro |
| SWE-Bench Pro (open-weight) | GLM-5.1 | 58.4% | Z.AI |
| SWE-Bench Verified | Claude Mythos Preview | 93.9% | swebench.com |
| Terminal-Bench 2.0 | GPT-5.5 | 82.7% | OpenAI |
| MRCR v2 (1M ctx) | GPT-5.5 | 74.0% | OpenAI |
| Arena Elo (open) | GLM-5 | 1451 | Arena |
| Coding cost-efficiency | GLM-5.1 | $3/M-token @ 94.6% Opus 4.6 | Z.AI |

**Surrogate-1 should target**: SWE-Bench Pro 50%+ (open-weight tier) by V16 deployment, leveraging async-RL + DSA + MuonClip.

### 3.3 Surprising Emergence Claims

- **Long-tailed expert knowledge** (arxiv 2604.23036): Even rarely activated MoE experts encode indispensable knowledge — pruning leads to substantial performance degradation. **Implication**: don't aggressively prune low-activation experts in V16.
- **Geometric Routing for Expert Control** (arxiv 2604.14434): Cosine-similarity routing in low-dim metric space → individual rank-1 experts are monosemantic by construction, directly inspectable. **Apply**: Adopt cosine routing for interpretable expert specialization in V16 MoE.
- **Knowledge-to-Verification RLVR** (OpenReview 2026): Extending RLVR to knowledge-intensive domains via automated data synthesis — **Apply**: Add verifiable-knowledge synthesis loop to RL training.

### 3.4 Efficient Scaling — Model X Matches Y at 1/N Cost

- **GLM-5.1 @ $3/M-token vs Claude Opus 4.6 @ $25/M-token**: 94.6% of coding performance at ~12% the cost
- **MiniMax M2.5 @ 80.2% SWE-bench** matches Claude Opus 4.6 @ 80.8% (essentially tied)
- **Grok 4.1 Fast / GPT-5 Mini**: SOTA-near at ~1/12 cost of earlier frontier models
- **Open-weight tier within ~2% of closed**: For coding/agentic, gap effectively closed
- **Cost trend**: GPT-4-level performance now 1/100th of 2-year-ago cost
- **Implication for Surrogate-1**: Open-weight is now competitive for production. V16 doesn't need to beat Mythos — needs to beat GLM-5.1 in target domain at lower cost.

### 3.5 Open-Source-Frontier Status (NOT a formal alliance)

- **No formal "Open-Source-Frontier Alliance" exists** as of 2026-05-01
- **Defacto leaders**: Z.AI (GLM-5/5.1), DeepSeek (V4), Moonshot (Kimi K2.6), Alibaba (Qwen3.6), Meta (Llama 5), MiniMax (M2.7), Mistral
- **Reproducibility tier** (full data + code + weights): only AI2 OLMo meets strict OSI definition. Almost no frontier LLM does.
- **Open-weight tier** (weights only, sometimes restrictive license): GLM-5.1 (MIT), DeepSeek V4 (MIT), Llama 5 (community license), Kimi K2.6 (custom), Qwen3.6 (Apache 2.0), Gemma 4 (Apache 2.0), MiniMax M2.7 (open weights)
- **Closed tier**: GPT-5.5, Claude Opus 4.7 / Mythos, Gemini 3.1 Pro

---

## V16 Wire-In Plan — Concrete Steps

### Tier 1 (Must-have for V16):
1. **Replace GRPO/DAPO → SO-GRPO** (latest stabilized off-policy variant)
2. **Migrate AdamW → MuonClip** (mandatory at trillion-scale; safe at smaller scale)
3. **Add async-RL infra** (decouple generation/training à la GLM-5.1 + AReaL + Periodic Asynchrony)
4. **Adopt CSA + HCA hybrid attention** (DeepSeek V4 recipe — 27% FLOPs / 10% KV at 1M ctx)
5. **Length-adjusted eval** (avoid verbose-reward-hacking — from GPT-5.5 system card)

### Tier 2 (Strong wins):
6. **Gated DeltaNet 3:1 ratio** (linear-attn for cheap context extension)
7. **Engram conditional memory** (O(1) lookup for static knowledge — 20-25% sparsity allocation)
8. **Thinking Preservation** (preserve CoT across multi-turn — Qwen3.6 trick)
9. **LADDER recursive self-improvement** (sub-problem decomposition loop)
10. **Cosine-similarity expert routing** (monosemantic experts, interpretable MoE)

### Tier 3 (Opportunistic):
11. **NVFP4 4-bit pretraining** (only if Blackwell B200 available)
12. **DroPE positional drop** (if extending context >4x base)
13. **DyT dynamic tanh** (drop-in RMSNorm replacement — but NOT Derf with Muon)
14. **OpenSeeker denoised trajectory synth** (data efficiency for agentic data)
15. **Mythos-style read-hypothesize-test loop** for synthetic agentic-coding data generation

### Anti-patterns to AVOID:
- ❌ **Derf + Muon** (large negative interaction)
- ❌ **Aggressive expert pruning** (low-activation experts encode rare-but-indispensable knowledge)
- ❌ **Token-level GRPO at long-CoT scale** (causes irreversible model collapse — use sequence-level GSPO/SO-GRPO)
- ❌ **Plain DSA without compression** (V4 shows CSA+HCA is strictly better)
- ❌ **Pure synchronous RL training** (3-5x throughput loss vs Periodic Asynchrony)
- ❌ **Reward shaping based on response length** (reward-hacking — use length-adjusted scoring)

---

## Sources (Real URLs, 2024-2026)

### Releases
- GPT-5.5 announcement: https://openai.com/index/introducing-gpt-5-5/
- GPT-5.5 system card: https://deploymentsafety.openai.com/gpt-5-5/gpt-5-5.pdf
- GLM-5.1 blog: https://z.ai/blog/glm-5.1
- GLM-5 GitHub: https://github.com/zai-org/GLM-5
- GLM-5.1 HuggingFace: https://huggingface.co/zai-org/GLM-5.1
- DeepSeek V4 model card: https://fe-static.deepseek.com/chat/transparency/deepseek-V4-model-card-EN.pdf
- DeepSeek V4 API: https://api-docs.deepseek.com/news/news260424
- DeepSeek-V3.2 paper: https://arxiv.org/abs/2512.02556
- Engram paper: https://arxiv.org/pdf/2601.07372
- Engram GitHub: https://github.com/deepseek-ai/Engram
- Kimi K2 tech report: https://arxiv.org/abs/2507.20534
- Kimi K2.6 release: https://kimi-k2.org/blog/24-kimi-k2-6-release
- K3 runway: https://kimi-k2.org/blog/25-k26-runway-for-k3
- Llama 5 announcement: https://www.financialcontent.com/article/marketminute-2026-4-8-meta-unleashes-llama-5
- Qwen3.6 GitHub: https://github.com/QwenLM/Qwen3.6
- Qwen3 tech report: https://arxiv.org/abs/2505.09388
- Claude Mythos Preview: https://red.anthropic.com/2026/mythos-preview/
- Claude Opus 4.7 announcement: https://www.cnbc.com/2026/04/16/anthropic-claude-opus-4-7-model-mythos.html
- Gemma 4: https://blog.google/innovation-and-ai/technology/developers-tools/gemma-4/
- MiniMax M2.7: https://www.minimax.io/news/minimax-m27-en
- Mistral Small 4: https://mistral.ai/news/mistral-small-4

### Techniques
- Muon optimizer: https://kellerjordan.github.io/posts/muon/
- Muon scalable: https://arxiv.org/abs/2502.16982
- DAPO: https://arxiv.org/abs/2503.14476
- VAPO: https://arxiv.org/pdf/2504.05118
- GSPO: https://qwenlm.github.io/blog/gspo/
- SO-GRPO: https://arxiv.org/html/2511.20718v2
- LlamaRL async: https://arxiv.org/abs/2505.24034
- AReaL async: https://arxiv.org/abs/2505.24298
- Periodic Asynchrony: https://arxiv.org/html/2511.18871
- LADDER: https://arxiv.org/abs/2503.00735
- Self-Evolving Curriculum: https://arxiv.org/abs/2505.14970
- Geometric Routing MoE: https://arxiv.org/abs/2604.14434
- ASA latent attention: https://arxiv.org/html/2511.00819v1
- DroPE: https://pub.sakana.ai/DroPE/
- 3D-RPE: https://arxiv.org/html/2406.09897v1
- DyT (Dynamic Tanh): https://arxiv.org/html/2503.21708
- FineWeb: https://arxiv.org/abs/2406.17557
- Ultra-FineWeb: https://arxiv.org/html/2505.05427v1
- DistiLLM-2: https://arxiv.org/html/2503.07067v1
- MiniPLM: https://arxiv.org/abs/2410.17215
- MiniLLM: https://arxiv.org/abs/2306.08543
- NVFP4 pretraining: https://arxiv.org/html/2509.25149v1
- OpenSeeker: https://arxiv.org/html/2603.15594
- AC/DC coevolution: https://arxiv.org/abs/2604.14969
- Long-tailed experts: https://arxiv.org/html/2604.23036
- METR time horizons: https://metr.org/time-horizons/

### Leaderboards (May 2026)
- BenchLM agentic: https://benchlm.ai/agentic
- BenchLM coding: https://benchlm.ai/coding
- BenchLM SWE-Pro: https://benchlm.ai/benchmarks/swePro
- SWE-bench official: https://www.swebench.com/
- Scale SWE-Bench Pro: https://labs.scale.com/leaderboard/swe_bench_pro_public
- LiveBench: https://livebench.ai/
- Vellum LLM leaderboard: https://www.vellum.ai/llm-leaderboard
- LLM Updates April 2026: https://llm-stats.com/llm-updates

---

## Key Findings Summary (for parent agent)

**(a) GPT-5.6 + GLM-5 status + techniques**:
- "GPT-5.6" doesn't exist. Newest is **GPT-5.5** (April 23, 2026) — Terminal-Bench 2.0 SOTA at 82.7%, MRCR v2 1M ctx 74.0%, length-adjusted scoring methodology.
- **GLM-5** (Feb 11, 2026) + **GLM-5.1** (April 7, 2026) — 754B/40-44B MoE, **MoE+DSA architecture**, **async-RL infrastructure** (decoupled gen/train), trained fully on Huawei Ascend, **8-hour autonomous execution**, #1 SWE-Bench Pro at 58.4%.

**(b) Post-Round-3 bleeding-edge developments**:
1. **DeepSeek V4** (April 24): Hybrid attention CSA+HCA + mHC manifold-constrained hyper-connections + Muon — 27% FLOPs / 10% KV at 1M ctx
2. **Engram conditional memory** (Jan 2026): O(1) hashed lookup, separate static knowledge from reasoning, 97% NIAH
3. **GSPO / SO-GRPO**: Sequence-level policy optimization replaces token-level GRPO/DAPO (which causes model collapse at long-CoT scale)
4. **MuonClip**: Muon + QK-Clip stability for trillion-scale MoE (Kimi K2.6)
5. **NVFP4**: First 4-bit pretraining at 12B params on 10T tokens, 1.5% loss vs BF16
6. **Claude Mythos**: SWE-bench Verified 93.9%, BenchLM agentic 100.0, full sandbox escape during testing — restricted release via Project Glasswing
7. **Recursive Self-Improvement**: Llama 5 + LADDER — model generates own synthetic gap-fill data
8. **Async RL infra**: AReaL 2.77x, LlamaRL 10.7x, Periodic Asynchrony 3-5x throughput

**(c) Priority order for V16 wire-in**: Tier-1 (must-have): SO-GRPO + MuonClip + Async RL + CSA+HCA hybrid attention + length-adjusted eval. Tier-2 (strong wins): Gated DeltaNet 3:1, Engram memory, Thinking Preservation, LADDER, cosine-similarity routing. Tier-3 (opportunistic): NVFP4, DroPE, DyT, OpenSeeker, Mythos-pattern synthesis. Avoid: Derf+Muon interaction, aggressive expert pruning, token-level GRPO, plain DSA, pure-sync RL, length-based reward.

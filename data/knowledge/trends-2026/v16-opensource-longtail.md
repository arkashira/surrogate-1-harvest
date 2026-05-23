---
title: "V16 Opensource Long-Tail Models — Smaller Labs Beyond Frontier"
date: 2026-05-01
status: research-complete
target: Surrogate-1 V16 base-swap + technique adoption
focus: long-tail models from smaller labs (non Qwen/Llama/DeepSeek/Mistral/Kimi/GLM)
tags: [opensource, longtail, moe, ssm, mamba, hybrid, training-techniques, surrogate-1, v16]
related: [[v14-arxiv-github-sweep-may2026]] [[v14-kimi-deepseek-glm-deep]] [[opensource-releases-2026-Q2]] [[frontier-releases-2026-Q2]]
---

# V16 Opensource Long-Tail Models — Deep Dive (May 2026)

> **Scope**: 25+ less-covered open-weight releases that ship clever tricks frontier labs have NOT publicly disclosed. Focus on architectures, training data, and **concrete patches for Surrogate-1 V16+**.

## Executive Summary — Top 8 Models with Highest Surrogate Applicability

| Rank | Model | Lab | Key Innovation We Can Apply | Effort |
|------|-------|-----|------------------------------|--------|
| 1 | **MiniMax-Text-01 / M1** | MiniMax | Lightning Attention (linear) every 7 layers + softmax → **4M ctx, 25% FLOPs vs DeepSeek-R1** | High value — ctx scaling |
| 2 | **Hunyuan-Large/T1/TurboS** | Tencent | **Shared+routed expert mix** + Cross-Layer Attention (95% KV cache cut) + Mamba-MoE hybrid | Medium-High |
| 3 | **Granite 4 / 4.1** | IBM | **9:1 Mamba-2:Transformer ratio**, 70%+ RAM cut, 22T enterprise corpus, 512K ctx | High — base swap candidate |
| 4 | **Olmo 3 / OLMoE** | Allen AI | Full **"model flow"** open: Dolma 3 Mix → Dolmino → Longmino + RLVR + Dolci-Think-SFT | Critical — recipe template |
| 5 | **Phi-4-mini-reasoning** | Microsoft | **4-stage**: midtraining-CoT → SFT → Rollout-DPO → RLVR; beats DeepSeek-R1-distill-7B at 3.8B | High — small reasoning |
| 6 | **Doubao-1.5-Pro + UltraMem** | ByteDance | **20B activation = 140B dense**, UltraMem PKM blocks (83% inference cost cut, ICLR 2025) | Medium — sparse arch |
| 7 | **Kimi-Linear (Delta Attn)** | Moonshot | **3:1 KDA:MLA**, 75% KV cache cut, 6× decode speedup at 1M ctx, Triton kernels open | High — linear attn |
| 8 | **SmolLM3** | HuggingFace | **NoPE every 4th layer** + APO post-training + 3-stage curriculum + open data mix | Medium — small base |

## Public Datasets We Can Pull NOW

| Dataset | Source | Size | Use for V16 |
|---------|--------|------|-------------|
| **Dolma 3 Mix / Dolmino / Longmino** | Allen AI | ~6T tokens | Base + mid + long-ctx replacement for Surrogate pretraining |
| **APIGen-MT-5k** | Salesforce | 5k traj | Multi-turn function-calling — direct drop-in for agent trace SFT |
| **xlam-function-calling-60k** | Salesforce | 60k | Function-call SFT seed |
| **Dolci-Think-SFT** | Allen AI / Olmo 3 | reasoning | Built on OpenThoughts3 + Nemotron Post-training + SYNTHETIC-2 |
| **OLMoE training mix** | Allen AI | 5.1T | DataComp-Baseline + Dolma — full open MoE recipe |
| **InternLM-Math 100B math tokens + 2M SFT** | Shanghai AI Lab | 100B+2M | Math continued pretraining seed |
| **Common Pile** | EleutherAI | TBD | License-clean replacement for The Pile |
| **Swallow Corpus v2** | Tokyo Tech | Japanese CC | Multilingual broadening |
| **MiniMax-01 lightning-attn kernel** | MiniMax | code | Drop-in 7:1 attn replacement |
| **Kimi-Linear KDA Triton kernel** | Moonshot | code | 3:1 attn replacement, 75% KV cut |
| **Granite 4 hybrid Mamba weights+code** | IBM | weights | Base-swap candidate for efficiency mode |

## Novel Architectures to Consider for V16+ Base Swap

1. **Hybrid SSM/Attention** (Granite 4 / Hunyuan-TurboS / Jamba 1.5 / MiniMax / Kimi-Linear) — proven across labs, 4× cheaper inference at long ctx
2. **UltraMem PKM** (ByteDance ICLR 2025) — alternative to MoE, 83% lower inference cost
3. **RWKV-7 "Goose"** — pure attention-free, constant memory, in-context gradient descent
4. **Liquid LFM2** — gated short conv + minority GQA, **hardware-in-the-loop NAS**
5. **Step-3 MFA + AFD** — Multi-Matrix Factorization Attention + disaggregated decode

---

## 1. MiniMax-Text-01 / MiniMax-M1 (MiniMax, Jan 2025 / Jun 2025)

**Release**: MiniMax-Text-01 Jan 2025 / MiniMax-M1 Jun 2025
**Size**: 456B total, 45.9B active (MoE, 32 experts)
**Context**: 1M training, **4M extrapolation** (Text-01); 1M native (M1)
**Architecture**: Hybrid — Lightning Attention every 7 layers + softmax attention; MoE FFN

### Novel Training Techniques
- **Lightning Attention**: linear O(d²n) instead of O(n²d), seven blocks per softmax block
- **3-phase context-length curriculum**: gradual extension via long-sequence upsampling to 1M
- **Data packing + Varlen Ring Attention**: zero-padding concat across variable lengths
- **DeepNorm + batch-size warmup** 16M → 128M tokens
- **CISPO** (M1): full RL on 512 H800s in 3 weeks for $534K (25% FLOPs of DeepSeek-R1 at 100K gen)

### Public Releases
- HuggingFace weights: `MiniMaxAI/MiniMax-Text-01`, `MiniMax-M1-40k`, `MiniMax-M1-80k`
- arXiv 2501.08313 (Text-01) / arXiv 2506.13585 (M1)
- GitHub: `MiniMax-AI/MiniMax-M1`

### Surrogate-1 V16 Patch
- **Replace 7:1 dense attention layers with Lightning attention** in Surrogate base — rewrite `attention.py` to alternate `LightningAttn` (7 layers) + `SoftmaxAttn` (1 layer)
- Adopt **3-phase curriculum** in `train.py`: short → medium → 1M-token packed sequences
- Apply **CISPO RL** for thinking-budget scaling (40K / 80K variants)

---

## 2. Tencent Hunyuan Family (Hunyuan-Large, Hunyuan-T1, Hunyuan-TurboS)

### Hunyuan-Large (Nov 2024)
**Size**: 389B total, 52B active (MoE), 256K ctx
**Architecture**: Transformer MoE with **mixed routing (shared + routed experts)**

#### Novel Techniques
- **Shared expert + top-k routable mix**: each token sees one shared expert + several routable
- **Cross-Layer Attention (CLA) + GQA = 95% KV cache reduction** (CLA shares KV across adjacent layers)
- **Expert-specific learning rates**
- **Synthetic data orders larger than prior literature**

### Hunyuan-T1 (Mar 2025) + TurboS (May 2025)
**Architecture**: First **Hybrid-Mamba-Transformer-MoE** at ultra-large scale
- TurboS: 56B activated (560B total), 128 layers, **AMF/MF block pattern** (Attention-Mamba-FFN / Mamba-FFN), 16T pretrain, 256K ctx
- Mamba2 (linear) + GQA + MoE FFN
- **3M instruction SFT + Adaptive Long-Short CoT Fusion + Multi-round Deliberation Learning + 2-stage RL**
- 96.7% of compute on RL post-training (T1)
- 60-80 tok/s generation (FP8 quant maintains 99.3% FP16)

### Public Releases
- arXiv 2411.02265 (Hunyuan-Large), 2505.15431 (TurboS)
- GitHub: `Tencent-Hunyuan/Hunyuan-TurboS`, `Tencent/llm.hunyuan.T1`

### Surrogate-1 V16 Patch
- **Shared-expert routing**: modify MoE router to always route through 1 shared FFN + top-k routed
- **CLA + GQA combo**: implement KV-cache-share-across-layers (target 80%+ reduction)
- **Adaptive Long-Short CoT Fusion**: SFT mix combining short answers + long thinking traces — gates verbose CoT only when needed
- **AMF/MF block pattern**: try interleaving Attn-Mamba-FFN / Mamba-FFN at 1:N ratios

---

## 3. Stepfun Step-3 / Step-3.5-Flash (2025)

**Size**: 321B total, 38B active (MoE multimodal)
**Architecture**: **Multi-Matrix Factorization Attention (MFA)** + **Attention-FFN Disaggregation (AFD)**

### Novel Techniques
- **MFA**: factorizes attention into multiple small matrices — reduces both KV cache AND compute while preserving expressiveness
- **AFD**: distributed inference decouples attention layers from FFN layers into separate subsystems (specialized hardware allocation)
- **StepMesh**: communication library for cross-subsystem AFD
- **Model-system co-design**: architecture chosen jointly with hardware deployment cost

### Public Releases
- arXiv 2507.19427 — Step-3 cost-effective decoding paper
- Apache 2.0, weights on HuggingFace `stepfun-ai/step3`, `stepfun-ai/Step-3.5-Flash`
- StepMesh communication lib open-sourced

### Surrogate-1 V16 Patch
- **MFA replacement for MHA/MLA** in Surrogate attention layers — try MFA factorization for attention KV
- For multi-GPU inference: investigate **AFD-style separation** of attention/FFN onto different GPU pools

---

## 4. 01.AI Yi-Lightning (Dec 2024)

**Architecture**: Enhanced MoE with expert segmentation; **3 sliding-window attn + 1 full attn** hybrid
**Context**: Standard with cross-layer KV reuse (50% memory cut for full-attn parts)

### Novel Techniques
- **Hybrid attention blocks**: 3 SWA + 1 full attention layer (local + global)
- **Cross-layer KV cache reuse** for full-attn layers (82.8% memory reduction)
- **RAISE safety framework** (Responsible AI Safety Engine, 4 components)
- **Expert FFN segmentation** into smaller specialist units for parallel routing

### Public Releases
- arXiv 2412.01253 — Yi-Lightning Technical Report
- HuggingFace papers; older Yi base on `01-ai/Yi`

### Surrogate-1 V16 Patch
- **3:1 SWA-to-full-attn ratio** — try replacing some full-attn with sliding-window-attn (window 4K)
- **Cross-layer KV reuse** for the full-attn-only positions

---

## 5. ByteDance Doubao 1.5 Pro + UltraMem (Jan-Feb 2025)

**Size**: 20B activated, equivalent to 140B dense
**Architecture**: Sparse MoE + **UltraMem (Product Key Memory)** layers

### Novel Techniques
- **UltraMem**: alternative to MoE — splits multiple **small memory layers** distributed across transformer at regular intervals
- **Skip-layer operation**: parallel memory access + transformer compute
- **20M values memory layer** — paths the way for "billion-expert" models
- **3× efficiency over conventional MoE** at same quality
- **"Voice-Native"**: training on raw audio tokens (no STT step)
- **Heterogeneous prefill-decode + attention-FFN disaggregation**
- ICLR 2025 published

### Public Releases
- ByteDance Seed blog + paper (ICLR 2025)
- Architecture spec disclosed; weights closed-source

### Surrogate-1 V16 Patch
- **PKM-style memory layer**: prototype `UltraMemLayer` to replace one of the FFN layers (every 8 blocks)
- **Voice-native ingestion** if Surrogate ever adds audio: tokenize raw waveform instead of STT

---

## 6. Baichuan-M2-32B (Sep 2025)

**Base**: Qwen2.5-32B continued pretraining
**Specialty**: **Medical reasoning** (HealthBench Hard >32, near GPT-5)

### Novel Techniques
- **Large Verifier System**: patient simulators + multi-dimensional verification
- **Mid-Training adaptation**: lightweight medical domain shift while preserving general capability
- **Multi-stage RL** with medical reward
- 4-bit quant runs on single RTX 4090; MTP version 58.5% throughput

### Public Releases
- arXiv 2509.02208
- Weights `baichuan-inc/Baichuan-M2-32B` + GPTQ-Int4

### Surrogate-1 V16 Patch
- **Verifier System pattern**: build domain-specific simulators (e.g., shell sandbox simulators for ops Surrogate role) to verify outputs during RL
- **Mid-Training**: adopt as the "role specialization" stage — preserve base ability while injecting domain skill

---

## 7. InternLM 3 + InternLM-Math-Plus (Shanghai AI Lab, Jan 2025)

**InternLM3-8B-Instruct**: 4T tokens, **75% lower training cost** than peers
**InternLM-Math-Plus**: **100B math-continued-pretraining + 2M bilingual math SFT**, beats DeepSeek-Math-7B-RL

### Novel Techniques
- Single model integrates deep reasoning + general conversation (no separate reasoning model)
- High data-quality filtering for 4T budget
- InternLM2-Math-Plus-Mixtral8x22B matches Claude 3 Opus on math

### Public Releases
- GitHub `InternLM/InternLM`, `InternLM/InternLM-Math`
- Weights on HuggingFace `internlm/`

### Surrogate-1 V16 Patch
- **Math continued pretraining recipe**: ~100B math tokens + 2M SFT — direct template for Surrogate's math-coding role
- **Unified reasoning+chat** — single Surrogate instead of separate "thinking" / "fast" variants

---

## 8. Microsoft Phi-4 / Phi-4-Reasoning / Phi-4-Mini-Reasoning (Dec 2024 - Apr 2025)

**Phi-4**: 14B dense, 50 synthetic datasets / 400B synthetic tokens
**Phi-4-mini-reasoning**: 3.8B, beats DeepSeek-R1-Distill-Qwen-7B by 3.2 pts on Math-500
**Phi-4-multimodal**: 5.6B unified speech+vision+text

### Novel Techniques
- **50 synthetic datasets / 400B synthetic tokens** generated via:
  - Multi-agent prompting
  - Self-revision workflows
  - Instruction reversal
  - GPT-4o rewriting web pages → exercises/Q&A/structured reasoning
- **Synthetic code validated through execution loops** (only correct retained)
- **4-stage Phi-4-mini-reasoning recipe**:
  1. Mid-training on distilled long-CoT data
  2. SFT on high-quality long-CoT
  3. **Rollout DPO** with curated preference dataset
  4. RLVR
- 1M synthetic math problems, 8 rollouts each, only correct kept = 30B math tokens

### Public Releases
- arXiv 2412.08905 (Phi-4), 2504.21318 (Phi-4-reasoning), 2504.21233 (mini-reasoning)
- Weights `microsoft/phi-4`, `microsoft/Phi-4-reasoning`, `microsoft/Phi-4-mini-reasoning`

### Surrogate-1 V16 Patch
- **Adopt 4-stage Mini-Reasoning recipe** as Surrogate's small-variant (3-7B) post-training pipeline
- **Synthetic data validation via execution**: drop-in for any code task — only retain rollouts whose `python -c` exits 0
- **Instruction reversal**: take answer → generate question — diversifies Surrogate's instruction data

---

## 9. IBM Granite 4 / 4.1 (Oct-Nov 2025)

**Architecture**: **Hybrid Mamba-2 / Transformer with 9:1 Mamba:Attn ratio** (4.0); 4.1 hybrid + 512K ctx
**Sizes**: 3B, 8B, 30B (4.1); 4.0-Micro / 4.0-h-Micro (h = hybrid)
**Training**: 22T enterprise-curated corpus (4.0); 15T multi-stage with long-context extension (4.1)

### Novel Techniques
- **9 Mamba-2 blocks per 1 Transformer block** — linear scaling for most of the stack
- 70%+ RAM reduction at long ctx
- Multi-stage pretraining: broad → annealing toward technical/scientific/math instruction
- Pretrain corpus: DataComp-LM (DCLM) + GneissWeb + TxT360 + Wikipedia + enterprise-relevant
- Granite-Code: 116 programming languages, Git-commit-paired-with-instruction synthetic data

### Public Releases
- HuggingFace `ibm-granite/granite-4.0-micro`, `granite-4.1-8b`, `granite-4.0-h-micro`
- Full training code + recipes + weights open

### Surrogate-1 V16 Patch
- **Drop-in base swap**: try Granite-4.1-8B as Surrogate base instead of Qwen3 — get the Mamba efficiency immediately
- **9:1 Mamba:Attn ratio** as architecture template for V16
- **22T enterprise pretrain mix** as filter recipe for Surrogate ops/code data

---

## 10. NVIDIA Nemotron Family (Nemotron-4-340B, Llama-Nemotron, Nemotron-Mini-4B, Nemotron-3 Nano-Omni)

**Nemotron-4-340B**: synth data generation + reward model + instruct trio
**Llama-Nemotron**: Nano/Super/Ultra reasoning trio (CES Jan 2025)
**Nemotron-Mini-4B**: GQA + RoPE, distillation+pruning+quantization for on-device
**Nemotron 3 Nano-Omni**: multimodal agent reasoning, single efficient model

### Novel Techniques
- Family shifted from dense → hybrid Mamba-attn → MoE routing (Nemotron-3)
- **Synth-data-first philosophy**: 340B specifically designed to GENERATE synthetic training data
- Distillation + pruning + quantization combined for Mini variants

### Public Releases
- HuggingFace `nvidia/Nemotron-Mini-4B-Instruct`, `nvidia/Nemotron-4-340B-Instruct`
- GitHub `NVIDIA-NeMo/Nemotron` developer asset hub (recipes, datasets, examples)

### Surrogate-1 V16 Patch
- **Synthetic data via Nemotron-4-340B**: use as bootstrap teacher for Surrogate trace generation (cheap distill-and-prune target)
- **Distill-prune-quantize pipeline** for Surrogate-1-Mini deployment variants

---

## 11. Allen AI OLMo 2 / Olmo 3 / OLMoE / Tülu 3

### OLMo 2 (Late 2024 - Mar 2025)
**Sizes**: 7B / 13B / 32B dense
**Training**: OLMo-Mix-1124 (3.9T tokens from DCLM + Dolma + Starcoder + Proof Pile II), 1.5 epochs / 6T
**Pipeline**: pretrain → midtrain → SFT → DPO → RLVR (Tülu 3 method applied)

### OLMoE (Sep 2024)
**Sizes**: 1B active / 7B total MoE — first fully-open MoE
**Training**: 5.1T tokens, FSDP + mixed-precision, 256× H100 / 10 days
**Trained 2× faster than dense at same active params**

### Olmo 3 (Nov 2025)
**"Model Flow"**: full lifecycle open — every stage, ckpt, datapoint, dependency
**3-stage training**: Dolma 3 Mix → Dolma 3 Dolmino (mid) → Dolma 3 Longmino (long-ctx)
**Olmo 3-Think**: SFT (Dolci-Think-SFT built on OpenThoughts3 + Nemotron Post-training + SYNTHETIC-2) → thinking DPO → RLVR
**Olmo 3-Think 32B**: strongest fully-open thinking model; matches Qwen 3 8B on MATH at 7B

### Public Releases
- Weights, full training data, training code, training logs, **thousands of intermediate ckpts**
- HuggingFace `allenai/`, GitHub `allenai/OLMo`, `allenai/OLMoE`, `allenai/open-instruct`
- arXiv 2501.00656 (OLMo 2), 2409.02060 (OLMoE), 2512.13961 (Olmo 3)

### Surrogate-1 V16 Patch (CRITICAL)
- **Adopt Olmo 3 model-flow as Surrogate's training spec**: pretrain → midtrain → long-ctx → SFT → DPO → RLVR — **mirror exactly**
- **Use Tülu 3 SFT/DPO recipes** via `allenai/open-instruct` codebase
- **Use Dolma 3 Mix as pretraining mix** (or carefully filtered subset for Surrogate's 24GB Mac CLI constraints)
- **Dolci-Think-SFT data**: direct seed for Surrogate reasoning role

---

## 12. Salesforce xLAM-2 + APIGen-MT (2025)

**Architecture**: MoE-based Large Action Models (xLAM-2-8B/70B-fc-r)
**Specialty**: **Multi-turn function calling**

### Novel Techniques
- **APIGen-MT**: 2-phase agentic pipeline
  - Phase 1: LLM-reviewer committee + iterative feedback generates task blueprints with ground-truth actions
  - Phase 2: simulated human-agent interplay → multi-turn trajectories
- **Tool calling = first-class**: agents call → interpret → ask clarifying questions → adapt
- **τ-bench**: 56.2% (xLAM-2-70b-fc-r), beats GPT-4o, approaches Claude 3.5 Sonnet

### Public Releases
- `Salesforce/xLAM-2-*` weights; `Salesforce/APIGen-MT-5k` dataset; `xlam-function-calling-60k` (60k single-turn)
- arXiv 2504.03601 (APIGen-MT)
- GitHub `SalesforceAIResearch/xLAM`

### Surrogate-1 V16 Patch (HIGH)
- **Drop in APIGen-MT-5k + xlam-function-calling-60k** to Surrogate's tool-use SFT mix immediately (free, Apache 2.0)
- **Implement APIGen-MT pipeline** for Surrogate-specific tool synthesis: LLM committee → blueprint → simulated turns
- **Multi-turn first**: shift Surrogate's eval from one-shot to τ-bench-style multi-turn

---

## 13. Cohere Aya Expanse 8B / 32B (2024-2025)

**Specialty**: 23 enterprise + 101 supported languages
**Pipeline**: SFT → preference training → model merging

### Novel Techniques
- **Data Arbitrage**: cross-source diverse training data routing (technique for multilingual)
- **Multilingual preference training**
- **Safety tuning**
- **Model merging** as final step
- 30% infrastructure cost reduction vs comparable models

### Public Releases
- Weights `CohereLabs/aya-expanse-8b`, `aya-expanse-32b`
- arXiv 2412.04261

### Surrogate-1 V16 Patch
- **Model merging as final step**: post-RL, merge Surrogate variants (math, code, ops) into single multi-role model
- **Data arbitrage** = principled multi-source data sampling — applicable to multi-role Surrogate

---

## 14. Reka Flash 3 (2025)

**Size**: 21B reasoning model, trained from scratch
**Architecture**: 'Noam' transformer (SwiGLU + GQA + RoPE), encoder-decoder multimodal
**Context**: 128K, interleaved multimodal (text+image+video+audio)

### Novel Techniques
- **Explicit reasoning tags** `<reasoning>...</reasoning>` in output
- **REINFORCE Leave-One-Out (RLOO)** — alternative to PPO/GRPO
- Model-based + rule-based rewards combined
- Pretrained on synthetic + public data

### Public Releases
- Weights `RekaAI/reka-flash-3`
- arXiv 2404.12387 (Core/Flash/Edge tech report)
- GGUF quants (DavidAU)

### Surrogate-1 V16 Patch
- **RLOO instead of GRPO/PPO**: try as RL algorithm — leave-one-out advantage estimator simpler than PPO clip
- **`<reasoning>` explicit tags** for Surrogate's CoT mode

---

## 15. HuggingFace SmolLM3-3B (2025)

**Size**: 3B
**Training**: 11.2T tokens, 3-stage (web → code → math/reasoning)

### Novel Techniques
- **NoPE every 4th layer** (RoPE→NoRoPE→Back paper, Yang et al. 2025) — selectively removes positional embeddings — **boosts long-ctx without hurting short**
- GQA with 4 groups
- Tied embeddings (Llama-style)
- **Midtraining 140B reasoning tokens** before SFT
- **APO (Anchored Preference Optimization)** post-training
- **Full open**: data mixtures, configs, training code released as "engineering blueprint"

### Public Releases
- Weights `HuggingFaceTB/SmolLM3-3B`, `SmolLM3-3B-Base`
- Full HF blog + training playbook + GitHub `huggingface/smollm`

### Surrogate-1 V16 Patch (HIGH)
- **NoPE every 4th layer**: drop-in change to Surrogate attention — should boost long-ctx for free
- **APO instead of DPO**: try anchored preference optim
- **3-stage curriculum (web → code → math)**: matches Surrogate's role-comprehensive training

---

## 16. Liquid AI LFM2 / LFM2.5 (2025)

**Sizes**: 350M / 700M / 1.2B / 2.6B dense + 8.3B MoE (1.5B active)
**Context**: 32K
**Architecture**: Hybrid — most blocks **gated short convolutions (LIV)**, minority **GQA**

### Novel Techniques
- **Hardware-in-the-loop architecture search** under edge latency/memory constraints
- **LIV (Linear Input-Varying) systems**: conv/recurrence/attn unified under one input-aware framework — weights generated on-the-fly from input
- **Multiplicative gates + short convolutions**
- LFM2-350M: 10 LIV blocks + 6 GQA blocks
- Multimodal vision/audio + late-interaction retrieval variants

### Public Releases
- Weights `LiquidAI/LFM2-350M`, `LFM2.5-1.2B-Instruct`
- arXiv 2511.23404 (LFM2 Tech Report)
- LM Studio integration

### Surrogate-1 V16 Patch
- **Edge-deployment Surrogate variant**: use LFM2 architecture as base for Surrogate-Mini (Mac/edge)
- **Hardware-aware NAS**: run latency-constrained search before training (prevents over-engineering)

---

## 17. Stability AI StableLM Zephyr 3B + AI21 Jamba 1.5 + Falcon-Mamba 7B

### StableLM Zephyr 3B
- 3B optimized for chat, **DPO at 3B scale (early adopter)**
- Beat Llama-2-70b-chat on MT-Bench

### Jamba 1.5 (Large 94B / mini 12B active)
- **1:7 Attn:Mamba ratio + MoE every 2 blocks**
- 256K ctx, top RULER long-ctx scores
- Hybrid Mamba-Transformer-MoE — inspired Hunyuan-TurboS

### Falcon-Mamba 7B
- **Pure Mamba** (no attention) at 7B — first attention-free 7B matching transformer baselines
- Excels at long-ctx reasoning

### Surrogate-1 V16 Patch
- **DPO-at-3B (Zephyr)** as Surrogate-Mini's standard alignment
- **Pure Mamba experiment** (Falcon-style) for ultra-long-ctx Surrogate variant

---

## 18. RWKV-7 "Goose" (Mar 2025)

**Architecture**: Pure RNN, no attention, no KV cache, **constant memory + constant time per token**

### Novel Techniques
- **Generalized delta rule with vector-valued gating + in-context learning rates**
- **Relaxed value replacement rule**
- **Test-time-training**: state evolves via in-context gradient descent at every token
- Beats attention/linear-attention paradigm — solves problems attention cannot at same compute
- 2.9B params → SoTA on multilingual + matches 3B SoTA on English
- Linux Foundation AI project, Apache 2.0

### Public Releases
- arXiv 2503.14456
- GitHub `BlinkDL/RWKV-LM`
- `rwkv-fla` Triton kernels

### Surrogate-1 V16 Patch (RESEARCH)
- **Surrogate-1 Mac variant** could be pure RWKV-7 — constant memory means **infinite context on 24GB**
- **In-context gradient descent** as a meta-learning bootstrap — Surrogate "learns" from its own session traces

---

## 19. EleutherAI — Pythia + PolyPythias + Common Pile (2025)

### Common Pile
- License-clean replacement for The Pile
- Two new models trained on it (released 2025)

### PolyPythias
- ICLR 2025: 50 pretraining runs at 160M, 410M, 1B, 2.8B
- Separates **data-ordering** vs **weight-init** effects
- Public dataset of ckpts for stability/outlier analysis

### Surrogate-1 V16 Patch
- **Use Common Pile as license-clean pretraining base** for any Surrogate ablations
- **Multi-seed training**: when Surrogate seems unstable, train 3-5 seeds (Pythia-style) and pick best — cheap reliability hedge

---

## 20. Tokyo Tech — Llama 3.3 Swallow 70B (2025)

**Specialty**: Japanese sovereign LLM
**Training**: 256× H100 / 16d 6h via SageMaker HyperPod, **4D parallelism** (data + tensor + pipeline + sequence)

### Novel Techniques
- **Swallow Education Classifier** to filter Common Crawl for educationally-valuable Japanese
- **Continual pretraining** on Llama 3.3 base (cheaper than scratch)
- Megatron-LM 4D parallelism for efficiency

### Public Releases
- Weights `tokyotech-llm/Llama-3.1-Swallow-8B-Instruct-v0.5` etc.
- Swallow Corpus v2 published
- AWS blog with full SageMaker HyperPod recipe

### Surrogate-1 V16 Patch
- **Education Classifier**: build a "Surrogate-relevant" classifier for filtering ops/code training data — quality over quantity
- **Continual pretraining template**: cheaper Surrogate role specialization (don't pretrain from scratch)

---

## 21. ByteDance Seed-Coder 8B (2025)

**Specialty**: Code (Base + Instruct + Reasoning at 8B)

### Novel Techniques
- **"Let the code model curate data for itself"** — model-centric data curation, not hand-crafted rules
- LLM-driven code-data filtering pipeline
- ELO 1553 on Codeforces (approaching o1-mini)

### Public Releases
- arXiv 2506.03524
- GitHub `ByteDance-Seed/Seed-Coder`, `Stable-DiffCoder` (diffusion code LM)
- `Seed-Thinking-v1.5` — reasoning variant

### Surrogate-1 V16 Patch
- **Self-curated data**: have Surrogate filter its own training corpus — labels low-quality with own judgment
- **Diffusion code LM (Stable-DiffCoder)**: experimental alternative to autoregressive for code

---

## 22. Salesforce xGen / SFR-Embedding-Mistral / CodeT5+ Legacy

### SFR-Embedding-Mistral
- Top-ranking text-embedding (MTEB 67.6)
- Trained on top of E5-mistral-7b-instruct + Mistral-7B-v0.1
- Use case: retrieval for RAG

### CodeT5+
- Encoder-decoder code LM family — span denoising + causal LM + contrastive + text-code matching
- Repo archived May 2025 but forks active

### xGen / SFR-RAG / SFR-Judge / SFR-RAG-Agent
- Newer Salesforce stack for retrieval-augmented agents

### Surrogate-1 V16 Patch
- **Use SFR-Embedding-Mistral** as Surrogate's RAG retriever (pair with FalkorDB graph)
- **SFR-Judge** as evaluator for Surrogate trace quality

---

## 23. Allen AI OLMoE-1B-7B-0924 (Sep 2024)

**Architecture**: 1B active, 7B total — **first fully-open MoE** with all artifacts
**Trained 2× faster than dense at same active params**

### Public Releases
- Pretrain ckpts + code, SFT ckpts + code, DPO/KTO ckpts + code
- HuggingFace `allenai/OLMoE-1B-7B-0924`
- vLLM + SGLang + llama.cpp + transformers integrated

### Surrogate-1 V16 Patch
- **Direct base swap candidate** for Surrogate-Mini (1B active = fits 24GB Mac comfortably)
- **MoE training data mix**: Dolma + DataComp-Baseline (study in detail before V16 mix)

---

## 24. Microsoft Phi-4-Multimodal (5.6B)

**Architecture**: Unified single-model speech + vision + text
**Phi-4-mini**: 3.8B dense, GQA, 200K vocabulary, shared input-output embeddings

### Surrogate-1 V16 Patch
- **Shared input-output embeddings** + 200K vocab as efficiency tweak for Surrogate's tokenizer
- If multimodal Surrogate ever needed: Phi-4-multimodal architecture as starting point

---

## 25. Cross-Cutting Patterns Across All 25 Models

### Architectural Convergence (everyone is doing it)
1. **Hybrid attention/SSM**: MiniMax (7:1), Hunyuan-TurboS (AMF/MF), Granite 4 (9:1), Jamba (1:7), Kimi-Linear (3:1), Liquid LFM2
2. **MoE with shared+routed experts**: Hunyuan-Large, DeepSeek-V3 (already covered), Yi-Lightning
3. **CLA / KV cache compression**: Hunyuan, Yi-Lightning, Kimi-Linear (75%), Granite (70%)
4. **Long-ctx via curriculum**: MiniMax, Olmo 3 Longmino, Granite 4.1, Phi-4

### Training Convergence (recipe pattern)
1. **Pretrain → midtrain → SFT → DPO/APO/Rollout-DPO → RLVR**: Olmo 3, Phi-4, OLMo 2, Hunyuan-TurboS
2. **Synthetic data via execution validation**: Phi-4, Seed-Coder, MiniMax (rollout filter)
3. **Multi-turn trajectory synthesis**: APIGen-MT, Hunyuan Multi-round Deliberation, Seed-Thinking-v1.5
4. **Education/quality classifier on raw web**: Swallow, Phi-4 (GPT-4o rewriting), Granite (DCLM filter)

### Data Convergence (everyone uses these)
- **DCLM** (DataComp-LM): OLMo 2, OLMoE, Granite 4
- **Dolma**: OLMo 2, OLMo 3 (3 variants)
- **OpenThoughts3**: Olmo 3-Think
- **Nemotron Post-training**: Olmo 3-Think
- **APIGen-MT**: xLAM-2 + many community projects

---

## V16 Concrete Patches — Action Items Sorted by Effort

### Tier 1 (High-Value, Low-Effort, Drop-In NOW)
1. **Add APIGen-MT-5k + xlam-function-calling-60k to Surrogate's tool-use SFT** (Apache 2.0, ready)
2. **NoPE every 4th layer** (SmolLM3 trick) — 5 lines in `attention.py`, free long-ctx boost
3. **Use Olmo 3 model-flow recipe template** (`allenai/open-instruct`) as Surrogate post-training spec
4. **Replace DPO with APO** (SmolLM3 / Anchored Preference Optim) — better stability
5. **`<reasoning>...</reasoning>` explicit tags** (Reka Flash 3 pattern) for CoT separation

### Tier 2 (Medium, Architecture Changes)
6. **Lightning Attention 7:1** (MiniMax) — replace 7/8 of softmax attn with linear → 4M ctx capability
7. **CLA + GQA combined** (Hunyuan) — 80%+ KV cache cut
8. **Shared expert + top-k routed** (Hunyuan-Large MoE pattern)
9. **9:1 Mamba:Attn** (Granite 4) — base-swap candidate for V16 efficient mode
10. **3:1 KDA:MLA** (Kimi-Linear) — 75% KV cut, 6× decode at 1M

### Tier 3 (Research, Higher Risk)
11. **UltraMem PKM layer** (ByteDance ICLR 2025) — alternative to MoE
12. **Pure RWKV-7** (Mac variant for infinite ctx)
13. **MFA + AFD** (Step-3) — multi-GPU disaggregated decode
14. **RLOO instead of GRPO** (Reka Flash 3) — simpler advantage estimator
15. **LIV systems + hardware-in-the-loop NAS** (Liquid LFM2) — for edge variant

### Tier 4 (Process / Discipline)
16. **Mid-Training adaptation** (Baichuan-M2): role specialization without losing general capability
17. **Verifier System** (Baichuan-M2): ops sandbox + multi-dim verification
18. **Synthetic-data-via-execution-validation** (Phi-4): only retain rollouts that exit 0
19. **Self-curated data** (Seed-Coder): Surrogate filters its own corpus
20. **Multi-seed stability** (PolyPythias): cheap reliability hedge

---

## V16 Recommended Base-Swap Shortlist (Priority Order)

| Choice | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Granite-4.1-8B (hybrid Mamba)** | 70% RAM cut, 512K ctx, full open, enterprise-curated 22T | New arch, less community tooling | **TOP — try first for V16** |
| **Olmo 3-Base 7B/32B** | Fully open model flow, every ckpt, Tülu recipe | Pure transformer — no efficiency win | Backup if hybrid fails |
| **Kimi-Linear 48B-A3B (MoE)** | 3B active fits Mac, Triton kernels open, 1M ctx, 6× decode | New arch, MoE complexity | Mac-friendly variant |
| **MiniMax-Text-01** | 4M ctx, 25% FLOPs vs DeepSeek-R1 | 456B total — too big for Mac | Cloud-only Surrogate |
| **OLMoE-1B-7B** | First fully open MoE, Mac-friendly, all artifacts | Older (Sep 2024) | Stable choice |
| **SmolLM3-3B** | NoPE trick, full HF blueprint | Small for full Surrogate | Mini variant base |

---

## What Frontier Labs HAVE NOT Published (But Long-Tail Has)

1. **UltraMem PKM** (ByteDance Seed) — 83% inference cost cut, no Anthropic/OpenAI equivalent disclosed
2. **APIGen-MT 2-phase pipeline** (Salesforce) — frontier teams almost certainly have similar but unpublished
3. **Adaptive Long-Short CoT Fusion** (Hunyuan-T1) — gates verbose CoT only when needed
4. **NoPE every 4th layer** (SmolLM3 + Yang et al.) — small but effective trick
5. **RLOO** (Reka) — simpler than GRPO/PPO, works
6. **Verifier System with patient simulators** (Baichuan-M2) — domain-RL pattern frontier hasn't written up
7. **Education Classifier filter** (Swallow) — quality-over-quantity selector
8. **Self-curated code data** (Seed-Coder) — model labels its own corpus
9. **Mid-Training role adaptation** (Baichuan, Olmo 3 Dolmino) — preserves general while injecting domain
10. **Hardware-in-the-loop architecture search** (Liquid LFM2) — under-explored for SLMs

---

## Sources & Reading List

### Papers (priority order for Surrogate V16)
- arXiv 2512.13961 — **Olmo 3** (model flow)
- arXiv 2506.13585 — **MiniMax-M1** (lightning attn + CISPO RL)
- arXiv 2505.15431 — **Hunyuan-TurboS** (Mamba-Transformer synergy)
- arXiv 2510.26692 — **Kimi-Linear** (KDA + 3:1 hybrid)
- arXiv 2504.21233 — **Phi-4-mini-reasoning** (4-stage SLM)
- arXiv 2504.03601 — **APIGen-MT** (multi-turn synthesis)
- arXiv 2509.02208 — **Baichuan-M2** (verifier system)
- arXiv 2412.01253 — **Yi-Lightning** (hybrid attn + RAISE)
- arXiv 2511.23404 — **LFM2** (HW-in-the-loop NAS)
- arXiv 2503.14456 — **RWKV-7 Goose**
- arXiv 2501.08313 — **MiniMax-Text-01**
- arXiv 2411.02265 — **Hunyuan-Large**
- arXiv 2409.02060 — **OLMoE**
- arXiv 2412.04261 — **Aya Expanse**
- arXiv 2506.03524 — **Seed-Coder**
- arXiv 2507.19427 — **Step-3** (MFA + AFD)
- arXiv 2412.08905 — **Phi-4** (synth data)

### GitHub Repos (clone-and-read)
- `allenai/open-instruct` — Tülu 3 / Olmo 3 post-training stack
- `allenai/OLMo`, `allenai/OLMoE` — pretrain code
- `allenai/dolma` — data pipeline
- `MoonshotAI/Kimi-Linear` — KDA Triton kernels
- `MiniMax-AI/MiniMax-M1` — lightning attn
- `Tencent-Hunyuan/Hunyuan-TurboS`
- `BlinkDL/RWKV-LM`
- `huggingface/smollm`
- `SalesforceAIResearch/xLAM`
- `ByteDance-Seed/Seed-Coder`, `Seed-Thinking-v1.5`
- `NVIDIA-NeMo/Nemotron`
- `InternLM/InternLM`, `InternLM/InternLM-Math`

### HuggingFace Datasets (free SFT seeds)
- `Salesforce/APIGen-MT-5k`
- `Salesforce/xlam-function-calling-60k`
- (Pull Olmo 3 Dolma 3 Mix / Dolmino / Longmino when public)

---

**Status**: ready for V16 architecture review. Primary recommendations:
1. **Architecture**: try Granite-4.1-8B hybrid Mamba as new base; fallback Olmo 3
2. **Training pipeline**: adopt Olmo 3 model-flow exactly (pretrain → midtrain → long → SFT → DPO → RLVR)
3. **Quick wins**: NoPE-every-4 + APIGen-MT data + APO + reasoning tags

> Next: V17 should cover (a) RL beyond RLVR (process rewards, GRPO+, RLOO comparison), (b) test-time-compute scaling (MiniMax-M1 thinking budgets, OpenAI o3-style), (c) post-training data flywheels.

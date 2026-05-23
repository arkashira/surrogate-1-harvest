---
title: Open-Source LLM Releases — 2026 Q2 Frontier (March-April)
date: 2026-05-01
window: 2026-03-01..2026-04-30
purpose: Inform Surrogate-1 (Qwen2.5-Coder fine-tune) training pipeline — datasets, techniques, benchmarks
status: research-snapshot
tags: [opensource, llm, training, qwen, deepseek, mistral, llama, granite, nemotron, surrogate-1]
related: [[coding-llm-frontier]] [[self-improvement]] [[data-ml-aiops]]
---

# Open-Source LLM Releases — 2026 Q2 (March-April)

> Snapshot of new open-weight model releases in the 60-day window prior to 2026-05-01, with explicit focus on training data, post-training recipes, and benchmark deltas relevant to **Surrogate-1** (Qwen2.5-Coder-7B/14B/32B SFT+RL fine-tune).

The two months covered span a *flagship-quality plateau* on dense 27-32B coding models (Qwen3.6-27B, Devstral-2, Llama-Nemotron-49B-v1.5) and a *frontier MoE refresh* from DeepSeek V4 (preview) and Qwen3.6-Max. RL post-training has converged on **GRPO + DAPO with verifiable rewards (RLVR)** as the de-facto recipe; SFT data is increasingly executor-validated synthetic traces from teacher models.

---

## 1. Qwen Family (Alibaba) — most relevant for Surrogate-1

### Qwen3.6-27B (dense) — 2026-04-22
- **Architecture**: Dense decoder-only Transformer, 27B params, 256K native context, vocab 151,936 (carried over from Qwen3 BPE — *fully tokenizer-compatible with Qwen2.5/Qwen3 LoRAs*) ([HF model card](https://huggingface.co/Qwen/Qwen3.6-27B), [Qwen blog](https://qwen.ai/blog?id=qwen3.6-27b)).
- **Headline claim**: 27B dense beats older 397B-A17B MoE on coding — `77.2% SWE-bench Verified` vs Claude Opus 4.6 80.8% ([buildfastwithai review](https://www.buildfastwithai.com/blogs/qwen3-6-27b-review-2026)).
- **Training recipe (inherited from Qwen3 + Qwen3.5)**:
  - Pre-train: 3-stage on 36T tokens, 119 languages, with Qwen2.5-VL extracting text from PDFs and Qwen2.5-Math/Coder generating textbooks + Q&A + code synth ([Qwen3 tech report](https://arxiv.org/pdf/2505.09388), [Kili data deep-dive](https://kili-technology.com/blog/data-story-qwen3)).
  - Stage 1: 30T general; Stage 2: knowledge-intensive STEM/code upweight; Stage 3: long-context (4K → 32K).
  - Post-train: SFT → DPO/SimPO → **GRPO with RLVR** for reasoning + tool-use.
  - "Flagship-level coding in a 27B dense model" — official tagline.
- **Long-context**: native 256K, validated at 1M via static YaRN scaling ([vLLM Qwen3.6 recipe](https://docs.vllm.ai/projects/recipes/en/latest/Qwen/Qwen3.5.html)).
- **Public training**: no full pre-train code, but **Unsloth + Axolotl + LLaMA-Factory all support Qwen3.6 fine-tuning**, and Qwen-Agent ships training/inference templates for Hermes-style tool use ([Qwen-Agent](https://github.com/QwenLM/Qwen-Agent)).

### Qwen3.6-Max-Preview (closed-but-API) — 2026-04-20
- Qwen team flagship, MoE, took **#1 on six coding benchmarks including SWE-bench Pro** ([TokenMix review](https://tokenmix.ai/blog/qwen3-6-max-preview-benchmark-review-2026)).
- Notable signal: Alibaba is moving frontier to closed-weights — but the **27B distillation target is open**.
- **Architectural shift**: Gated Delta Networks + sparse MoE for high-throughput inference ([buildfastwithai Max review](https://www.buildfastwithai.com/blogs/qwen3-6-max-preview-review-2026)).

### Qwen3-Coder-Next — 2026-04-08 (still relevant)
- 80B total / 3B active MoE, 64 routers top-2, **256K native context**, hybrid linearized + sparse attention.
- `58.7% SWE-bench Verified` (>70% with SWE-Agent scaffold), `42% LiveCodeBench` ([Qwen blog](https://qwen.ai/blog?id=qwen3-coder-next)).
- Claims "elaborate training recipe for long-horizon reasoning, complex tool use, recovery from execution failures".

### Qwen3.5-35B-A3B — 2026-02-16 (slightly outside window but contextual)
- MoE 35B/3B-active, predecessor to 3.6.

---

## 2. DeepSeek — frontier MoE, 1M context preview

### DeepSeek V4 (Preview) — 2026-04-24
- **Two variants** ([DeepSeek API docs](https://api-docs.deepseek.com/news/news260424), [Reuters via CNBC](https://www.cnbc.com/2026/04/24/deepseek-v4-llm-preview-open-source-ai-competition-china.html)):
  - V4-Pro: **1.6T total / 49B active MoE**, 1M context, 384K max output.
  - V4-Flash: **284B / 13B active MoE**, 1M context.
- Pricing: Flash $0.14/M in $0.28/M out; Pro $1.74/$3.48 — preview phase, not final ([Simon Willison](https://simonwillison.net/2026/Apr/24/deepseek-v4/)).
- Carries over architecture pillars: **MLA (Multi-head Latent Attention, 93.3% KV cache reduction)**, **DeepSeekMoE**, **FP8 mixed-precision training**, **auxiliary-loss-free load balancing**, **multi-token prediction** ([DeepSeek-V3 tech report](https://arxiv.org/abs/2412.19437)).
- New: **DSA (DeepSeek Sparse Attention)** generalized — two-stage lightning-indexer + top-k selection, kernels open-sourced in **FlashMLA + DeepGEMM + TileLang** ([V3.2 paper](https://arxiv.org/abs/2512.02556), [DeepSeek-V3.2-Exp repo](https://github.com/deepseek-ai/DeepSeek-V3.2-Exp)).

### DeepSeek mHC paper — first major 2026 architecture paper
- **Manifold-Constrained Hyper-Connections** ([arxiv 2512.24880](https://arxiv.org/abs/2512.24880)) — projects residual matrices onto Birkhoff Polytope (doubly-stochastic) via Sinkhorn–Knopp algorithm, restoring identity-mapping property destroyed by HC ([AI Papers Academy summary](https://aipapersacademy.com/deepseek-mhc/), [community impl](https://github.com/tokenbender/mHC-manifold-constrained-hyper-connections)).
- Solves training-instability + memory-access overhead from prior HC work; ships with kernel-fused mixed-precision TileLang impl. **Likely backbone for V4-Pro stability at 1.6T scale.**
- Impact: **Surrogate-1 should monitor — if validated for sub-100B dense, mHC could replace standard residuals**.

---

## 3. Mistral

### Devstral-2 (123B dense) + Devstral-Small-2 (24B) — 2026-Q1 carryover, hot in Q2
- Dense 123B Transformer, **256K context**, modified MIT license; Small-2 is Apache-2.0.
- **`72.2%` SWE-bench Verified** for Devstral-2; **`68.0%` for Small-2** — open-weight SOTA in dense category ([Mistral blog](https://mistral.ai/news/devstral-2-vibe-cli), [VentureBeat](https://venturebeat.com/ai/mistral-launches-powerful-devstral-2-coding-model-including-open-source)).
- Ships with **Mistral Vibe 2.0 CLI** (terminal-native coding agent) launched 2026-01-27.
- Free API trial during preview; `$0.40/$2.00` after.

### Mistral Small 4 — 2026-03-16
- Unifies Magistral (reasoning) + Pixtral (vision) + Devstral (coding) into a single configurable-effort model — **toggle reasoning depth at inference** ([Mistral blog](https://mistral.ai/news/mistral-small-4)).
- Validates the "single model with adjustable thinking budget" pattern (Anthropic's hybrid extended thinking; Phi-4-reasoning-vision; Qwen3 also has thinking/non-thinking modes).

### Codestral 25.08 (released 2025-08, still current)
- 80+ programming languages, no 25.04 release; latest open-weight code model from Mistral remains 2508 ([Mistral changelog](https://docs.mistral.ai/getting-started/changelog)).

---

## 4. Meta — Llama 4 family (released 2025-04, still the latest open Meta in window)

- **Scout** (109B total, 17B active, 16 experts, **10M context**) and **Maverick** (400B total, 17B active, 128 experts, 1M context) — *first MoE Llamas*; alternating dense+MoE layers, shared expert + 1 routed top-1 ([Meta Llama 4 blog](https://ai.meta.com/blog/llama-4-multimodal-intelligence/)).
- **Behemoth** still not released as of 2026-05-01; reasoning variant in development per [SiliconAngle](https://siliconangle.com/2025/05/15/meta-postpone-release-llama-4-behemoth-model-report-claims/).
- No new 4.x release in March-April 2026.

---

## 5. NVIDIA — Llama-3.3-Nemotron-Super-49B-v1.5

- **Released 2026-Q1, current SOTA Nemotron** ([NVIDIA model card](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5)).
- **Multi-phase post-training pipeline (worth copying)**:
  1. Block-wise knowledge distillation (FineWeb + Buzz-V1.2 + Dolma) — quality-vs-compute variants per block.
  2. SFT on Math, Code, Science, Tool-Calling.
  3. **RPO** (Reward-aware Preference Optimization) for chat.
  4. **RLVR** (Reinforcement Learning with Verifiable Rewards) for reasoning.
  5. Iterative **DPO** for tool-calling.
  6. Final = **merge of multiple RL + DPO checkpoints**.
- Nemotron 3 family (Nano/Super/Ultra) hybrid is shipping in H1 2026; **Nemotron 4 unconfirmed**, targeted to Rubin hardware.

---

## 6. IBM — Granite 4.x series

### Granite 4.0 3B Vision — 2026-04-01
- VLM specialized for enterprise document data extraction ([MarkTechPost](https://www.marktechpost.com/2026/04/01/ibm-releases-granite-4-0-3b-vision-a-new-vision-language-model-for-enterprise-grade-document-data-extraction/)).
- Architecture inherits Granite 4.0 hybrid Mamba-2/Transformer (9:1 ratio), **NoPE positional encoding** (Mamba's sequential reading replaces RoPE), Apache-2.0 ([IBM Granite 4 announce](https://www.ibm.com/new/announcements/ibm-granite-4-0-hyper-efficient-high-performance-hybrid-models)).

### Granite 4.1 (3B / 8B / 30B dense + speech + Guardian) — 2026-Q2
- Dense decoder-only this time, 22T-token enterprise corpus, 128K validated context, full Apache-2.0 ([IBM Research](https://research.ibm.com/blog/granite-4-1-ai-foundation-models)).

### Granite 3.3 Code (carryover)
- 12T tokens, 12 natural langs, 116 programming langs.

---

## 7. Microsoft — Phi-4 reasoning family

### Phi-4-Reasoning-Vision-15B — 2026-03-04
- 15B params, multimodal, 200B multimodal training tokens ([Microsoft blog](https://www.microsoft.com/en-us/research/blog/phi-4-reasoning-vision-and-the-lessons-of-training-a-multimodal-reasoning-model/), [tech report PDF](https://www.microsoft.com/en-us/research/wp-content/uploads/2026/03/Phi-4-reasoning-vision-15B-Tech-Report.pdf)).
- "**Knows when to think**" — hybrid: extended CoT for math/sci, direct inference for perception ([VentureBeat](https://venturebeat.com/technology/microsoft-built-phi-4-reasoning-vision-15b-to-know-when-to-think-and-when)).
- **Training data lineage**: SFT on `~1.4M STEM+coding` Qs + reasoning demos generated by **o3-mini (teacher distillation)**, then short-RL stage for Phi-4-reasoning-plus.

### Phi-4-Mini — current quantized inference reference
- LoRA fine-tuning + RAG + tool-use guide published 2026-04-20 ([MarkTechPost](https://www.marktechpost.com/2026/04/20/a-coding-implementation-on-microsofts-phi-4-mini-for-quantized-inference-reasoning-tool-use-rag-and-lora-fine-tuning/)).

---

## 8. Google — Gemma 4 (open-weights, in-window)

### Gemma 4 — 2026-04-02
- **Four sizes**: E2B + E4B (Effective, MatFormer-style elastic), **26B MoE**, **31B Dense** ([Gemma 4 launch HF post](https://huggingface.co/blog/gemma4), [Google AI dev docs](https://ai.google.dev/gemma/docs/core)).
- First Gemma with MoE; multimodal native; new tokenizer for 140+ languages.
- Training: distillation from Gemini frontier + RL + model merge.

---

## 9. Hugging Face — SmolLM3-3B (carryover 2025-Q4 but defines current "small" baseline)

- **3B dense, 11.2T tokens**, 3-stage training, 128K context, 6-language native + Arabic/Chinese/Russian.
- Stage-1 (0-8T) mix: Web 85% (FineWeb-Edu+DCLM+FineWeb2-HQ) / Code 12% (StackV2 + StarCoder2 PRs + notebooks + GH issues + StackExchange) / Math 3% ([HF blog](https://huggingface.co/blog/smollm3)).
- **Fully open recipe**: weights + training data + curriculum schedule = ideal teacher for distillation experiments on Surrogate.

---

## 10. Z.ai (Zhipu) — GLM-4.7 (still SOTA open coding model)

- 358B MoE, 131K context, **#1 open-weight on LiveCodeBench (84.9) + SWE-bench Pro** ([Z.ai blog](https://z.ai/blog/glm-4.7)).
- Introduces "**Interleaved / Preserved / Turn-level Thinking**" controls — multi-modal CoT scheduling.

---

## 11. DeepSWE / SERA / Open-SWE — coding-agent RL recipes (datasets matter)

### DeepSWE-Preview — Together AI + Agentica
- **Pure-RL fine-tune of Qwen3-32B** — no SFT — using `rLLM` framework on **R2E-Gym training environments** ([Together AI blog](https://www.together.ai/blog/deepswe), [HF](https://huggingface.co/agentica-org/DeepSWE-Preview)).
- 4,500 real-world SWE tasks, 6 days × 64 H100 = ~9k H100-hrs.
- **`59% SWE-bench Verified` (test-time scaling) / `42.2% Pass@1`** — top open-weight at release.
- *Everything open*: dataset, code, eval logs, training recipe.

### SERA (Soft-verified Efficient Repository Agents)
- 32B model, **`54.2% SWE-bench Verified` in only 40 GPU-days** training ([Ai2 blog](https://allenai.org/blog/open-coding-agents)).
- **SERA datasets** ship in "general model-agnostic format with verification thresholds + metadata for filtering".

### Open-SWE — async programming agent (2026-03-20)
- Open-source asynchronous agent ([AIToolly](https://aitoolly.com/ai-news/article/2026-03-20-open-swe-a-new-open-source-agent-for-asynchronous-programming-challenges)).

---

## 12. Cohere — Aya Expanse (no Q2 update)

- Aya Expanse 8B + 32B remain the multilingual-23-language flagship; no 2026 Q2 release notes found. Last paper update 2025-07-31 ([HF page](https://huggingface.co/CohereLabs/aya-expanse-32b)).

---

## 13. 01.AI — Yi-Coder (no Q2 update)

- Yi-Coder 1.5B + 9B (2024-09) — strong on CrossCodeEval, 128K context. **No 2026 release detected.**

---

## 14. BigCode — StarCoder (no Q2 update)

- StarCoder 2 (2024-02) remains current; **no StarCoder 3 announcement** as of 2026-05-01.

---

## 15. Reka — Flash 3 (recent quantization push)

- 21B dense, 32K, RLOO-trained, 4-bit quantizes to 11GB ([Reka quantization blog](https://reka.ai/news/reka-quantization-technology)).

---

## Cross-Cutting Training Techniques That Shipped in OSS (Mar-Apr 2026)

| Technique | First Use | Status | Surrogate-relevance |
|-----------|-----------|--------|---------------------|
| **GRPO** (Group Relative Policy Optimization) | DeepSeek R1 (2025) | Default in Axolotl 0.8+, Unsloth, Llamafactory 0.9.4, TRL | **HIGH — mandatory** |
| **DAPO** (4-trick GRPO upgrade) | ByteDance Seed 2025-03 | Open-sourced ([repo](https://github.com/BytedTsinghua-SIA/DAPO)) on `verl`; 50% fewer steps to AIME-50 ([paper](https://arxiv.org/pdf/2503.14476)) | **HIGH — drop-in replacement for GRPO** |
| **RLVR** (verifiable rewards) | DeepSeekMath 2024 | Industry default; eliminates reward-model training. Cautions re: contamination ([Promptfoo](https://www.promptfoo.dev/blog/rlvr-explained/)) | **HIGH** — must use executable-verified rewards on code |
| **RPO** (Reward-aware PO) | NVIDIA Llama-Nemotron | Used in v1.5 chat alignment | Medium |
| **SimPO** (reference-free PO) | Princeton 2024 | Liger-Kernel + Anyscale support; modular DPO replacement ([Liger nightly](https://pypi.org/project/liger-kernel-nightly/)) | Medium |
| **ORPO** (odds-ratio PO) | KAIST 2024 | TRL + Liger | Medium |
| **mHC** (Manifold-Constrained Hyper-Connections) | DeepSeek 2026-Q1 | [Open impl](https://github.com/tokenbender/mHC-manifold-constrained-hyper-connections); not in mainline frameworks yet | **Low (architecture change, requires re-pretrain)** |
| **DeepSeek Sparse Attention (DSA)** | DeepSeek V3.2 / V4 | FlashMLA + DeepGEMM kernels open | Low (inference optimization, not training) |
| **FP8 Mixed-Precision Training** | DeepSeek V3 | Validated at 1.6T scale; supported in TransformerEngine, Unsloth | **Medium** if multi-node available |
| **Test-time Scaling (CePO-style)** | Cerebras 2025 | Llama 3.3-70B + CePO beats Llama-405B ([Cerebras blog](https://www.cerebras.ai/blog/cepo)) | **Medium — eval-time only, no retrain** |
| **Knowledge Distillation w/ Block Variants** | NVIDIA Nemotron | Multi-quality variants per block | Medium |
| **Hybrid Mamba-2/Transformer 9:1** | IBM Granite 4 | Open Apache-2.0; no positional encoding (NoPE) | Low (full re-architect) |
| **Synthetic data via teacher Q&A + executor validation** | Qwen2.5-Coder, Phi-4 | Industry standard: 70% code / 20% text / 10% math mix | **HIGH** |
| **YaRN static long-context scaling** | Qwen3 1M extension | All major frameworks | **HIGH** for 128K+ context |
| **Multi-token prediction (MTP)** | DeepSeek V3 | Built-in speculative head | Medium |
| **MLA (Multi-head Latent Attention)** | DeepSeek V2/V3 | -93% KV cache; LLaMA Factory + verl support | Medium-High for long-context inference |

---

## Benchmark Snapshot (open-weight only, April 2026)

| Benchmark | Top open-weight | Score | Source |
|-----------|-----------------|-------|--------|
| SWE-bench Verified (no scaffold) | Qwen3.6-27B (dense) | 77.2% | [buildfastwithai](https://www.buildfastwithai.com/blogs/qwen3-6-27b-review-2026) |
| SWE-bench Verified (w/ scaffold) | Devstral-2 123B | 72.2% | [VB](https://venturebeat.com/ai/mistral-launches-powerful-devstral-2-coding-model-including-open-source) |
| SWE-bench Verified (RL-only, 32B) | DeepSWE (Qwen3-32B + RL) | 59% (TTS) / 42.2% Pass@1 | [Together](https://www.together.ai/blog/deepswe) |
| SWE-bench Pro | GLM-4.7 / Qwen3.6-Max-Preview | #1 open / closed | [Scale leaderboard](https://labs.scale.com/leaderboard/swe_bench_pro_public) |
| LiveCodeBench | GLM-4.7 Thinking | 84.9 | [BenchLM](https://benchlm.ai/coding) |
| LiveCodeBench (closed) | Gemini 3 Pro Preview | 91.7 | same |
| BFCL v4 (function-calling) | Qwen3-Coder-Next + Hermes-style | top open | [BFCL](https://gorilla.cs.berkeley.edu/leaderboard.html) |
| AIME 2024 (RL-trained 32B) | DAPO-trained Qwen2.5-32B | 50 | [DAPO paper](https://arxiv.org/pdf/2503.14476) |

---

## Datasets Released or Refreshed in Window (worth ingesting into Surrogate)

| Dataset | Source | Notes |
|---------|--------|-------|
| **R2E-Gym** | Together AI / Agentica (DeepSWE) | 4,500 real SWE tasks, executable verification; pure-RL ready |
| **SERA training set** | Ai2 | "Model-agnostic format" with verification thresholds + metadata; designed for filtering |
| **DAPO RL set** | ByteDance Seed | Curated math+code prompts, ships with `verl` framework |
| **FineWeb / FineWeb-Edu / FineWeb2-HQ** | HF | Used by SmolLM3, Nemotron — 8T+ web tokens, edu-filtered |
| **Buzz-V1.2 + Dolma** | NVIDIA / Ai2 | Distillation source mix for Nemotron |
| **The Stack v2 + StarCoder2 PRs + GH issues + StackExchange** | BigCode | Code mix used by SmolLM3 |
| **Phi-4-reasoning SFT (~1.4M STEM+coding QAs distilled from o3-mini)** | Microsoft (gated) | Reasoning-trace teacher distillation pattern |
| **Qwen3 synthetic mix (textbooks/QAs/code via Qwen2.5-Math + Qwen2.5-Coder)** | Alibaba (recipe disclosed, data not) | Reproducible recipe for self-generation |
| **DeepPlanning** | Qwen-Agent 2026-01-27 | Agent eval benchmark, open |
| **R2E-Gym + rLLM framework** | Agentica | RL post-training infrastructure |

---

## Pull-Into-Surrogate Action Items

### (a) Datasets to mix into Surrogate trainer
1. **R2E-Gym SWE tasks** (4.5k, executable-verified) — primary RL dataset for Surrogate-Coder. Match DeepSWE recipe on Qwen2.5-Coder-32B base.
2. **SERA training set** with verification thresholds — use for SFT cold-start before RL.
3. **DAPO RL set** (ByteDance) — math + code prompts; reuse `verl` framework checkpoints as warm-starts.
4. **The Stack v2 + StarCoder2 PRs + GH issues + StackExchange** (SmolLM3 mix) — re-mid-train code on this with executor-validation filter.
5. **Synthetic Qwen3 recipe**: generate code-Q&A and textbook-style explanations using Qwen2.5-Coder-32B itself as teacher; 70% code / 20% text / 10% math ratio.
6. **DeepPlanning agent eval** — use as validation suite for tool-use, not training.
7. **Phi-4-style reasoning traces** — generate ~1M reasoning-trace QAs distilled from a frontier teacher (Qwen3.6-Max via API or DeepSeek-V4-Pro) for RLVR cold-start.

### (b) Training techniques to add (not currently in Surrogate)
1. **GRPO → DAPO upgrade** — switch to DAPO (Clip-Higher, Dynamic Sampling, token-level loss, overlong reward shaping) for 50% fewer training steps. Use ByteDance's `verl` repo as reference impl.
2. **RLVR with executor-validated rewards** for code (compile-pass, test-pass, type-check-pass as reward signal) — eliminate reward-model overhead.
3. **Iterative DPO checkpoint merging** — Nemotron-style merge of N RL+DPO checkpoints as final.
4. **Multi-stage pre-train curriculum**: Stage-1 general → Stage-2 STEM/code upweight → Stage-3 long-context (Qwen3 + SmolLM3 pattern).
5. **YaRN static long-context scaling** to extend Qwen2.5-Coder beyond 32K — validated to 1M with quality preservation.
6. **Synthetic code generation w/ executor validation** at scale (Qwen2.5-Coder pattern: 4-stage filter → +5pp on HumanEval/MBPP).
7. **Block-wise distillation w/ multiple quality variants** (Nemotron pattern) — only if multi-node available.
8. **Hybrid thinking/non-thinking modes** (Mistral Small 4 + Qwen3 + Phi-4-reasoning) — train a single model with reasoning-effort toggle.
9. **Liger Kernel** for memory-efficient SimPO/ORPO/DPO/GRPO (-80% memory).
10. **MLA + MTP** — only if doing custom architecture; otherwise skip until full re-pretrain budget.

### (c) Eval suites to incorporate
1. **SWE-bench Verified** (primary; with both no-scaffold and SWE-Agent scaffold variants) — target Qwen3.6-27B dense's 77.2% as ceiling for our 7B/14B/32B siblings.
2. **SWE-bench Pro** (Scale) — harder, more diverse repos.
3. **LiveCodeBench** — contamination-free, monthly refresh.
4. **HumanEval+ / MBPP+** (EvalPlus) — sanity checks; Qwen2.5-Coder ablations show +5pp from filtering = good signal channel.
5. **CrossCodeEval** — cross-file dependency reasoning (Yi-Coder showed value).
6. **BFCL v4** — agentic function-calling evaluation (multi-turn, parallel calls).
7. **CRUXEval / CRUX-O** — code reasoning.
8. **AIME 2024** — math reasoning generalization (DAPO target).
9. **DeepPlanning** (Qwen-Agent) — agent planning capability.
10. **MultiPL-E** — multilingual programming languages.

### (d) Frameworks / tooling upgrades
- **Axolotl ≥ 0.29** — full GRPO + reward modeling + sequence parallelism + Qwen3.6 native support.
- **Unsloth Feb 2026** — 12× faster MoE training, ultra-long-context RL, embedding model support.
- **LlamaFactory ≥ 0.9.4** — Megatron-LM integration, KTransformers backend, OFT, uv-based.
- **rLLM** (Agentica) — RL post-training framework matching DeepSWE recipe.
- **verl** — DAPO reference implementation.
- **Liger Kernel** — memory-efficient post-training kernels.

---

## Sources

- [Qwen3.6-27B blog](https://qwen.ai/blog?id=qwen3.6-27b) · [HF model card](https://huggingface.co/Qwen/Qwen3.6-27B) · [Qwen3 tech report](https://arxiv.org/pdf/2505.09388) · [Qwen3-Coder-Next](https://qwen.ai/blog?id=qwen3-coder-next) · [Qwen-Agent repo](https://github.com/QwenLM/Qwen-Agent)
- [DeepSeek V4 API news](https://api-docs.deepseek.com/news/news260424) · [Reuters/CNBC](https://www.cnbc.com/2026/04/24/deepseek-v4-llm-preview-open-source-ai-competition-china.html) · [Simon Willison](https://simonwillison.net/2026/Apr/24/deepseek-v4/) · [DeepSeek-V3 tech report](https://arxiv.org/abs/2412.19437) · [DeepSeek mHC](https://arxiv.org/abs/2512.24880) · [DSA / V3.2 paper](https://arxiv.org/abs/2512.02556)
- [Mistral Devstral-2 + Vibe CLI](https://mistral.ai/news/devstral-2-vibe-cli) · [Mistral Small 4](https://mistral.ai/news/mistral-small-4) · [Magistral arxiv](https://arxiv.org/html/2506.10910v1)
- [Meta Llama 4 launch](https://ai.meta.com/blog/llama-4-multimodal-intelligence/)
- [NVIDIA Llama-3.3-Nemotron-Super-49B-v1.5](https://huggingface.co/nvidia/Llama-3_3-Nemotron-Super-49B-v1_5) · [Nemotron 3 family](https://blogs.nvidia.com/blog/nemotron-3-nano-omni-multimodal-ai-agents/)
- [IBM Granite 4.0](https://www.ibm.com/new/announcements/ibm-granite-4-0-hyper-efficient-high-performance-hybrid-models) · [Granite 4.1](https://research.ibm.com/blog/granite-4-1-ai-foundation-models) · [Granite 4 Vision](https://www.marktechpost.com/2026/04/01/ibm-releases-granite-4-0-3b-vision-a-new-vision-language-model-for-enterprise-grade-document-data-extraction/)
- [Phi-4-reasoning-vision-15B](https://www.microsoft.com/en-us/research/blog/phi-4-reasoning-vision-and-the-lessons-of-training-a-multimodal-reasoning-model/) · [tech report](https://www.microsoft.com/en-us/research/wp-content/uploads/2026/03/Phi-4-reasoning-vision-15B-Tech-Report.pdf)
- [Gemma 4](https://huggingface.co/blog/gemma4)
- [SmolLM3 blog](https://huggingface.co/blog/smollm3)
- [GLM-4.7](https://z.ai/blog/glm-4.7)
- [DeepSWE](https://www.together.ai/blog/deepswe) · [DeepSWE HF](https://huggingface.co/agentica-org/DeepSWE-Preview) · [Open-SWE](https://aitoolly.com/ai-news/article/2026-03-20-open-swe-a-new-open-source-agent-for-asynchronous-programming-challenges) · [Ai2 SERA](https://allenai.org/blog/open-coding-agents)
- [DAPO paper](https://arxiv.org/pdf/2503.14476) · [DAPO repo](https://github.com/BytedTsinghua-SIA/DAPO) · [Post-training 2026 review](https://llm-stats.com/blog/research/post-training-techniques-2026)
- [Promptfoo RLVR explained](https://www.promptfoo.dev/blog/rlvr-explained/) · [RLVR book](https://rlvrbook.com/)
- [Liger Kernel](https://pypi.org/project/liger-kernel/) · [Axolotl/Unsloth/LF 2026 review](https://dev.to/ultraduneai/eval-003-fine-tuning-in-2026-axolotl-vs-unsloth-vs-trl-vs-llama-factory-2ohg)
- [BFCL leaderboard](https://gorilla.cs.berkeley.edu/leaderboard.html) · [LiveCodeBench](https://livecodebench.github.io/leaderboard.html) · [SWE-bench](https://www.swebench.com/) · [SWE-bench Pro](https://labs.scale.com/leaderboard/swe_bench_pro_public)
- [Cerebras CePO test-time compute](https://www.cerebras.ai/blog/cepo)

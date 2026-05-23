---
title: Surrogate-1 v2 — Architecture & Context Extension Research
date: 2026-04-29
session: surrogate1-honest-audit
purpose: Inform v2 base-model + context extension decisions
base_v1: Qwen2.5-Coder-7B-Instruct (32K native, 131K via YaRN)
training_v1: max_length=2048 (way under what base supports)
goal: increase effective context for repo-scale tasks, agent traces
tags: [research, llm, context-extension, code-llm, moe, yarn, longrope, flash-attention, surrogate1]
---

# Surrogate-1 v2 — Architecture & Long-Context Research

This document audits state-of-the-art context-extension techniques and code-LLM architectures (2024 H2 → 2026 Q2) and scores their applicability to **Surrogate-1 v2**.

Bottom-line picks are at the end. Sections 1–6 are evidence; section 7 is the recommendation.

---

## 0. Surrogate-1 v1 baseline

| Field | v1 |
|---|---|
| Base | `Qwen2.5-Coder-7B-Instruct` |
| Native ctx | 32,768 (RoPE base 1,000,000) |
| YaRN-extended ctx | 131,072 (factor 4.0) |
| Training `max_length` | **2,048** ← 16× under base capability |
| Trainer | TRL/HF SFT (no Axolotl, no Unsloth long-ctx) |
| Attention | FlashAttention 2 (sm_80 / sm_89) |
| Hardware | Lightning H200, Kaggle, Modal (mixed) |

Architecture limitation: training at 2K means the model never *sees* repo-scale dependency chains — it only memorizes function-local patterns. Extending the **trained** sequence length is the single biggest lever for v2.

---

## 1. Context-length extension techniques

### 1.1 RoPE (Rotary Position Embeddings) recap

Qwen2.5-Coder-7B uses RoPE with base θ = 1,000,000 (changed from 10,000 during repo-level pretraining — see Qwen2.5-Coder Technical Report §3). RoPE rotates query/key vectors by angles proportional to position, so position information is encoded multiplicatively rather than added.

For position m and dim 2i:
```
θ_i = base^(-2i/d_model)
f(x, m) = x * (cos(m·θ_i), sin(m·θ_i))
```

Extending context = changing how θ_i scales with m. There are five mainstream methods.

### 1.2 Position Interpolation (PI) — Chen et al. 2023

```
m' = m * (L_train / L_target)
```

Linearly compress positions into the trained range. Simple, no fine-tuning required for ~4× extension. Quality degrades past 4× because adjacent tokens become indistinguishable.

**Use for Surrogate-1: NO.** 4× from 32K = 128K which is fine, but PI is dominated by NTK-aware/YaRN at the same training cost.

### 1.3 NTK-aware interpolation — bloc97 2023

Instead of scaling positions, scale the RoPE *base*:
```
base_new = base * α^(d/(d-2))
```
This slows rotation speeds non-uniformly: high-frequency dimensions stay fine-grained (token-level), low-frequency dimensions stretch (long-range). No fine-tuning needed for 8× without catastrophic loss.

**Better than PI for non-fine-tuned use.** Worse than YaRN after fine-tuning. Surrogate-1 will fine-tune, so skip.

### 1.4 Dynamic NTK / NTK-by-parts

Dynamic NTK adapts the scaling factor at runtime based on actual sequence length — preserves short-context perplexity. NTK-by-parts goes further: applies different scaling to different frequency bands. High-freq dims (local) get linear; low-freq dims (global) get NTK; with a smooth transition zone.

This is the foundation YaRN builds on.

### 1.5 YaRN (Yet another RoPE eXtensioN) — Peng et al. 2023, ICLR 2024

**Current SOTA pre-LongRoPE.** Combines NTK-by-parts + attention temperature scaling.

Two components:

**(a) NTK-by-parts interpolation:**
```python
# Pseudocode for inv_freq computation
inv_freq_extrapolation = 1.0 / (base ** (arange(0, dim, 2) / dim))
inv_freq_interpolation = inv_freq_extrapolation / scale
mask = ramp(beta_0=1, beta_1=32, dim_freqs)  # smooth transition
inv_freq = inv_freq_interpolation * (1 - mask) + inv_freq_extrapolation * mask
```

**(b) Attention temperature scaling:**
```
softmax(q^T k / (t * sqrt(D))),  t = 0.1 * ln(s) + 1
```
Implemented as `cos`/`sin` scale by `sqrt(1/t)` so zero inference overhead.

Training cost: **~400 fine-tuning steps, 0.1% of pretraining tokens** to reach 128K.

This is what Qwen2.5-Coder uses for its 32K → 131K extension. Config:
```json
{
  "rope_scaling": {
    "factor": 4.0,
    "original_max_position_embeddings": 32768,
    "type": "yarn"
  }
}
```

**Surrogate-1 applicability: 9/10.** Battle-tested, mainline-supported in transformers ≥ 4.37, Axolotl, Unsloth, vLLM. Static YaRN (constant factor) is fine for training; dynamic YaRN requires runtime support that vLLM lacks but llama.cpp/exllamav2 have.

### 1.6 LongRoPE — Microsoft 2024 (ICML 2024)

`arxiv:2402.13753`. Pushes beyond YaRN to **2,048K (2M)** tokens, integrated into Phi-3.

Three innovations:
1. Identifies two forms of non-uniformity in positional interpolation; uses evolutionary search to find a better starting RoPE rescale before fine-tuning. Enables 8× extension *without fine-tuning*.
2. Progressive extension: fine-tune to 256K, then re-interpolate from 256K → 2M.
3. Re-adjustment on 8K-length data to recover short-context performance lost during long-context fine-tuning.

Cost: ~1K fine-tuning steps at 256K context length.

**Surrogate-1 applicability: 6/10.** Real win. But 2M tokens is overkill for repo-scale work (most monorepos fit in 200–500K). Adds complexity. The progressive extension (32K → 128K → 256K) is the borrowable idea.

### 1.7 LongRoPE2 — Microsoft 2025 (ICML 2025)

`arxiv:2502.20082`. Replaces both LongRoPE and YaRN as SOTA. Key claim: **near-lossless** short-context performance (98.5%+ retention) at 128K target.

Three contributions:
1. Hypothesis: insufficient training in *higher* RoPE dimensions causes OOD issues. Most prior methods over-correct higher dims.
2. **Needle-driven perplexity** evolutionary search — instead of optimizing for general PPL on a long doc, target retrieval-style tasks (the actual failure mode).
3. **Mixed-context-window training**: in the same fine-tuning run, use rescaled RoPE for long sequences and original RoPE for short sequences. Preserves short-context capability without separate re-adjustment.

Result: **128K LLaMA3-8B with 98.5% short-context retention, 10B fine-tuning tokens (80× less than Meta's official LLaMA-3.1 path)**.

**Surrogate-1 applicability: 8/10.** This is the right technique, but reference implementation (`microsoft/LongRoPE` repo) is research-quality. YaRN is in mainline transformers; LongRoPE2 requires custom RoPE init code. Reasonable to use YaRN now and migrate to LongRoPE2 in v2.5 once ecosystem catches up.

### 1.8 LongLoRA — Chen et al. 2023, ICLR 2024 oral

`arxiv:2309.12307`. The "do long-context fine-tuning cheaply" paper.

Two components:
1. **Shifted Sparse Attention (S²-Attn):** during training, split context into groups of size G, do dense attention within each group. Half the heads shift tokens by G/2 to allow cross-group info flow. Two lines of code change. **2.1× faster training, 1.8× less GPU memory at 8192 ctx**.
2. **LoRA + trainable embeddings + norms.** Standard LoRA misses these; including them is necessary for long-context.

LongLoRA can take Llama-2-7B from 4K → 100K on **a single 8×A100 node**. For 70B → 32K on the same node.

**Surrogate-1 applicability: 7/10.** S²-Attn is a *training-only* trick — at inference, the model uses normal full attention. Means we get long-context training cheaply without inference penalty. Drawback: S²-Attn training quality is slightly below full-attention training, especially at the boundary tokens. With Lightning H200 / Modal H100s, the cost savings vs. full attention may not justify the quality hit.

### 1.9 Comparison table

| Method | Max ext. | FT steps | Quality (PPL @ 128K) | Short-ctx retention | Mainline support | Surrogate-1 fit |
|---|---|---|---|---|---|---|
| PI | 4× | ~1K | Fair | Poor | Yes | No |
| NTK-aware (static) | 8× | None | Good | Fair | Yes | No |
| Dynamic NTK | 8× | None | Good | Good | Partial | No |
| **YaRN** | 32× | ~400 | Excellent | Good | Yes (HF, vLLM, Axolotl, Unsloth) | **YES** |
| LongRoPE | 64× → 2M | ~1K | Excellent | Fair | Microsoft repo only | Maybe |
| **LongRoPE2** | 32× | ~1K | SOTA (98.5%) | **Excellent** | Microsoft repo only | Yes (v2.5) |
| LongLoRA + S²-Attn | 16× | training-time | Good | Fair | Author repo, Axolotl partial | Cost-driven yes |

### 1.10 Training-time considerations

**Do you need full-attention training to extend context?**

Short answer: **Yes for inference quality**, **no if compute-constrained**.

- **Full attention all the way:** Best quality, O(n²) memory. With FlashAttn-3 + gradient checkpointing on H100, doable up to 128K on 8×H100.
- **S²-Attn (LongLoRA):** O(n·G) with G=8192. Faster, slightly worse quality at chunk boundaries. Inference still uses full attention.
- **Sequence/Context Parallelism (Axolotl, Megatron-LM):** Split sequence across GPUs with ring-attention pattern. Each GPU sees only a chunk, KV-caches pass around the ring. Combined with FSDP, scales context length almost linearly with GPU count. **Axolotl 0.8.0+ supports this via `context_parallel_size`.**

For Surrogate-1 v2 on Lightning H200 or Kaggle 2×T4/A100: context parallelism + gradient checkpointing is the right recipe.

**Memory budget for 7B model at various ctx (with FA2/FA3, GC on, BF16):**

| ctx | Activation mem | KV cache (1 seq) | Total (1×H100 80GB) |
|---|---|---|---|
| 4K | ~3 GB | ~0.5 GB | ~28 GB (model 14 + opt 8 + act 3 + grad 3) |
| 8K | ~6 GB | ~1 GB | ~34 GB |
| 16K | ~12 GB | ~2 GB | ~45 GB |
| 32K | ~24 GB | ~4 GB | ~62 GB |
| 64K | OOM single GPU | ~8 GB | needs 2× GPU + CP |
| 128K | OOM single GPU | ~16 GB | needs 4–8× GPU + CP |

(GQA reduces KV cache by ~7× vs MHA — the table above already assumes GQA which Qwen2.5-Coder uses with 28 Q heads / 4 KV heads.)

---

## 2. Repo-scale code understanding

### 2.1 Repository-level pretraining (the inflection point)

DeepSeek-Coder, StarCoder 2, Qwen2.5-Coder, CodeGemma all do **two-stage pretraining**:

1. **File-level pretrain.** Each file as one independent sample. Most code-LLM papers stop here. → 5.2T tokens for Qwen2.5-Coder.
2. **Repo-level pretrain.** Concatenate files in a repository in dependency order, train at 32K+ context. Forces the model to attend across files. Qwen2.5-Coder used **300B repo-level tokens at 32K, switching RoPE base from 10K → 1M during this stage**.

Key insight from Qwen2.5-Coder Technical Report §3.4: repo-level pretrain is what makes the model usable for repo-scale tasks. File-level alone produces a CodeBERT-tier model that hallucinates cross-file APIs.

### 2.2 RepoCoder — Zhang et al. EMNLP 2023

`arxiv:2303.12570`. Iterative retrieval + generation pipeline:

1. Start with the cursor location and top-k similar code chunks (BM25 or CodeT5 embedding).
2. Generate a partial completion.
3. Use the **partial completion** as the new query to retrieve again.
4. Repeat for n iterations.

Result: +10% on RepoBench across all language splits. The retrieval-as-feedback loop is what beats vanilla RAG.

For Surrogate-1: this is a **runtime pattern** for the agent, not a training-time pattern. Good to bake into the inference scaffold.

### 2.3 SWE-Bench architecture lessons (2024–2026)

Top-performing scaffolds on SWE-Bench Verified:

| System | Score | Architecture | Notes |
|---|---|---|---|
| Claude Mythos Preview | 93.9% | Anthropic internal | Lead as of 2026-04 |
| GPT-5.3 Codex | 85.0% | OpenAI | |
| Claude Opus 4.5 | 80.9% | Anthropic | |
| Warp Agent | 71.0% | Single-agent + `edit_files` tool for multi-file diff | |
| Qwen3-Coder-480B-A35B | 67.0% | OpenHands | Best open-source |
| Qwen3-Coder-30B-A3B | 51.6% | OpenHands (100 turns) | Open small-MoE leader |

Architectural pattern: **single agent**, **editor tool that can patch many files in one turn**, **persistent terminal**, **git-aware navigation**. The agent succeeds via tool-use, not via raw long-context. Most successful runs touch 2–3 files; SWE-Bench averages 2–3 file edits per fix.

**Implication for Surrogate-1:** raw context length matters less than the right tool API. But agentic traces themselves are long (50–200 turns × tool calls × file contents) — the *agent loop* needs the long context, not the user's question.

### 2.4 Cross-file dependency-aware tokenization

Lines of work:
- **CodePlan** (Bairi et al. ACM SE 2024): build an explicit dependency graph from imports / file-tree, traverse it during generation. Adds plan→edit→verify loop.
- **CatCoder** (`arxiv:2406.03283`): retrieve type signatures (not bodies) of imported symbols. Massive token efficiency.
- **DependEval** (ACL 2025): benchmark for cross-file dependency reasoning. Repository Construction + Dependency Recognition + Multi-file Editing.
- **MRG-Bench** (`arxiv:2508.02998`): evaluates how much repository context actually helps, finds 60% of cross-file failures aren't fixed by just adding the right file — model also needs to *recognize* the dependency.

For Surrogate-1: training data should include **dependency-graph-ordered** repo concatenation, not just lexical concatenation. This is Qwen2.5-Coder's repo-level pretrain pattern, refined.

### 2.5 CodeLlama-Long lesson

CodeLlama-7B-Long was fine-tuned on **16K-length sequences** (vs. 4K Llama-2 base) for **20B extra tokens** with RoPE scaling to 100K. Result: stable generation up to 100K despite training only at 16K.

Training-length ≠ inference-length when the position encoding generalizes. Training at 16K is a sweet spot if compute-constrained.

---

## 3. Architecture choices for code

### 3.1 Dense vs MoE — when does sparse pay off?

Two recent results:

- **`arxiv:2506.12119`** ("Can MoE Surpass Dense LLMs Under Strictly Equal Resources?", 2025): Yes, *if* backbones are optimized and activation rate is in the optimal range (~10–15% active parameters). Qwen3-Coder-30B-A3B sits in this range (3.3B/30.5B = 10.8%).
- **DeepSeekMoE** (`arxiv:2401.06066`): fine-grained expert segmentation (more, smaller experts) + shared experts beats coarse-grained MoE. DeepSeek-V3 uses 256 routed experts + 8 active per token. DeepSeek-Coder-V2-Lite: 64 experts, 6 active per token.

For code specifically:
- **Code is bursty / multi-modal:** different domains (web vs systems vs DSP), different languages, different paradigms. MoE gives experts a chance to specialize.
- **Inference cost matters less than total params for VRAM:** A 30B-A3B model needs 30B params in VRAM but only does 3.3B compute per token. On a 24GB MacBook M3, this is *worse* than a 14B dense (more VRAM use). On an H100, it's *better* (compute-bound, not memory-bound).

### 3.2 Attention variants

| Variant | Used by | Quality | KV cache | Throughput |
|---|---|---|---|---|
| MHA | original Transformer | Best | 1.0× (baseline) | 1.0× |
| MQA | Falcon, PaLM | Worst | 1/h × | ~6× |
| **GQA** | Qwen2.5, Llama-3, Mistral | Near-MHA | 1/g × (typical g=4–8) | ~5× |
| **MLA** | DeepSeek-V2/V3 | MHA-equivalent | **1/32 ×** | ~20× via compression |
| Sliding window | Mistral 7B v0.1, Gemma 2 | Good (local) | bounded by W | linear |

**Multi-head Latent Attention (MLA)**: Compress K, V into a low-rank latent before caching, decompress on use. DeepSeek-V3 reports 32× compression and 20× speedup vs. MHA.

For Surrogate-1 v2: Qwen2.5-Coder GQA (28 Q / 4 KV) is already strong. MLA would require switching base to DeepSeek family. Worth it if v2 hits memory limits at 128K context.

### 3.3 Sliding window attention — Mistral lesson

Mistral 7B v0.1 used sliding window of 4096 with 32 layers, theoretically attending up to 4096 × 32 = 131K via stacked attention. **In practice, info beyond the immediate window degrades fast.** Mistral 7B v0.2 dropped sliding window in favor of full 32K context.

Lesson: full attention beats sliding window for code tasks where remote dependencies are common (function defs at top of file, imports far away, tests in different file).

### 3.4 FlashAttention 3 (FA3) — Tri Dao et al. 2024

`arxiv:2407.08608`. Hopper-only (sm_90 = H100/H200/H800). Three innovations:

1. Asynchronous warp specialization (overlap compute and TMA data movement).
2. Interleaved blockwise matmul/softmax.
3. Block-quantized FP8 with incoherent processing.

Results vs FA2:
- FP16 forward: **1.5–2.0× faster (740 TFLOPS, 75% of H100 peak vs 35% for FA2)**
- BF16 backward: 1.5–1.75× faster
- FP8 forward: ~1.2 PFLOPS, 2.6× smaller error than baseline FP8 attention

Caveat: as of 2026-04, FA3 is **not in mainline HuggingFace transformers**. Direct integration via `flash_attn_interface`. Some forks (vLLM-flash-attention) have it. LLaMA-Factory does not yet.

For Surrogate-1: if training on H100/H200, install FA3 manually and patch the model's attention forward. ~2× wall-clock training speed.

### 3.5 Sage Attention 2

`thu-ml/SageAttention`. ICLR 2025. Quantizes attention to INT8 (Q, K) with FP16 for V. **2.1–2.7× faster than FA2, ~matches FA3 speed but better accuracy than FA3-FP8**. Plug-and-play, supports H100/A100/RTX 4090.

Use case: inference acceleration for long-context. Less useful at training time.

### 3.6 Linear-time architectures (RWKV-7, Mamba)

- **RWKV-7 "Goose"**: matches Qwen2.5 English perf with 1/3 the training tokens; constant-memory inference; "infinite" context but **passkey retrieval degrades past 28K**. For code specifically: limited public benchmark data.
- **Codestral Mamba 7B**: 75.0% HumanEval, 68.5% MBPP. Solid but ~10pts below Qwen2.5-Coder-7B (88.4%). Mamba's selective state-space struggles with long-range exact recall, which code needs (function signatures, type defs).

Verdict: **not ready for code.** Linear architectures win on speech, vision, and casual chat where exact recall matters less. Stick with attention for code in 2026.

---

## 4. Better base models to consider

Side-by-side scoreboard for models in Surrogate-1's potential range:

| Model | Params | Active | Native ctx | YaRN max | HumanEval | HumanEval+ | MBPP | MBPP+ | LiveCodeBench | SWE-Bench V | License | Surrogate-1 fit |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Qwen2.5-Coder-7B-Instruct (v1)** | 7.6B | 7.6B | 32K | 131K | **88.4** | 84.1 | 83.5 | — | — | — | Apache-2 | ◯ baseline |
| Qwen2.5-Coder-14B-Instruct | 14.7B | 14.7B | 32K | 131K | ~89 | — | ~85 | — | — | — | Apache-2 | medium |
| Qwen2.5-Coder-32B-Instruct | 32.5B | 32.5B | 32K | 131K | **92.7** | — | **90.2** | — | — | — | Apache-2 | too big locally |
| Qwen2.5-7B-Instruct-1M | 7B | 7B | 256K | **1M** | — | — | — | — | — | — | Apache-2 | **strong long-ctx** |
| Qwen2.5-14B-Instruct-1M | 14B | 14B | 256K | **1M** | — | — | — | — | — | — | Apache-2 | strong if 14B fits |
| **Qwen3-Coder-30B-A3B-Instruct** | 30.5B | **3.3B** | 256K | **1M** | — | — | — | — | — | **51.6** | Apache-2 | **★★★** |
| Qwen3-Coder-480B-A35B | 480B | 35B | 256K | 1M | — | — | — | — | — | **~60–67** | Apache-2 | too big |
| DeepSeek-Coder-V2-Lite-Instruct | 16B | **2.4B** | **128K** | — | 81.1 | — | — | — | — | — | DeepSeek-Coder | strong if MoE |
| DeepSeek-Coder-V2-Instruct | 236B | 21B | 128K | — | 90.2 | — | — | — | — | — | DeepSeek-Coder | too big |
| OpenCoder-8B-Instruct | 7.8B | 7.8B | 8K | — | 83.5 | 78.7 | 79.1 | 69.0 | — | — | Apache-2 (but ctx weak) | weak ctx |
| Codestral-22B-v0.1 | 22B | 22B | 32K | — | 81.1 | — | 78.2 | — | — | — | MNPL (non-comm) | license blocker |
| Codestral Mamba 7B | 7B | 7B | 256K | — | 75.0 | — | 68.5 | — | — | — | Apache-2 | weak quality |
| CodeLlama-70B-Instruct | 70B | 70B | 16K | 100K | 67.8 | — | — | — | — | — | Llama 2 | obsolete |
| Llama-3.3-70B-Instruct | 70B | 70B | 128K | — | 88.4 | — | 87.6 | — | — | — | Llama 3 | not code-spec. |
| Phi-4 | 14B | 14B | 16K | — | **82.6** | — | — | — | — | — | MIT | tiny ctx |
| Gemma-2-27B | 27B | 27B | 8K | — | 51.8 | — | — | — | — | — | Gemma | weak code |

Notes:
- **HumanEval scores marked `—` are missing from search results, not necessarily lower.**
- "fit" rating is for Surrogate-1 v2 specifically (~7B–30B sweet spot, Apache-2 compatible, strong at code).
- License: `MNPL` = Mistral Non-Commercial; not usable for any deploy revenue.

### 4.1 Top three contenders

1. **Qwen3-Coder-30B-A3B-Instruct** (Apache-2). 30.5B total / 3.3B active → on H200 same memory footprint as a 30B dense, but ~10× faster compute. Native 256K. SWE-Bench 51.6%. **Best if hardware can fit 30B.**
2. **Qwen2.5-Coder-7B + YaRN to 131K** (current path). Lowest risk, mainline support, well-known quirks. **Best if compute-constrained.**
3. **Qwen2.5-7B-Instruct-1M** (not coder-specific but with 1M context). Apache-2. Best if v2's bottleneck is *long agent traces* more than code-specific perf.

---

## 5. Mixture of Experts deep-dive

### 5.1 Why MoE for code

Code generation has inherent task heterogeneity:
- different programming languages (Python ≠ Rust ≠ TS)
- different domains (web framework ≠ systems ≠ ML)
- different paradigms (functional ≠ OOP ≠ procedural)
- different goals (write new ≠ fix bug ≠ refactor ≠ explain)

Each of these can benefit from a specialized expert. DeepSeekMoE's fine-grained segmentation paper (`arxiv:2401.06066`) explicitly motivated by this — claims expert specialization improves with finer granularity.

### 5.2 Qwen3-Coder-30B-A3B internals

From HF model card:
- 30.5B total, 3.3B active per token
- 48 transformer layers
- 128 experts, 8 active per token (top-8 routing)
- GQA: 32 query heads / 4 KV heads
- 256K native context, 1M via YaRN

Sparsity ratio 10.8% — exactly in the "MoE wins" sweet spot from the 2025 dense-vs-MoE paper.

### 5.3 DeepSeek-V3 internals

For comparison (DeepSeek-V3, not coder variant):
- 671B total, 37B active
- 256 experts + 1 shared, top-8 routing
- MLA attention (32× KV cache compression)
- Auxiliary-loss-free load balancing
- 128K context

DeepSeek-Coder-V2-Lite (relevant to Surrogate-1):
- 16B total, 2.4B active
- 64 experts, 6 active
- 128K context
- 81.1% HumanEval (Python)

### 5.4 Train-time vs inference-time MoE

- **Inference**: MoE wins on throughput/quality at high VRAM.
- **Training**: MoE is *harder* than dense. Auxiliary load-balancing loss, expert dropout, capacity factors, all-to-all communication. Most fine-tuners (Axolotl, Unsloth, LLaMA-Factory) support MoE LoRA but have edge cases.
  - Axolotl 0.8+: supports Qwen3 MoE LoRA out of the box.
  - Unsloth: documented but with notes ("Qwen3 - How to Run & Fine-tune").
  - LLaMA-Factory: supports Qwen3-Coder-30B-A3B as of v0.9.

For Surrogate-1 v2, MoE fine-tuning is feasible on H200 80GB. It's not feasible on Kaggle 2×T4 16GB.

### 5.5 When MoE doesn't pay off

- Small VRAM budget (<24GB) — dense 7B fits, MoE 30B doesn't.
- Single-domain task — no specialization gain.
- Need fastest inference latency for a single request — MoE has higher per-token latency for batch=1 due to routing overhead.
- Distillation use case — distilling MoE to dense is harder than dense-to-dense.

---

## 6. Modern attention efficiency (2024–2026)

### 6.1 Speedup landscape on H100

| Implementation | Forward speed | Backward speed | FP8? | Mainline ready |
|---|---|---|---|---|
| Vanilla SDPA (PyTorch) | 1.0× | 1.0× | No | Yes |
| FlashAttention 2 | 3–4× over vanilla | 3× over vanilla | No | Yes (transformers, vLLM) |
| **FlashAttention 3** | **1.5–2× over FA2** | **1.5–1.75× over FA2** | **Yes** | Hopper only, not in HF mainline |
| SageAttention 2 | 2.1–2.7× over FA2 | 2× over FA2 | INT8 | Plugin install |
| xFormers | ~FA2 | ~FA2 | No | Yes |

### 6.2 Recommendation for Surrogate-1 v2 training

If on H100/H200:
- Install FA3 from `Dao-AILab/flash-attention/hopper`
- Monkey-patch Qwen attention to call `flash_attn_interface.flash_attn_func`
- Expected: ~1.7× over FA2 baseline

If on A100/V100/T4:
- FA2 (already in mainline). Done.

### 6.3 Linear attention status (2026 Q2)

RWKV-7, Mamba-2, Hyena, RetNet — all interesting research, none are production-grade for code. Stick with FlashAttention-family for code-LLM training in 2026.

---

## 7. Concrete recipes for Surrogate-1 v2

### 7.1 Recipe A — Conservative (Qwen2.5-Coder-7B + YaRN-32K-train)

**Goal:** maximize bang per training-hour, minimize risk. Stay on familiar ground.

**Config:**
```yaml
# Axolotl 0.8+ config
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
sequence_len: 32768           # 16× v1's 2048
sample_packing: true
pad_to_sequence_len: true
flash_attention: true         # FA2 or FA3 if H200

# YaRN: not needed for 32K training (native), needed only for 131K inference
# Activate at deploy time via config.json patch

context_parallel_size: 2      # if multi-GPU, split sequence across 2 GPUs

# Memory
gradient_checkpointing: unsloth  # or "true" if not using Unsloth
load_in_4bit: false              # full BF16 for quality
bf16: true

# LoRA
adapter: qlora                # if VRAM-tight, else "lora" or "fft" for full
lora_r: 64
lora_alpha: 128
lora_dropout: 0.05
lora_target_modules:
  - q_proj
  - k_proj
  - v_proj
  - o_proj
  - gate_proj
  - up_proj
  - down_proj

# Repo-aware data
datasets:
  - path: <surrogate1-dataset>
    type: chat_template
    chat_template: qwen2-5-coder
    field_messages: messages
```

**Hardware:** 1× H200 80GB OK (LoRA), 4× H100 80GB for full-FT.
**Training time estimate:** ~24h / 8B tokens at 32K on H200.
**Quality lift over v1 (max_len=2048):** large, because v1 never saw 32K samples.

### 7.2 Recipe B — Ambitious (Qwen3-Coder-30B-A3B MoE + YaRN-128K-train)

**Goal:** state-of-the-art repo-scale code LLM in 2026. Accept higher cost.

**Config:**
```yaml
base_model: Qwen/Qwen3-Coder-30B-A3B-Instruct
sequence_len: 131072           # 4× the model's 32K original was extended to;
                               # but base is 256K so we have headroom
sample_packing: true
flash_attention: true          # FA3 strongly recommended

# YaRN already in base model config — re-enable for 1M if needed
rope_scaling:
  type: yarn
  factor: 4.0                  # 256K → 1M
  original_max_position_embeddings: 262144

# Multi-GPU strategy
context_parallel_size: 4       # 4-way ring attention for sequence parallelism
fsdp:
  fsdp_sharding_strategy: FULL_SHARD
  fsdp_offload_params: false

gradient_checkpointing: true
bf16: true

# MoE-specific
moe:
  router_aux_loss_coef: 0.001
  num_experts_per_tok: 8
  expert_dropout: 0.0          # off for fine-tune

# LoRA on attention only (don't LoRA the experts — they're sparse already)
adapter: lora
lora_target_modules:
  - q_proj
  - k_proj
  - v_proj
  - o_proj
```

**Hardware:** 8× H200 141GB (for full-FT), 4× H100 80GB (for LoRA).
**Training time estimate:** ~72h / 8B tokens at 128K on 8×H200.
**Quality lift:** unknown — depends if Surrogate-1 dataset is rich enough to leverage MoE specialization. If dataset is < 1B tokens, MoE may not differentiate from dense.

### 7.3 Recipe C — Hybrid (Qwen2.5-Coder-7B + LongRoPE2 path to 256K)

**Goal:** punch above weight class on long context. Match Qwen2.5-7B-Instruct-1M's reach using code base.

Steps:
1. Take Qwen2.5-Coder-7B-Instruct.
2. Apply Microsoft LongRoPE2 evolutionary RoPE search (one-time, CPU job ~6h).
3. Fine-tune at 32K mixed with 128K samples (LongRoPE2 mixed-context training).
4. Re-validate short-ctx benchmarks (HumanEval, MBPP) — should retain 95%+.

**Risk:** LongRoPE2 reference impl is research-grade. Expect to write ~500 lines of plumbing.

**Reward:** 256K effective context with quality near v1 short-context perf. No MoE complexity.

---

## 8. Bottom-line picks (200-word summary follows in main response)

Recipe ranking for Surrogate-1 v2:

1. **Recipe A (Qwen2.5-Coder-7B + YaRN + train at 32K)** — biggest ROI, lowest risk. Just lifting `max_length` from 2048 → 32768 with proper packing/CP gives a step-change improvement. **Do this first.**
2. **Recipe B (Qwen3-Coder-30B-A3B MoE)** — best ceiling, needs 8×H100/H200. Do this once Recipe A is shipped and we know the dataset can support it.
3. **Recipe C (LongRoPE2)** — research bet. Defer to v2.5 unless team has a contributor with RoPE math chops.

Skip:
- Anything Mistral (license).
- CodeLlama 70B (obsolete).
- Phi-4 (16K context too small).
- Gemma 2 27B (weak code, 8K context).
- RWKV-7 / Mamba (not ready for code).

---

## 9. Implementation TODOs for v2

- [ ] Update Axolotl config to `sequence_len: 32768` (Recipe A immediate win)
- [ ] Enable `sample_packing: true` and `pad_to_sequence_len: true`
- [ ] Switch from FA2 → FA3 if training on H100/H200 (manual install + patch)
- [ ] Add `context_parallel_size` if using ≥2 GPUs
- [ ] Validate by checking activation memory stays under VRAM during a 128-step dry-run
- [ ] Re-tokenize dataset with repo-level concatenation (not just file-level shuffling)
- [ ] Eval suite: HumanEval+, MBPP+, RepoBench, SWE-Bench Lite, internal long-context retrieval
- [ ] Record short-ctx eval before/after long-ctx training to detect regression

---

## 10. Sources

### Context extension
- [YaRN: Efficient Context Window Extension of LLMs](https://arxiv.org/abs/2309.00071) — Peng et al., 2023, ICLR 2024
- [LongRoPE: Extending LLM Context Beyond 2M Tokens](https://arxiv.org/abs/2402.13753) — Microsoft, ICML 2024
- [LongRoPE2: Near-Lossless LLM Context Window Scaling](https://arxiv.org/abs/2502.20082) — Microsoft, ICML 2025
- [LongLoRA: Efficient Fine-tuning of Long-Context LLMs](https://arxiv.org/abs/2309.12307) — Chen et al., ICLR 2024 oral
- [How LLMs Scaled from 512 to 2M Context](https://amaarora.github.io/posts/2025-09-21-rope-context-extension.html) — technical deep-dive
- [Extending the RoPE — EleutherAI Blog](https://blog.eleuther.ai/yarn/)
- [Qwen2.5-Coder-7B-Instruct HF model card](https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct)

### Repo-scale code
- [RepoCoder: Repository-Level Code Completion via Iterative Retrieval](https://arxiv.org/abs/2303.12570) — EMNLP 2023
- [Qwen2.5-Coder Technical Report](https://arxiv.org/pdf/2409.12186)
- [DeepSeek-Coder-V2 Technical Report](https://arxiv.org/abs/2406.11931)
- [On Pretraining for Project-Level Code Completion](https://arxiv.org/html/2510.13697)
- [SWE-Bench Verified Leaderboard](https://www.swebench.com/verified.html)
- [DependEval: Benchmarking LLMs for Repository-Level Dependency Reasoning](https://aclanthology.org/2025.findings-acl.373.pdf)

### Architecture
- [Qwen2.5-Coder Series Blog](https://qwenlm.github.io/blog/qwen2.5-coder-family/)
- [Qwen3-Coder Blog](https://qwenlm.github.io/blog/qwen3-coder/)
- [Qwen3-Coder-30B-A3B-Instruct HF](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct)
- [DeepSeekMoE Paper](https://arxiv.org/abs/2401.06066)
- [DeepSeek-V3 Technical Report](https://arxiv.org/abs/2412.19437)
- [DeepSeek-V3 Multi-head Latent Attention Explained](https://towardsdatascience.com/deepseek-v3-explained-1-multi-head-latent-attention-ed6bee2a67c4/)
- [Qwen2.5-1M Blog (1M context)](https://qwenlm.github.io/blog/qwen2.5-1m/)

### Attention efficiency
- [FlashAttention-3 Paper](https://arxiv.org/abs/2407.08608) — Dao et al., NeurIPS 2024
- [FlashAttention-3 PyTorch Blog](https://pytorch.org/blog/flashattention-3/)
- [SageAttention GitHub](https://github.com/thu-ml/SageAttention)
- [Mistral 7B Sliding Window Architecture](https://mbrenndoerfer.com/writing/mistral-architecture-sliding-window-attention)
- [GQA: Training Generalized Multi-Query Transformer](https://www.ibm.com/think/topics/grouped-query-attention)

### Tools
- [Axolotl: Sequence Parallelism for Long Context](https://huggingface.co/blog/axolotl-ai-co/long-context-with-sequence-parallelism-in-axolotl)
- [Axolotl ND-Parallelism Docs](https://docs.axolotl.ai/docs/nd_parallelism.html)
- [Unsloth Long-Context Gradient Checkpointing](https://unsloth.ai/blog/long-context)
- [Unsloth Qwen2.5-Coder Fine-tune Blog](https://unsloth.ai/blog/qwen-coder)

### Benchmarks
- [Codestral-22B HF](https://huggingface.co/mistralai/Codestral-22B-v0.1)
- [OpenCoder-8B-Instruct HF](https://huggingface.co/infly/OpenCoder-8B-Instruct)
- [Phi-4 Technical Report](https://www.microsoft.com/en-us/research/wp-content/uploads/2024/12/P4TechReport.pdf)
- [Llama 3.3 70B benchmarks](https://huggingface.co/datasets/meta-llama/Llama-3.3-70B-Instruct-evals)
- [EvalPlus Leaderboard](https://evalplus.github.io/leaderboard.html)

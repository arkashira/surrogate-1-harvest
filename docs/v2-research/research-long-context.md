---
title: SOTA Long-Context Techniques for Code LLMs (2025-2026)
date: 2026-04-29
project: Surrogate-1
context: post v1 honest audit; planning v2 with extended effective context
target_model: Qwen2.5-Coder-7B + LoRA
goal: 32K -> 128K -> 1M effective context with strong middle-recall
tags: [long-context, rope, yarn, dca, ruler, code-llm, qwen, kimi, surrogate1]
---

# SOTA Long-Context Techniques for Code LLMs (2025-2026)

> Frontier capability bar in 2026: Kimi K2.6 (~262K), Qwen3-2507 (1M via DCA), Gemini 2.5 Pro (1M -> 2M), GPT-5.5 (~1M), Claude Sonnet 4.5 (200K, 1M beta), Qwen2.5-1M (1M open weights). The open-source SOTA recipe for 1M context = pretraining + progressive YaRN/ABF + DCA at inference + MInference sparse + chunked prefill.

## TL;DR for Surrogate-1 v2

| Decision | Recommendation | Why |
|---|---|---|
| **Native train length** | 32K | Free with Qwen2.5-Coder-7B native window. Fits 1x L40S 48GB with LoRA + sample packing. |
| **RoPE** | YaRN, factor=4.0, original_max_position_embeddings=32768 | Official Qwen recipe. Inference-time, no retrain to reach 128K. |
| **Inference extrapolation** | Add DCA (Dual Chunk Attention) via vLLM `VLLM_ATTENTION_BACKEND=DUAL_CHUNK_FLASH_ATTN` | Free 4x context multiplier post-32K training. |
| **Effective target** | 128K trained, 256K-1M via DCA at inference | Matches Qwen2.5-1M; realistic for L40S budget. |
| **RULER target (128K)** | >= 80 | Qwen2.5-7B-1M scores 84.7 at 128K (RULER); LongRoPE2 LLaMA3-8B = 82.0. |
| **Lost-in-middle** | NExtLong-style hard-negative synthesis + COLA repo packing | Cheapest middle-recall fix; no architecture change. |
| **Attention sinks** | StreamingLLM attention sink for inference if KV pressure | Not needed if DCA + chunked prefill in place. |

---

## 1. Frontier Long-Context Models 2025-2026

### 1.1 Kimi K2 / K2.5 / K2.6 / Thinking (Moonshot)

- **Architecture**: 1.04T param MoE, 32B activated, 384 experts (vs DeepSeek-V3's 256), 8 active per token. Multi-head Latent Attention (MLA) (paper: arxiv 2507.20534).
- **Context**:
  - K2 base/Instruct: 128K
  - K2.5: 256K
  - K2 Thinking: 256K
  - K2.6: 262.1K (per OpenRouter listing; Cloudflare confirms)
- **NOT 2M** despite the user's prompt assumption. Common confusion - Kimi App has higher context for paid tiers but the model itself caps at ~256K.
- **Long context training**: Standard YaRN at 4K -> 32K -> 128K progression. MuonClip optimizer with QK-Clip prevents attention logit explosion at trillion-scale.
- **Pre-training data**: 15.5T tokens. 10T at 4K, 5.5T with LR decay, then long-context activation phase.
- **Implication for Surrogate-1**: Kimi K2.6 is **NOT** the right north star for context length. The right north star for **open-weight code LLM at <1M** is **Qwen2.5-1M / Qwen3-2507** which actually delivers DCA-extended 1M context with public training recipe.

### 1.2 Qwen2.5-1M (Alibaba, Jan 2025)

Paper: arxiv 2501.15383. **THE most relevant reference** for Surrogate-1.

Five-stage progressive training:

| Stage | Context Length | RoPE base (theta) | Notes |
|---|---|---|---|
| 1 | 4,096 | 10,000 | Initial pre-training |
| 2 | 32,768 | ~1,000,000 | Adaptive Base Frequency (ABF) kicks in |
| 3 | 65,536 | 1,000,000 | Continual pre-training |
| 4 | 131,072 | 5,000,000 | Continual pre-training |
| 5 | 262,144 | 10,000,000 | Final pre-training context |

Each stage: 40% sequences at current max, 60% shorter sequences (mixed-length batching).

**Length extrapolation at inference**: DCA (Dual Chunk Attention) extends 256K trained -> 1M effective. Even models trained only at 32K achieve near-perfect passkey retrieval at 1M with DCA applied.

**Sparse attention**: MInference at inference. Reduces 30-min/prompt prefill at 1M to ~3 min on A100. With chunked prefill (32,768-token chunks), activation VRAM cut by **96.7%**.

**Data synthesis** (post-training):
- Take long documents -> prompt Qwen2.5 to generate queries on extracted segments
- Tasks: summarization, info retrieval, multi-hop QA, reasoning, coding
- Qwen-Agent generates high-quality answers via RAG, chunk-by-chunk reading, step-by-step reasoning

**RULER scores** (from Qwen blog visualization):
- Qwen2.5-7B-Instruct-1M @ 128K: ~84.7
- Qwen2.5-7B-Instruct-1M @ 1M: ~58 (Passkey 100%; harder tasks degrade)

**Hardware to RUN inference**:
- 7B-1M @ 1M context: **120GB VRAM** total (1-4 GPUs)
- 14B-1M @ 1M context: **320GB VRAM** total (1-8 GPUs)

### 1.3 Qwen3-2507 (Aug 2025)

- Qwen3-30B-A3B-2507 and Qwen3-235B-A22B-2507 hit **1M tokens** via DCA + MInference
- Up to **3x faster** at 1M sequences vs vanilla
- 30B: 240GB VRAM @ 1M; 235B: 1000GB VRAM @ 1M
- vLLM + SGLang both have first-class DCA support

### 1.4 DeepSeek-V3 / V3.2

- 4K -> 32K -> 128K progressive YaRN extension
- Two phases x 1000 steps each
- Phase 1: seq=32K, batch=1920
- Phase 2: seq=128K, batch=480
- Total: ~119K extra GPU hours - "cheap" relative to massive capability gain
- V3.2-Exp: introduces sparse attention for further long-context efficiency

### 1.5 Llama 3 / 3.1 / 3.3 (Meta)

- 405B and 70B and 8B all support 128K
- Paper: arxiv 2407.21783
- Uses 4D parallelism: FSDP + TP + PP + **Context Parallelism (CP)**
- Long-context phase: each GPU rank gets 8K sequence chunk; CP coordinates
- Llama 3.1 uses YaRN to reach 128K but **drops beyond 64K** on RULER (key driver behind LongRoPE2 paper)

### 1.6 Gemini 2.5 Pro

- 1M context, expanding to 2M
- 100% recall up to 530K, 99.7% recall at 1M (Google's own number)
- Architecture: MoE; integrated tool-use; deep reasoning chain
- Paper: arxiv 2507.06261
- Key infrastructure: context caching API for cost amortization

### 1.7 GPT-5.5 / GPT-5.4 (OpenAI)

- GPT-5.5: ~1.05M token window, 128K max output
- GPT-5.4: 1M input via API
- MRCR v2 at 512K-1M: GPT-5.5 = 74.0% (vs GPT-5.4 36.6% - 37 point jump)
- $0.50 per 1M cached input tokens (heavily incentivizes prompt caching)

### 1.8 Claude Sonnet 4.5

- 200K standard, 1M public beta
- 1M MRCR = **18.5%** - effectively unusable at 1M, despite the listed cap
- 200K SWE-Bench Verified = 77.2% on 500 problems
- Hard lesson: a "supported" context length != effective context length

### 1.9 Frontier Comparison Table

| Model | Listed Window | Effective (RULER ~80%) | Open weights | Technique stack |
|---|---|---|---|---|
| Kimi K2.6 | 262K | ~128K-256K | Yes (1T MoE) | YaRN + MLA + Muon |
| Qwen2.5-7B-1M | 1M | ~256K-512K | Yes (7B) | ABF + 5-stage + DCA + MInference |
| Qwen3-30B-A3B-2507 | 1M | 256K-1M | Yes (30B) | DCA + MInference + chunked prefill |
| DeepSeek-V3 | 128K | 128K | Yes (671B) | YaRN progressive (4K -> 32K -> 128K) |
| Llama 3.1 405B | 128K | ~64K (drops) | Yes | YaRN + 4D parallel CP |
| Gemini 2.5 Pro | 2M | ~1M | No | MoE + (proprietary) |
| GPT-5.5 | 1M | ~512K-1M (MRCR 74%) | No | Caching API |
| Sonnet 4.5 | 1M | ~200K (1M MRCR 18.5%) | No | (proprietary) |
| LongRoPE2 LLaMA3-8B | 128K | 128K (RULER 82.0) | Recipe only | Evolutionary search + mixed CW |

**Key takeaway for Surrogate-1**: target Qwen2.5-1M's open recipe. Listed and effective context match closely.

---

## 2. RoPE / Position Embedding Scaling

### 2.1 Methods Comparison

| Method | Year | Train-free? | Best for | Failure mode |
|---|---|---|---|---|
| Position Interpolation (PI) | Meta 2023 | Yes (with finetune) | Up to 4x | Adjacent tokens become indistinguishable |
| NTK-aware | Reddit 2023 | Yes | 2-4x | Ad-hoc base scaling |
| NTK-by-parts | bloc97 2023 | Yes | 4-8x | Better than PI |
| Dynamic NTK | 2023 | Yes | 4-8x | Per-step adaptive scaling |
| YaRN | arxiv 2309.00071 | Yes (with finetune) | 4-8x with finetune | OOD beyond 8x |
| LongRoPE | Microsoft 2024 | Finetune | Up to 2048k | Higher RoPE dims under-trained |
| **LongRoPE2** | Microsoft 2025 (ICML) | Finetune | 128K from 8K, 80x cheaper than Meta | Hypothesis: high-freq RoPE dims under-trained -> OOD |
| Adjusted Base Frequency (ABF) | Qwen | Train-time | Used by Qwen2.5 | Need full pre-training stage |
| PoSE | ICLR 2024 | Train | Decouples train length from target | Sparse training -> needs many tokens |
| SelfExtend | 2024 | Yes (zero training) | Quick wins | Floor-div trick, smaller gains |
| Dual Chunk Attention (DCA) | HKUNLP | Yes (zero training) | 8-32x extension | Best when base is YaRN-trained already |

### 2.2 YaRN Recipe (Surrogate-1's primary tool)

YaRN combines NTK-by-parts + attention temperature scaling. Math:
- softmax(q_m^T k_n / (t * sqrt(D))), where t = sqrt(1/s)
- s = scaling factor

**Qwen2.5-Coder-7B-Instruct YaRN config** (drop into `config.json`):

```json
{
  "rope_scaling": {
    "factor": 4.0,
    "original_max_position_embeddings": 32768,
    "type": "yarn"
  }
}
```

This gives 128K context. For 65K target use factor=2.0, for 256K use factor=8.0.

**Caveat**: vLLM only supports STATIC YaRN - the scale stays fixed regardless of input length. Short-context perf degrades slightly when you set rope_scaling. So:
- Apply rope_scaling **only when you need long context**
- Run two endpoints if you serve both (one with, one without)

YaRN paper hyperparams (defaults): `beta_fast=32, beta_slow=1, mscale=1, attention_factor ~= 0.18`.

### 2.3 LongRoPE2 (Feb 2025, ICML 2025)

- Paper: arxiv 2502.20082
- Hypothesis: insufficient training in HIGHER RoPE dims drives OOD at long context
- Solution:
  1. **Evolutionary search** for per-dim rescaling factors, guided by **needle-driven perplexity** (focuses on key answer tokens, not avg perplexity over all tokens)
  2. **Mixed context window training**: short sequences keep ORIGINAL RoPE, long sequences get RESCALED RoPE; model dynamically switches at inference
- **Result**: LLaMA3-8B at 128K: **RULER = 82.03** (vs LongRoPE 73.40, vs YaRN 49.39)
- Only **10B tokens** of training (Meta's recipe needs ~800B = 80x more)
- Retains **>= 98.5%** of short-context perf
- **Code**: github.com/microsoft/LongRoPE

For Surrogate-1: LongRoPE2 is a strong "push to 128K with little compute" candidate IF we have 10B-token training corpus available. Currently we don't, so YaRN+DCA is the cheaper path.

### 2.4 Adjusted Base Frequency (ABF) - Qwen recipe

Pre-train stage adjusts theta from 10,000 -> 1,000,000 -> 5,000,000 -> 10,000,000 as context grows. Surrogate-1 v2 should NOT do this (too expensive); rely on Qwen base which already has ABF baked in.

### 2.5 LongLoRA / LongQLoRA

- Paper: arxiv 2309.12307 (ICLR 2024 Oral)
- **Sparse local attention (S2-Attn)** during training: split sequence into chunks, half the heads do shifted-by-half-chunk attention -> approximates full attention at lower cost
- Models: LLaMA2-LongLoRA-7B-100k, 13B-64k, 70B-32k
- LongQLoRA: extends LLaMA2-7B/13B from 4K to 8-12K on **single 32GB V100**
- Repos:
  - github.com/dvlab-research/longlora
  - github.com/yangjianxin1/LongQLoRA
- For Surrogate-1: useful as a memory-saving **training trick** to fit longer ctx on L40S, but inference must drop S2-Attn back to vanilla attention.

### 2.6 PoSE (Positional Skip-wise Training)

- Paper: arxiv 2309.10400 (ICLR 2024)
- Smart trick: simulate long position indices using SHORT actual sequences via skipping bias
- Decouples train length from target length
- Extended LLaMA from 4K -> 128K with only 2K training context
- Repo: github.com/dwzhu-pku/PoSE
- For Surrogate-1: ideal if we can't afford 32K training - can train at 8K and get 128K target. But efficacy reports lower than full long-context training.

### 2.7 SelfExtend

- Zero-training; uses floor-division to map unseen large positions to seen ones during inference
- Small gains; brittle on hard tasks
- For Surrogate-1: worth trying as **zero-cost baseline** before investing in YaRN finetune

### 2.8 Ring Attention

- arxiv 2310.01889
- Distributes blockwise attention across GPUs in a ring topology, overlapping K/V comm with compute
- Achieves **near-zero overhead** scaling to millions of tokens (training)
- 1M tokens on 32 H100 GPUs (Llama3-8B). Meta did 1M prefill on Llama3-405B in 77s with 93% parallel efficiency.
- **Zig-Zag Ring Attention**: interleaves splits to balance load
- For Surrogate-1: **out of budget** (we have at most a few GPUs). But torchtitan / DeepSpeed-Ulysses + RingAttention is the path if/when we move to multi-GPU.

---

## 3. Lost-in-the-Middle Mitigation

### 3.1 The original "Lost in the Middle" (Liu et al., arxiv 2307.03172)

- LLMs find info at start (primacy) or end (recency); miss the middle
- Performance drop is U-shaped over position
- Even "long-context" models suffer if not specifically trained against middle-recall

### 3.2 Attention Sink / StreamingLLM

- Paper: arxiv 2309.17453 (ICLR 2024)
- Repo: github.com/mit-han-lab/streaming-llm
- **Phenomenon**: models attend strongly to FIRST few tokens regardless of relevance ("sink")
- **Fix**: keep KV of first 4 tokens + sliding window of recent tokens
- Result: Llama-2/MPT/Falcon/Pythia stable at 4M+ tokens **without retraining**
- For Surrogate-1: free win for **inference** if we ever stream tasks longer than train length

### 3.3 SinkTrack (2026)

- arxiv 2604.10027
- Plug-and-play, no retrain. Inject attention-sink-like signal at every 5 layers
- **+21.6% on SQuAD2.0 with Llama3.1-8B-Instruct**
- For Surrogate-1: bookmark for v3 if v2 still suffers middle-recall

### 3.4 Found in the Middle

- arxiv 2403.04797
- Argues lost-in-middle is **emergent from pretraining objective**, not architectural
- Implication: the right fix is **training data** (multi-position needle injection), not attention surgery

### 3.5 NExtLong (ICML 2025)

- arxiv 2501.12766
- **Negative document Extension**: synthesize long-context training data by inserting hard-negative distractors between dependent meta-chunks
- Compels model to discriminate signal from noise across distance
- **Top results on HELMET + RULER vs other synthesis approaches**
- Released LLaMA-3-8B-NExtLong-512K-Base + Instruct
- Code: github.com/caskcsg/longcontext/tree/main/NExtLong
- **Key for Surrogate-1**: this is the BEST cheap data-side fix for middle-recall.

### 3.6 ChunkAttention training

- Train with chunked masking, attention sinks injected, position skipping
- Used by aiXcoder COLA, Qwen, others

### 3.7 Hierarchical Memory Transformer (HMT)

- arxiv 2405.06067 (NAACL 2025)
- Memory-augmented segment-level recurrence: preserve early tokens, pass embeddings, recall by retrieval
- 2-57x fewer params than long-ctx LLMs at comparable quality
- 2.5-116x less inference memory
- Repo: github.com/OswaldHe/HMT-pytorch
- For Surrogate-1: too invasive (architecture change). Leave for future research direction.

### 3.8 Mamba / Mamba-3 / RWKV (recurrent SOTA)

- Mamba (arxiv 2312.00752): selective state spaces, linear time, 5x throughput vs Transformer
- Mamba-3: more expressive recurrence, complex-valued state updates, MIMO
- RWKV (Eagle/Finch/GoldFinch): linear attention RNNs with parallelizable training
- LongMamba: training-free receptive-field extension via token filtering
- For Surrogate-1: not applicable (Qwen base is Transformer). Watch space for Surrogate-2.

### 3.9 Test-Time Training (TTT-E2E, Dec 2025)

- arxiv 2512.23675 (Stanford/NVIDIA/Berkeley/UCSD/Astera)
- Treat long context as continual learning: model UPDATES weights at test time on the input
- For 3B model + 164B tokens: **TTT-E2E scales like full-attention Transformer** at long context, while Mamba 2 / Gated DeltaNet do NOT
- **Constant** inference latency regardless of context length
- 2.7x faster than full attention at 128K
- For Surrogate-1: experimental but may be a leapfrog - bookmark for serious eval.

---

## 4. Effective Long-Context Training

### 4.1 "Effective Long-Context Scaling of Foundation Models" (Meta, NAACL 2024)

- arxiv 2309.16039
- Continual pretraining of Llama-2 with longer sequences + upsampled long docs
- 70B variant beats GPT-3.5-turbo-16k on long-context tasks
- **Cost-effective instruction tuning** without expensive human annotation

### 4.2 "How to Train Long-Context Language Models (Effectively)" (ACL 2025)

- arxiv 2410.02660
- **Data mixture recommendation**: 60% long, 40% short ("ShortMix")
- Long sources: code repos (concatenate all files), books
- Synthetic SFT data **outperforms human-curated** in their tests
- Curriculum: standard SFT first, then dedicated long-context SFT (only ~200 iterations needed!)

### 4.3 LongSkywork (arxiv 2406.00605)

- 200K context model
- Architecture mostly unchanged; just tweak RoPE base 10000 -> 2,600,000 for 4K -> 200K
- **200 iterations** of long-context SFT enough to convert standard SFT model
- Two synthetic data techniques during continual pretraining + SFT

### 4.4 LongRecipe (ACL 2025)

- arxiv 2409.00509
- Recipe for efficient long-context generalization

### 4.5 Repository-level training (CODE specific)

- aiXcoder-7B-v2 / COLA-132K (arxiv 2503.15301):
  - 132K samples, cross-file context up to **128K tokens**
  - 4 languages (Python, Java, etc)
  - Pipeline: Repo crawl -> dedup -> dependency graph construction -> long-context extraction
  - Two-stage training: focus on cross-file context
  - **+19.7% exact match** on aiXcoder-7B
- Qwen2.5-Coder pre-training:
  - 8K -> 32K extension
  - RoPE base 10K -> 1M (ABF)
  - FIM tokens: `<|fim_prefix|>`, `<|fim_middle|>`, `<|fim_suffix|>`, `<|repo_name|>`, `<|file_sep|>`
  - Best-fit packing: multiple files per sequence with cross-file attention masking
- DeepSeekCoder: topological order based on API deps
- StarCoder: GitHub issues + commit messages

### 4.6 Memory-efficient training stack

| Tool | What it gives | Notes |
|---|---|---|
| **FlashAttention 2** | 2x speedup over FA1, 50-73% theoretical FLOPs/s on A100 | Required baseline |
| **FlashAttention 3** | 1.5-2x over FA2, 75% FLOPs/s on H100, FP8 1.2 PFLOP/s | H100 only really shines |
| **Liger Kernel** | 20% throughput, 60% less memory | OOMs at 4K -> works at 16K |
| **Chunked Cross-Entropy** | Fuses linear+CE, 80% memory savings | Critical for vocab-large models |
| **DeepSpeed ZeRO-3 + offload** | Optimizer states off GPU | Slow but enables huge models |
| **DeepSpeed-Ulysses** | All-to-all SP, 1M tokens on 64 A100s | Combines with ZeRO-3 |
| **Ring Attention** | Near-zero-overhead 1M+ context | Ring topology, blockwise compute |
| **Context Parallel (CP)** | 1M+ in HF accelerate, 300K on 8 GPUs | Needs FSDP v2, SDPA, causal mask |
| **torchtitan + CP** | 1M sequence Llama3-8B on 32 H100s | Best PyTorch stack |
| **Unsloth gradient checkpointing="unsloth"** | -30% memory, supports very long ctx | The simple win |

### 4.7 Data synthesis recipes

For Surrogate-1-relevant code corpus:
1. **Concat repo files in dependency order** (topo sort) -> long sequences
2. **Hard negative interleaving** (NExtLong) -> distractor docs between related chunks
3. **FIM repo-aware**: `<|repo_name|>` + `<|file_sep|>` between files
4. **Multi-hop QA generation**: prompt teacher model with full repo, ask cross-file questions
5. **Best-fit packing**: pack with cross-file attention masking for diversity

---

## 5. Sparse Attention for Long Context

### 5.1 FlashAttention family

- FA1 (arxiv 2205.14135): IO-aware, block-tiled, linear memory
- FA2 (arxiv 2307.08691): better warp partitioning, 2x speedup, 50-73% FLOP/s
- FA3 (arxiv 2407.08608): warp specialization, async TMA + WGMMA, FP8 support, 75% H100 utilization, 1.2 PFLOP/s FP8
- Sliding window attention: WIP in Triton backend

### 5.2 MInference 1.0 (NeurIPS 2024 Spotlight)

- arxiv 2407.02490 (Microsoft + U Surrey)
- **3 sparse patterns** in long-context attention: A-shape, Vertical-Slash, Block-Sparse
- Offline: decide pattern per head; online: approximate sparse index, dispatch to optimal kernel
- **10x prefill speedup** at 1M context with LLaMA-3-8B on A100 (30 min -> 3 min)
- Works on LLaMA-3-1M, GLM4-1M, Yi-200K, Phi-3-128K, Qwen2-128K **without modification or retraining**
- Repo: github.com/microsoft/MInference
- **For Surrogate-1**: drop-in inference acceleration. Use it.

### 5.3 SageAttention / SpargeAttn (THU-ML)

- SageAttention (ICLR 2025): 8-bit attention, plug-and-play; 2.1x vs FA2, 2.7x vs xformers
- SpargeAttn (ICML 2025): training-free sparse, 2.5-5x vs FA / SageAttn
- SageAttention2++: efficient implementation
- SageAttention3: NeurIPS 2025 Spotlight
- Repos: github.com/thu-ml/SageAttention, github.com/thu-ml/SpargeAttn
- **For Surrogate-1**: drop-in inference acceleration; experimental but reported to retain accuracy.

### 5.4 BigBird / Longformer / Performer / Linformer

- BigBird (arxiv 2007.14062): random + local + global sparse attention
- Longformer: sliding window + global tokens
- Performer: random feature kernel approximation
- Linformer: low-rank K/V projection
- **Status 2026**: largely superseded by FlashAttention + DCA / MInference for new training; still used in document retrieval and certain niches.
- **For Surrogate-1**: not relevant.

### 5.5 Sliding Window Attention (SWA)

- Used by Mistral, Gemma; window of recent tokens
- For 7B models with long context, SWA + sink achieves StreamingLLM-style infinite generation
- For Surrogate-1: useful if we serve infinite-stream agent tasks

---

## 6. Context Compression / Summarization

### 6.1 LongLLMLingua (Microsoft, ACL 2024)

- arxiv 2310.06839
- Compresses long prompts before sending to LLM
- Question-aware coarse-to-fine compression + document reordering + dynamic ratios
- **+21.4% on NaturalQuestions with 4x fewer tokens** (GPT-3.5)
- **94% cost reduction** on LooGLE
- 1.4-2.6x latency speedup at 2-6x compression
- Integrated in LangChain + LlamaIndex
- Repo: github.com/microsoft/LLMLingua
- **For Surrogate-1**: pre-LLM step in agent pipeline; cheap; works with any inference stack.

### 6.2 Recurrent compression / Memorizing Transformers

- Older approach; superseded by hybrid attention + RAG
- HMT (Section 3.7) is the modern instantiation

### 6.3 KV Cache Offloading

- KVSwap (arxiv 2511.11907): offloads to disk; in-memory metadata predicts which KVs to preload
- HEADINFER (arxiv 2502.12574): head-wise selective offloading
- NVIDIA NVFP4 KV cache: FP4 quantized KV at long context
- For Surrogate-1: reduce VRAM at very-long-context inference. Bookmark for v2.5.

---

## 7. Code-Specific Long-Context

### 7.1 Repository-level pretraining

- StarCoder: GitHub issues, commit messages, repo metadata in training input
- DeepSeekCoder: topological order based on API dependencies
- Qwen2.5-Coder: 8K -> 32K extension; FIM with repo and file_sep tokens; best-fit packing
- aiXcoder-7B-v2 / COLA-132K (Section 4.5): focused cross-file context, 128K samples

### 7.2 Cross-file dependency

- Tree-sitter for AST parsing
- Build dependency graph; flatten in topological order
- Mask cross-file attention in training (or DON'T - actually encourages cross-file learning)

### 7.3 RepoCoder

- Iterative retrieval + generation
- Sliding window similarity retriever + LM generator
- For RAG-style long-context augmentation

### 7.4 Aider repo map

- Compact code-aware summary of repo structure
- Functions/classes/symbols + which-file-defines-what
- Adds maybe 1-3K tokens of "scaffold" to LLM call
- High utility, low cost - **direct inspiration for Surrogate-1 context engineering**

### 7.5 SWE-Bench / SWE-Bench Verified

- Real GitHub issues -> generated PR
- Multi-file edits
- Top scores: Claude Sonnet 4.5 at 200K = 77.2%, GPT-5.5 = high 70s
- Standard eval target for Surrogate-1 v2

### 7.6 LongCodeArena (Bogomolov et al., NeurIPS 2025 Datasets)

- arxiv 2406.11612
- 6 benchmarks: library code gen, CI build repair, project-level completion, commit msg gen, bug localization, module summarization
- Each task: manual dataset + eval suite + open-source baselines
- **Best benchmark to track Surrogate-1 progress on long-context CODE**

### 7.7 LongCodeBench / LongCodeU / LoCoBench

- LongCodeBench (arxiv 2505.07897): coding LLMs at 1M context windows
- LongCodeU (ACL 2025): broader long-context code understanding
- LoCoBench (arxiv 2509.09614): complex software engineering long-context benchmark

---

## 8. Long-Context Benchmarks (key reference numbers)

### 8.1 RULER (NVIDIA, arxiv 2404.06654)

- 13 synthetic tasks: retrieval, multi-hop tracing, aggregation, QA
- Standard for measuring "real" effective context size
- Repo: github.com/NVIDIA/RULER
- Leaderboard 2025/2026:
  - Nemotron 3 Super 120B-A12B: 0.917 (top)
  - Average across tested models: 0.877
  - GPT-4.1 era top: 0.588 mean (HELM Long Context aggregation)

### 8.2 LongBench v2 (THUDM, longbench2.github.io)

- 503 multiple-choice questions, 8K-2M context
- 6 categories incl. code repo understanding
- Top: Qwen3.5-397B-A17B = 0.632
- Repo: github.com/THUDM/LongBench

### 8.3 InfiniteBench (OpenBMB, arxiv 2402.13718)

- 12 unique tasks, 100K+ context
- Repo: github.com/OpenBMB/InfiniteBench

### 8.4 HELMET (Princeton, arxiv 2410.02694, ICLR 2025)

- 7 application-centric task categories
- Supports >128K
- Reliable for both base + instruct models
- Adopted by Microsoft Phi-4 + AI21 Jamba 1.6

### 8.5 BABILong (NeurIPS 2024, arxiv 2406.10149)

- 20 reasoning tasks; lengths 0-10M tokens
- Findings: popular LLMs use only 10-20% of context effectively
- Llama-3.1 + Qwen-2.5 best among open
- RAG: only 60% accuracy regardless of context length
- Recurrent memory transformers handle up to 50M tokens

### 8.6 LongCodeArena (arxiv 2406.11612)

- See 7.6

### 8.7 NIAH ("Needle in a Haystack")

- Single-needle retrieval at varying positions
- Necessary but NOT sufficient (RULER is harder)
- Most modern models hit 99%+ on standard NIAH; RULER differentiates

### 8.8 Recommended target scores for Surrogate-1 v2

| Benchmark | Target | Stretch | Notes |
|---|---|---|---|
| RULER @ 32K | 90+ | 95+ | Native window; should be near-perfect |
| RULER @ 128K | 80+ | 85+ | YaRN factor=4; matches Qwen2.5-7B-1M |
| RULER @ 256K | 65+ | 75+ | DCA-extended |
| LongBench v2 (overall) | 0.40+ | 0.50+ | Realistic for 7B |
| Long Code Arena (avg) | matches Qwen2.5-Coder-7B-Instruct base + 5pp | + 10pp | Code repo-level focus |
| Lost-in-Middle MRCR @ 64K | 50+ | 65+ | Position-robust retrieval |
| BABILong @ 64K | 30+ | 45+ | Multi-hop reasoning |

---

## 9. Memory / External Memory Modules

### 9.1 MemGPT / Letta

- MemGPT now part of Letta (UC Berkeley Sky Lab origin)
- OS-inspired memory hierarchy: core memory (in-context), conversational memory, archival memory, external files
- Agent actively pages info in/out
- Letta: full agent platform with stateful memory
- For Surrogate-1: useful AGENT-LAYER design (not model-layer) - implement on top of Surrogate-1, not inside it

### 9.2 Memorizing Transformers / kNN-LM

- Older approach with external KV memory + kNN retrieval
- Limited adoption; RAG won

### 9.3 KV cache offloading (already covered 6.3)

---

## 10. Practical Extension on L40S 48GB GPU

### 10.1 Reality check: what fits

| Setup | Train context | Memory used | Notes |
|---|---|---|---|
| Qwen2.5-Coder-7B FP16 | 2K | ~16GB weights + ~5GB KV + grad/optim | Standard |
| + LoRA r=16 | 2K | ~18GB | Cheap |
| + LoRA + FA2 + grad checkpoint | **8K** | ~22GB | Comfortable |
| + Liger Kernel + Unsloth | **16K** | ~28GB | Recommended baseline |
| + Sample packing | **32K** | ~42GB | Tight; doable |
| Training above 32K on 1x L40S | **64K?** | OOM very likely | Need 2x L40S + sequence parallel |
| 2x L40S + DeepSpeed-Ulysses SP=2 | **64K** | ~42GB each | Feasible |
| 4x L40S + SP=4 + ZeRO-3 | **128K** | ~38GB each | Realistic for v2 if budget allows |

KV cache for 7B at FP16 ~ 0.3GB at 2K, ~5GB at 32K, ~20GB at 128K.

### 10.2 Surrogate-1 v2 Training Plan (single L40S + LoRA)

```yaml
# axolotl-style config
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
load_in_4bit: false  # LoRA, not QLoRA, on L40S
adapter: lora
lora_r: 32
lora_alpha: 64
lora_dropout: 0.05
lora_target_modules:
  - q_proj
  - k_proj
  - v_proj
  - o_proj
  - gate_proj
  - up_proj
  - down_proj

sequence_len: 32768
sample_packing: true
pad_to_sequence_len: true
flash_attention: true
gradient_checkpointing: unsloth   # or true; unsloth = -30% memory

# RoPE - keep native at 32K during train
# (do NOT set rope_scaling here; we apply YaRN at inference for >32K)

# data
datasets:
  - path: <your-long-context-code-corpus>
    type: completion
    field: text

micro_batch_size: 1
gradient_accumulation_steps: 16
num_epochs: 1   # don't overfit on synthetic
learning_rate: 1.5e-4
lr_scheduler: cosine
warmup_steps: 100
optimizer: adamw_torch_fused
bf16: true
tf32: true

# Liger
plugins:
  - axolotl.integrations.liger.LigerPlugin
liger_rope: true
liger_rms_norm: true
liger_glu_activation: true
liger_layer_norm: true
liger_fused_linear_cross_entropy: true

# loss / eval
val_set_size: 0.01
evals_per_epoch: 4
saves_per_epoch: 4
```

### 10.3 Surrogate-1 v2 Inference Recipe

For the **128K regime** (most common):

```bash
# vLLM 0.7+
VLLM_ATTENTION_BACKEND=DUAL_CHUNK_FLASH_ATTN \
vllm serve Qwen/Qwen2.5-Coder-7B-Instruct \
  --max-model-len 131072 \
  --rope-scaling '{"type":"yarn","factor":4.0,"original_max_position_embeddings":32768}' \
  --enable-chunked-prefill \
  --max-num-batched-tokens 32768 \
  --max-num-seqs 4 \
  --enforce-eager
```

For the **256K-512K regime** (DCA extrapolation beyond training):

```bash
VLLM_ATTENTION_BACKEND=DUAL_CHUNK_FLASH_ATTN \
vllm serve <surrogate-1-v2-merged> \
  --max-model-len 524288 \
  --rope-scaling '{"type":"yarn","factor":16.0,"original_max_position_embeddings":32768}' \
  --enable-chunked-prefill \
  --max-num-batched-tokens 32768 \
  --max-num-seqs 1 \
  --enforce-eager
```

For 1M, mirror Qwen's example: 4x GPU tensor parallel, 120GB total VRAM minimum (we don't have this; skip).

### 10.4 Data Synthesis Pipeline for Surrogate-1 v2

1. **Source long-context repo data**: clone target repos, dependency graph, topo sort
2. **Pack with FIM tokens**: `<|repo_name|>foo<|file_sep|>file_a.py\n...` etc
3. **Inject NExtLong-style hard negatives**: from unrelated repos, intersperse
4. **Multi-hop QA synthesis**: use Qwen2.5-72B as teacher to generate QA pairs spanning 5+ files
5. **Mix ratio**: 60% long (32K+), 40% short (1-8K)
6. **Volume target**: 100M-500M tokens for context extension stage (LongRoPE2 used 10B; we don't have that budget)
7. **Quality > quantity**: NExtLong shows synthetic > human-curated

---

## 11. Surrogate-1 v2 Integration Plan (consolidated)

### 11.1 Phase A - Free wins (zero training)

1. Apply YaRN config to Qwen2.5-Coder-7B-Instruct at inference: `factor=4.0` -> 128K
2. Enable DCA backend via vLLM
3. Add MInference for prefill speedup at 128K+
4. Implement Aider-style repo map at agent layer
5. Add LongLLMLingua compression in agent pipeline

**Result expected**: 128K context, RULER ~75-80, no training cost.

### 11.2 Phase B - LoRA at 32K (1x L40S, 1-3 days)

1. Curate ~50M-100M token long-context code corpus (focus repos relevant to Surrogate-1's domain)
2. Train Qwen2.5-Coder-7B + LoRA at 32K for 1 epoch (Liger + sample packing + Unsloth grad ckpt)
3. Apply YaRN(4.0) + DCA at inference -> 128K effective

**Result expected**: 32K trained native, 128K via DCA, RULER 80+ at 128K, marked improvement on Long Code Arena.

### 11.3 Phase C - Optional 128K LoRA fine-tune (2-4x L40S or rented H100, ~1 week)

1. Add NExtLong-style synthetic data
2. Train at 128K with Context Parallel SP=2 or SP=4
3. Eval on RULER 128K, LongBench v2, Long Code Arena
4. **Target**: RULER 85+ at 128K

### 11.4 Phase D - Lost-in-middle hardening

1. Add SinkTrack at inference
2. Add LongLLMLingua reordering
3. Eval MRCR + BABILong; iterate on training data

### 11.5 Long-term (Surrogate-2)

- Evaluate TTT-E2E for constant-latency long-context
- Consider Mamba-3 hybrid architecture if Transformer scaling plateaus
- HMT-style hierarchical memory module

---

## 12. Open Questions / Risks

1. **Static YaRN downside**: shorter prompts degrade slightly when YaRN is on. Mitigation: dual endpoints (one with, one without rope_scaling) OR use dynamic YaRN if available in our inference stack.
2. **DCA accuracy at 1M**: passkey near-perfect, but harder reasoning tasks degrade. Don't promise 1M effective context; promise 128K-256K confidently.
3. **Single L40S limit**: cannot train at >32K. We'll need multi-GPU rental for 128K training.
4. **Synthetic data risk**: NExtLong reports synthetic > human, but quality of teacher model matters. Use Qwen2.5-72B or similar.
5. **Code-specific eval**: Long Code Arena and LongCodeBench are newer, smaller benches. Variance can be high. Run 3 seeds.
6. **Memory pressure at long inference**: chunked prefill is essential. Don't disable.
7. **vLLM/SGLang version drift**: DCA support is recent. Pin versions in production.

---

## 13. Sources (primary)

- Qwen2.5-1M Technical Report: https://arxiv.org/abs/2501.15383 / https://qwenlm.github.io/blog/qwen2.5-1m/
- Qwen Technical Report: https://arxiv.org/abs/2412.15115
- Qwen2.5-Coder Technical Report: https://arxiv.org/abs/2409.12186
- Kimi K2 Technical Report: https://arxiv.org/abs/2507.20534
- DeepSeek-V3 Technical Report: https://arxiv.org/abs/2412.19437
- Llama 3 Herd: https://arxiv.org/abs/2407.21783
- LongRoPE2: https://arxiv.org/abs/2502.20082 (Microsoft, ICML 2025)
- YaRN: https://arxiv.org/abs/2309.00071
- LongLoRA: https://arxiv.org/abs/2309.12307 (ICLR 2024)
- LongQLoRA: https://arxiv.org/abs/2311.04879
- PoSE: https://arxiv.org/abs/2309.10400 (ICLR 2024)
- SelfExtend: https://arxiv.org/abs/2401.01325
- Lost in Middle: https://arxiv.org/abs/2307.03172
- StreamingLLM: https://arxiv.org/abs/2309.17453 (ICLR 2024)
- Found in Middle: https://arxiv.org/abs/2403.04797
- NExtLong: https://arxiv.org/abs/2501.12766 (ICML 2025)
- HMT: https://arxiv.org/abs/2405.06067 (NAACL 2025)
- LongSkywork: https://arxiv.org/abs/2406.00605
- LongRecipe: https://arxiv.org/abs/2409.00509
- Effective Long-Context Scaling (Meta): https://arxiv.org/abs/2309.16039
- How to Train Long-Context (effectively): https://arxiv.org/abs/2410.02660
- aiXcoder-7B-v2 / COLA: https://arxiv.org/abs/2503.15301
- QwenLong-L1.5: https://arxiv.org/abs/2512.12967
- Ring Attention: https://arxiv.org/abs/2310.01889
- DeepSpeed-Ulysses: https://arxiv.org/abs/2309.14509
- FlashAttention 1: https://arxiv.org/abs/2205.14135
- FlashAttention 2: https://arxiv.org/abs/2307.08691
- FlashAttention 3: https://arxiv.org/abs/2407.08608
- MInference 1.0: https://arxiv.org/abs/2407.02490 (NeurIPS 2024 Spotlight)
- SageAttention / SpargeAttn: github.com/thu-ml/SageAttention, thu-ml/SpargeAttn
- Liger Kernel: https://arxiv.org/abs/2410.10989
- LongLLMLingua: https://arxiv.org/abs/2310.06839
- TTT-E2E: https://arxiv.org/abs/2512.23675
- Query-only TTT: https://arxiv.org/abs/2512.13898
- Mamba: https://arxiv.org/abs/2312.00752
- RULER: https://arxiv.org/abs/2404.06654
- LongBench v2: https://longbench2.github.io/
- InfiniteBench: https://arxiv.org/abs/2402.13718
- HELMET: https://arxiv.org/abs/2410.02694 (ICLR 2025)
- BABILong: https://arxiv.org/abs/2406.10149
- Long Code Arena: https://arxiv.org/abs/2406.11612
- LongCodeBench: https://arxiv.org/abs/2505.07897
- Gemini 2.5 paper: https://arxiv.org/abs/2507.06261

## 14. Code / Repo Pointers

- microsoft/LongRoPE
- HKUNLP/ChunkLlama (DCA original)
- microsoft/MInference
- thu-ml/SageAttention, thu-ml/SpargeAttn
- linkedin/Liger-Kernel
- dvlab-research/longlora
- yangjianxin1/LongQLoRA
- dwzhu-pku/PoSE
- mit-han-lab/streaming-llm
- tomaarsen/attention_sinks
- caskcsg/longcontext (NExtLong)
- OswaldHe/HMT-pytorch
- Tongyi-Zhiwen/Qwen-Doc (QwenLong-L1.5)
- aixcoder-plugin/aixcoder-7b-v2
- NVIDIA/RULER
- THUDM/LongBench
- princeton-nlp/HELMET
- OpenBMB/InfiniteBench
- booydar/babilong
- microsoft/LLMLingua
- letta-ai/letta
- test-time-training/e2e
- axolotl-ai-cloud/axolotl (sequence parallelism)
- unslothai/unsloth
- vllm-project/vllm (DCA in PR #6139)

---

*Curated: 2026-04-29. Surrogate-1 v2 planning session.*

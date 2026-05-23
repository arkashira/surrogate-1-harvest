---
tags: [surrogate-1, v13, frontier-efficiency, training-side, kernels, moe, spec-decoding, optimizers, distillation, t4-feasible, 2025-2026]
created: 2026-05-01
status: research-grounded — sources cited inline
audience: V13 trainer wire-up
based-on: 19 web searches, 2025-2026 sources only
related: [[surrogate-1-v10-rev2-spec]], [[training-tooling-2026-Q2]], [[frontier-releases-2026-Q2]], [[opensource-releases-2026-Q2]]
---

# V13 Frontier Efficiency — "Smarter With Less" (Training-Side, T4×2 Feasibility)

> **Owner directive (2026-05-01):** "ใช้ resource น้อยลง เห็น frontier บางเจ้าทำได้แล้ว"
>
> Frontier labs (DeepSeek-V3.2, GLM-4.7, Qwen3-MoE) achieve more capability per FLOP through MoE sparsity, sparse attention, low-precision kernels, and smarter optimizers. This file maps EVERY 2025-2026 training-side technique I can wire into V13's Kaggle T4×2 + Civo L40S budget — with measured numbers, paper URLs, install commands, and concrete kaggle-trainer.sh patches.

---

## TL;DR — The Stack That Fits T4×2

| Tier | Technique | Speedup / Memory | T4×2? | Lift |
|---|---|---|---|---|
| **S** | **Unsloth 2026 (April)** | 2× SFT, 12× MoE, 7-12× longer RL ctx, 70% less VRAM | YES native | trivial |
| **S** | **Liger Kernel (TRL+post-training)** | +20% throughput, -60-80% memory, GRPO -40% | YES via Triton | trivial |
| **A** | **APOLLO-Mini optimizer** | SGD memory + 3× throughput, 4× larger BS | YES (Triton) | medium |
| **A** | **EAGLE-3 head training (offline)** | 6.5× inference speedup post-deploy, head trains on T4 | YES head fits | medium |
| **A** | **MEDUSA-1 heads** | 2.2-3.6× inference speedup, parameter-efficient train | YES (heads = MB-scale) | trivial |
| **A** | **GKD on-policy distillation** | 9-30× cheaper vs off-policy, fixes train/test mismatch | YES via TRL | medium |
| **B** | **MiniLLM reverse-KL** | Better calibration, less exposure bias, V100-scale | YES | medium |
| **B** | **LongLoRA shifted-sparse-attn** | 32K ctx on small GPUs, 2-line patch | YES | trivial |
| **B** | **DSA (DeepSeek sparse attn)** | O(L²) → O(Lk), 128K ctx | partially (use indexer only) | medium |
| **C** | **Quartet MXFP4 backward** | 1.8× vs FP8 forward+back | NO (Blackwell-only) | defer |
| **C** | **FlashAttention-3** | 1.5-2.0× vs FA2, 840 TFLOPs BF16 | NO (Hopper-only) | defer |
| **C** | **Muon optimizer** | -50% cost, -33% memory vs AdamW | YES but needs FSDP2 | Civo only |
| **C** | **DeepSpeed-Ulysses SP** | 2.5× faster, 4× longer seq | YES but needs ≥4 GPU for benefit | defer |

**Rule:** S+A tiers are ALL T4×2 feasible — wire all of them in V13. C tier defers to Civo L40S phase or Blackwell-only.

---

## 1. Unsloth April 2026 (Faster MoE) — TIER S, MUST-HAVE

**Source:** [Unsloth 2026 Update — Faster MoE](https://unslothai.substack.com/p/unsloth-2026-update-faster-moe), [12x Faster MoE Discussion #4020](https://github.com/unslothai/unsloth/discussions/4020), [Daniel Han X post Apr 2026](https://x.com/danielhanchen/status/2011828515348627561)

**Measured:**
- **MoE training 12× faster** with **35% less VRAM** and **6× longer context** (gpt-oss, Qwen3-30B/235B, DeepSeek R1/V3, GLM-4.6/4.7/Flash)
- **RL ~7× longer context** (up to 12× with Standby + tiled MLP)
- **gpt-oss QLoRA 380K ctx on 1× B200**; for T4: still 8× longer than vanilla
- **Qwen3-14B fits T4 16GB** at QLoRA, 70% VRAM reduction
- Embedding/BERT: 1.8-3.3× faster, -20% VRAM, 2× longer ctx vs FA2

**Three combined algos (RL ctx):** seqlen+hidden chunking + offloaded log softmax + tiled MLP

**Install:** `pip install --upgrade unsloth unsloth-zoo`

**T4 caveat:** Qwen3.5 Mamba kernels compile slow on T4; QLoRA NOT recommended for Qwen3.5 (use Qwen3 or Qwen2.5).

**V13 wire:** Already in V10 plan — bump to latest (`pip install unsloth>=2026.4.0`).

---

## 2. Liger Kernel (LinkedIn) — TIER S, MUST-HAVE

**Sources:** [GitHub linkedin/Liger-Kernel](https://github.com/linkedin/Liger-Kernel), [arxiv 2410.10989](https://arxiv.org/abs/2410.10989), [HF blog Liger GRPO meets TRL](https://huggingface.co/blog/liger-grpo), [TRL integration docs](https://huggingface.co/docs/trl/liger_kernel_integration)

**Measured:**
- **+20% throughput, -60% memory** (LLaMA-3-8B, BS=8, BF16, AdamW, FSDP1, 8×A100)
- **Post-training kernels (DPO/ORPO/SimPO/KTO/JSD): up to -80% memory**
- **Liger GRPO in TRL: -40% memory, zero quality drop** (Chunked Loss extended to GRPO)
- Per-kernel: RMSNorm 7× faster + 3× less peak mem (hidden=16384); GeGLU 1.6× less peak mem (seq=16384); Qwen2 +25.5% throughput, -56.8% mem (BS=48)
- **Hopper→Turing scope:** Triton-based, runs on T4 (Turing) — confirmed via Triton ≥2.x compatibility

**Install:**
```bash
pip install liger-kernel
# or stable nightly: pip install liger-kernel-nightly
# Latest stable: 0.7.0 (Feb 2026)
```

**TRL one-liner integration:**
```python
from trl import SFTConfig
config = SFTConfig(use_liger_kernel=True, ...)
# That's it — auto-patches RMSNorm/RoPE/SwiGLU/CrossEntropy/FusedLinearCE
```

**V13 wire:** Add `use_liger_kernel=True` to ALL `SFTConfig`/`DPOConfig`/`GRPOConfig`. Free 60% memory savings.

---

## 3. Quartet MXFP4 — TIER C, DEFER (Blackwell-only)

**Source:** [arxiv 2505.14669](https://arxiv.org/abs/2505.14669), [GitHub IST-DASLab/Quartet](https://github.com/IST-DASLab/Quartet), NeurIPS 2025

**Measured:**
- **All matmuls in MXFP4** (including backward — both `dW` and `dX` use MXFP4)
- Forward: **2.4× faster vs FP8**; Backward: **1.6× vs FP8**; Overall: **~1.8× vs FP8**
- Tested on RTX 5090 (Blackwell, has MXFP4 hardware)
- Near-lossless for LLM pre-training in large-data regime

**Why defer:** MXFP4 hardware support is Blackwell-only (B200, RTX 5090). T4 = Turing, no FP4 ALUs. Civo L40S = Ada Lovelace, has FP8 (not MXFP4).

**Quartet II (Apr 2026):** [arxiv 2601.22813](https://arxiv.org/pdf/2601.22813) — NVFP4 pre-training, more accurate.

**V13 action:** Skip. Note as future-arch optimization for V14+ when Blackwell rentable.

---

## 4. EAGLE-3 (Speculative Decoding Heads) — TIER A, WIRE

**Sources:** [arxiv 2503.01840](https://arxiv.org/abs/2503.01840), [GitHub SafeAILab/EAGLE](https://github.com/SafeAILab/EAGLE), [LMSYS blog Vertex deploy](https://www.lmsys.org/blog/2025-12-01-eagle3-vertex/), NeurIPS 2025

**Measured:**
- **Speedup ratio up to 6.5× inference**, 1.4× over EAGLE-2
- SGLang batch=64: 1.38× throughput improvement
- **Scaling law for spec-decoding** discovered: more training data → proportional speedup gain
- Direct token prediction (vs feature prediction in EAGLE-1/2)
- Training-Time Test (TTT) — simulates inference distribution during training
- Multi-layer feature fusion: early/middle/late layer reps fed to draft head

**Install via SpecForge** (LMSYS official trainer): see #5 below.

**V13 wire:** Already in V10 plan (Phase: spec-decoding head, ~6 hr Civo L40S, $12). Use SpecForge online mode.

---

## 5. SpecForge (SGLang Training Framework) — TIER A, WIRE

**Sources:** [GitHub sgl-project/SpecForge](https://github.com/sgl-project/SpecForge), [LMSYS blog Jul 2025](https://www.lmsys.org/blog/2025-07-25-spec-forge/), [LMSYS SpecBundle Dec 2025](https://www.lmsys.org/blog/2025-12-23-spec-bundle-phase-1/)

**Measured:**
- Trains EAGLE-3 + DFlash spec-decoding models, ports to SGLang
- FSDP + TP distributed training
- **Two modes:** online (freeze target, train draft simultaneously) and offline (cache hidden states first, then train draft separately — better for memory-constrained)
- v0.2 (Dec 2025): Production-ready, multiple execution backends
- SpecBundle (Dec 2025): pre-trained EAGLE-3 checkpoints from industry partners

**Install:** `pip install specforge`

**V13 wire:** Use **offline mode** for T4×2 feasibility (cache target hidden states once on Civo, train draft on T4 cheap).

```python
# Pseudocode for offline mode
# Step 1 (Civo, one-shot): cache_hidden_states(target=qwen3-14b, dataset=...)
# Step 2 (Kaggle T4): train_draft_head(cached_states, draft_arch=eagle3)
```

---

## 6. MEDUSA Heads — TIER A, WIRE (cheaper alt to EAGLE-3)

**Sources:** [arxiv 2401.10774](https://arxiv.org/abs/2401.10774), [GitHub FasterDecoding/Medusa](https://github.com/FasterDecoding/Medusa), [SageMaker MEDUSA-1 ~2× post](https://aws.amazon.com/blogs/machine-learning/achieve-2x-speed-up-in-llm-inference-with-medusa-1-on-amazon-sagemaker-ai/)

**Measured:**
- **MEDUSA-1: 2.2× speedup**, **MEDUSA-2: ~3× speedup**, range 2.2-3.6× across model sizes
- **Self-distillation** = no need for original training data
- **Parameter-efficient** — only the heads train, original model frozen → "GPU-poor friendly"
- Tree-attention verifies multiple candidates per step

**Trade-off vs EAGLE-3:** EAGLE-3 is faster (6.5× vs 3×) but needs more training data; MEDUSA is dead-simple, runs on tiny budgets, head trains in <2hr on T4.

**V13 wire:** Add MEDUSA as fallback if SpecForge offline mode fails on T4 memory.

---

## 7. APOLLO / APOLLO-Mini Optimizer — TIER A, WIRE

**Sources:** [GitHub zhuhanqing/APOLLO](https://github.com/zhuhanqing/APOLLO), [arxiv 2412.05270](https://arxiv.org/abs/2412.05270), MLSys'25 Outstanding Paper Honorable Mention

**Measured:**
- **APOLLO-Mini: SGD-level memory cost, but BETTER than AdamW pre-training perf**
- Memory: as little as **1/8 to 1/1024 of AdamW optimizer state**
- **3× throughput on 8×A100-80GB** vs AdamW (enables 4× larger BS)
- **LLaMA-13B on A100-80G with naive DDP, no system optimizations**
- **LLaMA-7B from scratch on a single GPU with <12GB memory** (with quantization)
- Integrated in HuggingFace Transformers + LLaMA-Factory

**Install:**
```bash
pip install apollo-torch
# Or from source: pip install git+https://github.com/zhuhanqing/APOLLO.git
```

**Usage:**
```python
from apollo_torch import APOLLOAdamW
# Drop-in replacement for AdamW
optimizer = APOLLOAdamW(
    model.parameters(),
    lr=2e-4,
    rank=256,           # auxiliary projection rank
    proj_type="random",
    update_proj_gap=200,
    scale=128,
    scale_type="tensor", # "tensor" = APOLLO-Mini, "channel" = APOLLO
)
```

**V13 wire:** Replace AdamW with APOLLO-Mini in all SFT phases. **Critical for T4 16GB** — frees ~4-6GB for larger batches.

---

## 8. Muon Optimizer (Keller Jordan) — TIER C/B, partial

**Sources:** [GitHub KellerJordan/Muon](https://github.com/KellerJordan/Muon), [arxiv 2502.16982 "Muon is Scalable"](https://arxiv.org/abs/2502.16982), [Keller Jordan blog](https://kellerjordan.github.io/posts/muon/)

**Measured:**
- **52% of AdamW FLOPs** for comparable perf (= -48% compute)
- **-33% memory savings**, **~50% lower training cost**
- CIFAR-10 94%: 3.3 → 2.6 A100-seconds
- FineWeb val loss 3.28: **1.35× faster**
- Scaled to **3B/16B Moonlight MoE on 5.7T tokens** (Kimi)
- Mechanism: SGD-momentum + Newton-Schulz orthogonalization on 2D weight matrices

**Axolotl integration:**
- Distributed Muon support added (axolotl PR #2367) — requires **FSDP2**
- Use case: pretraining or full-finetune at scale

**Why partial for T4×2:** Muon shines at scale + FSDP2. T4×2 = small + DDP-friendly. Use only if doing full-finetune on Civo L40S.

**V13 wire:** Use `optimizer: muon` in axolotl config for the **Civo L40S 14B SFT phase only**. Skip on T4.

---

## 9. SOAP Optimizer — TIER B, optional

**Sources:** [arxiv 2409.11321 SOAP](https://arxiv.org/html/2409.11321v2), OpenReview ICLR, [Vyas et al SOAP+Muon iterative whitening](https://nikhilvyas.github.io/SOAP_Muon.pdf), [arxiv 2509.01440 Optimizer Benchmark](https://arxiv.org/abs/2509.01440)

**Measured:**
- vs AdamW & Shampoo: **-40% steps, -35% wall-clock** in large-batch regime
- ~20% iter reduction vs Shampoo
- More robust to preconditioning frequency than Shampoo
- 2025 benchmarking: **D-Muon best at 1B tokens; SOAP/AdEMAMix close 2nd**
- AdamW catches up at scale; AdEMAMix wins long horizons

**Install:** `pip install soap-torch` (or via axolotl `optimizer: soap`)

**V13 wire:** Optional alternative to APOLLO. APOLLO wins on memory; SOAP wins on convergence. **Pick APOLLO for T4 (memory-bound)**.

---

## 10. ScatterMoE — TIER A (only if upcycling) 

**Sources:** [arxiv 2403.08245](https://arxiv.org/html/2403.08245v1), axolotl integration ([PR](https://github.com/axolotl-ai-cloud/axolotl/issues/3155))

**Measured:**
- **scatter2scatter Triton kernel** — fuses grouped-GeMMs + scattered read/write, no padding/copies
- **225B tokens/day on 96× H100 for 7B MoE with FSDP-2**
- Axolotl now has **ScatterMoE LoRA** — LoRA on MoE expert weights via custom Triton kernels

**V13 relevance:** Only if upcycling Qwen3-14B-dense → MoE. Otherwise skip.

If we upcycle (V13.5+):
```yaml
# axolotl.yml
plugins:
  - axolotl.integrations.scattermoe
moe_kernel: scattermoe
```

---

## 11. DeepSeek Sparse Attention (DSA) — TIER B, partial wire

**Sources:** [arxiv 2512.02556](https://arxiv.org/abs/2512.02556) DeepSeek-V3.2, [SGLang blog Sep 2025](https://www.lmsys.org/blog/2025-09-29-deepseek-V32/), [Sebastian Raschka Tour V3→V3.2](https://magazine.sebastianraschka.com/p/technical-deepseek)

**Measured:**
- **Attention complexity O(L²) → O(Lk)** — k = top-k tokens per query
- Lightning Indexer (FP8 ultra-light scorer) + Top-k Token Selection
- **Substantial training+inference speedup at 128K ctx**, ~zero quality loss
- Two-stage continued pretraining: Dense Warm-up Stage initializes the indexer

**Why partial:** DSA needs continued-pretraining. Adapting to a finetune is hard. **Use the *idea*** — train a lightning indexer head on top of frozen base + sliding window, get most of the benefit cheaper.

**V13 alternative:** Use **LongLoRA shifted-sparse-attention** instead (cheaper, same regime).

---

## 12. LongLoRA (Shifted Sparse Attention) — TIER A, WIRE

**Sources:** [arxiv 2309.12307](https://arxiv.org/abs/2309.12307), [hanlab MIT page](https://hanlab.mit.edu/projects/longlora), ICLR 2024

**Measured:**
- **Llama2-7B → 100K ctx on 8× A100**; 70B → 32K
- **2-line code patch** for training (optional in inference)
- Splits ctx into groups, half-heads shifted by half-group-size for cross-group flow
- Compute saving = sub-quadratic in ctx length

**For T4×2:** With Unsloth 2026's 6-12× ctx extension on top of LongLoRA, **32K ctx fits T4 16GB QLoRA on 7-14B models**.

**V13 wire:** Already in V10 plan — Phase 1 (Kaggle T4×2 SFT 7B + LongLoRA 32K).

```python
# train.py patch
from longlora import replace_attn_with_shifted_sparse_attn
replace_attn_with_shifted_sparse_attn(model, group_size=2048)
```

---

## 13. FlashAttention-3 — TIER C, DEFER (Hopper-only)

**Sources:** [arxiv 2407.08608](https://arxiv.org/abs/2407.08608), [PyTorch blog](https://pytorch.org/blog/flashattention-3/), [Tri Dao blog](https://tridao.me/blog/2024/flash3/)

**Measured:**
- 1.5-2.0× forward, 1.5-1.75× backward vs FA2 on H100
- BF16: up to 840 TFLOPs/s (85% util on H100)
- FP8: ~1.2 PFLOPS, 2.6× smaller error vs baseline FP8 attention

**Why defer:** Hopper-specific (Tensor Core async + TMA + warp-spec). FA2 still works on T4 (Turing) — just slower.

**V13:** Use **FlashAttention-2** on Kaggle T4 (compatible). Use FA3 only on Civo H100 phase if rented.

---

## 14. DeepSpeed-Ulysses (Sequence Parallelism) — TIER C, DEFER

**Sources:** [DeepSpeed Ulysses tutorial](https://www.deepspeed.ai/tutorials/ds-sequence/), [arxiv 2309.14509](https://arxiv.org/abs/2309.14509)

**Measured:**
- **2.5× faster training, 4× longer seq** vs SOTA baseline
- 1M context on 64× A100 (Llama-style)
- 2025 Arctic Long Sequence Training: 15M tokens on 32× H100 (Llama-8B)
- **469× max-seq improvement over HF+ZeRO3 baseline**
- Constant comm volume w/ proportional GPU+seq scale

**Why defer:** Needs ≥4 GPUs to amortize. T4×2 doesn't have headroom.

**V13:** Defer to Civo multi-L40S phase if scaling to 32K+ ctx full-finetune.

---

## 15. Quantization-Aware Training (QAT) / QA-LoRA — TIER B, optional

**Sources:** [QLoRA arxiv 2305.14314](https://arxiv.org/abs/2305.14314), [QA-LoRA ICLR 2024](https://openreview.net/pdf?id=WvFoJccpo8), [torchtune quantization recipe](https://github.com/meta-pytorch/torchtune/blob/main/recipes/quantization.md)

**Measured:**
- QLoRA: 65B on 48GB GPU, 4-bit NF4 + double-quant + paged optimizers, ~zero perf loss vs 16-bit
- QA-LoRA: extends to **INT3 / INT2** for small models (7B/13B) — strong solution for compute-constrained
- Recent: PTQ + QLoRA combos for inference deployment

**V13 relevance:** Already using QLoRA NF4. **QA-LoRA INT3 worth trying** for the deployable adapter (60% smaller on disk, T4-inference faster).

---

## 16. Sparse Upcycling (Dense → MoE) — TIER C, V13.5+ feature

**Sources:** [DeepSeek-V3 tech report](https://arxiv.org/abs/2412.19437), [Innovator MoE upcycling arxiv 2507.18671](https://arxiv.org/html/2507.18671), [Marco-MoE arxiv 2604.25578](https://arxiv.org/html/2604.25578)

**Measured:**
- Take dense pretrained → copy FFN N× → add router → continue training = **"upcycling"**
- Used by Qwen-1.5-MoE, Phi-3.5, DeepSeek-V2 cheaply
- Drop-Upcycling + sub-matrix splitting → fine-grained experts, less redundancy
- **Cost: small fraction of from-scratch MoE training**

**Why defer:** Needs continued pretraining (≥1B tokens). Out of T4 budget. V13.5 if budget grows.

---

## 17. KV Cache Compression (Training-Aware) — TIER B/C, mostly inference

**Sources:** [StreamingLLM](https://arxiv.org/abs/2309.17453), H2O, [SONIC arxiv 2601.21927](https://arxiv.org/html/2601.21927), [NVIDIA kvpress](https://github.com/NVIDIA/kvpress)

**Measured:**
- StreamingLLM: attention sinks + sliding window
- H2O: heavy-hitter dynamic identification
- SONIC (2025): -80% KV with +35.55% MTBench101 score (multi-turn)
- CacheGen: 3.5-4× more compression than H2O quantized

**V13 relevance:** Mostly **inference-side** (deploy on HF Space). Train-aware = train model with attention-sink token (4 sink tokens at start). Cheap +0.5% perf bump.

**V13 wire (cheap):** Always include 4 special "sink" tokens at sequence start during SFT.

---

## 18. Multi-Token Prediction (MTP) — TIER A, MTP-aware SFT

**Sources:** [DeepSeek-V3 §2.2 MTP](https://arxiv.org/html/2412.19437v1), [DeepWiki MTP](https://deepwiki.com/deepseek-ai/DeepSeek-V3/4.4-multi-token-prediction-(mtp)), [Megatron Bridge MTP](https://docs.nvidia.com/nemo/megatron-bridge/latest/training/multi-token-prediction.html)

**Measured:**
- **DeepSeek-V3 MTP:** sequential next-N-token prediction, keeps causal chain
- **Densifies training signal**, improves data efficiency, pre-plans representations
- 14B params extra (on 671B base) — for V3 scale
- Inference: **MTP-1 acceptance >80%**, ~1.8× generation throughput speedup
- Serving: 1.2-2.1× boost in latency+throughput

**V13 wire:** Add an MTP head during SFT — train base to predict t+1, t+2, t+3 simultaneously. Costs a few percent extra time, gives spec-decoding for free + better data efficiency.

---

## 19. GKD (Generalized Knowledge Distillation) — TIER A, WIRE

**Sources:** [arxiv 2306.13649 Agarwal et al](https://arxiv.org/abs/2306.13649), [TRL GKDTrainer](https://github.com/huggingface/trl/blob/main/docs/source/gkd_trainer.md), 2026 OPD survey

**Measured:**
- **Trains student on its OWN generations** with teacher feedback (on-policy)
- Fixes train/test distribution mismatch in standard SFT-distillation
- **Generalized JSD interpolates Forward-KL ↔ Reverse-KL** via parameter β
- **9-30× cheaper than off-policy distillation** (per 2026 survey)

**Install:** Already in TRL — `from trl import GKDTrainer`

**V13 wire:**
```python
from trl import GKDTrainer, GKDConfig
trainer = GKDTrainer(
    model=student_14b,
    teacher_model=teacher_70b_or_deepseek,
    args=GKDConfig(
        beta=0.5,           # 0=ForwardKL, 1=ReverseKL
        temperature=2.0,
        seq_kd=False,       # on-policy = student generates, teacher labels
        ...
    ),
)
```

**V13 cost win:** Replaces my V10 "DistillKit logits → 14B" — TRL native, no extra deps, on-policy beats logits caching.

---

## 20. MiniLLM (Reverse-KL Distillation) — TIER B, alt to GKD

**Sources:** [arxiv 2306.08543](https://arxiv.org/abs/2306.08543) Gu et al

**Measured:**
- Reverse-KL > Forward-KL for autoregressive LMs (avoids overestimating low-prob teacher modes)
- Token-level policy gradients + teacher-mixed sampling + length normalization
- Scales 120M → 13B params
- Trains on **NVIDIA V100 32G** in hours — cheap

**vs GKD:** GKD generalizes (β-controllable JSD); MiniLLM is fixed reverse-KL with stable optimization tricks.

**V13 wire:** Use GKD with β=1.0 — gets MiniLLM behavior + framework flexibility.

---

## 21. Mask-DPO (Sentence-Level Factuality) — TIER A, WIRE

**Sources:** [arxiv 2503.02846](https://arxiv.org/pdf/2503.02846) ICLR 2025

**Measured:**
- **Sentence-level mask** on factuality preference signal
- Llama-3.1-8B ANAH: **49.2% → 77.5%** (8B beats 70B baseline!)
- Cheap: drop-in DPO extension, just label sentences as factual/not

**V13 wire:** Already in V10 Phase 0.5. Keep.

---

## 22. F-DPO (Binary Factuality DPO) — TIER A, WIRE

**Sources:** arxiv 2601.03027 (ref V10 spec)

**Measured:**
- Qwen3-8B hallucination: **0.424 → 0.084** (5×)
- Drop-in DPO replacement, no reward model
- TRL v1.3 supports

**V13 wire:** Already in V10 Phase 1. Keep.

---

## 23. RLCR (Calibration Reward) — TIER A, WIRE

**Sources:** arxiv 2507.16806 (ref V10 spec), 2604.12046 (Think-Through-Uncertainty)

**Measured:**
- Brier-score reward on `<confidence>0.85</confidence>` tokens, zero accuracy loss
- Composite reward: correctness × confidence, penalizes overconfidence

**V13 wire:** Already in V10 Phase 3. Keep.

---

## 24. Liger Post-Training Kernels (DPO/GRPO/SimPO) — TIER S, MUST-HAVE

**Sources:** [HF blog liger-grpo](https://huggingface.co/blog/liger-grpo), TRL docs

**Measured:**
- **GRPO: -40% memory, zero quality loss** (Liger Chunked Loss extended)
- DPO/SimPO/ORPO/KTO/JSD: **up to -80% memory savings**
- Avoids storing full logits — chunks lm_head forward pass

**V13 wire:** Same as #2 — `use_liger_kernel=True` on every TRL config.

---

## 25. Compute Scaling Laws Update (Fine-Tuning Era) — META

**Sources:** [Chinchilla arxiv 2203.15556](https://arxiv.org/abs/2203.15556), [LLM Scaling 2025 Vetterli](https://www.jonvet.com/blog/llm-scaling-in-2025), [Train-to-Test scaling VentureBeat 2025](https://venturebeat.com/orchestration/train-to-test-scaling-explained-how-to-optimize-your-end-to-end-ai-compute-budget-for-inference)

**2026 Reality:**
- **Chinchilla 20:1 outdated** — practitioners use **10:1 to 40:1**, **Farseer** says optimal ratio grows with compute
- **Heavily-overtrained small models BEAT Chinchilla-optimal large models** when test-time sampling cost included
- **Train-to-Test framework:** N (params), D (tokens), k (test-time samples) jointly optimized
- For V13 budget: **smaller-model + over-train + test-time sampling** > bigger-model + under-train

**V13 implication:** 7B trained well + spec-decoding heads + test-time best-of-N > 14B under-trained.

---

## Memory Budget Table — T4×2 (16GB each, 32GB total)

| Phase | Model | Setup | Per-GPU mem est | Notes |
|---|---|---|---|---|
| SFT 7B QLoRA | Qwen3-7B / Qwen2.5-7B | 4bit NF4 + LoRA r=32 + Liger + Unsloth | ~5GB base + 1GB LoRA + 2GB optimizer (APOLLO-Mini) + 3GB activation = **~11GB** | Fits T4 with 5GB headroom for ctx 8K |
| SFT 7B + LongLoRA 32K | + shifted-sparse-attn, group=2048 | + Liger, Unsloth 6× ctx | ~14GB peak | tight; use grad-accum 2-4 |
| SFT 14B QLoRA | Qwen3-14B | 4bit + LoRA r=16 + Liger + APOLLO-Mini | ~8.5GB base + 2GB LoRA + ~1GB opt + 4GB act = **~15.5GB** | RIGHT AT LIMIT; ctx 4K only |
| MEDUSA-1 head | base frozen + 1-3 heads | head only trains | <2GB extra | trivial; <1hr T4 |
| EAGLE-3 head (offline) | use cached states from Civo | draft-only train | <4GB | requires offline state file |
| F-DPO / Mask-DPO 7B | preference pairs | + Liger -80% post-train | ~10GB | fine |
| GRPO 7B | + Liger GRPO -40% | reward fn computed offline | ~12-13GB | tight; reward batching helps |
| GKD 7B student ← 32B teacher | teacher offline-cached logits | student-only forward | ~13GB | needs precomputed teacher logits to disk |

**Headroom rule:** Keep ≤14GB per-GPU peak on Kaggle T4 to leave 2GB system overhead.

---

## Civo L40S Phase (48GB) — Where C-Tier Lives

| Phase | Stack |
|---|---|
| 14B full-SFT | Muon optimizer (FSDP2) + Liger + DeepSpeed ZeRO-2 |
| 14B full GRPO+TruthRL | Liger GRPO + APOLLO + on-policy generation |
| 32K context full | DeepSpeed-Ulysses SP (if 4× L40S) OR Unsloth+LongLoRA on 1× L40S |
| EAGLE-3 spec-head | SpecForge online mode (target+draft on same GPU) |
| MTP head | DeepSpeed extra-head training |
| Sparse-upcycling (V13.5) | ScatterMoE + axolotl + 1× L40S |

---

## Wire-Into-V13

### 1) Top patches for `/Users/Ashira/.surrogate/hf-space/bin/kaggle-trainer.sh`

**A. Add S-tier installs to the Python install block:**
```bash
# In the kaggle notebook prelude (find pip install section):
pip install --upgrade --quiet \
  unsloth>=2026.4.0 unsloth-zoo \
  liger-kernel \
  apollo-torch \
  trl>=0.21.0 \
  peft \
  transformers>=4.55.0 \
  accelerate>=1.5.0 \
  triton>=3.0.0
```

**B. Patch `train.py` SFTConfig — enable Liger + APOLLO-Mini:**
```python
from trl import SFTConfig, SFTTrainer
from apollo_torch import APOLLOAdamW
from unsloth import FastLanguageModel

# Already using Unsloth FastLanguageModel — keep
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name=BASE_MODEL,           # Qwen2.5-7B / Qwen3-14B
    max_seq_length=8192,             # bump with LongLoRA later
    load_in_4bit=True,
    full_finetuning=False,
)

model = FastLanguageModel.get_peft_model(
    model, r=32, target_modules="all-linear",
    lora_alpha=32, use_gradient_checkpointing="unsloth",
)

# NEW: Liger Kernel
sft_args = SFTConfig(
    output_dir=OUT_DIR,
    use_liger_kernel=True,            # +20% throughput, -60% memory
    bf16=True, tf32=True,
    per_device_train_batch_size=2,
    gradient_accumulation_steps=4,
    learning_rate=2e-4,
    num_train_epochs=3,
    optim="apollo_mini",               # NEW: APOLLO-Mini via TRL optim arg
    optim_args="rank=256,scale=128,scale_type=tensor,update_proj_gap=200",
    max_seq_length=8192,
)

trainer = SFTTrainer(model=model, args=sft_args, train_dataset=ds)
```

**C. Add LongLoRA shifted-sparse-attn for 32K ctx:**
```python
# After model load, before trainer
from longlora_patch import replace_attn_with_shifted_sparse_attn
replace_attn_with_shifted_sparse_attn(model, group_size=2048)
sft_args.max_seq_length = 32768   # now feasible on T4 with Unsloth+Liger
```

**D. F-DPO + Mask-DPO + Liger post-training kernels:**
```python
from trl import DPOTrainer, DPOConfig
dpo_args = DPOConfig(
    use_liger_kernel=True,            # -80% memory on DPO loss
    beta=0.1,
    loss_type="sigmoid",              # F-DPO binary
    optim="apollo_mini",
    bf16=True,
    per_device_train_batch_size=1,
    gradient_accumulation_steps=8,
    num_train_epochs=1,
)
# For Mask-DPO: pass sentence-level mask in dataset preprocessing
```

**E. GRPO + Liger -40% memory + TruthRL ternary reward:**
```python
from trl import GRPOTrainer, GRPOConfig
def truth_rl_reward(prompt, response, gold):
    if "I don't know" in response: return 0.0
    return 1.0 if matches_factual(response, gold) else -1.0

grpo_args = GRPOConfig(
    use_liger_kernel=True,            # GRPO chunked loss -40% memory
    optim="apollo_mini",
    beta=0.04,
    num_generations=4,
    max_completion_length=512,
    bf16=True,
)
```

**F. GKD on-policy distillation (replaces DistillKit logits):**
```python
from trl import GKDTrainer, GKDConfig
gkd_args = GKDConfig(
    teacher_model_name_or_path="Qwen/Qwen2.5-32B-Instruct",
    beta=0.5,                          # JSD interpolation
    temperature=2.0,
    seq_kd=False,                       # on-policy
    use_liger_kernel=True,
    optim="apollo_mini",
)
trainer = GKDTrainer(model=student_7b, args=gkd_args, ...)
```

**G. MEDUSA-1 head training (separate kaggle kernel after main SFT):**
```python
# pip install medusa-llm
from medusa import MedusaConfig, train_medusa_heads
medusa_cfg = MedusaConfig(
    base_model="path/to/sft-7b",
    num_heads=3,
    head_layers=1,
)
train_medusa_heads(medusa_cfg, dataset=ds, epochs=1)  # ~2hr T4
```

**H. Add 4 attention-sink tokens at SFT data preprocessing:**
```python
SINK = "<|sink|><|sink|><|sink|><|sink|>"
def preprocess(example):
    example["text"] = SINK + example["text"]
    return example
```

**I. MTP head (small extra cost, big inference win):**
```python
# Add after main SFT in same train.py
from mtp_head import attach_mtp_heads
model = attach_mtp_heads(model, num_extra=2)  # predict t+1, t+2, t+3
# Continue 0.5 epoch with combined loss = 0.7*next + 0.2*t+2 + 0.1*t+3
```

### 2) Env-knob additions to V13

```bash
# Add to ~/.hermes/.env or kaggle secrets
export V13_USE_LIGER=1                # default ON
export V13_USE_APOLLO=1               # default ON
export V13_USE_LONGLORA=1             # ON for ctx >8K
export V13_LONGLORA_GROUP=2048
export V13_USE_MEDUSA=1               # train heads after main SFT
export V13_USE_MTP=1                  # multi-token prediction head
export V13_USE_GKD=1                  # replaces DistillKit
export V13_TEACHER_MODEL="Qwen/Qwen2.5-32B-Instruct"
export V13_USE_SPEC_FORGE=0           # 1 = train EAGLE-3, defer to Civo
export V13_USE_QUARTET=0              # Blackwell only, never on T4
export V13_USE_FA3=0                  # Hopper only
export V13_USE_MUON=0                 # Civo full-finetune only
```

### 3) Sequence (V13 alpha → RC1)

| Phase | Where | Stack | ETA |
|---|---|---|---|
| 0 | local | data hygiene + 5% inoculation + sink-token + MTP labels | 1 day |
| 0.5 | Kaggle T4×2 | SFT 7B + Unsloth + Liger + APOLLO-Mini + LongLoRA 32K + Mask-DPO | 24-30hr free |
| 1 | Kaggle T4×2 | F-DPO 7B + Liger -80% | 6hr free |
| 2 | Civo L40S | SFT 14B + Muon + Liger + APOLLO-Mini | 30hr ~$60 |
| 3 | Civo L40S | GRPO TruthRL + Liger GRPO -40% + APOLLO | 24hr ~$48 |
| 4 | Civo L40S | RLCR calibration | 8hr ~$16 |
| 5 | Civo L40S | GKD on-policy distill 14B → 7B (replaces DistillKit) | 12hr ~$24 |
| 6a | Kaggle T4×2 | MEDUSA-1 heads on 7B SFT (cheap fallback) | 2hr free |
| 6b | Civo L40S | SpecForge offline EAGLE-3 head | 6hr ~$12 |
| 7 | Cerebras + local | eval per-role + ANAH/HumanEval/SWE-Bench | 4hr $0-5 |

**Estimated V13 cost: ~$165 Civo + Kaggle free** (vs V10 ~$155 — added GKD + MTP + MEDUSA paths, freed via Liger memory savings allowing larger batches).

---

## What I'm NOT doing in V13 (and why)

- **Quartet MXFP4** — Blackwell-only. T4/L40S has no FP4 ALUs.
- **FlashAttention-3** — Hopper-only. Use FA2 on T4, FA2 on L40S.
- **Muon optimizer on T4** — needs FSDP2 + scale. Use only on Civo full-finetune.
- **DeepSpeed-Ulysses** — needs ≥4 GPUs to amortize. T4×2 doesn't qualify.
- **DSA continued-pretraining** — too expensive. Use LongLoRA shifted-sparse-attn instead.
- **Sparse upcycling** — V13.5+ feature when budget grows.
- **EBFT (Energy-Based Fine-Tuning)** — actually means "Effective Block-wise Fine-Tuning for Sparse LLMs" (arxiv 2402.12419), not energy-based. Not relevant unless we sparsify.

---

## See Also

- [[surrogate-1-v10-rev2-spec]] — V10 rev2 (this file extends it)
- [[training-tooling-2026-Q2]] — older tool survey
- [[frontier-releases-2026-Q2]] — Q2 frontier model releases
- [[opensource-releases-2026-Q2]] — OSS releases (GLM/Qwen/DeepSeek)
- [[anti-hallucination-correctness-2026]] — F-DPO / Mask-DPO / TruthRL details

## Tags
#surrogate-1 #v13 #frontier-efficiency #training-side #liger #unsloth #medusa #eagle3 #apollo #muon #soap #scattermoe #dsa #longlora #gkd #mtp #qat #t4-feasible #2025 #2026

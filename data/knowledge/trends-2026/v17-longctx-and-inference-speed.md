---
title: V17 Long-Context Native + Inference-Speed Training Recipes
date: 2026-05-01
project: surrogate-1
version-target: V17
predecessor: V16 (8K train / 16K YaRN serve, no spec-decoding)
tags:
  - long-context
  - speculative-decoding
  - eagle-3
  - medusa
  - yarn
  - rope
  - nope
  - mamba
  - ssm
  - kda
  - dsa
  - quartet
  - mxfp4
  - training-recipe
  - surrogate-1
  - v17
status: research-complete
---

# V17 Catch-Up Plan: Long-Context Native + Inference-Speed Training

V16 baseline weaknesses:
1. **Long context** — trains 8K, serves 16K via YaRN. Specialty leaders train native 32K-128K, serve 256K-4M.
2. **Inference speed** — no draft head. EAGLE-3-trained 7-9B do 2-6.5x faster.

This document maps every realistic 2024-2026 training-side technique against V17 budget (T4×2 = 32GB, ~2hr per training run, distill on V16 final ckpt).

## Decision Matrix (TL;DR)

| Track | Best path V17 | T4×2 fit | Lift |
|---|---|---|---|
| Long ctx (continued-pretrain) | NoPE-every-4 + YaRN θ↑ + LongAlign-10k SFT | LoRA-only feasible | 8K→32K serve, 64K via YaRN |
| Long ctx (free, swap base) | **Granite-4.1-8B Hybrid (native 512K)** | inference only | 16K→512K serve, no train cost |
| Inference speed | **EAGLE-3 head via SpecForge (online)** | ~4-6hr T4×2 LoRA-distill | 2-3x TPS, lossless |
| Inference speed (cheaper) | Medusa-1 head | ~1.5-2hr T4×2 | 2.0-2.2x, also lossless |

---

## == AXIS 1: LONG-CONTEXT NATIVE TRAINING ==

### 1.1 YaRN (Yet another RoPE extensioN)
- **Paper**: arXiv 2309.00071 (ICLR 2024). Repo: github.com/jquesnelle/yarn
- **Recipe**: Modify RoPE freq base, fine-tune 400 steps on 32K-chunked PG19 with `s=16` scale factor. 10x fewer tokens, 2.5x fewer steps than vanilla position interp.
- **Train-side patch (HF transformers)**:
  ```python
  config.rope_scaling = {"type": "yarn", "factor": 4.0,
                         "original_max_position_embeddings": 8192}
  config.max_position_embeddings = 32768
  ```
- **V17 fit**: ✅ already used at serve-time (V16 16K). Move to train-time → 32K continued-pretrain on 1-2B tokens.
- **T4×2 feasibility**: with LoRA + DeepSpeed Zero-3 + grad-ckpt, 32K training on 8B model is borderline. Recommend 16K continued-pretrain → YaRN s=2 to 32K serve.

### 1.2 RoPE↔NoPE Hybrid (Cohere, NeurIPS 2025)
- **Paper**: "Rope to Nope and Back Again" arXiv 2501.18795 (Yang et al., Cohere — NOT Princeton/AWS as originally noted).
- **Recipe**: Alternate NoPE / RoPE+SWA per layer. NoPE layers do retrieval; RoPE layers do local context.
- **Drop-in patch**:
  ```python
  # in modeling_*.py forward() per layer
  if layer_idx % 2 == 0:  # or % 4 = SmolLM3 variant
      q, k = apply_rotary(q, k, cos, sin)  # RoPE
  else:
      pass  # NoPE — no positional rotation
  ```
- **V17 fit**: ✅ V16 already has NoPE option. Activate in V17 + extended training.

### 1.3 NoPE-every-4th-layer (SmolLM3, HuggingFace, 2025)
- **Source**: HF SmolLM3 blueprint + RoPE-to-NoPE paper.
- **Recipe**: Skip RoPE on every 4th layer. Then 2-stage context extension:
  1. 4K → 32K, RoPE θ = 1.5M, 50B tokens
  2. 32K → 64K, RoPE θ = 5M, 50B tokens
- **V17 fit**: ✅✅ Direct port. V16 has NoPE flag. Apply `if layer_idx % 4 == 3: skip rope`. Combine with YaRN for 64K serve.
- **T4×2 feasibility**: Continued-pretrain on 100M-500M tokens at 16K (LoRA) achievable in ~12-24hr; full SmolLM3 100B-token recipe out-of-budget but partial recipe works.

### 1.4 LongLoRA (CUHK + MIT, ICLR 2024 Oral)
- **Paper**: arXiv 2309.12307. Repo: github.com/dvlab-research/LongLoRA
- **Recipe**: S²-Attn (Shifted Sparse Attention) — 2-line training-time patch enables 32K on Llama-7B with 1×8 A100 (or smaller scale).
  ```python
  # in attention forward — shift half the heads by group_size/2
  qkv[:, :, :num_heads//2] = qkv[:, :, :num_heads//2].roll(-G//2, dims=1)
  ```
- **V17 fit**: ✅✅✅ **THIS IS THE T4×2 PATH**. Designed for budget GPUs. LoRA on attention + embedding + norm.
- **T4×2 feasibility**: Llama-7B 32K LoRA on single 24GB GPU shown in paper. T4×2 (32GB total) → 7-9B at 16K-32K achievable.

### 1.5 ProLong (Princeton, 2024)
- **Paper**: arXiv 2410.02660 (ACL 2025). Repo: github.com/princeton-nlp/ProLong
- **Recipe**: Continued-pretrain Llama-3-8B → 512K. RoPE θ: 8e6 (64K) → 1.28e8 (512K). Disable cross-document attention. 20B tokens at 64K + 20B at 512K. 50/50 mix code, 83/17 mix natural language.
- **V17 fit**: ⚠️ requires 8×80GB GPUs minimum per official scripts. **Out of T4×2 budget**.
- **Alternative**: use ProLong's *RoPE θ schedule + data mix recipe* on partial scale (e.g. 100M tokens at 32K).

### 1.6 LongAlign (THUDM, EMNLP 2024)
- **Repo**: github.com/THUDM/LongAlign — already in V15 data per V15 notes.
- **Asset**: LongAlign-10k dataset (10K instructions, 8K-64K length).
- **Recipe**: SFT with packing + loss weighting OR sorted batching. Released models: Llama-2-7B-64K, ChatGLM3-6B-128K.
- **V17 fit**: ✅✅ Use as SFT stage *after* continued-pretrain context extension. Already in pipeline.

### 1.7 Activation Beacon / Long Context Compression (Microsoft, 2024)
- **Paper**: arXiv 2401.03462. Microsoft Research.
- **Recipe**: Compress KV directly (not soft prompts). Random compression ratio per train step (4x, 8x, 16x). Trained via compression-based auto-regression on plain text + instructions.
- **Benefit**: 4K→400K context, 2x inference speedup, 8x KV cache memory reduction.
- **V17 fit**: ⚠️ requires custom modeling code. Higher complexity than NoPE/YaRN. Defer to V18 unless critical.

### 1.8 InfLLM (Training-Free, NeurIPS 2024)
- **Approach**: StreamingLLM attention-sink + retrieval from offloaded global KV cache (host RAM). NO training.
- **V17 fit**: ✅ pure inference-time — orthogonal to training plan. Stack on top.

### 1.9 Lightning Attention 7:1 Hybrid (MiniMax-01, 2025)
- **Paper**: arXiv 2501.08313 ("MiniMax-01") + 2506.13585 ("MiniMax-M1").
- **Recipe**: 7 transnormer (lightning-attn) blocks per 1 softmax-attn block. Trained 1M ctx, serves 4M. 456B/45.9B MoE.
- **V17 fit**: ❌ architectural rewrite. 456B not feasible. Recipe useful for V18+ if base swap considered.

### 1.10 Kimi Delta Attention (KDA) 3:1 Hybrid (Moonshot, Oct 2025)
- **Paper**: arXiv 2510.26692. Repo: github.com/MoonshotAI/Kimi-Linear
- **Recipe**: 3 KDA layers per 1 MLA full-attn layer. Channel-wise gating extending Gated DeltaNet. Pretrained 48B/3B.
- **Benefits**: -75% KV cache, 6.3x decoding throughput, beats full-attention baselines.
- **V17 fit**: ❌ architectural — same constraint as Lightning Attn.

### 1.11 Granite-4.1 Mamba 9:1 Hybrid (IBM, Oct 2025-Nov 2026)
- **Source**: ibm.com/granite Granite 4.1 announcement, arXiv tech report.
- **Architecture**: Hybrid Mamba2 + Transformer. 8B dense Granite-4.1 matches Granite-4.0 32B-MoE. Native 512K context, Apache 2.0, 5-phase training pipeline published.
- **V17 fit**: ✅✅✅ **BIGGEST WIN — base swap = 512K for free**. No continued-pretrain needed. Drop V16 distill on Granite-4.1-8B base.
- **Trade**: must reproduce V16 distill targets on Granite tokenizer; not trivial but still 10-50x cheaper than 512K continued-pretrain.

### 1.12 Hunyuan-TurboS AMF/MF Block (Tencent, May 2025)
- **Paper**: arXiv 2505.15431. Repo: github.com/Tencent-Hunyuan/Hunyuan-TurboS
- **Architecture**: 128-layer hybrid: 57 Mamba2 + 7 Attention + 64 FFN, AMF (Attn→Mamba2→FFN) + MF (Mamba2→FFN) pattern. 56B/560B MoE, 16T tokens, 256K ctx.
- **V17 fit**: ❌ scale + architectural complexity — defer.

### 1.13 Mamba2 / LFM2 / Pure SSM
- **Mamba2**: arXiv 2405.21060 — 2-8x faster than Mamba1. Linear scaling.
- **LFM2**: arXiv 2511.23404 (Liquid AI, Nov 2025) — gated convolutions + GQA, edge-deployed.
- **V17 fit**: ❌ pure SSM has known weaknesses on copy/ICL tasks. Hybrids preferred. Granite-4.1 already gives this benefit.

### 1.14 DeepSeek Sparse Attention DSA (DeepSeek-V3.2, Dec 2025)
- **Paper**: arXiv 2512.02556. Repo: github.com/deepseek-ai/DeepSeek-V3.2-Exp
- **Recipe**: Lightning indexer warm-up = 1000 steps × 16 seqs × 128K = 2.1B tokens. Then full DSA training on top. Reduces O(L²) → O(Lk).
- **V17 fit**: ❌ V3.2 is 600B+ MoE, recipe non-trivial to port. DSA indexer pattern interesting for V18.

### 1.15 Continued Pretraining Best Practices (2024-2026 distilled)
- RoPE θ schedule: 10K → 1.5M (32K) → 5M (64K) → 8e6 (64K stable) → 1.28e8 (512K) — match target ctx.
- Data mix: ~50% code, ~30-50% natural-long (books, papers), ~10-20% short instruction.
- Disable cross-document attention.
- Two-stage: short-to-medium then medium-to-long, ~50B tokens each (or scale down 100x for budget).
- Eval: HELMET, RULER, NIAH, LongBench-Chat — V17 should run RULER as gate.

---

## == AXIS 2: INFERENCE-SPEED TRAINING ==

### 2.1 MEDUSA-1 / MEDUSA-2 (Together AI / FasterDecoding, Jan 2024)
- **Paper**: arXiv 2401.10774. Repo: github.com/FasterDecoding/Medusa
- **Architecture**: K extra "heads" predict tokens t+1, t+2, ..., t+K simultaneously. Tree-based draft verification.
- **MEDUSA-1**: heads only, frozen backbone. 2.0-2.2x speedup.
- **MEDUSA-2**: full-model + self-distillation. 2.2-3.6x speedup.
- **Training cost**: Vicuna-7B + Medusa heads = ~1.5-2hr on A100 40GB. **T4×2 (32GB total) feasible** at 7-9B with LoRA backbone or MEDUSA-1 mode.
- **V17 fit**: ✅ MEDUSA-1 is the cheapest path to 2x speedup. Heads-only train preserves V16 weights.

### 2.2 EAGLE-1 (ICML 2024) → EAGLE-2 (EMNLP 2024) → EAGLE-3 (NeurIPS 2025)
- **Paper trail**: 2401.15077 (EAGLE-1), 2406.16858 (EAGLE-2), arXiv 2503.01840 (EAGLE-3).
- **Repo**: github.com/SafeAILab/EAGLE
- **EAGLE-3 innovation**: Drops feature prediction. Direct token prediction via "training-time test" — at train time, feed back draft's own outputs to simulate inference. Replaces top-layer features with multi-layer fusion.
- **Speedup**: up to 6.5x (1.4x over EAGLE-2). Lossless (matches greedy/sampling outputs).
- **Draft head**: 1-2 transformer layers, conditioned on target's hidden states.
- **Training**: SafeAILab official: 16x A100 typical. ShareGPT 68K + UltraChat-200K 464K = ~532K examples.
- **Storage warning**: offline training requires 2-12 TB of pre-computed hidden states — infeasible on T4×2.
- **V17 fit**: ✅ ONLINE mode (target frozen, train draft alongside) — 1-2 GPU-days on 4x A100 → ~4-6hr T4×2 LoRA-distill if budget tight.

### 2.3 SpecForge (LMSYS / SGLang, Jul 2025)
- **Source**: lmsys.org/blog/2025-07-25-spec-forge. Repo: github.com/sgl-project/SpecForge
- **What**: Production training framework for EAGLE-3 heads, SGLang-integrated. Out-of-box scripts for Llama3/4, Qwen3-8B, Qwen3-30B-A3B, Qwen3-235B.
- **Modes**: online (target frozen, train draft jointly — recommended) vs offline (precompute hidden states first).
- **Performance**: up to 9.99x speedup measured. Released checkpoints on HF: `lmsys/SGLang-EAGLE3-Qwen3-30B-A3B-Instruct-2507-SpecForge-*`.
- **V17 fit**: ✅✅✅ **THE recommended pipeline**. Online mode + V16 backbone (LoRA-frozen) + SpecForge scripts → EAGLE-3 head trained in ~4hr T4×2.

### 2.4 Lookahead Decoding (Hao AI Lab, ICML 2024)
- **Paper**: arXiv 2402.02057. Repo: github.com/hao-ai-lab/LookaheadDecoding
- **Approach**: NO training. Parallel n-gram lookahead branch + verification branch. Drop-in.
- **V17 fit**: ✅ pure inference accelerator — stack OR test as MEDUSA/EAGLE alternative if training budget exhausted. Lower speedup than EAGLE-3 (typically 1.5-2x).

### 2.5 Kangaroo (NeurIPS 2024)
- **Paper**: arXiv 2404.18911. Repo: github.com/Equationliu/Kangaroo
- **Approach**: Self-speculative — fixed shallow sub-network of target as draft, train only adapter. Double early-exit.
- **Speedup**: 2.04x walltime, 88.7% fewer params than MEDUSA-1.
- **V17 fit**: ✅ if MEDUSA/EAGLE train fails, this is fallback — adapter is tiny.

### 2.6 Saguaro / Speculative Speculative Decoding (ICLR 2026)
- **Paper**: arXiv 2603.03251 (Kumar, Dao, May).
- **Approach**: Parallelize draft/verify dependence. While verify runs, draft predicts likely verify outcomes preemptively.
- **Speedup**: ~5x autoregressive baseline; +30% over best speculative baselines; up to 2x over optimized spec-decoding.
- **V17 fit**: ⚠️ orthogonal — stack on top of EAGLE-3 at inference time, no extra training.

### 2.7 Scaling Laws for Speculative Decoding
- **Paper**: arXiv 2505.07858. Log-linear scaling of acceptance rate vs (pretrain tokens, draft capacity, batch size).
- **V17 utility**: predict speedup of V17 EAGLE-3 head before training. Roughly: 1-2 layer draft → 2-3x for 8B target.

### 2.8 Spec-Bench (ACL 2024 Findings)
- **Repo**: github.com/hemingkx/Spec-Bench
- **Coverage**: 6 sub-tasks: MT-Bench, summarization, RAG, translation, QA, math reasoning. 80 instances each.
- **V17 utility**: standard eval gate before V17 release. Compare V17 EAGLE head vs MEDUSA-1 head fairly.

### 2.9 Quartet MXFP4 + Spec Decoding
- **Paper**: arXiv 2505.14669. MXFP4 for forward + backward. ~2x over FP8 forward, 1.6x backward on Blackwell RTX 5090.
- **Hardware**: Native NVFP4 tensor cores on Blackwell ONLY. Hopper (H100) has no NVFP4 — uses simulated FP4 with overhead.
- **V17 fit**: ❌ T4 = Turing (no FP4, no FP8). Quartet not applicable. Useful future when V17→V18 trains on H100/B200.

---

## V17 RECOMMENDED RECIPE (concrete)

### Path A: stay on V16 base, add NoPE-every-4 + EAGLE-3 head
```python
# 1) Long ctx — patch V16 base config
config.rope_scaling = {"type": "yarn", "factor": 2.0,
                       "original_max_position_embeddings": 8192}
config.max_position_embeddings = 16384  # 32K via YaRN serve
# add per-layer flag: skip RoPE if layer_idx % 4 == 3 (NoPE-every-4)

# 2) Continued-pretrain LoRA — 100-500M tokens at 16K
# data: 50% code, 30% long natural, 20% LongAlign-10k
# T4×2: ~12-24hr w/ DeepSpeed Zero-3 + grad-ckpt + LongLoRA S²-Attn

# 3) EAGLE-3 head via SpecForge (online mode)
# git clone https://github.com/sgl-project/SpecForge
# bash scripts/online/train_eagle3_qwen3_8b.sh \
#       --target-model /path/to/V16-final \
#       --draft-layers 1 --epochs 1 \
#       --train-data sharegpt_subset_50k.jsonl
# T4×2: ~4-6hr LoRA-distill, lossless 2-3x speedup
```

### Path B: swap base to Granite-4.1-8B (recommended if V16 distill is portable)
- Free 512K native context (no continued-pretrain)
- Hybrid Mamba2-Transformer 9:1
- Re-distill V16 targets on Granite tokenizer
- Add EAGLE-3 head via SpecForge (Granite scripts: in SpecForge roadmap, may need adaptation)

### Path C: minimum viable speedup (if EAGLE-3 fails)
- MEDUSA-1 heads only (frozen backbone) — ~1.5-2hr T4×2
- 2.0-2.2x lossless speedup
- Stack Lookahead Decoding at inference (no training, +1.5x stack)

---

## Risks / Open Questions

1. **EAGLE-3 LoRA-fine-tuned-base compatibility**: paper trains on full base model. V16 has LoRA adapters — re-merge LoRA into base before head training, OR train head on merged checkpoint. Search inconclusive whether SpecForge online-mode handles unmerged LoRA gracefully — assume merge required.
2. **MoE upcycled bases + EAGLE-3**: SpecForge has Qwen3-30B-A3B (MoE) checkpoints — works on MoE.
3. **T4 disk for offline mode**: 2-12 TB infeasible. Stick to ONLINE mode only.
4. **Granite-4.1 SpecForge support**: not yet in mainline. Watch SpecForge issues #486-style RFCs.
5. **NoPE-every-4 stability with YaRN**: combination not yet validated in published recipes — V17 should A/B test against pure-RoPE+YaRN baseline.

---

## Base Model Long-Context Comparison (May 2026)

| Base candidate | Params | Native train ctx | Serve ctx | Arch | License | V17 cost to swap |
|---|---|---|---|---|---|---|
| Qwen3-8B (V16 default) | 8B | 4K→32K (stage 3) | 32K, 128K via YaRN+DCA | Dense Transformer | Apache 2.0 | $0 (incumbent) |
| Granite-4.1-8B Hybrid | 8B | 512K native | 512K | Mamba2/Transformer 9:1 | Apache 2.0 | Re-distill V16 (~50% effort) |
| Olmo-3-7B-Base | 7B | up to 65K | 66K | Dense Transformer | Apache 2.0 | Re-distill (~50% effort) |
| Phi-4-mini-reasoning | 3.8B | 32K (16K base) | 32K | Dense Transformer | MIT | Smaller, possible quality drop |
| Qwen3-Next-80B-A3B | 80B / 3B-A | 32K→256K | 256K | MoE | Apache 2.0 | Massive compute upgrade |
| LLaMA-3-ProLong-8B-512K | 8B | 64K + 512K | 512K | Dense Transformer | Llama license | Re-distill, license check |
| MiniMax-M1 (456B/45.9B) | 456B / 45.9B | 1M | 4M | Lightning 7:1 hybrid MoE | MIT | Out of scale |
| Kimi Linear 48B-A3B | 48B / 3B-A | 1M+ | 1M+ | KDA 3:1 hybrid | Open | Out of scale |
| DeepSeek-V3.2 (DSA) | 671B-A37B | 160K | 160K | Sparse-attn MoE | MIT | Out of scale |

**V17 verdict**: Granite-4.1-8B is the only realistic base swap that buys 512K for free. Qwen3-8B incumbent path keeps tokenizer + V15-V16 pipeline compatible.

---

## Speculative Decoding Method Comparison (May 2026)

| Method | Year | Speedup | Train data | Train cost (T4×2 LoRA est.) | Lossless | Notes |
|---|---|---|---|---|---|---|
| Standard Spec Decoding (SpS) | 2022 | 1.5-2x | none (use small model) | n/a | yes | requires aligned tokenizer draft |
| MEDUSA-1 | 2024 | 2.0-2.2x | ShareGPT-like | ~1.5-2hr | yes | heads-only, V16 frozen |
| MEDUSA-2 | 2024 | 2.2-3.6x | ShareGPT + self-distill | ~6-12hr | yes | full-model fine-tune |
| EAGLE-1 | ICML'24 | 2.5-3.0x | ShareGPT 68K | ~2-4hr | yes | feature pred, 1-layer draft |
| EAGLE-2 | EMNLP'24 | 4.0-4.5x | + dynamic tree | ~3-5hr | yes | tree expansion improvement |
| EAGLE-3 | NeurIPS'25 | up to 6.5x | ShareGPT + UltraChat 532K | ~4-6hr (online LoRA) | yes | training-time test, multi-layer fusion |
| Lookahead Decoding | ICML'24 | 1.5-2x | NO training | $0 | yes | drop-in inference accelerator |
| Kangaroo | NeurIPS'24 | ~2.04x | adapter only | ~1hr | yes | self-spec, smallest params |
| Saguaro / Spec-Spec Dec | ICLR'26 | up to 5x AR / +30% over EAGLE | inference only | $0 | yes | parallelize draft/verify |
| SpecForge (LMSYS) | Jul 2025 | up to 9.99x measured | EAGLE-3 + extras | ~4-6hr | yes | production framework, Qwen3-ready |

---

## EAGLE-3 Online Training Concrete Recipe (V17 path)

```bash
# 1. Clone framework
git clone https://github.com/sgl-project/SpecForge && cd SpecForge
pip install -e .

# 2. Prepare data (target's own outputs, NOT canonical responses)
#    Regenerate ShareGPT user prompts through V16 final
python scripts/regenerate_responses.py \
    --target-model /path/to/V16-final-merged \
    --input data/sharegpt_68k.jsonl \
    --output data/sharegpt_68k_v16regen.jsonl \
    --max-new-tokens 1024

# 3. Train EAGLE-3 head — online mode (recommended for T4×2)
torchrun --nproc-per-node=2 train.py \
    --target-model /path/to/V16-final-merged \
    --draft-config configs/eagle3_qwen3_8b.yaml \
    --train-data data/sharegpt_68k_v16regen.jsonl \
    --num-draft-layers 1 \
    --learning-rate 5e-5 \
    --batch-size 2 \
    --grad-accum 16 \
    --max-steps 4000 \
    --save-dir checkpoints/v17-eagle3-head \
    --use-lora-on-target \
    --gradient-checkpointing

# 4. Eval on Spec-Bench
git clone https://github.com/hemingkx/Spec-Bench
cd Spec-Bench && python evaluation/spec_bench.py \
    --model-path /path/to/V16-final-merged \
    --draft-path ../SpecForge/checkpoints/v17-eagle3-head \
    --bench-name MT-Bench
```

### Expected output
- Spec-Bench MT-Bench: 2-3x speedup
- Math reasoning sub-task: 2.4-3.5x (highest gain)
- RAG sub-task: 1.8-2.2x
- All lossless vs greedy V16 outputs.

---

## Long-Context Continued-Pretrain Concrete Recipe (V17 path)

```bash
# Stage 1: 8K → 16K (LongLoRA S²-Attn + YaRN factor=2)
python train_longctx.py \
    --base-model /path/to/V16-final \
    --rope-scaling '{"type": "yarn", "factor": 2.0, "original_max_position_embeddings": 8192}' \
    --max-position-embeddings 16384 \
    --use-s2-attn --s2-group-size 2048 \
    --nope-layers '3,7,11,15,19,23,27,31' \
    --train-data slim-pajama-100B-mix.jsonl \
    --num-train-tokens 200_000_000 \
    --lora-rank 64 --lora-alpha 128 \
    --batch-size 1 --grad-accum 32 \
    --gradient-checkpointing --deepspeed-zero3
# T4×2 estimate: 12-18hr

# Stage 2: SFT on LongAlign-10k
python sft_longctx.py \
    --base-model checkpoints/v17-stage1 \
    --train-data hf://THUDM/LongAlign-10k \
    --packing --loss-weighting \
    --max-seq-len 16384
# T4×2 estimate: 4-6hr

# Stage 3 (eval): RULER + LongBench-Chat at 16K and YaRN-extrapolated 32K
python eval_ruler.py --model checkpoints/v17-stage2 --max-seq-len 32768
```

---

## Datasets Inventory for V17

| Dataset | Use | License | Size | Notes |
|---|---|---|---|---|
| ShareGPT 68K | EAGLE-3 head training | CC-BY-NC | ~68K convos | regen target with V16 |
| UltraChat 200K | EAGLE-3 head bigger train | MIT | 464K convos | optional, larger lift |
| LongAlign-10k (THUDM) | long-ctx SFT | open | 10K convos at 8-64K | already V15 |
| princeton-nlp/prolong-data-64K | long-ctx pretrain | open | ~20B tokens | for stage 1 |
| princeton-nlp/prolong-data-512K | long-ctx pretrain | open | ~20B tokens | only if extending past 64K |
| SlimPajama-627B | base mix | open | 627B tokens | trim to 100-500M for V17 |
| RULER eval | long-ctx benchmark | open | n/a | gate metric |
| Spec-Bench | spec-decoding eval | MIT | 480 instances | gate metric |
| HELMET | long-ctx unified eval | open | varies | optional |

---

## Order of Operations (suggested V17 sprint)

1. **Week 1 — base swap decision**: A/B distill V16 targets onto Granite-4.1-8B vs keep Qwen3-8B. Measure quality retention on V16 eval suite.
2. **Week 2 — EAGLE-3 head training**: SpecForge online mode, Qwen3-8B target (or Granite if A wins). Target: 2-3x Spec-Bench.
3. **Week 3 — long-ctx continued-pretrain** (only if Qwen3-8B path): LongLoRA S²-Attn + NoPE-every-4 + YaRN factor 2 → 16K. SFT on LongAlign-10k.
4. **Week 4 — eval gate**: Spec-Bench + RULER + LongBench-Chat. Compare against V16 baseline.
5. **Week 5 — Saguaro/Lookahead stack** at inference for additional 30%+ on top of EAGLE-3.

---

## See Also
- [[v13-frontier-efficiency]] (V13 baseline efficiency notes)
- [[opensource-releases-2026-Q2]] (Granite, Qwen3, Olmo3)
- [[training-tooling-2026-Q2]] (DeepSpeed, FlashAttention, FSDP)
- [[coding-llm-frontier]] (Qwen3-Coder, etc.)
- [[frontier-releases-2026-Q2]] (DeepSeek-V3.2 DSA, MiniMax-M1)
- Memory: [[project_surrogate1_state]]

## Sources
- [SafeAILab/EAGLE GitHub](https://github.com/SafeAILab/EAGLE)
- [EAGLE-3 paper arXiv 2503.01840](https://arxiv.org/abs/2503.01840)
- [SpecForge LMSYS blog Jul 2025](https://www.lmsys.org/blog/2025-07-25-spec-forge/)
- [SpecForge GitHub](https://github.com/sgl-project/SpecForge)
- [FasterDecoding/Medusa GitHub](https://github.com/FasterDecoding/Medusa)
- [MEDUSA paper arXiv 2401.10774](https://arxiv.org/pdf/2401.10774)
- [Lookahead Decoding GitHub](https://github.com/hao-ai-lab/LookaheadDecoding)
- [Kangaroo NeurIPS 2024 arXiv 2404.18911](https://arxiv.org/abs/2404.18911)
- [Saguaro / Spec-Spec Decoding ICLR 2026 arXiv 2603.03251](https://arxiv.org/abs/2603.03251)
- [Spec-Bench ACL 2024 GitHub](https://github.com/hemingkx/Spec-Bench)
- [Speculative Decoding Scaling Laws arXiv 2505.07858](https://arxiv.org/abs/2505.07858)
- [YaRN paper ICLR 2024 arXiv 2309.00071](https://arxiv.org/abs/2309.00071)
- [YaRN GitHub jquesnelle](https://github.com/jquesnelle/yarn)
- [LongLoRA ICLR 2024 arXiv 2309.12307](https://arxiv.org/abs/2309.12307)
- [ProLong Princeton GitHub](https://github.com/princeton-nlp/ProLong)
- [ProLong paper arXiv 2410.02660](https://arxiv.org/pdf/2410.02660)
- [LongAlign THUDM GitHub](https://github.com/THUDM/LongAlign)
- [Activation Beacon arXiv 2401.03462](https://arxiv.org/abs/2401.03462)
- [InfLLM NeurIPS 2024](https://proceedings.neurips.cc/paper_files/paper/2024/file/d842425e4bf79ba039352da0f658a906-Paper-Conference.pdf)
- [RoPE-to-NoPE Cohere arXiv 2501.18795](https://arxiv.org/abs/2501.18795)
- [SmolLM3 HuggingFace blog](https://github.com/huggingface/blog/blob/main/smollm3.md)
- [MiniMax-01 arXiv 2501.08313](https://arxiv.org/abs/2501.08313)
- [MiniMax-M1 arXiv 2506.13585](https://arxiv.org/abs/2506.13585)
- [Kimi Linear / KDA arXiv 2510.26692](https://arxiv.org/abs/2510.26692)
- [Kimi-Linear GitHub](https://github.com/MoonshotAI/Kimi-Linear)
- [Granite 4.1 IBM Research](https://research.ibm.com/blog/granite-4-1-ai-foundation-models)
- [Granite 4.0 IBM announcement](https://www.ibm.com/new/announcements/ibm-granite-4-0-hyper-efficient-high-performance-hybrid-models)
- [Hunyuan-TurboS arXiv 2505.15431](https://arxiv.org/abs/2505.15431)
- [Mamba2 SSD arXiv 2405.21060](https://arxiv.org/abs/2405.21060)
- [LFM2 arXiv 2511.23404](https://arxiv.org/html/2511.23404v1)
- [DeepSeek-V3.2 arXiv 2512.02556](https://arxiv.org/abs/2512.02556)
- [DeepSeek-V3.2-Exp GitHub](https://github.com/deepseek-ai/DeepSeek-V3.2-Exp)
- [Quartet MXFP4 arXiv 2505.14669](https://arxiv.org/html/2505.14669v4)
- [Qwen3 technical report arXiv 2505.09388](https://arxiv.org/pdf/2505.09388)
- [Olmo 3 AllenAI blog](https://allenai.org/blog/olmo3)
- [Phi-4 technical report](https://arxiv.org/html/2412.08905v1)

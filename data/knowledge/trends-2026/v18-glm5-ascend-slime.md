---
title: V18 — GLM-5 Base-Swap on Huawei Ascend with slime async RL
date: 2026-05-01
tags: [v18, surrogate-1, glm-5, glm-5.1, ascend-910b, slime, llama-factory, qdrant, langgraph, mcp, acp, async-rl, moe, fp8]
related:
  - "[[v14-kimi-deepseek-glm-deep]]"
  - "[[v17-moe-sublora-composition]]"
  - "[[training-tooling-2026-Q2]]"
  - "[[opensource-releases-2026-Q2]]"
  - "[[v16-tool-use-frontier]]"
status: research-spec
owner_review_required: true
---

# V18 — GLM-5 Base-Swap on Huawei Ascend with slime async RL

> **Owner directive (2026-05-01):** Surrogate-1 README states the **production target is GLM-5 (744B/40B active, MoE, FP8) + LLaMA-Factory QLoRA on Huawei Ascend 910B + slime async RL + Qdrant + LangGraph MCP/ACP**. Kaggle T4×2 + Qwen2.5-Coder-7B is a stepping-stone for **technique validation**, not the destination.
>
> **Mission:** translate "actual target architecture" into a concrete, costed, risk-evaluated transfer plan. Document every primary source, every install command, every cluster-size estimate, every fallback. **No magic numbers without a citation.**

---

## TL;DR — the four answers the owner needs

| # | Question | Answer (concrete) |
|---|----------|-------------------|
| **a** | GLM-5 base availability + training cost | **MIT-licensed, weights on HF (`zai-org/GLM-5`, `zai-org/GLM-5.1`, `zai-org/GLM-5-FP8`)**, FP8 inference on 8×H100 / 8×910B. **QLoRA fine-tune ≈ $300-$1,000** range generally; for 744B MoE specifically expect **~$2k-$5k for a meaningful task-specific LoRA** on H100×8 over 1-2 days, or **~50-70% of that on Ascend 910B×8** if/when access is available. Full base re-train is ~$10M-class — **out of scope**. |
| **b** | Ascend 910B vs H100 trade-off | **910B: 64GB HBM2e, ~320 TFLOPS FP16 (BF16 ~781 TF claimed by Huawei), 400 GB/s BW, 400W, NO native FP8 hardware on 910B (FP8 lands on 910C/D), CANN+MindSpore stack, ASCEND_RT_VISIBLE_DEVICES selection.** vs **H100: 989 TFLOPS FP16, ~3 TB/s HBM3, native FP8.** Baidu leak: 8×910B matches 8×H100 on Llama-2-70B with **+8% wall-clock**. **910B = ~60% raw H100 perf, but cheaper/chip in PRC + no NVIDIA export risk + officially proven on GLM-5's 100k-chip pretrain.** Outside PRC = effectively not rentable in 2026 — **assume H100/H200 for V18 training, save Ascend for if-and-when Huawei Cloud opens international ModelArts in 2026-Q3+.** |
| **c** | slime async RL setup walkthrough | **Docker is mandatory** (`rlsys/slime:latest` for ROCm or build from `Dockerfile.h100` for NVIDIA). Stack = Megatron-LM (training) + SGLang (rollout) + Ray (orchestration) + APRIL (long-tail rollout, +22.5% throughput). Architecture: decoupled GPU pools, training engine consumes from rollout buffer, R3 (Rollout Routing Replay) prevents MoE expert-mismatch collapse. **GLM-5 day-0 supported**, examples in `examples/` for GLM-4.5 / DeepSeek-V3. **Min cluster: 8 GPUs** (4 train + 4 rollout) for tiny POC; **GLM-5-class needs ≥32 GPUs** (16 train Megatron TP + 16 rollout SGLang TP). |
| **d** | Full transfer cost Kaggle 7B → GLM-5 production | **Phase A (validate techniques on 7B Kaggle)**: $0 (already done). **Phase B (port LoRA + RL recipe to GLM-5 inference + fine-tune)**: ~$2.5k-$8k one-time on Lambda/Civo H100/L40S. **Phase C (slime async RL, 5-10k steps)**: ~$3k-$15k on H100×16-32 for 24-72h. **Phase D (production inference/RAG/agent)**: ~$70-200/month serverless inference + Qdrant ECS. **Total V18 transfer: $5k-$25k one-time + $200/month run.** Compare current claim of "$30-60/month" — that's INFERENCE only, training was implicit and unbudgeted. **Real budget for V18 = $5-25k upfront, then ~$100-300/month steady-state.** |

---

## A. GLM-5 / GLM-5.1 — Architecture & Availability

### A.1 Release timeline & licensing

| Release | Date | Params | Active | Context | License | Hardware trained on |
|---------|------|--------|--------|---------|---------|---------------------|
| **GLM-4.5** | Aug 2025 (arXiv 2508.06471) | 355B MoE | 32B | 128K | MIT | Mixed |
| **GLM-4.6 / 4.7** | late-2025 / early-2026 | 355B MoE | 32B | 200K | MIT | Mixed |
| **GLM-5** | **Feb 11, 2026** | **744B MoE** | **40B** | **200K** | **MIT** | **100k Ascend 910B (zero NVIDIA)** |
| **GLM-5.1** | **Apr 7, 2026** | **754B MoE** | **40B** | **200K** | **MIT** | **100k Ascend 910B** |

- GLM-5 paper: ["GLM-5: from Vibe Coding to Agentic Engineering"](https://arxiv.org/html/2602.15763v1) (arXiv:2602.15763v1)
- HF: [`zai-org/GLM-5`](https://huggingface.co/zai-org/GLM-5), [`zai-org/GLM-5.1`](https://huggingface.co/zai-org/GLM-5.1), [`zai-org/GLM-5-FP8`](https://huggingface.co/zai-org/GLM-5-FP8)
- GitHub: [`zai-org/GLM-5`](https://github.com/zai-org/GLM-5)
- Blog: [Z.AI GLM-5.1 announcement](https://www.marktechpost.com/2026/04/08/z-ai-introduces-glm-5-1-an-open-weight-754b-agentic-model-that-achieves-sota-on-swe-bench-pro-and-sustains-8-hour-autonomous-execution/) (Apr 8 2026)

### A.2 Architecture details

- **256 experts, 80 layers, top-8 routing per token + 1 always-active shared expert**
- **GLM_MOE_DSA**: MoE + DeepSeek Sparse Attention (DSA) for 200K context without quadratic blow-up
- **FP8 native for rollout inference** (cuts rollout latency, used in slime's R3 replay)
- **MTP (multi-token prediction)** speculative decoding head supported in vLLM (`--speculative-config.method mtp`)

### A.3 Training recipe (from paper + companion Kili blog)

- **~28.5T tokens** total pretrain, two phases: ~27T general+code, then mid-training (long context + agentic) ramping to 200K
- **Reasoning RL** across math / science / code / TIR (tool-integrated reasoning)
- **Source-specific quality scoring + semantic dedup + stagewise mixture shifts** (extends GLM-4.5 recipe)
- Post-training pattern (GLM-4.5 baseline, GLM-5 extends): three "expert" training tracks → unified self-distillation → multi-stage filter (correctness verifier + reward model + tool-call protocol validation)
- **slime is the RL infra** — see §C

### A.4 Benchmarks (head-to-head)

| Bench | GLM-5 | GLM-5.1 | GPT-5.4 | Claude Opus 4.6 |
|-------|-------|---------|---------|-----------------|
| **SWE-Bench Verified** | 77.8 | — | — | — |
| **SWE-Bench Pro** | — | **58.4** (#1) | 57.7 | 57.3 |
| **AIME 2026** | 95.83 | 95.3 | — | — |
| **HMMT Feb 2026** | 86.36 | 82.6 | — | — |
| **HMMT Nov 2025** | — | 94.0 | — | — |

Source: [reeboot.fr GLM-5 review](https://reeboot.fr/en/blog/glm-5/), [winbuzzer GLM-5.1](https://winbuzzer.com/2026/04/09/z-ai-releases-glm-5-1-754b-model-tops-swe-bench-pro-xcxwbn/)

### A.5 Compute requirements (inference + fine-tune)

**Inference (FP8 quant):**
- 744B at FP8 ≈ 750 GB → fits **8× H100 80GB (640GB) is too small** → minimum **8× H200 (1.1TB)** or **8× MI300X (1.5TB)** or **8× 910B with 64GB×8 = 512GB → also too small at FP8**
- Practical: **16× H100 / 8× H200 / 16× 910B** for full-precision FP8 inference
- Aggressive INT4 quant fits on **4× H100** but degrades performance

**Fine-tune QLoRA (the V18 path):**
- LoRA adapters only (~50-200M params) → **8× H100 BF16 + 4-bit base quant works**
- DeepSpeed ZeRO-3 + offloading → **could fit on 4× H100 with patience**
- **Wall-clock estimate**: 7B → 7B LoRA = ~1-3h on Kaggle T4×2 (current). 744B → 744B LoRA on 8× H100 ≈ **24-72h for one good run** depending on dataset size (10k-100k pairs).

### A.6 Cost estimates (USD, May 2026 pricing)

| Path | GPUs | Hours | $/hr/GPU | Total | Notes |
|------|------|-------|----------|-------|-------|
| QLoRA 5k-pair adapter | 8× H100 SXM | 12 | $4.29 | **$412** | tiny domain LoRA |
| QLoRA 50k-pair adapter | 8× H100 SXM | 36 | $4.29 | **$1,236** | medium LoRA |
| QLoRA 50k-pair, ZeRO-3 | 8× H200 | 30 | $5-6 | **$1,500** | better headroom |
| Full LoRA on Ascend 910B (China only) | 8× 910B | 36 | ~$1.5-2.5* | **$432-720** | *estimated, no public price |
| slime async RL 5k steps | 16× H100 | 48 | $3.39 (PCIe) | **$2,604** | rollout-heavy |
| slime async RL 10k steps | 32× H100 | 60 | $3.39 | **$6,509** | full pipeline |
| Pretrain from scratch (FOR REFERENCE) | ~10k× H100 | 30 days | $3.39 | **$24M** | **out of scope** |

H100/L40S/H200 pricing: [Lambda](https://lambda.ai/pricing), [getdeploying](https://getdeploying.com/gpus/nvidia-h100). Civo L40S **$0.89/hr** with 36-month commit, **$1.29/hr on-demand**: [Civo L40S](https://www.civo.com/cloud-gpu/nvidia-l40s-gpu).

---

## B. Huawei Ascend 910B — Specs, Stack, Reality Check

### B.1 Hardware

| Spec | 910B | H100 SXM | Ratio |
|------|------|----------|-------|
| HBM | **64GB HBM2e** | 80GB HBM3 | 0.8× |
| Memory BW | **400 GB/s** | ~3000 GB/s | **0.13×** ← bottleneck |
| FP16 dense | **~320 TFLOPS** | 989 TFLOPS | 0.32× |
| BF16 (Huawei claim) | ~781 TFLOPS | 989 TFLOPS | 0.79× |
| FP8 native | **NO (910B)** / yes (910C/D) | YES | — |
| INT8 | 640 TOPS | ~3958 TOPS | 0.16× |
| Power | 400W | 700W | — |
| Process | SMIC N+2 (7nm-class) | TSMC 4N | — |
| Price/chip | ~110k RMB (~$15k) | $25-30k | 0.5-0.6× |

Sources: [WareDB Ascend 910B specs](https://www.waredb.com/processor/ascend-910b), [Tom's Hardware Ascend deep dive](https://www.tomshardware.com/tech-industry/artificial-intelligence/huaweis-homegrown-ai-chip-examined-chinese-fab-smic-produced-ascend-910b-is-massively-different-from-the-tsmc-produced-ascend-910), [Awesome Agents 910B](https://awesomeagents.ai/hardware/huawei-ascend-910b/), [letsdatascience GLM-5 on Huawei](https://letsdatascience.com/blog/china-trained-frontier-ai-model-glm-5-without-nvidia)

### B.2 Software stack (CANN + MindSpore + Pangu)

- **CANN Toolkit + Kernels** required (download from Ascend developer site, run install + source env script)
- **Python ≥ 3.10**
- **PyTorch via torch_npu** plugin OR **MindSpore native** (Huawei's framework)
- **DeepSpeed Accelerator Setup Guide** has Ascend section: [DeepSpeed Ascend](https://www.deepspeed.ai/tutorials/accelerator-setup-guide/)
- **Pangu** (Huawei's flagship, 1.085T params, trained 100+ days on 512× 910 with MindSpore — proves the stack works at scale): [Wikipedia PanGu](https://en.wikipedia.org/wiki/Huawei_PanGu)
- **openPangu** (open-source 7B + 72B MoE released Jun 2025) — runs on Ascend with optimized inference: [Huawei Pangu open-source](https://www.huaweicloud.com/intl/en-us/news/20230707180809498.html)

### B.3 LLaMA-Factory on Ascend (the V18 fine-tune target)

Source: [LLaMA-Factory NPU docs](https://llamafactory.readthedocs.io/en/latest/advanced/npu_training.html), PR [#6601 NF4 QLoRA](https://github.com/hiyouga/LLaMA-Factory/pull/6601)

```bash
# 1. Install CANN
# Download CANN Toolkit + Kernels for 910b from Huawei Ascend dev portal
bash Ascend-cann-toolkit_*.run --install
bash Ascend-cann-kernels-910b_*.run --install
source /usr/local/Ascend/ascend-toolkit/set_env.sh

# 2. Python env
conda create -n llamafac python=3.10 -y
conda activate llamafac

# 3. PyTorch + Ascend plugin
pip install torch==2.1.0 torch_npu==2.1.0.post*  # match CANN version
pip install -e ".[deepspeed,metrics]"  # from LLaMA-Factory repo

# 4. QLoRA on Ascend needs HAND-COMPILED bitsandbytes
# Requires cmake>=3.22.1, g++>=12.x
# Follow github.com/Dao-AILab/bitsandbytes-npu install docs

# 5. Run (note: ASCEND_RT_VISIBLE_DEVICES, NOT CUDA_VISIBLE_DEVICES)
ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
  llamafactory-cli train \
    --stage sft \
    --model_name_or_path zai-org/GLM-5 \
    --dataset surrogate_dev_sre \
    --finetuning_type lora \
    --quantization_bit 4 \
    --quantization_method bnb \
    --deepspeed examples/deepspeed/ds_z3_config.json \
    --bf16 true \
    --gradient_checkpointing true \
    --output_dir saves/glm5-lora-sre
```

**Risk:** GLM-5 may not have a `tokenizer_config.json` shim that LLaMA-Factory recognizes by name yet. If model_type isn't registered, must register custom template. Same risk as GLM-4.5 fine-tune in late-2025.

### B.4 Reality check: can we actually rent 910B?

- **Huawei Cloud ModelArts** offers Ascend resource pools, billing pay-per-use or yearly: [Huawei ModelArts pricing](https://support.huaweicloud.com/intl/en-us/price-modelarts/price-modelarts-0011.html)
- **International Huawei Cloud is limited.** Ascend 910B/910C clusters are largely PRC-only commercially. International ModelArts has Ascend SKUs but availability is opaque, no public US$/hr quote in May 2026.
- **No-NVIDIA alternative providers** (e.g., Tencent Cloud, Alibaba Cloud) have Ascend resource pools but require PRC business entity in most cases.
- **Realistic for non-PRC user (Ashira)**: V18 training **uses H100/H200/L40S** until Huawei opens international ModelArts pricing. Plan migration to Ascend for steady-state inference once price-per-token is meaningfully cheaper.

---

## C. slime — async RL framework (THE recipe for GLM-5 RL post-training)

### C.1 What slime is, exactly

**Source-of-truth:** [github.com/THUDM/slime](https://github.com/THUDM/slime), [thudm.github.io/slime](https://thudm.github.io/slime/), [LMSYS slime announcement (Jul 2025)](https://www.lmsys.org/blog/2025-07-09-slime/), [DeepWiki slime](https://deepwiki.com/THUDM/slime)

- **Author:** THUDM (Tsinghua University KEG / Z.AI). slime is the **official RL infra behind GLM-4.5/4.6/4.7/5/5.1**.
- **Stack:** Megatron-LM (training) + SGLang + router (rollout) + Ray (resource scheduler) + Apex + Torch Memory Saver
- **Modes:** synchronous (co-located GPU) **or** asynchronous (decoupled GPU pools — the GLM-5 production mode)
- **MoE-safe:** **Rollout Routing Replay (R3)** — records expert routing during SGLang inference, replays during training to ensure bit-wise expert alignment. Without R3, MoE RL collapses (Qwen3, DeepSeek-V3 evidence). **R3 is the unlock for >355B MoE RL.**
- **End-to-end FP8** (inference + training in same precision) — Miles fork extends this further (§C.6)
- **Models day-0 supported:** GLM family (4.5/4.6/4.7/5/5.1), Qwen series (Qwen2.5/3/3MoE/3Next/3.5/3.6), DeepSeek-V3/V3.1

### C.2 Architecture (decoupled async)

```
                ┌──────────────────────────┐
                │     Ray Cluster          │
                │  (resource scheduler)    │
                └──────────┬───────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                                     ▼
 ┌──────────────┐                     ┌──────────────┐
 │ TRAIN GROUP  │  weight sync        │ ROLLOUT GROUP│
 │ Megatron-LM  │ ◄─────────────────► │ SGLang+router│
 │ (8-128 GPU)  │                     │ (8-128 GPU)  │
 └──────┬───────┘                     └──────┬───────┘
        │                                    │
        │  reads trajectories                │ writes
        │                                    │ trajectories
        │                                    ▼
        │                            ┌──────────────┐
        │                            │ DATA BUFFER  │
        └────────────────────────────┤ (Ray object  │
                                     │  store)      │
                                     └──────────────┘
                                          ▲
                                          │
                                     ┌────┴─────┐
                                     │ Reward / │
                                     │ Verifier │
                                     └──────────┘
```

**Cycle:** Generation → Reward/Verify → Buffer Push → Train Pull → Train Step → Weight Sync → repeat. Train and Rollout run **independently in async mode** — train doesn't block on rollout, rollout doesn't block on train. APRIL adds smart cancellation of long-tail rollouts.

### C.3 APRIL — Active Partial Rollouts (2026 paper)

**Source:** [arXiv:2509.18521 APRIL](https://arxiv.org/abs/2509.18521), [APRIL GitHub](https://github.com/RLsys-Foundation/APRIL)

- The "long-tail rollout problem": a few slow trajectories block 90%+ of RL training time. Idles hundreds of GPUs.
- APRIL **over-provisions rollout requests, lets training step proceed when *enough* rollouts done**, kills stragglers, replaces with fresh prompts.
- **Result:** **+22.5% rollout throughput average, +44% peak**, **+2.1% accuracy avg, +8% peak** across GRPO/DAPO/GSPO algorithms. Hardware-agnostic (NVIDIA + AMD).
- **Already integrated in slime** as of late-2025/early-2026.

### C.4 Install (Docker mandatory)

```bash
# OPTION 1: Docker (RECOMMENDED — pre-built env)
docker pull rlsys/slime:latest    # AMD ROCm variant
# or build NVIDIA variant from source:
git clone https://github.com/THUDM/slime
cd slime
docker build -f docker/Dockerfile.h100 -t slime:nvidia-h100 .

# OPTION 2: bare metal (BRITTLE — temporary patches to Megatron + SGLang)
git clone https://github.com/THUDM/slime
cd slime
# pin to a known-good commit if main is unstable:
git checkout 0934a0e
pip install -e .
# Megatron-LM and SGLang with slime's patches applied
```

**Hardware support:**
- NVIDIA: H100, H200, B200 (full CI), B-series identical setup to H-series
- AMD: MI300X, MI325X (ROCm via `rlsys/slime`)
- Ascend: **NOT officially supported in slime as of May 2026** — Z.AI uses internal Huawei infra, not the same slime stack as the open-source repo. Open-source slime = NVIDIA/AMD only. **This is a key V18 constraint.**

### C.5 Quick-start config (GLM-4.5/5 RL)

From [slime quick_start docs](https://github.com/THUDM/slime/blob/main/docs/en/get_started/quick_start.md):

```bash
# 1. Set up Ray cluster
ray start --head --num-gpus=16

# 2. Launch slime training (decoupled mode)
python tools/run_slime.py \
  --model-name zai-org/GLM-5 \
  --tensor-model-parallel-size 8 \
  --pipeline-model-parallel-size 1 \
  --expert-model-parallel-size 8 \
  --actor-num-nodes 1 \
  --actor-num-gpus-per-node 8 \
  --rollout-num-gpus 8 \
  --num-rollout 3000 \
  --rollout-batch-size 16 \
  --n-samples-per-prompt 8 \
  --num-steps-per-rollout 1 \
  --global-batch-size 128 \
  --sglang-mem-fraction-static 0.85 \
  --rollout-routing-replay \
  --april-enable \
  --april-overprovision-ratio 1.5 \
  --rm-type custom \
  --custom-rm-path my_rewards.py
```

**Key knobs:**
- `--num-rollout`: total rollout iterations
- `--rollout-batch-size × --n-samples-per-prompt`: batch shape per iter
- `--num-steps-per-rollout`: train steps per rollout batch (1 = pure async, >1 = trades stability)
- `--global-batch-size`: effective batch size for gradient
- All SGLang args prefixed `--sglang-*`, all Megatron args passed through directly

### C.6 Miles — enterprise fork of slime

**Source:** [github.com/radixark/miles](https://github.com/radixark/miles), [LMSYS Miles announcement (Nov 2025)](https://www.lmsys.org/blog/2025-11-19-miles/), [DeepSeek-V4 Day-0 with Miles+SGLang](https://www.lmsys.org/blog/2026-04-25-deepseek-v4/)

- **Why fork:** slime is research-grade — Miles adds production reliability for large-scale MoE post-training
- **First-of-its-kind end-to-end FP8 sampling+training** — eliminates RL collapse from quantization-induced drift
- **Production R3** with stronger guarantees, scheduler hardening
- **Day-0 RL for DeepSeek-V4** (April 2026) — battle-tested at frontier scale
- **ROCm-native** (March 2026) — runs on AMD Instinct out of the box
- **Decision rule for V18:** start with slime (research mode, faster iteration); if slime training collapses on GLM-5 MoE, switch to Miles (production hardening)

### C.7 Realistic min-cluster for GLM-5 slime RL

| Cluster | Train GPU | Rollout GPU | Mode | Step/min | $/hr | Notes |
|---------|-----------|-------------|------|----------|------|-------|
| **Toy POC (LoRA only)** | 4× H100 | 4× H100 | sync | low | ~$32 | proves recipe runs |
| **Realistic V18 floor** | 8× H100 (TP=8, EP=8) | 8× H100 | async | medium | ~$64 | LoRA RL, slow but works |
| **V18 target** | 16× H100 | 16× H100 | async + APRIL | high | ~$128 | actual GLM-5 LoRA RL |
| **Z.AI scale (REFERENCE)** | 5000× 910B | 5000× 910B | async | massive | n/a | full pretrain — out of scope |

**Note:** for LoRA RL (vs full SFT/RLHF), the train side parallelism can be lighter — often 8 GPUs with TP=8 + adapter-only updates is sufficient. Rollout side scales with desired throughput.

---

## D. LLaMA-Factory ≥0.10 — the QLoRA driver

**Source:** [hiyouga/LLaMA-Factory](https://github.com/hiyouga/LlamaFactory), [PyPI llamafactory](https://pypi.org/project/llamafactory/), [NPU training docs](https://llamafactory.readthedocs.io/en/latest/advanced/npu_training.html)

### D.1 Capabilities relevant to V18

- **100+ LLMs supported** (GLM, Qwen, DeepSeek, LLaMA, Mistral, Pangu, etc.)
- **2/3/4/5/6/8-bit QLoRA** via AQLM/AWQ/GPTQ/LLM.int8/HQQ/EETQ (also **NF4 on Ascend**, PR #6601)
- **DeepSpeed ZeRO-1/2/3** with offloading
- **Full SFT, LoRA, DoRA, PiSSA, LoRA+, QLoRA, RLHF (PPO/DPO/KTO/ORPO), GRPO**
- **Multi-modal SFT** (vision LoRAs)
- **Auto-template detection** from model card

### D.2 GLM-5 fine-tune YAML (V18-ready template)

```yaml
# config/glm5_lora_qlora.yaml
model_name_or_path: zai-org/GLM-5
finetuning_type: lora
lora_target: all          # all linear layers
lora_rank: 32             # bump to 64-128 for harder tasks
lora_alpha: 64
lora_dropout: 0.05

quantization_bit: 4
quantization_method: bnb  # or "hqq" if bnb fails on Ascend
quantization_config:
  bnb_4bit_compute_dtype: bfloat16
  bnb_4bit_use_double_quant: true
  bnb_4bit_quant_type: nf4

stage: sft                # then rerun with stage: dpo or stage: grpo
dataset: surrogate_devops_sre,surrogate_iac_safety
template: glm5            # ensure template registered
cutoff_len: 8192          # extend to 32k/200k if data allows
preprocessing_num_workers: 16

learning_rate: 1.0e-4
num_train_epochs: 3
per_device_train_batch_size: 1
gradient_accumulation_steps: 16
warmup_ratio: 0.03
lr_scheduler_type: cosine
bf16: true
gradient_checkpointing: true

deepspeed: examples/deepspeed/ds_z3_offload_config.json
output_dir: saves/glm5-surrogate-v18
logging_steps: 10
save_steps: 200
plot_loss: true

# ASCEND PATH ADDITIONAL:
# device: npu
# (uses ASCEND_RT_VISIBLE_DEVICES instead of CUDA_VISIBLE_DEVICES)
```

### D.3 ZeRO-3 config (for 8× H100 80GB)

```json
{
  "zero_optimization": {
    "stage": 3,
    "offload_optimizer": {"device": "cpu", "pin_memory": true},
    "offload_param": {"device": "cpu", "pin_memory": true},
    "overlap_comm": true,
    "contiguous_gradients": true,
    "stage3_max_live_parameters": 1e9,
    "stage3_max_reuse_distance": 1e9,
    "stage3_gather_16bit_weights_on_model_save": true
  },
  "bf16": {"enabled": true},
  "gradient_accumulation_steps": "auto",
  "train_batch_size": "auto",
  "gradient_clipping": 1.0,
  "steps_per_print": 10
}
```

**Memory math for 744B base:** without quantization, full BF16 = 1.49 TB → far exceeds 8× H100 80GB (640GB). With **4-bit base (372 GB) + LoRA only trainable (~200MB) + ZeRO-3 CPU offload of optimizer state** → fits with headroom on 8× H100. This is THE config for V18 LoRA SFT on H100 (or H200 = even more comfortable headroom).

---

## E. Qdrant — RAG layer for Surrogate-1

**Source:** [Qdrant docs](https://qdrant.tech/), [Hybrid Search article](https://qdrant.tech/articles/hybrid-search/), [Hybrid Search guide April 2026](https://blog.supermemory.ai/hybrid-search-guide/)

### E.1 Why Qdrant fits Surrogate-1

- **Self-hostable** on AWS ECS or Huawei ECS — no per-token vendor lock
- **Native BM25 + dense vector hybrid** with Reciprocal Rank Fusion (RRF) — single API
- **Sharding to billions of vectors** for code+SRE corpus
- **Prometheus `/metrics`** built-in — fits existing observability
- **+6ms p99 latency, +1.4× storage** for hybrid vs dense-only — acceptable

### E.2 Production setup pattern (ECS)

```yaml
# docker-compose.yml on ECS
services:
  qdrant:
    image: qdrant/qdrant:v1.13  # or latest
    ports:
      - "6333:6333"
      - "6334:6334"  # gRPC
    volumes:
      - qdrant_data:/qdrant/storage
    environment:
      QDRANT__SERVICE__API_KEY: ${QDRANT_API_KEY}
      QDRANT__TLS__CERT: /certs/cert.pem
      QDRANT__CLUSTER__ENABLED: true
    deploy:
      resources:
        reservations:
          memory: 16G  # for ~10M vectors @ 1024-dim
```

**Collection setup for hybrid search:**

```python
from qdrant_client import QdrantClient, models

c = QdrantClient(url="https://qdrant.surrogate.local:6334", api_key=api_key)

c.create_collection(
    collection_name="surrogate_code",
    vectors_config={
        "dense": models.VectorParams(size=1024, distance=models.Distance.COSINE),
    },
    sparse_vectors_config={
        "bm25": models.SparseVectorParams(
            modifier=models.Modifier.IDF  # qdrant computes IDF for you
        )
    }
)

# Hybrid query
results = c.query_points(
    collection_name="surrogate_code",
    prefetch=[
        models.Prefetch(query=dense_emb, using="dense", limit=20),
        models.Prefetch(query=bm25_sparse, using="bm25", limit=20),
    ],
    query=models.FusionQuery(fusion=models.Fusion.RRF),
    limit=10,
)
```

### E.3 Embedding model picks

- **Dense:** `BAAI/bge-large-en-v1.5` (1024-dim) or `nomic-embed-text-v1.5` (768-dim) — both run on Ascend NPU or local Mac via Ollama
- **Sparse BM25:** `qdrant/bm25` model (Qdrant computes IDF inline)

---

## F. LangGraph + MCP + ACP/A2A — agent layer

**Source:** [LangChain LangGraph](https://www.langchain.com/langgraph), [LangGraph + MCP guide 2026](https://techbytes.app/posts/langgraph-mcp-multi-agent-workflow-guide-2026/), [Morph LLM agent frameworks 2026](https://www.morphllm.com/ai-agent-framework), [Multi-agent ACP design (Mar 2026)](https://www.marktechpost.com/2026/03/01/how-to-design-a-production-grade-multi-agent-communication-system-using-langgraph-structured-message-bus-acp-logging-and-persistent-shared-state-architecture/)

### F.1 Stack roles

| Layer | Tool | Role |
|-------|------|------|
| **Graph runtime** | LangGraph 1.0 | stateful execution, checkpointing, human-in-loop |
| **Tool protocol** | MCP (Model Context Protocol) | versioned, network-accessible tools — 200+ servers in registry |
| **Agent-to-agent** | A2A (formerly ACP) | peer-to-peer task delegation. **ACP merged into A2A under Linux Foundation late-2025** |
| **Observability** | LangSmith | traces, evals, prod monitoring |

### F.2 V18 wiring (replaces V17 tool-set)

```python
# agent/controller.py (V18)
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres import PostgresSaver
from langchain_mcp_adapters import MCPClient
from a2a_protocol import AgentCard, A2AClient

# 1. Discover MCP tools (registry of 200+ servers)
mcp = MCPClient.from_registry(["filesystem", "git", "kubectl", "aws", "terraform"])

# 2. Discover A2A peer agents (specialty agents on the network)
a2a = A2AClient(agent_card=AgentCard.load("./surrogate.card.json"))
peers = a2a.discover(["security-reviewer", "iac-validator"])

# 3. Build LangGraph
g = StateGraph(SurrogateState)
g.add_node("planner", planner_node)        # uses GLM-5 base
g.add_node("coder", coder_node_with(mcp))  # GLM-5 + MCP tools
g.add_node("reviewer", lambda s: a2a.delegate(peers["security-reviewer"], s))
g.add_node("verifier", verifier_node)
g.add_edge("planner", "coder")
g.add_conditional_edges("coder", needs_review, {"yes": "reviewer", "no": "verifier"})
g.add_edge("reviewer", "verifier")
g.add_conditional_edges("verifier", is_done, {"yes": END, "retry": "coder"})

app = g.compile(checkpointer=PostgresSaver.from_conn_string(PG_URL))
```

### F.3 Compatibility with V17

V17's 37-token specialty set assumed direct LLM tool-calls. V18 wraps each specialty:
- V17 token `<plan>` → V18 LangGraph `planner` node
- V17 token `<bash>` → MCP `filesystem`/shell servers
- V17 token `<review>` → A2A delegation to peer reviewer agent
- **Adapter layer:** keep V17 token grammar in GLM-5 LoRA, but route token → MCP/A2A call at runtime. **No retraining needed for V17→V18 protocol shift, only adapter layer.**

---

## G. Transfer cost from Kaggle 7B → V18 GLM-5 production

### G.1 Path summary

```
PHASE A (DONE / IN PROGRESS — Kaggle T4×2 + Qwen2.5-Coder-7B)
  ├ Validate technique stack (DAPO, longalign, expert iter, dedup)
  ├ Build dataset pipeline (HF dataset surrogate-1-* repos)
  ├ Cost: $0
  └ Output: 7B LoRA + harness scripts + dataset corpus

PHASE B (V18 PORT — H100 cloud, 1-3 days)
  ├ B.1 GLM-5 inference smoke test on Lambda H100×8 — 4h, ~$120
  ├ B.2 LLaMA-Factory QLoRA SFT on GLM-5 with V17 dataset — 24-48h, ~$400-1.5k
  ├ B.3 Eval LoRA on coding+SRE benchmarks — 6h, ~$200
  └ Cost: ~$700-$1.7k

PHASE C (V18 RL — H100/H200 cloud, 24-72h)
  ├ C.1 slime async RL setup + smoke test 4×H100 — 8h, ~$200
  ├ C.2 slime+APRIL GRPO on GLM-5 LoRA, 5k steps, 16×H100 — 24-48h, ~$1.3k-$2.6k
  ├ C.3 (optional) Miles fork if collapse — +30% time, ~+$1k
  └ Cost: ~$1.5k-$5k

PHASE D (V18 STEADY-STATE — production)
  ├ D.1 vLLM/SGLang inference on rented H200 (or pay-per-token via Z.AI/DeepInfra)
  ├ D.2 Qdrant on AWS ECS t3.large (~$30/mo)
  ├ D.3 LangGraph orchestrator on ECS Fargate (~$20/mo)
  ├ D.4 Postgres for checkpoints (~$15/mo)
  ├ D.5 LLM inference: pay-per-token API (Z.AI / DeepInfra) → ~$50-200/mo for personal usage
  └ Cost: ~$100-300/month (NOT $30-60 as README claims)

TOTAL UPFRONT (Phases B+C): $2.2k - $7k
TOTAL STEADY: ~$100-300/month
```

### G.2 Cost-saving moves (in priority order)

1. **Use L40S (Civo $0.89-$1.29/hr)** instead of H100 for QLoRA-SFT phase. L40S has 48GB → can hold quantized GLM-5 with ZeRO-3 offload, takes ~2× longer but ~3× cheaper → net **~50% cost reduction on Phase B**.
2. **Skip slime RL entirely if SFT-only LoRA passes eval bar.** RL contributes ~1-3 pp on benchmarks but costs 2-5× more.
3. **Inference via API not self-host.** Z.AI, DeepInfra, OpenRouter all serve GLM-5 at $0.50-$2.50 / 1M tokens. Personal usage of <50M tokens/month → $25-125/month, no hardware ops burden.
4. **Hybrid strategy:** train on H100, infer on Ascend or via API. Don't fight the train-on-NVIDIA reality.

### G.3 Risk register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **GLM-5 template not in LLaMA-Factory** | Medium | 4-12h dev block | Register custom template, copy from GLM-4.5 with 256-expert tweaks |
| **slime RL collapses on GLM-5 MoE** | Medium | Days lost | Switch to Miles fork (production R3) |
| **Ascend NPU training fails (bnb compile, kernel mismatch)** | High | Migration block | Skip Ascend training entirely for V18, use only for steady-state inference once Huawei opens international ModelArts |
| **744B too big for our budget cluster** | Medium-High | Recipe needs scale-down | Fall back to GLM-4.5 (355B) on 8× H100 — same recipe, ~3× cheaper |
| **Civo/Lambda capacity unavailable** | Low-Med | Schedule slip | Multi-provider: Lambda + Vast.ai + Runpod + Civo — preemptible OK for SFT |
| **MIT license shifts** | Very Low | Major | Mirror weights to private S3/HF the moment training starts; track upstream license |

### G.4 Decision tree: which base to swap to NOW?

```
Q1: Do we have H100×8 + 24-48h budget?
  └ NO  → STAY on Qwen2.5-Coder-7B (Kaggle/Lightning), build technique library
  └ YES → continue
Q2: Is GLM-5.1 LoRA ecosystem mature in LLaMA-Factory?
  └ NO  → fall back to GLM-4.5 (355B) — proven recipe, smaller cluster
  └ YES → continue
Q3: Is RL essential for our benchmarks?
  └ NO  → SFT-only QLoRA on GLM-5 → done. ~$1k-2k total.
  └ YES → slime async RL → ~$3k-7k total.
Q4: Will we self-host inference?
  └ NO  → API (Z.AI, DeepInfra) → $50-200/mo
  └ YES → 8× H200 reserved → $40k+/yr — NOT viable for personal project
```

**Recommended V18 path (May 2026):**
1. **Continue Phase A on Qwen2.5-Coder-7B** until eval shows recipe works at 7B
2. **Phase B: SFT-only QLoRA on GLM-4.5 (355B) first** — 8× H100 24h, ~$800. Validates the recipe at MoE scale before going to GLM-5.
3. **Phase B': promote to GLM-5 (744B)** if 4.5 LoRA passes — 8× H100 48h, ~$1.6k.
4. **Phase C: optional slime RL** only if SFT plateaus.
5. **Phase D: API inference** (NOT self-host). Re-evaluate Ascend self-host in 2026-Q4 when Huawei international ModelArts pricing is public.

---

## H. V17 specialty LoRA migration to V18

### H.1 What survives, what re-trains

| V17 Asset | Migrates to V18? | Notes |
|-----------|-------------------|-------|
| 37-token specialty grammar | YES (via adapter layer) | Tokens map to MCP/A2A calls at runtime |
| Dataset corpus (axentx/surrogate-1-*) | YES | Format-compatible, just bigger model |
| DAPO/GRPO recipe | YES | slime supports GRPO/DAPO/GSPO directly |
| Expert-iteration self-distill | YES | GLM-4.5 paper documents same pattern |
| 7B LoRA weights | NO | Must re-train QLoRA on GLM-5 (different arch) |
| Reward models | YES | Same RM, just longer rollout context |
| Eval suites (HumanEval, MBPP, IaC safety) | YES | Same harness, larger model |
| Kaggle harness scripts | NO | Replace with slime/LF scripts |

### H.2 Re-train cost per specialty LoRA

V17 ships **multiple specialty LoRAs** (coder, devsecops, sre, architecture, etc.). On V18:
- Each specialty LoRA on GLM-5 ≈ **$300-1,500 wall-cost** depending on dataset size and base size choice
- **Strategy:** train ONE composite LoRA covering all specialties (V17 lessons learned — sub-LoRA composition works well in MoE), not N independent LoRAs
- See [[v17-moe-sublora-composition]] for detailed composition pattern

---

## I. Open questions / unknowns / call-out

1. **Exact Ascend 910B per-hour price** outside PRC remains opaque. ModelArts International page lists Ascend SKUs but no public US$/hr quote (May 2026). **Action:** open a Huawei Cloud International account and get a sales quote before committing to Ascend production inference.
2. **GLM-5 template in LLaMA-Factory** — likely lands in 0.10.x but worth confirming before committing budget. **Action:** check `hiyouga/LLaMA-Factory` issue tracker for "GLM-5 template" before each Phase B run.
3. **slime on Ascend** — open-source slime is NVIDIA/AMD only. Z.AI used internal Huawei-flavored slime for GLM-5 pretrain. **Action:** track THUDM/slime issues for "Ascend" — if it lands, the V18 Ascend training story changes dramatically.
4. **MTP speculative decoding** for GLM-5 inference — vLLM v0.19+ supports `--speculative-config.method mtp` but real-world tps gain unmeasured for 744B. **Action:** benchmark in Phase D smoke test.
5. **A2A adoption** — ACP merged into A2A under LF late-2025 but ecosystem maturity (May 2026) still rough. **Fallback:** start with MCP-only, add A2A peer-discovery in V19.

---

## J. Bibliography (primary sources)

### GLM-5 / Z.AI
- [GLM-5 paper (arXiv:2602.15763)](https://arxiv.org/html/2602.15763v1) — "From Vibe Coding to Agentic Engineering"
- [GLM-5 GitHub (zai-org/GLM-5)](https://github.com/zai-org/GLM-5)
- [GLM-5 HF model](https://huggingface.co/zai-org/GLM-5)
- [GLM-5.1 HF model](https://huggingface.co/zai-org/GLM-5.1)
- [GLM-5-FP8 HF model](https://huggingface.co/zai-org/GLM-5-FP8)
- [Z.AI GLM-5.1 launch (MarkTechPost Apr 2026)](https://www.marktechpost.com/2026/04/08/z-ai-introduces-glm-5-1-an-open-weight-754b-agentic-model-that-achieves-sota-on-swe-bench-pro-and-sustains-8-hour-autonomous-execution/)
- [GLM-5 vs GPT-5.2/Claude (DigitalApplied)](https://www.digitalapplied.com/blog/zhipu-ai-glm-5-release-744b-moe-model-analysis)
- [GLM data story Kili](https://kili-technology.com/blog/data-story-glm-model-family)
- [Reeboot.fr GLM-5 review](https://reeboot.fr/en/blog/glm-5/)

### Ascend / Huawei
- [Awesome Agents Ascend 910B](https://awesomeagents.ai/hardware/huawei-ascend-910b/)
- [WareDB Ascend 910B specs](https://www.waredb.com/processor/ascend-910b)
- [Tom's Hardware Ascend deep dive](https://www.tomshardware.com/tech-industry/artificial-intelligence/huaweis-homegrown-ai-chip-examined-chinese-fab-smic-produced-ascend-910b-is-massively-different-from-the-tsmc-produced-ascend-910)
- [letsdatascience GLM-5 on Huawei chips](https://letsdatascience.com/blog/china-trained-frontier-ai-model-glm-5-without-nvidia)
- [Huawei Ascend Cloud intro (Medium)](https://medium.com/@huaweiclouddevelper/a-brief-introduction-to-huawei-ascend-cloud-cbef8f25bc34)
- [ModelArts pricing](https://support.huaweicloud.com/intl/en-us/price-modelarts/price-modelarts-0011.html)
- [ModelArts billing items](https://support.huaweicloud.com/intl/en-us/price-modelarts/price-modelarts-0042.html)
- [Pangu Wikipedia](https://en.wikipedia.org/wiki/Huawei_PanGu)
- [CloudMatrix 384 (gpuvec)](https://gpuvec.com/posts/huawei_and_deepseek)
- [SecondState Ascend 910B agents](https://www.secondstate.io/articles/llm-agents-on-ascend/)
- [MindSpore + GLM-5 (Medium)](https://thamizhelango.medium.com/mindspore-zhipu-ai-huawei-ascend-how-china-built-a-frontier-ai-model-without-a-single-nvidia-68403d92cedb)

### slime / Miles / APRIL
- [THUDM/slime GitHub](https://github.com/THUDM/slime)
- [slime docs site](https://thudm.github.io/slime/)
- [slime quick start](https://github.com/THUDM/slime/blob/main/docs/en/get_started/quick_start.md)
- [LMSYS slime announcement (Jul 2025)](https://www.lmsys.org/blog/2025-07-09-slime/)
- [DeepWiki slime](https://deepwiki.com/THUDM/slime)
- [APRIL paper (arXiv:2509.18521)](https://arxiv.org/abs/2509.18521)
- [APRIL GitHub](https://github.com/RLsys-Foundation/APRIL)
- [Miles GitHub (radixark/miles)](https://github.com/radixark/miles)
- [Miles announcement (LMSYS Nov 2025)](https://www.lmsys.org/blog/2025-11-19-miles/)
- [DeepSeek-V4 + Miles Day-0 (LMSYS Apr 2026)](https://www.lmsys.org/blog/2026-04-25-deepseek-v4/)
- [Miles ROCm (LMSYS Mar 2026)](https://lmsys.org/blog/2026-03-17-rocm-miles-rl-amd/)
- [HF blog: 16 RL libs landscape](https://huggingface.co/blog/async-rl-training-landscape)

### LLaMA-Factory
- [LLaMA-Factory GitHub](https://github.com/hiyouga/LlamaFactory)
- [LLaMA-Factory NPU docs](https://llamafactory.readthedocs.io/en/latest/advanced/npu_training.html)
- [PR #6601 Ascend NF4 QLoRA](https://github.com/hiyouga/LLaMA-Factory/pull/6601)
- [GLM-OCR fine-tune cookbook (zai-org)](https://github.com/zai-org/GLM-OCR/blob/main/examples/finetune/README.md)

### Inference / vLLM / SGLang
- [vLLM GLM5 recipe](https://docs.vllm.ai/projects/recipes/en/latest/GLM/GLM5.html)
- [GLM-5.1 vLLM/SGLang self-host (Lushbinary)](https://lushbinary.com/blog/glm-5-1-self-hosting-guide-vllm-sglang-deployment/)
- [GLM-5.1 HF + Ollama + vLLM guide (explainx)](https://explainx.ai/blog/glm-5-1-hugging-face-how-to-run-ollama)
- [SGLang docs](https://docs.sglang.io/)

### Qdrant
- [Qdrant docs](https://qdrant.tech/)
- [Hybrid search article](https://qdrant.tech/articles/hybrid-search/)
- [Hybrid search guide April 2026](https://blog.supermemory.ai/hybrid-search-guide/)
- [Qdrant + LlamaIndex hybrid](https://developers.llamaindex.ai/python/examples/vector_stores/qdrant_hybrid/)

### LangGraph + MCP + A2A/ACP
- [LangChain LangGraph](https://www.langchain.com/langgraph)
- [LangGraph + MCP guide 2026](https://techbytes.app/posts/langgraph-mcp-multi-agent-workflow-guide-2026/)
- [Agent frameworks 2026 (Morph)](https://www.morphllm.com/ai-agent-framework)
- [Multi-agent ACP design pattern (MarkTechPost Mar 2026)](https://www.marktechpost.com/2026/03/01/how-to-design-a-production-grade-multi-agent-communication-system-using-langgraph-structured-message-bus-acp-logging-and-persistent-shared-state-architecture/)
- [LangGraph multi-agent (gurusup 2026)](https://gurusup.com/blog/best-multi-agent-frameworks-2026)

### GPU pricing references
- [Lambda pricing](https://lambda.ai/pricing)
- [Civo L40S](https://www.civo.com/cloud-gpu/nvidia-l40s-gpu)
- [getdeploying H100](https://getdeploying.com/gpus/nvidia-h100)
- [getdeploying L40S](https://getdeploying.com/gpus/nvidia-l40s)
- [Spheron 2026 pricing](https://www.spheron.network/blog/gpu-cloud-pricing-comparison-2026/)
- [Cerebras GLM pricing](https://www.cerebras.ai/blog/glm)

---

## K. Wire-into-codebase action list (V18 implementation)

> Owner can pick which of these to greenlight; each is independent.

1. **`config/training_config_v18.yaml`** — port the YAML in §D.2 into `~/axentx/surrogate-1/config/`
2. **`scripts/training/finetune_glm5_h100.sh`** — Lambda H100 launcher
3. **`scripts/training/finetune_glm5_ascend.sh`** — ModelArts launcher (commented out, gated on Huawei International access)
4. **`scripts/rl/slime_grpo_glm5.sh`** — slime + APRIL launcher (16× H100)
5. **`docker/Dockerfile.v18-train`** — wrap `slime:nvidia-h100` + LLaMA-Factory + axentx datasets
6. **`agent/v18/controller.py`** — LangGraph + MCP + A2A skeleton (replace V17 controller)
7. **`scripts/qdrant/init_collections.py`** — bootstrap surrogate_code, surrogate_docs, surrogate_runbooks
8. **`config/qdrant_config.yaml`** — sharding, BM25, tenancy
9. **`tests/v18/eval_glm5_lora.py`** — port V17 eval suite to GLM-5 LoRA
10. **`docs/v18-runbook.md`** — owner-facing runbook for "how to launch a V18 training run"

---

## Status & next-refresh trigger

- **Status:** research-spec, awaiting owner sign-off on Phase B scope and budget cap.
- **Next refresh** when:
  - Huawei opens international ModelArts pricing publicly → re-cost Ascend path
  - Z.AI publishes GLM-5 Cookbook with LLaMA-Factory recipe → simplify §D
  - GLM-5.2 / GLM-6 lands → re-evaluate base swap
  - DeepSeek V5 / Kimi K3 / Qwen 4 lands → comparative re-baseline
- **Reviewer note:** all dollar estimates assume May 2026 cloud pricing. Re-validate every refresh.

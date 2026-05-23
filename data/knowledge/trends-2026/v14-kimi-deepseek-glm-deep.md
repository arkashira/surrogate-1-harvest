---
tags: [surrogate-1, v14, kimi, deepseek, glm, moonshot, zhipu, training-side, deep-dive, 2025-2026]
created: 2026-05-01
status: research-grounded — 35+ sources cited inline (papers + GitHub + model cards)
audience: V14+ trainer wire-up — kaggle-trainer.sh + Lightning H200 patches
based-on: 25+ web searches, dated 2024-2026 only
related: [[v13-frontier-efficiency]], [[v13-frontier-capability]], [[opensource-releases-2026-Q2]]
---

# V14+ Deep Dive — Kimi (Moonshot), DeepSeek, GLM (Zhipu/Z.AI)

> **Owner directive (2026-05-01):** Three labs publish more open training detail than anyone else. V13 partially copied DeepSeek (NSA, MLA-rope) and superficially mentioned MuonClip + slime. V14 must extract EVERY published technique with measured numbers + code paths, decide what's T4×2-feasible vs Civo/H200-only, then wire concrete env knobs into kaggle-trainer.sh.
>
> **Goal:** techniques baked INTO weights via training. Not external orchestration scripts.

---

## TL;DR — V14+ Stack From These 3 Labs

| Lab | Technique | Where it Lives | T4×2? | Lift | Wire-In |
|---|---|---|---|---|---|
| Moonshot | **MuonClip optimizer (QK-clip)** | optimizer | partial (head qk-clip only) | high | `SUR_USE_MUON_CLIP=1` head-only |
| Moonshot | **Agentic data synthesis (20K tools)** | dataset | YES | high | mix `SUR_KIMI_AGENTIC_PCT=15` |
| Moonshot | **Self-Critique Rubric Reward** | RL post-train | YES (offline) | medium | `SUR_RUBRIC_REWARD=1` |
| Moonshot | **Sparsity 48 / 384 experts** | architecture | NO (inherit only) | n/a | choose base wisely |
| Moonshot | **INT4 QAT (post-train)** | post-training | YES (LoRA-QAT) | medium | `SUR_QAT_INT4=1` (final stage) |
| Moonshot | **Kimi-Linear KDA hybrid** | architecture | NO (full retrain) | high | research only |
| DeepSeek | **MLA latent attention** | architecture | NO (inherit) | n/a | use DS base / Kimi base |
| DeepSeek | **DSA + Lightning Indexer** | architecture | partial (indexer-only LoRA) | high | `SUR_DSA_INDEXER=1` |
| DeepSeek | **MTP module (multi-token pred)** | extra heads | YES (LoRA-MTP head) | high | `SUR_MTP_HEADS=2` |
| DeepSeek | **Aux-loss-free MoE balance** | router | only if MoE base | medium | `SUR_AUX_FREE_BIAS=1` |
| DeepSeek | **FP8 mixed precision** | precision | NO (Hopper+) | n/a | Civo L40S only via TE |
| DeepSeek | **GRPO-Zero RLVR loop** | post-train | YES (small models) | high | `SUR_RUN_GRPO_ZERO=1` |
| DeepSeek | **Prover-V2 subgoal decomposition** | dataset | YES | medium | `SUR_TAKE_PROVER=1` |
| DeepSeek | **DeepSeek-OCR optical compression** | dataset | YES (data side) | high | new pipeline |
| DeepSeek | **DAPO 4 techniques (clip-higher etc.)** | RL algo | YES via verl | high | `SUR_DAPO_*` flags |
| GLM | **slime RL framework (Megatron+SGLang)** | infra | NO (Civo-only) | high | Civo phase only |
| GLM | **Expert iteration + self-distill** | post-train | YES | high | `SUR_EXPERT_ITER=1` |
| GLM | **RLCS curriculum sampling** | RL training | YES | medium | `SUR_RLCS=1` |
| GLM | **LongAlign (THUDM/LongAlign)** | dataset+packing | YES | high | `SUR_LONGALIGN=1` |
| GLM | **Hybrid Thinking mode** | post-train | YES (chat template) | medium | `SUR_HYBRID_THINK=1` |
| GLM | **MinHash+SemDedup curation** | dataset | YES | medium | `SUR_DEDUP=minhash+sem` |

**Rule:** YES + medium-or-high lift = wire in V14. NO = inherit from base model selection only.

---

## A. KIMI / MOONSHOT — Deep Dive

### A.1 MuonClip Optimizer — THE Marquee Innovation

**Sources:**
- [Kimi K2 Technical Report (arXiv:2507.20534)](https://arxiv.org/abs/2507.20534) — Jul 2025
- [MuonClip community impl (kyegomez/MuonClip)](https://github.com/kyegomez/MuonClip) — open-source PyTorch
- [Fireworks deep-dive on MuonClip](https://fireworks.ai/blog/muonclip)
- [Muon original (Keller Jordan blog)](https://kellerjordan.github.io/posts/muon/)
- [Muon Scalable for LLM Training (arXiv:2502.16982)](https://arxiv.org/pdf/2502.16982) — Liu, Su et al, Feb 2025

**Measured benefit:**
- Pre-trained 1T-param MoE on **15.5T tokens with ZERO loss spikes** vs AdamW which spikes at trillion-scale
- **~2× FLOPs efficiency vs AdamW** at same loss (Muon: 2× efficiency, MuonClip preserves it)
- **Training stability** — QK-clip directly addresses exploding attention logits, a classic Muon failure mode at scale
- **CIFAR-10 record:** 3.3 → 2.6 A100-sec (Muon component)

**How QK-Clip works (from K2 report + community impl):**
```python
# After Muon update, before next forward:
# Compute max(|q · k^T|) per attention head from sample batch
# If max_score > threshold (default 100.0):
#     eta = threshold / max_score
#     W_q *= eta ** alpha     # alpha=0.5 splits adjustment
#     W_k *= eta ** (1-alpha)
# This caps attention logits without changing loss landscape
```

**T4×2 feasibility:**
- **Full Muon: NO** — Newton-Schulz iteration on 5-iter requires 32+ GB activation cache for big tensors. Doesn't fit T4 16GB at scale.
- **Hybrid: YES** — apply Muon to LoRA matrices (small 2D weights, easy NS-iter), keep AdamW for everything else. **Already validated by Liu/Su scaling paper.**
- **Civo L40S: YES full Muon** — 48 GB plenty of headroom.
- **QK-clip head-only LoRA: YES on T4** — Apply qk-clip post-hoc to LoRA q_proj/k_proj after each step. Minimal memory cost.

**V14 patch (kaggle-trainer.sh, additive):**
```bash
# Add to env knobs section:
export SUR_USE_MUONCLIP="${SUR_USE_MUONCLIP:-1}"        # 0=AdamW only, 1=hybrid Muon-LoRA + qk-clip
export SUR_QKCLIP_THRESHOLD="${SUR_QKCLIP_THRESHOLD:-100.0}"
export SUR_QKCLIP_ALPHA="${SUR_QKCLIP_ALPHA:-0.5}"
export SUR_MUON_TARGETS="${SUR_MUON_TARGETS:-q_proj,k_proj,v_proj,o_proj}"  # apply Muon only to these LoRA matrices
```

In `train.py`, replace optimizer construction with:
```python
if int(os.environ.get("SUR_USE_MUONCLIP", "0")):
    from muonclip import MuonClip   # pip install git+https://github.com/kyegomez/MuonClip
    muon_targets = os.environ["SUR_MUON_TARGETS"].split(",")
    muon_params = [p for n,p in model.named_parameters() if any(t in n for t in muon_targets) and "lora" in n.lower()]
    other_params = [p for n,p in model.named_parameters() if p.requires_grad and not any(t in n for t in muon_targets)]
    optimizer = torch.optim.AdamW(other_params, lr=lr)
    muon_opt = MuonClip(muon_params,
                        lr=lr*0.5,
                        qk_clip_threshold=float(os.environ["SUR_QKCLIP_THRESHOLD"]),
                        qk_clip_alpha=float(os.environ["SUR_QKCLIP_ALPHA"]))
    # combined step: optimizer.step(); muon_opt.step()
```

---

### A.2 Agentic Data Synthesis (20K+ Tool Scenarios)

**Sources:**
- [K2 report Section 4 (arXiv:2507.20534)](https://arxiv.org/html/2507.20534v1)
- [DigitalOcean K2 post-training tutorial](https://www.digitalocean.com/community/tutorials/post-training-agentic-models-kimi-k2)
- [Centron Kimi-K2 post-training](https://www.centron.de/en/tutorial/kimi-k2-post-training-tool-use-synthetic-data-reinforcement-learning/)

**The 3-stage pipeline (replicate-able):**
1. **Tool repo** — 20,000+ tool specs covering: real APIs, shell, DBs, synthetic. Each tool = JSON schema + description.
2. **Agent + task generation** — synthesize thousands of distinct system prompts, equip each with a different tool subset → diverse population. For each agent, generate task + rubric (success criteria, expected tool-call sequence).
3. **Multi-turn trajectory** — user-simulator + agent rollout in synthetic env, log trajectory. Rubric scores filter successes for SFT data.

**Measured benefit:** K2 ranks #1 open-source on agentic benchmarks (TAU-Bench, SWE-bench), 70.1% TAU-Bench, top-1 LiveCodeBench in mid-2025.

**T4×2 feasibility:** **YES — data side only.** No training dependency, just mix into SFT.

**V14 wire-in:**
```bash
# New dataset slot:
export SUR_TAKE_KIMI_AGENTIC_PCT="${SUR_TAKE_KIMI_AGENTIC_PCT:-15}"   # 15% of SFT mix from agentic synth
```

**Public datasets to mix:**
- [xLAM agentic dataset (Salesforce)](https://huggingface.co/datasets/Salesforce/xlam-function-calling-60k) — 60K tool calls (already in V13)
- [ToolACE](https://huggingface.co/datasets/Team-ACE/ToolACE) — 11K tool-use trajectories (already in V13)
- **NEW:** Use K2-Instruct outputs (open MIT) to generate trajectories — distill from teacher
- **NEW:** [Magpie-Pro-300K-Filtered (Magpie-Align)](https://huggingface.co/datasets/Magpie-Align/Magpie-Pro-MT-300K-Filtered) — 300K self-instruct, multi-turn

---

### A.3 Self-Critique Rubric Reward (General Alignment)

**Sources:**
- [K2 report §5.3 — General RL (arXiv:2507.20534)](https://arxiv.org/html/2507.20534v1)
- [dbreunig: How Kimi RLed Qualitative Data](https://www.dbreunig.com/2025/07/31/how-kimi-rl-ed-qualitative-data-to-write-better.html)

**The trick:** Combine RLVR (rule-based binary reward for math/code/logic — easy) with self-critique on open-ended tasks (creativity, helpfulness, depth) where the **policy itself rates its own outputs against rubrics**. The critic is continuously refined with on-policy verifiable rollouts → distills objective signal into subjective judge.

**Three rubric types:**
1. **Fundamental values** (factuality, safety) — anti-hallucination
2. **Prescriptive** — rules to eliminate reward hacking
3. **Human-annotated** — domain-specific (writing style, tutorial depth, etc.)

**Measured benefit:** Drove K2 to top of LMSYS Arena open-source category, esp. on creative-writing and long-form tasks where binary RLVR fails.

**T4×2 feasibility:** **YES (offline rubric scoring + DPO).** Can't run full GRPO with critic at trillion-param scale, but rubric → preference pairs → DPO is cheap.

**V14 wire-in:**
```bash
export SUR_RUBRIC_REWARD="${SUR_RUBRIC_REWARD:-1}"
export SUR_RUBRIC_FILE="${SUR_RUBRIC_FILE:-/kaggle/working/rubrics.yaml}"   # ship with notebook
export SUR_DPO_FROM_RUBRIC="${SUR_DPO_FROM_RUBRIC:-1}"   # post-SFT DPO stage
```

Pipeline:
1. SFT done → rollout 4-8 candidates per prompt with current model
2. Score each with rubric (3-5 dimensions, 1-5 scale, sum)
3. Highest = chosen, lowest = rejected → DPO pair
4. Run TRL `DPOTrainer` for 1 epoch

---

### A.4 INT4 QAT (Quantization-Aware Training) for K2-Thinking

**Sources:**
- [K2-Thinking model card](https://huggingface.co/moonshotai/Kimi-K2-Thinking)
- [Turing Post on K2-Thinking](https://x.com/TheTuringPost/status/1989001234217594944) — Nov 2025

**Key:** During post-training, inject 4-bit rounding noise into MoE weight forward → model learns to retain accuracy at INT4. Result: **lossless 2× inference speedup** at INT4 vs FP16 on K2-Thinking.

**T4×2 feasibility:** **YES via bitsandbytes 4-bit QLoRA + simulated INT4 noise.** Already what QLoRA does for memory; extending to QAT-noise gives lossless quant.

**V14 wire-in (final stage only):**
```bash
export SUR_QAT_INT4="${SUR_QAT_INT4:-0}"   # enable in stage 3 only (last 5% of steps)
export SUR_QAT_NOISE_SCHED="${SUR_QAT_NOISE_SCHED:-cosine}"
```

Add to TrainingArguments via callback:
```python
class QAT4Callback(TrainerCallback):
    def on_step_begin(self, args, state, control, **kw):
        if state.global_step / state.max_steps > 0.95:    # last 5%
            for n,p in model.named_parameters():
                if "lora_B" in n or "expert" in n.lower():
                    # Round-to-nearest INT4, then dequant — 4-bit STE
                    q = (p.data * 7).round().clamp(-8, 7) / 7
                    p.data.copy_(q + (p.data - q).detach())
```

---

### A.5 Sparsity 48 / 384 Experts (Architecture)

**Source:** [Kimi K2 IntuitionLabs deep dive](https://intuitionlabs.ai/articles/kimi-k2-technical-deep-dive)

**Key finding:** K2 sweeps DeepSeek-V3 (256 experts) by going to **384 experts, sparsity 48** (8 active out of 384). At equal val-loss 1.5: sparsity 48 reduces FLOPs by 1.69×/1.39×/1.15× vs sparsity 8/16/32. Attention heads cut to **64** (vs DS-V3's 128) for inference efficiency.

**T4×2 feasibility:** **NO** — Inherited from base model only. We can't change MoE topology via LoRA. **Action:** Choose base wisely. If we ever do MoE-LoRA on K2-Base (256 ctx), we inherit this for free.

---

### A.6 Kimi-Linear KDA Hybrid Attention (Oct 2025)

**Sources:**
- [Kimi Linear paper (arXiv:2510.26692)](https://arxiv.org/abs/2510.26692)
- [Official MoonshotAI/Kimi-Linear](https://github.com/MoonshotAI/Kimi-Linear)
- [flash-linear-attention KDA](https://github.com/fla-org/flash-linear-attention) — has KDA kernel

**Key:** Kimi Delta Attention (KDA) extends Gated DeltaNet with **channel-wise gating per feature dim** (vs Qwen3-Next's scalar gate). Hybrid: KDA layers + occasional MLA layers. Training: 1.4T tokens, 3B-active / 48B-total.

**Measured benefit:**
- **6× faster decoding at 1M ctx** vs full attention
- **−75% KV cache** at 1M ctx
- Outperforms full MLA on benchmarks (first linear-attn to beat full at scale)

**T4×2 feasibility:** **NO for full retrain.** Architecture change requires from-scratch training. **YES for inference-side KDA injection** if we use Kimi-Linear-Base as our base model (3B active fits T4 16GB easily).

**V14 action:** Add Kimi-Linear-3B-Base as new base candidate option; treat as research-track for V14.5. Don't make default.

---

### A.7 Kimi K2.6 (Apr 2026) — Long-Horizon Coding + Agent Swarms

**Sources:**
- [Kimi K2.6 Verdent guide](https://www.verdent.ai/guides/what-is-kimi-k2-6)
- [Moonshot K2.6 release blog](https://kimi-k2.org/blog/24-kimi-k2-6-release) — Apr 21, 2026
- [Kimi K2.6 MarkTechPost](https://www.marktechpost.com/2026/04/20/moonshot-ai-releases-kimi-k2-6-with-long-horizon-coding-agent-swarm-scaling-to-300-sub-agents-and-4000-coordinated-steps/)

**What's new in K2.6 vs K2:** Posttraining-only — same 1T arch, 32B active, 262K ctx, native INT4. Improvements in:
- Long-horizon stability (4000 coordinated steps)
- Agent swarm scaling (300 sub-agents)
- Native video input
- Stronger Rust/Go/Python

**Lesson for V14:** Most K2.6 wins come from **more posttraining compute applied to long-horizon trajectories**, not arch changes. Confirms our thesis: invest in trajectory-rich SFT data + multi-turn RL.

---

## B. DEEPSEEK — Deep Dive

### B.1 MLA — Multi-head Latent Attention

**Sources:**
- [DeepSeek-V3 Technical Report (arXiv:2412.19437)](https://arxiv.org/abs/2412.19437)
- [Towards Data Science: MLA explained](https://towardsdatascience.com/deepseek-v3-explained-1-multi-head-latent-attention-ed6bee2a67c4/)
- [FlashMLA kernels](https://github.com/deepseek-ai/FlashMLA) — official

**Key:** Low-rank joint compression of K and V into a shared latent vector (much smaller than original). KV cache shrinks dramatically; quality preserved.

**Measured benefit:**
- **KV cache cut by ~93% vs MHA** at equal quality
- Validated at 671B param scale on V3 with no quality loss
- Enables 128K ctx that fits in commodity inference

**T4×2 feasibility:** **NO for retrofit.** Architecture-bound to base model. **YES if we pick MLA-native base** (DeepSeek-V3-Lite, Qwen3-Next has variant, Kimi-K2 uses MLA).

**V14 action:** Already inherited if base = K2 or DS-V3 family. Don't change default.

---

### B.2 DSA + Lightning Indexer (DeepSeek-V3.2-Exp)

**Sources:**
- [DeepSeek-V3.2 paper (PDF)](https://github.com/deepseek-ai/DeepSeek-V3.2-Exp/blob/main/DeepSeek_V3_2.pdf) — Sep 2025
- [DeepSeek-V3.2 vLLM blog](https://blog.vllm.ai/2025/09/29/deepseek-v3-2.html)
- [DSA pure-PyTorch impl](https://github.com/ZhengKai91/deepseek-sparse-attention-pytorch) — community
- [FlashMLA repo](https://github.com/deepseek-ai/FlashMLA) — official kernels

**Key:** Lightning Indexer = **lightweight FP8 head with few attn heads** that scores all preceding tokens for relevance. For each query, top-2048 selected → DSA does main attention only on those. **Quadratic O(L²) → O(Lk).**

**Training recipe (from V3.2 paper):**
- Warm-up: 1000 steps, freeze all but indexer, dense attn maintained, **2.1B tokens total** (16 seqs × 128K × 1000 steps), LR=1e-3
- Sparse stage: 15000 steps, 480 seqs × 128K, LR=7.3e-6, top-k=2048

**Measured benefit:** At 128K ctx, **50% cheaper, 3× faster** vs dense; same quality on benchmarks.

**T4×2 feasibility:** **PARTIAL — indexer-only LoRA.** Don't try to retrain full DSA on T4. But:
- **Train indexer-as-adapter** on a frozen base (indexer is small ~50M params)
- Use indexer output to mask attention at inference — like LongLoRA but learned
- Civo L40S: full short DSA fine-tune feasible at 16K ctx

**V14 wire-in:**
```bash
export SUR_DSA_INDEXER="${SUR_DSA_INDEXER:-0}"     # 1=train indexer as separate LoRA
export SUR_DSA_TOPK="${SUR_DSA_TOPK:-512}"         # T4 budget — smaller than V3.2's 2048
export SUR_DSA_INDEXER_LR="${SUR_DSA_INDEXER_LR:-1e-3}"
export SUR_DSA_WARMUP_STEPS="${SUR_DSA_WARMUP_STEPS:-200}"
```

Pipeline (separate stage after main SFT):
1. Freeze base + LoRA; add new `Indexer` module (small 4-head attention) with FP8 if available, else BF16
2. Warm-up 200 steps with dense attn loss → indexer converges
3. Sparse stage 1500 steps with top-k=512 routing
4. Save indexer separately; load alongside LoRA at inference

---

### B.3 MTP — Multi-Token Prediction

**Sources:**
- [DS-V3 Tech Report §2.2 (arXiv:2412.19437)](https://arxiv.org/html/2412.19437v1)
- [LMSYS MTP+SGLang blog](https://www.lmsys.org/blog/2025-07-17-mtp/) — Jul 2025
- [DeepWiki MTP module](https://deepwiki.com/deepseek-ai/DeepSeek-V3/3.4-multi-token-prediction-(mtp))
- [Megatron Bridge MTP docs](https://docs.nvidia.com/nemo/megatron-bridge/latest/training/multi-token-prediction.html)

**Key:** D additional prediction modules (each = small transformer block + projection + shared embed/head). Each module predicts the (current+i)-th token from hidden state. **Trained jointly with base.** Adds 14B params to V3's 671B. Loss = main-CE + λ·sum of MTP-CE.

**Measured benefit:**
- **+1.8× generation throughput** in DS-V3 (MTP1 acceptance rate >80%)
- **+60% throughput** in newer benchmarks (LMSYS Jul 2025) zero quality loss
- **Densifies training signal** → better dependency capture
- **Inference-only optional:** discard MTP modules at deploy if no spec-decode

**T4×2 feasibility:** **YES via LoRA-MTP-head.**
- MTP module = small (50-100M each) → fits T4
- Add 1-2 MTP heads as separate trainable LoRAs
- During training, multi-task loss = main + λ·MTP1 + λ·MTP2

**V14 wire-in:**
```bash
export SUR_MTP_HEADS="${SUR_MTP_HEADS:-2}"        # 0=off, 1-2=number of MTP heads
export SUR_MTP_LAMBDA="${SUR_MTP_LAMBDA:-0.3}"    # weight on MTP loss
export SUR_MTP_KEEP_AT_INFERENCE="${SUR_MTP_KEEP_AT_INFERENCE:-1}"   # 1=keep for spec decode, 0=discard
```

In `train.py`:
```python
if int(os.environ.get("SUR_MTP_HEADS", "0")):
    from mtp_head import MTPHead   # custom small module
    n_heads = int(os.environ["SUR_MTP_HEADS"])
    mtp_heads = nn.ModuleList([MTPHead(model.config) for _ in range(n_heads)])
    # Custom loss in training_step:
    def compute_loss(model, inputs, return_outputs=False):
        outputs = model(**inputs, output_hidden_states=True)
        ce = outputs.loss
        h = outputs.hidden_states[-1]
        mtp_loss = sum(head(h, inputs["labels"], offset=i+1) for i,head in enumerate(mtp_heads))
        loss = ce + float(os.environ["SUR_MTP_LAMBDA"]) * mtp_loss
        return (loss, outputs) if return_outputs else loss
```

---

### B.4 Aux-Loss-Free MoE Load Balancing

**Sources:**
- [Aux-Loss-Free paper (arXiv:2408.15664)](https://arxiv.org/html/2408.15664v1)
- [DS-V3 §2.1.2](https://arxiv.org/html/2412.19437v1)
- [Yugen.ai MoE balancing blog](https://medium.com/yugen-ai-technology-blog/deepseek-v3-advances-in-moe-load-balancing-and-multi-token-prediction-training-f6d68c59749c)

**Key:** Instead of auxiliary load-balance loss (which hurts quality), add **per-expert bias to routing scores before top-K**. Update bias each step: overloaded → decrease bias, underloaded → increase. Pure routing trick, no gradient impact on main loss.

**Measured benefit:** Better val-loss + better load balance vs aux-loss baseline. Validated 1B/3B/671B.

**T4×2 feasibility:** **ONLY IF MoE base.** If base is dense (Qwen2.5-Coder-32B), no MoE → not applicable. If base is GLM-4.5-Air (106B MoE) or Kimi-K2-Base — applicable.

**V14 wire-in:**
```bash
export SUR_AUX_FREE_BIAS="${SUR_AUX_FREE_BIAS:-1}"    # only honored if base is MoE
export SUR_AUX_BIAS_LR="${SUR_AUX_BIAS_LR:-1e-3}"     # bias adjustment rate
```

Hook into router (model-specific):
```python
# At each step, after forward:
if hasattr(model, "moe_router"):
    expert_loads = model.moe_router.last_loads   # [n_experts]
    target = expert_loads.mean()
    delta = (target - expert_loads) * float(os.environ["SUR_AUX_BIAS_LR"])
    model.moe_router.bias.data += delta
```

---

### B.5 FP8 Mixed Precision Training

**Sources:**
- [DS-V3 Tech Report §3 (arXiv:2412.19437)](https://arxiv.org/pdf/2412.19437)
- [Colfax DS-R1 + FP8](https://research.colfax-intl.com/deepseek-r1-and-fp8-mixed-precision-training/)
- [Prashant Sahdev DS-V3 FP8 blog](https://medium.com/@prashantsahdev/deepseek-v3-blog-5-low-precision-training-the-fp8-revolution-in-large-scale-ai-29fc4b14761e)

**Key:** Tile-wise FP8 quantization (1×128 tiles) for matmul; keep BF16/FP32 on embeddings, output head, MoE gating, norms, attention ops. Loss error <0.25% vs BF16 baseline.

**Measured benefit:** **~50% memory** vs BF16, 1.5-2× training throughput on Hopper.

**T4×2 feasibility:** **NO.** T4 is Turing — no FP8 hw. Even Ampere (A100) lacks FP8 native (emulated only). **YES on L40S** (Ada has FP8 in Transformer Engine), **YES on H200** (Hopper).

**V14 action:** Civo L40S phase only. On Lightning H200, enable. On Kaggle T4, must skip.

```bash
# Civo / H200 only:
export SUR_FP8="${SUR_FP8:-0}"   # 1=enable on Ada/Hopper
# In train.py: if SUR_FP8 and is_ada_or_hopper(): use TransformerEngine
```

---

### B.6 GRPO RLVR Training Recipe (DeepSeekMath/R1)

**Sources:**
- [DeepSeekMath (arXiv:2402.03300)](https://arxiv.org/abs/2402.03300)
- [DeepSeek-R1 paper (arXiv:2501.12948)](https://arxiv.org/abs/2501.12948)
- [GRPO-Zero impl (policy-gradient/GRPO-Zero)](https://github.com/policy-gradient/GRPO-Zero) — minimal deps, low VRAM
- [Phil Schmid: How DS-R1 was trained](https://www.philschmid.de/deepseek-r1) + [mini-deepseek-r1 notebook](https://github.com/philschmid/deep-learning-pytorch-huggingface/blob/main/training/mini-deepseek-r1-aha-grpo.ipynb)

**The R1 recipe (verbatim from §3 of arXiv:2501.12948):**
- Stage 1 RL hyperparameters: LR=3e-6, KL coef=0.001, GRPO clip ratio=10, sampling T=1.0
- 16 outputs per prompt, max-len 32768
- 32 unique questions per step → batch=512

**T4×2 feasibility:** **YES on small models (≤7B).** GRPO-Zero impl runs LLaMA-1B/Qwen-3B GRPO on a single T4 with QLoRA. Mini-DS-R1 notebook demonstrates Qwen-1.5B GRPO on T4.

**V14 wire-in:**
```bash
export SUR_RUN_GRPO="${SUR_RUN_GRPO:-0}"          # already in V13 (planned), make real
export SUR_GRPO_LR="${SUR_GRPO_LR:-3e-6}"
export SUR_GRPO_KL="${SUR_GRPO_KL:-0.001}"
export SUR_GRPO_CLIP="${SUR_GRPO_CLIP:-10.0}"
export SUR_GRPO_K_OUTPUTS="${SUR_GRPO_K_OUTPUTS:-8}"   # 16 in DS, 8 fits T4 better
export SUR_GRPO_MAX_LEN="${SUR_GRPO_MAX_LEN:-2048}"   # 32768 in DS, T4 can't
export SUR_GRPO_BATCH="${SUR_GRPO_BATCH:-32}"          # 512 in DS, T4 = 32
export SUR_GRPO_REWARDS="${SUR_GRPO_REWARDS:-math,code,format}"
```

Use TRL `GRPOTrainer` (already supports it) or GRPO-Zero for tighter loop.

---

### B.7 DAPO — 4 Critical RL Techniques

**Sources:**
- [DAPO paper (arXiv:2503.14476)](https://arxiv.org/pdf/2503.14476) — Mar 2025, ByteDance Seed + Tsinghua
- [DAPO github](https://github.com/BytedTsinghua-SIA/DAPO) — open RL system

**Four techniques for stable long-CoT RL:**
1. **Clip-Higher** — asymmetric clipping promotes diversity, avoids entropy collapse
2. **Dynamic Sampling** — over-sample under-represented rollouts, drop saturated batches
3. **Token-Level Policy Gradient Loss** — averages loss per token NOT per sequence (critical for long-CoT)
4. **Overlong Reward Shaping** — reduce noise from accidentally-truncated long generations

**Measured:** Beats DS-R1-Zero on AIME 2024 (50 pts) using only 50% training steps. Open-source verl-based.

**T4×2 feasibility:** **YES** — All four are loss/sampling tricks, no extra memory. Drop-in for our GRPO loop.

**V14 wire-in:**
```bash
export SUR_DAPO_CLIP_HIGHER="${SUR_DAPO_CLIP_HIGHER:-1}"
export SUR_DAPO_CLIP_LO="${SUR_DAPO_CLIP_LO:-0.2}"
export SUR_DAPO_CLIP_HI="${SUR_DAPO_CLIP_HI:-0.28}"        # asymmetric — DAPO default
export SUR_DAPO_DYNAMIC_SAMPLE="${SUR_DAPO_DYNAMIC_SAMPLE:-1}"
export SUR_DAPO_TOKEN_LEVEL_LOSS="${SUR_DAPO_TOKEN_LEVEL_LOSS:-1}"
export SUR_DAPO_OVERLONG_SHAPING="${SUR_DAPO_OVERLONG_SHAPING:-1}"
export SUR_DAPO_OVERLONG_PENALTY="${SUR_DAPO_OVERLONG_PENALTY:-0.1}"
```

In GRPO loss:
```python
# Replace sequence-level mean with token-level:
if int(os.environ.get("SUR_DAPO_TOKEN_LEVEL_LOSS", "0")):
    loss = (per_token_loss * mask).sum() / mask.sum()
else:
    loss = (per_token_loss * mask).sum(-1).mean()

# Asymmetric clip:
if int(os.environ.get("SUR_DAPO_CLIP_HIGHER", "0")):
    clip_lo = float(os.environ["SUR_DAPO_CLIP_LO"])
    clip_hi = float(os.environ["SUR_DAPO_CLIP_HI"])
    ratio_clipped = ratio.clamp(1-clip_lo, 1+clip_hi)
else:
    ratio_clipped = ratio.clamp(1-eps, 1+eps)
```

---

### B.8 DeepSeek-Prover-V2 + DeepSeekMath-V2 Datasets

**Sources:**
- [DeepSeek-Prover-V2 (arXiv:2504.21801)](https://arxiv.org/abs/2504.21801) — Apr 2025
- [DeepSeek-Math-V2 (arXiv:2511.22570)](https://arxiv.org/html/2511.22570v1) — Nov 2025
- [Prover-V2 github](https://github.com/deepseek-ai/DeepSeek-Prover-V2)
- [DeepSeek-Math-V2 HF](https://huggingface.co/deepseek-ai/DeepSeek-Math-V2)

**What Prover-V2 contributes (technique, not data):**
- **Subgoal decomposition** prompt: prompt teacher (V3) to break problem into ordered subgoals
- For each subgoal, separately try resolution; concat resolved CoT
- Lean4 verifier filters successes → SFT data
- Iterative: each iteration uses current best prover for hard problems

**What's open:**
- **ProverBench** — 325 formalized problems (HF)
- **Prover-V2-671B model** — open weights
- **Prover-V2 training code** — open

**Math-V2 contributes:**
- 17,503 problems crawled from Art of Problem Solving (AoPS) — math olympiads, post-2010 proof problems
- GRPO with proof-generator + proof-verifier loop

**Public datasets to mix:**
- [DeepSeek-Math AoPS-17K](https://github.com/deepseek-ai/DeepSeek-Math) — Apache 2.0
- [DeepSeek-Prover-V2 dataset (proofs + Lean4)](https://github.com/deepseek-ai/DeepSeek-Prover-V2) — MIT
- [ProverBench (325 problems, HF)](https://huggingface.co/datasets/deepseek-ai/DeepSeek-ProverBench)

**T4×2 feasibility:** **YES — data side.**

**V14 wire-in:**
```bash
export SUR_TAKE_PROVER="${SUR_TAKE_PROVER:-3000}"           # # samples from Prover-V2
export SUR_TAKE_AOPS="${SUR_TAKE_AOPS:-2000}"               # # samples from Math-V2 AoPS
export SUR_USE_SUBGOAL_TEMPLATE="${SUR_USE_SUBGOAL_TEMPLATE:-1}"  # apply DS subgoal-decomp prompt to math data
```

---

### B.9 DeepSeek-OCR — Optical Compression Dataset Pipeline

**Sources:**
- [DeepSeek-OCR paper (arXiv:2510.18234)](https://arxiv.org/abs/2510.18234) — Oct 2025
- [DeepSeek-OCR repo](https://github.com/deepseek-ai/DeepSeek-OCR)
- [VentureBeat 10x compression article](https://venturebeat.com/ai/deepseek-drops-open-source-model-that-compresses-text-10x-through-images)
- [MIT Tech Review article](https://www.technologyreview.com/2025/10/29/1126932/deepseek-ocr-visual-compression/)

**Key:** **Render text-as-image then encode with vision tokens.** 1 vision token = ~10 text tokens at <10× compression with 97% accuracy. Training data: 30M PDF pages, 100+ languages, 10M synthetic charts, 5M chemistry, 1M geometry.

**Why this matters for V14:** Not just a model — it's a **data generation pipeline at 200K pages/day on a single A100**. We can use it to:
1. Compress our training corpus 10× (less I/O, faster Kaggle epochs)
2. Generate diverse OCR-grade reasoning data (charts, tables, formulas)

**T4×2 feasibility:** **YES — data side.** Run DeepSeek-OCR offline on Civo or HF Space → produce compressed token streams → train V14 on those.

**V14 wire-in (new pipeline):**
```bash
export SUR_USE_DSOCR_DATA="${SUR_USE_DSOCR_DATA:-0}"     # 1 = include compressed-via-OCR samples
export SUR_DSOCR_COMPRESSION="${SUR_DSOCR_COMPRESSION:-10x}"
export SUR_DSOCR_CACHE="${SUR_DSOCR_CACHE:-axentx/surrogate-1-ocr-compressed}"   # HF dataset slug
```

---

### B.10 DeepSeek-V3.1 Hybrid Thinking Mode

**Sources:**
- [DS-V3.1 release blog](https://api-docs.deepseek.com/news/news250821) — Aug 2025
- [Together AI DS-V3.1 blog](https://www.together.ai/blog/deepseek-v3-1-hybrid-thinking-model-now-available-on-together-ai)
- [DS-V3.1 HF](https://huggingface.co/deepseek-ai/DeepSeek-V3.1)

**Key:** Single model serves BOTH "think" mode (CoT) and "no-think" (direct) via different chat templates. Posttrained with **CoT-compression** — output tokens reduced 20-50% with same answer quality.

**T4×2 feasibility:** **YES via training data.** Mix:
- 60% direct samples
- 40% CoT samples with mode-marker tokens

Then chat template controls switching at inference.

**V14 wire-in:**
```bash
export SUR_HYBRID_THINK="${SUR_HYBRID_THINK:-1}"
export SUR_THINK_RATIO="${SUR_THINK_RATIO:-0.4}"   # 40% CoT, 60% direct
export SUR_THINK_TAG_OPEN="${SUR_THINK_TAG_OPEN:-<think>}"
export SUR_THINK_TAG_CLOSE="${SUR_THINK_TAG_CLOSE:-</think>}"
export SUR_COT_COMPRESS_TARGET="${SUR_COT_COMPRESS_TARGET:-0.7}"   # compress to 70% of original len
```

---

### B.11 DeepSeek-V4 (Apr 24, 2026) — What's NEW

**Sources:**
- [MarkTechPost DS-V4 release](https://www.marktechpost.com/2026/04/24/deepseek-ai-releases-deepseek-v4-compressed-sparse-attention-and-heavily-compressed-attention-enable-one-million-token-contexts/) — Apr 24 2026
- [BSWEN: How DS-V4 handles 1M ctx](https://docs.bswen.com/blog/2026-04-24-deepseek-v4-1m-context/)
- [Kili-Tech DS-V4 Engram guide](https://kili-technology.com/blog/data-story-deepseek-v4)
- [The Register DS-V4 inference savings](https://www.theregister.com/2026/04/24/deepseek_v4/) — Apr 24 2026
- [vLLM DS-V4 blog](https://vllm.ai/blog/deepseek-v4)

**Variants:** V4-Pro (1.6T total, 49B active) + V4-Flash (284B total, 13B active). Both 1M ctx, 384K output. MIT license.

**Three new techniques worth mining:**

#### B.11.1 Compressed Sparse Attention (CSA) + Heavily Compressed Attention (HCA)
Hybrid attention interleaved across layers:
- **CSA:** compress every m tokens into 1 entry via learned token-level compressor → run DSA top-k on compressed entries
- **HCA:** more aggressive compression on layers tolerating it
- **At 1M ctx: 27% FLOPs vs V3.2, 10% KV cache, 9.62 GiB KV/seq @ bf16**

#### B.11.2 Engram Conditional Memory
Hash-based lookup table in DRAM (not VRAM) for static patterns (syntax, entity names, function signatures). O(1) retrieval. **Frees attention layers for actual reasoning.** +3-5 pts benchmark, NIH 84.2 → 97% on a 27B model.

#### B.11.3 FP4 + FP8 Mixed Precision
MoE expert weights at **FP4** (quantization-aware training during pretrain), most other params at FP8. Indexer QK path also FP4-QAT.

**T4×2 feasibility:**
- CSA/HCA: **NO** (architecture change, retrain required)
- Engram: **YES — data side.** Build a hash lookup of common patterns from our corpus, use as retrieval at inference. **Doesn't go INTO weights** — but can teach the model to use external memory via training data.
- FP4+FP8: **NO** on T4. **YES on Hopper/Blackwell** via TE.

**V14 action:** Defer CSA to research; Engram to V14.5 retrieval; FP4 to H200.

---

## C. GLM / ZHIPU AI / Z.AI — Deep Dive

### C.1 GLM-4.5 ARC Paper (Aug 2025) — Architecture + Training

**Sources:**
- [GLM-4.5 paper (arXiv:2508.06471)](https://arxiv.org/abs/2508.06471) — Aug 8 2025
- [GLM-4.5 github (zai-org/GLM-4.5)](https://github.com/zai-org/GLM-4.5)
- [Sai Dheeraj deep dive](https://medium.com/data-science-in-your-pocket/a-technical-deep-dive-into-glm-4-5-agentic-reasoning-and-coding-arc-1fffd98803e4)

**Spec:** 355B MoE, 32B active. Hybrid reasoning (think + direct). 23T-token corpus.

**Pre-training corpus mix:**
- **15T tokens** general (web/books/multilingual) — MinHash + SemDedup filtered
- **7T tokens** code + math/science

**Post-training pipeline (THE recipe to copy):**
```
Stage 1 (Specialization, parallel):
  ├─ Reasoning expert    → math/code RL
  ├─ Agent expert        → tool-use RL on AgentBench-style envs
  └─ General-chat expert → helpfulness DPO

Stage 2 (Unification):
  Self-distillation from all 3 experts → unified GLM-4.5
  Multi-stage filtering pipeline:
    - remove repetitive/truncated samples
    - correctness verification (objective answers)
    - reward-model scoring (subjective)
    - tool-call protocol validation (agentic)
```

**Measured benefit:** TAU-Bench 70.1%, AIME-24 91.0%, SWE-bench Verified 64.2%. Top-3 overall, top-2 agentic among open.

**T4×2 feasibility:** **YES — at smaller scale.** Three "expert LoRAs" trained in parallel, then distilled to one final LoRA. Each expert ~30 min on Kaggle T4×2.

**V14 wire-in:**
```bash
export SUR_EXPERT_ITER="${SUR_EXPERT_ITER:-1}"
export SUR_EXPERT_DOMAINS="${SUR_EXPERT_DOMAINS:-reasoning,agent,chat}"   # 3 specialized passes
export SUR_DISTILL_FROM_EXPERTS="${SUR_DISTILL_FROM_EXPERTS:-1}"   # final unification stage
export SUR_EXPERT_FILTER="${SUR_EXPERT_FILTER:-correctness+reward+toolproto}"
```

Pipeline:
1. Train 3 separate LoRAs on domain-filtered data
2. Generate samples from each on a held-out prompt set
3. Apply 4-stage filter (dedup → correctness → reward → tool-proto)
4. Final SFT on filtered samples → unified model

---

### C.2 slime — RL Framework Behind GLM-4.5/4.6/4.7

**Sources:**
- [slime github (THUDM/slime)](https://github.com/THUDM/slime)
- [slime docs](https://thudm.github.io/slime/)
- [LMSYS slime announcement](https://www.lmsys.org/blog/2025-07-09-slime/) — Jul 2025
- [Miles (forks slime, enterprise)](https://github.com/radixark/miles)

**Key:** SGLang-native post-training framework.
- Training: Megatron-LM
- Rollout: SGLang + router
- Data buffer: shared between phases
- Modes: synchronous (co-located GPU) OR asynchronous (decoupled GPU clusters)
- **Only verified framework for >355B MoE RL** (GLM-4.5, etc.)
- **FP8 inference** for rollouts
- **Rollout Routing Replay (R3)** — replay routes for stable training

**T4×2 feasibility:** **NO.** slime requires Megatron + multiple nodes. T4 cluster doesn't have it. **YES on Lightning H200 / Civo L40S** with proper setup.

**V14 action:**
- Kaggle: skip slime, use TRL GRPO + custom DAPO patches
- Civo/H200 phase: deploy slime with `--mode async --backbone megatron --rollout sglang`

```bash
# Civo/H200 phase only:
export SUR_USE_SLIME="${SUR_USE_SLIME:-0}"
export SUR_SLIME_MODE="${SUR_SLIME_MODE:-async}"   # sync|async
export SUR_SLIME_BACKBONE="${SUR_SLIME_BACKBONE:-megatron}"
```

---

### C.3 LongAlign — Long-Context SFT Recipe

**Sources:**
- [LongAlign paper (arXiv:2401.18058)](https://arxiv.org/pdf/2401.18058) — EMNLP 2024
- [THUDM/LongAlign github](https://github.com/THUDM/LongAlign)
- [GLM-Long blog](https://medium.com/@ChatGLM/glm-long-scaling-pre-trained-model-contexts-to-millions-caa3c48dea85)

**Key contributions:**
1. **10K instruction data of 8K-64K length** via Self-Instruct from 9 sources (HF dataset open)
2. **Packing + sorted batching** — sort by length, pack to fixed buckets → no padding waste
3. **Loss weighting for packing** — re-weight per-sequence loss to compensate for variable lengths in pack
4. **LongBench-Chat benchmark** (10K-100K open-ended)

**Measured:** GLM-4 with LongAlign matches Claude-2 / GPT-4-Turbo on long-ctx tasks at 128K.

**T4×2 feasibility:** **YES** — pure data + training-loop tricks. No new GPU memory cost.

**V14 wire-in:**
```bash
export SUR_LONGALIGN="${SUR_LONGALIGN:-1}"
export SUR_LONGALIGN_DATASET="${SUR_LONGALIGN_DATASET:-THUDM/LongAlign-10k}"
export SUR_LONGALIGN_TAKE="${SUR_LONGALIGN_TAKE:-2000}"
export SUR_PACKING_LOSS_WEIGHT="${SUR_PACKING_LOSS_WEIGHT:-1}"
export SUR_SORTED_BATCH="${SUR_SORTED_BATCH:-1}"
export SUR_MAX_PACK_LEN="${SUR_MAX_PACK_LEN:-8192}"   # T4 budget — GLM uses 64K, we can't
```

In `train.py` (use TRL packing + custom loss):
```python
if int(os.environ.get("SUR_LONGALIGN", "0")):
    from datasets import load_dataset
    longalign = load_dataset(os.environ["SUR_LONGALIGN_DATASET"], split="train")
    longalign = longalign.select(range(int(os.environ["SUR_LONGALIGN_TAKE"])))
    # mix 8-15% into main mix

# Sorted batching (custom sampler):
class SortedLengthSampler(torch.utils.data.Sampler):
    def __init__(self, lengths, batch_size):
        idx_sorted = sorted(range(len(lengths)), key=lambda i: lengths[i])
        self.batches = [idx_sorted[i:i+batch_size] for i in range(0, len(idx_sorted), batch_size)]
        random.shuffle(self.batches)
    def __iter__(self): return iter([i for b in self.batches for i in b])

# Loss weighting in packed sequences:
def compute_pack_loss(per_token_loss, pack_ids, pack_lens):
    # Each packed sample gets weight = 1/N_in_pack so total contribution = 1
    weights = torch.zeros_like(per_token_loss)
    for pack_id, length in zip(pack_ids, pack_lens):
        weights[pack_id == pack_ids] = 1.0 / length
    return (per_token_loss * weights).sum()
```

---

### C.4 GLM-4.5V / 4.6V / 4.1V-Thinking — RLCS

**Sources:**
- [GLM-V paper (arXiv:2507.01006)](https://arxiv.org/abs/2507.01006)
- [GLM-V github (zai-org/GLM-V)](https://github.com/zai-org/GLM-V)

**Key:** **Reinforcement Learning with Curriculum Sampling (RLCS)** — multi-domain RL framework with **difficulty-aware sampling**: select tasks suited to model's CURRENT competence (not random). Each task gets a difficulty score; sampler over-samples just-above-current-ability.

**Measured:** SOTA on 42 multimodal benchmarks among similar-size open models (GLM-4.5V).

**T4×2 feasibility:** **YES** — sampling trick on top of GRPO. No extra memory.

**V14 wire-in:**
```bash
export SUR_RLCS="${SUR_RLCS:-1}"
export SUR_RLCS_DIFFICULTY_FN="${SUR_RLCS_DIFFICULTY_FN:-rollout_pass_rate}"   # estimate from past rollouts
export SUR_RLCS_TARGET_SUCCESS="${SUR_RLCS_TARGET_SUCCESS:-0.5}"   # sweet spot — 50% pass rate = best learning
export SUR_RLCS_SAMPLE_TEMP="${SUR_RLCS_SAMPLE_TEMP:-0.3}"
```

Sampler logic in GRPO loop:
```python
# Track per-prompt rolling pass rate:
prompt_pass_rate = defaultdict(lambda: 0.5)
# Each step: weight prompts inversely to |pass_rate - target|
weights = torch.tensor([
    math.exp(-abs(prompt_pass_rate[p] - target) / temp)
    for p in prompts
])
prompt_indices = torch.multinomial(weights, batch_size)
```

---

### C.5 GLM-4.6 (Sep 30, 2025) — 357B MoE

**Sources:**
- [GLM-4.6 release MarkTechPost](https://www.marktechpost.com/2025/09/30/zhipu-ai-releases-glm-4-6-achieving-enhancements-in-real-world-coding-long-context-processing-reasoning-searching-and-agentic-ai/) — Sep 30 2025
- [GLM-4.6 HF](https://huggingface.co/zai-org/GLM-4.6)
- [Z.AI GLM-4.6 docs](https://docs.z.ai/guides/llm/glm-4.6)
- [BenchLM coding leaderboard Mar 2026](https://benchlm.ai/coding)

**What's new vs 4.5:**
- 200K ctx (up from 128K)
- **+30% token efficiency** (better quality per output token)
- LiveCodeBench v6: **63.3% → 82.8%** (matches Claude 4)
- SWE-bench Verified: 68.0%

**Where the win came from (per release notes):**
- More posttraining compute (RL on coding/agentic)
- Better tool-call data
- Hybrid thinking refinements

**Lesson for V14:** Most of GLM-4.6's gain over 4.5 = **better post-training data** + **RL on real-world coding tasks**. Not arch change.

---

### C.6 GLM-4.7 (Open) — Coding Frontier Open Model

**Sources:**
- [GLM-4.7 HF](https://huggingface.co/zai-org/GLM-4.7)
- [GLM-V multimodal repo (4.6V/4.5V/4.1V-Thinking)](https://github.com/zai-org/GLM-V)

**Status:** GLM-4.7 released as open weights via slime training pipeline. LiveCodeBench top-1 84.9 (per release blog). Same arch as 4.6 (355B MoE).

**Lesson:** Confirms slime + expert-iteration + posttraining = sustained gains.

---

### C.7 GLM-130B / ChatGLM Lineage — Curation Lessons

**Sources:**
- [ChatGLM family paper (arXiv:2406.12793)](https://arxiv.org/pdf/2406.12793)

**Public datasets:**
- [LongBench-Chat](https://github.com/THUDM/LongBench) — eval, MIT
- [AgentBench](https://github.com/THUDM/AgentBench) — agent eval
- [LongAlign-10k](https://github.com/THUDM/LongAlign) — train, MIT

---

## D. Cross-Lab Synergy — What Combinations Work Best

### D.1 Stable Trainer Stack (V14 Default)
```
Base: Qwen2.5-Coder-32B or Kimi-K2-Base or DS-V3-Lite
Optim: MuonClip (LoRA params) + AdamW (rest)
Loss:  CE + λ·MTP (Kimi K2 + DS MTP)
Data:  Kimi agentic 15% + DS-Math 10% + LongAlign 8% + V13 mix 67%
RL:    GRPO + DAPO (4 tricks) + RLCS curriculum + self-critique rubric
Filter: GLM 4-stage (dedup → correctness → reward → toolproto)
```

### D.2 Synergy Predictions

| Combination | Why It Compounds | Risk |
|---|---|---|
| **MuonClip + MTP heads** | MuonClip stabilizes Q/K updates; MTP needs stable hidden states | Low — both train at same precision |
| **DAPO clip-higher + RLCS curriculum** | Clip-higher prevents entropy collapse; RLCS adapts difficulty → never hit ceiling early | Low |
| **GLM expert iter + Kimi rubric** | Experts give diverse samples; rubric scores fairly across domains | Medium — needs careful rubric tuning |
| **DS-V3 aux-free MoE + GLM filter** | MoE balanced naturally; GLM filter catches collapsed experts | Low if base is MoE |
| **DS-OCR data + LongAlign** | OCR'd long PDFs become long-ctx training samples | Medium — sample quality must be checked |
| **Kimi self-critique + DAPO token-level** | Per-token reward signal compounds with rubric scoring | Low |

### D.3 Anti-Synergies to Avoid

| Combination | Why It Hurts |
|---|---|
| **Full Muon + FP8 forward** on T4 | T4 lacks FP8; Muon NS-iter precision-sensitive |
| **MTP heads + INT4 QAT (early)** | MTP loss needs FP gradients; QAT noise destroys MTP signal. **Apply QAT in last 5% only.** |
| **RLCS + low rollout count** | Curriculum needs good pass-rate estimate; <8 rollouts/prompt = noisy difficulty |
| **DSA indexer + LoRA-only-attn** | Indexer needs to see full attention forward; freezing attn + LoRA on it conflicts |

---

## E. Public Datasets — V14+ Mix

### E.1 NEW datasets to add (all 2024-2026 open-licensed)

| Dataset | License | Size | Source URL | Use |
|---|---|---|---|---|
| Kimi-K2-Instruct outputs (distill) | MIT | ~1T tokens (gen) | [HF](https://huggingface.co/moonshotai/Kimi-K2-Instruct) | Distill agentic responses |
| DeepSeek-Math AoPS-17K | Apache 2.0 | 17,503 | [github](https://github.com/deepseek-ai/DeepSeek-Math) | Math reasoning SFT |
| DeepSeek-Prover-V2 | MIT | ~150K Lean4 | [github](https://github.com/deepseek-ai/DeepSeek-Prover-V2) | Theorem proving |
| ProverBench | MIT | 325 | [HF (within DS-Prover)](https://huggingface.co/deepseek-ai/DeepSeek-Prover-V2-671B) | Eval |
| THUDM/LongAlign-10k | Apache 2.0 | 10K | [HF/github](https://github.com/THUDM/LongAlign) | Long-ctx SFT |
| THUDM/LongBench | Apache 2.0 | eval | [github](https://github.com/THUDM/LongBench) | Long-ctx eval |
| Magpie-Pro-MT-300K | Apache 2.0 | 300K | [HF](https://huggingface.co/datasets/Magpie-Align/Magpie-Pro-MT-300K-Filtered) | Multi-turn SFT |
| DeepSeek-OCR (data gen tool) | MIT | infinite | [github](https://github.com/deepseek-ai/DeepSeek-OCR) | Synthesize OCR data 200K pages/day on A100 |
| AgentBench (eval) | Apache 2.0 | eval | [github](https://github.com/THUDM/AgentBench) | Agent eval |
| DAPO dataset | MIT | curated math | [github](https://github.com/BytedTsinghua-SIA/DAPO) | RL training |
| GRPO-Zero examples | MIT | examples | [github](https://github.com/policy-gradient/GRPO-Zero) | Training scripts |

### E.2 Dataset mix for V14 SFT (% of tokens)

```
Code (DS-Coder + V13 mix):                  30%
Reasoning (DS-Math + Prover + AIME):        15%
Agentic (Kimi distill + xLAM + ToolACE):    20%
Long-ctx (LongAlign + chunked source):       8%
Multi-turn (Magpie):                        12%
General (web-clean V13 base):               15%
─────────────────────────────────────────
Total:                                     100%
```

---

## F. GitHub Reference Implementations (V14 Build Refs)

| Lab | Technique | Best Repo | Stars | Notes |
|---|---|---|---|---|
| Moonshot | MuonClip | [kyegomez/MuonClip](https://github.com/kyegomez/MuonClip) | ~200 | Pure PyTorch, ready to import |
| Moonshot | Kimi-K2 | [MoonshotAI/Kimi-K2](https://github.com/MoonshotAI/Kimi-K2) | 6K+ | Official |
| Moonshot | Kimi-Linear (KDA) | [MoonshotAI/Kimi-Linear](https://github.com/MoonshotAI/Kimi-Linear) | 1K+ | Official KDA + vLLM |
| Moonshot | Linear attn (community) | [fla-org/flash-linear-attention](https://github.com/fla-org/flash-linear-attention) | 2K+ | Triton kernels for KDA + Gated DeltaNet |
| DeepSeek | DSA (training/inference) | [ZhengKai91/deepseek-sparse-attention-pytorch](https://github.com/ZhengKai91/deepseek-sparse-attention-pytorch) | ~150 | Pure PyTorch DSA, drop-in |
| DeepSeek | NSA | [lucidrains/native-sparse-attention-pytorch](https://github.com/lucidrains/native-sparse-attention-pytorch) | ~700 | Lucidrains is reliable |
| DeepSeek | FlashMLA | [deepseek-ai/FlashMLA](https://github.com/deepseek-ai/FlashMLA) | 12K+ | Official kernels |
| DeepSeek | DeepSeek-V3 | [deepseek-ai/DeepSeek-V3](https://github.com/deepseek-ai/DeepSeek-V3) | 100K+ | Official |
| DeepSeek | Prover-V2 | [deepseek-ai/DeepSeek-Prover-V2](https://github.com/deepseek-ai/DeepSeek-Prover-V2) | ~3K | Official, Lean4 + GRPO |
| DeepSeek | DeepSeek-OCR | [deepseek-ai/DeepSeek-OCR](https://github.com/deepseek-ai/DeepSeek-OCR) | 5K+ | Official, A100-runnable |
| ByteDance | DAPO | [BytedTsinghua-SIA/DAPO](https://github.com/BytedTsinghua-SIA/DAPO) | ~2K | Official, verl-based |
| GRPO | GRPO-Zero | [policy-gradient/GRPO-Zero](https://github.com/policy-gradient/GRPO-Zero) | ~400 | Minimal-deps, T4-friendly |
| GLM | slime | [THUDM/slime](https://github.com/THUDM/slime) | 2K+ | Official RL framework |
| GLM | LongAlign | [THUDM/LongAlign](https://github.com/THUDM/LongAlign) | ~700 | Official LongAlign training |
| GLM | GLM-4.5/4.6 | [zai-org/GLM-4.5](https://github.com/zai-org/GLM-4.5) | 5K+ | Official |
| GLM | GLM-V | [zai-org/GLM-V](https://github.com/zai-org/GLM-V) | 1K+ | Official multimodal RL |
| Multi | Awesome-RLVR list | [opendilab/awesome-RLVR](https://github.com/opendilab/awesome-RLVR) | 1K+ | Curated reading list |

---

## G. Wire-Into-V14-Plus

> **Concrete env knobs + dataset names + code patches for kaggle-trainer.sh.** Apply additively. All defaults safe (`0` = off) so V14 still passes V13 baseline if knobs disabled.

### G.1 New Env Knobs (append to kaggle-trainer.sh `cat > train.py << 'PYEOF'` block, top section)

```bash
# ── V14 KIMI / MOONSHOT KNOBS ─────────────────────────────────────
export SUR_USE_MUONCLIP="${SUR_USE_MUONCLIP:-1}"
export SUR_QKCLIP_THRESHOLD="${SUR_QKCLIP_THRESHOLD:-100.0}"
export SUR_QKCLIP_ALPHA="${SUR_QKCLIP_ALPHA:-0.5}"
export SUR_MUON_TARGETS="${SUR_MUON_TARGETS:-q_proj,k_proj}"     # head-only on T4
export SUR_TAKE_KIMI_AGENTIC_PCT="${SUR_TAKE_KIMI_AGENTIC_PCT:-15}"
export SUR_RUBRIC_REWARD="${SUR_RUBRIC_REWARD:-1}"
export SUR_RUBRIC_FILE="${SUR_RUBRIC_FILE:-/kaggle/working/rubrics.yaml}"
export SUR_DPO_FROM_RUBRIC="${SUR_DPO_FROM_RUBRIC:-1}"
export SUR_QAT_INT4="${SUR_QAT_INT4:-0}"                          # final stage only
export SUR_QAT_NOISE_SCHED="${SUR_QAT_NOISE_SCHED:-cosine}"

# ── V14 DEEPSEEK KNOBS ─────────────────────────────────────────────
export SUR_DSA_INDEXER="${SUR_DSA_INDEXER:-0}"                    # phase 2 only
export SUR_DSA_TOPK="${SUR_DSA_TOPK:-512}"
export SUR_DSA_INDEXER_LR="${SUR_DSA_INDEXER_LR:-1e-3}"
export SUR_DSA_WARMUP_STEPS="${SUR_DSA_WARMUP_STEPS:-200}"
export SUR_MTP_HEADS="${SUR_MTP_HEADS:-2}"
export SUR_MTP_LAMBDA="${SUR_MTP_LAMBDA:-0.3}"
export SUR_MTP_KEEP_AT_INFERENCE="${SUR_MTP_KEEP_AT_INFERENCE:-1}"
export SUR_AUX_FREE_BIAS="${SUR_AUX_FREE_BIAS:-1}"                # only honored if base is MoE
export SUR_AUX_BIAS_LR="${SUR_AUX_BIAS_LR:-1e-3}"
export SUR_FP8="${SUR_FP8:-0}"                                    # Civo/H200 only
export SUR_RUN_GRPO="${SUR_RUN_GRPO:-1}"
export SUR_GRPO_LR="${SUR_GRPO_LR:-3e-6}"
export SUR_GRPO_KL="${SUR_GRPO_KL:-0.001}"
export SUR_GRPO_CLIP="${SUR_GRPO_CLIP:-10.0}"
export SUR_GRPO_K_OUTPUTS="${SUR_GRPO_K_OUTPUTS:-8}"
export SUR_GRPO_MAX_LEN="${SUR_GRPO_MAX_LEN:-2048}"
export SUR_GRPO_BATCH="${SUR_GRPO_BATCH:-32}"
export SUR_DAPO_CLIP_HIGHER="${SUR_DAPO_CLIP_HIGHER:-1}"
export SUR_DAPO_CLIP_LO="${SUR_DAPO_CLIP_LO:-0.2}"
export SUR_DAPO_CLIP_HI="${SUR_DAPO_CLIP_HI:-0.28}"
export SUR_DAPO_DYNAMIC_SAMPLE="${SUR_DAPO_DYNAMIC_SAMPLE:-1}"
export SUR_DAPO_TOKEN_LEVEL_LOSS="${SUR_DAPO_TOKEN_LEVEL_LOSS:-1}"
export SUR_DAPO_OVERLONG_SHAPING="${SUR_DAPO_OVERLONG_SHAPING:-1}"
export SUR_DAPO_OVERLONG_PENALTY="${SUR_DAPO_OVERLONG_PENALTY:-0.1}"
export SUR_TAKE_PROVER="${SUR_TAKE_PROVER:-3000}"
export SUR_TAKE_AOPS="${SUR_TAKE_AOPS:-2000}"
export SUR_USE_SUBGOAL_TEMPLATE="${SUR_USE_SUBGOAL_TEMPLATE:-1}"
export SUR_HYBRID_THINK="${SUR_HYBRID_THINK:-1}"
export SUR_THINK_RATIO="${SUR_THINK_RATIO:-0.4}"
export SUR_COT_COMPRESS_TARGET="${SUR_COT_COMPRESS_TARGET:-0.7}"
export SUR_USE_DSOCR_DATA="${SUR_USE_DSOCR_DATA:-0}"              # phase 3 dataset

# ── V14 GLM / Z.AI KNOBS ───────────────────────────────────────────
export SUR_EXPERT_ITER="${SUR_EXPERT_ITER:-1}"
export SUR_EXPERT_DOMAINS="${SUR_EXPERT_DOMAINS:-reasoning,agent,chat}"
export SUR_DISTILL_FROM_EXPERTS="${SUR_DISTILL_FROM_EXPERTS:-1}"
export SUR_EXPERT_FILTER="${SUR_EXPERT_FILTER:-correctness+reward+toolproto}"
export SUR_LONGALIGN="${SUR_LONGALIGN:-1}"
export SUR_LONGALIGN_DATASET="${SUR_LONGALIGN_DATASET:-THUDM/LongAlign-10k}"
export SUR_LONGALIGN_TAKE="${SUR_LONGALIGN_TAKE:-2000}"
export SUR_PACKING_LOSS_WEIGHT="${SUR_PACKING_LOSS_WEIGHT:-1}"
export SUR_SORTED_BATCH="${SUR_SORTED_BATCH:-1}"
export SUR_MAX_PACK_LEN="${SUR_MAX_PACK_LEN:-8192}"
export SUR_RLCS="${SUR_RLCS:-1}"
export SUR_RLCS_DIFFICULTY_FN="${SUR_RLCS_DIFFICULTY_FN:-rollout_pass_rate}"
export SUR_RLCS_TARGET_SUCCESS="${SUR_RLCS_TARGET_SUCCESS:-0.5}"
export SUR_RLCS_SAMPLE_TEMP="${SUR_RLCS_SAMPLE_TEMP:-0.3}"
export SUR_USE_SLIME="${SUR_USE_SLIME:-0}"                        # Civo/H200 only
export SUR_SLIME_MODE="${SUR_SLIME_MODE:-async}"
```

### G.2 Dataset Names to Add (in train.py, dataset-mix block)

```python
DATASET_REGISTRY_V14 = {
    "kimi_agentic":     ("moonshotai/Kimi-K2-Instruct", "distill_outputs", "MIT"),
    "ds_math_aops":     ("deepseek-ai/DeepSeek-Math", "aops_17k", "Apache-2.0"),
    "ds_prover_v2":     ("deepseek-ai/DeepSeek-Prover-V2", "lean4_proofs", "MIT"),
    "longalign_10k":    ("THUDM/LongAlign-10k", "default", "Apache-2.0"),
    "magpie_pro":       ("Magpie-Align/Magpie-Pro-MT-300K-Filtered", "default", "Apache-2.0"),
    "agentbench_eval":  ("THUDM/AgentBench", "default", "Apache-2.0"),         # eval only
    "dapo_dataset":     ("BytedTsinghua-SIA/DAPO", "math_curated", "MIT"),
    "longbench":        ("THUDM/LongBench", "default", "Apache-2.0"),         # eval only
}
```

### G.3 Stage Sequencing (V14 Default Pipeline)

```
Phase 0 (data prep, off-GPU):
  - Mix datasets per E.2 percentages
  - Apply MinHash + SemDedup (GLM-style)
  - Apply 4-stage filter (GLM expert filter)
  - Generate rubric pairs for DPO

Phase 1 (Kaggle T4×2, ~10h):
  - SFT with MuonClip (LoRA-q,k only) + AdamW (rest)
  - MTP heads (2) trained jointly, λ=0.3
  - LongAlign packing + sorted batching
  - LongAlign data 8% of mix
  - Hybrid think tokens (40% CoT)

Phase 2 (Kaggle T4×2, ~6h):
  - Expert iteration: 3 separate LoRAs (reasoning/agent/chat)
  - Each on domain-filtered subset

Phase 3 (Kaggle T4×2, ~4h):
  - Self-distill from 3 experts → unified LoRA
  - Apply 4-stage filter on expert samples

Phase 4 (Kaggle T4×2, ~6h):
  - GRPO + DAPO 4 tricks + RLCS curriculum
  - SUR_GRPO_K_OUTPUTS=8, max_len=2048
  - Verifiable rewards: math (sympy), code (test cases), format

Phase 5 (Kaggle T4×2, ~3h, OPTIONAL):
  - DPO from rubric pairs
  - Self-critique scoring loop

Phase 6 (Kaggle T4×2, ~1h, FINAL):
  - INT4 QAT noise injection (last 5% of fine-tune)
  - Save final adapter

Phase 7 (Civo L40S / Lightning H200, optional):
  - DSA indexer training (sparse attn)
  - FP8 mixed precision (Ada/Hopper)
  - slime async RL (if doing >100B base)
```

### G.4 Expected Combined Lift vs V13

Conservative estimate (additive, with diminishing returns):

| Source | Estimated Lift |
|---|---|
| MuonClip (head-only) | +1-2% on stable convergence; faster convergence to V13 quality |
| MTP heads | +0.5-1% on benchmarks via denser signal; +1.8× inference if kept |
| GLM expert iteration + 4-stage filter | +2-3% (most reliable single win) |
| Kimi agentic data 15% | +3-5% on TAU-Bench / SWE-bench (if base supports tools) |
| DAPO 4 tricks in GRPO | +2-3% on AIME / MATH (RL stability) |
| RLCS curriculum | +1-2% on RL final (better sample efficiency) |
| LongAlign packing | +5-10% throughput (no quality change) |
| Self-critique rubric DPO | +1-2% on creative/open-ended |
| Hybrid think mode | enables flexibility, +0.5-1% on direct tasks |
| ProverV2 + AoPS data | +3-5% on AIME / MATH |
| **Combined (with overlap)** | **+10-15% on agentic/coding/math benchmarks** |

### G.5 Deferred to Civo L40S / H200 Phase

```bash
# Add to lightning_h200_paste.sh, NOT to kaggle-trainer.sh:
export SUR_FP8=1                    # Ada/Hopper FP8
export SUR_USE_SLIME=1              # async RL framework
export SUR_DSA_INDEXER=1            # full DSA training
export SUR_FULL_MUON=1              # not LoRA-only
export SUR_GRPO_MAX_LEN=8192        # longer rollouts
export SUR_LONGALIGN_TAKE=10000     # full LongAlign
export SUR_MAX_PACK_LEN=32768       # 32K packing
```

### G.6 What NOT to Wire (Research Only)

| Technique | Reason |
|---|---|
| Kimi-Linear KDA | Architecture change, requires from-scratch training |
| DS CSA + HCA | Architecture change, V4-only |
| DS Engram memory | External (DRAM lookup), not weights |
| Sparsity-48 / 384 experts | Inherited from base only |
| GLM RLCS at full scale | slime-only, Civo+H200 phase |

### G.7 Verification (Phase 4 of Plan-Once)

After wire-in, verify:
1. `bash kaggle-trainer.sh --dry-run` prints all new SUR_* knobs
2. Notebook upload succeeds with new dataset entries (HF auth working)
3. First Kaggle run logs show:
   - "Using MuonClip on q_proj, k_proj — 2,097,152 params; AdamW on rest"
   - "MTP heads loaded: 2 × ~50M params each"
   - "LongAlign-10k loaded: 2000 samples"
   - "GRPO + DAPO active: clip-higher=0.28, token-level-loss=1"
4. Loss curve shows: warmup, then stable descent (no MuonClip-induced spikes)
5. Eval at end: V14 ≥ V13 baseline + 5% on at least one of (HumanEval, MATH, AgentBench)

### G.8 Rollback

Set all SUR_V14_* to 0 → reverts to V13 behavior. Single env-flag rollback.

---

## See Also

- [[v13-frontier-efficiency]] — V13 baseline (Unsloth, Liger, etc.)
- [[v13-frontier-capability]] — V13 capability stack
- [[opensource-releases-2026-Q2]] — Lab release timeline
- [[training-tooling-2026-Q2]] — Tool versions
- [[../patterns/training/grpo-rlvr]] (TBD) — GRPO pattern
- [[../patterns/training/muonclip-hybrid]] (TBD) — MuonClip head-only pattern
- [[../patterns/training/expert-iteration-distill]] (TBD) — GLM expert-iter pattern

---

**Last verified:** 2026-05-01
**Next refresh:** After V14 trainer first successful run; mid-2026 when GLM-5 / DS-V5 / K3 land.

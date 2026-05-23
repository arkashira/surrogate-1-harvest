---
date: 2026-04-29
type: research
project: surrogate1
focus: SOTA training techniques for code LLMs (2025-2026)
base_model: Qwen2.5-Coder-7B-Instruct
current_state: 1 epoch SFT, 1329 samples, LoRA r=32 alpha=64 lr=2e-4 cosine, ctx=2048, loss 1.331→0.7
goals: bigger context, better coding, sharper reasoning
tags: [llm-training, lora, dpo, rlef, grpo, context-extension, surrogate1-v2]
---

# SOTA Training Techniques for Code LLMs (2025-2026) — Research for Surrogate-1 v2

> Comprehensive audit of post-training techniques to upgrade Surrogate-1 from a single-epoch SFT baseline into a competitive code/DevSecOps model. Each section gives paper, key insight, recipe, and a Surrogate-1 applicability score (1-5).

---

## Table of Contents

1. [Preference Optimization Family (DPO / IPO / KTO / ORPO / SimPO)](#1-preference-optimization-family)
2. [Multi-Stage Training Pipelines That Actually Ship](#2-multi-stage-training-pipelines)
3. [LoRA Hyperparameter Sweet Spots for Code](#3-lora-hyperparameter-sweet-spots)
4. [LoRA Variants — DoRA, PiSSA, rsLoRA, QLoRA](#4-lora-variants)
5. [RL from Execution Feedback (RLEF) and GRPO for Code](#5-rl-from-execution-feedback)
6. [Context Extension — RoPE Scaling, YaRN, LongRoPE](#6-context-extension)
7. [Self-Improvement Loops](#7-self-improvement-loops)
8. [Code-Specific Preference-Data Construction](#8-code-specific-preference-data)
9. [Surrogate-1 v2 Recommendation — Concrete Plan](#9-surrogate-1-v2-plan)

---

## 1. Preference Optimization Family

### 1.1 DPO — Direct Preference Optimization

- **Paper**: Rafailov et al., "Direct Preference Optimization: Your Language Model is Secretly a Reward Model", arXiv:2305.18290 (2023, last revised 2024).
- **Key insight**: Skip the reward model. The Bradley-Terry preference probability has a closed-form policy solution; minimize a sigmoid-classifier loss over (chosen, rejected) pairs against a frozen reference.
- **Loss**:

$$
\mathcal{L}_{\mathrm{DPO}}(\theta) = -\mathbb{E}_{(x,y^{+},y^{-})}\!\left[\log \sigma\!\left(\beta\Big(\log\frac{\pi_{\theta}(y^{+}\!\mid x)}{\pi_{\mathrm{ref}}(y^{+}\!\mid x)}-\log \frac{\pi_{\theta}(y^{-}\!\mid x)}{\pi_{\mathrm{ref}}(y^{-}\!\mid x)}\Big)\right)\right]
$$

- **2025 hyperparameter consensus** (TRL defaults + Phil Schmid 2025 guide + alignment-handbook sweep):
  - `learning_rate`: **5e-7 to 1e-6** (full fine-tune); **1e-5** for LoRA adapters (TRL tip). 10–100x smaller than SFT lr.
  - `beta`: **0.01–0.1** start, sweep to 0.5 if data is high-confidence. Smaller beta = more aggressive divergence from reference.
  - `epochs`: **1–3** (DPO overfits fast; watch `rewards/margins` plateau).
  - `loss_type`: `sigmoid` (default), `ipo` (regularized, less overfit), `robust` (label-flip noise), `apo_zero` (boost winners — use when model underperforms).
  - `max_length`: 1536 prompt+response; `max_prompt_length`: 768.
  - LR schedule: **constant + 3% warmup** (Phil Schmid 2025) OR cosine.
- **Failure modes** (Smaug paper, arXiv:2402.13228):
  - **Likelihood collapse**: chosen log-prob drops even though margin grows — fix with **DPO-Positive (DPOP)** (add penalty when chosen log-prob drops below SFT reference) or use a higher beta.
  - **Reference-model dependence**: garbage SFT → garbage DPO. Always run SFT first on same domain.
  - **Out-of-distribution overfit**: switch to IPO (`loss_type="ipo"`) — adds regularization on log-ratio magnitude.
- **Surrogate-1 applicability**: **5/5** — Single biggest win. With 1.3K SFT examples already, generate 4 completions per prompt at temperature 1.0, judge with a stronger model (or execution feedback), build ~1500–2000 preference pairs, train 1 epoch DPO at lr=5e-7, beta=0.1.

```python
# TRL DPO recipe for Surrogate-1 v2 (LoRA, single H100/A100)
from datasets import load_dataset
from trl import DPOTrainer, DPOConfig
from peft import LoraConfig

cfg = DPOConfig(
    output_dir="surrogate1-v2-dpo",
    learning_rate=1e-5,            # LoRA lr (full FT would be 5e-7)
    beta=0.1,                       # standard start
    loss_type="sigmoid",            # try "ipo" if overfit
    max_length=4096,                # 4K context post-YaRN
    max_prompt_length=2048,
    num_train_epochs=1,
    per_device_train_batch_size=2,
    gradient_accumulation_steps=8,
    bf16=True,
    gradient_checkpointing=True,
    warmup_ratio=0.03,
    lr_scheduler_type="constant",   # safer than cosine for DPO
    logging_steps=10,
    save_strategy="epoch",
)

trainer = DPOTrainer(
    model="Qwen/Qwen2.5-Coder-7B-Instruct",  # or your SFT checkpoint
    args=cfg,
    train_dataset=load_dataset("json", data_files="surrogate1_dpo_pairs.json")["train"],
    peft_config=LoraConfig(
        r=32, lora_alpha=64,
        target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
        lora_dropout=0.0, bias="none", task_type="CAUSAL_LM",
    ),
)
trainer.train()
```

---

### 1.2 IPO — Identity Preference Optimization

- **Paper**: Azar et al., "A General Theoretical Paradigm to Understand Learning from Human Preferences", arXiv:2310.12036 (2023).
- **Key insight**: DPO over-fits when chosen prob → 1 (saturates sigmoid). IPO replaces logit transform with identity, regularizing log-ratio magnitude.
- **When to use**: When you have NOISY preferences (e.g., LLM-judged with high disagreement) or small dataset (<2K pairs). IPO won't push the model into the sigmoid tail.
- **TRL**: `loss_type="ipo"` in DPOConfig. Beta tends to be smaller (~0.01).
- **Surrogate-1 applicability**: **3/5** — Useful as a fallback if DPO overfits on 1.5–2K pairs. Try after DPO baseline.

---

### 1.3 KTO — Kahneman-Tversky Optimization

- **Paper**: Ethayarajh et al., "KTO: Model Alignment as Prospect Theoretic Optimization", arXiv:2402.01306 (2024).
- **Key insight**: Don't need PAIRS. Just binary "good"/"bad" labels per sample. Models loss-aversion (penalize bad outputs harder than reward good ones), inspired by Kahneman-Tversky prospect theory.
- **Why it matters for code**: You can label single samples as PASS/FAIL based on test execution — no need to construct chosen-vs-rejected pairs from the same prompt.
- **TRL**: `from trl.experimental.kto import KTOTrainer, KTOConfig` (note: experimental as of TRL v1.0).
- **Hyperparameters**: beta=0.1, lr=5e-7, dataset must have `desirable`/`undesirable` boolean column.
- **Performance**: Matches or exceeds DPO at 1B-30B scales when data is binary-only (Contextual AI blog).
- **Surrogate-1 applicability**: **4/5** — Excellent if you instrument execution: every code sample executed → desirable if PASS, undesirable if FAIL. Skips the pair-construction headache. Recommend as *alternative* to DPO if you can stand up a sandbox.

```python
# KTO recipe — single-sample binary labels
from trl.experimental.kto import KTOTrainer, KTOConfig

cfg = KTOConfig(
    output_dir="surrogate1-v2-kto",
    learning_rate=5e-7,
    beta=0.1,
    desirable_weight=1.0,
    undesirable_weight=1.0,    # increase to 1.5 if you have more bad than good
    max_length=4096,
    num_train_epochs=1,
    per_device_train_batch_size=4,
    bf16=True,
)
# Dataset row: {"prompt": "...", "completion": "...", "label": True}  # PASS = True
```

---

### 1.4 ORPO — Odds Ratio Preference Optimization

- **Paper**: Hong, Lee, Thorne, "ORPO: Monolithic Preference Optimization without Reference Model", arXiv:2403.07691 (2024). EMNLP 2024.
- **Key insight**: **Skip SFT and reference model entirely.** ORPO combines a standard SFT loss with an odds-ratio penalty that pushes down rejected log-likelihood. One-stage training.
- **Loss**: `L_ORPO = L_SFT + λ · L_OR` where `L_OR = -log σ(log(odds(y_chosen|x) / odds(y_rejected|x)))`.
- **When to use**:
  - Building a model from scratch with preference data only (no separate SFT phase).
  - Highly imbalanced data (10:1 rejected:chosen) — odds ratio normalizes vs sampling bias.
- **Reported gains**: Phi-2/Mistral-7B/Llama-2-7B on UltraFeedback alone (no SFT) hit AlpacaEval 2 = 12.20%, MT-Bench = 7.32 — surpassing many 13B SFT'd models.
- **Surrogate-1 applicability**: **2/5** — Less useful since you've already done SFT. Could use it for a *fresh* run from base + bigger preference dataset, but not the right tool for your incremental upgrade.

---

### 1.5 SimPO — Simple Preference Optimization

- **Paper**: Meng, Xia, Chen, "SimPO: Simple Preference Optimization with a Reference-Free Reward", arXiv:2405.14734 (NeurIPS 2024).
- **Key insight**: Drop reference model. Use **average log-prob** of sequence (length-normalized) as implicit reward, plus a **target margin γ**.
- **Loss**:

$$
\mathcal{L}_{\mathrm{SimPO}} = -\mathbb{E}\left[\log \sigma\left(\frac{\beta}{|y_w|}\log \pi_\theta(y_w|x) - \frac{\beta}{|y_l|}\log \pi_\theta(y_l|x) - \gamma\right)\right]
$$

- **Hyperparameters**:
  - `beta`: **2.0–10.0** (much higher than DPO's 0.1!)
  - `gamma_beta_ratio`: **0.5** (γ/β), sweep 0–1.
- **Reported gains**: +6.4 on AlpacaEval 2, +7.5 on Arena-Hard vs DPO at same model.
- **Pros**: No reference model = less VRAM (no need to load πref). Reference-free = no reference-collapse failure.
- **Cons**: Can be more aggressive — risk of mode collapse without good data.
- **Surrogate-1 applicability**: **4/5** — Compelling alternative to DPO. **For a small 24GB Mac**, the no-reference-model property halves memory. If you go DPO and run OOM, switch to SimPO. Implemented as CPO trainer with SimPO loss in Axolotl.

---

### 1.6 Quick-Reference Comparison Table

| Method | Reference Model? | Pairs Required? | Beta Range | Best For | Surrogate-1 Fit |
|--------|------------------|-----------------|------------|----------|-----------------|
| **DPO** | Yes (frozen SFT) | Yes | 0.01–0.5 | Default, robust, well-tooled | **5/5** |
| **IPO** | Yes | Yes | ~0.01 | Noisy preferences, small data | 3/5 |
| **KTO** | Yes | NO (binary labels) | 0.1 | Execution-graded data, asymmetric loss | **4/5** |
| **ORPO** | NO | Yes | λ=0.1–0.5 | Skip SFT, fresh run | 2/5 |
| **SimPO** | NO | Yes | 2.0–10.0 | VRAM-constrained, aggressive | **4/5** |
| **GRPO** | NO (group baseline) | Multi-completions | n/a | Verifiable rewards, code execution | **5/5** (see §5) |

**Recommended pipeline for Surrogate-1 v2**: SFT → DPO → (optional) GRPO with execution rewards.

---

## 2. Multi-Stage Training Pipelines

### 2.1 The Modern 4-Stage Recipe

Based on DeepSeek-V3, Qwen2.5/3-Coder, OpenCoder, DeepSeek-R1:

```
[1] Continued Pre-training (CPT)        → domain shift to code
[2] Mid-training / Annealing            → quality bump on curated data
[3] Supervised Fine-Tuning (SFT)        → instruction-following
[4] Preference Optimization (DPO/RL)    → preference + execution alignment
```

### 2.2 DeepSeek-V3 / Coder-V2 Recipe

- **Paper**: DeepSeek-V3 Technical Report, arXiv:2412.19437 (2024); DeepSeek-Coder-V2 arXiv:2406.11931.
- **Stages**:
  1. Pre-training: 14.8T tokens, two-stage context extension 4K → 32K → 128K via YaRN.
  2. SFT: lr=1e-5, ~10K steps, 1.5M instruction examples.
  3. **Knowledge distillation from R1 long-CoT** model into V3 via supervised distillation.
  4. **GRPO** RL phase using compiler/test rewards for code domain (no separate critic model).
- **Data composition (Coder-V2 CPT)**: 60% source code / 10% math / 30% NL.
- **Learning-rate schedule**: 80%:10%:10% three-stage decay.
- **Surrogate-1 applicability**: **3/5** for stages 1-2 (you don't have CPT compute), **5/5** for stages 3-4 (this is your roadmap).

### 2.3 Qwen2.5-Coder Three-Stage Recipe

- **Paper**: arXiv:2409.12186.
- **Stages**:
  1. **File-level CPT**: 8192 ctx, 5.2T tokens, NTP + Fill-in-the-Middle (FIM).
  2. **Repo-level CPT**: ctx 8K → 32K, **RoPE base 10K → 1,000,000**, ~300B tokens. YaRN extends to 131K.
  3. **Instruction tuning**: Coarse-to-fine. Tens-of-millions diverse → millions high-quality with rejection sampling.
- **Key takeaway**: They didn't do DPO. Their alignment is rejection-sampled SFT + repo-level FIM augmentation.
- **Surrogate-1 applicability**: **5/5** for the RoPE-base trick alone (10K → 1M for context extension). See §6.

### 2.4 Qwen3 Four-Stage Post-Training

- **Stages**: (1) long-CoT cold-start SFT, (2) reasoning RL with rule-based rewards, (3) thinking-mode fusion, (4) general RL on 20+ task domains.
- **Insight**: Stage 2 uses **rule-based rewards** (regex match, test pass) — no reward model. Avoids reward hacking.
- **Surrogate-1 applicability**: **3/5** — stages 1-2 are reproducible. Stage 3 (thinking-mode fusion) needs significant data engineering.

### 2.5 OpenCoder Recipe (Most Reproducible)

- **Paper**: Huang et al., "OpenCoder: The Open Cookbook for Top-Tier Code LLMs", arXiv:2411.04905. ACL 2025.
- **Why it matters**: **Open data + open code** — the only top-tier code LLM you can fully reproduce.
- **Pre-training**: RefineCode (960B tokens, 607 langs). Heuristic dedup (SHA256 + LSH fuzzy), copyright/PII strip, language-balanced downsampling (Java 409→200GB, HTML 213→64GB).
- **Synthetic SFT**: Three pipelines: filtered_infinity_instruct, real-user_instruct (extracted from GPT logs), large-scale_diverse_instruct (CommonCrawl-seeded).
- **Key insights**:
  - 90% raw code / 10% code-related web text.
  - **Annealing stage** uses 10x higher-quality synthetic data at lower lr — biggest single jump in benchmark scores.
- **Surrogate-1 applicability**: **4/5** — The pre-training data pipeline (`opc_data_filtering`) is reusable for cleaning your DevSecOps corpus. The annealing-on-synthetic insight is the cheap win.

### 2.6 Optimal Data Ratios (2025 Practitioner Consensus)

| Stage | Tokens / Examples | Learning Rate | Notes |
|-------|-------------------|---------------|-------|
| CPT | 10–100B (domain), if available | 1e-5 to 5e-5 | High lr, cosine, longer warmup |
| Annealing (mid-training) | 1–10B curated | 5e-6 to 1e-5 | Decay to 0; HIGHEST-quality data here |
| SFT stage 1 (diverse) | 100K–1M | 5e-6 to 2e-5 | Broad coverage |
| SFT stage 2 (high-quality) | 10K–100K | 2e-6 to 1e-5 | Rejection-sampled, multi-source judge |
| DPO/KTO | 1K–50K pairs | 5e-7 to 1e-6 (full FT) / 1e-5 (LoRA) | beta 0.01–0.1 |
| GRPO/RLEF | 5K–100K prompts × N rollouts | 1e-7 to 5e-7 | Slowest, needs sandbox |

For Surrogate-1 (1.3K samples), you're already past stages 1-3. Focus on **stage 2 SFT (curated DevSecOps Q&A)** + **DPO**.

---

## 3. LoRA Hyperparameter Sweet Spots

### 3.1 Rank `r` — When does each pay off?

| Rank | When to use | Trade-off |
|------|-------------|-----------|
| 4–8 | Style/persona/format only (small behavior delta) | Cheapest, fastest |
| 16 | **Default for SFT on instruction-following** | 90% of full FT quality on most tasks |
| 32 | **Code/math SFT, complex domains** ← Surrogate-1's current setting | Sweet spot for code tasks |
| 64 | Adding new factual knowledge, multi-domain | Use **rsLoRA** to avoid gradient instability |
| 128–256 | Almost full FT for niche expertise | Use **rsLoRA + PiSSA**, training cost ~1.1x of r=32 |

**Surrogate-1 verdict**: r=32 is correct. **Don't lower it.** If quality plateaus (which yours has), don't raise rank — switch optimizer (DPO) or method (DoRA/PiSSA), not rank.

### 3.2 Alpha — Is `α = 2r` still valid?

- **Short answer**: Yes, *as a starting heuristic*. Sebastian Raschka's 2025 sweeps confirm α = 2r is sweet spot for most r values.
- **Caveats**:
  - At r=256, α=128 (0.5× scaling) sometimes outperforms α=512.
  - For **rsLoRA**, the formula changes to `α = scale · √r`, e.g., r=64 → α≈8·√64 = 64.
- **Effective scaling factor**: `α / r` (LoRA) or `α / √r` (rsLoRA) — what really matters.
- **Surrogate-1 verdict**: Your α=64, r=32 → 2× scaling = correct. Keep it.

### 3.3 Target Modules — Attention only vs MLP

**2025 consensus from Unsloth, NVIDIA NeMo, HuggingFace PEFT, and Amazon Science**:

```python
# RECOMMENDED for code LLMs (Qwen2.5-Coder, Llama, Mistral)
target_modules = [
    "q_proj", "k_proj", "v_proj", "o_proj",     # attention
    "gate_proj", "up_proj", "down_proj",         # MLP (the 60% of params!)
]
# OR equivalently in PEFT 0.10+:
target_modules = "all-linear"
```

**Why MLP matters**:
- For Qwen2.5-Coder-7B, MLP is ~70% of trainable params.
- Attention-only LoRA = adapting "where to look", missing "what to compute".
- Empirical (Amazon Science 2025): targeting all linear layers vs attention-only = **+3-7 pts on HumanEval+** for the same r.

**When attention-only is OK**:
- Pure style transfer (no new logic).
- Latency-constrained inference (o_proj alone = -22.6% latency, ~2% accuracy loss per Amazon study).

**Surrogate-1 verdict**: If you're currently `q_proj, v_proj` only, **upgrade to all 7 modules**. This is likely 30-40% of your unrealized gain.

### 3.4 Dropout, Bias, LR

| Param | Recommendation | Source |
|-------|----------------|--------|
| `lora_dropout` | **0.0** (default) — Unsloth notes it's unreliable for short runs <3 epochs | Unsloth 2025 |
| `bias` | `"none"` | Standard |
| `learning_rate` (SFT-LoRA) | **2e-4** | Unsloth, Databricks 2025 |
| `weight_decay` | 0.01 | Standard |
| Optimizer | `adamw_torch_fused` (or `paged_adamw_8bit` for QLoRA) | Memory + speed |
| LR scheduler | `cosine` with 3-10% warmup | Default |
| `gradient_checkpointing` | `"unsloth"` or `True` | Saves 30% VRAM |

### 3.5 Surrogate-1 Hyperparameter Audit

| Param | Current | v2 Recommendation | Reason |
|-------|---------|-------------------|--------|
| `r` | 32 | **32** (keep) | Sweet spot for code |
| `alpha` | 64 | **64** (keep) | 2r heuristic still good |
| `target_modules` | (likely q,v only?) | **all 7 linear** | Big lift if currently partial |
| `dropout` | ? | **0.0** | Short run |
| `lr` | 2e-4 | **2e-4** SFT, **1e-5** DPO LoRA | Standard |
| `epochs` | 1 | **2-3 SFT, 1 DPO** | 1 epoch SFT often undertrained |
| `use_rslora` | False | **True if r≥64**, else False | Safety net |
| `use_dora` | False | **Try True** | +1-3 pts for free |
| Quantization | ? | **4-bit QLoRA** if RAM tight | DoRA + 4-bit = QDoRA |

---

## 4. LoRA Variants

### 4.1 QLoRA — 4-bit base + LoRA adapter

- **Paper**: Dettmers et al., arXiv:2305.14314.
- **What**: Quantize base to NF4, train LoRA in bf16. Saves ~75% VRAM.
- **Quality**: 80–90% of full FT quality.
- **Use when**: Base model + activations don't fit in VRAM (Qwen2.5-Coder-7B at full bf16 = ~14GB params; 4-bit = 4GB).
- **Surrogate-1**: Mac M3 24GB → **probably needed if context goes >4K**. Use `bnb_4bit_quant_type="nf4"`, `bnb_4bit_compute_dtype=torch.bfloat16`.

### 4.2 DoRA — Weight-Decomposed LoRA

- **Paper**: Liu et al. (NVIDIA, HKUST), "DoRA: Weight-Decomposed Low-Rank Adaptation", arXiv:2402.09353. ICML 2024 Oral.
- **Insight**: Decompose `W = m · (V/||V||)` into magnitude `m` (scalar per col) + direction `V`. Apply LoRA only to direction; train magnitude as a vector.
- **Result**: Consistently outperforms LoRA on Llama, LLaVA, VL-BART. Same trainable param budget. Works at low ranks where LoRA struggles.
- **PEFT support**: `LoraConfig(use_dora=True, ...)`.
- **Catch**: ~5-10% slower per step than LoRA (extra magnitude term).
- **Surrogate-1 applicability**: **4/5** — Drop-in upgrade. Use as your default for v2.

```python
from peft import LoraConfig
config = LoraConfig(
    r=32, lora_alpha=64,
    target_modules="all-linear",
    use_dora=True,            # ← single flag
    task_type="CAUSAL_LM",
)
```

### 4.3 PiSSA — Principal Singular Adaptation

- **Paper**: Meng et al., NeurIPS 2024 Spotlight, arXiv:2404.02948.
- **Insight**: Initialize LoRA A,B from **top-r SVD components** of W (not random Gaussian + zeros). Residual W^res = W - AB is frozen.
- **Result**: Faster convergence + higher final accuracy. Gemma-7B GSM8K: 77.7% (PiSSA) vs 74.5% (LoRA).
- **Quantized variant (QPiSSA)**: Llama-3-70B GSM8K = 86.05% vs QLoRA 81.73%.
- **PEFT support**: `LoraConfig(init_lora_weights="pissa")`.
- **Catch**: 1-time SVD on init (a few seconds). Quantization error is reduced because the residual is the "less informative" part.
- **Surrogate-1 applicability**: **4/5** — Free quality bump. Combine with DoRA cautiously (test first; some report DoRA + PiSSA is unstable).

### 4.4 rsLoRA — Rank-Stabilized LoRA

- **Paper**: Kalajdzievski, "A Rank Stabilization Scaling Factor for Fine-Tuning with LoRA", arXiv:2312.03732.
- **Insight**: Standard LoRA scaling `α/r` causes gradient vanishing as r grows. Replace with `α/√r` for stable gradients at high rank.
- **PEFT**: `LoraConfig(use_rslora=True)`.
- **When to use**: r ≥ 64. At r=32, marginal benefit. At r=256, **+0.16 MT-Bench** over LoRA r=16 (Damjan-K HF blog).
- **Surrogate-1 applicability**: **2/5** at r=32 (your current). **5/5** if you bump to r=64+ for new factual knowledge (DevSecOps lore).

### 4.5 Comparison Matrix

| Method | Quality vs Full FT | VRAM | Training speed | Inference cost | Setup complexity |
|--------|--------------------|------|----------------|----------------|------------------|
| LoRA | 90-95% | Low | Fast | None (merge) | Trivial |
| QLoRA | 80-90% | Lowest | Med | None | Easy |
| DoRA | 95-98% | Low | -10% | None | Trivial (1 flag) |
| QDoRA | 85-92% | Lowest | -15% | None | Easy |
| PiSSA | 92-96% | Low | Fast | None | Trivial (1 flag) |
| QPiSSA | 88-94% | Lowest | Fast | None | Easy |
| rsLoRA | 92-96% (r≥64) | Low | Fast | None | Trivial (1 flag) |

**Once LR is properly tuned, all methods peak at similar performance (within ~0.5% of each other on instruction tasks)** — Kaitchup 2025.

So **method choice matters less than LR tuning, target_modules, and data quality**. Pick DoRA + all-linear + good LR sweep, move on.

---

## 5. RL from Execution Feedback

### 5.1 RLEF — The Headline Paper

- **Paper**: Gehring et al. (Meta FAIR), "RLEF: Grounding Code LLMs in Execution Feedback with Reinforcement Learning", arXiv:2410.02089. ICML 2025.
- **Setup** (Markov Decision Process):
  - **State**: Problem description + conversation history.
  - **Action**: Generate code.
  - **Transition**: Run code in sandbox → get test results → format as feedback message.
  - **Reward**:
    - +1 if all public + private tests pass at episode end.
    - -1 if any test fails at end.
    - -0.2 for invalid code in non-final responses.
    - KL penalty: `-β · log(π/πref)` with β=0.05.
- **Algorithm**: PPO with token-level policy + **turn-level value function** ("response-based value estimation": predict value from last token of each response).
- **Hyperparameters** (Llama-3.1-8B):
  - Optimizer: AdamW, lr=2e-7, weight_decay=0.1
  - Warmup: 50 linear steps
  - PPO clipping ε=0.2, value clip α=0.2
  - γ=1.0 (no discounting in episode)
  - 1024 rollouts/update, batches of 256
  - 12,000 updates (8B), 8,000 (70B)
  - Sampling temp=1.0
- **Sandbox**: Python 3.10, 1GB memory cap, 10s wall-clock per test (CodeContests eval codebase).
- **Result**:
  - CodeContests: 8B model jumps from ~10% to ~25% pass@1 with 3 turns. Generalizes to HumanEval+, MBPP+.
  - Reduces samples needed for solve@K by **10x**.
- **Surrogate-1 applicability**: **3/5** — Powerful but heavy. Requires PPO infra, sandbox, and significant GPU budget. **Out of scope for v2 unless you rent rented H100s for 3-5 days**.

### 5.2 GRPO — The Practical RL Choice

- **Paper**: Shao et al., "DeepSeekMath: Pushing the Limits of Mathematical Reasoning", arXiv:2402.03300 (introduces GRPO). DeepSeek-R1 (arXiv:2501.12948) made it famous.
- **Insight**: PPO needs a critic model (doubles VRAM). GRPO replaces critic with **group baseline**: sample G completions per prompt, advantage = `(r_i - mean(r)) / std(r)`. No critic.
- **Why it's the 2025 default**: DeepSeek-R1, Qwen3, DeepSeek-Coder-V2 all use it.
- **Axolotl support**: First-class. `rl: grpo` with custom `rewards.py` returning `list[float]` per completion.

```yaml
# Axolotl GRPO config for code execution rewards
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
rl: grpo
trl:
  num_generations: 8           # G = 8 completions per prompt
  max_completion_length: 1024
  beta: 0.04                    # KL coefficient (lower = more exploration)
  reward_funcs:
    - rewards.test_pass_reward      # +1 if pytest passes
    - rewards.format_reward          # +0.1 if has correct fence/structure
  reward_weights: [1.0, 0.1]
vllm:
  host: "0.0.0.0"
  port: 8000
  tensor_parallel_size: 1
datasets:
  - path: ./surrogate1_grpo_prompts.json
    type: prompt_completion
adapter: lora
lora_r: 32
lora_alpha: 64
lora_target_modules: ["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"]
learning_rate: 5.0e-7
warmup_ratio: 0.03
num_epochs: 1
micro_batch_size: 1
gradient_accumulation_steps: 16
bf16: true
```

```python
# rewards.py for GRPO
import subprocess, tempfile, os, json

def test_pass_reward(completions, **kwargs):
    """+1 if generated code passes ground-truth tests, else 0."""
    tests = kwargs["tests"]   # list of test cases from dataset row
    rewards = []
    for completion, test_set in zip(completions, tests):
        code = extract_code(completion[-1]["content"])  # last assistant turn
        with tempfile.TemporaryDirectory() as tmp:
            (open(f"{tmp}/sol.py","w")).write(code)
            (open(f"{tmp}/test_sol.py","w")).write(test_set)
            try:
                r = subprocess.run(
                    ["python","-m","pytest","-x","-q",f"{tmp}/test_sol.py"],
                    cwd=tmp, timeout=10, capture_output=True,
                )
                rewards.append(1.0 if r.returncode == 0 else 0.0)
            except subprocess.TimeoutExpired:
                rewards.append(0.0)
    return rewards

def format_reward(completions, **kwargs):
    """+1 if response has ```python ... ``` fenced block, else 0."""
    return [1.0 if "```python" in c[-1]["content"] else 0.0 for c in completions]
```

- **Surrogate-1 applicability**: **5/5** — This is the most cost-effective RL approach. Use it AFTER you have a stable DPO checkpoint. For DevSecOps, your "tests" can be: scanner output checks (Prowler/Trivy passes), CFN-lint validation, terraform plan succeeds, etc.

### 5.3 RLTF — Earlier Variant

- **Paper**: Liu et al., "RLTF: Reinforcement Learning from Unit Test Feedback", ICLR 2024 (OpenReview hjYmsV6nXZ).
- **Insight**: Granular reward signals from compile errors, runtime errors, test failures (vs binary pass/fail).
- **Surrogate-1 applicability**: **3/5** — Good reference for designing graded rewards. Your "format_reward" + "syntax_reward" + "test_reward" structure can borrow this idea.

### 5.4 Sandbox Tools

| Tool | Purpose | License | Notes |
|------|---------|---------|-------|
| `llm-sandbox` (vndee) | Multi-language Docker sandbox, MCP server | MIT | Plug-and-play |
| `ai-code-sandbox` (typper-io) | Python-only, Docker | MIT | Lightweight |
| `cohere-terrarium` | Python sandbox | Apache | Cohere internal-style |
| Modal/Lightning sandboxes | Cloud-managed | Paid | Best for scale |
| Custom Docker | Roll-your-own | — | Full control |

**Recommendation for Surrogate-1**: Use `llm-sandbox` for prototyping, custom Docker for production. Your DevSecOps domain wants extra tools (`prowler`, `trivy`, `tflint`, `cfn-lint`) inside the sandbox image.

---

## 6. Context Extension

### 6.1 The Problem

Surrogate-1 currently trains at 2048 ctx. Modern code tasks need 8K–32K (whole file + tests + dependencies). Qwen2.5-Coder-7B base supports 128K via YaRN, but **fine-tuning at 2K erases that capability** if RoPE/positional embeddings drift.

### 6.2 RoPE Scaling — The Family

| Method | Year | How it works | Pros | Cons |
|--------|------|--------------|------|------|
| **Linear interpolation** | 2023 (kaiokendev) | Divide position indices by factor | Trivial | Bad at long range |
| **NTK-aware** | 2023 | Scale only high-freq dims (preserve high-freq for short text) | Better than linear | Still imperfect |
| **NTK-by-parts** | 2023 | Different scaling per freq band | Better | Hand-tuned |
| **Dynamic NTK** | 2023 | Scale changes with seq len | Adaptive | Slower, no caching |
| **YaRN** | 2023 (Peng et al.) | NTK-by-parts + attention temperature scaling | SOTA quality with minimal training | Static at chosen factor |
| **LongRoPE** | 2024 (Microsoft) | Search non-uniform per-dim scaling + progressive 256K→2M | 2M+ context with 1K fine-tune steps | Complex search |

### 6.3 YaRN Recipe for Qwen2.5

- **Paper**: Peng et al., "YaRN: Efficient Context Window Extension", arXiv:2309.00071.
- **Key insight**: Combines NTK-by-parts interpolation + attention temperature scaling. Reaches SOTA with fine-tuning on **<0.1% of pre-training tokens**.

**Config-only enable** (no fine-tuning needed for inference):

```json
// Qwen2.5-Coder-7B config.json — extends to 128K
{
  "max_position_embeddings": 32768,
  "rope_scaling": {
    "factor": 4.0,
    "type": "yarn"
  }
}
```

For vLLM: also add `"original_max_position_embeddings": 32768`.

**Factor selection**:
- Factor 2.0 → 64K
- Factor 4.0 → 128K (Qwen-recommended)
- Quality degrades on shorter contexts at higher factors.

### 6.4 Fine-Tuning at Long Context — The Recipe

Qwen2.5-Coder team uses RoPE-base bump from 10,000 → **1,000,000** for repo-level pretraining (8K→32K). For your case:

```python
# Step 1: Load base + bump RoPE base + enable YaRN scaling
from transformers import AutoConfig, AutoModelForCausalLM
config = AutoConfig.from_pretrained("Qwen/Qwen2.5-Coder-7B-Instruct")
config.rope_theta = 1_000_000          # was 10,000
config.rope_scaling = {"type": "yarn", "factor": 2.0}  # 32K → 64K
config.max_position_embeddings = 32768
model = AutoModelForCausalLM.from_pretrained(
    "Qwen/Qwen2.5-Coder-7B-Instruct",
    config=config,
    torch_dtype=torch.bfloat16,
)
```

```yaml
# Axolotl YAML for context extension fine-tune
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
sequence_len: 8192                    # ← bump from 2048
sample_packing: true
pad_to_sequence_len: true
overrides_of_model_config:
  rope_theta: 1000000
  rope_scaling:
    type: yarn
    factor: 2.0

# Use long-context dataset for FT
datasets:
  - path: ./surrogate1_long_examples.jsonl
    type: chat_template

# LoRA + DoRA + sample packing
adapter: lora
lora_r: 32
lora_alpha: 64
lora_dropout: 0.0
peft_use_dora: true
lora_target_modules: ["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"]

# Memory savers (Mac M3 24GB)
load_in_4bit: true
flash_attention: true
gradient_checkpointing: true
micro_batch_size: 1
gradient_accumulation_steps: 16
optimizer: paged_adamw_8bit
learning_rate: 1e-5                    # lower for long-ctx FT
warmup_ratio: 0.05
num_epochs: 1
bf16: true
```

### 6.5 Practical Targets for Surrogate-1

| Goal | Approach | Cost | Surrogate-1 Fit |
|------|----------|------|-----------------|
| **2K → 8K** | Bump `sequence_len`, no RoPE change needed | ~2x VRAM, ~3x time | **5/5** — start here |
| **2K → 32K** | YaRN factor=2 + sequence_len 8192 (train), serve at 32K | 4x VRAM during train | 4/5 |
| **2K → 128K** | YaRN factor=4 + LongRoPE ideas | Real cost | 2/5 |

**For Surrogate-1 v2**: Aim for **8K context** during training (config trick: YaRN+RoPE config-only at inference can reach 32K without retrain). Don't try 128K yet — your data isn't long enough to benefit.

---

## 7. Self-Improvement Loops

### 7.1 Self-Rewarding Language Models

- **Paper**: Yuan et al. (Meta FAIR), arXiv:2401.10020 (2024).
- **Loop**:
  1. Model M_t generates K responses per prompt.
  2. **Same model M_t** judges them via LLM-as-Judge prompting (rates 1–5).
  3. Highest + lowest scored = preference pair.
  4. Train M_{t+1} via DPO on collected pairs.
  5. Repeat. Llama-2-70B → 3 iters → beats Claude 2 / Gemini Pro / GPT-4-0613 on AlpacaEval 2.
- **Caveat**: Risk of self-bias amplification ("model judges its own style as good"). Mitigated by **Meta-Rewarding** (judge the judge — Wu et al., NeurIPS 2024).
- **Surrogate-1 applicability**: **3/5** — Useful late-stage. **Don't run on 7B models — judging quality too noisy**. Use a stronger judge (GPT-4o, Claude 3.5).

### 7.2 ReST — Reinforced Self-Training

- **Paper**: Gulcehre et al. (DeepMind), arXiv:2308.08998 (2023). Extended in "Beyond Human Data" arXiv:2312.06585 (DeepMind, ICLR 2024).
- **Loop**:
  - **Grow**: Sample N candidates per prompt from current policy.
  - **Improve**: Filter/rank via reward model or rule-based scorer; train via offline RL (DPO/MLE on top-quartile).
  - Iterate.
- **Why it's nice**: Offline (no online critic), reuses data across iterations. Cheaper than PPO.
- **Surrogate-1 applicability**: **4/5** — Natural fit. With execution as your reward, it's basically: generate → execute → keep top-K passing → SFT/DPO on those → repeat.

### 7.3 Voyager — Skill Library (Conceptual Borrow)

- **Paper**: Wang et al. (NVIDIA), "Voyager: An Open-Ended Embodied Agent", arXiv:2305.16291 (2023). NeurIPS 2023.
- **Insight**: Don't fine-tune the policy. Instead, build a **library of executable code skills** (composable functions) the agent retrieves at inference.
- **Components**: (1) curriculum, (2) skill library (vector-indexed code), (3) iterative self-verification with execution feedback.
- **Surrogate-1 applicability**: **3/5 (different angle)** — Don't replace fine-tuning. **Augment** Surrogate-1 with a tool/skill library at inference: pre-curated DevSecOps snippets indexed by purpose. Combine with retrieval-augmented inference.

### 7.4 Constitutional AI / RLAIF for Code

- **Paper**: Bai et al. (Anthropic), arXiv:2212.08073 (2022). Updated 2024.
- **For code**:
  - Generate response.
  - Self-critique against principles ("Is this code safe? Does it handle errors? Is it idiomatic?").
  - Self-revise.
  - Train on revised pairs.
- **CodeUltraFeedback** (arXiv:2403.09032) is the canonical code-domain implementation:
  - 10K instructions, 4 responses each from 14 LLMs.
  - GPT-3.5 judges on 5 axes: instruction following, code explanation, complexity/efficiency, readability, coding style.
  - DPO on resulting pairs → CodeLlama-7B-Instruct beats larger models on alignment + HumanEval+.
- **Surrogate-1 applicability**: **5/5** — Cheap to run. Use Claude/GPT to judge your model's outputs along DevSecOps axes (security correctness, AWS best practices, IaC idiom). Build preference pairs without execution.

---

## 8. Code-Specific Preference Data Construction

### 8.1 CodeDPO

- **Paper**: Zhang et al., "CodeDPO: Aligning Code Models with Self Generated and Verified Source Code", arXiv:2410.05605. ACL 2025.
- **Method**:
  1. From real code repos, generate (problem prompt, code, tests) triples.
  2. Cross-validate: every code runs against every test.
  3. **PageRank-style ranking**: tests that distinguish correct/incorrect code get higher weight; code that passes more "high-quality" tests gets higher rank.
  4. Top-rank vs bottom-rank → preference pairs.
- **Hyperparameters**: 10 PageRank iters, damping=0.85, lr=5e-6, 10 epochs DPO, generation temp=1.5.
- **Dataset**: 93K correctness pairs + 21K efficiency pairs = 114K total.
- **Result**: DeepSeek-Coder-6.7B + CodeDPO → HumanEval 83.5%, MBPP 80.7%.
- **Surrogate-1 applicability**: **4/5** — The **PageRank ranking trick** is reusable for any domain where you have "tests" (here: scanner runs).

### 8.2 Focused-DPO

- **Paper**: arXiv:2502.11475 (2025). ACL 2025 Findings.
- **Method**: **Token-level preference optimization** focused on error-prone regions (common prefix/suffix between chosen and rejected). PageRank-based identification of these regions.
- **Why it works**: Standard DPO treats all tokens equally; Focused-DPO concentrates gradient on the actual delta.
- **Surrogate-1 applicability**: **3/5** — Implementation is more complex; gains are marginal vs vanilla DPO on small datasets. Defer.

### 8.3 CodeUltraFeedback

- **Paper**: Weyssow et al., "CodeUltraFeedback", arXiv:2403.09032 (2024). TOSEM 2025.
- **Recipe**:
  - 10K Magicoder Evol-Instruct prompts.
  - 14 LLMs generate 4 responses each.
  - GPT-3.5 judges along 5 preferences (instruction following, explanation, efficiency, readability, style).
  - SFT + DPO on rankings.
- **Reusable**: HF dataset `coseal/CodeUltraFeedback`. Includes preference pairs ready for DPO.
- **Surrogate-1 applicability**: **4/5** — Mix this in (~5K pairs) with your domain DPO data for general code quality. Stops over-fitting to DevSecOps style.

### 8.4 RepoST — Repo-Level Execution Sandbox

- **Paper**: arXiv:2503.07358 (2025).
- **Method**: Construct minimal sandbox per function: isolate target + its deps, generate tests, execute. Scales to repo-level.
- **Surrogate-1 applicability**: **3/5** — Useful if you target multi-file CFN/Terraform projects. Set up sandbox with `cfn-lint`, `tflint`, `prowler`, `tfsec`.

---

## 9. Surrogate-1 v2 Plan

### 9.1 Diagnosis of v1

- **Symptom**: Loss converged 1.331 → 0.7 (47% drop) but quality plateaued.
- **Root causes** (likely):
  1. **Single epoch SFT is undertrained** (most public recipes do 2-5 epochs).
  2. **Likely q,v-only target_modules** (you'd see ~30% improvement going to all-linear).
  3. **No preference signal** — SFT teaches mimicry, not preferences. Plateau is expected.
  4. **2K context** — DevSecOps tasks (CFN templates, Prowler reports) easily exceed 2K.
  5. **No execution grounding** — model can hallucinate plausible-looking AWS commands.

### 9.2 Recommended v2 Stages (Cost-Ordered)

```
Stage A: Fix LoRA hyperparams + 2-3 epochs SFT     [Cheap, 1 day, biggest gain]
Stage B: Bump context 2K → 8K with YaRN config     [Free at config layer]
Stage C: Build 2K preference pairs + DPO            [3 days, moderate]
Stage D: Sandbox + GRPO with exec rewards           [1 week, complex]
Stage E: Self-rewarding iteration                    [Ongoing]
```

### 9.3 Concrete Stage A+B+C Recipe

```yaml
# Axolotl YAML — Surrogate-1 v2 Stage A (SFT) → Stage B (long-ctx) → Stage C (DPO)

# === STAGE A: SFT v2 with corrected hyperparams ===
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
sequence_len: 8192                    # ← was 2048
sample_packing: true
pad_to_sequence_len: true

overrides_of_model_config:
  rope_theta: 1000000                  # ← repo-level RoPE base
  rope_scaling:
    type: yarn
    factor: 2.0                        # 32K serving capability

# Adapter: LoRA + DoRA + all-linear + PiSSA init
adapter: lora
lora_r: 32
lora_alpha: 64
lora_dropout: 0.0
peft_use_dora: true                    # ← DoRA upgrade
lora_target_modules: ["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"]
lora_modules_to_save: []
peft_init_lora_weights: pissa          # ← PiSSA init (faster convergence)

# Training
load_in_4bit: true                     # QLoRA-style, fits Mac M3 24GB
flash_attention: true
gradient_checkpointing: true
micro_batch_size: 1
gradient_accumulation_steps: 8
num_epochs: 3                          # ← was 1
optimizer: paged_adamw_8bit
learning_rate: 2e-4
weight_decay: 0.01
warmup_ratio: 0.03
lr_scheduler: cosine
bf16: true

datasets:
  - path: ./surrogate1_devsecops_v2.jsonl
    type: chat_template

output_dir: ./surrogate1-v2-sft
```

```yaml
# === STAGE C: DPO on preference pairs ===
base_model: ./surrogate1-v2-sft         # ← SFT checkpoint
rl: dpo
sequence_len: 8192
chat_template: chatml

datasets:
  - path: ./surrogate1_dpo_pairs.jsonl
    split: train
    field_chosen: chosen
    field_rejected: rejected
    field_messages: conversation
    message_field_role: role
    message_field_content: content

# DPO-specific
dpo_beta: 0.1
rl_beta: 0.1                            # axolotl alias
loss_type: sigmoid                      # try "ipo" if overfit
precompute_ref_log_probs: true

# Adapter (continue from SFT LoRA OR fresh adapter)
adapter: lora
lora_r: 32
lora_alpha: 64
peft_use_dora: true
lora_target_modules: ["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"]

# Training
sample_packing: false                   # DPO incompatible with packing
load_in_4bit: true
flash_attention: true
gradient_checkpointing: true
micro_batch_size: 1
gradient_accumulation_steps: 16
num_epochs: 1                           # DPO overfits fast
learning_rate: 1e-5                     # ← LoRA-DPO lr (full FT would be 5e-7)
warmup_ratio: 0.03
lr_scheduler: constant                  # safer than cosine for DPO
bf16: true
optimizer: paged_adamw_8bit

output_dir: ./surrogate1-v2-dpo
```

### 9.4 Preference Pair Construction Pipeline

For Surrogate-1, your "tests" are DevSecOps validation:

```python
# build_preference_pairs.py — Surrogate-1 specific
import subprocess, json
from anthropic import Anthropic   # or openai

client = Anthropic()

def generate_candidates(prompt, n=4, model="surrogate1-v2-sft"):
    """Sample N completions at temp 1.0 from the SFT model."""
    return [sample(prompt, temp=1.0) for _ in range(n)]

def grade_devsecops(completion, prompt_meta):
    """Multi-axis grader."""
    score = 0
    # 1. CFN/Terraform validity (rule-based)
    if prompt_meta.get("type") == "cloudformation":
        r = subprocess.run(["cfn-lint", "-"], input=completion, capture_output=True)
        score += 2 if r.returncode == 0 else 0
    if prompt_meta.get("type") == "terraform":
        r = subprocess.run(["tflint", "-"], input=completion, capture_output=True)
        score += 2 if r.returncode == 0 else 0

    # 2. Security check (Prowler / cfn-guard)
    if "iam" in prompt_meta.get("tags", []):
        r = subprocess.run(["cfn-guard","validate","-r","sec-rules.guard","-d","-"],
                          input=completion, capture_output=True)
        score += 1 if r.returncode == 0 else 0

    # 3. LLM-as-judge for explanation quality
    judge = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=500,
        messages=[{"role":"user","content":f"Rate this DevSecOps answer 1-5 on accuracy and AWS best practices:\n\n{completion}"}],
    )
    score += int(extract_score(judge.content[0].text))   # 1-5
    return score

def build_pairs(prompts):
    pairs = []
    for p in prompts:
        cands = generate_candidates(p["prompt"])
        scored = [(c, grade_devsecops(c, p)) for c in cands]
        scored.sort(key=lambda x: x[1], reverse=True)
        if scored[0][1] - scored[-1][1] >= 2:           # margin ≥ 2 to avoid noise
            pairs.append({
                "prompt": p["prompt"],
                "chosen": scored[0][0],
                "rejected": scored[-1][0],
            })
    return pairs

# Run
prompts = json.load(open("surrogate1_devsecops_prompts.json"))
pairs = build_pairs(prompts)
json.dump(pairs, open("surrogate1_dpo_pairs.json","w"))
print(f"Built {len(pairs)} pairs from {len(prompts)} prompts")
```

### 9.5 Expected Gains per Stage (Empirical Estimates)

| Stage | Effort | Expected Quality Lift |
|-------|--------|-----------------------|
| **A1**: 1ep → 3ep SFT | Trivial | +5-10% pass on benchmarks |
| **A2**: q,v → all-linear | Trivial | +10-20% (if currently partial) |
| **A3**: LoRA → DoRA + PiSSA | Trivial | +1-3% |
| **B**: 2K → 8K context | Trivial (config) | Unlocks longer tasks (qualitative) |
| **C**: SFT → SFT+DPO | 3 days, ~2K pairs | +5-15% on preference axes |
| **D**: GRPO with exec rewards | 1 week, sandbox | +10-30% on executable tasks |
| **E**: Self-rewarding iter | Ongoing | +2-5% per iteration |

**Total estimated v2 lift over v1**: **30-60%** on DevSecOps task quality, with most coming from A+C.

### 9.6 What NOT to do (anti-patterns)

- ❌ **Don't bump rank to 64+ without rsLoRA** — gradient instability.
- ❌ **Don't combine DoRA + PiSSA without testing** — some report instability; verify on small run first.
- ❌ **Don't run DPO without SFT first** on same domain (unless you go ORPO).
- ❌ **Don't use LR cosine for DPO** — constant + warmup is safer (Phil Schmid 2025).
- ❌ **Don't use sample_packing with DPO** — incompatible.
- ❌ **Don't extend context beyond 8K without sufficient long-context training data** — model will produce garbage at long ranges.
- ❌ **Don't use a 7B as its own judge for self-rewarding** — judge quality is too noisy. Use Claude/GPT.
- ❌ **Don't skip evaluation** — set up HumanEval+, MBPP+, and a custom DevSecOps eval set BEFORE training v2.

### 9.7 Evaluation Checklist

Before you call v2 done:

- [ ] HumanEval+ pass@1 ≥ baseline + 5%
- [ ] MBPP+ pass@1 ≥ baseline + 5%
- [ ] LiveCodeBench (recent, contamination-free) baseline established
- [ ] Custom DevSecOps eval (50-100 hand-crafted questions): accuracy ≥ 80%
- [ ] Long-context retrieval (passkey at 4K, 8K, 16K): pass
- [ ] Format adherence (structured DevSecOps output): ≥ 95%
- [ ] No regression on Qwen2.5-Coder base capabilities (run a sanity diff)

---

## Sources / References

### Papers (with arXiv IDs)
- DPO: 2305.18290 (Rafailov et al., 2023)
- IPO: 2310.12036 (Azar et al., 2023)
- KTO: 2402.01306 (Ethayarajh et al., 2024)
- ORPO: 2403.07691 (Hong et al., 2024) — EMNLP 2024
- SimPO: 2405.14734 (Meng et al., 2024) — NeurIPS 2024
- DPO-Positive (Smaug): 2402.13228 (2024)
- DoRA: 2402.09353 (Liu et al., 2024) — ICML 2024 Oral
- PiSSA: 2404.02948 (Meng et al., 2024) — NeurIPS 2024 Spotlight
- rsLoRA: 2312.03732 (Kalajdzievski, 2023)
- QLoRA: 2305.14314 (Dettmers et al., 2023)
- YaRN: 2309.00071 (Peng et al., 2023)
- LongRoPE: 2402.13753 (Microsoft, 2024) — ICML 2024
- RLEF: 2410.02089 (Gehring et al., Meta FAIR) — ICML 2025
- GRPO/DeepSeekMath: 2402.03300 (Shao et al., 2024)
- DeepSeek-V3: 2412.19437 (2024)
- DeepSeek-Coder-V2: 2406.11931 (2024)
- DeepSeek-R1: 2501.12948 (2025)
- Qwen2.5-Coder: 2409.12186 (2024)
- Qwen3 Tech Report: 2505.09388 (2025)
- OpenCoder: 2411.04905 (2024) — ACL 2025
- Magicoder/OSS-Instruct: 2312.02120 (Wei et al., 2023) — ICML 2024
- CodeUltraFeedback: 2403.09032 (2024) — TOSEM 2025
- CodeDPO: 2410.05605 (2024) — ACL 2025
- Focused-DPO: 2502.11475 (2025) — ACL 2025 Findings
- Self-Rewarding LMs: 2401.10020 (Yuan et al., 2024)
- ReST: 2308.08998 (Gulcehre et al., DeepMind, 2023)
- Voyager: 2305.16291 (Wang et al., 2023) — NeurIPS 2023
- Constitutional AI: 2212.08073 (Bai et al., Anthropic, 2022)

### Practical Guides (2025-2026)
- Phil Schmid, "How to align open LLMs in 2025 with DPO & synthetic data" — philschmid.de/rl-with-llms-in-2025-dpo
- HuggingFace TRL DPO Trainer docs
- HuggingFace blog: rsLoRA (damjan-k)
- HuggingFace blog: Preference Tuning DPO/IPO/KTO (pref-tuning)
- Unsloth LoRA Hyperparameters Guide
- Axolotl RLHF docs (docs.axolotl.ai/docs/rlhf.html)
- Axolotl GRPO docs (docs.axolotl.ai/docs/grpo.html)
- Kaitchup, "Advanced LoRA Fine-Tuning" — kaitchup.substack.com
- Sebastian Raschka, "Practical Tips for Finetuning LLMs Using LoRA"
- NVIDIA DoRA blog — developer.nvidia.com/blog/introducing-dora
- Medium (Fahey), "DPO Isn't Enough: SimPO, ORPO, KTO and Beyond"

### Code / Tools
- TRL: github.com/huggingface/trl
- Axolotl: github.com/axolotl-ai-cloud/axolotl
- Unsloth: github.com/unslothai/unsloth
- ms-swift (CPT/SFT/DPO/GRPO for 600+ LLMs): github.com/modelscope/ms-swift
- llm-sandbox: github.com/vndee/llm-sandbox
- ai-code-sandbox: github.com/typper-io/ai-code-sandbox
- PiSSA: github.com/GraphPKU/PiSSA
- DoRA (NVlabs): github.com/NVlabs/DoRA
- SimPO: github.com/princeton-nlp/SimPO
- LongRoPE (Microsoft): github.com/microsoft/LongRoPE
- CodeUltraFeedback dataset: huggingface.co/datasets/coseal/CodeUltraFeedback
- Code-Preference-Pairs: huggingface.co/datasets/Vezora/Code-Preference-Pairs

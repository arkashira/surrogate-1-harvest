---
title: Anti-Hallucination & Correctness Training Techniques 2025-2026 (for Surrogate-1 V10)
created: 2026-05-01
updated: 2026-05-01
tags: [hallucination, factuality, training, dpo, grpo, rlvr, prm, calibration, surrogate, code, sre]
status: research-complete
scope: training-side techniques only — NOT runtime guards (see [[anti-hallucination-playbook]] for inference-time)
related:
  - "[[anti-hallucination-playbook]]"
  - "[[coding-llm-frontier]]"
  - "[[devsecops-sre-agentic]]"
  - "[[surrogate-latest-improvements-2026]]"
---

# Anti-Hallucination & Correctness Training Techniques (2025-2026)

**Purpose**: Pick training-side methods to bake INTO Surrogate-1 weights (V10+). The existing [[anti-hallucination-playbook]] covers runtime guards (citation-or-refuse, MiniCheck, CoVe prompt-chain). This file is the complement: techniques that **change weights** so the served model is more truthful before any runtime filter runs.

**Stack to extend**: Qwen2.5-Coder 7B/14B/32B base + LoRA r=64 + DoRA + PiSSA/CorDA init + Spectrum-lite + LoRA+ + NEFTune (already V8). Trainer = TRL `SFTTrainer` + scaffolded `GRPOTrainer` on Kaggle T4×2 / Civo H200. See `~/.surrogate/hf-space/bin/kaggle-trainer.sh`.

**Cutoff**: 2024-2026 sources only. Today = 2026-05-01.

---

## Section 1 — RL methods optimized for truthfulness (highest impact, V10 priority)

### 1.1 TruthRL (GRPO + ternary reward)
- **Paper**: [TruthRL: Incentivizing Truthful LLMs via RL](https://arxiv.org/abs/2509.25760) (Sep 2025)
- **Mechanism**: GRPO with ternary reward — `+1 correct`, `0 abstain`, `-1 hallucinate`. Distinguishes guessing (penalized) from refusal (neutral) from correct (rewarded).
- **Measured uplift**: vs vanilla RL → **−28.9% hallucination rate**, **+21.1% truthfulness** on 4 knowledge-intensive benchmarks (Qwen + Llama backbones, retrieval and non-retrieval).
- **Cost**: GRPO loop with verifiable reward. No new model heads. Needs (prompt, ground-truth answer) pairs + abstention labeling rubric.
- **Surrogate fit**: ★★★★★ — directly attacks the problem, GRPO already scaffolded in trainer.
- **Wire**: replace placeholder `reward_unit_test_pass` with ternary `reward_truth(prompt, completion, gt_answer)` returning `{1.0, 0.0, -1.0}`.

### 1.2 KnowRL (atomic-fact verification reward)
- **Paper**: [KnowRL: Knowledgeable RL for Factuality](https://arxiv.org/abs/2506.19807) (Jun 2025)
- **Mechanism**: Decompose CoT into atomic facts → verify each against external KB (the `KnowRL-Knowledge-Base` HF dataset) → step-level reward instead of outcome-only.
- **Measured uplift**: Incorrect Rate on SimpleQA −20.3% on slow-thinking models; GPQA 37.4 → 42.4; AIME 2025 stable. Teaches the model to say "I don't know."
- **Cost**: process-level reward (more expensive than outcome). Needs an atomic-fact extractor (small LM) + KB.
- **Surrogate fit**: ★★★★★ — Surrogate's FalkorDB graph is ready-made KB; we already have entity-typed knowledge.
- **Dataset**: [`zjunlp/KnowRL-Knowledge-Base`](https://huggingface.co/datasets/zjunlp/KnowRL-Knowledge-Base).

### 1.3 FSPO — Factuality-aware Step-wise Policy Optimization
- **Paper**: [Reasoning Models Hallucinate More](https://arxiv.org/abs/2505.24630) (May 2025)
- **Key insight**: pure GRPO with binary outcome reward causes high-variance gradients + entropy explosion → reasoning models hallucinate MORE than their non-reasoning base. FSPO injects step-level factuality verification into the advantage calc.
- **Measured uplift**: significantly higher factuality vs vanilla GRPO with no loss in reasoning depth, on small data.
- **Cost**: per-step verifier (NLI-style). Higher per-rollout cost but fewer rollouts needed.
- **Surrogate fit**: ★★★★ — directly relevant if we ever GRPO on long CoT; pairs naturally with KnowRL's atomic-fact verifier.

### 1.4 Binary Retrieval-Augmented Reward (Binary RAR)
- **Paper**: [Train for Truth, Keep the Skills](https://arxiv.org/abs/2510.17733) (Oct 2025)
- **Mechanism**: KL-constrained RL with reward = `1 if (entire output factually correct against retrieved evidence) else 0`. No partial credit.
- **Measured uplift**: long-form hallucination 76.2 → **45.8** (vs DPO 66.9 and continuous-VeriScore-RL 51.7). PopQA −44.4% wrong; GPQA −21.7%. AlpacaEval drops only −1.4% (vs continuous-reward baselines −22.8%). Calibrated abstention emerges naturally.
- **Cost**: needs retrieval at training time (slow but doable). One forward to score, one to verify.
- **Surrogate fit**: ★★★★★ — **stacks cleanly with TruthRL**; binary signal is more stable than continuous on small corpora.

### 1.5 RLCR — Reinforcement Learning with Calibration Rewards
- **Paper**: [Beyond Binary Rewards: Training LMs to Reason About Their Uncertainty](https://arxiv.org/abs/2507.16806) (Jul 2025) · [GitHub `damanimehul/RLCR`](https://github.com/damanimehul/RLCR)
- **Mechanism**: model emits `(answer, confidence%)` after reasoning. Reward = correctness + Brier-score on confidence. Trains calibration jointly with accuracy.
- **Measured uplift**: substantial calibration improvement with **zero accuracy loss**, in- and out-of-domain. Confidence-weighted test-time scaling becomes free upgrade.
- **Cost**: format change in output (must produce confidence). Same RL loop, just new reward term.
- **Surrogate fit**: ★★★★★ — perfect for SRE/code where "I'm 90% sure" matters more than "definitely yes." Stacks with TruthRL.

### 1.6 Behaviorally Calibrated RL (Abstain-R1 family)
- **Paper**: [Mitigating LLM Hallucination via Behaviorally Calibrated RL](https://arxiv.org/abs/2512.19920) (Dec 2025) · related: [Abstain-R1](https://arxiv.org/abs/2604.17073)
- **Mechanism**: stochastic abstention penalty calibrated to model's own confidence histogram. Qwen3-4B beats GPT-5 on accuracy-to-hallucination ratio (BeyondAIME 0.806 vs 0.207).
- **Cost**: needs confidence estimate + reward shaping; verifiable RL backbone.
- **Surrogate fit**: ★★★★ — small-model success story directly applicable to our 7B/14B targets.

---

## Section 2 — DPO variants for factuality (cheap, no rollouts)

### 2.1 FLAME (Factuality-Aware Alignment, NeurIPS 2024)
- **Paper**: [FLAME](https://arxiv.org/abs/2405.01525) (Lin et al., Waterloo + CMU + Meta)
- **Mechanism**: factuality-aware SFT (filter SFT data so model isn't trained on facts it doesn't know — main hallucination cause) + factuality-aware DPO (FActScore as preference signal).
- **Measured uplift**: AlpacaFact FActScore 43.9 (SFT+DPO) → **48.7** (FLAME); FAVA 55.0 → 58.9. Win rate maintained at 51.2% on Alpaca Eval.
- **Cost**: needs FActScore atomic-fact eval (uses Wikipedia retrieval). One-time DPO data annotation.
- **Surrogate fit**: ★★★★ — directly applicable; we generate DPO pairs already.

### 2.2 Mask-DPO (sentence-level masking)
- **Paper**: [Mask-DPO: Generalizable Fine-grained Factuality Alignment](https://arxiv.org/abs/2503.02846) (Mar 2025, ICLR 2025)
- **Mechanism**: instead of treating whole responses as preferred/rejected, mask **only factually-correct sentences** in chosen + **only factually-wrong sentences** in rejected. Avoids penalizing correct content in rejected responses.
- **Measured uplift**: Llama-3.1-8B-Instruct on ANAH 49.2% → **77.5%** (surpasses Llama-3.1-70B at 53.4%). Out-of-domain Biography FActScore 30.3 → 39.4.
- **Cost**: needs sentence-level fact annotator (one-time, can use a 7B verifier). DPO loss change is ~10 lines.
- **Surrogate fit**: ★★★★★ — **biggest measured gain in our research**. Generalizes across domains (key for code+SRE+security mix).

### 2.3 F-DPO (binary-label factuality DPO)
- **Paper**: [Reducing Hallucinations via Factuality-Aware Preference Learning](https://arxiv.org/abs/2601.03027)
- **Mechanism**: label-flipping when chosen ≤ rejected on factuality + factuality-margin term. Reduces to vanilla DPO when both equal.
- **Measured uplift**: Qwen3-8B hallucination rate 0.424 → **0.084 (−5×)**, factuality 5.26 → 7.90 (+50%). Qwen2.5-14B TruthfulQA MC1 +17%, MC2 +49%. No reward model, no token-level annotation, single stage.
- **Cost**: lowest of any factuality method — only need binary labels.
- **Surrogate fit**: ★★★★★ — **easiest to ship in V10**. Fits our 140 → 500 pair pipeline. Drop-in replacement for `DPOTrainer` loss.

### 2.4 VeriCoT-DPO (logic-verification preference pairs)
- **Paper**: [VeriCoT: Neuro-symbolic CoT Validation](https://arxiv.org/abs/2511.04662) (Nov 2025)
- **Mechanism**: extract first-order logic from CoT → verify with solver → pairs where verified-correct CoT preferred to verified-failed.
- **Measured uplift**: SFT+DPO on VeriCoT pairs → +4.3% verification pass rate (+18.4% relative), +3.4% verified-correct answer (+17.7% relative). Tested on legal/biomedical.
- **Cost**: needs FOL extractor + Z3-style solver. Heavier than F-DPO, lighter than KnowRL.
- **Surrogate fit**: ★★★ — high value for IaC compliance reasoning (CIS controls map to logic rules nicely); medium for general code.

---

## Section 3 — Process Reward Models (PRM) for code/reasoning

### 3.1 Math-Shepherd (automatic step-level annotation)
- **Paper**: [Math-Shepherd](https://arxiv.org/pdf/2312.08935) (ACL 2024)
- **Mechanism**: automatic step-correctness labels via MCTS rollouts (no humans). Train PRM, then step-level PPO/DPO.
- **Measured uplift**: Mistral-7B GSM8K **77.9% → 84.1%**; MATH 28.6% → 33.0%. Outperforms human-annotated PRM800K with 4× more data.
- **Cost**: rollout-heavy (MCTS at training time). Once PRM is trained it's a 7B model that scores steps.
- **Surrogate fit**: ★★ for math; ★★★★ if we adapt the recipe for **code reasoning** (use unit-test pass-rate as auto-label proxy).

### 3.2 PRM800K (OpenAI's human-annotated PRM dataset)
- **Resource**: [`openai/prm800k` GitHub](https://github.com/openai/prm800k) — 800K step labels on GPT-4 MATH solutions, MIT-licensed.
- **Use**: foundation training set for any PRM. Math-only.
- **Surrogate fit**: ★★ — math-only, but the dataset structure is the gold standard if we annotate our own DevSecOps step traces in same shape.

### 3.3 ThinkPRM (long-CoT verifier, 1% labels)
- **Paper**: [Process Reward Models That Think](https://arxiv.org/pdf/2504.16828) (Apr 2025)
- **Mechanism**: train a verifier that thinks before judging. Beats discriminative PRMs using only **1% of PRM800K labels**.
- **Cost**: just SFT a verifier on long CoT-style judgments. Dramatically cheaper than Math-Shepherd.
- **Surrogate fit**: ★★★★ — small-data PRM is exactly what we need. Pair with Mask-DPO.

### 3.4 rStar-Math (MCTS + PPM + 4-round self-evolution)
- **Paper**: [rStar-Math](https://arxiv.org/abs/2501.04519) (Microsoft, Jan 2025) · [GitHub `microsoft/rStar`](https://github.com/microsoft/rStar)
- **Mechanism**: MCTS rollouts + Process Preference Model (PPM, not absolute PRM) + 4 rounds of policy-PPM co-evolution. From-scratch training of both.
- **Measured uplift**: Qwen2.5-Math-7B MATH **58.8% → 90.0%** (surpasses o1-preview by +4.5pp). Phi3-mini-3.8B 41.4% → 86.4%. AIME 53.3% (top 20% of US students).
- **Cost**: very heavy — millions of rollouts on 747K problems. Realistic only at ~100 GPU-days.
- **Surrogate fit**: ★★ for now (too expensive on Kaggle T4×2). Reserve for Civo H200 V11+ if budget allows. The **PPM-vs-PRM lesson** still applies cheaply: use preferences not absolute scores.

### 3.5 R-Zero (Challenger-Solver self-evolution)
- **Paper**: [R-Zero](https://arxiv.org/pdf/2508.05004) (Aug 2025)
- **Mechanism**: Challenger-LM generates hard questions → Solver-LM trained with GRPO on filtered ones. No human seed.
- **Measured uplift**: Qwen3-4B-Base +6.49 average on math benchmarks after 3 self-evolve rounds.
- **Surrogate fit**: ★★★ — promising but still math-flavored; possible adaptation to "Challenger writes hard SRE incidents" vs "Solver runs through them."

### 3.6 SWE-RM (execution-free code reward model)
- **Paper**: [SWE-RM: Execution-Free Feedback for SWE Agents](https://arxiv.org/abs/2512.21919) (Dec 2025)
- **Mechanism**: 30B-MoE reward model (3B active) scores patches without running tests. Used for both test-time-scaling AND RL training feedback.
- **Measured uplift**: Qwen3-Coder-Flash **51.6% → 62.0%** on SWE-bench Verified; Qwen3-Coder-Max 67.0% → 74.6%. RL with SWE-RM gives **+3pp pass@1 over execution-based** while being faster + more stable.
- **Cost**: need to run a 30B reward model alongside policy. Heavy but doable on Civo H200.
- **Surrogate fit**: ★★★★★ for code-specialist Surrogate. Pairs with our existing GRPO scaffold.

### 3.7 SWE-Gym (executable training environment)
- **Resource**: [GitHub `SWE-Gym/SWE-Gym`](https://github.com/SWE-Gym/SWE-Gym) (ICML 2025)
- **Use**: 2.4K real Python tasks with executable Docker env + tests. Foundation for any code-RL training.
- **Surrogate fit**: ★★★★ — drop-in env for the GRPO scaffold's reward function (replace dummy `reward_unit_test_pass` with SWE-Gym test runner).

---

## Section 4 — Self-improvement loops

### 4.1 V-STaR (joint generator-verifier DPO)
- **Paper**: [V-STaR](https://arxiv.org/abs/2402.06457) (COLM 2024)
- **Mechanism**: STaR generates correct + incorrect solutions → verifier trained via DPO on them → verifier filters Best-of-N at test. Iterate.
- **Measured uplift**: **+4 to +17%** test accuracy on code+math benchmarks (LLaMA2 family) over plain self-improvement.
- **Cost**: iterative — multiple generator+verifier rounds. ~3× plain DPO cost.
- **Surrogate fit**: ★★★★ — the verifier doubles as runtime filter. Stacks with V-STaR-SQL idea for code/SQL.

### 4.2 Self-Rewarding LM + Meta-Rewarding (Meta, 2024-2025)
- **Papers**: [Self-Rewarding LM](https://arxiv.org/abs/2401.10020); [Meta-Rewarding](https://arxiv.org/abs/2407.19594) (EMNLP 2025)
- **Mechanism**: model judges its own outputs → DPO on self-rankings. Meta-Rewarding adds a meta-judge step that judges the judgments — fixes saturation.
- **Measured uplift**: Llama-3-8B-Instruct AlpacaEval-2 22.9% → **39.4%**; ArenaHard 20.6% → 29.1%.
- **Cost**: extra forward passes per training prompt for self-judging. No human labels.
- **Surrogate fit**: ★★★ — useful when human labels exhausted. Beware: bootstrap bias on factuality (a wrong model judging itself stays wrong); works best for style/format not for factuality.

### 4.3 Iterative DPO + rejection sampling (Llama-3-style)
- **Papers**: Llama-3 post-training; [Iterative DPO Empirical Investigation](https://arxiv.org/abs/2503.12854); [VerIPO](https://arxiv.org/abs/2505.19000)
- **Mechanism**: SFT → sample N → PRM/verifier ranks → top/bottom become DPO pairs → iterate 3-5 rounds.
- **Measured uplift**: Llama-3 attributes large reasoning+coding gains to this loop. Empirically robust across backbones.
- **Cost**: rejection sampling each round. Verifier needed.
- **Surrogate fit**: ★★★★★ — already partially scaffolded. Add verifier (Section 3) and iterate.

### 4.4 Quiet-STaR (silent rationale pretraining)
- **Paper**: [Quiet-STaR](https://arxiv.org/abs/2403.09629) (Zelikman et al. 2024); [Fast Quiet-STaR](https://aclanthology.org/2025.findings-emnlp.1020.pdf) (2025)
- **Mechanism**: continued pretraining where the model generates internal "thoughts" before each token; learnable thought-start/end tokens; teacher-forcing extension.
- **Measured uplift**: zero-shot GSM8K **5.9% → 10.9%**; CommonsenseQA 36.3% → 47.2%. No fine-tuning on tasks.
- **Cost**: continued pretraining cost (full forward+backward, no LoRA). Heavy.
- **Surrogate fit**: ★★ — too expensive on T4×2. Reserve as future Civo H200 experiment. Fast Quiet-STaR cheaper but still pretraining-flavored.

### 4.5 Constitutional AI v2 + character training (Anthropic, 2024-2026)
- **Sources**: [Anthropic CAI](https://www.anthropic.com/research/constitutional-ai-harmlessness-from-ai-feedback); [Claude's new constitution](https://www.anthropic.com/news/claude-new-constitution); [Synthetic Data & CAI (Lambert)](https://rlhfbook.com/c/13-cai)
- **Mechanism**: model critiques + revises its own responses against a written constitution → SL on revisions + RLAIF on AI-rated preferences. v2 (used in Claude 3/4) integrates with character training and switches from list-of-rules to **principle-with-explanation**.
- **Cost**: needs a constitution + critique-rewrite loop. Generating synthetic data is cheap; RLAIF stage costs an RL run.
- **Surrogate fit**: ★★★★ — write a Surrogate-1 constitution (DevSecOps ethics: never recommend exploits, always cite, refuse-when-unsure, stay-on-topic) → critique + revise existing 242 Hermes pairs. Cheap factuality+style win.

---

## Section 5 — Decoding-time methods we should NOT bake into training

These exist but are inference-time. Listed for completeness — they'd be **runtime additions to vLLM/Ollama**, not training changes:

| Method | Effect | Training-time? | Verdict |
|--------|--------|----------------|---------|
| **DoLa** (early-vs-late layer contrast) | TruthfulQA +12-17pp on LLaMA family | ❌ inference-only | Modest gains; "limited improvements on long-context tasks" — skip for code/SRE |
| **ITI** (activation steering toward truthful direction) | Alpaca TruthfulQA 32.5% → 65.1% | Hybrid (probe-trained on small set, applied at inference) | Worth a runtime experiment, NOT training |
| **Adaptive Activation Steering (ACT)** | LLaMA truthful +142%, LLaMA3 +34% | Inference-only, ~few dozen calibration samples | Runtime add-on |
| **Active Layer-Contrastive Decoding (ActLCD)** | Beats static DoLa via RL-when-to-contrast | Inference-only (RL trains the trigger) | Worth a future runtime experiment |
| **Self-Consistency / Majority Voting** | Test-time only; +5-15pp on reasoning | Inference-only | Stack at runtime, not training |
| **Best-of-N + verifier rerank** | Big (uses our PRM) | **YES via training-data generation** | Use to MINE training pairs (Section 4.3) |

**Key**: only Best-of-N+verifier ports into training (as a data-generation strategy, not a loss change). Everything else is runtime.

---

## Section 6 — Knowledge editing (precision surgery, NOT bulk training)

| Method | Edits | Capability degradation | Use case for Surrogate |
|--------|-------|-----------------------|------------------------|
| **ROME** (rank-one FFN edit) | <10 facts before degradation | High after few edits | Single-fact corrections only |
| **MEMIT** (multi-layer progressive) | Stable to ~40 edits | Moderate after 40 | Periodic CVE/version-string updates |
| **GRACE** (codebook adapter, no weight change) | Unbounded | None on base | Best for "live" facts (CVEs, prices, version pins) |
| **MAKE** (memory-associated, 2025) | Bulk edits with associated transfer | Low | Newest; promising for KB sync |
| **AdaEdit** (continuous, ACL 2025) | Continuous lifelong | Low | Future direction |

**Surrogate verdict**: do **NOT** use ROME/MEMIT for general knowledge — they trade base capability. Use **GRACE-style adapter** for fast-moving facts (CVE database, library versions) and rely on RAG for everything else. Source: [EasyEdit](https://github.com/zjunlp/EasyEdit), [MAKE](https://direct.mit.edu/tacl/article/doi/10.1162/TACL.a.26/132652/MAKE-Memory-Associated-Knowledge-Editing).

---

## Section 7 — Datasets to add to Surrogate's training mix

Ranked by factuality-uplift-per-token. All sourced from HF Hub.

| Dataset | Size | License | Use | HF link |
|---------|------|---------|-----|---------|
| **TruthfulQA** | 817 Q | Apache-2.0 | TruthfulQA-MC1/MC2 fine-tuning + eval | [`truthfulqa/truthful_qa`](https://huggingface.co/datasets/truthfulqa/truthful_qa) |
| **HaluEval** | ~35K | MIT | Hallucination QA + dialogue + summarization labels — direct training signal | [`pminervini/HaluEval`](https://huggingface.co/datasets/pminervini/HaluEval) |
| **PopQA** | 14K | MIT | Long-tail entity QA, hallucination-prone | [`akariasai/PopQA`](https://huggingface.co/datasets/akariasai/PopQA) |
| **SimpleQA** (OpenAI Oct 2024) | 4,326 | MIT | Short-form factual; single verifiable answer; held-out eval signal | [openai/simple-evals](https://github.com/openai/simple-evals) |
| **HalluLens** | dynamic gen | mixed (MIT base, FActScore portions MIT) | Distinguishes intrinsic vs extrinsic hallucination — train on the harder ones | [`facebookresearch/HalluLens`](https://github.com/facebookresearch/HalluLens) |
| **PRM800K** | 800K step labels | MIT | Math step-level reasoning supervision | [`openai/prm800k`](https://github.com/openai/prm800k) |
| **KnowRL-Knowledge-Base** | KB | research | Atomic-fact verification for KnowRL training | [`zjunlp/KnowRL-Knowledge-Base`](https://huggingface.co/datasets/zjunlp/KnowRL-Knowledge-Base) |
| **SWE-Gym** | 2.4K tasks + tests | research-only | Code RL environment — wire into GRPO `reward_funcs` | [`SWE-Gym/SWE-Gym`](https://github.com/SWE-Gym/SWE-Gym) |
| **FActScore biographies** | varies | MIT | Long-form generation factuality eval | [shmuhammad/factscore](https://huggingface.co/datasets/shmuhammad/factscore) (or paper repo) |
| **ANAH train** | sentence-level facts | research | Mask-DPO source data | per [Mask-DPO](https://arxiv.org/abs/2503.02846) |

**Top 3 priority for Surrogate factuality mix**:
1. **HaluEval** — direct hallucination label signal at scale; mix into SFT.
2. **TruthfulQA + PopQA** — eval guardrails; keep as held-out gold but sample 20% for training (like Mask-DPO showed: training on simple precise QA generalizes broadly).
3. **SWE-Gym** — code-execution truthfulness reward (replaces Surrogate trainer's dummy reward fn).

---

## Section 8 — Stack analysis: what combos actually work

Based on cross-paper signal:

### ✅ Confirmed-stacks-well (additive gains)
- **TruthRL ternary reward + Binary RAR retrieval-grounded reward** — both use same GRPO, complementary signals (truth vs evidence). Use Binary RAR when retrieval available, TruthRL ternary when not.
- **Mask-DPO + KnowRL** — Mask-DPO for SFT-stage factuality alignment; KnowRL for downstream RL. Different stages, no conflict.
- **F-DPO + RLCR** — F-DPO is one-shot DPO, RLCR is on-policy RL with calibration. F-DPO first to clean preference data, then RLCR for calibration polish.
- **Quiet-STaR (continued pretraining) + V-STaR (post-SFT verifier)** — different layers of the stack; if compute allows, both help.
- **CAI critique-revise + FLAME factuality-aware SFT** — CAI cleans style+ethics, FLAME cleans factuality. Run CAI first to fix obvious problems, then FLAME for factual subtlety.
- **ThinkPRM (verifier-as-judge) + Iterative DPO** — verifier mines preference pairs cheaply for next DPO round. Standard Llama-3 recipe.

### ⚠ Cancel each other / known interference
- **Vanilla GRPO outcome-only + reasoning model** — INCREASES hallucination per [Reasoning Models Hallucinate More](https://arxiv.org/abs/2505.24630). Replace outcome-only with FSPO or KnowRL.
- **Standard DPO + factuality** — DPO on length-biased preferences → longer hallucinated outputs (per FLAME). Always use F-DPO or Mask-DPO if factuality is a goal.
- **Self-Rewarding LM on factuality** — model bias propagates; saturates fast. Per Meta-Rewarding paper, you need meta-judge OR external verifier — pure self-judging on factual claims is weak.
- **DoLa + heavy RL fine-tuning** — DoLa benefit reportedly diminishes after task-specialized RL (the "factual" layer signal moves). Pick one or the other for any given path.
- **ROME/MEMIT bulk edits + general capability** — locate-and-edit methods degrade base after 10+ edits (per [EasyEdit benchmarks](https://github.com/zjunlp/EasyEdit)). Use only for a handful of high-priority corrections.
- **NEFTune α=5 + very low LR factuality DPO** — NEFTune adds embedding noise that helps SFT but mildly degrades calibration. Drop NEFTune in the DPO/RL phase (keep only in SFT phase).

### Stack Recipe Recommended for Surrogate V10
```
Phase 0: SFT (existing — Kaggle T4×2)
   + Mask-DPO sentence-level factuality on top
   + drop NEFTune for the DPO sub-phase

Phase 1: F-DPO factuality round 1 (one-shot, easy ship)
   data = 500 (chosen, rejected, factuality-binary) pairs

Phase 2: GRPO with TruthRL ternary reward (replace dummy reward_unit_test_pass)
   reward = +1 correct, 0 abstain, -1 hallucinate
   group_size = 4, KL beta = 0.04

Phase 3: GRPO with Binary RAR (RAG-grounded)
   reward = 1 if all claims supported by retrieved evidence else 0
   uses existing RAG (113K SQLite + ChromaDB + FalkorDB)

Phase 4: RLCR calibration polish
   reward = correctness + Brier(confidence)
   model output format = `<answer>X</answer><conf>0.85</conf>`

For code (Civo H200 budget allowing):
   Phase 2-alt: GRPO with SWE-RM or SWE-Gym execution feedback
                + KnowRL atomic-fact reward for any docstring/comment claims
```

---

## Wire-Into-V10 Trainer

Concrete additions to `~/.surrogate/hf-space/bin/kaggle-trainer.sh` (and matching civo-trainer.sh):

### A. New env knobs (add to Kaggle Secrets bootstrap list, line ~139)
```python
"FACTUALITY_DPO",          # "off" | "f_dpo" | "mask_dpo"
"GRPO_REWARD",             # "unit_test" | "truthrl_ternary" | "binary_rar" | "rlcr"
"GRPO_REWARD_BETA",        # KL coefficient (default 0.04)
"CONFIDENCE_FORMAT",       # "off" | "tagged" — RLCR <conf>0.85</conf>
"TRUTH_VERIFIER_MODEL",    # HF id of NLI/PRM verifier (default "lytang/MiniCheck-Flan-T5-Large")
"FACT_KB_DATASET",         # default "zjunlp/KnowRL-Knowledge-Base"
"ABSTENTION_TOKEN",        # default "I_DONT_KNOW" — special abstention emit
"HALLUEVAL_TAKE",          # int — pairs to mix from HaluEval (default 5000)
"TRUTHFULQA_TAKE",         # int — held-back-from-eval mixing (default 200)
"POPQA_TAKE",              # int — long-tail QA (default 3000)
"SWE_GYM_TAKE",            # int — code RL tasks (default 1000)
```

### B. New dataset merges (extend `merge_external` block ~line 410)
```python
merge_external("pminervini/HaluEval",            int(os.environ.get("HALLUEVAL_TAKE", "5000")), 1.0, "HaluEval")
merge_external("akariasai/PopQA",                int(os.environ.get("POPQA_TAKE",     "3000")), 1.0, "PopQA-longtail")
merge_external("truthfulqa/truthful_qa",         int(os.environ.get("TRUTHFULQA_TAKE","200")),  1.0, "TruthfulQA-train")
# SWE-Gym is loaded separately as a GRPO env, not as SFT pairs
```

### C. F-DPO loss patch (post-SFT phase, scaffolded behind FACTUALITY_DPO knob)
Add a new block after `trainer.train()`:
```python
if os.environ.get("FACTUALITY_DPO", "off") == "f_dpo":
    from trl import DPOTrainer, DPOConfig
    # Build (prompt, chosen, rejected, fact_label_chosen, fact_label_rejected)
    # F-DPO loss: standard DPO + factuality margin term, label-flip if rejected more factual
    # See arxiv 2601.03027 §3 for exact loss form (~30 lines).
    ...
```

### D. Replace `reward_unit_test_pass` with multi-mode reward (GRPO block ~line 735)
```python
def reward_truthrl_ternary(prompts, completions, gt_answers, **kw):
    rewards = []
    for c, gt in zip(completions, gt_answers):
        if "I_DONT_KNOW" in c.upper(): rewards.append(0.0)              # abstain
        elif _matches(c, gt):           rewards.append(1.0)              # correct
        else:                            rewards.append(-1.0)             # hallucinate
    return rewards

def reward_binary_rar(prompts, completions, retrieved_evidence, **kw):
    """All claims must be supported by retrieved passages (NLI verifier)."""
    rewards = []
    for c, evid in zip(completions, retrieved_evidence):
        claims = _extract_atomic_claims(c)
        rewards.append(1.0 if all(_nli_supports(claim, evid) for claim in claims) else 0.0)
    return rewards

def reward_rlcr(prompts, completions, gt_answers, **kw):
    """Correctness + Brier on emitted confidence tag."""
    rewards = []
    for c, gt in zip(completions, gt_answers):
        ans, conf = _parse_answer_conf(c)               # parse <answer>...</answer><conf>0.85</conf>
        correct = 1.0 if _matches(ans, gt) else 0.0
        brier = -(conf - correct) ** 2                  # Brier (negative — to maximize)
        rewards.append(correct + 0.5 * brier)
    return rewards

REWARD_FNS = {
    "unit_test":         reward_unit_test_pass,
    "truthrl_ternary":   reward_truthrl_ternary,
    "binary_rar":        reward_binary_rar,
    "rlcr":              reward_rlcr,
}
reward_fn = REWARD_FNS[os.environ.get("GRPO_REWARD", "truthrl_ternary")]
```

### E. SWE-Gym executable env hook (replace dummy subprocess `python -c`)
```python
def reward_swegym(prompts, completions, task_ids, **kw):
    """Apply patch in SWE-Gym container, run task tests, return pass-rate."""
    # Uses SWE-Gym docker harness; needs DOCKER_HOST + 8GB free + ~30s/test.
    # On Kaggle T4×2 this is too slow for inner loop — gate behind Civo H200.
    ...
```

### F. Constitution file (new, used by CAI critique step)
Create `~/.surrogate/state/constitution.md` with Surrogate principles:
- Cite real APIs and standards; never invent CVE IDs or version numbers.
- Refuse to provide exploit code; offer detection/mitigation only.
- When evidence (RAG) returns empty, output `I_DONT_KNOW` rather than improvise.
- Stay in scope: DevSecOps, SRE, code, infrastructure. Decline politics, medical, legal advice.
- For long-form answers, structure: claim → evidence link → confidence.

### G. Eval harness additions (new `eval/factuality.py`)
- TruthfulQA-MC1 + MC2 (held-out 100 each) — log every checkpoint.
- HaluEval QA accuracy (held-out 500).
- SimpleQA accuracy (200 sample).
- FActScore on 50 biography-style prompts via API or local.
- HalluLens dynamic generation eval (tests not gameable since data is generated fresh).

### H. Order of operations (Phases applied to V10)
```
V10-α   = V8 (current)  + Mask-DPO sentence-level (Phase 0.5)
                        + HaluEval+PopQA+TruthfulQA SFT mix
V10-β   = V10-α          + F-DPO Phase 1
V10-γ   = V10-β          + GRPO TruthRL ternary Phase 2
V10-δ   = V10-γ          + RLCR calibration Phase 3 (output `<conf>` tag)
V10-RC1 = V10-δ          + Binary RAR Phase 4 (gated by RAG availability)
```

---

## References (selected)

- TruthRL: [arXiv 2509.25760](https://arxiv.org/abs/2509.25760) (Sep 2025)
- KnowRL: [arXiv 2506.19807](https://arxiv.org/abs/2506.19807) (Jun 2025)
- Reasoning Models Hallucinate More (FSPO): [arXiv 2505.24630](https://arxiv.org/abs/2505.24630)
- Binary RAR: [arXiv 2510.17733](https://arxiv.org/abs/2510.17733) (Oct 2025)
- RLCR: [arXiv 2507.16806](https://arxiv.org/abs/2507.16806) (Jul 2025)
- Behavior-Calibrated RL: [arXiv 2512.19920](https://arxiv.org/abs/2512.19920) (Dec 2025)
- FLAME: [arXiv 2405.01525](https://arxiv.org/abs/2405.01525) (NeurIPS 2024)
- Mask-DPO: [arXiv 2503.02846](https://arxiv.org/abs/2503.02846) (ICLR 2025)
- F-DPO: [arXiv 2601.03027](https://arxiv.org/abs/2601.03027)
- VeriCoT: [arXiv 2511.04662](https://arxiv.org/abs/2511.04662) (Nov 2025)
- Math-Shepherd: [arXiv 2312.08935](https://arxiv.org/pdf/2312.08935) (ACL 2024)
- ThinkPRM: [arXiv 2504.16828](https://arxiv.org/pdf/2504.16828) (Apr 2025)
- rStar-Math: [arXiv 2501.04519](https://arxiv.org/abs/2501.04519) (Jan 2025)
- R-Zero: [arXiv 2508.05004](https://arxiv.org/pdf/2508.05004) (Aug 2025)
- SWE-RM: [arXiv 2512.21919](https://arxiv.org/abs/2512.21919) (Dec 2025)
- SWE-Gym: [GitHub](https://github.com/SWE-Gym/SWE-Gym) (ICML 2025)
- V-STaR: [arXiv 2402.06457](https://arxiv.org/abs/2402.06457) (COLM 2024)
- Self-Rewarding LM: [arXiv 2401.10020](https://arxiv.org/abs/2401.10020)
- Meta-Rewarding: [arXiv 2407.19594](https://arxiv.org/abs/2407.19594) (EMNLP 2025)
- Quiet-STaR: [arXiv 2403.09629](https://arxiv.org/abs/2403.09629); Fast-Quiet-STaR: [ACL Findings 2025](https://aclanthology.org/2025.findings-emnlp.1020.pdf)
- DoLa: [arXiv 2309.03883](https://arxiv.org/abs/2309.03883); Active LCD: [arXiv 2505.23657](https://arxiv.org/pdf/2505.23657)
- ITI: [arXiv 2306.03341](https://arxiv.org/abs/2306.03341); Adaptive AS (ACT): [arXiv 2406.00034](https://arxiv.org/abs/2406.00034)
- CAI / RLAIF: [Anthropic CAI paper](https://arxiv.org/abs/2212.08073); [Claude's new constitution](https://www.anthropic.com/news/claude-new-constitution); [RLHF Book ch 13](https://rlhfbook.com/c/13-cai)
- HalluLens: [arXiv 2504.17550](https://arxiv.org/abs/2504.17550)
- HaluEval: [GitHub](https://github.com/RUCAIBox/HaluEval); TruthfulQA: [GitHub](https://github.com/sylinrl/TruthfulQA)
- Knowledge Editing: [EasyEdit](https://github.com/zjunlp/EasyEdit); MAKE: [TACL 2025](https://direct.mit.edu/tacl/article/doi/10.1162/TACL.a.26/132652/MAKE-Memory-Associated-Knowledge-Editing); AdaEdit: [ACL 2025](https://aclanthology.org/2025.acl-long.208.pdf)
- SimPO: [NeurIPS 2024](https://arxiv.org/pdf/2405.14734); KTO/IPO via [DPO Survey](https://arxiv.org/html/2410.15595v3)

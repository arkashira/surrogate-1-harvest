---
title: Surrogate-1 v2 Ambitious Targets — Beyond Conservative via Free Techniques
date: 2026-04-29
tags: [surrogate-1, v2, targets, ambitious, free-techniques]
status: ready
---

# v2 Ambitious Targets (push beyond conservative via TECHNIQUE not money)

## Updated target table

| Domain | Conservative (initial) | **AMBITIOUS (technique-driven)** | Reference / mechanism |
|--------|------------------------|----------------------------------|----------------------|
| **LiveCodeBench v6** | 42-45% | **55-60%** | rStar-Coder 7B = 57.3% (paper-confirmed) |
| **HumanEval+** | ≥84% | **88-90%** | rStar-Coder + DPO + XGrammar |
| **MBPP+** | ≥75% | **82-85%** | same |
| **SWE-Bench Lite** | 25-30% | **40-45%** | DeepSWE recipe + R2E-Gym + DAPO RL |
| **SWE-Bench Pro** | 8-15% | **15-20%** | same + agent traces |
| **BFCL v3 overall** | 70-75 | **82-87** | Toucan-1.5M + xLAM + DPO + Hermes XML |
| **BFCL multi-turn** | 45-50 | **60-65** | When2Call DPO + agent SFT |
| **GAIA Level 1** | 20-30% | **35-45%** | multi-agent SFT + Letta memory |
| **RULER @ 32K** | 90+ | **94+** | 32K training + Liger + sample packing |
| **RULER @ 128K** | 80+ | **88+** | YaRN+DCA + NExtLong synth + 200M long-ctx tokens |
| **CodeHalu rate** | <8% | **<3%** | XGrammar + DoLa + Cite-or-Abstain + TruthRL |
| **Phantom imports** | <5% | **<2%** | XGrammar + AST-validity decoding |
| **Calibration AUC** | >0.85 | **>0.92** | Behaviorally Calibrated RL (Dec 2025 — Qwen3-4B = 0.902) |
| **Compile rate** | 100% | 100% | XGrammar (already perfect) |
| **DevSecOps custom** | 65%+ | **80%+** | validator-graded RLVR (PIPer paper) |
| **Cloud Eval (5-tier)** | 65% | **78%** | 250K IaC + Crossplane v2 + Terraform module distillation |
| **CyberMetric** | ≥75% | **≥85%** | Primus 5B continued pretrain + reasoning distill |
| **CTI-Bench** | ≥65% | **≥75%** | same |
| **CyberSOCEval** | ≥55% | **≥65%** | Sigma synth + IR runbook RLVR |
| **AI Eng composite** | 60-70% | **80%+** | 180K samples × 3 stages (SFT + SimPO + GRPO) |
| **AIOpsLab** | parity GPT-4o | **above GPT-4o on detection+localization** | 28-35K SRE SFT + sandboxed kubectl traces |
| **Multi-role debate** | ≥45% blind preference | **≥55%** | 100K CAMEL synth + 9-LoRA Arrow composition |
| **Continuous Bench** | 40% | **55%** | Devin-pattern + Manus todo.md + Aider git-as-persistence |
| **30-day soft launch** | ≥8/10 goals | **≥9/10 goals**, ≤3h/wk founder time | full Phase A+B+C polish |

## How to push BEYOND conservative — technique-by-technique

### 1. rStar-Coder (THE breakthrough for 7B coder)
**Paper**: [arxiv 2505.21297](https://arxiv.org/abs/2505.21297)

**What they did**:
- 418K competitive programming problems
- 580K long-reasoning solutions (CoT verified by tests)
- 3-step input generation + mutual verification for test cases
- Result: Qwen2.5-7B 17.4% → **57.3% LCB**, matches Claude 3.5 Sonnet

**Implementation for v2**:
- Use `microsoft/rStar-Coder` dataset (already in dataset-mirror.sh v2 list — 30K samples)
- BUMP allocation to 100K samples (full available is 580K — paper used 580K!)
- Train at 32K context with sample packing
- Long reasoning chains naturally fit (avg ~3K tokens/example)

**Expected lift**: +20-25pt on LiveCodeBench v6 alone

### 2. DeepSeek-V3 Multi-Token Prediction (MTP)
**Paper**: [arxiv 2412.19437](https://arxiv.org/html/2412.19437v1)

**What it does**:
- Auxiliary heads predict tokens 2, 3 positions ahead
- Maintains causal chain (sequential prediction)
- Densifies training signal (more gradients per forward pass)
- Bonus: speculative decoding 1.8× speedup at inference

**Implementation for v2**:
- Add MTP heads to LoRA training (custom Axolotl plugin)
- 2 auxiliary heads = 3× signal density
- Discard heads at inference (or repurpose for spec-decoding)

**Expected lift**: +3-5% on all coding metrics (Qwen3-Coder used MTP)

### 3. Magpie self-instruct (FREE 1M instructions)
**Paper**: [ICLR 2025](https://github.com/magpie-align/magpie)

**What it does**:
- Prompt aligned LLM with ONLY chat template (no actual prompt)
- Auto-regressive nature → model generates user query + response
- ZERO API cost beyond GPU hours
- Generated 1M-3M from Llama-3-70B in ~600 GPU-hr

**Implementation for v2**:
- Run Magpie on `Qwen2.5-Coder-32B-Instruct` (free via HF Inference or local)
- Generate 1M code-related instructions
- Cost: ~200 GPU-hr free Lightning quota
- vs. Claude API for same volume = $5,000+

**Expected**: 1M extra training samples for FREE

### 4. DAPO RL (ByteDance/Tsinghua, BEATS GRPO)
**Paper**: [arxiv 2503.14476](https://arxiv.org/abs/2503.14476)

**What it does**:
- Decoupled clip + Dynamic sampling + Token-level policy gradient loss
- Qwen2.5-32B → 50pt AIME 2024 (better than GRPO)
- Open-source via verl framework

**Implementation for v2 Stage 3**:
- Replace GRPO → DAPO in stage3-rlvr.yml
- Same data (SWE-Gym + R2E-Gym + custom DevSecOps)
- verl framework supports out-of-box

**Expected lift**: +5-8% on SWE-Bench (vs GRPO baseline)

### 5. Mergekit 9-LoRA composition (TIES + DARE)
**Tools**: [mergekit](https://github.com/arcee-ai/mergekit), [PEFT merging](https://huggingface.co/blog/peft_merging)

**What it does**:
- Combine 9 specialized LoRAs into 1 model
- TIES: sign-consensus, dropout interfering weights
- DARE: random prune + rescale
- DARE-TIES: best for 5+ adapters
- CPU-only or 8GB VRAM

**Implementation for v2 Phase B end**:
- Train 9 LoRAs separately (eng-build, eng-ops, eng-sec, etc.)
- Merge via DARE-TIES into single super-LoRA
- vLLM serves single model (no multi-LoRA latency)

**Expected lift**: +2-5% across all domain benchmarks (vs single LoRA)

### 6. XGrammar default decoding (FREE structural correctness)
**Tool**: [XGrammar](https://github.com/mlc-ai/xgrammar) (default vLLM 2026-04+)

**What it does**:
- Context-free grammar enforcement at decode
- JSON / regex / custom CFG
- 96-98% structural correctness
- 5× TPOT speedup
- Zero training cost

**Implementation for v2 inference**:
- Already planned. Just enable: `vllm serve --guided-decoding-backend xgrammar`
- Define grammars per use case:
  - Tool calls: JSON schema
  - Code blocks: Python/Bash/SQL/Terraform/YAML grammars
  - Output structure: Markdown headers

**Expected**: 100% syntax correctness on tool calls + code blocks

### 7. NExtLong long-context curriculum (ICML 2025)
**Paper**: arxiv 2501.12766

**What it does**:
- Long sequences with HARD negatives interleaved
- Synthetic > human-curated for long context
- ~10B tokens needed (we use 200M-500M subset)

**Implementation for v2 Stage 1**:
- 60% long context (≥16K) repo-concat with FIM
- 40% short context
- Hard negatives: similar-but-incorrect code samples interleaved
- NExtLong synth via free LLM ladder

**Expected**: RULER @ 128K from 80 → **88+**

### 8. Behaviorally Calibrated RL (Dec 2025)
**Paper**: arxiv (Dec 2025) — Qwen3-4B AUC 0.902

**What it does**:
- Train model to KNOW when it doesn't know
- Reward = 1 if correct + confident OR refused + uncertain
- Penalty for confident-wrong (TruthRL-style ternary)

**Implementation in v2 Stage 5**:
- Already in plan via TruthRL
- Add: behavioral cal eval suite
- Target AUC > 0.92 (above the paper)

**Expected**: Hallucination rate <3% + calibration AUC > 0.92

### 9. Self-Play SWE-RL (Together AI DeepSWE)
**Blog**: [Together DeepSWE](https://www.together.ai/blog/deepswe)

**What they did**:
- Generate bugs synthetically
- Train model to fix them
- Iterative: model becomes better at finding bugs → trains on harder bugs
- Open recipe at [agentica-project/rllm](https://github.com/agentica-project/rllm)

**Implementation for v2 Stage 4-5 (post Phase B)**:
- Self-play loop: bug-injector model + bug-fixer model
- Both start from Phase B artifact
- Diverge over time

**Expected lift**: SWE-Bench Lite +5-10pp

### 10. Stack-Edu / FineWeb-Edu classifier filtering
**Tools**: HuggingFaceTB/stack-edu-classifier-python, fineweb-edu-classifier

**What it does**:
- Score each code/text sample 1-5 for educational quality
- Train only on threshold ≥3 (Phi-4 method)

**Implementation for v2 data pipeline**:
- Already in dedup-decontaminate.py plan
- Apply BEFORE final SFT mix
- Drop ~30% lowest-quality

**Expected lift**: +2-3% on HumanEval+ from cleaner data

---

## Compute & cost (NO Anthropic API)

| Item | Cost | Source |
|------|------|--------|
| HF PRO | $9/mo | HuggingFace |
| Wasabi 1 TB | $6/mo | Wasabi |
| Lightning H200 | free 80hr/mo (ashiradevops + ashirapit) | Lightning |
| Anthropic API | **$0** ❌ removed | replaced by free LLM ladder |
| Synth data gen | $0 | Cerebras qwen-3-235b + Groq llama-3.3-70b free + Magpie self-instruct |
| GPU compute extra | $0-200 (RunPod spot only if Lightning exhausted) | optional |

**Total**: $15/mo + $0-200 one-time. (down from prior $1,700-3,800 estimate)

## v2 Phase Map (revised)

| Phase | Weeks | Output | Cost |
|-------|-------|--------|------|
| **A**: Code+Tool+Agent SFT/DPO | 4 | `surrogate-1-coder-7b-lora-v2-mvp` | $0-200 |
| **A+**: rStar-Coder 100K + Magpie 1M continued SFT | +1 | bigger lift on LCB | free |
| **B**: 9 LoRA cluster expertise (parallel) | 4 | 9 LoRAs | $200-500 (parallel) |
| **B+**: DARE-TIES merge → super-LoRA | 0.5 | 1 merged LoRA | free (CPU) |
| **C**: DAPO RLVR + TruthRL | 2-3 | RL polish | $200-500 |
| **C+**: Self-Play SWE-RL bug inject/fix | 1-2 | iterative improvement | free (Lightning) |

**Total: 12-15 weeks / $400-1,200 / no Anthropic API**


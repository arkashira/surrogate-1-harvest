---
title: Coding LLM Fine-Tune Frontier (2025-2026)
date: 2026-05-01
tags: [llm, code, fine-tuning, rl, surrogate-1, qwen-coder, t4, lora, grpo, rlvr, kaggle]
target_model: Qwen2.5-Coder-7B
target_hardware: Kaggle T4 x2 (16+16GB)
target_pipeline: Surrogate-1 V8
status: actionable
---

# Coding LLM Fine-Tune Frontier (V8 candidate techniques)

> Research for Surrogate-1 V8 — what to add on top of V7's stack
> (LoRA r=64 + DoRA + RSLoRA + LoftQ + NEFTune α=5 + Spectrum-lite + sample
> packing + Magpie + active-learning + cosine_with_restarts + AdamW8bit + NF4 + BF16 + grad-ckpt).
> Constraint: must fit on **2 × T4 16GB**, no >24GB GPU tricks, no exotic preference data.

## Score-Lift Table (expected, vs V7 baseline on Qwen2.5-Coder-7B)

| Technique | HumanEval+ | MBPP+ | LCB v6 | SWE-bV | Memory | Code-only? |
|---|---|---|---|---|---|---|
| GRPO + execution rewards (RLVR-Code) | +4-7 pp | +3-5 pp | +5-9 pp | +1-3 pp | +0 (LoRA) | yes |
| Self-distill rejection-sampling SFT | +3-5 pp | +2-4 pp | +4-8 pp | n/a | +0 | yes |
| LoRA+ (η_B = 16·η_A) | +1-2 pp | +1-2 pp | +1 pp | n/a | +0 | yes |
| PiSSA init (replace LoftQ) | +1-3 pp | +1-2 pp | +1-2 pp | n/a | +0 | yes |
| RLEF execution-feedback multi-turn | +2-3 pp | +2 pp | +5-8 pp | +2-4 pp | +0 | yes |
| APOLLO-Mini optimizer | ±0 (perf) | ±0 | ±0 | n/a | -25% optim mem | no |
| SOAP optimizer | +0-1 pp | +0-1 pp | +0-1 pp | n/a | +5% | no |
| Self-consistency @ eval (k=10 voting) | +2-4 pp | +2-3 pp | +3-5 pp | n/a | +0 (inference) | yes |
| OpenCodeInterpreter Code-Feedback data | +1-3 pp | +1-2 pp | +2-4 pp | n/a | +0 | yes |
| SWE-Gym 500-instance subset | +0 | +0 | +0-1 pp | +3-5 pp | +0 | agent |
| Verifier-reranking @ eval | +1-3 pp | +1-2 pp | +2-4 pp | n/a | +0 (inference) | yes |
| MEDUSA inference heads | 0 | 0 | 0 | 0 | inference 2-3× | n/a |

**pp** = percentage points. **SWE-bV** = SWE-bench Verified (only attainable with multi-turn agent loop).

---

## 1. GRPO with Execution-Based Rewards (RLVR-Code)

**1-line**: PPO without a critic — sample G≥4 completions per prompt, normalise rewards within the group, update with policy-gradient + KL anchor; reward = unit-test pass rate.

**Citations**:
- DeepSeekMath / DeepSeek-R1 GRPO (arXiv:2402.03300, Feb-2024)
- DAPO improvements: Clip-Higher + Dynamic Sampling + Token-Level loss (arXiv:2503.14476, ByteDance Seed, Mar-2025)
- DeepCoder-14B applied "GRPO+" → 60.6% LCB v5 / 92.6% HumanEval+ (Together AI / Agentica, Apr-2025)
- ReST-RL (`m2DziqZ5YZ` OpenReview, 2025) — variance-aware GRPO for code
- TRL `GRPOTrainer` + Unsloth GRPO patch (Aug-2025+)

**Why for Surrogate-1**: Single biggest documented score lift from V7→V8 candidates. Unit tests are a free, dense, *verifiable* reward — no preference labels needed. DeepCoder showed +30 pp jump on LCB after GRPO from a Qwen2.5-Coder-base; even a fraction of that on 7B-LoRA is worth the risk. Pairs naturally with our Magpie + active-learning data.

**HOW (kaggle-trainer.sh)**:
- Env knobs: `SUR_RL_STAGE=grpo`, `SUR_RL_GROUP_SIZE=4` (T4 OOM if >4), `SUR_RL_KL=0.04`, `SUR_RL_STEPS=200`, `SUR_RL_LR=5e-7`, `SUR_RL_REWARD=execution`.
- Place AFTER current SFT epoch (two-stage: SFT → GRPO on adapter-merged checkpoint).
- TRL ≥ 0.12 has `GRPOTrainer`; pass `peft_config=current LoRA cfg`. If `peft_config` rejected → fall back to merge-then-LoRA-again, OR use Unsloth GRPO recipe (works on T4).
- Reward fn: spawn `subprocess.run(["python", "-c", code], timeout=5)` against held-out unit tests from MBPP-test or self-generated tests via `bigcode/codeparrot-clean` snippets. Use `firejail`-style restriction or run in a `docker` container if available — on Kaggle, just rlimit + timeout + tempdir is sufficient.
- T4 mem: with G=4, seq=1024, ref-model offloaded to CPU and 4-bit quantised → fits in 14GB / GPU. Use `vllm`-style pre-rollout if `trl` ≥ 0.13 (Liger-GRPO blog, Aug-2025) — gives ~3× rollout speed. **Fallback**: if vllm OOMs, set `vllm_gpu_memory_utilization=0.5` and `--use_vllm=False`.
- Bootstrap reward data: sample N=2000 problems from `MBPP/sanitized` + `LiveCodeBench/release_v6` train split + Magpie-generated problems with self-generated tests filtered by base-model pass-rate ∈ [0.2, 0.8] (medium difficulty — Dr.GRPO finding).

**Risks**:
- Reward hacking: model emits `import sys; sys.exit(0)` → use `assert`-counting + AST sanity check.
- Entropy collapse → use DAPO's Clip-Higher (ε_high=0.28, ε_low=0.2) and add 0.001 entropy bonus.
- Length explosion → add length penalty `r -= 0.05·max(0, tokens-512)/512`.
- KL divergence blow-up → if KL > 1.0 over 50 steps, reduce `lr` by 5× (auto-throttle).

---

## 2. Self-Distillation via Rejection Sampling (SSD-Code)

**1-line**: Sample N≥8 completions for each training prompt with temp=0.7, **filter only those that pass unit tests**, retrain SFT on the filtered self-outputs.

**Citations**:
- "Embarrassingly Simple Self-Distillation Improves Code Generation" (arXiv:2604.01193 — corrected Apr-2026) — Qwen3-30B 42.4 → 55.3 LCB v6 (+12.9 pp pass@1).
- REDI two-stage (arXiv:2505.24850, May-2025) — adds negative-trace utilisation in stage 2.
- RLHF-book / Lambert chapter 9 — rejection sampling fine-tuning canonical recipe.

**Why for Surrogate-1**: SSD is the single most underrated 2025 finding — beats DPO-style preference RL on code without needing pair labels, and runs as **plain SFT** so it reuses our entire V7 pipeline (LoRA r=64 + DoRA + sample-packing). The +12.9 pp is on a stronger base; expect +3-5 pp on a 7B base after V7. Can be stacked under GRPO (do SSD first, then GRPO on top).

**HOW (kaggle-trainer.sh)**:
- Env knobs: `SUR_SSD_ENABLE=1`, `SUR_SSD_N=8`, `SUR_SSD_TEMP=0.7`, `SUR_SSD_PASS_REQUIRED=1`.
- Insert as a **pre-training data-augment phase** (separate Kaggle session, output saved to HF dataset hub):
  1. Load current best Surrogate-1 checkpoint (V7 final).
  2. For each training prompt (~5k subset of Magpie pairs), generate 8 completions via vLLM batched (T4 fits 7B in 4-bit at batch 16).
  3. Evaluate each completion with execution sandbox (same as #1).
  4. Keep all that pass; if prompt has 0 passing → keep the highest-loglikelihood one as "soft positive".
  5. Save to `surrogate-1-ssd-mix.jsonl` and append to V8 SFT mix.
- T4 mem: rollout-only, no gradients → 12GB / GPU peak.
- **Fallback**: if vLLM unavailable on Kaggle T4 (CUDA mismatch) → use HF `model.generate` with `do_sample=True, num_return_sequences=8, batch_size=2`. ~3× slower but works.

**Risks**:
- Mode collapse: if base model is weak, all 8 samples wrong → drift. Mitigate: keep "hard" prompts (where pass@8 = 0) as **rejected** examples for KTO-style stage 3 (skip if no time).
- Distribution shift away from human-written code style → mix 50/50 SSD outputs + original Magpie ground truth.
- Cost: rollout adds ~1.5 GPU-hr per 5k prompts. Run in spare Kaggle session, don't gate V8 on this.

---

## 3. LoRA+ (asymmetric A/B learning rates)

**1-line**: Set `lr_B = 16 × lr_A` in the LoRA adapter — the "B" matrix needs much higher LR for efficient feature learning under Adam.

**Citations**:
- Hayou, Ghosh, Yu, "LoRA+: Efficient Low Rank Adaptation of Large Models" (arXiv:2402.12354, ICML-2024).
- Thinking Machines Lab "LoRA Without Regret" (Sep-2025) confirms +1-2 pp consistently.
- HF PEFT LoRA+ flag merged in PEFT 0.14.

**Why for Surrogate-1**: Free win. Costs zero memory, zero extra code, ~2× faster convergence in the published curves. We're already on a near-optimal stack — this is the kind of marginal lift we need to keep stacking. Independent of GRPO/SSD; can apply in V7.5 as a quick A/B before V8.

**HOW (kaggle-trainer.sh)**:
- Env knobs: `SUR_LORA_PLUS=1`, `SUR_LORA_PLUS_RATIO=16` (paper sweet spot for LM head; try 8 if loss spikes).
- In `train.py` after `LoraConfig(...)`:
  ```python
  if os.environ.get("SUR_LORA_PLUS") == "1":
      ratio = float(os.environ.get("SUR_LORA_PLUS_RATIO", "16"))
      from peft.optimizers import create_loraplus_optimizer
      optimizer = create_loraplus_optimizer(
          model=model, optimizer_cls=bnb.optim.AdamW8bit,
          lr=cfg.learning_rate, loraplus_lr_ratio=ratio,
      )
      trainer.optimizer = optimizer
  ```
- **Fallback**: if PEFT < 0.14 → manually create two param groups: `[p for n,p in model.named_parameters() if "lora_B" in n]` with 16× LR vs `lora_A`.
- T4 mem: identical to plain LoRA. Zero overhead.

**Risks**:
- Gradient explosion if ratio too high (>32) on small base. Stick to 16.
- Interaction with DoRA's magnitude vector unclear — paper recommends LoRA+ ratio=16 on `lora_B`, leave `magnitude` and `lora_A` at base LR. Verify loss curve is monotonic first 200 steps.

---

## 4. PiSSA Initialisation (replace / complement LoftQ)

**1-line**: Initialise LoRA's A,B with the **principal singular vectors** of W (top-r SVD components) instead of random+zero — adapter starts already projecting onto the most informative weight directions.

**Citations**:
- Meng et al., "PiSSA" (arXiv:2404.02948, NeurIPS-2024 Spotlight). +5.16 pp Mistral-7B GSM8K vs LoRA.
- "Make LoRA Great Again" extension (arXiv:2502.16894, Feb-2025) — improves PiSSA stability under quant.
- HF PEFT `init_lora_weights="pissa_niter_4"` since PEFT 0.11.

**Why for Surrogate-1**: We're already running LoftQ — PiSSA is a *strict superset* in the published comparisons (QPiSSA beats QLoRA *and* QLoftQ on GSM8K LLaMA-3-70B). Setup cost is one SVD at init (~30 sec for Qwen2.5-Coder-7B). Especially valuable for the first 500 steps of LoRA where most of the score gap comes from. Free win, but reviewer should A/B against current LoftQ first — they target the same problem (init under quant), so we keep ONE not both.

**HOW (kaggle-trainer.sh)**:
- Env knobs: `SUR_LORA_INIT=pissa` (alt: `pissa_niter_8` for higher fidelity), `SUR_LORA_INIT=loftq` (current default), `SUR_LORA_INIT=kaiming` (escape valve).
- In `train.py` `LoraConfig` constructor:
  ```python
  init = os.environ.get("SUR_LORA_INIT", "loftq")
  if init.startswith("pissa"):
      lora_cfg = LoraConfig(..., init_lora_weights=init)  # "pissa" or "pissa_niter_4"
  elif init == "loftq":
      lora_cfg = LoraConfig(..., init_lora_weights="loftq", loftq_config=LoftQConfig(loftq_bits=4))
  ```
- After fit, run `model.peft_config['default'].init_lora_weights = True` and call `model.save_pretrained(..., convert_pissa_to_lora=base_model_path)` to save *residual+adapter* in standard LoRA format for downstream merge.
- T4 mem: SVD is one-time CPU op for 7B linear layers, ~2GB peak host RAM. No GPU cost.

**Risks**:
- PiSSA mutates the base weights (subtracts U·Σ·V from W), so checkpoint restoration must convert back. Forgetting this corrupts the saved model. **Always test save→load→generate before training overnight.**
- Conflict with LoftQ: pick one. Current evidence: PiSSA > LoftQ for code (similar magnitude on math GSM8K).

---

## 5. RLEF — Multi-Turn Execution Feedback

**1-line**: RL-train the model to *iterate* — given a public-test failure trace, produce a fix; reward = pass on private tests after ≤3 turns.

**Citations**:
- Gehring et al., "RLEF: Grounding Code LLMs in Execution Feedback with RL" (arXiv:2410.02089, ICML-2025 Spotlight).
- Outperforms single-shot sampling by 1 OOM (10× fewer samples for same pass-rate on CodeContests, transferable to HumanEval+/MBPP+).

**Why for Surrogate-1**: This is the *only* technique on this list that legitimately moves SWE-bench Verified, because real-world bug fixes are inherently multi-turn (read traceback → patch → rerun). LiveCodeBench v6 also has hidden tests, so multi-turn helps there too. The 8B-model variant works; 7B should too. Builds on top of GRPO infrastructure (#1) — same reward, same trainer, just multi-turn rollout.

**HOW (kaggle-trainer.sh)**:
- Env knobs: `SUR_RLEF_ENABLE=1`, `SUR_RLEF_MAX_TURNS=3`, `SUR_RLEF_PUBLIC_FRAC=0.5` (50% of unit tests visible during turns, rest hidden for final reward).
- Implement as a custom rollout fn passed to `GRPOTrainer.rollout_fn`:
  ```python
  def rlef_rollout(prompt, model, tokenizer):
      conv = [{"role":"user","content":prompt}]
      for turn in range(MAX_TURNS):
          out = model.generate(conv)
          conv.append({"role":"assistant","content":out})
          pub_pass, pub_log = run_tests(out, public_tests)
          if pub_pass:
              break
          conv.append({"role":"user","content":f"Tests failed:\n{pub_log}\nFix it."})
      priv_pass = run_tests(out, private_tests)
      return out, priv_pass  # priv_pass is the reward
  ```
- Same execution sandbox as #1 but split tests 50/50 visible/hidden.
- **Fallback**: if multi-turn rollout exceeds 4096 tokens in 50% of cases → drop to MAX_TURNS=2 or shorten public_tests to 2 examples.
- T4 mem: 3 turns × 1024 tokens = 3k context; fits with KV-cache offload. Group size G=2 only (4 still risky).

**Risks**:
- Long-context training instability — must set `attn_impl="flash_attn_2"` or fall back to `sdpa`. Don't use eager.
- Reward sparsity: 70% of CodeContests problems will fail all 3 turns → use `α-shaped` reward (partial = #public_passed/#public_total at the last turn).
- CodeContests is too hard for 7B — bootstrap from MBPP+ (easier) first.

---

## 6. Self-Consistency Voting at Eval Time

**1-line**: At eval/serving time, sample k=10 completions, run all in sandbox, **return the one whose output most other completions agree with** (or simply the most common solution) → +2-4 pp HumanEval+ for free.

**Citations**:
- ICLR-2025 paper "Test-case-based self-consistency for code" (arXiv eg. 31a57804... in proceedings).
- Brown et al. (2024) "Large Language Monkeys" — repeated sampling scales pass-rate.

**Why for Surrogate-1**: **Pure inference-time technique**, zero training change. Verifies our final V8 score on HumanEval+/MBPP+/LiveCodeBench reports — caller can choose pass@1 (k=1) or pass@1 with self-consistency (k=10). Public model card numbers should be honest pass@1 (k=1) but our internal benchmark sweeps should both.

**HOW (kaggle-trainer.sh)**:
- Implement in `eval.py` not in trainer. Env: `SUR_EVAL_K=10`, `SUR_EVAL_CONSISTENCY=majority` (alt: `verifier`).
- For each problem: generate 10 with temp=0.6; cluster by output-on-public-tests; pick mode-cluster's top-loglikelihood completion.
- Cost: 10× eval time. Run nightly, not per-checkpoint.

**Risks**: None for training; only watch the eval cost budget.

---

## 7. OpenCodeInterpreter Code-Feedback Data Mix

**1-line**: Add 68k multi-turn (problem → code → exec error → fix) trajectories to the SFT mix — directly teaches the model to read a traceback.

**Citation**: Zheng et al., "OpenCodeInterpreter: Integrating Code Generation with Execution and Refinement" (ACL-2024 Findings, arXiv:2402.14658, v3 Jan-2025). 33B model 76.4 → 84.6 HumanEval+ with feedback turns; smaller-scale lift for 7B documented.

**Why for Surrogate-1**: Cheapest single data-mix add. The dataset is on HF (`m-a-p/Code-Feedback`, 68k samples ~80MB after dedup). Pairs perfectly with RLEF (#5) — SFT on Code-Feedback first, then RLEF on top. Even *without* RLEF, this dataset alone gives a 1-3 pp lift on HumanEval+ for SFT-only runs.

**HOW (kaggle-trainer.sh)**:
- Env: `SUR_DATA_CODEFEEDBACK=1`, `SUR_CODEFEEDBACK_RATIO=0.15` (15% of mix).
- In `train.py` data-loading:
  ```python
  if os.environ.get("SUR_DATA_CODEFEEDBACK") == "1":
      cf = load_dataset("m-a-p/Code-Feedback", split="train")
      cf = cf.map(lambda x: {"text": format_chat(x["messages"])})
      mix = interleave([magpie, codefeedback], probabilities=[0.85, 0.15])
  ```
- Filter: drop rows with >SEQ_LEN tokens (sample-packing handles the rest).
- T4 mem: same as current; just more data to iterate.

**Risks**: Some Code-Feedback turns are >2k tokens — sample-packing helps but watch OOM. Tighten `SEQ_LEN` to 2048 max if mix shows >5% truncation.

---

## 8. SWE-Gym Mini-Slice for SWE-bench Lift

**1-line**: 2,438 real Python repo bug-fix tasks with executable tests — sample 200-500 SHORT ones for SFT to seed multi-file-edit ability.

**Citation**: Pan et al., "Training Software Engineering Agents and Verifiers with SWE-Gym" (ICML-2025, arXiv:2412.21139). 7B agent went from 7% → 14.6% SWE-bench Lite.

**Why for Surrogate-1**: ONLY relevant if we care about SWE-bench Verified. 7B agents historically stuck at sub-10% — SWE-Gym is the proven recipe. Caveat: full SWE-Gym training is **agent-rollout heavy** and won't fit on T4 within Kaggle 9hr limit. Compromise: take a 200-instance subset where the patch is < 50 lines, format as plain SFT (not agent rollout), include in V8 SFT mix.

**HOW (kaggle-trainer.sh)**:
- Env: `SUR_DATA_SWEGYM=1`, `SUR_SWEGYM_MAX_PATCH_LINES=50`.
- Format: `<task_description>\n<file_contents>\n---\n<patch_diff>` as instruction-completion. Skip rollout — too expensive for T4.
- T4 mem: large contexts (4-8k tokens common). Use `SEQ_LEN=4096`, sample-pack, NEFTune still on.

**Risks**:
- Long contexts cut throughput ~3×. Budget accordingly. If iteration drops below 0.3 it/s on T4, drop SWE-Gym.
- 200 instances is too few to move SWE-bench Verified meaningfully; this is "seeding" not "training". Real lift only with full agent loop (out of scope for V8).
- **Skip if RLEF (#5) is enabled** — they target the same gap and RLEF is more efficient.

---

## 9. APOLLO-Mini Optimizer (memory rescue)

**1-line**: SGD-like memory cost (rank-1 auxiliary), AdamW-level convergence — gives back ~2GB on T4 to use for batch-size or longer context.

**Citations**:
- Zhu et al., "APOLLO: SGD-like Memory, AdamW-level Performance" (MLSys-2025 Outstanding Paper Honorable Mention, arXiv:2412.05270).
- Code: github.com/zhuhanqing/APOLLO.

**Why for Surrogate-1**: Pure memory win. We're optimizer-state bound on T4 — AdamW8bit already saves a lot, but APOLLO-Mini is even more aggressive (~1/8 to 1/1024 the optimizer memory). The 2GB headroom unlocks: G=4 → G=6 in GRPO, OR `per_device_train_batch_size` 1 → 2, OR `SEQ_LEN` 2048 → 3072. Choose what helps the eval most.

**HOW (kaggle-trainer.sh)**:
- Env: `SUR_OPTIMIZER=apollo_mini` (alt: `adamw_8bit` current, `apollo`, `soap`).
- `pip install apollo-torch` then:
  ```python
  if optname == "apollo_mini":
      from apollo_torch import APOLLOAdamW
      optimizer = APOLLOAdamW(model.parameters(), lr=lr,
          rank=1, scale_type="tensor", update_proj_gap=200)
  ```
- **Fallback**: `apollo-torch` install fail on Kaggle → use `pip install galore-torch` and use GaLore-rank-1 as moral equivalent.
- T4 mem: -25% optimizer memory vs AdamW8bit (8bit was already efficient; APOLLO is sub-8bit effectively).

**Risks**:
- Newer codebase, fewer eyeballs than AdamW8bit. **Run for 200 steps first**, compare loss curve to AdamW8bit run. Abort if divergence > 5%.
- Reuse of LR tuned for AdamW: paper says works with same LR; verify on small slice.

---

## 10. Verifier Reranking (Best-of-N at Eval)

**1-line**: Train a small (<1B) verifier head on (problem, candidate-solution) → score; at eval, sample N=20 with base model, rerank by verifier+execution, pick top-1.

**Citations**:
- "Trust but Verify" survey (arXiv:2508.16665v3, Aug-2025).
- Self-Certainty BoN (OpenReview `29FRqmVQK8`, 2025) — uses model's own loglikelihood as verifier, no extra model.

**Why for Surrogate-1**: Eval-time only; doesn't change training. Use **self-certainty** variant (no separate verifier model) for our T4 constraint — at eval, score each candidate by `mean -log(p(token))` and pick the most-confident one that also passes public tests. +1-3 pp pass@1 with k=20 candidates.

**HOW**: Eval-only, no training change. In `eval.py`:
```python
candidates = model.generate(prompt, num_return_sequences=20, temperature=0.6, return_dict_in_generate=True, output_scores=True)
scored = [(seq, mean_logp) for seq, mean_logp in zip(candidates.sequences, compute_logp(candidates.scores))]
passing = [(seq, lp) for seq, lp in scored if run_public_tests(seq).passed]
best = max(passing or scored, key=lambda x: x[1])
```

**Risks**: Same as self-consistency — eval cost. Combine with #6 (one inference budget covers both).

---

## 11. SOAP Optimizer (alternative to AdamW8bit)

**1-line**: Adam in Shampoo's eigenbasis — 35% wall-clock reduction vs AdamW on LM pretraining; one extra HP (preconditioning_freq).

**Citation**: Vyas et al., "SOAP" (arXiv:2409.11321, ICLR-2025). 40% iteration reduction at 360M-660M scale.

**Why for Surrogate-1**: **Lower priority than APOLLO** for our specific T4 setup, because SOAP adds memory (preconditioner state) — exactly what we don't have. Keep on radar; revisit when we move to A100 in V9.

**HOW**: `pip install soap-optimizer`; same swap-in pattern as APOLLO.

**Risks**: +5% optimizer memory. T4 will OOM at SEQ_LEN=2048 if base + LoRA fits tightly.

---

## 12. MEDUSA / EAGLE-3 Inference Heads (post-training, optional)

**1-line**: Train 4 small "future-token" heads on the frozen final model — at inference, predict 4 tokens in parallel via tree attention, verify with the base; 2.2-3.6× throughput.

**Citations**:
- Cai et al., MEDUSA (arXiv:2401.10774, ICML-2024).
- Li et al., EAGLE-3 (NeurIPS-2025, arXiv:2503.01840) — best draft-model approach, 3.05-6.5× speedup.

**Why for Surrogate-1**: ZERO HumanEval lift (mathematically equivalent at temp=0). Pure deployment win. Worth doing **after** V8 score is locked, to triple HF-Space inference throughput. Out of scope for the training-techniques shortlist; mentioned for completeness.

**HOW**: Train MEDUSA heads with `medusa-vicuna` repo style: freeze base, train 4 heads on the same SFT data for 1 epoch. ~2hrs on 2×T4. Save heads alongside the LoRA adapter.

**Risks**: vLLM serves Medusa natively; HF transformers does not. Choose the right serving stack first.

---

## What we're NOT recommending (and why)

| Technique | Verdict | Reason |
|---|---|---|
| Muon optimizer | **Skip** | "AdamW-pretrained → Muon-FT" is documented to *underperform*. Qwen2.5 was AdamW-pretrained. |
| Schedule-Free AdamW | Skip for V8 | Wins on schedule-search budget; we already tuned cosine_with_restarts. Marginal. |
| ORPO / SimPO / KTO | Skip | Need preference pairs we don't have. RLVR-Code (#1) gives the same lift without preference labels. |
| ReLoRA / Chain-of-LoRA | Skip | Designed for *pretraining*, paper explicitly notes "for fine-tuning does not outperform LoRA". |
| VeRA / VB-LoRA | Skip | 10× fewer params is great but our 7B-LoRA isn't param-bound, it's quality-bound. We *want* more capacity. |
| GaLore | Skip | APOLLO-Mini supersedes it (same author lineage, better empirical). |
| MCTS-on-code at inference | Skip for V8 | AB-MCTS is impressive but 50× inference cost. Self-consistency (#6) gives 80% of the win at 5× cost. |
| Lookahead decoding | Skip for V8 | EAGLE-3 strictly better; deal with serving in V9. |
| Process Reward Models (PRM800K-style) | Defer | CodePRM is interesting but needs step-level annotations we'd have to bootstrap; high complexity vs lift. Add to V9 candidates. |
| ToolACE / Hermes function-calling | Skip | Surrogate-1 isn't aiming for tool-use yet. Premature. |
| Agent-FLAN | Skip | Same — agent-tuning is a separate axis. |

---

## Pick Order for V8

Ordered by (lift × probability-of-success / integration-cost). Each tier is independent — implement top-down, stop when Kaggle 9hr budget is hit.

### Tier S — DO THESE
1. **GRPO + Execution Rewards** (#1) — biggest documented lift for code; ~6-8 hr Kaggle session per RL phase. Bootstrap reward data is auto-generatable; no external download needed.
2. **Rejection-Sampling Self-Distillation** (#2) — orthogonal to GRPO, runs as plain SFT; ~3 hr rollout + reuse current SFT pipeline. Pure code change to train.py.
3. **LoRA+** (#3) — 5-line code change, no extra cost, +1-2 pp guaranteed. Pure code change.

### Tier A — STRONG ADDS
4. **PiSSA initialisation** (#4, replacing LoftQ) — A/B vs current LoftQ; pick the better. Pure code change.
5. **OpenCodeInterpreter Code-Feedback data mix** (#7, 15% blend) — ~80MB external download; HF dataset.

### Tier B — IF TIME PERMITS
6. **RLEF multi-turn execution feedback** (#5) — only after #1 lands and is stable; same trainer, multi-turn rollout. Reuses #1's reward sandbox.
7. **APOLLO-Mini optimizer** (#9) — gives back 2GB → use for G=6 GRPO or longer SEQ_LEN. Low risk if A/B'd. Pure code change.

### Tier C — Eval-time wins (no training risk)
8. **Self-Consistency Voting @ k=10** (#6) — eval-only, +2-4 pp on the public score. Pure code change to eval.py.
9. **Verifier-Reranking (Self-Certainty)** (#10) — combine with #6, same inference budget. Pure code change to eval.py.

### Defer to V9
- SWE-Gym integration with full agent loop (need bigger GPU)
- MEDUSA/EAGLE-3 heads (deployment optimisation, not score)
- Process Reward Model for code (CodePRM-style)
- Muon / SOAP (post-A100 migration)

---

## Bootstrapping summary — where to get the missing data

| Data need | How we get it without external download |
|---|---|
| Unit tests for #1 GRPO | (a) MBPP-test split has tests included. (b) For Magpie-generated problems → ask current model to **write 3 unit tests** per problem at temp=0 (filter by "compiles + non-trivial assert count"). |
| Multi-turn traces for #5 RLEF | Generate from Magpie problems via #1's sandbox: failed-rollout trajectories naturally produce traceback strings. |
| Preference pairs (NOT NEEDED) | We don't use DPO/SimPO — RLVR-Code replaces them. |
| Rejection-sampling pairs for #2 | Self-generated from current best checkpoint; sandboxed. |

The only external download required is `m-a-p/Code-Feedback` (~80 MB on HF) for #7. Everything else is auto-generated inside the training pipeline using the **existing model + execution sandbox** combo.

---

## Sources

- DeepSeekMath / GRPO — arXiv:2402.03300
- DAPO — arXiv:2503.14476 ; verl-docs / NeMo-RL DAPO recipe
- DeepCoder-14B — together.ai/blog/deepswe + Agentica DeepCoder release
- RLEF — arXiv:2410.02089 (ICML-2025 Spotlight)
- RLVR (DeepSeek-R1 / Tulu 3) — arXiv:2506.14245 ; arXiv:2411.15124
- Self-Distill SSD-Code — arXiv:2604.01193
- LoRA+ — arXiv:2402.12354 (ICML-2024)
- PiSSA — arXiv:2404.02948 (NeurIPS-2024 Spotlight)
- DoRA — arXiv:2402.09353 (current V7 stack reference)
- OpenCodeInterpreter / Code-Feedback — arXiv:2402.14658v3 (ACL-2024 Findings, Jan-2025 v3)
- SWE-Gym — arXiv:2412.21139 (ICML-2025)
- SWE-RL — arXiv:2502.18449 (NeurIPS-2025)
- APOLLO — arXiv:2412.05270 (MLSys-2025 OP-HM)
- SOAP — arXiv:2409.11321 (ICLR-2025)
- Muon — arXiv:2502.16982 (Moonlight TR)
- MEDUSA — arXiv:2401.10774 (ICML-2024)
- EAGLE-3 — arXiv:2503.01840 (NeurIPS-2025)
- Lookahead — arXiv:2402.02057 (ICML-2024)
- VeRA — arXiv:2310.11454 (ICLR-2024)
- Verifier survey "Trust but Verify" — arXiv:2508.16665
- Magpie — arXiv:2406.08464 (ICLR-2025)
- Tulu 3 — arXiv:2411.15124
- Thinking Machines "LoRA Without Regret" (Sep-2025)

## See Also

- [[finops-and-other-ops]] — Kaggle / Lightning H200 cost trade-offs
- [[data-ml-aiops]] — RAG, vector DB, training infra
- [[development]] — code generation tooling
- [[lessons_learned]] — past Surrogate-1 training mistakes
- `~/.surrogate/state/kaggle-nb/train.py` — current trainer entrypoint
- `~/.surrogate/hf-space/bin/kaggle-trainer.sh` — orchestrator script

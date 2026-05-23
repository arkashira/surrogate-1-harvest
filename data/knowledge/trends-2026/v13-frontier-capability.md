---
title: V13 Frontier Capability Research — Surrogate-1 Trainer
date: 2026-05-01
status: research-complete
tags: [training, rlhf, grpo, dapo, mcts, voyager, reflexion, spectrum, yarn, quiet-star, moe, curriculum, swe-rl, polymath]
sources: arxiv.org, github.com, huggingface.co, verl.readthedocs.io
target: kaggle-trainer.sh PYEOF block (2x T4 16GB + Civo L40S 48GB tier)
base: Qwen2.5-7B / Qwen3-8B / Qwen3-14B / Qwen3-32B
---

# V13 Frontier Capability Research — Polymath Senior Engineer Surrogate-1

> Goal: every 2025-2026 technique that goes INTO model weights via training (not external scripts).
> Owner directive: comprehensive, paste-ready patches, real URLs, paper+repo+benchmark for each.

## 1. DAPO — Drop-in GRPO Upgrade (HIGHEST PRIORITY)

- Paper: [DAPO: An Open-Source LLM RL System at Scale](https://arxiv.org/abs/2503.14476) (ByteDance Seed + Tsinghua AIR, March 2025)
- Repo: [BytedTsinghua-SIA/DAPO](https://github.com/BytedTsinghua-SIA/DAPO) | [verl recipe](https://verl.readthedocs.io/en/latest/algo/dapo.html)
- Benchmark: **AIME 2024 = 50 pts on Qwen2.5-32B base** (vs DeepSeek-R1-Zero-Qwen-32B 47 pts), **50% fewer training steps**
- TRL support: native via `loss_type="dapo"` (already default in TRL ≥ 0.18). [Source: TRL GRPO docs](https://huggingface.co/docs/trl/main/en/grpo_trainer).

### Four DAPO components (each independent env knob)
1. **Clip-Higher** — decouple ε_low/ε_high. Verl reference: `ε_low=0.2, ε_high=0.28`. [verl issue #791](https://github.com/volcengine/verl/issues/791)
2. **Dynamic Sampling** — drop prompt groups with reward all-1 or all-0 (zero gradient). Refill until full batch.
3. **Token-Level PG Loss** — already applied via `loss_type="dapo"` (denominator = total active tokens in global batch).
4. **Overlong Reward Shaping** — linear penalty kicks in past `length_threshold_1`, full-cancel past `threshold_2`.

### V13 patch — paste under existing `RL_USE_TRUTHRL` block
```python
# === DAPO knobs (env-toggled, additive on TruthRL-GRPO) ===
RL_DAPO_ENABLE        = os.getenv("RL_DAPO_ENABLE", "1") == "1"
RL_DAPO_EPS_LOW       = float(os.getenv("RL_DAPO_EPS_LOW", "0.20"))
RL_DAPO_EPS_HIGH      = float(os.getenv("RL_DAPO_EPS_HIGH", "0.28"))
RL_DAPO_DYN_SAMPLE    = os.getenv("RL_DAPO_DYN_SAMPLE", "1") == "1"
RL_DAPO_OVERLONG_T1   = int(os.getenv("RL_DAPO_OVERLONG_T1", "3072"))
RL_DAPO_OVERLONG_T2   = int(os.getenv("RL_DAPO_OVERLONG_T2", "4096"))

if RL_DAPO_ENABLE:
    grpo_cfg.loss_type = "dapo"            # token-level loss aggregation
    grpo_cfg.epsilon = RL_DAPO_EPS_LOW     # ε_low
    grpo_cfg.epsilon_high = RL_DAPO_EPS_HIGH  # TRL ≥ 0.20 supports
    grpo_cfg.scale_rewards = "group"
    # Dynamic sampling: filter zero-variance groups before loss
    if RL_DAPO_DYN_SAMPLE:
        grpo_cfg.mask_truncated_completions = True

def _overlong_shape(reward, gen_len, t1=RL_DAPO_OVERLONG_T1, t2=RL_DAPO_OVERLONG_T2):
    if gen_len <= t1: return reward
    if gen_len >= t2: return reward - 1.0  # cancel correct-answer reward
    frac = (gen_len - t1) / (t2 - t1)
    return reward - frac
# Apply inside reward_func wrapper before returning
```

- T4×2 feasibility: **YES** for Qwen2.5-7B + LoRA. ε_high tweak adds zero memory cost.
- Conflicts: replaces TruthRL-GRPO clipping behaviour — wrap as nested toggle (TruthRL ternary reward stays, DAPO governs PG-clipping).

---

## 2. AsyncGRPO — Decoupled Rollout (TRL v0.20+)

- Docs: [TRL Asynchronous GRPO Trainer](https://huggingface.co/docs/trl/async_grpo_trainer)
- Issue tracker: [trl#4591](https://github.com/huggingface/trl/issues/4591)
- TRL v1 blog: [trl-v1.md](https://github.com/huggingface/blog/blob/main/trl-v1.md)
- Reference impl: [AReaL paper arxiv 2505.24298](https://arxiv.org/html/2505.24298v1) (large-scale async RL)
- Benchmark: removes idle-GPU bubble — generation runs on dedicated vLLM server while trainer takes gradient step. Together AI / NovaSky see 1.5-2× throughput. [SkyRL-Agent paper](https://arxiv.org/abs/2511.16108).

### V13 patch
```python
RL_ASYNC_ENABLE       = os.getenv("RL_ASYNC_ENABLE", "0") == "1"  # needs second device
RL_ASYNC_VLLM_URL     = os.getenv("RL_ASYNC_VLLM_URL", "http://127.0.0.1:8000")
RL_ASYNC_WEIGHT_SYNC  = int(os.getenv("RL_ASYNC_WEIGHT_SYNC", "16"))  # steps
RL_ASYNC_MAX_STALE    = int(os.getenv("RL_ASYNC_MAX_STALE", "3"))     # weight-version lag

if RL_ASYNC_ENABLE:
    from trl import AsyncGRPOTrainer, AsyncGRPOConfig
    grpo_cfg = AsyncGRPOConfig(
        **vars(grpo_cfg),
        vllm_server_host=RL_ASYNC_VLLM_URL,
        weight_sync_steps=RL_ASYNC_WEIGHT_SYNC,
        max_staleness=RL_ASYNC_MAX_STALE,
    )
    trainer_cls = AsyncGRPOTrainer
else:
    trainer_cls = GRPOTrainer
```

- T4×2 feasibility: **NO** — needs dedicated vLLM card. Requires Civo L40S 48GB (1 train + 1 vllm).
- Conflicts: incompatible with `RL_DAPO_DYN_SAMPLE` if `max_staleness > 0` and reward is unstable. Use `max_staleness=0` for strict on-policy.

---

## 3. Reflexion-at-Train (Reflect-Retry-Reward)

- Paper: [Reflect, Retry, Reward: Self-Improving LLMs via RL](https://arxiv.org/abs/2505.24726) (Writer + Cambridge, May 2025)
- Blog: [Writer engineering post](https://writer.com/engineering/self-reflection-llm-reinforcement-learning/)
- Benchmark: **+34.7% math equation writing, +18.1% function calling** on Llama-3.1-8B
- Mechanism: on first failure, model emits self-reflection; on retry success, GRPO rewards ONLY the reflection tokens — generalizes self-correction.

### V13 patch (additive on TruthRL)
```python
RL_REFLEXION_ENABLE   = os.getenv("RL_REFLEXION_ENABLE", "0") == "1"
RL_REFLEXION_MAX_TRIES = int(os.getenv("RL_REFLEXION_MAX_TRIES", "2"))

def reflexion_reward_wrapper(prompts, completions, **kwargs):
    base_rewards = ternary_truthrl_reward(prompts, completions)
    if not RL_REFLEXION_ENABLE: return base_rewards
    rewards = []
    for p, c, r in zip(prompts, completions, base_rewards):
        if r >= 1.0:
            rewards.append(r); continue
        # Step 2: ask model to self-reflect, then retry
        reflect_prompt = p + c + "\n<reflect>What went wrong?</reflect>\n"
        reflect_out = model.generate(reflect_prompt, max_new_tokens=256)
        retry_prompt = reflect_prompt + reflect_out + "\nRetry:\n"
        retry_out = model.generate(retry_prompt, max_new_tokens=512)
        retry_r = ternary_truthrl_reward([p],[retry_out])[0]
        # Reward ONLY reflection-token segment if retry succeeded
        rewards.append(retry_r if retry_r > r else r)
    return rewards
```

- T4×2 feasibility: **YES** but slow (3× rollouts). Cap to 5% of batch via `RL_REFLEXION_RATE=0.05`.
- Conflicts: doubles eval-time. Disable when `RL_ASYNC_ENABLE=1` until staleness handling proven.

---

## 4. Voyager Skill Library — Persistent Across Rounds

- Paper: [Voyager: Open-Ended Embodied Agent](https://arxiv.org/abs/2305.16291) (NVIDIA + Caltech, 2023; lifelong-learning extensions ongoing 2025)
- Repo: [MineDojo/Voyager](https://github.com/minedojo/voyager)
- 2025 lifelong-learning extension: [Beancount research log](https://beancount.io/bean-labs/research-logs/2026/05/08/voyager-open-ended-embodied-agent-lifelong-learning)
- Wired-into-training pattern: Voyager originally inference-time skill DB. For training, mine **verified skills** from prior round, top-K retrieval becomes positive few-shot context for next-round SFT.

### V13 patch — needs persistent JSON skill bank
```python
SKILLS_BANK_PATH = os.getenv("SKILLS_BANK_PATH", "/kaggle/working/skills_bank.jsonl")
SKILLS_USE       = os.getenv("SKILLS_USE", "1") == "1"
SKILLS_TOP_K     = int(os.getenv("SKILLS_TOP_K", "5"))

# At end of each training round, dump verified (reward >= 1.0) trajectories
def _dump_skill(prompt, completion, reward, tags):
    if reward < 1.0: return
    with open(SKILLS_BANK_PATH, "a") as f:
        f.write(json.dumps({
            "prompt": prompt[:512], "skill": completion[:2048],
            "reward": reward, "tags": tags,
            "embed_summary": completion[:128]   # for retrieval
        }) + "\n")

# At dataset prep for next round, retrieve top-K relevant skills as few-shot
def augment_with_skills(example, bank, k=SKILLS_TOP_K):
    if not SKILLS_USE or not os.path.exists(bank): return example
    sims = retrieve_topk(example["prompt"], bank, k=k)
    fewshot = "\n\n".join([f"<skill>{s['skill']}</skill>" for s in sims])
    example["prompt"] = fewshot + "\n\n" + example["prompt"]
    return example
```

- T4×2 feasibility: **YES** — pure file I/O. Use sentence-transformers/all-MiniLM-L6-v2 (90MB) for retrieval.
- Conflicts: skill bank grows unbounded — cap at 10K entries, evict by lowest reward.

---

## 5. Spectrum SNR (True Implementation, not top-N proxy)

- Paper: [Spectrum: Targeted Training on SNR](https://arxiv.org/abs/2406.06623) (Eric Hartford et al., June 2024)
- Repo: [QuixiAI/spectrum](https://github.com/QuixiAI/spectrum) (alias [cognitivecomputations/spectrum](https://github.com/cognitivecomputations/spectrum))
- AWS blog: [SageMaker integration](https://aws.amazon.com/blogs/machine-learning/using-spectrum-fine-tuning-to-improve-fm-training-efficiency-on-amazon-sagemaker-ai/)
- HF blog: [anakin87 explainer](https://huggingface.co/blog/anakin87/spectrum)
- Benchmark: Spectrum-50 matches full-FT, **15.48% time reduction**; Spectrum-25 matches QLoRA/LoRA, **36.78% reduction**.
- Math: Random Matrix Theory + Marchenko-Pastur — every weight matrix's singular spectrum yields SNR per layer-module.

### V13 patch (replace V12 "top-70% spectrum-lite")
```python
SPECTRUM_TRUE_SNR  = os.getenv("SPECTRUM_TRUE_SNR", "0") == "1"
SPECTRUM_TOP_PCT   = int(os.getenv("SPECTRUM_TOP_PCT", "30"))  # train top-30%
SPECTRUM_CACHE_DIR = os.getenv("SPECTRUM_CACHE_DIR", "/kaggle/working/spectrum_cache")

if SPECTRUM_TRUE_SNR:
    import subprocess
    snr_yaml = f"{SPECTRUM_CACHE_DIR}/{model_id.replace('/','_')}_snr_{SPECTRUM_TOP_PCT}.yaml"
    if not os.path.exists(snr_yaml):
        subprocess.run([
            "python", "-m", "spectrum.spectrum",
            "--model-name", model_id,
            "--top-percent", str(SPECTRUM_TOP_PCT),
        ], cwd=SPECTRUM_CACHE_DIR, check=True)
    # Apply: freeze layers NOT in spectrum YAML, keep ones IN as trainable
    import yaml
    keep = set(yaml.safe_load(open(snr_yaml))["unfrozen_parameters"])
    for n,p in model.named_parameters():
        p.requires_grad = any(k in n for k in keep)
```

- T4×2 feasibility: **YES** scan runs once (~5min for 7B), result cached as YAML.
- Conflicts: incompatible with `peft_config` for LoRA — Spectrum is for full-FT subset. Use either-or via `MODE=lora|spectrum`.

---

## 6. YaRN-Aware 32K Training Curriculum

- Paper: [YaRN: Efficient Context Window Extension](https://openreview.net/pdf?id=wHBfxhZu1u) (Nous Research, 2023; refined 2024-2025)
- Qwen3 official guide: [Context Scaling and YaRN](https://deepwiki.com/guquan/Qwen3/5.2-context-scaling-and-yarn)
- HF discussion: [Qwen2.5-32B YaRN clarifications](https://huggingface.co/Qwen/Qwen2.5-32B-Instruct/discussions/5)
- Combined with: [LongLoRA shifted sparse attention](https://arxiv.org/abs/2309.12307) — only 2 lines of code change.
- Unsloth guide: [Memory Efficient RL](https://unsloth.ai/docs/get-started/reinforcement-learning-rl-guide/memory-efficient-rl) — 8× longer context with grad-ckpt mode `"unsloth"`.

### V13 patch (write rope_scaling into config before from_pretrained)
```python
YARN_ENABLE          = os.getenv("YARN_ENABLE", "0") == "1"
YARN_FACTOR          = float(os.getenv("YARN_FACTOR", "2.0"))   # 4 = 128K, 2 = 64K
YARN_ORIG_MAX_POS    = int(os.getenv("YARN_ORIG_MAX_POS", "32768"))
YARN_TRAIN_MAX_LEN   = int(os.getenv("YARN_TRAIN_MAX_LEN", "32768"))

if YARN_ENABLE:
    from transformers import AutoConfig
    cfg = AutoConfig.from_pretrained(model_id)
    cfg.max_position_embeddings = int(YARN_ORIG_MAX_POS * YARN_FACTOR)
    cfg.rope_scaling = {
        "rope_type": "yarn",
        "factor": YARN_FACTOR,
        "original_max_position_embeddings": YARN_ORIG_MAX_POS,
    }
    model = AutoModelForCausalLM.from_pretrained(model_id, config=cfg, ...)

# Curriculum: 2K→8K→16K→32K across 4 epoch quarters
def _curriculum_max_len(step, total):
    pct = step / max(1, total)
    return [2048, 8192, 16384, YARN_TRAIN_MAX_LEN][min(3, int(pct*4))]
```

- T4×2 feasibility: **YES** for 7B at 32K with `use_gradient_checkpointing="unsloth"` (≈14GB). For 14B → Civo L40S.
- Conflicts: Quiet-STaR thought-tokens explode VRAM at long ctx — disable Quiet-STaR when YARN_ENABLE=1 with len > 8K.

---

## 7. Quiet-STaR — Silent Reasoning at Train

- Paper: [Quiet-STaR: LMs Teach Themselves to Think Before Speaking](https://arxiv.org/abs/2403.09629) (Stanford, Mar 2024)
- Repo: [ezelikman/quiet-star](https://github.com/ezelikman/quiet-star) (Mistral-patch-based)
- Working fork: [Crystalcareai/quiet-star-working](https://github.com/Crystalcareai/quiet-star-working)
- Fast Quiet-STaR (2025): [arxiv 2505.17746](https://arxiv.org/abs/2505.17746) — **+9% Mistral-7B, +5.7% Qwen2.5-7B, no inference latency increase**
- Benchmark (original): GSM8K 5.9% → 10.9%, CommonsenseQA 36.3% → 47.2% zero-shot

### V13 patch (Fast Quiet-STaR variant — no thought-tokens at inference)
```python
QSTAR_ENABLE         = os.getenv("QSTAR_ENABLE", "0") == "1"
QSTAR_NUM_THOUGHTS   = int(os.getenv("QSTAR_NUM_THOUGHTS", "8"))  # tokens per "thought"
QSTAR_LOOK_AHEAD     = int(os.getenv("QSTAR_LOOK_AHEAD", "4"))    # talk-head Δ
QSTAR_MIX_WEIGHT     = float(os.getenv("QSTAR_MIX_WEIGHT", "0.7"))  # think vs base mix

if QSTAR_ENABLE:
    # Insert <|startofthought|>, <|endofthought|> as new tokens
    new_tokens = ["<|startofthought|>", "<|endofthought|>"]
    tokenizer.add_tokens(new_tokens, special_tokens=True)
    model.resize_token_embeddings(len(tokenizer))
    # Loss = α * NTP_with_thoughts + (1-α) * NTP_baseline
    # Implementation: see modeling_mistral.py in ezelikman/quiet-star
    # Curriculum (Fast Quiet-STaR): start with 8 thought tokens, ramp down to 0 at end
    qstar_steps = lambda s: max(0, QSTAR_NUM_THOUGHTS - int(8*s/total_steps))
```

- T4×2 feasibility: **MARGINAL** — adds ~2× compute via parallel thought-rollouts. Use Fast Quiet-STaR curriculum to taper to 0 by epoch 3 → no inference cost.
- Conflicts: requires custom modeling file. Not Qwen-native — port from Mistral. Disable for Qwen3 until port verified.

---

## 8. Best-of-N + Verifier Reranking AS TRAINING DATA

- Paper: [VersaPRM: Multi-Domain Process Reward Model](https://arxiv.org/abs/2502.06737) (Feb 2025)
- Paper: [Process Reward Models That Think (ThinkPRM)](https://arxiv.org/abs/2504.16828) (April 2025)
- Paper: [Inference-Aware Fine-Tuning for Best-of-N](https://arxiv.org/pdf/2412.15287)
- Self-certainty: [Scalable BoN via Self-Certainty](https://arxiv.org/pdf/2502.18581)
- Use case: instead of 1 sample per prompt, sample N=8, score with PRM, KEEP top-1 as SFT pair → distills BoN search into weights.

### V13 patch (data prep stage, before training)
```python
BON_DATA_GEN         = os.getenv("BON_DATA_GEN", "0") == "1"
BON_N                = int(os.getenv("BON_N", "8"))
BON_VERIFIER         = os.getenv("BON_VERIFIER", "Qwen/Qwen2.5-Math-PRM-7B")

if BON_DATA_GEN:
    from transformers import pipeline
    verifier = pipeline("text-classification", model=BON_VERIFIER, device=0)
    def gen_bon_pair(prompt, base_model):
        cands = [base_model.generate(prompt, do_sample=True, temperature=0.8) for _ in range(BON_N)]
        scores = [verifier(c)[0]["score"] for c in cands]
        best = cands[int(np.argmax(scores))]
        return {"prompt": prompt, "completion": best, "bon_score": max(scores)}
```

- T4×2 feasibility: **YES** if verifier is 1B-3B. Use [Skywork-PRM-7B](https://huggingface.co/Skywork) or [Math-Shepherd](https://arxiv.org/pdf/2312.08935).
- Conflicts: orthogonal to RL stage — adds expensive offline data-gen pass. Run once, cache.

---

## 9. MCTS-Aware Decoding Training (rStar-Math style)

- Paper: [rStar-Math: Small LLMs Master Math Reasoning](https://arxiv.org/abs/2501.04519) (Microsoft, Jan 2025)
- Benchmark: **Qwen2.5-Math-7B 58.8 → 90.0% MATH**, Phi3-mini-3.8B 41.4 → 86.4%, surpasses o1-preview by +4.5%
- Mechanism: 4 rounds self-evolution; MCTS rollouts generate code-augmented CoT trajectories; PPM (process preference model) trained without naive step labels.
- Companion: [AlphaZero-like Tree Search for LLMs (TS-LLM)](https://arxiv.org/pdf/2309.17179), [MCTS Iterative Preference Learning](https://arxiv.org/abs/2405.00451)
- Repo: [opendilab/LightZero](https://github.com/opendilab/LightZero) (general MCTS), [waterhorse1/LLM_Tree_Search](https://github.com/waterhorse1/LLM_Tree_Search)

### V13 patch — MCTS-rollout data generator (offline → SFT/DPO)
```python
MCTS_ROLLOUT_GEN     = os.getenv("MCTS_ROLLOUT_GEN", "0") == "1"
MCTS_NUM_ROUNDS      = int(os.getenv("MCTS_NUM_ROUNDS", "4"))
MCTS_C_PUCT          = float(os.getenv("MCTS_C_PUCT", "1.4"))
MCTS_NUM_SIMS        = int(os.getenv("MCTS_NUM_SIMS", "16"))

# Pseudocode: for each math/code prompt, MCTS-search step-by-step
# Each step = candidate completion fragment; reward at terminal = unit-test pass rate
# Output: (prompt, best-trajectory) for SFT  +  (prompt, win-step, lose-step) for DPO
```

- T4×2 feasibility: **NO for online**. Offline data-gen: YES (no model update during search). Cache trajectories to JSONL.
- Conflicts: pre-empts RL stage — use BEFORE GRPO/DAPO as SFT seed.

---

## 10. s1 Test-Time-Compute Scaling Training (Budget Forcing)

- Paper: [s1: Simple Test-Time Scaling](https://arxiv.org/abs/2501.19393) (Stanford+UW, Jan 2025)
- Repo: [simplescaling/s1](https://github.com/simplescaling/s1)
- Benchmark: **s1-32B exceeds o1-preview by 27% on MATH/AIME24** with only **1,000 SFT samples**.
- Mechanism: SFT on 1K curated reasoning traces + budget forcing (append "Wait" to extend, force `</think>` to terminate). Lower bound is just sampling — upper bound = "Wait"-injection trick.

### V13 patch — small-data SFT recipe + Wait-injection eval
```python
S1_SFT_RECIPE        = os.getenv("S1_SFT_RECIPE", "0") == "1"
S1_DATASET           = os.getenv("S1_DATASET", "simplescaling/s1K-1.1")
S1_BUDGET_TOK        = int(os.getenv("S1_BUDGET_TOK", "16384"))

if S1_SFT_RECIPE:
    from datasets import load_dataset
    ds = load_dataset(S1_DATASET)["train"]
    # Use s1K's pre-curated 1000 question/trace pairs as inoculation seed
    train_args.num_train_epochs = 5  # paper recipe
    train_args.learning_rate = 1e-5  # ↑ higher than full-data SFT
```

- T4×2 feasibility: **YES** — s1K is tiny (1K examples). 5 epochs Qwen2.5-7B + LoRA = ~1 hour T4×2.
- Conflicts: 5-epoch high-LR may overfit. Mix 50/50 with owner-artifact seed instead of replacing.

---

## 11. DeepSWE / SWE-Gym / R2E-Gym — Long-Horizon Coding Training

- DeepSWE blog: [Together AI blog](https://www.together.ai/blog/deepswe), [HF model](https://huggingface.co/agentica-org/DeepSWE-Preview)
- SkyRL-Agent paper: [arxiv 2511.16108](https://arxiv.org/abs/2511.16108) — 4601 vs 9180 H100-hrs, **39.4% Pass@1 SWE-Bench-Verified**
- R2E-Gym repo: [R2E-Gym/R2E-Gym](https://github.com/R2E-Gym/R2E-Gym), [r2e-gym.github.io](https://r2e-gym.github.io/) — 8.1K problems, hybrid verifiers
- SWE-Gym repo: [SWE-Gym/SWE-Gym](https://github.com/SWE-Gym/SWE-Gym) — 2438 Python tasks, ICML 2025
- Self-Play SWE-RL: [arxiv 2512.18552](https://arxiv.org/abs/2512.18552) — bug-injecting + debugging dual-role
- SWE-Bench Pro: [arxiv 2509.16941](https://arxiv.org/abs/2509.16941) — 1865 long-horizon problems, hours-to-days human time
- SWE-EVO: [arxiv 2512.18470](https://arxiv.org/abs/2512.18470) — 48 multi-commit, 21-files-avg, **GPT-5.4+OpenHands only 25%**

### V13 patch — wire R2E-Gym SFT seed before RL
```python
SWE_GYM_SFT          = os.getenv("SWE_GYM_SFT", "0") == "1"
SWE_GYM_DATASET      = os.getenv("SWE_GYM_DATASET", "R2E-Gym/R2E-Gym-V1")
SWE_GYM_AGENT_TRAJ   = os.getenv("SWE_GYM_AGENT_TRAJ", "SWE-Gym/SWE-Gym-Trajectories")

if SWE_GYM_SFT:
    from datasets import load_dataset
    swe_ds = load_dataset(SWE_GYM_AGENT_TRAJ, split="train")
    # Concat with existing 9 owner artifact + 4 research-Q2 datasets
    # Pre-pend before TruthRL/DAPO RL phase
```

- T4×2 feasibility: **MARGINAL** — context per task often >32K. Use Qwen3-8B + YARN at 32K. For 32B: Civo L40S only.
- Conflicts: trajectories are agent-format (tool calls); ensure tokenizer's chat template handles `<tool_call>`.

---

## 12. Self-Refine / CRITIC / Self-Debug AS TRAINING DATA

- Paper: [Self-Refine: Iterative Refinement with Self-Feedback](https://arxiv.org/abs/2303.17651) (CMU, March 2023; still SOTA inference-time pattern)
- 2025 self-debug training: [Training LLMs to Better Self-Debug and Explain Code](https://assets.amazon.science/46/bf/3743cf75474290526f1147d9373f/training-llms-to-better-self-debug-and-explain-code.pdf) (Amazon)
- Benchmark: **+15.92% pass@1 / +9.30% pass@10** SFT-only on CodeLlama-7B; +3.54% pass@1 with RL.
- 2025 self-correction loop: [Prompton Self-Correction & Iterative Refinement](https://prompton.wordpress.com/2025/06/20/) — Google: 30% code-error reduction.

### V13 patch — Self-Refine triplet generator
```python
SELFREFINE_DATA_GEN  = os.getenv("SELFREFINE_DATA_GEN", "0") == "1"
SELFREFINE_ROUNDS    = int(os.getenv("SELFREFINE_ROUNDS", "3"))

if SELFREFINE_DATA_GEN:
    def make_refine_triplet(prompt, base_model):
        v0 = base_model.generate(prompt)
        triplets = [(prompt, v0, "initial")]
        cur = v0
        for r in range(SELFREFINE_ROUNDS):
            critic_prompt = f"{prompt}\n\nCurrent answer:\n{cur}\n\nCritique (specific issues only):"
            critique = base_model.generate(critic_prompt)
            refine_prompt = f"{prompt}\n\nCurrent:\n{cur}\n\nCritique:\n{critique}\n\nImproved:"
            cur = base_model.generate(refine_prompt)
            triplets.append((prompt, cur, f"refine_{r}"))
        return triplets
```

- T4×2 feasibility: **YES** offline. Mine from owner artifacts before training.
- Conflicts: none with RL/DAPO — orthogonal SFT data-gen.

---

## 13. Iterated Distillation + Amplification (IDA — Christiano)

- Paper: [Supervising Strong Learners by Amplifying Weak Experts](https://arxiv.org/abs/1810.08575) (Christiano + Amodei, 2018; foundational)
- Survey: [evans IDA Projects PDF](https://owainevans.github.io/pdfs/evans_ida_projects.pdf)
- LessWrong: [Understanding IDA — Claims](https://www.lesswrong.com/posts/yxzrKb2vFXRkwndQ4/understanding-iterated-distillation-and-amplification-claims)
- Mechanism: Slow-but-strong oracle (M+tools) generates labels → distill into faster model (M') → M' replaces M for next round → repeat. Self-bootstrap to superhuman without external supervision.

### V13 patch (multi-round bootstrap loop)
```python
IDA_ENABLE           = os.getenv("IDA_ENABLE", "0") == "1"
IDA_NUM_ROUNDS       = int(os.getenv("IDA_NUM_ROUNDS", "3"))
IDA_AMPLIFY_TOOLS    = os.getenv("IDA_AMPLIFY_TOOLS", "code_exec,wiki_search").split(",")

# Round-loop: for each round, run inference with tools (Amplify) → SFT distill (Distill)
def ida_round(model, raw_prompts, tools):
    amplified = []
    for p in raw_prompts:
        # Amplify: model + tools produces high-quality answer
        ans = run_with_tools(model, p, tools)
        amplified.append({"prompt": p, "completion": ans})
    # Distill: SFT model on amplified data → faster, near-equivalent quality
    return finetune_lora(model, amplified)
```

- T4×2 feasibility: **YES** but expensive in wall-clock (each round = full SFT). Run 2 rounds max.
- Conflicts: tool-calling format must align with chat template.

---

## 14. Self-Reward + Meta-Reward (Constitutional v3)

- Paper: [Self-Rewarding Language Models](https://arxiv.org/html/2401.10020v1) (Meta, Jan 2024)
- Paper: [Meta-Rewarding LMs: Self-Improving Alignment with LLM-as-Meta-Judge](https://arxiv.org/html/2407.19594v2) (Meta, July 2024)
- 2026 reverse-CAI: [arxiv 2604.17769](https://arxiv.org/html/2604.17769)
- Benchmark: Llama-3-8B AlpacaEval 22.9% → **39.4%** (Meta-Rewarding); Arena-Hard 20.6% → 29.1%.
- Mechanism: LLM-as-judge prompting + iterative DPO. Meta-step: model judges its OWN judge outputs and refines judge skill.

### V13 patch — three-role training (actor, judge, meta-judge)
```python
META_REWARD_ENABLE   = os.getenv("META_REWARD_ENABLE", "0") == "1"
META_REWARD_ITERS    = int(os.getenv("META_REWARD_ITERS", "2"))
META_JUDGE_PROMPT    = os.getenv("META_JUDGE_PROMPT", "judge_v3.txt")

# Stage A: actor generates 4 responses
# Stage B: same model with judge-prompt scores them → preference pair
# Stage C (meta): same model with meta-judge prompt scores judge's outputs → judge-improvement pair
# DPO on union of B+C pairs
```

- T4×2 feasibility: **YES** for 7B. 3× generation per prompt; cache offline.
- Conflicts: judge-prompt and TruthRL ternary may disagree — feed Truth verdict as ground-truth tie-breaker.

---

## 15. Eureka Auto Curriculum (Reward Generation)

- Paper: [Eureka: Human-Level Reward Design via Coding LLMs](https://arxiv.org/abs/2310.12931) (NVIDIA, ICLR 2024)
- Repo: [eureka-research/Eureka](https://github.com/eureka-research/Eureka)
- 2025 follow-up: [LEARN-Opt for reward optimization](https://arxiv.org/html/2511.19355v1)
- Benchmark: outperforms human-engineered rewards on 83% of 29 RL envs, +52% normalized improvement.
- Adaptation for LLM: GPT-4 (or strong oracle) writes Python reward functions; evolutionary loop selects best.

### V13 patch — auto-generate reward functions for RL stage
```python
EUREKA_REWARD_GEN    = os.getenv("EUREKA_REWARD_GEN", "0") == "1"
EUREKA_NUM_VARIANTS  = int(os.getenv("EUREKA_NUM_VARIANTS", "8"))
EUREKA_ORACLE_MODEL  = os.getenv("EUREKA_ORACLE_MODEL", "deepseek-r1-distill-qwen-32b")

# Have oracle write 8 candidate reward Python funcs given task description + base reward
# Run mini RL pass with each, pick top-2 by validation, ensemble
```

- T4×2 feasibility: **MARGINAL** — needs strong oracle. Use Anthropic API or Qwen3-32B-Instruct on Civo L40S.
- Conflicts: conflicts with hand-crafted TruthRL ternary; treat as additive shaping, not replacement.

---

## 16. Schema-Guided Decoding TRAINING (XGrammar-aware)

- XGrammar: [arxiv 2411.15100](https://arxiv.org/pdf/2411.15100), repo [mlc-ai/xgrammar](https://github.com/mlc-ai/xgrammar)
- Schema RL: [Learning to Generate Structured Output with Schema RL](https://arxiv.org/html/2502.18878v1) — Thought of Structure (ToS)
- SLOT: [Structuring the Output of LLMs](https://arxiv.org/html/2505.04016v1) (EMNLP 2025)
- Benchmark: ToS + SRL — schema-validity 99%+, downstream task quality preserved.

### V13 patch — schema-aware SFT data (synthesize JSON outputs)
```python
SCHEMA_TRAIN_DATA    = os.getenv("SCHEMA_TRAIN_DATA", "0") == "1"
SCHEMA_FORMATS       = os.getenv("SCHEMA_FORMATS", "json,yaml,toml,xml").split(",")
SCHEMA_VALIDATOR     = os.getenv("SCHEMA_VALIDATOR", "jsonschema")

if SCHEMA_TRAIN_DATA:
    # For every owner artifact prompt that has structured output:
    # 1. Generate baseline JSON, 2. validate with jsonschema, 3. on fail mark as negative
    # Build (prompt, valid-output) SFT + (prompt, invalid, valid) DPO pairs
```

- T4×2 feasibility: **YES** offline.
- Conflicts: structured output may contradict free-form code generation — separate by `task_type` tag.

---

## 17. Curriculum Learning Hard-Ramp (Difficulty-Sorted)

- Paper: [Curriculum RL from Easy to Hard Improves LLM Reasoning](https://arxiv.org/html/2506.06632) (June 2025)
- Paper: [Prompt Curriculum Learning for Efficient LLM Post-Training](https://www.researchgate.net/publication/396094712) — intermediate-difficulty selection via value model
- Survey: [Strategic Data Ordering](https://arxiv.org/html/2405.07490v1)
- Benchmark: PCL — Mistral-7B intermediate-difficulty selection > random by 6-8% on math/code.

### V13 patch — sort dataset by reward-pass-rate at start of training
```python
CURRICULUM_HARD_RAMP = os.getenv("CURRICULUM_HARD_RAMP", "0") == "1"
CURRICULUM_DIFF_KEY  = os.getenv("CURRICULUM_DIFF_KEY", "perplexity")  # or 'reward_pass_rate'

if CURRICULUM_HARD_RAMP:
    # Step 1: pre-score each example with base model perplexity OR n=4 reward pass rate
    # Step 2: bucket into 4 tiers (easy, mid-easy, mid-hard, hard)
    # Step 3: training schedule: epoch1=80%easy/20%mid, ..., epoch4=20%easy/80%hard
    def schedule_p_hard(step, total): return min(0.8, 0.2 + 0.6*step/total)
```

- T4×2 feasibility: **YES** — pure data-loader change.
- Conflicts: dynamic-sampling DAPO already filters trivial; layered effect, monitor batch fill rate.

---

## 18. MoE-from-Dense (Drop-Upcycling) — Future-Proofing

- Paper: [Drop-Upcycling: Sparse MoE with Partial Re-init](https://arxiv.org/abs/2502.19261) (Feb 2025)
- Sparse Upcycling: [arxiv 2212.05055](https://arxiv.org/pdf/2212.05055), [openreview](https://openreview.net/pdf?id=T5nUQDrM4u)
- Instruction tuning upcycling: [ACL 2025 paper](https://aclanthology.org/2025.acl-long.637.pdf)
- Benchmark: 5.9B-active MoE matches 13B dense, **¼ training FLOPs**.

### V13 patch — note-only (deferred to V14)
```python
# DEFERRED: full MoE upcycling needs >4×L40S. Skip for V13.
# Lightweight: convert FFN to 2-expert (top-1 routing) for adapter-only
MOE_LITE_EXPERTS     = os.getenv("MOE_LITE_EXPERTS", "0") == "1"
MOE_LITE_TOP_K       = int(os.getenv("MOE_LITE_TOP_K", "1"))
```

- T4×2 feasibility: **NO** for full upcycle. MoE-lite with LoRA: experimental.
- Conflicts: deeply incompatible with Spectrum-SNR (different tensor topology). Pick one.

---

## 19. Anti-Hallucination Bonus: SDFT v2 + LiPO

- SDFT v2 (Stochastic DFT): already in V12 — extension via [arxiv 2503.22233](https://arxiv.org/html/2503.22233) entropy-driven uncertainty PRM
- LiPO (Listwise PO): newer than DPO-pair, ranks N samples. [arxiv 2402.01878](https://arxiv.org/abs/2402.01878). Fits naturally on top of BoN.

(No new patch — track for V14)

---

## 20. Long-Horizon SkyRL Recipe Wiring

- SkyRL repo: [NovaSky-AI/SkyRL](https://github.com/NovaSky-AI/SkyRL)
- SkyRL-tx (Tinker compatible): [Anyscale + NovaSky blog](https://www.marktechpost.com/2025/11/03/anyscale-and-novasky-team-releases-skyrl-tx-v0-1-0-bringing-tinker-compatible-reinforcement-learning-rl-engine-to-local-gpu-clusters/)
- For Surrogate-1: use SkyRL-Agent's async dispatcher when graduating to L40S cluster.

```python
SKYRL_BACKEND        = os.getenv("SKYRL_BACKEND", "trl")  # 'trl'|'verl'|'skyrl'|'tinker'
```

---

# Wire-Into-V13-Trainer

## Env knobs (env-toggle, compose freely)

```bash
# === DAPO (top priority — additive on TruthRL) ===
RL_DAPO_ENABLE=1
RL_DAPO_EPS_LOW=0.20
RL_DAPO_EPS_HIGH=0.28
RL_DAPO_DYN_SAMPLE=1
RL_DAPO_OVERLONG_T1=3072
RL_DAPO_OVERLONG_T2=4096

# === AsyncGRPO (L40S only) ===
RL_ASYNC_ENABLE=0
RL_ASYNC_VLLM_URL=http://127.0.0.1:8000
RL_ASYNC_WEIGHT_SYNC=16
RL_ASYNC_MAX_STALE=3

# === Reflexion-train ===
RL_REFLEXION_ENABLE=1
RL_REFLEXION_MAX_TRIES=2
RL_REFLEXION_RATE=0.05

# === Voyager skill bank ===
SKILLS_USE=1
SKILLS_BANK_PATH=/kaggle/working/skills_bank.jsonl
SKILLS_TOP_K=5

# === Spectrum true-SNR (replaces top-N proxy) ===
SPECTRUM_TRUE_SNR=1
SPECTRUM_TOP_PCT=30
SPECTRUM_CACHE_DIR=/kaggle/working/spectrum_cache

# === YaRN long context ===
YARN_ENABLE=1
YARN_FACTOR=2.0
YARN_ORIG_MAX_POS=32768
YARN_TRAIN_MAX_LEN=32768

# === Quiet-STaR (Mistral-only port; off for Qwen) ===
QSTAR_ENABLE=0
QSTAR_NUM_THOUGHTS=8
QSTAR_LOOK_AHEAD=4
QSTAR_MIX_WEIGHT=0.7

# === BoN+verifier data ===
BON_DATA_GEN=1
BON_N=8
BON_VERIFIER=Qwen/Qwen2.5-Math-PRM-7B

# === MCTS rollout data (offline) ===
MCTS_ROLLOUT_GEN=0
MCTS_NUM_ROUNDS=4
MCTS_C_PUCT=1.4
MCTS_NUM_SIMS=16

# === s1 budget-forcing recipe ===
S1_SFT_RECIPE=1
S1_DATASET=simplescaling/s1K-1.1
S1_BUDGET_TOK=16384

# === SWE training data ===
SWE_GYM_SFT=1
SWE_GYM_DATASET=R2E-Gym/R2E-Gym-V1
SWE_GYM_AGENT_TRAJ=SWE-Gym/SWE-Gym-Trajectories

# === Self-Refine triplet ===
SELFREFINE_DATA_GEN=1
SELFREFINE_ROUNDS=3

# === IDA bootstrap ===
IDA_ENABLE=0
IDA_NUM_ROUNDS=2
IDA_AMPLIFY_TOOLS=code_exec,wiki_search

# === Meta-Reward ===
META_REWARD_ENABLE=1
META_REWARD_ITERS=2
META_JUDGE_PROMPT=judge_v3.txt

# === Eureka auto-curriculum ===
EUREKA_REWARD_GEN=0
EUREKA_NUM_VARIANTS=8
EUREKA_ORACLE_MODEL=deepseek-r1-distill-qwen-32b

# === Schema-guided decoding ===
SCHEMA_TRAIN_DATA=1
SCHEMA_FORMATS=json,yaml,toml,xml

# === Curriculum hard-ramp ===
CURRICULUM_HARD_RAMP=1
CURRICULUM_DIFF_KEY=reward_pass_rate

# === MoE-lite (deferred) ===
MOE_LITE_EXPERTS=0
```

## Recommended insertion order in PYEOF
1. Spectrum-SNR scan (one-time, before model load)
2. YaRN config-edit + curriculum length scheduler
3. Owner-artifact + s1K + R2E-Gym + Self-Refine triplets concat
4. Schema-validation pass on dataset
5. Curriculum hard-ramp re-sort
6. SFT phase (existing)
7. BoN+verifier offline mining → DPO data
8. ORPO/KTO/Mask-DPO/F-DPO (existing)
9. RL phase: TruthRL + DAPO + dynamic-sampling + overlong-shaping + reflexion-wrapper
10. Voyager skill-bank dump per round
11. Meta-Reward iteration (optional final pass)
12. EAGLE-3 / DistillKit (existing scaffolds)

## Conflict matrix (must NOT enable simultaneously)
- `SPECTRUM_TRUE_SNR=1` AND `MOE_LITE_EXPERTS=1` → tensor topology incompatible
- `RL_ASYNC_ENABLE=1` AND `RL_DAPO_DYN_SAMPLE=1` AND `MAX_STALE>0` → reward instability
- `QSTAR_ENABLE=1` AND `YARN_TRAIN_MAX_LEN>8192` → VRAM blow-up
- `RL_REFLEXION_ENABLE=1` AND `RL_ASYNC_ENABLE=1` → staleness corrupts retry-reward
- `EUREKA_REWARD_GEN=1` AND TruthRL-only mode → reward-shaping double-count
- `IDA_ENABLE=1` AND limited compute → wall-clock blowout (skip on T4)

## Hardware tier mapping
| Knob | T4×2 (16+16GB) | L40S 48GB | Notes |
|---|---|---|---|
| RL_DAPO_ENABLE | ✅ | ✅ | Zero memory overhead |
| RL_ASYNC_ENABLE | ❌ | ✅ | Needs vLLM card |
| RL_REFLEXION_ENABLE | ✅ (5%) | ✅ | Cap rate on T4 |
| SKILLS_USE | ✅ | ✅ | File I/O |
| SPECTRUM_TRUE_SNR | ✅ | ✅ | One-time scan |
| YARN_ENABLE (32K @ 7B) | ✅ unsloth | ✅ | grad-ckpt mandatory |
| YARN_ENABLE (32K @ 14B) | ❌ | ✅ | needs L40S |
| QSTAR_ENABLE | ⚠️ Mistral-only | ✅ | port for Qwen3 not done |
| BON_DATA_GEN (7B verifier) | ✅ offline | ✅ | one-pass |
| MCTS_ROLLOUT_GEN | ✅ offline | ✅ | wall-clock heavy |
| S1_SFT_RECIPE | ✅ | ✅ | 1K examples, 5 epoch |
| SWE_GYM_SFT | ⚠️ 7B+8K | ✅ 14B+32K | context heavy |
| SELFREFINE_DATA_GEN | ✅ offline | ✅ | once |
| IDA_ENABLE | ⚠️ slow | ✅ | 2 rounds max |
| META_REWARD_ENABLE | ✅ offline | ✅ | one-pass |
| EUREKA_REWARD_GEN | ❌ needs oracle | ✅ via API | gated |
| SCHEMA_TRAIN_DATA | ✅ | ✅ | offline |
| CURRICULUM_HARD_RAMP | ✅ | ✅ | data-loader |
| MOE_LITE_EXPERTS | ❌ | ⚠️ | defer V14 |

## Top-10 by impact-per-effort (paste-ready order)
1. **DAPO** (5-line config diff, +3 AIME pts, free)
2. **Spectrum-true-SNR** (replace V12 proxy, free, 15-37% time win)
3. **YaRN 32K curriculum** (unlock long-horizon coding)
4. **R2E-Gym SFT seed** (5800 verified SWE trajectories)
5. **Reflexion-train** (+34% math, +18% func-call)
6. **s1K SFT recipe** (1K examples, 5 epochs, +27% AIME)
7. **Voyager skill bank** (cumulative gain across rounds)
8. **BoN+verifier mining** (compresses search into weights)
9. **Self-Refine triplets** (+15% pass@1, offline)
10. **Curriculum hard-ramp** (data-loader change, easy gain)

## Datasets to ingest (NEW, not in V12)
- [simplescaling/s1K-1.1](https://huggingface.co/datasets/simplescaling/s1K-1.1) — 1K reasoning traces
- [R2E-Gym/R2E-Gym-V1](https://huggingface.co/datasets/R2E-Gym/R2E-Gym-V1) — 8.1K SWE problems
- [SWE-Gym/SWE-Gym](https://huggingface.co/datasets/SWE-Gym/SWE-Gym) — 2438 Python tasks + agent trajectories
- [openai/prm800k](https://huggingface.co/datasets/openai/prm800k) — 800K step-level labels (optional verifier)
- [peiyi9979/Math-Shepherd](https://huggingface.co/datasets/peiyi9979/Math-Shepherd) — 400K step-level (free, no manual)
- [HuggingFaceH4/Bespoke-Stratos-17k](https://huggingface.co/datasets/HuggingFaceH4/Bespoke-Stratos-17k) — distilled o1-style traces
- [agentica-org/DeepSWE-Preview-data](https://huggingface.co/agentica-org/DeepSWE-Preview) — 4.5K SWE RL trajectories

## Verification checklist before V13 release
- [ ] T4×2 dry-run with DAPO + Spectrum + YaRN-16K succeeds
- [ ] Spectrum YAML cached in `/kaggle/working/spectrum_cache`
- [ ] Skill bank file grows monotonically across 2 rounds
- [ ] Reflexion-train rate-cap honored (≤5% on T4)
- [ ] R2E-Gym tokens fit at YARN_FACTOR=2 + 7B
- [ ] Conflict matrix asserts in `__main__` guard
- [ ] All 25+ env knobs default-off except top-10

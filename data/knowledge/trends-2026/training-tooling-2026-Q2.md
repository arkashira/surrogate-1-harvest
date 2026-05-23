---
tags: [trends-2026, surrogate-1, training, github-repos, sft, dpo, grpo, peft, distillation, swe-agent]
created: 2026-05-01
updated: 2026-05-01
purpose: ingest into Surrogate-1 V10 trainer (kaggle-trainer.sh / civo-trainer)
horizon: last 60-90 days, repos with shipped commits between 2026-02-01 and 2026-04-30
status: research-complete, action-items at bottom
related:
  - "[[surrogate-1-v10-spec]]"
  - "[[surrogate-1-v9-spec]]"
  - "[[surrogate-latest-improvements-2026]]"
  - "[[trends-2026/coding-llm-frontier]]"
  - "[[trends-2026/devsecops-sre-agentic]]"
---

# Training-Tooling 2026 Q2 — Active Repos to Wire Into Surrogate-1

> Research date 2026-05-01. Scope: training-side tooling that we **ingest into Surrogate's
> SFT/preference/RL pipeline**, not external SaaS. Each entry: repo URL, last meaningful
> commit, what it does, how to wire into our `kaggle-trainer.sh` / `civo-trainer`,
> compute fit (T4×2 16+16 GB Kaggle vs L40S 48 GB Civo), public dataset (HF link +
> license), and reproducibility status (does the repo include working training code?).
>
> Quick legend per row: SFT-only / Pref-data / RL / Distill / Verifier / SpecDecode.

---

## A. Trainer Frameworks (the workhorses)

### 1. TRL (HuggingFace) — `huggingface/trl`

- Repo: <https://github.com/huggingface/trl> · Releases: <https://github.com/huggingface/trl/releases>
- Last meaningful tag: **v1.3.0 (2026-04-26)**, v1.2.0 (2026-04-17), v1.1.0 (2026-04-12), v1.0.0 (2026-03-31).
- **What v1.0.0+ shipped that V9 trainer must absorb**:
  - `AsyncGRPOTrainer` (vLLM-backed rollout, 2-4× wall-clock vs sync GRPO on 7B–32B).
  - `BFD packing` 35 % faster (best-fit decreasing); rename `bfd-requeue` → `bfd_split`.
  - **`SSDTrainer`** (Embarrassingly Simple Self-Distillation) — sample temp=high → SFT on own outputs. No reward model needed. SFT-feasible-only.
  - **`DistillationTrainer`** — on-policy distillation, generation buffer (40× speedup), external teacher server, binary log-prob payloads (~5× transfer compression).
  - **`SDPO`** (Self-Distillation Preference Optimization) — uses self-rollouts as both chosen and rejected.
  - **`TPO`** (Triple Preference Optimization) experimental — adds neutral pair to DPO triple.
  - `VESPO` (Variational Sequence-Level Soft PPO) loss type for GRPO.
  - `DPPO` (Divergence-Proximal PPO) experimental.
  - GRPO: `pad_to_multiple_of`, `use_transformers_paged` deprecated (paged path 20 % slower, 6× peak VRAM — **remove from our config**).
  - Speculative decoding in `trl vllm-serve` (drop-in for GRPO rollout).
- **Wire-in**: bump `requirements: trl>=1.3.0,<1.4.0` (was `>=0.12.0,<0.16.0`). Replace our v1.2 GRPO scaffold with `AsyncGRPOTrainer` for any Civo L40S run; T4×2 keeps sync GRPO. Add `RUN_DISTILL=1` env-knob → run `DistillationTrainer` after SFT phase using a teacher hosted on Cerebras/Groq via OpenAI-compat URL (no local teacher VRAM cost).
- **Compute**: TRL itself is plumbing — fits anywhere. AsyncGRPO needs vLLM server on a separate process; Kaggle T4×2 is borderline (one card serves vLLM, one card trains). Civo L40S 48 GB → comfortable.
- **Public datasets**: none (framework only).
- **Reproducibility**: `examples/scripts/{sft,dpo,grpo,distillation,sdpo}.py` all runnable with `--dataset_name`. Steal directly.
- Citations: <https://github.com/huggingface/trl/releases/tag/v1.0.0> · <https://huggingface.co/docs/trl/v1.0.0/en/sdft_trainer> · <https://huggingface.co/docs/trl/v1.3.0/en/distillation_trainer>

### 2. PEFT (HuggingFace) — `huggingface/peft`

- Repo: <https://github.com/huggingface/peft> · Releases: <https://github.com/huggingface/peft/releases>
- Last meaningful tag: **v0.19.0 (2026-04-14)**, v0.18.0 (2025-11-13), v0.17.0 (2025-08-01).
- **What v0.18 / v0.19 shipped (what we already use vs. what's new)**:
  - **Already in trainer**: PiSSA (`pissa_niter_4`), OLoRA, CorDA, LoftQ, DoRA, LoRA+. Keep.
  - **NEW v0.18 to consider**: RoAd (2D rotations — cheap rotational adapter), ALoRA (selective activation), Arrow (dynamic routing across LoRAs), WaveFT (wavelet domain adapter), DeLoRA (decoupled adaptation), OSF (orthogonal fine-tuning).
  - **NEW v0.19 to consider**: GraLoRA, BD-LoRA, Cartridges, PVeRA, PSOFT, Lily, PEANuT, TinyLoRA, AdaMSS — 9 new methods.
  - **NEW v0.19 utilities**: `convert non-LoRA → LoRA` (lets us merge BoNE/IA3 adapters into the LoRA stack), **LoRA-GA initialization** (gradient-aligned init — competes with PiSSA), Intruder-Dimension reduction (fights catastrophic forgetting in continual SFT), Transformer Engine + Tensor Parallel support (Civo multi-GPU).
- **Wire-in**: bump `peft>=0.19.0,<0.21.0`. Add new env-knob `SUR_LORA_INIT=lora_ga` to ladder our 4-init bake-off (loftq · pissa_niter_4 · loftq+pissa · lora_ga). Add Intruder-Dim reduction call before any continual round (we plan continual SFT R1→R5 in V10).
- **Compute**: free, all 4-bit-compatible.
- **Public datasets**: none.
- **Reproducibility**: `examples/sft/lora_ga.py`, `examples/lora_dora/run.py` — drop-in.
- Citations: <https://github.com/huggingface/peft/releases/tag/v0.19.0> · <https://huggingface.co/docs/peft/v0.19.0/en/developer_guides/lora>

### 3. Unsloth — `unslothai/unsloth`

- Repo: <https://github.com/unslothai/unsloth> · Docs: <https://unsloth.ai/docs/new/changelog>
- Last meaningful tag: **v0.1.37-beta (2026-04-23)**, v0.1.36-beta (2026-04-08), v0.1.35-beta (2026-04-02).
- **2026-Q2 firepower**:
  - **MoE 12× faster + 35 % less VRAM + 6× longer context** via new Triton + math kernels (no accuracy loss). gpt-oss-20B trains in 12.8 GB; Qwen3-30B-A3B 16-bit LoRA fits 63 GB.
  - **RL long-context: 7-12× longer context** at no accuracy or speed cost. gpt-oss QLoRA 380K context on single B200. T4 still gets ≥3× context boost.
  - **3× faster SFT** with kernels + packing combined.
  - Embedding/BERT/classifier training 1.8-3.3× faster, 20 % less VRAM, 2× longer context (HF collab).
  - Architecture-aware KV-cache VRAM estimation (5-path) — fewer OOMs on Kaggle.
  - Multi-GPU training/inference via `device_map="balanced_low0"` or `"sequential"`.
  - +50 % tool-call accuracy (chat template fix in v0.1.3-beta).
- **Wire-in**: optional alternative to TRL on Kaggle. Offers `FastLanguageModel.from_pretrained(...)` with built-in 4-bit + LoRA + Triton — replaces our `bnb + peft` stack with one call. **Strategy**: keep TRL for GRPO phase; switch SFT phase to Unsloth on Kaggle for the 3× speedup. Civo can stay TRL-only.
- **Compute**: best-in-class for T4 16 GB. 7B QLoRA fits. 14B QLoRA tight but works.
- **Public datasets**: none.
- **Reproducibility**: official Kaggle/Colab notebooks per model — `unsloth/notebooks` repo.
- Citations: <https://github.com/unslothai/unsloth/releases> · <https://unsloth.ai/docs/new/grpo-long-context> · <https://docs.unsloth.ai/new/3x-faster-training-packing>

### 4. Axolotl — `axolotl-ai-cloud/axolotl`

- Repo: <https://github.com/axolotl-ai-cloud/axolotl> · Releases: <https://github.com/axolotl-ai-cloud/axolotl/releases>
- Last meaningful tag: **v0.16.1 (2026-04-02)**, v0.16.0 (2026-04-02), v0.15.0 (2026-03-06).
- **v0.15-v0.16 highlights**:
  - **Async GRPO + vLLM** = 58 % faster step times.
  - **ScatterMoE + LoRA fused Triton kernels** = 15× faster forward pass on MoE.
  - **SonicMoE LoRA** — Hopper/Blackwell only (Civo L40S = Ada Lovelace, partial fit).
  - Flash Attention 4 (graceful fallback to FA2 on Ampere).
  - **NeMo Gym integration** — single-turn and multi-turn RL training.
  - **Energy-Based Fine-Tuning (EBFT)** — novel RL method, smaller variance than GRPO on math.
  - **MX Quantization-Aware Training** (FP8/MXFP4 native).
  - CPU layer offloading for LoRA — extends usable batch size.
  - Custom optimizers: Flash AdamW, Optimi, ADOPT, **Muon**, Dion. (Muon = matrix-aware momentum, 1.5× faster convergence on math reasoning per published benches.)
  - PyTorch 2.10 + uv-based Docker.
- **Wire-in**: keep Axolotl as the **alt-trainer for Civo L40S**. Our `civo-trainer` should use axolotl YAML (single config file, easier than our Python script). Specifically use `optim: muon` for the 14B SFT phase, `rl: grpo` async for the post-SFT phase. Kaggle stays on raw TRL — axolotl Docker is too heavy for Kaggle session lifecycle.
- **Compute**: needs Docker. L40S 48 GB → 14B QLoRA + GRPO comfortable. T4×2 → not recommended.
- **Public datasets**: none (framework).
- **Reproducibility**: `examples/qwen2_5_coder/qlora.yml`, `examples/grpo/qwen.yml`. Drop-in YAML.
- Citations: <https://github.com/axolotl-ai-cloud/axolotl/releases/tag/v0.16.0> · <https://docs.axolotl.ai/docs/grpo.html>

### 5. LLaMA-Factory — `hiyouga/LlamaFactory`

- Repo: <https://github.com/hiyouga/LlamaFactory> · 70.6K stars by 2026-Q2.
- Last meaningful commit cadence: weekly. EasyR1 multimodal-GRPO sub-project announced 2026-02-24.
- **What's interesting for Surrogate**:
  - 100+ model presets (Qwen2.5/3.5/3.6, GLM, Hunyuan, DeepSeek, Mistral, Llama 3.x, gpt-oss, Gemma 4, MoE families).
  - PPO / DPO / KTO / ORPO / GRPO / reward-model trainers in single CLI.
  - LLAMABOARD web UI — useful for non-coders, irrelevant for us.
  - **NVIDIA DGX Spark playbook (2026-02)** — official PyTorch CUDA 13 LoRA/QLoRA/full-FT recipes.
- **Wire-in**: **don't replace TRL stack**, but mine their `examples/train_lora/` YAML for hyperparameter ladders. Specifically `qwen2_5_coder_7b_lora_sft.yaml` and `llama3_grpo.yaml` — copy LR/warmup/scheduler shapes to our trainer.
- **Compute**: identical to axolotl.
- **Public datasets**: none.
- **Reproducibility**: 50+ ready YAML files in `examples/`.
- Citations: <https://github.com/hiyouga/LlamaFactory> · <https://llamafactory.readthedocs.io/en/latest/>

### 6. OpenRLHF — `OpenRLHF/OpenRLHF`

- Repo: <https://github.com/OpenRLHF/OpenRLHF> · Docs: <https://openrlhf.readthedocs.io/>
- Last meaningful tag: **0.10.x (2026-Q2)**.
- **0.10 highlights**:
  - **Multi-Turn VLM RL** — multi-step interactions with images in prompts AND environment feedback (e.g. screenshot-based agentic loops).
  - **Async training + Partial Rollout** — overlap rollout with training, vLLM pause/resume for weight sync.
  - PPO / REINFORCE++ / REINFORCE++-baseline / GRPO / RLOO / **DAPO** / TIS — all decoupled from agent execution mode (single-turn, multi-turn, async, agentic).
  - Ray-based scaling — multi-node Civo cluster ready.
- **Wire-in**: **future V11 candidate**, not V10. Our V10 stays on TRL because GRPO-async in TRL v1.0 is simpler. Keep OpenRLHF on shortlist for when we need multi-turn agentic RL with screenshots (Surrogate desktop-control role).
- **Compute**: requires Ray cluster — overkill for single L40S.
- **Public datasets**: none.
- **Reproducibility**: `examples/train_grpo_*.sh` shell scripts — runnable.
- Citations: <https://github.com/OpenRLHF/OpenRLHF> · <https://huggingface.co/blog/async-rl-training-landscape>

### 7. veRL — `verl-project/verl` (formerly volcengine/verl)

- Repo: <https://github.com/verl-project/verl> · <https://github.com/volcengine/verl>
- Last meaningful: actively merging through 2026-Q2; recipe directory migrated to `verl-recipe` submodule (PR #4795).
- **Algorithms**: PPO, GRPO, **GSPO**, ReMax, REINFORCE++, RLOO, **PRIME**, **DAPO**, **DrGRPO**, KL_Cov, Clip_Cov.
- Hybrid programming model (single + multi-controller). Backed by FSDP + Megatron-LM + vLLM.
- **2026 PyTorch Conf Europe**: Megatron LoRA + router-replay support showcased.
- **Wire-in**: **alt to OpenRLHF for multi-node future scenarios**. Worth tracking but not V10. The DrGRPO loss is the reference impl — useful to crib for our trainer if `trl.GRPOConfig.loss_type="dr_grpo"` ever ships.
- **Compute**: same as OpenRLHF — needs Ray.
- **Reproducibility**: `recipes/` submodule has runnable configs.
- Citations: <https://github.com/verl-project/verl>

---

## B. Software-Engineering Agent Training (SWE-* gym repos)

### 8. SWE-smith — `SWE-bench/SWE-smith` (NeurIPS 2025 D&B Spotlight)

- Repo: <https://github.com/SWE-bench/SWE-smith> · License: **MIT**.
- Last commit: 2026-Q2 active.
- **Ships**: 52K task instances + 26K SWE-agent trajectories + 250+ Docker environments. Dataset: <https://huggingface.co/datasets/SWE-bench/SWE-smith>.
- **Trained model proof**: SWE-agent-LM-32B (Qwen2.5-Coder-32B fine-tuned on 5K of those trajectories) hits **40.2 % pass@1 on SWE-bench Verified** — SOTA among open-source 32B as of NeurIPS 2025.
- **Wire-in**: add to `merge_external` ladder. Take 5K-10K trajectories. Weight 2.0×. Dataset is ShareGPT-style — our `extract_pair()` handles it.
- **Compute**: pure SFT, no Docker needed at training time. Trajectories are static text.
- **Reproducibility**: `tutorials/finetune_qwen.md` shows exact SWE-agent CLI to fine-tune Qwen-2.5-Coder. Crib their LR schedule (cosine, 2 epochs, lr 1e-5).
- Citations: <https://huggingface.co/datasets/SWE-bench/SWE-smith> · <https://arxiv.org/abs/2504.21798>

### 9. R2E-Gym — `R2E-Gym/R2E-Gym` (COLM 2025)

- Repo: <https://github.com/R2E-Gym/R2E-Gym> · License: **Apache-2.0**.
- 9 commits as of 2026-Q2 — small but active.
- **Ships**: R2E-Gym-Lite, R2E-Gym-Full, R2EGym-SFT-Trajectories, R2EGym-TestingAgent-SFT-Trajectories, **R2EGym-Verifier-Trajectories**. 8.1K procedurally-curated executable tasks.
- **Innovation**: SWE-GEN — synthesizes executable training environments WITHOUT human-written PRs/tests. Hybrid verifier (execution + execution-free) → 51 % SWE-Bench Verified, competitive with o1 + sonnet-3.5.
- **LLaMA-Factory recipe shipped**: `train/train_r2egym_32B_agent.yaml` directly usable.
- **Wire-in**: add `R2E-Gym/R2EGym-SFT-Trajectories` to `merge_external` (target 8K take, weight 2.0×). Use `R2EGym-Verifier-Trajectories` to train a SEPARATE small (1.5B) verifier head — Civo L40S phase only, post-SFT.
- **Compute**: docker images 300-500 MB each (only needed for env replay, not SFT). 32B agent training cost mentioned implicitly (multi-GPU multi-node) — for us, just consume the trajectories.
- Citations: <https://r2e-gym.github.io/> · <https://huggingface.co/R2E-Gym>

### 10. SWE-Gym — `SWE-Gym/SWE-Gym` (ICML 2025)

- Repo: <https://github.com/SWE-Gym/SWE-Gym> · License: see repo (research).
- 2.4K real-world Python tasks from 11 repos. Lite split = 234 instances.
- **Verifier-trained**: best-of-n via execution-based verifier → inference-time scaling.
- **Wire-in**: lower priority than SWE-smith (smaller, narrower). Use as eval set, not training set.
- Citations: <https://arxiv.org/pdf/2412.21139>

### 11. OpenSWE — `GAIR-NLP/OpenSWE` (2026)

- Repo: <https://github.com/GAIR-NLP/OpenSWE> · Dataset: <https://huggingface.co/datasets/GAIR/OpenSWE>
- **Scale**: 45,320 executable Docker environments across 12.8K Python repos. ~13K curated trajectories from 9K quality-guaranteed envs. SOTA: OpenSWE-32B = 62.4 % SWE-bench Verified, OpenSWE-72B = 66.0 %.
- **License**: not yet declared on dataset card as of 2026-05-01 — **gate behind a check**.
- **Wire-in (highest priority for V10 SWE module)**: target 12K trajectories from `GAIR/OpenSWE`, weight 1.5×. Their SFT recipe matches our existing pipeline. **Caveat**: validate license before bulk-merging.
- **Compute**: SFT-only consumption is cheap. Env replay would need Docker + many GBs — skip.
- Citations: <https://huggingface.co/datasets/GAIR/OpenSWE> · <https://arxiv.org/pdf/2603.13023>

### 12. SWE-Swiss — `zhenyuhe00/SWE-Swiss`

- Repo: <https://github.com/zhenyuhe00/SWE-Swiss>
- Multi-task (file localization + program repair + SWE-bench) fine-tuning recipe + RL recipe for issue resolution. Uses Qwen2.5-Coder.
- **Wire-in**: study their **multi-task interleave ratio** (FL : repair : resolution = 2 : 3 : 5) — adopt in our SFT data mix. They report this beats unbalanced.
- Citations: <https://github.com/zhenyuhe00/SWE-Swiss>

---

## C. Function Calling / Tool Use Datasets (already partially in trainer)

### 13. ToolACE — `Team-ACE/ToolACE` (already integrated)

- Dataset: <https://huggingface.co/datasets/Team-ACE/ToolACE> · Apache-2.0.
- 26,507 diverse APIs, dual-layer verification, 8B model trained on it = comparable to GPT-4 on Berkeley Function Calling Leaderboard.
- **Status**: V8 already pulls 8K via `merge_external`, weight 1.5×. Keep.
- **2026-Q2 update**: paper accepted to ICLR 2026 (no major dataset rev — still v1).

### 14. Salesforce xLAM-function-calling-60k (already integrated)

- Dataset: <https://huggingface.co/datasets/Salesforce/xlam-function-calling-60k> · CC-BY-NC-4.0 → **non-commercial only**.
- Top-3 trending dataset on HF.
- **Status**: V8 pulls 10K, weight 1.0×. **Audit**: is Surrogate-1 commercial use? If yes, drop xLAM and replace with ToolACE+Hermes mix.

### 15. NousResearch Hermes-Function-Calling-v1

- Dataset: <https://huggingface.co/datasets/NousResearch/hermes-function-calling-v1> · Apache-2.0.
- ChatML-formatted multi-turn tool-calling dialogs. Used to train Hermes 2 Pro.
- **Wire-in**: NEW addition. Target 5K, weight 1.5×. Replaces xLAM if license-blocked.
- Citations: <https://github.com/NousResearch/Hermes-Function-Calling>

---

## D. Verifier / PRM / Process-Reward Repos

### 16. PRM800K — `openai/prm800k`

- Repo: <https://github.com/openai/prm800k> · MIT.
- 800K step-level correctness labels on MATH solutions.
- **Wire-in**: train a 1.5B Qwen-Math PRM head on Civo L40S (post-V10 V11 work). Use TRL `RewardTrainer` with `process_reward_loss=True` (added in v1.0).
- Citations: <https://github.com/openai/prm800k>

### 17. CodePRM (paper + planned code)

- Paper: <https://aclanthology.org/2025.findings-acl.428/> (no public repo as of 2026-05-01).
- **Generate-Verify-Refine** pipeline: PRM scores each thought step using execution feedback.
- **Wire-in**: replicate the GVR loop as a TRL custom reward function. Pseudo:
  ```python
  def code_prm_reward(completions, sandbox):
      pass_rate = run_in_sandbox(completions, timeout=10)
      step_scores = our_prm.score_thoughts(completions, pass_rate)
      return step_scores.mean(dim=-1)
  ```
- **Compute**: requires sandbox (we have axentx/codesand-runner). L40S only.
- Citations: <https://aclanthology.org/2025.findings-acl.428/>

### 18. PURE (Process Reward Min-Form) — `CJReinforce/PURE`

- Repo: <https://github.com/CJReinforce/PURE>
- "Stop summation: min-form credit assignment is all PRM needs for reasoning" — replaces sum-over-steps with min-over-steps in PRM credit.
- **Wire-in**: 5-line patch in our reward function — change sum to min. Free quality bump.
- Citations: <https://github.com/CJReinforce/PURE>

---

## E. Speculative-Decoding Trainable Heads (inference-time speedup, training-side responsibility)

### 19. EAGLE — `SafeAILab/EAGLE`

- Repo: <https://github.com/SafeAILab/EAGLE> · Last commit: actively merged 2026-Q2.
- EAGLE-3 (NeurIPS '25) — 6.5× speedup on inference, +1.4× over EAGLE-2.
- **Training cost**: small. Draft head adds ~0.25B params for 8B base, ~1B for 70B. Trainable on **Civo L40S in 8-12 hr** for 7B-14B base. Kaggle T4×2 borderline — use 7B base only.
- **Wire-in**: post-V10 inference upgrade, but training is part of V10 spec. Add `phase: train_eagle3_head` after SFT+GRPO. Their `eagle/train/main.py` is the reference script.
- Citations: <https://github.com/SafeAILab/EAGLE/blob/main/eagle/train/main.py>

### 20. SpecForge — `sgl-project/SpecForge`

- Repo: <https://github.com/sgl-project/SpecForge>
- The team strongly recommends SpecForge over raw EAGLE for Qwen-3 (Qwen2.5/3.5/3.6 RFC #486 in flight 2026-Q1).
- **Pre-trained heads available** for GPT-OSS 120B/20B and Qwen3-Coder 30B — direct download, no train.
- **Wire-in**: download pre-trained head if Surrogate base = Qwen3-Coder; else train via SpecForge on Civo L40S.
- Citations: <https://github.com/sgl-project/SpecForge> · <https://huggingface.co/lmsys/SGLang-EAGLE3-Qwen3-Coder-30B-A3B-Instruct-SpecForge>

---

## F. Knowledge Distillation Repos

### 21. DistillKit — `arcee-ai/DistillKit`

- Repo: <https://github.com/arcee-ai/DistillKit> · Apache-2.0.
- Modes: online (real-time teacher) + offline (pre-captured logits with bit-packed compression).
- Loss menu: KL, JSD, TVD, ranking, hidden-state alignment, plus standard CE.
- **Pre-captured teacher datasets**: Qwen3-235B logits (1.5B tokens), DeepSeek-V3/R1 mixed-mode (5B tokens), DeepSeek-V3 base (1.2B tokens) — all on HF Hub.
- **Wire-in**: NEW phase `distill_phase` after SFT. Use offline DeepSeek-V3 logits → 14B student → Civo L40S 24-48 hr. Kaggle T4×2 → 7B student only, ~12 hr.
- Citations: <https://github.com/arcee-ai/DistillKit> · <https://huggingface.co/datasets/arcee-ai/DistilLogits-1.0>

### 22. distillKitPlus — `agokrani/distillKitPlus`

- Repo: <https://github.com/agokrani/distillKitPlus>
- Adds offline + PEFT (LoRA distillation) for low-compute environments.
- **Wire-in**: T4×2 distillation alternative if DistillKit OOMs.

### 23. MiniLLM — `microsoft/LMOps/tree/main/minillm`

- Reverse-KL objective (more stable than forward-KL for generative students).
- **Wire-in**: option B for distillation phase. Test once vs DistillKit, pick winner.
- Citations: <https://github.com/microsoft/LMOps/tree/main/minillm>

### 24. DistiLLM — `jongwooko/distillm` (ICML 2024) + MiniPLM `thu-coai/MiniPLM` (ICLR 2025)

- Streamlined distillation; pre-training-stage variant.
- **Wire-in**: lower priority than DistillKit / MiniLLM for V10.

---

## G. Long-Context Training Repos

### 25. ProLong — `princeton-nlp/ProLong`

- Repo: <https://github.com/princeton-nlp/ProLong>
- Llama-3-8B continued pre-training + SFT to 512K context. Best-in-class 10B-scale long-context model.
- **Wire-in**: study their data mix (book + code + GitHub-PR + arxiv long-thread). For Surrogate V10 we don't go to 512K — but their SFT-stage recipe (32K → 128K → 256K curriculum) is portable to our 32K target.
- Compute: their full curriculum needs 8×A100. We do **only the 8K → 32K step** on Civo L40S in 12-18 hr.
- Citations: <https://github.com/princeton-nlp/ProLong>

### 26. LongLoRA — `dvlab-research/LongLoRA`

- Repo: <https://github.com/dvlab-research/LongLoRA>
- Shifted Sparse Attention (S² Attn) — sparse during fine-tune, dense at inference. 70 % less train memory.
- **Wire-in**: **the cleanest path to 32K context on Kaggle T4×2**. Their patch is 5 lines into the Qwen modeling file. Worth integrating.
- Citations: <https://github.com/dvlab-research/LongLoRA>

### 27. LongQLoRA — `yangjianxin1/LongQLoRA`

- Combines LongLoRA + QLoRA (4-bit). Even cheaper.
- **Wire-in**: Kaggle preferred path if we go long-context.

### 28. YaRN — already supported via `transformers` rope_scaling config

- We already set YaRN in `kaggle-trainer.sh` (R7 in technique stack). Keep.

---

## H. MoE Fine-Tuning Repos

### 29. MixLoRA — `TUDB-Labs/MixLoRA`

- Repo: <https://github.com/TUDB-Labs/MixLoRA>
- Builds sparse MoE from frozen dense base by injecting top-k routed LoRA experts in FFN. 40 % less GPU memory, 30 % less latency.
- **Wire-in**: **V11 candidate** — convert SFT'd Qwen-Coder-14B into MoE via MixLoRA for cheaper inference. V10 stays dense.
- Citations: <https://github.com/TUDB-Labs/MixLoRA>

### 30. mLoRA — `TUDB-Labs/mLoRA`

- "Factory" for building many LoRAs at once → useful for training a per-role adapter pool then composing via X-LoRA / LoraHub.
- **Wire-in**: V11 (per-role-adapter idea from `surrogate-1-v10-spec.md` § "30 roles").

---

## I. Continual Learning / Adapter Composition

### 31. LoraHub — `sail-sg/lorahub` (COLM 2024)

- Repo: <https://github.com/sail-sg/lorahub>
- Compose N LoRAs with few-shot examples of new task. Black-box optimizer chooses weights.
- **Wire-in**: **V10.5 path** — train one LoRA per major role (Eng / Ops / QA / Product / etc., ~30 LoRAs of r=8 each), compose at inference per query via LoraHub. Much smaller per-role LoRA storage.
- Citations: <https://github.com/sail-sg/lorahub>

### 32. MoLE — `adithya-s-k/MoLE` (Mixture of LoRA Experts)

- Per-layer gate weights LoRA outputs.
- **Wire-in**: alternative to LoraHub. Test bake-off in V11.

### 33. X-LoRA — see <https://github.com/EricLBuehler/x-lora>

- Mixture-of-LoRA experts, runtime routing. Works with PEFT.
- **Wire-in**: same V11 timeframe.

---

## J. Reflexion / Self-Correction Training

### 34. Reflexion — `noahshinn024/reflexion`

- Repo: <https://github.com/noahshinn024/reflexion>
- Original verbal-RL self-correction. Inference-time loop (not training).
- **Train-side**: ICLR 2026 submission introduced **ReTrace** — 200K structured self-correction examples bootstrapped from a teacher. Dataset not yet on HF.
- **Wire-in**: **bootstrap our own ReTrace-style data** from Cerebras Llama-3.3-70B as teacher. Generate ~10K traces of "draft → self-critique → revised draft" — merge into SFT set, weight 1.5×.
- Citations: <https://github.com/noahshinn024/reflexion>

### 35. Self-RAG — `AkariAsai/self-rag`

- Repo: <https://github.com/AkariAsai/self-rag>
- Trained model that generates retrieval + reflect + critique tokens.
- **Wire-in**: **format the most useful** for V10 — adopt their `<retrieve>`, `<critic>`, `<revise>` token vocabulary in our chat template. Train Surrogate to emit them.

### 36. Awesome-LLM-Self-Reflection — `rxlqn/awesome-llm-self-reflection`

- Aggregated paper/code list — mining target.

---

## K. Anti-Hallucination Training

### 37. TruthRL (paper, no code yet)

- Paper: <https://arxiv.org/abs/2509.25760>
- GRPO with **ternary reward** (correct / hallucinate / abstain). −28.9 % hallucination, +21.1 % truthfulness on 4 benches with Qwen + Llama bases.
- **Wire-in**: **single biggest leverage point in V10**. Implement as TRL custom reward function:
  ```python
  def truthrl_ternary_reward(completion, ground_truth, abstain_re=r"^I (don'?t|do not) know"):
      if re.match(abstain_re, completion): return 0.0   # abstain neutral
      if normalized_match(completion, ground_truth): return +1.0
      return -1.0   # hallucination penalty
  ```
  Need: dataset of (q, gt) pairs with verifiable answers — use TriviaQA + NaturalQuestions + our internal corpora.
- Citations: <https://huggingface.co/papers/2509.25760>

### 38. LRV-Instruction — `FuxiaoLiu/LRV-Instruction` (multimodal-focused, but technique transfers)

- Negative + positive instructions in same batch.
- **Wire-in**: include "I don't know" examples in our SFT mix with non-zero weight. Our existing data has zero abstention examples — that's a hallucination pump. Fix.

---

## L. Multi-Agent Training Data

### 39. CAMEL — `camel-ai/camel`

- Repo: <https://github.com/camel-ai/camel>
- Role-playing data synthesis between two GPT agents → 25K conversations used in OpenHermes + Microsoft Phi training.
- **Wire-in**: synthesize ~20K role-playing pairs across our 30 V10 roles using Cerebras Llama-3.3-70B (free tier) as both role agents. Cost ~0. Weight 1.5×.
- Citations: <https://github.com/camel-ai/camel>

### 40. AgentVerse — `OpenBMB/AgentVerse`

- Repo: <https://github.com/OpenBMB/AgentVerse>
- Multi-agent task-solving + simulation framework. Trajectories can be exported.
- **Wire-in**: similar to CAMEL — generate trajectories, harvest, merge.

### 41. MARTI — `TsinghuaC3I/MARTI` (ICLR 2026)

- Repo: <https://github.com/TsinghuaC3I/MARTI>
- LLM Multi-Agent **training** (not just inference) with RL. MARTI-v2 adds tree-search-augmented RL for code generation.
- **Wire-in**: V11 candidate — once we have a working V10 we can use MARTI to train the multi-role coordination directly. Right now, scaffold via CAMEL data.
- Citations: <https://openreview.net/forum?id=E7jZqo0A50>

---

## M. Voyager-Style Skill Library (training-side angle)

### 42. Voyager — `MineDojo/Voyager`

- Repo: <https://github.com/minedojo/voyager>
- Original is GPT-4 inference-time, no fine-tune.
- **Train-side adaptation**: convert their JS skill library to Python skill library; turn each successful skill into an SFT pair `(task, code)` → merge into Surrogate's training data. Estimated yield: 200-500 high-quality "tool-build" pairs.
- **Wire-in**: low priority but novel signal. Out of V10 scope unless cheap.
- Citations: <https://github.com/minedojo/voyager>

---

## N. Code-Specific Datasets (already merged or candidate)

- **Code-Feedback (m-a-p)** — already merged. <https://huggingface.co/datasets/m-a-p/Code-Feedback>. 45K multi-turn debug dialogues.
- **VisCoder / VisCode-200K** — runtime-guided revision for plotting code. <https://github.com/TIGER-AI-Lab/VisCoder>. Niche but high signal for data-viz role.
- **Multi-IaC-Eval (AmazonScience)** — already merged. Apache-2.0.
- **Nemotron-RL-Super-Training-Blends (Nvidia)** — <https://huggingface.co/datasets/nvidia/Nemotron-RL-Super-Training-Blends>. Apache + MIT. The actual 6-stage RL blend (RLVR ×3, SWE ×2, RLHF ×1). **HIGH-VALUE merge candidate** — these are the exact mixes that produced Nemotron-Super-120B.

---

## O. Constitutional AI / RLAIF (already in alignment pipeline)

- **anthropics/hh-rlhf** — <https://github.com/anthropics/hh-rlhf> · MIT. 170K pref pairs (helpful + harmless). Stable, no 2026 update needed.
- **OpenAssistant/oasst1** — <https://huggingface.co/datasets/OpenAssistant/oasst1> · Apache-2.0. 161K messages, 35 langs, 461K quality ratings. Stable.
- **2026-Q2 finding**: no major new public CAI dataset. Constitutional approach is now baked into TRL `RewardTrainer` indirectly via DPO.

---

## P. Long-Sequence Training Infrastructure

### 43. DeepSpeed-Ulysses — `deepspeedai/DeepSpeed`

- Repo: <https://github.com/deepspeedai/DeepSpeed/tree/master/blogs/deepspeed-ulysses>
- Sequence parallelism + ZeRO-3. 4× longer seq, 10× less comms, 2.5× throughput.
- **Arctic Long Sequence Training (ALST)** — 2026 evolution: **400× improvement** in trainable context for Llama-8B on H100 cluster. <https://github.com/deepspeedai/DeepSpeed/blob/master/blogs/ulysses-offload/README.md>
- HF Accelerate already integrates Ulysses SP — we get it free via `accelerate launch`.
- **Wire-in**: confirm `accelerate>=1.2.0` in requirements (already set). Add `--use_seq_parallel` flag for Civo L40S long-context phase.
- Citations: <https://www.deepspeed.ai/tutorials/ds-sequence/>

---

## Q. Other notable items briefly

- **Together AI DeepSWE** — Qwen3-32B + open-RL coding agent. <https://www.together.ai/blog/deepswe>. Trained on R2E-Gym extension.
- **APEX-SWE (Mercor)** — <https://huggingface.co/datasets/mercor/APEX-SWE>. Commercial. Skip.
- **SWE-bench_Pro (ScaleAI)** — <https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro>. Likely closed eval.
- **EasyDistill (modelscope)** — <https://github.com/modelscope/easydistill>. Lower priority alt to DistillKit.

---

## Compute Budget Reality Check

| Phase | Hardware | Time | Cost |
|---|---|---|---|
| SFT 7B Qwen2.5-Coder LoRA | Kaggle T4×2 (free 30 hr/wk) | 12 hr | $0 |
| SFT 14B Qwen2.5-Coder QLoRA | Civo L40S 48 GB | 18-24 hr | ~$45 (Civo $1.85/hr) |
| GRPO (post-SFT) on 14B | Civo L40S 48 GB | 8-12 hr | ~$22 |
| DistillKit offline (DeepSeek-V3 logits → 14B student) | Civo L40S 48 GB | 24-36 hr | ~$66 |
| EAGLE-3 head training on 14B | Civo L40S 48 GB | 8-12 hr | ~$22 |
| LongLoRA 32K extension | Kaggle T4×2 | 18 hr | $0 |
| **Total V10 single full run** | mixed | ~108 hr | ~$155 |

---

## Wire-Into-Surrogate-Trainer Action Items

> Concrete code-change list, ordered by ROI. File paths assume `/Users/Ashira/.surrogate/hf-space/bin/kaggle-trainer.sh` (Kaggle) and the planned `civo-trainer/` directory.

### TIER 1 — Must do for V10 (high ROI, low risk)

1. **Bump versions** in `kaggle-trainer.sh` deps block:
   ```python
   "trl>=1.3.0,<1.4.0",        # was 0.12-0.16
   "peft>=0.19.0,<0.21.0",     # was 0.13-0.15
   "accelerate>=1.2.0,<1.5.0", # was 1.0-1.3
   "transformers>=4.50.0,<4.55.0",
   ```
   Removes deprecated `use_transformers_paged` (saves 20 % step time + 6× peak VRAM).

2. **Add TruthRL ternary reward** as a new `truthrl_phase` after GRPO. ~40 lines. Single biggest hallucination-killer in V10.

3. **Add SWE-smith merge** — 8K trajectories, weight 2.0× into `merge_external` ladder. One line:
   ```python
   merge_external("SWE-bench/SWE-smith", int(os.environ.get("TAKE_SWESMITH", "8000")), 2.0, "SWE-smith")
   ```

4. **Add R2E-Gym SFT trajectories** — 6K, weight 2.0×:
   ```python
   merge_external("R2E-Gym/R2EGym-SFT-Trajectories", int(os.environ.get("TAKE_R2EGYM", "6000")), 2.0, "R2E-Gym-SFT")
   ```

5. **Add Nvidia Nemotron-RL-Super blends** — 10K, weight 1.0× (proven RL data):
   ```python
   merge_external("nvidia/Nemotron-RL-Super-Training-Blends", int(os.environ.get("TAKE_NEMORL", "10000")), 1.0, "Nemotron-RL-Super")
   ```

6. **Replace xLAM (CC-BY-NC-4.0)** with `NousResearch/hermes-function-calling-v1` (Apache-2.0) if Surrogate is commercial:
   ```python
   merge_external("NousResearch/hermes-function-calling-v1", int(os.environ.get("TAKE_HERMESFC", "5000")), 1.5, "Hermes-FC")
   ```

7. **Adopt PURE min-form credit** — replace `step_rewards.sum()` with `step_rewards.min()` in our PRM phase scaffold (5-line change).

8. **Set `optim="paged_adamw_8bit"` only on Kaggle T4×2**; on Civo L40S use **Muon** via axolotl YAML. Muon converges 1.5× faster on math/code reasoning.

### TIER 2 — Should do for V10 (medium ROI)

9. **Add `RUN_DISTILL=1` env-knob** that triggers TRL `DistillationTrainer` after SFT. Teacher = Cerebras Llama-3.3-70B endpoint (no local VRAM cost). Student = our LoRA-SFT'd Qwen-Coder.

10. **Add `RUN_SSD=1` env-knob** for `SSDTrainer` (Embarrassingly Simple Self-Distillation) — runs entirely from model's own samples. Useful when Cerebras quota exhausted.

11. **Add LongLoRA patch** for any run with `SEQ_LEN ≥ 16384`. 5-line monkeypatch in modeling_qwen.py via the `LongLoRA` `replace_llama_attn_with_flash_attn()`-equivalent.

12. **Add CAMEL self-synthesized roleplay** — write a one-off generator script using Cerebras free tier to produce 20K role-pair conversations across our 30 V10 roles. Save to `axentx/surrogate-1-roleplay-camel`. Merge weight 1.5×.

13. **Add Reflexion-style ReTrace bootstrap** — 10K "draft → critique → revise" traces from Cerebras teacher. Save to `axentx/surrogate-1-retrace`. Merge weight 1.5×.

14. **Add OpenSWE merge** (after license check) — 12K trajectories, weight 1.5×:
   ```python
   merge_external("GAIR/OpenSWE", int(os.environ.get("TAKE_OPENSWE", "12000")), 1.5, "OpenSWE")
   ```

15. **Self-RAG token vocab** — extend our chat template to support `<retrieve>`, `<critic>`, `<revise>` tokens. Train model to emit them.

### TIER 3 — Civo L40S only / V10.5

16. **Civo trainer = axolotl YAML** instead of raw Python. Use `optim: muon`, `rl: grpo` async, FA2 (or FA4 if Hopper). One YAML per phase: `sft.yml`, `grpo.yml`, `distill.yml`, `eagle3.yml`.

17. **EAGLE-3 head training phase** post-V10. Use SpecForge if base = Qwen3-Coder, else SafeAILab/EAGLE.

18. **SWE-Swiss multi-task ratio** — adopt FL : repair : resolution = 2 : 3 : 5 within our SWE data slice.

19. **OLoRA / LoRA-GA bake-off** — extend our `SUR_LORA_INIT` ladder from 4 to 6 options: `loftq · pissa_niter_4 · loftq+pissa · corda · olora · lora_ga`. Run once on identical data, pick winner for V10.

### TIER 4 — V11 candidates (not V10)

20. **MixLoRA dense → MoE conversion** post-V10.
21. **LoraHub per-role adapter pool** (one r=8 LoRA per V10 role, compose at inference).
22. **MARTI multi-agent RL** for coordinator-role training.
23. **Voyager-style skill library** harvest → SFT pairs.
24. **OpenRLHF / veRL** when we move to multi-node Civo cluster.

---

## See Also

- [[surrogate-1-v10-spec]] — V10 polymath spec, 30 roles
- [[surrogate-1-v9-spec]] — V9 baseline
- [[surrogate-latest-improvements-2026]] — running improvements log
- [[trends-2026/coding-llm-frontier]] — frontier model landscape
- [[trends-2026/devsecops-sre-agentic]] — SRE/DevSecOps datasets
- [[trends-2026/data-ml-aiops]] — data/ML pipeline tooling

---
name: Self-Improvement & Continual Learning — Trends 2026
description: Top techniques (2024-2026) for letting a deployed code/SRE agent (Surrogate-1, Qwen2.5-Coder-7B) keep improving from its own outputs + execution outcomes without manual data labeling.
tags: [trends, 2026, self-improvement, continual-learning, RLAIF, RLEF, RLVR, DPO, GRPO, surrogate-1, post-training]
last_updated: 2026-05-01
aliases: [self-improvement-2026, continual-learning-llm, rlvr-loop]
---

# Self-Improvement & Continual Learning for Deployed Code/SRE LLMs

> Compiled 2026-05-01 for **Surrogate-1** (Qwen2.5-Coder-7B fine-tune). Goal: a deployed agent that **keeps getting better from its own outputs + outcomes**, no manual labeling. All cost numbers assume H200/A100 hourly rates as of 2026-Q2 and a 7B base.

---

## TL;DR — The 2026 Self-Improvement Stack

The single biggest 2025 insight (DeepSeek-R1, RLEF, DAPO): **verifiable rewards (unit tests, type-checks, exec-pass) beat human preference labels** for code/SRE because the signal is binary, free, infinite, and unbiased. Our entire loop should be built around it.

**Recommended pipeline for Surrogate-1 (full loop)**:
```
deploy → log (prompt, response, outcome) → bucket by signal type
  ├ exec-passing traces → SFT replay buffer (cheap, weekly)
  ├ exec + diff vs prior → DPO/SimPO pairs (medium, biweekly)
  ├ exec + reasoning chain → GRPO/DAPO RLVR rollouts (expensive, monthly)
  ├ judge-disputed traces → Self-Rewarding / Meta-Rewarding judge update (cheap)
  └ user-flagged regression → fuzz-bench mining → SWE-Gym style task seed
↓
LoRA delta → eval gate (HumanEval+, MBPP+, internal bench) → merge or reject
```

---

## Part 1 — Self-Play Fine-Tuning (Generator-Generator Loop)

### 1.1 SPIN — Self-Play Fine-Tuning (Chen et al. 2024)
- **Paper**: arXiv 2401.01335, ICML 2024 — [GitHub uclaml/SPIN](https://github.com/uclaml/SPIN)
- **Signal**: distinguish own old outputs vs human-annotated SFT data via a DPO-style discriminator loss (no external reward model).
- **Loop**: at iter `t`, model `M_t` generates synthetic `y'`; train `M_{t+1}` to prefer `y_human` over `y'`. Repeats until `y' ≈ y_human`.
- **Wire-in for Surrogate-1**: keep our 60k human SFT corpus as anchor; each refresh, sample `y'` from current Surrogate-1 → pairs `(y_human, y')` → DPO loss with β=0.1.
- **Cost/iter (7B)**: ~6h on 1× H200, LoRA r=64. **Verdict**: cheap baseline; saturates after 3-4 iters. Good warm-up before RLEF.

### 1.2 Self-Rewarding LMs (Yuan et al. 2024)
- **Paper**: arXiv 2401.10020 — Llama-2-70B → 3 iters → beat Claude 2 / Gemini Pro on AlpacaEval 2.0.
- **Signal**: model itself acts as **LLM-as-judge** scoring its own multiple completions → builds DPO pairs.
- **Risk for code**: judge bias on style over correctness. Mitigation: gate judge votes behind a **mandatory exec-pass filter** — only judge among exec-passing candidates.
- **Cost/iter**: ~8h on 1× H200 (judge inference + DPO).

### 1.3 STaR / V-STaR / ReST^EM (2022-2024)
- **STaR** (Zelikman 2022, arXiv 2203.14465): generate rationale → if answer wrong, retry with answer hint → SFT on successful rationales. Bootstraps reasoning chains.
- **V-STaR** (Hosseini 2024, arXiv 2402.06457): uses **both** correct and incorrect rationales to train a **DPO verifier**. +4-17% over STaR on MATH/GSM8K.
- **ReST^EM** (Singh 2024): casts STaR as **EM**: E-step = generate-and-filter, M-step = SFT on filtered. Provably converges.
- **Wire-in**: every SRE/code task we deploy already has a verifier (terraform plan, pytest, kubectl apply --dry-run). Use STaR loop: log failed attempts, retry with ground-truth fix from runbook, train on successful retry trajectories.
- **Cost/iter**: ~4h on 1× H200 for SFT, ~2h extra for V-STaR verifier head.

### 1.4 CodeRL / CodeRL+ (Le 2022, 2025)
- **Paper**: NeurIPS 2022 + arXiv 2510.18471 (CodeRL+, Oct 2025)
- **Signal**: actor-critic with unit-test pass rate as dense reward; CodeRL+ adds **execution semantics alignment** (intermediate state matching, not just final output).
- **Wire-in**: bridge between SFT and full RLEF. Use as warm-start for actor before GRPO.
- **Cost/iter**: ~12h on 1× H200 for 7B (heavier than DPO, lighter than full GRPO).

---

## Part 2 — Online RL with Execution Feedback (THE 2025 STACK)

### 2.1 RLEF — RL from Execution Feedback (Gehring et al., Meta 2024)
- **Paper**: arXiv 2410.02089, ICML 2025 — multi-turn structure: model writes code → exec → feedback prompt → revised code → reward.
- **Result**: 8B model matches 70B on CodeContests with 10× fewer samples; gains transfer to HumanEval+/MBPP+.
- **Signal**: pass-fail on visible test cases at each turn (multi-turn dense reward).
- **Wire-in for Surrogate-1**: this is **the** primary loop. Every deployed call → exec sandbox (Modal/Lightning function) → reward = #tests_passed/total. Multi-turn rollouts with **PPO or GRPO** as optimizer.
- **Cost/iter**: ~24h on 1× H200, batch=64 rollouts × 4 turns. **Most important refresh** — biweekly minimum.

### 2.2 GRPO — Group Relative Policy Optimization (DeepSeek 2024)
- **Paper**: DeepSeekMath arXiv 2402.03300, DeepSeek-R1 arXiv 2501.12948 (Nature 2025).
- **Innovation**: drops critic entirely; advantage = `(r_i - mean(group)) / std(group)`. Group=8-16 rollouts per prompt. Memory ½ of PPO.
- **Result**: AIME pass@1: 15.6% → 77.9% on Qwen2.5-32B base.
- **Wire-in**: replace PPO inside RLEF loop with GRPO. Implementation: [OpenRLHF](https://github.com/OpenRLHF/OpenRLHF) (Ray + vLLM) or `trl.GRPOTrainer`.
- **Cost/iter**: ~30% cheaper than equivalent PPO due to no critic. ~16h on 1× H200 for 7B with LoRA.

### 2.3 DAPO — Decoupled Clip + Dynamic Sampling (ByteDance 2025)
- **Paper**: arXiv 2503.14476 (March 2025) — [GitHub BytedTsinghua-SIA/DAPO](https://github.com/BytedTsinghua-SIA/DAPO)
- **Improvements over GRPO**: (1) **Clip-Higher** — asymmetric clip (`ε_low=0.2, ε_high=0.28`) prevents entropy collapse; (2) **Dynamic Sampling** — drop prompts where all rollouts pass or all fail (zero gradient); (3) Token-level loss; (4) Overlong reward shaping.
- **Result**: Qwen2.5-32B → 50 on AIME 2024 with **half** the steps of DeepSeek-R1-Zero.
- **Wire-in**: drop-in replacement for GRPO once we have ≥10k task pool. Dynamic sampling is the killer feature for our heterogeneous SRE task mix.
- **Cost/iter**: same as GRPO but converges 2× faster → **net 50% cheaper**.

### 2.4 RLOO / REINFORCE++
- RLOO (Ahmadian 2024) and REINFORCE++ (Hu 2025) are simpler variants: leave-one-out baseline, no clipping. Slightly worse than GRPO but **3× simpler to implement and debug**. Use as fallback if GRPO training is unstable.
- Cost: ~10h on 1× H200.

---

## Part 3 — DPO Family on Self-Generated Outputs

| Method | Year | Signal | Best for | Cost vs DPO |
|---|---|---|---|---|
| **DPO** (Rafailov 2023) | base | preference pairs | general baseline | 1.0× |
| **IPO** (Azar 2024) | regularized DPO | pairs | small datasets, anti-overfit | 1.0× |
| **KTO** (Ethayarajh 2024) | unpaired ±labels | binary like/dislike | logs with thumb-up/down only | 0.7× (no pairs needed) |
| **ORPO** (Hong 2024) | odds-ratio + SFT | pairs | combine SFT+DPO in one pass | 0.6× (single stage) |
| **SimPO** (Meng 2024, NeurIPS) | reference-free, length-normalized | pairs | memory-constrained | 0.5× (no ref model) |
| **sDPO** (Kim 2024) | iterative ref update | pairs | multi-iter improvement | 1.2× × N iters |
| **BCO** (Jung 2024) | binary classifier | unpaired ±labels | scaled-up KTO | 0.7× |
| **RPO** (Reward-aware PO 2025) | reward magnitude | pairs + scores | when reward model exists | 1.1× |

**Recommendation for Surrogate-1**: **SimPO + KTO hybrid** for biweekly refresh.
- SimPO on (exec-pass) vs (exec-fail) pairs from same prompt — no reference model = fits 7B in 24GB.
- KTO on stand-alone exec results (no pair needed) — utilizes 100% of logs, not just dual-rollout cases.

---

## Part 4 — Skill Libraries & Memory Update

### 4.1 Voyager (Wang 2023, NVIDIA)
- **Paper**: arXiv 2305.16291 — Minecraft lifelong agent: auto-curriculum + skill library (NL-keyed embedding) + iterative prompting.
- **Wire-in**: each successful Surrogate-1 trajectory → distill into a named skill (e.g., `fix_terraform_lock`, `restart_failing_pod`) → store in vector DB → retrieve at next inference.

### 4.2 AutoSkill (ECNU 2025) / EvoSkill / SkillFoundry
- **AutoSkill** ([GitHub](https://github.com/ECNU-ICALK/AutoSkill)) — extracts skills from real user interactions, versions them, prunes stale.
- **EvoSkill** (arXiv 2603.02766) — failure-driven: analyses exec failure → proposes new skill → materializes.
- **SkillFoundry** (arXiv 2604.03964) — tree-guided skill library from heterogeneous resources.
- **Wire-in**: nightly job → mine last-24h failures → propose skill → human review → add to library.

### 4.3 Eureka (Ma 2023, NVIDIA)
- **Signal**: LLM-designed reward functions via evolutionary search over reward code.
- **Wire-in**: when we lack a clear reward for a SRE task (e.g., "page severity"), Eureka can synthesize a Python reward function from a task description.

### 4.4 ToolUniverse (2025)
- 1000+ APIs/datasets/models standardized for AI scientists. For Surrogate-1: maintain a `tools.json` registry that the agent can query at runtime; weekly update from ToolUniverse + our internal MCP servers.

---

## Part 5 — Continual LoRA Stacking (Avoid Forgetting)

| Method | Year | Mechanism | Use case |
|---|---|---|---|
| **LoRA Hub** (Huang 2023, COLM 2024) | sail-sg/lorahub | gradient-free composition of pre-trained LoRAs | combine "terraform-LoRA" + "k8s-LoRA" at inference |
| **MoLE** (Mixture of LoRA Experts, Wu 2024) | hierarchical gating | ICLR 2024 — branch selection routing | multi-domain Surrogate-1 (devops + general code) |
| **X-LoRA** (Buehler 2024) | mixture of low-rank adapter experts | dense LoRA mixing per token | fine-grained domain switching |
| **I-LoRA** (Iterative Merging, 2025) | iterative merge of routing-tuned LoRAs | order-invariant continual | weekly LoRA additions w/o re-training |
| **K-Merge** (2025, arXiv 2510.13537) | online continual merging on-device | streaming LoRA adds | edge deployment, ZeroGPU |
| **Sparse Memory Finetuning** (Meta FAIR Oct 2025) | sparse memory layer slots | only updates slots tied to new info | aggressive online updates, low forgetting |

**Recommendation**: keep **base SFT LoRA frozen**, stack new LoRAs per refresh stage. Use **I-LoRA merge** every 4 iterations to consolidate. SuRe (arXiv 2511.22367) for replay buffer (surprise-driven sampling).

---

## Part 6 — Curriculum & Auto-Eval

### 6.1 Auto-Tuned Curriculum
- **CAMPUS** — competence-aware scheduling: maintain difficulty-bucketed sub-curricula, advance based on rolling pass-rate.
- **TAPIR** — multi-round: hard-instruction seed pool from MFD scores, teacher-expand, rebalance.
- **Adaptive Difficulty CL** (ACL 2025 arXiv "Learning Like Humans") — +16.6% on AIME25 by ramping difficulty with model competence.
- **Wire-in**: bucket Surrogate-1 task pool into easy/medium/hard by current pass-rate. Each refresh, sample 60% from "frontier" bucket (40-70% pass-rate) where gradient is highest. Auto-promote tasks as model improves.

### 6.2 Auto-Eval Generation
- **EvalPlus** — 80× more test cases than HumanEval via LLM mutation. Should be used as our gating bench every refresh.
- **AlpacaEval 2.0 LC-WR** — length-controlled, 0.98 corr w/ Arena, <$10 per run.
- **MT-Bench juries** — multi-turn LLM-as-judge.
- **Wire-in**: (a) every PR/refresh runs HumanEval+, MBPP+, EvalPlus mutations; (b) internal SRE tasks use a Llama-3-70B jury for non-exec tasks (architectural questions).

### 6.3 Regression Test Growth
- **SWE-Gym** (ICML 2025) — 2438 real Python tasks with full exec env. **Use as regression bench from day 1.**
- **SWE-smith** (NeurIPS 2025) — turn any GitHub repo into a SWE-gym; unlimited tasks per repo.
- **R2E-Gym / SWE-GEN** (COLM 2025) — 51% on SWE-Bench Verified (open-weight SOTA). Procedural env generation w/o human PRs.
- **SWE-Bench++** (Dec 2025) — programmatic PR → reproducible exec task pipeline.
- **Wire-in**: nightly cron — scrape closed issues from our internal repos → SWE-smith pipeline → add to regression suite. Catches "fixed-then-regressed" automatically.

---

## Part 7 — Self-Distillation & Weak-to-Strong

### 7.1 SDFT — Self-Distillation Fine-Tuning (Yang ACL 2024)
- **Paper**: ACL 2024 — [GitHub sail-sg/sdft](https://github.com/sail-sg/sdft) — bridges distribution gap by SFT on **model's own rephrasing** of target answers.
- **Continual Learning version** (Shenfeld 2026, arXiv 2601.19897): **prevents catastrophic forgetting**. SQA: 70.2% (SDFT) vs 66.2% (vanilla SFT).
- **Cost**: 2.5× SFT compute. Needs strong base model w/ in-context learning.
- **Wire-in**: every refresh, regenerate training labels through Surrogate-1 itself first (on-policy distillation). Mitigates the "8 hops away from base distribution" problem after many refresh cycles.

### 7.2 Weak-to-Strong Generalization (OpenAI Dec 2023, ICML 2024)
- **Paper**: arXiv 2312.09390. GPT-2 supervisor elicits ~GPT-3.5 from GPT-4.
- **Wire-in**: when we eventually outpace the human-labeled SFT corpus, weak-to-strong gives us a path to keep improving with **synthetic weak labels** rather than starving for human data.

### 7.3 IDA — Iterated Amplification + Distillation (Christiano 2018, applied 2025)
- Pattern: weak slow agent → amplify (decompose, ensemble) → distill into faster student → repeat.
- **Wire-in**: Cerebras / Groq inference of large open model = "amplifier"; Surrogate-1 = "student". Burst LLM at scale to solve hard tasks, distill answers into Surrogate-1 weekly.

### 7.4 Knowledge Distillation from Larger Models
- 10-100× smaller student retains 95-99% teacher quality (NVIDIA NeMo 2025).
- **Wire-in**: when API quota allows, use **GPT-5 / Claude Opus 4.7 / DeepSeek-V4** to label hard Surrogate-1 failures; SFT student on labels. Budget: $50/refresh max.

---

## Part 8 — Memory & RAG Updates

| System | Year | Type | Wire-in |
|---|---|---|---|
| **MemGPT / Letta** | 2023, 2025 docs | OS-style paging memory | Surrogate-1 long convo state |
| **A-Mem** (Xu 2025) | adaptive memory | 2025 | episodic logs of agent actions |
| **Mem0** (arXiv 2504.19413) | 3-tier user/session/agent | 2025 | thumbs-up/down logs |
| **LangMem** | 2025 | episodic + semantic + procedural | full Tulving taxonomy |
| **Sparse Memory Finetuning** (Meta 2025) | trainable memory slots | edge | sub-1B params per skill |

**Recommendation**: 3-tier memory — (1) **Episodic** in Mem0/Letta (per-session conversation), (2) **Semantic** in ChromaDB+FalkorDB (our existing GraphRAG stack), (3) **Procedural** as LoRA deltas (skill library). Refresh episodic→semantic weekly, semantic→procedural monthly.

---

## Part 9 — Constitutional AI v2 + RLAIF

### 9.1 Constitutional AI (Anthropic 2022, ongoing 2025)
- **Two-phase**: (1) self-critique → revision → SFT on revisions; (2) RLAIF — model picks preferred response per constitution → reward model → PPO.
- **Wire-in for Surrogate-1**: maintain a `constitution.md` (DevOps best practices: never hardcode secrets, prefer least-privilege IAM, etc.). Daily: sample 100 logged responses → self-critique → filter violations → retrain.

### 9.2 Meta-Rewarding LMs (Wu 2024)
- **Paper**: arXiv 2407.19594 — model is **actor + judge + meta-judge**.
- **Result**: Llama-3-8B-Instruct AlpacaEval 2.0: 22.9% → 39.4% (no human data).
- **Wire-in**: extends Self-Rewarding (§1.2). Budget: ~10h/iter on 1× H200. Cap at 4 iters before saturation.

---

## Part 10 — Signals We Already Have (Surrogate-1 Specific)

Inventory as of 2026-05-01:

| Source | Signal type | Volume/day | Refresh stage it feeds |
|---|---|---|---|
| HF Spaces logs (ingestion + chat) | (prompt, response, latency) | ~1-2k | replay buffer, KTO |
| ZeroGPU function calls | exec-pass rate, error trace | ~200-500 | RLEF, GRPO core |
| Lightning Studio H200 training jobs | gradient norms, eval delta | per-train | learning curve QA |
| Internal benchmark runs | HumanEval+/MBPP+/SWE-Gym pass | per-refresh | gating, regression |
| User thumbs-up/down (when wired) | binary preference | TBD | KTO, BCO |
| HF dataset mirror commits | new code samples | weekly | SFT replay |
| Cerebras/Groq burst calls (large model) | distillation labels | per-budget | SDFT, knowledge distillation |
| Slack/Linear ops incidents | failed automations | ~5-20 | SWE-smith mining, V-STaR |

**Gaps to close**:
- Wire **structured exec sandbox** (Modal function w/ pytest + cfn-lint + tflint) — required for RLEF.
- Add **thumbs-up/down UI** to HF Space → enables KTO with zero label cost.
- Set up **regression suite cron** scraping closed GitHub issues → SWE-smith pipeline.

---

## Continuous Improvement Loop Spec for Surrogate-1

### Architecture
```
                   ┌──────────────────────────────────┐
                   │  HF Space (Surrogate-1 deployed) │
                   │  Mac CLI orchestration only      │
                   └──────────────┬───────────────────┘
                                  │ logs
                                  ▼
                ┌─────────────────────────────────────┐
                │  Signal Bus (S3 + DuckDB)           │
                │  (prompt, response, exec_result,    │
                │   thumbs, latency, model_ver)       │
                └─────────────┬───────────────────────┘
                              │
        ┌─────────────────────┼──────────────────────┐
        ▼                     ▼                      ▼
 ┌──────────────┐    ┌─────────────────┐   ┌─────────────────┐
 │ DAILY (zero $)│    │ WEEKLY ($5-10)  │   │ BIWEEKLY ($20)  │
 │  - constit.   │    │ - replay SFT    │   │ - SimPO/KTO     │
 │    self-      │    │ - LoraHub       │   │   on pairs      │
 │    critique   │    │   merge skills  │   │ - skill lib     │
 │  - mem0 com-  │    │ - HumanEval+    │   │   versioning    │
 │    pact       │    │   eval gate     │   │ - I-LoRA merge  │
 └──────────────┘    └─────────────────┘   └─────────────────┘
                              │
                              ▼
              ┌─────────────────────────────────┐
              │  MONTHLY ($60-150)              │
              │  - RLEF + GRPO/DAPO rollouts   │
              │  - SWE-smith regression growth  │
              │  - Meta-Rewarding judge update  │
              │  - SDFT consolidation           │
              └─────────────────────────────────┘
                              │
                              ▼
              ┌─────────────────────────────────┐
              │  QUARTERLY ($200-500)           │
              │  - Knowledge distillation       │
              │    from GPT-5/Opus on hard fails│
              │  - W2S generalization audit     │
              │  - Full retrain from base if    │
              │    cumulative LoRA drift > 5%   │
              └─────────────────────────────────┘
```

### Cron Cadence

| Stage | Cron | Compute | Cost (Lightning H200 ~$3.5/hr) |
|---|---|---|---|
| Constitution self-critique + Mem0 compact | `0 4 * * *` (daily 4am) | Mac CLI + Cerebras burst | $0-1/day |
| SFT replay on exec-pass traces | `0 5 * * 0` (weekly Sun 5am) | 1× H200 × 4h | ~$14 |
| HumanEval+/MBPP+ gating eval | after every train | 1× H200 × 1h | ~$3.5 |
| SimPO/KTO on logged prefs | `0 6 1,15 * *` (1st + 15th 6am) | 1× H200 × 6h | ~$21 |
| RLEF + GRPO/DAPO rollouts | `0 6 5 * *` (5th of month) | 1× H200 × 24h | ~$84 |
| SWE-smith regression mining | `0 3 * * 1` (weekly Mon) | Mac orchestration + Modal exec | ~$2 |
| Meta-Rewarding judge refresh | `0 6 10 * *` (10th of month) | 1× H200 × 10h | ~$35 |
| Knowledge distillation from frontier | `0 6 1 */3 *` (quarterly) | API + 1× H200 × 8h | ~$80 |
| Full base retrain (if drift > 5%) | manual | 1× H200 × 48h | ~$170 |

**Total monthly steady-state**: ~$190/mo (well under quarterly $500 budget).

### Eval Gates (mandatory before merging any LoRA)
1. HumanEval+ pass@1 ≥ baseline - 0.5% (no regression).
2. MBPP+ pass@1 ≥ baseline - 0.5%.
3. Internal SWE-Gym subset (50 tasks) ≥ baseline.
4. AlpacaEval 2.0 LC-WR ≥ baseline - 1% (instruction following).
5. Constitution-violation rate ≤ baseline + 0.1%.

If any fail → **reject LoRA, log failure into lessons_learned, do not deploy**.

### Anti-Patterns to Avoid
- Training on own outputs without exec-filter → mode collapse (RLEF paper §5).
- Skipping regression suite → forget old skills (SuRe + I-LoRA mitigates).
- Judge-only rewards on code → reward-hacking on style/length (always exec-gate first).
- Long DPO chains without ref-model anchor → preference drift (use sDPO ≤ 4 iters or SimPO).
- Letting LoRA stack grow unbounded → inference latency. Merge every 4 iters via I-LoRA / K-Merge.

### Top 5 Wire-In Priorities (NOW, 2026-05-01)
1. **Exec sandbox** (Modal/Lightning function) — RLEF prerequisite. Without it, no verifiable rewards.
2. **Signal bus** (S3 → DuckDB) for (prompt, response, exec_result) logging.
3. **HumanEval+ / MBPP+ / SWE-Gym** gating bench — required before any merge.
4. **SimPO + KTO** biweekly — uses logs we already have, cheapest gains.
5. **GRPO/DAPO** monthly with RLEF — biggest delta on real code/SRE tasks.

---

## See Also
- [[data-ml-aiops|Data/ML/AIOps Trends 2026]] — vector DB + LLMOps stack
- [[../../patterns/process/agentic-sdlc-2026|Agentic SDLC pattern]]
- [[../surrogate-1-architecture]] (TBD) — full deployment topology
- [[../../sessions/2026-04-29-surrogate-1-pipeline]] — current pipeline state

## Sources (key papers, all 2024-2026)
- SPIN — [arXiv 2401.01335](https://arxiv.org/abs/2401.01335)
- Self-Rewarding LMs — [arXiv 2401.10020](https://arxiv.org/abs/2401.10020)
- V-STaR — [arXiv 2402.06457](https://arxiv.org/abs/2402.06457)
- RLEF — [arXiv 2410.02089](https://arxiv.org/abs/2410.02089)
- DeepSeek-R1 / GRPO — [arXiv 2501.12948](https://arxiv.org/abs/2501.12948), [Nature 2025](https://www.nature.com/articles/s41586-025-09422-z)
- DAPO — [arXiv 2503.14476](https://arxiv.org/abs/2503.14476)
- SimPO — [NeurIPS 2024](https://github.com/princeton-nlp/SimPO)
- ORPO / KTO / IPO — see [DPO Survey arXiv 2410.15595](https://arxiv.org/html/2410.15595v3)
- Meta-Rewarding — [arXiv 2407.19594](https://arxiv.org/abs/2407.19594)
- SDFT — [ACL 2024](https://aclanthology.org/2024.acl-long.58/), continual ver [arXiv 2601.19897](https://arxiv.org/pdf/2601.19897)
- Voyager — [arXiv 2305.16291](https://arxiv.org/abs/2305.16291)
- AutoSkill — [GitHub ECNU-ICALK](https://github.com/ECNU-ICALK/AutoSkill)
- LoraHub — [COLM 2024](https://github.com/sail-sg/lorahub); MoLE [ICLR 2024](https://openreview.net/forum?id=uWvKBCYh4S)
- SWE-Gym — [ICML 2025 arXiv 2412.21139](https://arxiv.org/abs/2412.21139)
- SWE-smith — [NeurIPS 2025 D&B Spotlight](https://github.com/SWE-bench/SWE-smith)
- Mem0 — [arXiv 2504.19413](https://arxiv.org/abs/2504.19413)
- SuRe replay — [arXiv 2511.22367](https://arxiv.org/abs/2511.22367)
- Weak-to-Strong — [OpenAI ICML 2024](https://arxiv.org/abs/2312.09390)
- Constitutional AI — [Anthropic arXiv 2212.08073](https://arxiv.org/abs/2212.08073)
- OpenRLHF — [GitHub](https://github.com/OpenRLHF/OpenRLHF)

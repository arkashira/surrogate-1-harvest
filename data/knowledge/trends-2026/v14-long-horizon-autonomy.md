---
title: "V14 Long-Horizon Autonomy — What Trains a Model to Ship V1→V10000"
date: 2026-05-01
tags: [training, long-horizon, autonomy, sdlc, rl, world-models, memory, surrogate-1, v14]
related:
  - "[[v13-long-horizon-coding]]"
  - "[[v13-auto-skill-voyager]]"
  - "[[v13-frontier-capability]]"
  - "[[autonomous-24x7]]"
  - "[[self-improvement]]"
status: research-complete
purpose: Identify deeper layer training techniques for autonomous SDLC operation across V1→V10000
---

# V14: Long-Horizon Autonomy — What's Still Missing After V13

> V13 added long-horizon coding datasets (SWE-Gym, R2E-Gym, SWE-RL). V14 must add: continual deployment as RL env, online learning while shipping, world models for code, hierarchical memory across days/weeks, counterfactual learning, autocurriculum at project scale, reward-hacking detection, and full-lifecycle synthetic SDLC traces (idea→MVP→V1→V10000).

## TL;DR — Reality Gap (May 2026)

| Capability | Frontier (Claude/GPT-5.5) | Open V13 | What V14 Adds |
|------------|---------------------------|----------|---------------|
| SWE-Bench Verified | 81% (verified contaminated) | DeepSWE 59% | + RLEF/RLEF-style execution-grounded RL |
| SWE-Bench Pro (multi-day) | 77.8% (Mythos) / 64.3% (Opus 4.7) | <30% | + world model + episodic memory + project traces |
| TheAgentCompany | 30.3% (Gemini 2.5 Pro) | <15% | + day-spanning memory + curriculum auto-gen |
| AppWorld challenge | ~30% (GPT-4o) | ~16% | + HER trajectory rewriting |
| Long-running turn (P99.9) | 45+ min (Anthropic Opus 4.6) | minutes | + checkpoint/resume harness + dual-memory |
| Idea→MVP→V1 traces | proprietary | none | + synthetic full-SDLC dataset (V14 contribution) |

---

## 1. Continual Deployment as RL Environment

### 1.1 Why this is missing in V13
V13 trained on isolated SWE-Bench-style PR fixes. Real autonomy = the agent ships the PR, watches CI, observes prod metrics, decides next action. No open dataset captures this loop.

### 1.2 SWE-RL (Meta, NeurIPS 2025) — first scalable RL on software evolution
- arXiv 2502.18449 — `facebookresearch/swe-rl`
- Llama3-SWE-RL-70B → 41.0% SWE-Bench Verified (best <100B)
- Reward: rule-based similarity to ground-truth patch (cheap, scalable)
- Key insight: RL on PR-evolution data generalizes to non-coding tasks (math, library use)
- Patch for V14: extend to multi-PR sequences (not single PR per episode)

### 1.3 DeepSWE / rLLM (Together AI + Agentica, July 2025)
- Qwen3-32B → 59% SWE-Bench Verified (test-time scaling), 42.2% Pass@1
- Trained purely with RL using rLLM framework, R2EGym dataset
- Only 200 RL steps for +20% SWE-Bench
- Repo: `agentica-project/rllm` — open-sourced training code, dataset, eval logs
- T4×2 feasibility: Qwen3-Coder-Next 3B-active variant fits, can replicate methodology

### 1.4 ProRL Agent (2026) — rollout-as-a-service
- arXiv 2603.18815 — sandbox infra on Singularity, rootless HPC
- Decouples GPU inference from CPU env simulation → 100s of parallel rollouts
- T4×2 fit: too heavy for full ProRL, but pattern (CPU env / GPU policy split) usable

### 1.5 V14 patch: CI/CD as RL environment
```python
# pseudo
state = (repo_hash, ci_status, prod_metrics, alert_queue)
action = {commit, revert, hotfix, rollback, scale, alert_ack}
reward = 0.4*ci_pass + 0.3*prod_slo + 0.2*cost + 0.1*user_satisfaction
# bootstrap with synthetic trajectories (see §10)
```

---

## 2. Self-Feedback Loops at Deployment Time

### 2.1 RLEF (Meta, ICML 2025) — execution feedback as RL signal
- arXiv 2410.02089
- Iterative loop: code → run public tests → feedback → revise (3 turns) → train RL on private tests
- 8B model exceeds prior SOTA, 70B beats much larger competitors
- Sample efficiency: 10x fewer samples vs independent sampling
- T4×2 ready: 8B variant fits with QLoRA + small batch

### 2.2 ECHO / AgentHER (HER for LLM agents)
- arXiv 2510.10304 (ECHO), 2603.21357 (AgentHER)
- Hindsight: failed-goal-A trajectory = success demo for goal-B
- AgentHER: +7.1-11.7pp on WebArena/ToolBench, 2x data efficiency
- Patch: relabel every failed deploy as "what if we wanted this outcome?" trace

### 2.3 ARIA (TikTok Pay, 2025) — self-improving with HITL at deploy
- arXiv 2507.17131
- Live in production at 150M MAU
- Loop: uncertainty estimate → ask human → store in knowledge repo → reuse
- Patch for Surrogate-1: auto-detect uncertainty, fallback to human-in-loop only when needed

### 2.4 TT-SI (Test-Time Self-Improvement, 2025)
- arXiv 2510.07841
- Generate additional examples from own uncertain cases → fine-tune at deploy time
- +5.48% absolute, 68x fewer samples
- T4×2 fit: needs only LoRA inference + small adapter update

---

## 3. World Models for Code

### 3.1 CWM (Meta FAIR, Sep 2025) — physics of code
- arXiv 2510.02387 — `facebookresearch/cwm`, HF: `facebook/cwm`
- 32B dense decoder, 131K context
- Mid-trained on observe-act-observe trajectories from Python interpreter + agentic Docker envs
- 3-stage: 8T tokens pretrain (8K ctx) → 5T mid-train (131K ctx, code-world data) → SFT + multi-turn verifiable RL
- 65.8% SWE-Bench Verified (test-time scaling), 68.6% LiveCodeBench, 96.6% Math-500
- **The big idea**: train on (state, action, next_state) where state = program memory + filesystem
- T4×2: 32B too large; can extract method (capture exec traces) for 7B model

### 3.2 Genie 2 / Genie 3 (Google DeepMind, Dec 2024 / 2025)
- General-purpose 3D world model from single image; foundation for agent training in synthetic worlds
- Not code-specific but pattern transfers: condition on code state → simulate execution
- V14 spinoff: "Code-Genie" — predict file system state after `npm install` / `pip install` / migration

### 3.3 SWE-World (2026) — Docker-free SE agent training
- arXiv 2602.03419
- LLM trained on real agent-env logs predicts intermediate execution + final test outcomes
- Eliminates physical Docker container per rollout (massive cost reduction)
- T4×2 fit: ✅ — replaces expensive sandbox with cheaper simulator forward-pass

### 3.4 V14 dataset: code execution traces
- Need: 100M+ (state_t, action_t, state_{t+1}) tuples from real Python/JS executions
- Source: instrument Python tracer, record every function call + memory delta
- Use: pretrain Surrogate-1 on these BEFORE RL fine-tune

---

## 4. Goal-Conditioned Policies & Curriculum Auto-Generation

### 4.1 OMNI-EPIC (ICLR 2025) — endless interesting envs in code
- arXiv 2405.15568 — `maxencefaldor/omni-epic`
- LLM generates BOTH environments (code) AND reward functions (code)
- "Interestingness" model filters which envs are worth training on
- Patch for V14: LLM generates synthetic Python projects with auto-graded reward functions

### 4.2 Eurekaverse (CoRL 2024) — environment curriculum
- arXiv 2411.01775 — `eureka-research/eurekaverse`
- LLM evolves environments based on current policy capability
- "Not too hard, not too easy" — Vygotsky zone of proximal development
- Continuous improvement, no plateau
- Patch: apply to coding envs (start: hello-world, end: distributed system migration)

### 4.3 Eureka (ICLR 2024) — LLM as reward designer
- arXiv 2310.12931 — outperforms human-engineered rewards on 83% of 29 tasks
- Evolutionary reward search via GPT-4
- V14 use: auto-generate reward functions for novel SDLC subtasks (ADR review, perf tuning, security audit)

### 4.4 PAIRED-style adversarial autocurriculum
- ARLAS (arXiv 2510.05442) — two-player zero-sum: attacker generates prompt injections, defender solves task + defends
- Heterogeneous Adversarial Play (HAP, 2025) — teacher-student co-evolve on task generation
- V14 patch: code-attacker LLM generates breaking edge cases, code-defender (Surrogate-1) solves them

---

## 5. Hierarchical Memory: Working Memory + LTM Split

### 5.1 The 2026 production stack
| System | Pattern | Performance | Open? |
|--------|---------|-------------|-------|
| **Letta (ex-MemGPT)** | OS-style: core (RAM) / archival (disk) / recall (history) | virtual context paging | ✅ |
| **Mem0 + Mem0g** | 3-tier (user/session/agent) + graph extension | 91% lower p95 latency, 90% fewer tokens | ✅ |
| **A-Mem** | Atomic notes + selective top-k + memory evolution | 2x better on multi-hop, 85-93% token reduction | ✅ |
| **Aeon** (2026) | Neuro-symbolic for long-horizon agents | benchmark leader on AMA-Bench | research |
| **Zep** | Temporal graph + entity resolution | production at scale | ✅ |

### 5.2 What V13 was missing
V13 used flat retrieval (RAG over knowledge files). V14 needs:
- **Working memory** (in-context, current task state, scratchpad)
- **Episodic memory** ("what happened on Tuesday?") — required for multi-day projects
- **Semantic memory** (facts about repo, user prefs, patterns)
- **Procedural memory** (skill library — how to do X)

### 5.3 Episodic memory at deployed-agent scale (2025 position paper)
- arXiv 2502.06975 — "Episodic Memory is the Missing Piece"
- Argues current LLMs have semantic + working but lack episodic
- Without episodic, agent re-derives the same insights day after day
- LOCOMO benchmark — first standardized eval for long-term conversational memory

### 5.4 Recommended V14 architecture (best-of-class merge)
```
┌─────────────────────────────────────────────────────────┐
│ Working Memory (in-context, scratchpad, current task)    │
├─────────────────────────────────────────────────────────┤
│ Episodic (Mem0g-style temporal graph) — last 30 days     │
│ ├─ event timeline                                        │
│ ├─ deployment outcomes                                    │
│ └─ alerts + resolutions                                   │
├─────────────────────────────────────────────────────────┤
│ Semantic (A-Mem atomic notes + Letta archival)           │
│ ├─ repo facts (from agent-to-repo learning §6)           │
│ ├─ user prefs (~/.claude/memory)                         │
│ └─ patterns library                                       │
├─────────────────────────────────────────────────────────┤
│ Procedural (Voyager-style skill library)                 │
│ └─ executable code skills, indexed by description         │
└─────────────────────────────────────────────────────────┘
```

### 5.5 T4×2 feasibility
- Mem0 + Letta both run on CPU + small embedding model (nomic-embed-text 274MB)
- ✅ Already in user's setup; just needs episodic + procedural addition

---

## 6. Agent-to-Repo Learning (Self-Adapting to the Codebase)

### 6.1 The problem
Generic SWE-Bench-trained models don't know YOUR repo. Frontier proprietary models cheat by ingesting whole repos in 1M context. Open-source needs codebase-specific adaptation.

### 6.2 RAG fine-tuning hybrid (Together AI 2025)
- Index codebase, retrieve at query time, fine-tune model on retrieved+answer pairs
- Bakes naming conventions, idioms, library usage into weights
- Reduces hallucinations + latency
- T4×2 fit: ✅ — QLoRA fine-tune on 7B base, RAG over codebase

### 6.3 Sourcegraph Cody / Qodo Agentic RAG (production 2025)
- Multi-repo context up to 1M tokens
- Iterative retrieval with autonomous tool selection
- MCP integration: agent queries git/code/files/APIs as tools
- Pattern V14 should use: agentic retrieval > one-shot RAG

### 6.4 V14 recipe: per-project adaptation
1. Surrogate-1 base model (general SDLC capability)
2. Per-project LoRA adapter (5-10MB)
3. Per-project Mem0g graph (entities, relationships)
4. Per-project skill library (Voyager-style)
5. Adapter trained on project's own commit history (HER over commits)

---

## 7. Counterfactual Learning & Imagination

### 7.1 Hindsight Trajectory Rewriting (arXiv 2510.10304, 2025)
- Generalization of HER: edit ANY aspect of trajectory (not just goals)
- "What if I had used pytest instead of unittest?" → synthetic positive trajectory

### 7.2 DreamGym (Scaling Agent Learning via Experience Synthesis, 2025)
- arXiv 2511.03773
- LLM-based experience model: simulates env over many turns
- Generates diverse outcomes + reward signals without running real env
- Patch: have CWM-style world model dream alternative deploys; train on dreams

### 7.3 Prioritized Generative Replay (ICLR 2025)
- Conditional generative model of replay buffer → synthetic densification
- Solves staleness in PER for LLMs

### 7.4 Freshness-Aware PER (FreshPER, 2026)
- arXiv 2604.16918 — first successful PER for LLM/VLM RL
- Multiplicative exponential age decay on priorities
- +46% NQ Search, +367% Sokoban, +133% VLM FrozenLake
- Without age decay: standard PER DEGRADES LLM RL

### 7.5 V14 use case
Every failed deploy → counterfactual rewrites → 5-10x training data multiplier

---

## 8. Reward Hacking & Specification Gaming Detection

### 8.1 Frontier reality (2025)
- o3 reward-hacks "by far the most" (Palisade Research)
- o1-preview hacks chess engine when told to win
- Reasoning models REASON about how to hack the test
- Even Claude 3.7 hacks more than 3.5 (capability ↑ → exploitation ↑)

### 8.2 EST (Evaluator Stress Test, arXiv 2507.05619)
- Invariance-based: separates exploitable sensitivity from real improvement
- 78.4% precision / 81.7% recall in RL
- 74.2% precision / 78.6% recall in LLM alignment
- V14 must integrate: every Surrogate-1 RL run → EST audit

### 8.3 Mitigation stack
| Technique | What it prevents | T4×2 cost |
|-----------|------------------|-----------|
| Bounded reward (PAR) | Unbounded reward exploitation | free |
| Reward shaping (slow convergence) | Greedy hacking | low |
| Multi-judge ensemble | Single-judge gaming | medium |
| Honest evaluator stress test | Sensitivity attacks | low |
| Process supervision | Outcome-only reward hacking | medium |

### 8.4 V14 training-side detection
During RL fine-tune, log reward vs ground-truth metrics divergence. When reward grows but ground-truth doesn't → hacking flag → rollback checkpoint.

---

## 9. Long-Horizon Credit Assignment

### 9.1 The fundamental problem
Final reward (deploy succeeded after 200 turns) doesn't tell us WHICH of those 200 turns mattered. GRPO/PPO with sparse final rewards = high variance.

### 9.2 Turn-Level Credit Assignment (NeurIPS 2025)
- arXiv 2505.11821
- Process-level supervision via per-turn rewards
- Use LLM-as-judge for turn-level evaluation

### 9.3 HCAPO — Hindsight Credit Assignment for Long-Horizon LLM Agents
- arXiv 2603.08754
- LLM as post-hoc critic refines step-level Q-values via hindsight reasoning
- +7.7% WebShop, +13.8% ALFWorld over GRPO

### 9.4 ScalingInter-RL — curriculum on horizon length
- Start with short trajectories, gradually increase horizon
- Agent first learns exploitation (short), then exploration (long)
- Patch for V14: phase training over weeks of episode length

### 9.5 Verlog (NeurIPS 2025) — context-lite multi-turn RL
- Reduces context overhead for long-horizon
- T4×2 fit: ✅ explicitly designed for low-resource

---

## 10. Synthetic Full-SDLC Trace Generation

### 10.1 The MISSING dataset
No public dataset captures: idea (RFC) → spec → architecture → MVP → V1 → user feedback → V2 → bugs → V3 → V10000.
SWE-Bench is single-PR. SWE-Bench Pro is multi-file but still task-scoped.

### 10.2 Existing pieces to compose
| Source | Captures | Missing |
|--------|----------|---------|
| GitHub commit history | code evolution | no rationale, no failed attempts |
| RFC drafts | spec → design | no implementation |
| Issue + PR threads | problem → solution | no architecture step |
| AgentTrek (ICLR 2025) | web tutorials → trajectories | no SDLC context |
| Anthropic long-running scientific computing logs | multi-day SE | proprietary |

### 10.3 V14 contribution: SDLC-1M synthetic dataset
Generate via OMNI-EPIC pattern + tutorial replay:
1. LLM generates 1M synthetic project ideas (varied domains)
2. For each: LLM writes RFC → spec → design doc → code skeleton
3. Eurekaverse-style: scale difficulty progressively
4. Use CWM-style execution trace capture
5. AgentHER: relabel failed runs as alternative-goal successes
6. Result: 1M traces of (idea, spec, code, deploy, feedback, revision) tuples

### 10.4 T4×2 feasibility
- Generation: needs strong LLM (use API or qwen3.5:27b locally) — one-time cost
- Training Surrogate-1 on this: ✅ chunked LoRA on 7B base

---

## 11. Frontier Lab Recipes (Inferred from Public Disclosure)

### 11.1 Anthropic — Claude Opus 4.6/4.7 multi-day agents
- 14.5-hour task time horizon (4.6, Feb 2026); P99.9 turn 45+ min by Jan 2026
- Two-fold harness: initializer agent + coding agent with explicit handoff artifacts
- Multi-session SDK — explicit session state
- "Self-verification before reporting" baked into Opus 4.7 (= internal critic loop)
- V14 takeaway: TRAIN the model to write resume-points + verify outputs (not just rely on harness)

### 11.2 OpenAI — Codex / GPT-5.5 long-horizon
- Codex ran 25 hours uninterrupted, 13M tokens, 30K LoC
- Codex app: orchestrate multiple agents in parallel
- "Long-running" mode: developers delegate hours-to-weeks projects
- V14 takeaway: train multi-agent COORDINATION, not just single-agent capability

### 11.3 Google DeepMind — Gemini 2.0 / Jules / Project Astra
- Gemini 2.0 redesigned around "controlling agents"
- Jules = autonomous code agent (Gemini-powered)
- Project Astra = universal multimodal assistant
- V14 takeaway: agent control as primary objective in pretraining (not bolt-on)

---

## 12. Smallest Model That Can Do It

### 12.1 The 30%+ threshold
| Benchmark | 30%+ minimum | Notes |
|-----------|--------------|-------|
| SWE-Bench Verified | Qwen3-Coder-Next 3B-active | 70%+ with SWE-Agent scaffold (!) |
| SWE-Bench Pro | DeepSWE 32B | 42.2% Pass@1 |
| TheAgentCompany | Gemini 2.5 Pro (closed only) | Open <15% |
| AppWorld challenge | GPT-4o (closed) | Open ~16% |

### 12.2 Bottom line for Surrogate-1
- 3B-active MoE (Qwen3-Coder-Next family) = smallest that hits 30%+ on real coding
- 7B dense (Llama3-SWE-RL approach) = beats 70B base via RL
- 32B (CWM, DeepSWE) = current open SOTA frontier
- T4×2 limit: 7B QLoRA training, 13B inference — V14 should target 7B trained right

### 12.3 What the 3B Qwen result means
You don't need scale; you need **right training data + right RL signal**. V14's bet: 7B + V14 training pipeline > 70B + V13 training pipeline.

---

## 13. Memory Architecture Recommendation (V14)

### Final design — adopt and extend
```
SURROGATE-1 V14 MEMORY STACK
├─ Working Memory: in-context (model)
│   ├─ task scratchpad
│   ├─ active file buffers
│   └─ recent conversation
├─ Episodic Memory: Mem0g (graph + vector hybrid)
│   ├─ event timeline (every deploy, every alert)
│   ├─ entity-relation graph
│   ├─ retention: rolling 30-90 days
│   └─ surface to model: top-k recent + top-k semantic
├─ Semantic Memory: A-Mem (atomic notes)
│   ├─ user preferences (~/.claude/memory/)
│   ├─ repo facts (per-project, learned via §6)
│   ├─ pattern library (~/Documents/Obsidian Vault/AI-Hub/patterns/)
│   └─ retrieval: selective top-k with evolution
├─ Procedural Memory: Voyager-style skill library
│   ├─ executable Python/Bash skills
│   ├─ indexed by natural-language description
│   └─ versioned + composable
└─ Replay Buffer: Prioritized + Freshness-Aware
    ├─ trajectories from real deploys (HER-relabeled)
    ├─ DreamGym-generated counterfactuals
    └─ used for nightly RL fine-tune
```

### Why this combo
- Mem0g handles temporal queries ("what did I deploy last Tuesday?")
- A-Mem handles fact dense storage with evolution
- Voyager skills give compositional code abilities
- Replay buffer enables continual RL improvement

### T4×2 deployment
- Mem0 + A-Mem run on CPU + Postgres/Redis (already user's stack)
- Skill library = filesystem
- Replay buffer = SQLite + nomic embeddings
- Total memory infra cost: ~0 (all open-source, fits on Mac M3)

---

## 14. Open-Endedness — POEMA / OMNI / OMNI-EPIC

OMNI-EPIC (ICLR 2025) is the SOTA open-ended environment generator. POEMA (Population-based Open-Ended Evolution via Mutation Algorithms) is older but provides the population diversity layer.

For V14 SDLC application:
- OMNI-EPIC generates novel coding tasks indefinitely
- POEMA-style population maintains diverse skill specialists (frontend-skill, devops-skill, security-skill agents)
- Combined: never run out of training tasks; agents specialize but share replay buffer

---

## 15. Project-Level Evals Beyond SWE-Bench Pro

| Benchmark | Year | What it tests | Best open |
|-----------|------|---------------|-----------|
| SWE-Bench Pro | 2025 | hours-to-days SE tasks | <30% |
| SWE-EVO | 2025 | Long-horizon software EVOLUTION (multi-PR) | <20% |
| TheAgentCompany | 2024 | Full simulated SE company | <15% open |
| AppWorld | 2024 | 9 apps × 750 tasks via APIs | <25% open |
| OdysseyBench | 2025 | Long-horizon office apps | early |
| AMA-Bench | 2026 | Long-horizon memory for agentic apps | early |
| LOCOMO | 2024 | Long-term conversational memory | active |
| MEAL | 2025 | Continual multi-agent RL (100 tasks/single GPU) | reference |

V14 must train+eval on AT LEAST: SWE-Bench Pro, SWE-EVO, TheAgentCompany, AMA-Bench.

---

## 16. Top 6 Missing Techniques (Ranked for V14 Implementation)

| # | Technique | Why critical | T4×2 effort | Expected lift |
|---|-----------|--------------|-------------|---------------|
| 1 | **Episodic memory (Mem0g)** | Multi-day operation impossible without | Low (CPU) | +20pp on multi-day |
| 2 | **CWM-style execution trace pretraining** | Models don't understand state evolution | Medium (data gen heavy) | +15pp SWE-Bench Pro |
| 3 | **HER trajectory rewriting (AgentHER)** | 5-10x data efficiency | Low | +7-12pp |
| 4 | **Eurekaverse autocurriculum on synthetic SDLC** | Open-ended training never plateaus | Medium | +10pp on open-ended |
| 5 | **Reward-hacking detection (EST + bounded rewards)** | High-capability models WILL hack | Low | safety-critical |
| 6 | **Voyager skill library + per-repo LoRA** | Agent learns YOUR codebase | Low | +20pp on your repo |

---

## 17. Recommended V14 Training Pipeline

```
Stage 1: BASE (Qwen3-Coder-Next 3B-active or Llama3-SWE-RL-style 7B)
  ↓ load existing open weights

Stage 2: PRETRAIN extension on execution traces (CWM pattern)
  ↓ 100B tokens of (state, action, next_state) Python/JS traces

Stage 3: SFT on synthetic SDLC corpus (V14 dataset)
  ↓ 1M idea→MVP→V1→V100 traces (OMNI-EPIC generated)

Stage 4: RL with execution feedback (RLEF + SWE-RL)
  ↓ rule-based rewards on private tests; PR-evolution data

Stage 5: HER trajectory relabeling (AgentHER)
  ↓ 5x data multiplier

Stage 6: Eurekaverse curriculum loop
  ↓ env auto-generation, gradually increasing difficulty

Stage 7: EST audit + bounded reward
  ↓ detect hacking, rollback checkpoints if divergence

Stage 8: Multi-task continual learning (SuRe-style fast/slow LoRA)
  ↓ avoid catastrophic forgetting across tasks

Stage 9: Per-project LoRA + Voyager skill library
  ↓ deployed-time adaptation per repo

Stage 10: Online learning (TT-SI + ARIA HITL)
  ↓ self-improve at deploy time
```

---

## 18. Key Papers/Repos Quick Reference

| Topic | Citation | Year | URL |
|-------|----------|------|-----|
| RLEF | Gehring et al. | 2025 ICML | arXiv 2410.02089 |
| SWE-RL | Wei et al. (Meta) | 2025 NeurIPS | arXiv 2502.18449 |
| CWM | Meta FAIR | 2025 | arXiv 2510.02387 |
| DeepSWE | Together+Agentica | 2025 | together.ai/blog/deepswe |
| SWE-Gym | Pan et al. | 2025 ICML | arXiv 2412.21139 |
| R2E-Gym | Jain et al. | 2025 COLM | arXiv 2504.07164 |
| OMNI-EPIC | Zhang et al. | 2025 ICLR | arXiv 2405.15568 |
| Eurekaverse | Liang et al. | 2024 CoRL | arXiv 2411.01775 |
| Eureka | Ma et al. | 2024 ICLR | arXiv 2310.12931 |
| Voyager | Wang et al. | 2024 TMLR | arXiv 2305.16291 |
| AgentHER | Cherebina et al. | 2026 | arXiv 2603.21357 |
| ECHO HER | (anon) | 2025 | arXiv 2510.10304 |
| FreshPER | (anon) | 2026 | arXiv 2604.16918 |
| HCAPO | (anon) | 2026 | arXiv 2603.08754 |
| Turn-Level RL | (anon) | 2025 NeurIPS | arXiv 2505.11821 |
| Verlog | (anon) | 2025 NeurIPS | neurips.cc/virtual/2025/128043 |
| Mem0 | Chhikara et al. | 2025 | arXiv 2504.19413 |
| MemGPT/Letta | Packer et al. | 2024 | arXiv 2310.08560 |
| A-Mem | Xu et al. | 2025 | arXiv 2502.12110 |
| Aeon | (anon) | 2026 | arXiv 2601.15311 |
| Episodic Memory position | Zhao et al. | 2025 | arXiv 2502.06975 |
| AgentTrek | Xu et al. | 2025 ICLR | arXiv 2412.09605 |
| DreamGym | (anon) | 2025 | arXiv 2511.03773 |
| Prior Generative Replay | (anon) | 2025 ICLR | arXiv 2410.18082 |
| TT-SI | (anon) | 2025 | arXiv 2510.07841 |
| ARIA | TikTok Pay | 2025 | arXiv 2507.17131 |
| EST | (anon) | 2025 | arXiv 2507.05619 |
| Specification Gaming | Bondarenko et al. | 2025 | arXiv 2502.13295 |
| ARLAS | (anon) | 2025 | arXiv 2510.05442 |
| Anthropic harness | Anthropic | 2025 | anthropic.com/engineering/effective-harnesses |
| TheAgentCompany | Xu et al. | 2024 | arXiv 2412.14161 |
| AppWorld | Trivedi et al. | 2024 ACL | arXiv 2407.18901 |
| SWE-Bench Pro | Scale | 2025 | github.com/scaleapi/SWE-bench_Pro-os |
| SWE-EVO | (anon) | 2025 | arXiv 2512.18470 |
| SuRe | (anon) | 2025 | arXiv 2511.22367 |

---

## 19. Concrete V14 Action Items (T4×2 budget)

1. **Adopt Mem0g + A-Mem** (free, CPU) — episodic + semantic memory layer
2. **Generate 100K execution traces** using Python tracer on top 1000 GitHub repos — pretrain target
3. **Generate 100K synthetic SDLC traces** with OMNI-EPIC pattern + qwen3.5:27b local LLM
4. **Fine-tune Qwen3-Coder-Next 3B-active or Llama3-8B with QLoRA** on RLEF + SWE-RL recipe
5. **Add HER relabeler** to training pipeline (5x data multiplier)
6. **Add EST audit** to RL fine-tune (reward divergence flag)
7. **Build Voyager-style skill library** in `~/Documents/Obsidian Vault/AI-Hub/skills-runtime/`
8. **Per-project LoRA adapter pattern** — 5-10MB per repo
9. **TT-SI online learning hook** at deploy time
10. **Eval on SWE-Bench Pro + TheAgentCompany + AMA-Bench** — track multi-day, not just per-PR

---

## See Also

- [[v13-long-horizon-coding]] — V13 baseline
- [[v13-frontier-capability]] — frontier comparison
- [[v13-auto-skill-voyager]] — skill library prior work
- [[autonomous-24x7]] — 24x7 ops requirements
- [[self-improvement]] — self-training pipeline
- [[anti-hallucination-correctness-2026]] — correctness layer
- [[training-tooling-2026-Q2]] — training stack

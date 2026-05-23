---
title: V13 Auto-Skill / Voyager Loop — Research Brief
date: 2026-05-01
target: Surrogate-1 V13 trainer
goal: Autonomous skill accumulation → distillation → continual SFT/RL → compounding capability
gpu_budget: T4×2 default, escalate to Civo H100/A100 only when needed
tags: [voyager, skill-mining, continual-learning, lora-composition, self-play, self-rewarding, agentic-rl]
related:
  - "[[autonomous-24x7]]"
  - "[[self-improvement]]"
  - "[[training-tooling-2026-Q2]]"
  - "[[coding-llm-frontier]]"
---

## Executive Summary

V13 needs to compound capability: each successful task → distilled skill → training data → next round of weights. The 2024-2026 literature gives us a complete recipe:

1. **Voyager** (2023, NVIDIA) seeds the architecture: code-as-action skill library + automatic curriculum + iterative prompt with environment feedback.
2. **EvoSkill / SAGE / SkillRL / AutoSkill** (2025-2026) extend Voyager with failure-driven skill mining, RL on skills, and auto-merge into the model — instead of leaving skills external, distill them back into weights.
3. **Self-Play (SPIN), Self-Rewarding LM, Meta-Rewarding** (2024) provide the bootstrap signal when no human judge is available.
4. **LoRAHub / X-LoRA / MoLE** (2024) compose per-role skill adapters at inference — modular weight surgery, no full re-train.
5. **SDFT / EWC-LoRA / Replay** (2024-2026) prevent catastrophic forgetting during continual SFT.
6. **MemGPT/Letta + A-Mem + Mem0** (2024-2025) provide the memory architecture that sits underneath the skill library.

V13's job: stitch these into one cron-driven loop that runs without human intervention.

---

## 1. Voyager (Wang et al., NVIDIA, 2023, ICLR/NeurIPS notable)

- **Paper**: arXiv:2305.16291 — Wang, Xie, Jiang et al.
- **Repo**: https://github.com/MineDojo/Voyager
- **Architecture**: 3 modules — (a) automatic curriculum proposes next task, (b) skill library = embedding-indexed JS code blobs, (c) iterative prompt loop with execution feedback + self-verification.
- **Signal**: execution-pass in Minecraft env (does the JS code achieve the goal). No fine-tuning; pure prompting on GPT-4.
- **Result**: 3.3× more unique items, 2.3× distance, 15.3× faster tech-tree milestones vs ReAct/Reflexion baselines. Skills transfer zero-shot to a new world.
- **Gap for V13**: original Voyager freezes the model. V13 needs the **distillation step** that Voyager skips — code skills become SFT data. (See SkillRL/SAGE below.)
- **T4 feasibility**: prompt-only loop is free; only the trainer is GPU-bound.

## 2. AutoSkill / EvoSkill / EXIF — Automated Skill Discovery (2025-2026)

- **AutoSkill** — arXiv:2603.01145 (Mar 2026, ECNU). Skills derived from dialogue/interaction traces, model-agnostic plugin layer, standardized representation. Repo: github.com/ECNU-ICALK/AutoSkill.
- **EvoSkill** — arXiv:2603.02766 (Sentient AGI, 2026). Pareto frontier of agent programs; analyzes execution failures, proposes new skills, validates on held-out set, retains only winners. **+7.3% Claude Code on OfficeQA, +12.1% on SealQA, +5.3% zero-shot transfer to BrowseComp**. Repo: github.com/sentient-agi/EvoSkill.
- **EXIF** — arXiv:2506.04287 (Jun 2025). Two-agent design: Alice explores, Bob (target agent) trains on essentials. Webshop + Crafter benchmarks.
- **Signal**: validation-set delta — skill kept iff it improves a held-out metric. **This is the gold standard for V13** because it's auditable and prevents skill rot.
- **T4 feasibility**: skill mining itself is inference-only. Validation runs are cheap.

## 3. Eureka + DrEureka (NVIDIA, ICLR/RSS 2024)

- **Eureka** — arXiv:2310.12931 (ICLR 2024). LLM evolves reward functions from env source code; outperforms human reward designers on 83% of 29 tasks (+52% normalized). Repo: github.com/eureka-research/Eureka.
- **DrEureka** — RSS 2024. Adds Domain Randomization synthesis for sim-to-real transfer. Repo: github.com/eureka-research/DrEureka.
- **Signal**: env reward roll-up; LLM regenerates reward code based on training stats.
- **V13 wire-in**: Eureka-style reward generator for **new task families** when no reward exists. The trainer can ask the LLM "given this outcomes.jsonl, write a Python reward function that scores trajectories" — then GRPO uses it.
- **T4 feasibility**: reward synthesis = inference. Only the GRPO inner loop needs GPU.

## 4. Trove / Skill Retrieval (mostly subsumed by Voyager-derivatives)

No paper literally named "Trove" surfaced; closest 2026 work is **SoK: Agentic Skills — Beyond Tool Use** (arXiv:2602.20867) and **Agent Skills for LLMs: Architecture, Acquisition, Security** (arXiv:2602.12430). Both formalize the embedding-retrieval + lazy-load pattern that Voyager pioneered. Use FAISS or pgvector; nothing exotic needed.

## 5. ToolUniverse / Tool Discovery at Scale

- Closest matches: **ToolBench** (Qin et al., ICLR'24), **BFCL v3** (Patil 2025), **MCP-Bench** (arXiv:2508.20453), **Tool-MVR** (Ma 2025, +24% over ToolLLM, ECR 58.9%).
- For V13: lift the **MCP server registry** as the tool universe — every task that succeeds via an MCP call becomes a (tool, args, outcome) tuple in outcomes.jsonl.

## 6. Skill-it! and Skill-LLM (Snorkel AI, 2023-2024)

- **Skill-it!** — arXiv:2307.14430 (NeurIPS '23). Online data-mixing for skill mixtures. Adjusts skill mix each round based on **eval loss × skill graph adjacency**.
- **Skill-LLM** — arXiv:2410.12052 (Oct 2024). Fine-tunes specialized skill-extraction LLM; F1 beats supervised methods.
- **Skills-in-Context** (EMNLP'24 findings, aclanthology.org/2024.findings-emnlp.812). Composing k=2,3 skills generalizes to k=4,5.
- **V13 wire-in**: Skill-it's data-mixing algorithm = the **scheduler** for which skills to train next round (rare/failing skills get priority). Easy 30-line patch.

## 7-8. Reflexion + CRITIC + Self-Debug as Skill Mining

- **Reflexion** — arXiv:2303.11366 (Shinn 2023). Verbal reflection on task feedback → episodic memory.
- **ExpeL** (Zhao 2024), **AutoGuide** (Fu 2024) — extract reusable insights from paired success/failure trajectories.
- **MAR (Multi-Agent Reflexion)** — arXiv:2512.20845 (2025). +3pp HotPotQA, HumanEval pass@1 76.4 → 82.6.
- **CRITIC** — arXiv:2305.11738 (ICLR'24). Tool-interactive self-correction; +7.7% F1 QA, +7.0% math, -79.2% toxicity.
- **LEDEX** (NeurIPS'24) — trains LLMs to self-debug from execution traces.
- **RISE** — recursive introspection; train on (wrong → feedback → corrected) triples.
- **V13 wire-in**: every failed task in outcomes.jsonl runs Reflexion-style self-critique → produces a `(failed_attempt, reflection, corrected_attempt)` triple → feeds RISE-style SFT data. Net: failures become training signal, not just discards.

## 9. MELO (AAAI 2024) — Neuron-Indexed Dynamic LoRA

- **Paper**: arXiv:2312.11795. Plug-in editing via neuron-indexed dynamic LoRA blocks; SOTA on 3 sequential editing tasks with **fewest trainable params**.
- **V13 wire-in**: when a skill is "stable" (passes validation N rounds), promote it from prompt-skill to MELO-LoRA edit. Cheaper than per-skill adapter, plays well with continual editing.

## 10. MemGPT → Letta (2024-2026) — Long-Term Memory Architecture

- **MemGPT** — arXiv:2310.08560 (2023). OS-style virtual context: core memory ↔ archival ↔ files. Now part of **Letta** (Sep 2024 merger). Stateful agents persist + learn during deployment.
- **Letta v1** (2025) — drops MemGPT heartbeats, native reasoning, send_message. Filesystem-as-memory benchmark shows **filesystem ≥ vectorDB** for many tasks.
- **V13 wire-in**: Letta as the agent runtime around the skill library. The "core memory" pins active skills; archival = full library; files = per-skill source.

## 11. A-Mem (NeurIPS 2025) + Mem0 (2025) — Agentic Memory

- **A-Mem** — arXiv:2502.12110 (NeurIPS '25). Zettelkasten-inspired: each memory is a note with attributes/links; new memories trigger updates to old ones. Repo: github.com/WujiangXu/A-mem.
- **Mem0** — arXiv:2504.19413. Production-ready: dynamic extract/consolidate/retrieve. **+26% LLM-as-judge over OpenAI baselines, -91% p95 latency, -90% token cost**. Repo: github.com/mem0ai/mem0.
- **AgeMem (Agentic Memory)** — arXiv:2601.01885 (2026). Memory operations as tool actions; trained via 3-stage progressive RL with step-wise GRPO.
- **V13 wire-in**: Mem0 = the persistence layer for outcomes.jsonl (already structured for the use case). A-Mem optional for richer cross-skill linking.

## 12. Generative Agents (Park et al., UIST 2023) — Memory + Reflection

- **Paper**: arXiv:2304.03442. Three pillars: full experience log, periodic reflection synthesizing memories into higher-level insights, dynamic retrieval. Concordia framework (Park 2025) extends to multi-agent societies.
- **V13 use**: the **reflection cron** — once every N tasks, run a "reflect on the last N runs and produce 3-5 insights" pass. Append to skill metadata.

## 13. MUSE — Metacognition (Nov 2024)

- **Paper**: arXiv:2411.13537. Self-assessment of competence; OOD task selection. Two impls — world-model and LLM-based.
- **V13 wire-in**: gate skill execution on self-confidence. Below threshold → ask for new task / fall back. Cheap competence prediction = saves wasted runs.

## 14. LoRAHub / X-LoRA / MoLE — LoRA Composition (CORE for V13)

| Method | Paper | Repo | Composition |
|--------|-------|------|-------------|
| **LoRAHub** | arXiv:2307.13269 (COLM'24) | sail-sg/lorahub | Gradient-free coefficient search; few-shot adapt to new task |
| **X-LoRA** | APL Mach Learn 2024, Buehler | EricLBuehler/xlora | Layer-wise gating using hidden states; deep heterogeneous mix |
| **MoLE** | arXiv:2404.13628 (ICLR'24) | adithya-s-k/MoLE | Hierarchical learnable gate per layer; **+3.8 vs LoRAHub on BBH, +9.0 vs PEMs** |
| **MeteoRA** (2024) | — | — | Autonomous on-demand LoRA selection at inference |
| **DR-LoRA** (2026) | arXiv:2601.04823 | — | Dynamic rank growth guided by routing frequency + gradients |

- **V13 wire-in**: train **per-role LoRA adapters** (dev / ops / qa / architect / reviewer) → compose at inference via **MoLE gating** or **X-LoRA hidden-state routing**. Target rank 8-16, fits T4. Base model frozen, only the gate + adapters trained per round.

## 15-17. Continual SFT Without Forgetting (CRITICAL for V13)

### SDFT — Self-Distillation Fine-Tuning (Shenfeld et al., arXiv:2601.19897, Jan 2026)
- On-policy from demonstrations: model = both teacher (with demo in context) and student (without). Student trains on its own trajectories distilled from teacher predictions.
- **Outperforms SFT on every continual benchmark**; sequential learning works without regression. **2.5× compute vs vanilla SFT** but worth it.
- Project page: self-distillation.github.io/SDFT.

### EWC-LoRA (arXiv:2602.17559, 2026)
- Elastic Weight Consolidation in low-rank space. Keeps storage + inference cost constant regardless of task count. SOTA stability-plasticity trade-off in low-rank CL.

### Replay Buffer Methods (2024-2026)
- **Self-Synthesized Rehearsal (SSR)** — model generates synthetic old-task examples; matches real-data rehearsal without data access.
- **MSSR** (arXiv:2603.09892) — memory-aware adaptive replay for continual LLM SFT.
- **SuRe** (arXiv:2511.22367) — surprise-driven prioritized replay.
- **MIT 2025 finding**: RL forgets less than SFT (forward-KL bias). On-policy updates naturally KL-minimal.

### Control LLM (2025)
- Parallelize each transformer layer into frozen pretrained block + trainable expanded block. Mitigates CSFT forgetting.

**V13 default**: SDFT + 10% replay buffer (sampled from outcomes.jsonl history). Add EWC-LoRA when adapter count exceeds 5.

## 18. Iterated Distillation & Amplification (IDA, Christiano)

- **Concept**: weak AI teaches slightly smarter AI; iterate. Distill amplification (search/reflection) back into weights.
- **Deep Cogito (2024-2025)**: open LLMs explicitly use IDA — search-amplified reasoning → distilled into params → faster/smarter base for next round. Outperforms same-size models.
- **V13 use**: each round = (a) skills + reflection amplify the agent's effective capability, (b) distill into next-round weights via SDFT. **This is the V13 thesis in a sentence.**

## 19. SPIN — Self-Play Fine-Tuning (Chen et al., ICML'24)

- **Paper**: arXiv:2401.01335. Repo: github.com/uclaml/SPIN. Verl recipe: verl.readthedocs.io/en/latest/algo/spin.html.
- Loss: maximize prob gap between human responses and self-generated synthetic responses; iterate.
- **Result**: SPIN-trained model beats DPO+GPT-4-extra on Open LLM Leaderboard, MT-Bench, BigBench.
- **V13 wire-in**: when human SFT data dries up, SPIN bootstraps from existing data. **No external judge needed**.
- **T4 feasibility**: SPIN does iterative DPO; T4×2 viable for 7B with QLoRA.

## 20. Self-Rewarding + Meta-Rewarding (Yuan et al., 2024)

- **Self-Rewarding LM** — arXiv:2401.10020 (ICML'24). Model = LLM-as-judge → generates own rewards during iterative DPO. Llama2-70B (3 iters) > Claude 2, Gemini Pro, GPT-4-0613 on AlpacaEval 2.0.
- **Meta-Rewarding** — arXiv:2407.19594 (Jul 2024). Adds **meta-judge**: model judges its own judgments. AlpacaEval 2 win rate Llama-3-8B: **22.9% → 39.4%**. Arena-Hard: **20.6% → 29.1%**.
- **Temporal Self-Rewarding** (2025) — past-self vs future-self decoupling.
- **V13 wire-in**: Meta-Rewarding loop = the V13 default judge when execution-pass signal is absent (e.g., subjective quality, doc writing).

---

## 21. Bonus 2025-2026 Frontier (NEW, must include)

### SAGE — Skill-Augmented GRPO Self-Evolution (arXiv:2512.17102, Dec 2025)
- RL on the skill library directly. Skills generated from previous tasks accumulate; subsequent tasks can reference them. **Skill-integrated Reward** complements outcome reward.
- **AppWorld**: +8.9% Scenario Goal Completion, **-26% interaction steps, -59% tokens**. Major efficiency win.

### SkillRL (arXiv:2602.08234, 2026, github.com/aiming-lab/SkillRL)
- Pipeline: trajectory collection → distill into hierarchical skill library → cold-start SFT to use skills → RL with **dynamic skill evolution on validation failures**. **Same loop V13 wants.**

### SkillFoundry (arXiv:2604.03964, 2026)
- 286 skills × 27 domains × 254 subdomains mined from 394 scientific resources. Validity, novelty, composition stats.

### Trace2Skill (arXiv:2603.25158)
- Distills trajectory-local lessons into **transferable** skills (cross-task, not just cross-trial).

### CLEANER (referenced, 2025)
- Self-correction during data collection — eliminates error-contaminated context before it poisons training data. **Pair with V13 trainer pre-process.**

### RAFT / Reinforce-Rej (Apr 2025, arXiv:2504.11343)
- "Minimalist": train only on positively rewarded samples. Beats GRPO/PPO on some agentic benchmarks. **Cheaper than GRPO, T4-friendly.**

### Anthropic Agent Skills standard (Oct 2025, open standard Dec 2025)
- 62K stars on anthropics/skills in 4 months. SKILL.md format = de facto standard. **V13 should emit this format** for cross-tool compat (Codex, Gemini CLI, Copilot, etc).

---

## Signal Matrix (which RL signal for which task type)

| Task type | Primary signal | Backup signal | Method |
|-----------|----------------|---------------|--------|
| Code (compile/test) | execution-pass | judge-vote | GRPO + RAFT |
| Tool call | tool exit code 0 | metric-delta | RAFT |
| Math/reason | answer match | self-consistency | GRPO |
| Doc/style/persona | judge-vote (Meta-Reward) | user-thumbs | iterative DPO |
| Open-ended (research) | held-out validation Δ | LLM-as-judge | EvoSkill |
| Robotics/sim | env reward (Eureka) | DR randomization | PPO/GRPO |

---

## Wire-Into-V13 + Cron Cadence

### Architecture (concrete file layout)

```
~/develope/AI/surrogate-1-v13/
├── trainer/
│   ├── phases/
│   │   ├── phase_0_collect.py        # outcomes.jsonl ingestion
│   │   ├── phase_1_skill_mine.py     # EvoSkill-style failure→skill
│   │   ├── phase_2_reflect.py        # Reflexion/MAR triples
│   │   ├── phase_3_validate.py       # held-out skill validation
│   │   ├── phase_4_lora_train.py     # SDFT + EWC-LoRA continual SFT
│   │   ├── phase_5_compose.py        # MoLE/X-LoRA gate training
│   │   ├── phase_6_self_play.py      # SPIN/Meta-Rewarding bootstrap
│   │   └── phase_7_publish.py        # push to surrogate-1-skills-voyager
│   ├── env_knobs.yaml                # toggle each phase on/off per round
│   └── cron.yaml
├── skills/                           # Voyager-style library
│   ├── <skill_name>/
│   │   ├── SKILL.md                  # Anthropic-format metadata
│   │   ├── code.py                   # executable
│   │   ├── tests.py
│   │   └── stats.json                # success_rate, n_uses, last_used
└── outcomes.jsonl                    # source of truth (Mem0-backed)
```

### env_knobs.yaml (toggle each Phase per round)

```yaml
# Each round of training reads this file
PHASE_0_COLLECT: true            # always on
PHASE_1_SKILL_MINE: true         # EvoSkill-style
PHASE_2_REFLECT: true            # Reflexion + RISE pairs
PHASE_3_VALIDATE: true           # held-out gate (mandatory)
PHASE_4_LORA_TRAIN:
  enabled: true
  method: SDFT                   # SDFT | LoRA-SFT | RAFT | GRPO
  ewc_lambda: 0.4                # EWC reg strength (0 = off)
  replay_pct: 0.10               # 10% old data mixed in
  rank: 16
  base_model: qwen3.5:9b         # local primary
PHASE_5_COMPOSE:
  enabled: true
  method: MoLE                   # MoLE | X-LoRA | LoRAHub
  roles: [dev, ops, qa, architect, reviewer]
PHASE_6_SELF_PLAY:
  enabled: false                 # turn ON when human SFT runs out
  method: SPIN                   # SPIN | Meta-Rewarding | Self-Rewarding
  iterations: 3
PHASE_7_PUBLISH: true            # push to GitHub axentx/surrogate-1-skills-voyager
ESCALATE_TO_CIVO_IF_LARGER_THAN: 12B  # T4 OOM fallback
```

### Phase 1 — Skill Mining (EvoSkill, ~25 lines)

```python
# trainer/phases/phase_1_skill_mine.py
def mine_skills(outcomes_path: Path, skill_lib: SkillLibrary) -> list[Skill]:
    failures = [o for o in load_jsonl(outcomes_path) if not o["success"]]
    successes = [o for o in load_jsonl(outcomes_path) if o["success"]]
    candidates = []
    for fail in failures:
        # Reflexion-style: ask LLM what skill would have prevented this
        skill = llm.complete(SKILL_DISTILL_PROMPT.format(
            failure=fail, similar_success=nearest(successes, fail)))
        candidates.append(skill)
    # Pareto filter: keep skill iff val-set delta > 0
    kept = [s for s in candidates if validate(s, holdout_set) > skill_lib.baseline(s.task)]
    return kept
```

### Phase 4 — Continual SFT with SDFT + EWC + Replay (~30 lines)

```python
# trainer/phases/phase_4_lora_train.py — runs on T4×2 for 7-9B base
from peft import LoraConfig, get_peft_model
from trl import SFTTrainer

def continual_sft(new_skills, replay_buffer, base_model_path, ewc_lambda=0.4):
    base = load_4bit(base_model_path)             # QLoRA NF4
    lora = get_peft_model(base, LoraConfig(r=16, alpha=32, target_modules="all-linear"))
    fisher = load_fisher_diag(prev_round_path)    # for EWC

    # SDFT teacher pass
    teacher_logits = []
    for s in new_skills:
        logits = base.generate(prompt=s.task, demo=s.code, return_logits=True)
        teacher_logits.append(logits)

    # mix new + replay (10%)
    train_data = new_skills + sample(replay_buffer, k=int(0.1 * len(new_skills)))

    def loss_fn(student_logits, batch, idx):
        ce = ce_loss(student_logits, batch.tokens)
        kd = kl_div(student_logits, teacher_logits[idx])
        ewc = ewc_lambda * sum(fisher[k] * (lora.state_dict()[k] - prev_params[k])**2
                                for k in fisher)
        return ce + kd + ewc

    SFTTrainer(model=lora, train_dataset=train_data, compute_loss=loss_fn).train()
    save_fisher_diag(lora, this_round_path)
    return lora
```

### Phase 5 — MoLE Composition Gate (~20 lines)

```python
# trainer/phases/phase_5_compose.py
class MoLEGate(nn.Module):
    def __init__(self, n_experts=5, hidden=4096):
        super().__init__()
        self.gate = nn.Linear(hidden, n_experts)
    def forward(self, hidden_states, expert_outputs):
        weights = F.softmax(self.gate(hidden_states), dim=-1)  # per-token gate
        return sum(w * o for w, o in zip(weights.unbind(-1), expert_outputs))

# Train only the gate; freeze adapters from Phase 4
gate = MoLEGate(n_experts=len(roles))
for batch in mixed_role_data:
    expert_outputs = [adapter[role](batch) for role in roles]
    out = gate(batch.hidden, expert_outputs)
    loss = ce_loss(out, batch.target)  # ~5min on T4 per epoch
```

### Phase 7 — Auto-Publish to GitHub (~15 lines)

```python
# trainer/phases/phase_7_publish.py
def publish(skill_lib_path: Path, repo="axentx/surrogate-1-skills-voyager"):
    branch = f"round-{round_id()}"
    subprocess.run(["git", "checkout", "-b", branch], cwd=skill_lib_path)
    subprocess.run(["git", "add", "."], cwd=skill_lib_path)
    subprocess.run(["git", "commit", "-m", f"round {round_id()}: +{n_new} skills"],
                   cwd=skill_lib_path)
    subprocess.run(["gh", "pr", "create", "--fill", "--auto-merge"], cwd=skill_lib_path)
```

### Cron Cadence

| Phase | When it fires | Why this cadence |
|-------|---------------|------------------|
| **0 Collect** | Continuous (every agent run writes to outcomes.jsonl) | Source of truth must always be fresh |
| **1 Skill mine** | Every 200 outcomes OR daily 02:00 ICT | Need enough volume for Pareto signal |
| **2 Reflect** | Every 50 failures | Failures are scarcer than successes; reflect early |
| **3 Validate** | Before every Phase 4 | Mandatory gate — no skill enters training without held-out delta |
| **4 LoRA-SFT** | **Biweekly Sunday 03:00 ICT** | Aligned with current Prowler cadence; gives 14d of data per round |
| **5 Compose gate** | After every Phase 4 | Gate retrains in <10min on T4 |
| **6 Self-Play** | Monthly OR when new SFT data < 100 examples | Bootstrap only when data is the bottleneck |
| **7 Publish** | After every successful Phase 4+5 | Atomic: weights + skill repo move together |
| **Reflection cron** (Generative-Agents-style) | Every 500 outcomes | Higher-order insights, not skills |
| **Eureka reward synth** | On new task family detection | Reactive; needed only when no reward exists |
| **SPIN iters** | 3 iterations per Phase 6 invocation | SPIN paper's optimal setting |

### T4×2 Feasibility Matrix

| Component | T4×2 (32GB) | Civo H100/A100 needed? |
|-----------|-------------|------------------------|
| 7-9B QLoRA SFT (rank 16) | ✅ Fits | No |
| 13B QLoRA SFT | ⚠️ Tight (offload) | Optional |
| 30B+ SFT/RL | ❌ | **Yes** (escalate) |
| GRPO 7B (300 tok/s) | ✅ | No |
| MoLE gate training | ✅ Trivial | No |
| Skill mining (inference only) | ✅ | No |
| SDFT 7B (2.5× SFT compute) | ✅ Fits, slow | Civo if biweekly < 12h |
| SPIN 3-iter on 9B | ⚠️ ~24h on T4×2 | Civo recommended for monthly |

**Default**: T4×2 for biweekly Phase 4. Escalate to Civo H100 only for SPIN months and when base model exceeds 13B.

### Ready-to-Wire Pattern Priority (top 6 first)

1. **EvoSkill Phase 1** (skill mining from failures) — 25 lines, T4-free, immediate ROI.
2. **SDFT Phase 4** (continual SFT without forgetting) — 30 lines, biweekly, T4×2.
3. **MoLE Phase 5** (per-role LoRA composition) — 20 lines + 5 role adapters, after Phase 4.
4. **Meta-Rewarding judge** (when no execution signal) — drop-in for subjective tasks, no GPU.
5. **Mem0 backbone** (outcomes.jsonl persistence) — already production-ready, swap for filesystem.
6. **Phase 7 auto-publish** to axentx/surrogate-1-skills-voyager — 15 lines, GitOps.

### Needs Data Prep First

- **SPIN / Self-Rewarding**: defer until ≥1000 quality SFT examples exist; otherwise judge is too weak.
- **Eureka reward synth**: only when V13 needs to learn a new task family with no existing reward; not Phase-1 priority.
- **EWC-LoRA**: turn on after 5+ adapter rounds; before that, replay buffer alone is enough.
- **A-Mem cross-skill linking**: defer until skill count > 50; below that, simple embedding retrieval suffices.

### Risks & Anti-Patterns

- **Skill rot**: skills that were validated once degrade as base model evolves. Re-validate every Phase 4 with current weights, prune <50% pass rate.
- **Catastrophic forgetting**: never run Phase 4 without SDFT teacher OR replay OR EWC. Pick at least one.
- **Reward hacking**: GRPO with poor reward → exploits. Always pair with held-out eval (Phase 3) and trajectory dedup.
- **Skill explosion**: cap library at N=200 active skills; LRU evict by `last_used × success_rate`.
- **Self-play collapse**: SPIN can mode-collapse if generator > judge. Cap at 3 iters, restart from latest SFT checkpoint.

---

## Sources

- [Voyager paper](https://arxiv.org/abs/2305.16291) | [Voyager repo](https://github.com/MineDojo/Voyager)
- [AutoSkill paper](https://arxiv.org/abs/2603.01145) | [EvoSkill repo](https://github.com/sentient-agi/EvoSkill) | [EvoSkill paper](https://arxiv.org/abs/2603.02766) | [EXIF](https://arxiv.org/abs/2506.04287)
- [Eureka](https://arxiv.org/abs/2310.12931) | [DrEureka repo](https://github.com/eureka-research/DrEureka)
- [SoK Agentic Skills](https://arxiv.org/html/2602.20867v1) | [Agent Skills LLMs survey](https://arxiv.org/abs/2602.12430)
- [Skill-it!](https://arxiv.org/abs/2307.14430) | [Skill-LLM](https://arxiv.org/abs/2410.12052) | [Skills-in-Context](https://aclanthology.org/2024.findings-emnlp.812.pdf)
- [Reflexion](https://arxiv.org/abs/2303.11366) | [CRITIC](https://arxiv.org/abs/2305.11738) | [Multi-Agent Reflexion](https://arxiv.org/html/2512.20845)
- [MELO repo](https://github.com/ECNU-ICALK/MELO) | [MELO paper](https://arxiv.org/abs/2312.11795)
- [MemGPT paper](https://arxiv.org/abs/2310.08560) | [Letta docs](https://docs.letta.com/concepts/memgpt/)
- [A-Mem paper](https://arxiv.org/abs/2502.12110) | [A-Mem repo](https://github.com/WujiangXu/A-mem) | [Mem0 paper](https://arxiv.org/abs/2504.19413) | [Mem0 repo](https://github.com/mem0ai/mem0)
- [Generative Agents](https://arxiv.org/abs/2304.03442)
- [MUSE](https://arxiv.org/abs/2411.13537)
- [LoRAHub paper](https://arxiv.org/abs/2307.13269) | [LoRAHub repo](https://github.com/sail-sg/lorahub) | [X-LoRA paper](https://pubs.aip.org/aip/aml/article/2/2/026119/3294581) | [X-LoRA repo](https://github.com/EricLBuehler/xlora) | [MoLE paper](https://arxiv.org/abs/2404.13628) | [MoLE repo](https://github.com/adithya-s-k/MoLE)
- [SDFT paper](https://arxiv.org/abs/2601.19897) | [SDFT page](https://self-distillation.github.io/SDFT) | [EWC-LoRA](https://arxiv.org/html/2602.17559)
- [SPIN paper](https://arxiv.org/abs/2401.01335) | [SPIN repo](https://github.com/uclaml/SPIN) | [Verl SPIN recipe](https://verl.readthedocs.io/en/latest/algo/spin.html)
- [Self-Rewarding LM](https://arxiv.org/abs/2401.10020) | [Meta-Rewarding](https://arxiv.org/abs/2407.19594)
- [SAGE paper](https://arxiv.org/abs/2512.17102) | [SkillRL paper](https://arxiv.org/abs/2602.08234) | [SkillRL repo](https://github.com/aiming-lab/SkillRL) | [SkillFoundry](https://arxiv.org/abs/2604.03964) | [Trace2Skill](https://arxiv.org/html/2603.25158)
- [RAFT/Reinforce-Rej](https://arxiv.org/abs/2504.11343) | [Anthropic Agent Skills](https://github.com/anthropics/skills) | [Lifelong Agents ICLR 2026](https://lifelongagent.github.io/)

## See Also

- [[autonomous-24x7]]
- [[self-improvement]]
- [[training-tooling-2026-Q2]]
- [[coding-llm-frontier]]
- [[surrogate-1-autonomous-arch]]
- [[surrogate-1-v10-rev2-spec]]

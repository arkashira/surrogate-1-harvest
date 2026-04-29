---
title: "Surrogate-1 v2: Built-in Multi-Agent / Swarm Capabilities Research"
date: 2026-04-29
session: 2026-04-29-surrogate1-honest-audit
audience: Surrogate-1 v2 design
status: research-complete
tags:
  - multi-agent
  - agent-swarm
  - self-orchestration
  - kimi-k26
  - manus
  - devin
  - claude-multi-agent
  - parl
  - swe-gym
  - hermes-traces
  - klear-agentforge
  - axolotl
  - qwen2.5-coder-7b
  - lora
related:
  - "[[research-arch-context]]"
  - "[[research-training-techniques]]"
  - "[[research-data-curation]]"
  - "[[research-evaluation]]"
  - "[[v2-master-plan]]"
---

# Multi-Agent / Swarm Capabilities Built INTO the Model

> Goal: Surrogate-1 (Qwen2.5-Coder-7B + LoRA) must self-orchestrate teams of subagents like Kimi K2.6 / Manus / Claude Multi-Agent — all from a single model with shared context. No external coordinator (no Hermes), no glue framework needed.

This document is the multi-agent analogue to `research-arch-context.md`. It collects: (1) what's actually built into 2025-2026 frontier models, (2) the orchestration patterns they implement, (3) shared-memory / parallel-execution mechanics, (4) datasets + JSONL formats that teach a 7B to spawn / coordinate, (5) Axolotl recipes for Qwen2.5-Coder-7B, (6) v2 plan with realistic GAIA / SWE-Bench expectations.

---

## TL;DR (the 60-second read)

| Capability | Reference | Mechanism | What we copy |
|---|---|---|---|
| Self-spawn subagents | Kimi K2.6 (300 sub-agents, 4000 steps) | PARL: orchestrator trainable, sub-agents frozen, 3-term reward | PARL-mini: orchestrator-only LoRA on Qwen2.5-Coder-7B |
| Parallel research | Anthropic Multi-Agent Research (90% time cut) | LeadResearcher spawns 3-5 subagents, each w/ 3+ parallel tools | Trajectory dataset where Claude does this; teach format via SFT |
| 100+ generalist swarm | Manus Wide Research | Each subagent = full general Manus; result merger | Out of scope at 7B; defer to v3 |
| Cloud parallel IDE | Devin 2.0 | Parallel VMs, per-agent Dev Box | Skip — infra not model |
| Skills as memory | Voyager skill library | Code-as-skill, embedding-indexed retrieval | RAG of past trajectories at inference (cheap) |
| Episodic reflection | Reflexion | Verbal RL from failures into memory buffer | Synth: failed → reflected → succeeded triples |
| Tool calls as graph | Hermes/ChatML format | `<think>` + `<tool_call>` + `<tool_response>` | Surrogate-1 native format |
| Self-orchestration training | Klear-AgentForge (Qwen3-8B → matches 32B) | SFT on tool-use traces → RL with verifiable rewards | Same recipe on Qwen2.5-Coder-7B |
| RL infra at scale | Qwen3-Coder (20k parallel envs) | Long-horizon agent RL on verifiable code tasks | Use SWE-Gym subset; small RL run after SFT |

**Honest expectation for v2 (Qwen2.5-Coder-7B + LoRA, 24 GB single GPU):**

- SWE-Bench Verified: **18-26 %** (Klear-AgentForge proves 8B can reach 32% with full SFT+RL; LoRA gives ~70% of that lift)
- GAIA Level 1: **20-35 %** with light tool-use SFT (vs Manus 75 % full GAIA, but Manus is a giant cloud system)
- "Swarm" capability: Surrogate-1 v2 will spawn **2-5 subagents** reliably with shared scratchpad, NOT 100+ — that needs a 100B+ orchestrator class model
- v2 unlocks: orchestrator-style routing prompts, plan-act-reflect loop, basic delegation via nested function calls

---

## 1. Built-in Multi-Agent in Frontier Models (2025-2026)

### 1.1 Anthropic Claude Multi-Agent Research System

**Pattern**: Orchestrator-worker (lead-researcher with subagents).

- **LeadResearcher** receives query, plans approach, persists plan to Memory tool (so 200k context truncation doesn't lose state).
- Spawns **3-5 subagents in parallel** (was sequential pre-2025; 90% latency cut after parallelizing).
- Each subagent uses **3+ tools in parallel** internally (compounding parallelism).
- A separate **CitationAgent** runs after to add citations.
- Bottleneck identified: synchronous barrier between waves of subagents.

**Performance**: Claude Opus 4 lead + Sonnet 4 subagents **+90.2 % over solo Opus 4** on internal eval. **Token usage explains 80 % of variance** (multi-agent burns ~15× tokens vs chat).

**Prompt engineering encoded into the model's behavior** (per Anthropic's blog):
1. "Examine all available tools first, match tool usage to user intent."
2. "Start with short, broad queries, evaluate, progressively narrow."
3. Scaling rules: 1 agent for simple queries, 10+ for complex research.
4. "Interleaved thinking after tool results to evaluate quality, identify gaps, refine next query."

**What this teaches us**: parallel decomposition is a *prompt-level* skill that the model learned from (a) Anthropic-internal RLHF on multi-agent traces and (b) tool-call format that natively supports parallel calls.

---

### 1.2 Kimi K2.6 (Moonshot AI, April 2026) — Agent Swarm

**Architecture**: 1T-param MoE, 32B active per token, 384 experts (8 selected + 1 shared), 256k context. Native multimodal.

**Agent Swarm specs**:
- Up to **300 sub-agents** in parallel (K2.5 was 100).
- **4000 coordinated steps** per task (K2.5 was 1500).
- 12-hour autonomous coding runs (financial-engine refactor: +185% throughput, +133% performance, 13 hours unattended).

**SWE-Bench Pro**: 58.6 (vs GPT-5.4 at 57.7, Claude Opus 4.6 at 53.4).
**HLE-Full (with tools)**: 54.0 — leads all open + closed.
**BrowseComp (Swarm mode)**: 86.3.

**Training: PARL (Parallel Agent RL)** — the most important paper for v2 plan:

- **Trainable**: orchestrator only.
- **Frozen**: sub-agents (their trajectories excluded from policy gradient → solves credit-assignment ambiguity).
- **Reward** = `λ₁·r_parallel + λ₂·r_finish + r_perf`
  - `r_parallel`: prevents serial collapse (orchestrator falling back to single-agent).
  - `r_finish`: rewards completed subtasks (prevents spurious parallelism / fake decomposition).
  - `r_perf`: actual task outcome.
  - λ's annealed to 0 → final policy purely optimizes performance.
- **Cost metric**: `CriticalSteps = Σ_t (S_main^(t) + max_i S_sub,i^(t))` — i.e. longest parallel branch, NOT sum. Drives balanced decomposition.
- **Result**: 4.5× latency cut, BrowseComp 78.4 % vs 60.6 % single-agent.

This is the gold-standard recipe. Open-source PARL repo: `github.com/The-Swarm-Corporation/PARL` (HuggingFace integration `quickstart_hf.py`, reward fn `PARLReward(lambda_init=0.1, lambda_final=0.0, total_training_steps=10000)`).

---

### 1.3 Manus AI — Wide Research (July 2025)

**Architecture**: Unified user interface, **multi-agent internally**:
- Coordinating orchestration layer.
- Specialized sub-agents for: planning, retrieval, code-gen, tool-exec, data-analysis, verification.
- **Wide Research mode**: spawns 100+ parallel sub-agents, each is a **full general Manus instance** (not a role-specialist) — flexible, scalable, no rigid template.
- Each session = dedicated cloud VM.

**GAIA**: 75% overall (matches H2O.ai); tops Level 3 (hardest).

**Architectural detail Moonshot/Manus deliberately don't disclose**: shared-memory protocol, result merging, spawn mechanism. Indicates this is competitive moat, NOT public knowledge.

**Practical note for Surrogate-1**: Manus's "fully general subagent" concept is interesting but needs huge infra. Don't copy at 7B; revisit at v3.

---

### 1.4 Devin 2.0 (Cognition AI, April 2025)

**Architecture**:
- Devin Backend (brain + metadata) separate from Customer VPC.
- **Customer VPC** hosts isolated Dev Boxes (Linux shell, editor, browser, agent).
- Devin 2.0: spin up **multiple parallel Devin instances**, each in its own VM, each w/ own IDE.
- ARR: $1M (Sep 2024) → $73M (Jun 2025), $150M+ post-Windsurf acquisition.

**Takeaway**: Devin's multi-agent advantage is mostly *infrastructure* (parallel sandboxed VMs), not *model architecture*. Their model could be GPT-5/Claude under the hood. **Not directly applicable to Surrogate-1's 7B model architecture choices**, but informs our v2 deployment plan: support spawning Surrogate-1 instances in parallel via Modal/Lightning workers.

---

### 1.5 GPT-5.5 (OpenAI, April 23, 2026) — Codex Subagents

- Codename "Spud", shipped 6 weeks after GPT-5.4.
- Pivot from dialogue → execution.
- **Subagent workflows in Codex**: spawn specialist agents in parallel (research / explore / analyze).
- Concurrent agents on single project via **isolated git worktrees** (prevents merge conflicts) — interesting infra primitive.
- CI/CD via Codex SDK.
- "Move noisy, exploratory work off the main thread."

OpenAI explicitly advertises "the model itself acts as orchestrator" — same pattern as Kimi.

---

### 1.6 DeepSeek-V3.2 (December 2025)

- 1800+ environments, 85k complex prompts in agent training synthesis.
- "Cold-start" unifies reasoning + tool-use in single trajectory.
- **Specialist-based post-training**: 6 specialists from same V3.2 base — math, code, general reasoning, **agentic tasks, agentic coding, agentic search**.
- Agent tasks: rule-based outcome reward + length penalty + language consistency.
- Post-training compute > 10% of pretraining.
- **SWE-Bench Verified: 72-74%** across frameworks (Claude Code, RooCode, internal). Best open model.

DeepSeek's recipe is the closest to what we want for Surrogate-1: **separate specialist for "agentic coding" with verifiable rewards.**

---

### 1.7 Genie 2 / SIMA 2 (DeepMind, Nov-Dec 2025)

- Different domain — embodied 3D agents in virtual worlds.
- SIMA 2: Gemini Flash-Lite + visuomotor stack across many games.
- **Self-improvement in Genie-generated worlds** = synth-env bootstrap.

Not directly applicable to Surrogate-1 (we're code-focused), but the **synthetic environment scaling** pattern is portable: Surrogate-1 v3 could synth code-gym environments using a teacher (Qwen3-Coder-Next).

---

### 1.8 When does multi-agent beat single-agent? (Princeton & Google 2025-2026)

The honest answer:

- **Parallelizable tasks**: multi-agent wins by **+80 %** (e.g. Finance-Agent, multi-document research).
- **Sequential reasoning**: multi-agent **degrades by 39-70 %** (PlanCraft etc.) — coordination overhead fragments reasoning.
- **64% of benchmark tasks**: single-agent matches or beats multi-agent when both have same tools/context (Princeton NLP).
- **Orchestrator (centralized) vs P2P**: orchestrator caps error amplification at 4.4× ; independent P2P amplifies 17.2×.
- **Coding**: multi-agent SWE-Bench Verified 72.2 % vs 65 % solo.

**Implication for Surrogate-1**: don't add agents reflexively. Train the model to **decide when to spawn**: complex-parallelizable → swarm; sequential-reasoning → solo.

---

## 2. Orchestration Patterns (and which to bake into Surrogate-1)

### 2.1 Hierarchical (Manager + Workers) — the default

CrewAI, Claude Multi-Agent Research, Kimi PARL all use this.

- Manager owns: planning, decomposition, aggregation, validation.
- Workers own: a single subtask, no global state.
- Communication: tool-call return values (manager invokes worker like a function).
- Strengths: error containment (orchestrator validates), simple credit assignment.
- Weaknesses: synchronous barrier (manager waits for slowest worker).

**Surrogate-1 v2 default = this pattern.**

---

### 2.2 Mesh / Peer-to-Peer

AutoGen GroupChat, swarm-style.

- All agents see one shared thread.
- Higher emergent behavior, but error amplification is brutal (17.2× per Princeton).
- Hard to train RL on (multiple gradients fight).

**Skip for v2.**

---

### 2.3 Pub/Sub via Shared State

LangGraph (TypedDict shared state passed between nodes).

- State object = "scratchpad" all agents read/write.
- Each agent gets `(state) -> updated_state`.
- Easy to checkpoint + resume.

**Surrogate-1 v2 will use a JSON scratchpad** as a tool (`scratchpad_read`, `scratchpad_write`) — cheap, model-internal, no graph framework needed.

---

### 2.4 Mixture of Agents (MoA, Together.ai)

- Layered: each layer = N parallel agents, all see prior layer outputs.
- Aggregator agent in final layer.
- Open-source MoA → **65.1 % AlpacaEval 2.0** vs GPT-4o's 57.5 %.
- Used as **distillation source**: produce SFT data, then train one model.

**Use MoA as a teacher pipeline** (running Qwen3-Coder-Next + DeepSeek-V3.2 + Kimi-K2.6 in 3 layers) to synth multi-agent traces → SFT Surrogate-1 v2.

---

### 2.5 AutoGen Conversation Patterns

- Two-Agent (assistant ↔ user-proxy with code exec).
- Group chat (n agents, shared thread).
- Sequential chat (output of A becomes carryover for B).
- Nested chat (a tool call internally runs a chat).

**Useful as JSON schema for synthetic trajectories**. We'll generate AutoGen-style traces and convert to ShareGPT.

---

### 2.6 LangGraph Nodes

- Stateful graph: each node is a function `(state) -> partial_state`.
- Conditional edges, loops, supervisor patterns.
- Good for *runtime* orchestration; not directly trainable.

**Use LangGraph at deploy time** (Surrogate-1 + LangGraph supervisor for production). Don't train against it directly.

---

### 2.7 Spring AI Subagent Orchestration (Jan 2026)

Pattern docs (Spring blog 4-part series):
- Subagent boundaries, isolated context, blocking tool-style invocation.
- Same primitive as Claude Code's Task tool.

---

### Training the model to choose the right pattern

The "decision skill" we want Surrogate-1 to learn:

```
if task.is_parallelizable() and task.scope > 1_file:
    spawn(subagents, n=adaptive)
elif task.requires_long_planning:
    enter_architect_mode(plan_first)
elif task.is_simple:
    solo_execute()
```

This is teachable via SFT on traces where the lead model demonstrates this routing. Hermes-agent-reasoning-traces + SWE-Gym + synth = covers all branches.

---

## 3. Shared Context / Memory Protocols

### 3.1 Three-tier memory (MemGPT / Letta — current best practice)

- **Core memory blocks**: in-context, model rewrites them.
- **Recall memory** (database): chronological, all conversation history; model retrieves on demand via search.
- **Archival memory**: long-term semantic store, vector-indexed.

Letta v1 (post-Sep-2025) uses Claude Sonnet 4.5's memory tools — model directly controls own memory blocks via tool calls (`memory_read`, `memory_write`, `memory_search`).

**For Surrogate-1**: implement 3 simple tools:
- `scratchpad_*` (in-context shared between sibling subagents).
- `memory_search(query)` (RAG over past sessions, ChromaDB or FalkorDB).
- `memory_write(key, value)` (durable across sessions).

---

### 3.2 Voyager-Style Skill Library

- Each skill = code snippet, indexed by embedding of its description.
- On new task: retrieve top-5 relevant skills.
- Compose simple skills into complex ones.
- Bypasses fine-tuning by adding to library (no weight updates).

**For Surrogate-1**: `skills/` directory under `~/.claude/memory/`; tool `skill_recall(query)` returns top-k matching past solutions. Cheap, effective. Already partially implemented in user's `knowledge_index.md` workflow.

---

### 3.3 Reflexion Episodic Memory

- After failure: model writes verbal self-reflection.
- Reflection = additional context for next attempt.
- "Verbal RL" — no parameter updates.
- SOTA on HumanEval / Reasoning at the time (NeurIPS 2023).

**For Surrogate-1**: synthesize SFT data from `(failed_trace, reflection, succeeded_trace)` triples. Forces the model to learn *recovery from failure*, not just success paths.

---

### 3.4 Vector RAG for Cross-Agent Memory

- All sibling agents read/write shared ChromaDB.
- Cheap shared scratchpad with semantic search.
- User already has this: `~/.claude/bin/rag-index.sh`, `ask.sh`.

**Reuse, don't rebuild.**

---

### 3.5 Anthropic's Approach (LeadResearcher)

- Saves plan to Memory tool **at start** (before context fills).
- Subagents inherit relevant slice of plan.
- After 200k context: spawn fresh subagent with handoff summary + memory pointer.

This is the cleanest pattern. **Bake into Surrogate-1 system prompt**: "When task estimated > 50% of context window → write plan to memory before acting."

---

## 4. Parallel Execution Mechanics

### 4.1 When can agents work in parallel?

- **Independent reads** (research multiple docs): always.
- **Independent transforms** (refactor different files): always with worktree isolation.
- **Independent tests**: always.
- **NOT parallelizable**: sequential reasoning chains (one logical step depends on previous), shared-state writes without locks.

### 4.2 Sync points

Anthropic Multi-Agent and Kimi PARL use **wave-based** sync:
- Wave 1: orchestrator decomposes → spawn N subagents.
- Wave N: orchestrator collects all results → decide next wave or finalize.
- Synchronous barrier between waves (Anthropic acknowledges this as bottleneck).

**Surrogate-1 v2 uses wave-based sync** because async (free spawning, results trickle in) is much harder to train. v3 can move to async.

### 4.3 Task decomposition

The planning agent's job:
1. Read task.
2. Estimate scope (LoC, file count, domains).
3. Identify independent subtasks.
4. Assign each to one subagent with: task description, expected output schema, time budget, tools allowed.

**Train via**: synth traces where decomposition is explicit. Format:
```json
{"role": "assistant", "content": "<plan>...</plan><spawn n=3>...</spawn>"}
```

### 4.4 Result aggregation

Two patterns:
- **Concatenate**: all subagent outputs → orchestrator summarizes.
- **Vote/synthesize**: orchestrator reads all, picks best or merges.

For coding tasks: each subagent returns a patch; orchestrator validates, picks the one that passes tests.

### 4.5 Conflict resolution

- **Worktrees**: GPT-5.5 Codex pattern — each subagent works in isolated git worktree; merge at end with conflict detection.
- **Locks**: shared memory with read/write locks (slow, error-prone, skip).
- **Optimistic**: subagents work on copies; orchestrator reconciles.

**Surrogate-1 v2** = worktree pattern via the Claude Code SDK we already use (`EnterWorktree` tool exists). No new infra needed.

---

## 5. Training Data for Multi-Agent Capability

### 5.1 Hermes Agent Reasoning Traces (`lambda/hermes-agent-reasoning-traces`)

**This is the winner for Surrogate-1 v2 SFT.** Apache-2.0 licensed.

**Schema**:
| Field | Type |
|---|---|
| `id` | string (UUID) |
| `conversations` | list of `{from, value}` (ShareGPT) |
| `tools` | string (JSON tool definitions) |
| `category` / `subcategory` / `task` | strings |

**Roles**: `system`, `human`, `gpt`, `tool`.

**Inside `gpt` messages**:
```xml
<think>
chain-of-thought reasoning
</think>

<tool_call>
{"name": "function_name", "arguments": {...}}
</tool_call>
```

**Inside `tool` messages**: actual execution result (terminal output, file diff, etc.).

**Two configs**:
- `kimi`: 7,646 samples, **24.3 turns/sample**, 13.9 tool calls/sample (414 words avg reasoning).
- `glm-5.1`: 7,055 samples, 19.1 turns, 9.7 tool calls (70 words avg reasoning).

**Total: 14,701 samples, 1.62 GB.**

**Why it's perfect for v2**:
- Multi-turn (24 turns is exactly the swarm-coordination horizon we need).
- Real tool calls, real outputs (no synthetic toy traces).
- Already in ShareGPT — Axolotl loads natively.
- Two reasoning styles (long vs short) — v2 can learn to scale reasoning to task difficulty.

---

### 5.2 nebius/SWE-agent-trajectories

**80,036 trajectories** from SWE-bench, CC-BY-4.0.

**Schema**: `instance_id`, `model_name`, `target` (resolved bool), `trajectory` (list of `{role, system_prompt, content}`), `exit_status`, `generated_patch`, `eval_logs`.

**Roles**: `system`, `ai`, `user` (user = environment observation).

**Use case**: code-specific multi-step trajectories. Single-agent only (no orchestrator/subagent split), but each is 50-200 turns of real code editing.

**RFT recipe** (Nebius blog): keep only trajectories where final patch passes tests. Then SFT 6 epochs, batch 128, seq 32k, LR 4e-6 cosine.

**v2 plan**: include 5-10k filtered (only successful) trajectories.

---

### 5.3 SWE-Gym + SWE-Gym-Lite

- **SWE-Gym**: 2.4k real Python tasks, 11 repos. Executable Docker images.
- **SWE-Gym-Lite**: 234 instances.
- Apple/Berkeley showed: 32B Qwen2.5-Coder fine-tuned on **<500 trajectories from SWE-Gym** → +14% absolute on SWE-Bench Verified (15.3% → +12.3%; 20.6% → +13.6%).
- Hyperparams: torchtune, LR 1e-4, 5 epochs, batch 8, ctx 32768.

**Key insight**: **<500 trajectories matters more than millions if they're verifiable + diverse.** This is much smaller than 80k Nebius set.

**v2 plan**: use **400 SWE-Gym successful trajectories + 100 hand-curated multi-agent decomposition traces** for the orchestrator skill.

---

### 5.4 microsoft/orca-agentinstruct-1M-v1

**Don't use as-is for multi-agent.** 1.05M rows, but:
- General instruction-tune data, not agent-specific.
- "AgentInstruct" refers to the synth-data pipeline (multi-agent generators), not agent-as-output.
- 15 splits: QA, MCQ, code, creative, RAG, classification — none are explicitly agent-decomposition.

**Use as background mix at 5-10% to maintain general capability**, but it's not the multi-agent training set.

---

### 5.5 SWE-smith (SWE-Bench team)

- 10s of thousands of agent trajectories.
- Built `SWE-agent-LM-32B` → SOTA open SWE-Bench Verified.
- Same format as nebius set (SWE-agent ACI traces).

**Include alongside Nebius set** (same format, complementary distribution).

---

### 5.6 SmolAgents (HuggingFace) — code-as-actions traces

- Agents output Python code as actions (vs JSON tool calls).
- Multi-agent: Manager agent + specialists.
- ~1k LoC framework.

**Use SmolAgents as a *runner* during synth-data gen**: spin up Surrogate-1 v1 in SmolAgents Manager+Worker setup, capture traces → SFT v2.

---

### 5.7 GAIA Benchmark Traces

- 466 questions across 3 levels.
- HAL leaderboard hosts evaluation traces.
- We can pull top-scoring agents' (h2oGPTe, Manus) public traces if available, but most are proprietary.

**Use GAIA as eval, not training set** (avoid leakage).

---

### 5.8 Synthetic multi-agent traces (the secret sauce)

For Surrogate-1 v2, we need **explicitly multi-agent** traces (orchestrator → subagent → return → orchestrator). None of the public datasets above have this cleanly.

**Recipe**: use Kimi K2.6 or Claude Multi-Agent (via API) as **teacher**:
1. Pick 500 GAIA-like / SWE-Gym-like tasks.
2. Run with full Claude Opus 4 + Sonnet 4 multi-agent pipeline (or Kimi swarm mode).
3. Capture full trace including subagent spawns, returns, aggregation.
4. Convert to ShareGPT with explicit `<spawn>` / `<receive>` markers.
5. Filter for successful traces only (RFT).

Budget: ~$200 of Claude API for ~500 traces. Cheap.

**Format example** (full sample below in §6).

---

### 5.9 Voyager-Style Skill Trajectories

- Each "skill" = code function + description embedding.
- We synth: `(task → skill_recall(top-5) → adapted_code → executed → result)`.
- Can pull from user's existing `~/.claude/memory/lessons_learned.md` + `knowledge_index.md` — already structured as skills.

---

## 6. JSONL Training Data Samples

### 6.1 Single-agent multi-turn tool-call (Hermes-style ShareGPT)

```jsonl
{"id": "001", "category": "code-edit", "subcategory": "bug-fix", "task": "Fix SQL injection in user search", "tools": "[{\"name\":\"read_file\",...},{\"name\":\"edit_file\",...},{\"name\":\"run_tests\",...}]", "conversations": [{"from": "system", "value": "You are Surrogate-1, a senior engineer. Decompose, reflect, execute."}, {"from": "human", "value": "Fix the SQL injection in /api/search.py:42."}, {"from": "gpt", "value": "<think>Need to: (1) read file (2) identify vulnerability (3) parameterize query (4) test.</think>\n<tool_call>{\"name\":\"read_file\",\"arguments\":{\"path\":\"/api/search.py\",\"start\":35,\"end\":60}}</tool_call>"}, {"from": "tool", "value": "def search(q):\n    sql = f\"SELECT * FROM users WHERE name LIKE '%{q}%'\"\n    return db.exec(sql)"}, {"from": "gpt", "value": "<think>Confirmed f-string interpolation. Replace with parameterized query.</think>\n<tool_call>{\"name\":\"edit_file\",\"arguments\":{\"path\":\"/api/search.py\",\"old\":\"sql = f\\\"SELECT * FROM users WHERE name LIKE '%{q}%'\\\"\\n    return db.exec(sql)\",\"new\":\"sql = \\\"SELECT * FROM users WHERE name LIKE ?\\\"\\n    return db.exec(sql, (f'%{q}%',))\"}}</tool_call>"}, {"from": "tool", "value": "Edit applied. Diff: -1 +1."}, {"from": "gpt", "value": "<tool_call>{\"name\":\"run_tests\",\"arguments\":{\"path\":\"tests/test_search.py\"}}</tool_call>"}, {"from": "tool", "value": "PASSED tests/test_search.py::test_no_injection"}, {"from": "gpt", "value": "Fix applied: parameterized query replaces f-string interpolation in search(). Tests pass."}]}
```

---

### 6.2 Multi-agent orchestrator trace (synthesized)

```jsonl
{"id": "ma-001", "category": "swarm", "subcategory": "parallel-research", "task": "Compare 3 Python ORM libraries for our project", "tools": "[{\"name\":\"spawn_subagent\",\"arguments\":{\"task\":\"string\",\"return_schema\":\"json\"}},{\"name\":\"receive_results\",\"arguments\":{\"agent_ids\":\"list\"}},{\"name\":\"web_search\",...}]", "conversations": [{"from": "system", "value": "You are Surrogate-1 orchestrator. Decompose parallelizable tasks. Spawn subagents only when subtasks are independent and parallelizable."}, {"from": "human", "value": "Compare SQLAlchemy, Tortoise, and Django ORM for our async FastAPI project. Need: pros, cons, perf, ecosystem."}, {"from": "gpt", "value": "<think>3 independent ORMs to research. Each is parallelizable. Spawn 3 subagents.</think>\n<plan>\n1. Spawn researcher per ORM\n2. Each returns: {pros, cons, async_support, perf_notes, ecosystem}\n3. Aggregate + recommend\n</plan>\n<tool_call>{\"name\":\"spawn_subagent\",\"arguments\":{\"task\":\"Research SQLAlchemy 2.x async support, perf, ecosystem. Return JSON: {pros, cons, async, perf, ecosystem}.\",\"return_schema\":\"json\",\"id\":\"sub-1\"}}</tool_call>\n<tool_call>{\"name\":\"spawn_subagent\",\"arguments\":{\"task\":\"Same questions for Tortoise ORM.\",\"return_schema\":\"json\",\"id\":\"sub-2\"}}</tool_call>\n<tool_call>{\"name\":\"spawn_subagent\",\"arguments\":{\"task\":\"Same questions for Django ORM async.\",\"return_schema\":\"json\",\"id\":\"sub-3\"}}</tool_call>"}, {"from": "tool", "value": "[spawned sub-1, sub-2, sub-3 in parallel]"}, {"from": "gpt", "value": "<tool_call>{\"name\":\"receive_results\",\"arguments\":{\"agent_ids\":[\"sub-1\",\"sub-2\",\"sub-3\"]}}</tool_call>"}, {"from": "tool", "value": "{\"sub-1\":{\"pros\":[\"mature\",\"flexible\"],\"async\":\"yes (2.x)\",\"perf\":\"high\"},\"sub-2\":{\"pros\":[\"async-native\",\"simple\"],\"perf\":\"medium\"},\"sub-3\":{\"pros\":[\"integrated\"],\"async\":\"limited (4.0+)\",\"perf\":\"medium\"}}"}, {"from": "gpt", "value": "<think>SQLAlchemy 2.x best for FastAPI async + perf. Tortoise simpler but lower perf. Django ORM tied to Django.</think>\nRecommendation: **SQLAlchemy 2.x** — async support is mature, perf is highest, ecosystem (Alembic for migrations) is rich. Tortoise is acceptable if simplicity > perf. Django ORM is the wrong fit for standalone FastAPI."}]}
```

Notice the explicit `<plan>` block — that's the decomposition skill we want to teach. Also notice three `spawn_subagent` calls in **one assistant turn** = parallel spawn (matches Anthropic's "3+ tools in parallel" pattern).

---

### 6.3 Plan-Reflect-Execute trace (Reflexion-style)

```jsonl
{"id": "pre-001", "task": "Implement binary search in Python", "conversations": [{"from": "system", "value": "Plan, attempt, reflect, fix."}, {"from": "human", "value": "Implement binary search."}, {"from": "gpt", "value": "<plan>def bsearch(arr, target): use lo/hi pointers, return index or -1.</plan>\n<tool_call>{\"name\":\"write_file\",\"arguments\":{\"path\":\"bs.py\",\"content\":\"def bsearch(arr, target):\\n    lo, hi = 0, len(arr)\\n    while lo < hi:\\n        mid = (lo+hi)//2\\n        if arr[mid] == target: return mid\\n        elif arr[mid] < target: lo = mid+1\\n        else: hi = mid\\n    return -1\"}}</tool_call>"}, {"from": "tool", "value": "Written."}, {"from": "gpt", "value": "<tool_call>{\"name\":\"run_tests\",\"arguments\":{\"file\":\"test_bs.py\"}}</tool_call>"}, {"from": "tool", "value": "FAILED test_empty: bsearch([], 5) returned -1, but expected -1. PASSED. Failed test_target_at_end: bsearch([1,2,3], 3) returned -1, expected 2."}, {"from": "gpt", "value": "<reflect>hi=len(arr) means arr[mid] for mid=len-1 is checked, but elif arr[mid] < target sets lo=mid+1 = len, exits. For target at last index: lo=0, hi=3, mid=1 arr[1]=2<3, lo=2. Now lo=2 hi=3 mid=2 arr[2]=3==target, return 2. Wait, that should work. Let me re-trace... Actually lo=0,hi=3,mid=1 arr[1]=2<3 lo=2. Then lo=2<hi=3, mid=2, arr[2]=3==3 return 2. Test should pass. Hmm. Maybe test calls bsearch differently. Let me read the test.</reflect>\n<tool_call>{\"name\":\"read_file\",\"arguments\":{\"path\":\"test_bs.py\"}}</tool_call>"}, {"from": "tool", "value": "assert bsearch([1,2,3], 3) == 2\nassert bsearch([1,2,3,4], 4) == 3"}, {"from": "gpt", "value": "<reflect>My trace says it should return 2 for [1,2,3],3. Maybe the actual error is in [1,2,3,4],4. Trace: lo=0,hi=4,mid=2,arr[2]=3<4,lo=3. lo=3<hi=4,mid=3,arr[3]=4==4,return 3. That's correct too. Let me actually re-read my code. Oh — my code looks right, maybe test runner issue? Let me re-run.</reflect>\n<tool_call>{\"name\":\"run_tests\",\"arguments\":{\"file\":\"test_bs.py\",\"verbose\":true}}</tool_call>"}, {"from": "tool", "value": "PASSED 2/2"}, {"from": "gpt", "value": "Resolved. First run had stale cached test file. Final implementation passes all tests."}]}
```

This is gold for teaching recovery. The reflect → re-trace → verify loop is what makes Reflexion work.

---

### 6.4 PARL-style multi-agent reward sample (for RL not SFT)

```jsonl
{"task_id": "swe-bench-django-12345", "trajectory": {"orchestrator_turns": 4, "subagents_spawned": 3, "subagent_steps": [12, 8, 15], "critical_steps": 4 + 15 + 3, "subagent_finish_rate": 3/3, "final_outcome": "patch_passes_tests"}, "rewards": {"r_parallel": 0.8, "r_finish": 1.0, "r_perf": 1.0, "lambda_1": 0.05, "lambda_2": 0.1, "total": 0.05*0.8 + 0.1*1.0 + 1.0}}
```

Used as the reward signal during PARL RL phase. Trajectory comes from the rollout; rewards are computed by environment.

---

## 7. Axolotl Config for Surrogate-1 v2 (Qwen2.5-Coder-7B + LoRA)

```yaml
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
load_in_4bit: false
load_in_8bit: false
bf16: true

model_type: AutoModelForCausalLM
tokenizer_type: AutoTokenizer
trust_remote_code: true

# === Multi-agent SFT mix ===
datasets:
  - path: lambda/hermes-agent-reasoning-traces
    name: kimi
    type: chat_template
    chat_template: chatml
    field_messages: conversations
    message_field_role: from
    message_field_content: value
    roles_to_train:
      - gpt
    train_on_eos: true

  - path: lambda/hermes-agent-reasoning-traces
    name: glm-5.1
    type: chat_template
    chat_template: chatml
    field_messages: conversations
    message_field_role: from
    message_field_content: value
    roles_to_train:
      - gpt

  # 5k filtered nebius SWE-agent traces (only target=true)
  - path: nebius/SWE-agent-trajectories
    type: completion
    field: trajectory
    filter: "target == true"
    sample: 5000

  # 400 SWE-Gym successful traces
  - path: SWE-Gym/SWE-Gym
    type: completion
    sample: 400
    filter: "patch_passes_tests == true"

  # 500 synth multi-agent orchestrator traces (we generate via Claude API teacher)
  - path: ./data/synth-multi-agent-500.jsonl
    type: chat_template
    chat_template: chatml
    field_messages: conversations

  # 5% general capability mix
  - path: microsoft/orca-agentinstruct-1M-v1
    type: chat_template
    chat_template: chatml
    field_messages: messages
    sample: 1500

# === LoRA ===
adapter: lora
lora_r: 64
lora_alpha: 128
lora_dropout: 0.05
lora_target_modules:
  - q_proj
  - k_proj
  - v_proj
  - o_proj
  - gate_proj
  - up_proj
  - down_proj

# === Training ===
sequence_len: 16384  # multi-turn agent traces are long
sample_packing: true
pad_to_sequence_len: true

micro_batch_size: 1
gradient_accumulation_steps: 16
num_epochs: 3
optimizer: adamw_bnb_8bit
lr_scheduler: cosine
learning_rate: 1.0e-4
warmup_ratio: 0.03
weight_decay: 0.01

flash_attention: true
gradient_checkpointing: true
gradient_checkpointing_kwargs:
  use_reentrant: false

# Multi-GPU on single node (Lightning AI H200 or 2x A100)
deepspeed: configs/deepspeed_zero2.json

# Logging
output_dir: ./out/surrogate-1-v2-multi-agent
save_steps: 200
eval_steps: 200
logging_steps: 10

# === Evaluation ===
val_set_size: 0.02
test_datasets:
  - path: ./data/swe-bench-verified-eval-50.jsonl
    type: chat_template

# === Special tokens for spawn_subagent ===
special_tokens:
  additional_special_tokens:
    - "<spawn>"
    - "</spawn>"
    - "<receive>"
    - "</receive>"
    - "<plan>"
    - "</plan>"
    - "<reflect>"
    - "</reflect>"

# === Resume / checkpoint ===
resume_from_checkpoint: null
```

**Hardware**: Lightning AI H200 (80 GB) → fits seq 16384 with batch 1 + grad-accum 16. Cost ~$3-4/hr; 3 epochs on ~30k samples ≈ 8-12 hours = $30-50 total.

---

## 8. RL Phase (Optional v2.5 — defer if SFT-only meets bar)

After SFT lands ~70 % of capability, add a small PARL run:

### 8.1 PARL-mini setup

- Orchestrator: trainable Surrogate-1 LoRA (continue training).
- Subagents: **frozen** Surrogate-1 v1 (the SFT-only checkpoint).
- Environment: SWE-Gym-Lite (234 instances, fast turn-around).
- Reward (3-term, PARL paper):

```python
from parl import PARLReward

reward_fn = PARLReward(
    lambda_1=0.05,   # r_parallel weight (anneals to 0)
    lambda_2=0.10,   # r_finish weight (anneals to 0)
    lambda_init=0.10,
    lambda_final=0.0,
    total_training_steps=2000,
)

reward = reward_fn.compute_full_reward(
    num_subagents=n_spawned,
    trajectory_features={
        "subagents_completed": k_finished,
        "critical_steps": longest_branch_len,
        "total_steps": all_steps,
    },
    success=patch_passes_tests,
    training_step=current_step,
)
```

- Algorithm: **GRPO** (works better than PPO for code RL per practitioner's guide; long-horizon).
- Hyperparams (from §A practitioner's guide for Qwen2.5-7B):
  - Actor LR 5e-7, KL 0.001, clip 0.2, gamma 1.0.
  - Batch 64 (smaller for swarm, each rollout has multiple subagents = expensive).
  - Rollout temp 0.7.
- Compute: 2× A100 80GB or H200, ~24 hours.
- Cost: ~$80-120.

### 8.2 Why optional

- SFT alone on Klear-AgentForge recipe: Qwen3-8B → SOTA on SWE-Bench among 8B models, matches some 32B.
- **RL adds 3-7 percentage points**, but doubles cost and adds instability risk.
- Decision gate: **if SFT v2 ≥ 22 % SWE-Bench Verified, ship it; if 18-22 %, add PARL**.

---

## 9. Self-Orchestration: Teaching the Model to Decide

The hardest skill: "spawn or solo?"

### 9.1 Decision routing in system prompt

```
DECISION RULE (apply BEFORE every action):

1. Is the task decomposable into ≥2 independent subtasks?
   YES → consider spawn.
   NO  → solo execute.

2. Will subtasks individually exceed 20% of context window?
   YES → spawn (each gets fresh context).
   NO  → solo (overhead not worth it).

3. Are subtasks parallelizable (no data dependency between them)?
   YES → spawn N (where N = #independent subtasks, max 5).
   NO  → sequential (solo with plan).

4. Is the task purely sequential reasoning (chain of inference)?
   YES → ALWAYS solo. Multi-agent degrades sequential tasks 39-70%.
   NO  → see (1).

5. Is critical-step budget < 20?
   YES → solo (orchestration overhead > benefit).
   NO  → spawn.
```

### 9.2 Training data must contain BOTH branches

Synth recipe:
- 50% of synth traces: model decides "solo" (decomposition fails the rule).
- 50% of synth traces: model decides "spawn" (decomposition succeeds).
- Each trace explicitly verbalizes the decision in `<plan>` block.

**This is the v2 secret sauce**: most agent training datasets (Hermes, Nebius) don't have explicit "I considered spawning but chose not to" examples. We generate these.

### 9.3 Conversation graphs as training data

Conceptually, each multi-agent trace is a tree:
```
root_task
├── orchestrator_turn_1
│   ├── subagent_1 (sub_trace_1)
│   ├── subagent_2 (sub_trace_2)
│   └── subagent_3 (sub_trace_3)
├── orchestrator_turn_2 (aggregation)
└── final_answer
```

Flatten into ShareGPT linearly with explicit `<spawn>` markers (see §6.2). The **tree structure is encoded by which tool calls return when**.

### 9.4 Decomposition prompts in training data

Every synth trace's orchestrator turn 1 starts with:
```
<plan>
Subtasks:
- T1: <description> (independent: yes, est_steps: 12)
- T2: <description> (independent: yes, est_steps: 8)
- T3: <description> (depends on T2: no, est_steps: 15)
Decision: spawn 3 in parallel.
</plan>
```

This trains the model to **always emit a plan before acting on multi-agent tasks**. Aligns with user's `~/.claude/rules/plan_once.md`.

---

## 10. Benchmarks: Realistic v2 Expectations

### 10.1 Current SOTA (April 2026)

| Benchmark | Top Score | Model |
|---|---|---|
| SWE-Bench Verified | 75 % | Claude Opus 4.6 (with multi-agent) |
| SWE-Bench Verified (open) | 72 % | DeepSeek V3.2 |
| SWE-Bench Verified (8B) | 32 % | Klear-AgentForge-8B |
| SWE-Bench Pro | 58.6 | Kimi K2.6 |
| GAIA Level 1 | ~88 % | h2oGPTe / Manus |
| GAIA Level 3 | ~53 % | h2oGPTe |
| GAIA2 (dynamic) | 42 % pass@1 | GPT-5 high |
| HumanEval | 95 %+ | most models saturated |
| BrowseComp | 86.3 | Kimi K2.6 swarm |

### 10.2 Realistic Surrogate-1 v2 targets

Qwen2.5-Coder-7B base + LoRA (no full fine-tune, no 1T params, no swarm infra at training):

| Benchmark | Surrogate-1 v1 (SFT only on instructions) | v2 SFT (multi-agent SFT) | v2 + PARL RL |
|---|---|---|---|
| SWE-Bench Verified | ~12 % | **18-22 %** | **22-26 %** |
| SWE-Bench Lite | ~15 % | 22-28 % | 26-32 % |
| HumanEval | 84 % | 86 % | 86 % |
| GAIA Level 1 | n/a | **20-30 %** | 25-35 % |
| GAIA Level 2 | n/a | 8-15 % | 12-18 % |
| GAIA Level 3 | n/a | 2-5 % | 3-7 % |
| Custom multi-agent eval | low | should pass orchestration tests | should beat solo by 15-25 % |

**Lift estimate basis**:
- SWE-Gym paper: +14 % from 500 trajectories on 32B Qwen2.5. 7B + LoRA → ~70 % of that lift = **+10 %**.
- Klear-AgentForge: 8B → SOTA 8B on SWE-Bench. Qwen2.5-Coder-7B with similar recipe → similar level.
- PARL RL on top: +3-7 % if data is good and reward function is verifiable.

### 10.3 Multi-agent benchmark we should add

No single existing benchmark cleanly tests "model spawns subagents and aggregates correctly." Build a tiny eval (30-50 tasks) that:
- 15 parallelizable tasks (multi-agent should win).
- 15 sequential tasks (solo should win — penalize spurious spawning).
- 10 mixed.

Score: % correctly routed + % correctly executed.

---

## 11. Integration into Surrogate-1 v2 (concrete plan)

### 11.1 Tooling baked into model's prompt

Add to system prompt (always-on):
```
Tools available:
- spawn_subagent(task: str, return_schema: json) -> agent_id
- receive_results(agent_ids: list[str]) -> dict[str, any]
- scratchpad_read() -> str
- scratchpad_write(key: str, value: str) -> ok
- memory_search(query: str, k: int=5) -> list[dict]
- memory_write(key: str, value: str, ttl_days: int=30) -> ok
- skill_recall(query: str, k: int=5) -> list[dict]
+ existing: read_file, edit_file, run_bash, run_tests, web_search
```

### 11.2 Inference-time setup

- Single Surrogate-1 v2 weights file.
- A **Python harness** (~200 LoC) implements: spawn, scratchpad, memory, skill_recall.
- Subagents = the same model weights, fresh KV cache, fresh sub-context.
- Pool size: max 5 concurrent subagents on H200, max 2 on local Mac M3 24GB.

### 11.3 Compatibility with user's existing infra

- ShareGPT format → compatible with existing local LLM tooling (`~/.claude/local-fallback.env`).
- Tool format → same `<tool_call>` ChatML pattern user's Claude Code already produces.
- Memory tools → backed by user's existing ChromaDB / FalkorDB at `~/.claude/bin/`.
- Skills → `~/.claude/memory/lessons_learned.md` and `knowledge_index.md` already structured for this.

**No new infrastructure needed.** Surrogate-1 v2 plugs into user's existing knowledge graph + RAG.

---

## 12. Risks + Mitigations

| Risk | Probability | Mitigation |
|---|---|---|
| 7B can't reliably orchestrate (just pretends to spawn) | Medium | Synth data must include explicit failure cases + solo decisions; also add format consistency reward in RL |
| LoRA doesn't have enough capacity for multi-agent | Low | r=64 is generous; can bump to 128 if needed |
| Synth traces lower quality than real Claude swarm | High | Mitigate: use Claude Opus 4 + Sonnet 4 (best teacher), filter strictly by success |
| PARL training unstable | Medium | Skip if SFT bar is met; or use GRPO not PPO; small batch + KL coef 0.001 |
| Subagent loops infinite | Medium | Hard cap: max 4000 critical steps (Kimi pattern), 30 turns per subagent |
| Context overflow on long swarms | High | Force memory_write at 70 % context use; spawn fresh subagent on overflow with handoff summary (Anthropic pattern) |
| Token cost on inference (15× chat) | High | Document for user; budget at deploy time |

---

## 13. Open Questions for v2

1. **Pure-coding focus or general-agent?** Surrogate-1 v2 needs to choose. Recommend: **coding-first** (matches Qwen2.5-Coder base). Add general agent in v3.
2. **Tool format**: ChatML `<tool_call>` (Hermes) vs Qwen-native `<tools>`? Hermes wins for compatibility; Qwen-native wins for raw quality. Recommend Hermes (already proven, broader ecosystem).
3. **Use Claude Code SDK harness as the runtime?** It already does spawn/scratchpad/memory. Yes — write Surrogate-1 as a "model provider" in the existing harness.
4. **RL or SFT-only?** Recommend SFT-only for v2.0 ship; PARL RL for v2.5 if eval shows ceiling.
5. **Context window**: stick at 32k or push to 128k? Qwen2.5-Coder-7B-Instruct supports up to 128k with YaRN. Recommend **32k for SFT, 128k inference-time** (RoPE scaling, no retrain).

---

## 14. Pipeline (executable order)

```
1. Generate 500 synth multi-agent traces via Claude API teacher (~$200, ~6 hrs).
2. Curate Hermes (14k) + Nebius RFT (5k) + SWE-Gym (400) + synth (500) = ~20k samples.
3. Build Axolotl config (§7).
4. Run SFT on Lightning H200, 3 epochs, ~10 hrs, ~$40.
5. Eval: SWE-Bench Lite + custom multi-agent eval (30 tasks).
6. Decision gate: if SWE-Bench Verified ≥ 22 % → ship v2.0. Else → step 7.
7. PARL RL on SWE-Gym-Lite, 2000 steps, ~24 hrs, ~$120.
8. Re-eval. Ship v2.5.
9. Document failure modes; spawn v3 plan (more data, larger model, full FT not LoRA).
```

Total v2 cost: $200 + $40 = **$240** (SFT only), or **$360** with RL. Well within personal-project budget.

Total v2 timeline: **2-3 weeks** elapsed (most blocking on synth data gen + waiting on Lightning).

---

## 15. References (key papers + repos)

**Frontier model architectures** ([Anthropic Multi-Agent Research](https://www.anthropic.com/engineering/multi-agent-research-system), [Kimi K2.6 announcement](https://www.marktechpost.com/2026/04/20/moonshot-ai-releases-kimi-k2-6-with-long-horizon-coding-agent-swarm-scaling-to-300-sub-agents-and-4000-coordinated-steps/), [Manus Wide Research](https://manus.im/blog/introducing-wide-research), [Devin 2.0](https://cognition.ai/blog/devin-2), [DeepSeek-V3.2 paper](https://arxiv.org/abs/2512.02556)).

**Multi-agent RL training** ([Kimi/Cursor/Chroma RL guide by Phil Schmid](https://www.philschmid.de/kimi-composer-context), [PARL repo](https://github.com/The-Swarm-Corporation/PARL), [Klear-AgentForge paper](https://arxiv.org/abs/2511.05951), [AgentRL paper](https://arxiv.org/abs/2510.04206), [Practitioner's guide to multi-turn agentic RL](https://arxiv.org/html/2510.01132v2)).

**Datasets** ([Hermes Agent Reasoning Traces](https://huggingface.co/datasets/lambda/hermes-agent-reasoning-traces), [Nebius SWE-agent-trajectories](https://huggingface.co/datasets/nebius/SWE-agent-trajectories), [SWE-Gym](https://github.com/SWE-Gym/SWE-Gym), [microsoft/orca-agentinstruct-1M-v1](https://huggingface.co/datasets/microsoft/orca-agentinstruct-1M-v1)).

**Frameworks** ([SWE-Agent (Princeton)](https://github.com/SWE-agent/SWE-agent), [OpenHands](https://github.com/All-Hands-AI/OpenHands), [AutoGen](https://github.com/microsoft/autogen), [LangGraph](https://github.com/langchain-ai/langgraph), [CrewAI](https://github.com/crewaiinc/crewai), [SmolAgents](https://github.com/huggingface/smolagents), [Claude Code Subagents](https://code.claude.com/docs/en/sub-agents)).

**Memory + Reflection** ([MemGPT/Letta](https://github.com/letta-ai/letta), [Voyager](https://arxiv.org/abs/2305.16291), [Reflexion](https://arxiv.org/abs/2303.11366), [MoA](https://arxiv.org/abs/2406.04692)).

**Benchmarks** ([GAIA leaderboard](https://hal.cs.princeton.edu/gaia), [SWE-Bench Verified](https://www.swebench.com/), [Princeton multi-vs-single agent study](https://arxiv.org/abs/2505.18286), [Google research on agent scaling](https://research.google/blog/towards-a-science-of-scaling-agent-systems-when-and-why-agent-systems-work/)).

**Training infra** ([Axolotl](https://github.com/axolotl-ai-cloud/axolotl), [Qwen3-Coder blog](https://qwenlm.github.io/blog/qwen3-coder/), [Kimi-Dev paper](https://arxiv.org/abs/2509.23045)).

**Comparisons + decision frameworks** ([Aider Architect mode](https://aider.chat/2024/09/26/architect.html), [Cline blog "What Makes a Coding Agent"](https://cline.bot/blog/what-makes-a-coding-agent), [Roo Code](https://github.com/RooCodeInc/Roo-Code), [Spring AI subagent patterns](https://spring.io/blog/2026/01/27/spring-ai-agentic-patterns-4-task-subagents/)).

---

## Appendix A: Sample synthetic multi-agent trace generation script

```python
# scripts/synth_multi_agent.py
# Generate 500 multi-agent traces via Claude API for v2 SFT.
import anthropic, json, pathlib

client = anthropic.Anthropic()

TASKS = json.loads(open("./data/v2_tasks_500.json").read())
# 250 parallelizable + 250 sequential (model should choose solo on these)

def run_multi_agent(task):
    """Use Claude Multi-Agent Research mode to solve, capture full trace."""
    response = client.messages.create(
        model="claude-opus-4-5-20260101",
        system=open("./prompts/orchestrator.md").read(),
        messages=[{"role": "user", "content": task["prompt"]}],
        tools=[
            {"name": "spawn_subagent", "input_schema": {...}},
            {"name": "receive_results", "input_schema": {...}},
            # ... + standard tools
        ],
        max_tokens=8000,
    )
    return extract_trace(response)

def to_sharegpt(trace):
    return {
        "conversations": [
            {"from": role_map[t["role"]], "value": t["content"]}
            for t in trace
        ],
        "tools": json.dumps(TOOLS),
        "category": "multi-agent",
        "task": task["prompt"][:200],
    }

out = pathlib.Path("./data/synth-multi-agent-500.jsonl")
with out.open("w") as f:
    for task in TASKS:
        trace = run_multi_agent(task)
        if trace_succeeded(trace):
            f.write(json.dumps(to_sharegpt(trace)) + "\n")
```

---

## Appendix B: Format of `<spawn>` / `<receive>` blocks for tokenization

When extending the tokenizer, add as **single tokens** (not character sequences) — improves quality of multi-token planning blocks:

```
<spawn>     -> token id 151700
</spawn>    -> token id 151701
<receive>   -> token id 151702
</receive>  -> token id 151703
<plan>      -> token id 151704
</plan>     -> token id 151705
<reflect>   -> token id 151706
</reflect>  -> token id 151707
```

Add to Axolotl `special_tokens` block (already in §7 config). Tokenizer + embedding will be auto-extended; LoRA on embeddings adds ~10MB.

---

## Appendix C: Decision framework - spawn vs solo (rule-based, train as classifier first?)

Before SFT, optionally pre-train a tiny classifier head on Surrogate-1 v1 to predict "spawn(yes/no)" given task. Use as auxiliary loss during full SFT to anchor the routing decision. If classifier alone gives 80%+ accuracy on a held-out routing eval, the v2 model will inherit that signal as strong prior.

```python
# Auxiliary classifier head (added during SFT)
class RouterHead(nn.Module):
    def __init__(self, hidden_size=4096):
        super().__init__()
        self.proj = nn.Linear(hidden_size, 2)  # spawn / solo
    def forward(self, h):
        return self.proj(h.mean(dim=1))

# Loss = α·LM_loss + (1-α)·BCE_loss(router_pred, gold_decision)
# α=0.9 keeps LM as primary objective
```

Defer if it adds complexity; SFT trajectories with explicit `<plan>` markers already encode this signal.

---

End of research. Ready for v2 plan execution.

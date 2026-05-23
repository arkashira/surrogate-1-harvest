---
title: V14+ Swarm Agents at Scale — Hierarchical, Decentralized, and Massively Parallel
date: 2026-05-01
project: surrogate-1
version: V14+
status: research-complete
tags: [swarm, multi-agent, hierarchical, decentralized, stigmergy, pheromone, marl, marti, marft, moa, swarmbench, agent-data, surrogate-1, v14]
related:
  - "[[v13-multi-agent-baked-in]]"
  - "[[v13-long-horizon-coding]]"
  - "[[v13-frontier-capability]]"
  - "[[autonomous-24x7]]"
  - "[[training-tooling-2026-Q2]]"
---

# V14+ — Swarm Agents at Scale

> **Owner goal**: Train ONE model that natively spawns and coordinates **10–100+ sub-agent instances of itself** in either hierarchical (manager-of-managers ≥3 deep) or decentralized (gossip / consensus / stigmergy) topologies. The orchestration logic lives **in the weights**, not in external Python. V13 baked `<spawn>/<await>/<aggregate>` for ≤5 children — V14 must scale to depth ≥3 and breadth ≥100.
>
> Frontier proof-points:
> - **Anthropic Research** (Jun 2025): orchestrator-worker, Claude Opus 4 + 3–5 Sonnet sub-agents, **+90.2% over single-agent** on internal research eval.
> - **Kimi K2.6** (Apr 2026): **300 sub-agents × 4,000 coordinated steps**, 13-hour autonomous runs.
> - **MegaAgent** (ACL 2025): **590 agents** in national policy simulation, no predefined SOPs.
> - **ChatDev MacNet** (2024): >1,000 agents collaborating via DAG without context overflow.
> - **Fortytwo** (Oct 2025): peer-ranked swarm consensus — **+17.21pp on GPQA Diamond** vs. majority vote.
>
> V14 wires this proof-of-capability into trainable signal for a single 27B base. Runtime parser stays under 200 LOC. The model carries the topology.

This document captures every relevant 2024–2026 paper, dataset, repo, and recipe — then specifies the concrete training-side patches (token format, dataset additions, reward functions) for V14.

---

## TL;DR Decision Matrix (V14 deltas over V13)

| Decision | V13 baseline | V14+ extension | Why |
|----------|--------------|----------------|-----|
| Topology | Single-level orchestrator → 3–5 workers | **Recursive depth ≥3** + decentralized fallback | Anthropic +90.2% saturated at 5 workers; Kimi went to 300; MegaAgent to 590. To capture that we need depth, not just width. |
| Token vocab | `<spawn>`, `<await>`, `<aggregate>` (3 tags, XML) | + `<broadcast>`, `<bid>`, `<vote>`, `<pheromone>`, `<gossip>`, `<barrier>`, `<role-card>` (7 new) | Enables stigmergy, contract-net, BFT consensus — without these the model can only do tree dispatch, not gossip / market / blackboard. |
| Coordination protocol | Tree dispatch only | Tree + **A2A** (capability cards) + **MCP** (tool envelope) + **blackboard** (shared scratchpad) | A2A donated to Linux Foundation; MCP is industry standard. Training on the wire format means deployment compatibility for free. |
| RL recipe | SFT on 20–30K spawn-format traces | SFT + **MARFT** (Flex-MG) + **MARTI v2** (tree-search RL) + **MAPoRL** post-co-training | MARFT defines the MG mathematically; MARTI delivers production code; MAPoRL is collaborative RL atop both. |
| Critic architecture | None (pure SFT) | **CTDE: Centralized Language Critic + decentralized actors** (LangMARL pattern) | Trajectory-level credit assignment in language space; agents share weights but receive individualized gradient. |
| Ranking / consensus | Single aggregator | **Bradley-Terry pairwise (Fortytwo)** + **WBFT-weighted vote** | Fortytwo +17.21pp; WBFT survives 85.7% Byzantine fault rate. Both implementable as inference-time rerank without architecture changes. |
| Coordination signal | Direct messages only | + **Stigmergic pheromone field** (write to shared blackboard, decay over turns) | Decouples agents — N can grow without N² messages. AMRO-S: 4.7× speedup. |
| Trajectory volume | 20–30K converted | + **30K hierarchical** (≥3 deep) + **20K decentralized** (gossip/vote/bid) | Specifically address swarm patterns the SFT mix from V13 underrepresents. |

---

## 1. The Frontier Proof-Points (must reproduce qualitatively)

### 1.1 Anthropic Multi-Agent Research System — the "+90.2%" paper (Jun 2025)
- **URL**: <https://www.anthropic.com/engineering/multi-agent-research-system>
- **Setup**: Lead Claude Opus 4 + 3–5 Sonnet 4 workers, in parallel; sub-agents call 3+ tools in parallel.
- **Result**: +90.2% over single-agent Opus 4 on internal research eval; 90% wall-clock reduction on complex queries.
- **Production lesson**: ~95% of perf variance explained by THREE factors — (a) total tokens spent (15× single-agent), (b) parallelism, (c) sub-agent quality.
- **Implication for V14**: budget 10–15× the tokens for breadth-first queries; dispatch sub-agents EARLY (before main thinking is complete); workers must have full tool access, not stripped-down.

### 1.2 Kimi K2.6 Agent Swarm (Apr 2026, Moonshot AI)
- **URL**: <https://kimi-k2.org/blog/24-kimi-k2-6-release> + <https://kimi-k2.org/kimi-k26>
- **Scale**: 300 sub-agents × 4,000 coordinated steps; 13-hour autonomous runs.
- **Achievement**: 185% throughput improvement on financial matching engine via 12 optimization rounds + 1,000+ tool calls + 4,000 LOC modified.
- **Benchmark**: HLE-Full with tools 54.0 — leads GPT-5.4 (52.1), Claude Opus 4.6 (53.0), Gemini 3.1 Pro (51.4).
- **Inference formula**: scale UP from K2.5 (100 sub-agents, 1,500 steps) ≈ 3× breadth + 2.7× depth.
- **Implication for V14**: 300-agent runs require **shared state** (blackboard) + **bounded message volume** (stigmergy). Cannot be raw N² gossip.

### 1.3 MegaAgent (ACL 2025 Findings, Xtra-Computing)
- **arXiv**: <https://arxiv.org/abs/2408.09955> — GitHub: <https://github.com/Xtra-Computing/MegaAgent>
- **Architecture**: 3-level hierarchy — Boss → Admin (group leaders) → Workers. NO predefined SOPs; agents generated dynamically based on task complexity.
- **Result**: 590 agents in national policy simulation; Gobang game in 800s.
- **Mechanism**: Three-tier monitoring — agent-level (per-action checklist), group-level (admin reviews group output), system-level (boss reviews all groups).
- **Implication for V14**: training data MUST include 3-tier role tokens (`role="boss"`, `role="admin"`, `role="worker"`) — not just generic spawned children.

### 1.4 ChatDev MacNet — DAG topology (2024 + 2025 puppeteer paradigm)
- **GitHub**: <https://github.com/OpenBMB/ChatDev>
- **MacNet**: directed-acyclic-graph topologies among 1,000+ agents without context overflow.
- **Puppeteer (May 2025)**: novel paradigm where ONE master controls many puppet agents — different from manager-of-managers; controller has direct strings.
- **Implication for V14**: train both **tree-tree (manager-of-managers)** AND **DAG (cross-cutting deps)** AND **puppeteer (centralized direct control)** — three distinct topology classes.

### 1.5 Fortytwo Swarm Inference (Oct 2025)
- **arXiv**: <https://arxiv.org/abs/2510.24801> — HF: <https://huggingface.co/Fortytwo-Network>
- **Mechanism**: Bradley-Terry pairwise ranking aggregation across heterogeneous models, reputation-weighted, proof-of-capability against Sybil.
- **Result**: 85.90% on GPQA Diamond vs. 68.69% for majority vote — **+17.21pp**. Prompt-injection degradation 0.12% vs. 6.20% monolithic.
- **Implication for V14**: replace `<aggregate>` simple-mean with `<vote method="bradley-terry">` token + train on pairwise comparison traces.

---

## 2. Hierarchical Agent Swarms — Depth ≥3

### 2.1 MegaAgent (covered above) — 3-tier with system-level parallelism
- Level 2 groups run in parallel, agents within a group sequentially.
- "Admin agents" oversee group; "Boss agent" reviews all admin outputs.
- Trainable role schema: `boss > admin > worker`.

### 2.2 OWL Workforce (NeurIPS 2025, CAMEL-AI)
- **arXiv**: <https://arxiv.org/abs/2505.23885> — GitHub: <https://github.com/camel-ai/owl>
- **Three-tier**: domain-agnostic Planner + Coordinator + specialized Workers.
- **Training**: Only Planner is RL-trained — Coordinator and Workers are frozen / prompt-engineered.
- **Result**: 69.70% on GAIA — beats OpenAI Deep Research by +2.34%; OWL-trained 32B → +16.37% on hard tasks.
- **Implication for V14**: budget RL only on the *outermost* spawning agent; sub-spawns can stay SFT-only. Same model weights, different sampling regimen at depth.

### 2.3 LangGraph Hierarchical Teams (LangChain, 2024–2025)
- **Pattern**: supervisors managing supervisors. Production-tested in Anthropic-style orchestrator-worker.
- **Schema**: each team has a supervisor; supervisors of teams are themselves managed by a top-supervisor.
- **Training implication**: when generating SFT data, ensure depth-3 traces (orchestrator → team-supervisor → worker). V13 only trained depth-2.

### 2.4 HiAgent (ACL 2025)
- **arXiv**: <https://arxiv.org/abs/2408.09559>
- **Mechanism**: hierarchical working-memory management — subgoals as memory chunks.
- **Result**: 2× success rate on long-horizon tasks; -3.8 avg steps.
- **Implication for V14**: tokenize subgoal-as-memory-chunk (`<memory id="subgoal-3">…</memory>`). Hierarchical agent must be able to read/write memory chunks at its level only — sandboxed.

### 2.5 Society-of-Mind Modern (Mindstorms, 2023; revisited 2025)
- **arXiv**: <https://arxiv.org/abs/2305.17066> + Adnan Masood revisit (2025).
- **Lesson**: "agencies" — domain-coupled groups managed by higher-level mechanisms; structures emerge bottom-up.
- **Implication for V14**: don't hard-code hierarchy depth — let role-card system emit `<role-card name="planner" tier="1">…<spawn-budget>5</spawn-budget>…</role-card>` and the model decides depth.

---

## 3. Decentralized Coordination

### 3.1 SwarmBench (RUC-GSAI, May 2025)
- **arXiv**: <https://arxiv.org/abs/2505.04364> — GitHub: <https://github.com/RUC-GSAI/YuLan-SwarmIntell> — HF: <https://huggingface.co/datasets/6cf/swarmbench>
- **5 tasks**: Pursuit, Synchronization, Foraging, Flocking, Transport — 2D grid, k×k local view, local-only communication.
- **Findings**: leading LLMs (deepseek-v3, o4-mini) zero-shot **struggle** — long-range planning + adaptive strategy under decentralization is broken; behavioral flexibility explains 24.5% of variance.
- **Use for V14**: this is the canonical eval. Train, then run zero-shot, then SFT on captured traces.

### 3.2 Byzantine-Robust Decentralized Coordination
- **Yongrae Jo et al., Jul 2025** — <https://arxiv.org/abs/2507.14928>
- **DecentLLMs**: leaderless. Workers generate in parallel; evaluators score + rank using Byzantine-robust aggregation. Faster consensus, robust to targeted-leader attacks.
- **Weighted BFT (WBFT, May 2025)**: voting weights adapt to response quality + trustworthiness. Blockchain-backed. Survives **85.7% Byzantine fault rate** (CP-WBFT variant).
- **Implication for V14**: training data must include corrupt/adversarial worker traces with `<vote weight="0.3" reason="low-confidence">…</vote>` reweighting examples — taught at SFT time, not just RL.

### 3.3 Stigmergic / Pheromone Coordination (MUST-HAVE for V14)
- **AMRO-S** — *Efficient and Interpretable Multi-Agent LLM Routing via Ant Colony Optimization* (2026): pheromone specialists per task; **4.7× speedup** + cost reduction; pass@1 +1.97%. Adaptive pheromone decay.
- **Pressure Fields and Temporal Decay** — <https://arxiv.org/html/2601.08129v3>: "positive pheromone" effect requires no external memory system — the prompt itself carries reinforcement; temporal decay weakens stale signals.
- **S-MADRL** — Stigmergic Multi-Agent Deep RL: virtual pheromones model local + social interactions, decentralized emergent coordination.
- **Ledger-State Stigmergy** — Heylighen 4-component decomposition: agent + medium + trace + stimulation rule.
- **Implication for V14**: introduce `<pheromone topic="X" strength="0.7" decay="0.9">…</pheromone>` token. Training data: rewrite gather/coordinate traces as pheromone-write + pheromone-read. Decay is a multiplicative half-life applied each turn.

### 3.4 Contract Net / Auctions
- **DALA** (2026) — <https://arxiv.org/html/2511.13193v1>: communication bandwidth as scarce resource; agents bid for "speak" slots based on predicted message value-density.
- **MarketBench** (2026) — <https://arxiv.org/html/2604.23897>: agents-as-market participants. Models forecast their success-prob + token usage poorly → bad bids. Training signal: improve metacognitive self-assessment.
- **Implication for V14**: introduce `<bid task-id="X" price="2000-tokens" confidence="0.8">…</bid>` + `<award winner="agent-7">`. Training data: contract-net traces; reward = task-success / bid-price. This forces the model to learn its OWN cost.

### 3.5 Gossip Protocols (FREE-MAD, AAMAS 2025)
- **arXiv**: <https://arxiv.org/pdf/2509.11035> — *FREE-MAD: Consensus-Free Multi-Agent Debate*
- **Mechanism**: no central aggregator; agents exchange beliefs gossip-style; convergence emerges.
- **Implication for V14**: `<gossip from="agent-3" to="agent-5,agent-7" topic="hypothesis-A">…</gossip>` token. Useful for very large N where centralized aggregation bottlenecks.

### 3.6 Blackboard Architecture (Hayes-Roth 1985, revived 2025)
- **arXiv**: <https://arxiv.org/html/2507.01701v1> — *Exploring Advanced LLM MAS Based on Blackboard*
- **Result**: 13–57% relative improvement over best baseline.
- **Mechanism**: agents read/write a shared blackboard; manager triggers control flow; perfect for very-large-N where direct communication is N².
- **Implication for V14**: `<blackboard-write key="…" value="…">` + `<blackboard-read key="…">` tokens. PC-Agent already validates this with manager + workers.

---

## 4. MARL / RL Frontier — Specifically for LLM Swarms

### 4.1 MARFT (Apr 2025)
- **arXiv**: <https://arxiv.org/abs/2504.16129> — GitHub: <https://github.com/jwliao-ai/MARFT>
- **Contribution**: brand-new Markov game (Flex-MG) aligned with LaMAS optimization; first to ground MARL into language-agent fine-tuning.
- **Impl**: universal RL framework targeting LaMAS specifically.
- **Use for V14**: Flex-MG as the formal substrate. SAC-style with language critic over rollouts.

### 4.2 MARTI v2 (TsinghuaC3I, May 2025 → ICLR 2026)
- **GitHub**: <https://github.com/TsinghuaC3I/MARTI> — Paper: <https://openreview.net/forum?id=E7jZqo0A50>
- **v1**: centralized multi-agent interaction + distributed policy training; multi-turn async rollouts; rule-based + LLM-based generative rewards.
- **v2 (2026)**: tree-search-augmented RL for code generation. Adaptive node expansion + refinement.
- **Reported gain**: +8% over base on Qwen3-8B (V13 cited number).
- **Use for V14**: this is the production framework. Built on OpenRLHF.

### 4.3 MAPoRL2 (ACL 2025)
- **URL**: <https://aclanthology.org/2025.acl-long.1459.pdf>
- **Contribution**: multi-agent post-co-training for *collaboration*. Not just better individual agents — better at collaborating.
- **Reward**: collaboration-quality signal (agent-as-judge + ground truth).
- **Use for V14**: post-MARTI co-training stage to specifically raise collaboration scores.

### 4.4 LangMARL — Natural-Language MARL (2026)
- **arXiv**: <https://arxiv.org/html/2604.00722>
- **CTDE pattern**: Language Policy Actors decentralized + Centralized Language Critic for trajectory-level credit assignment IN LANGUAGE SPACE.
- **Use for V14**: critic emits **language critique** ("agent-3 was redundant on step 5"); agents read critique → policy gradient.

### 4.5 CoLLM-CC / CoLLM-DC (2026)
- **URL**: <https://arxiv.org/html/2601.21972>
- **CC**: centralized critic for joint history value.
- **DC**: decentralized critics for individual-history value.
- **Use for V14**: CC for cooperative tasks (research, coding). DC for competitive (debate, audit).

### 4.6 MALT (Dec 2024)
- **arXiv**: <https://arxiv.org/abs/2412.01928>
- **Setup**: Generator + Verifier + Refiner sequential pipeline. Multi-agent search-tree + ground-truth-graded value-iteration. Off-policy.
- **Result**: +15.66% MATH, +7.42% GSM8K, +9.40% CSQA.
- **Use for V14**: 3-role specialization is the simplest tier-3 hierarchy with measurable RL gains. Works as warm-start before MARFT.

### 4.7 Self-MoA + MoAA (ICML 2025)
- **MoAA**: <https://www.together.ai/blog/moaa> — MoA produces synthetic SFT data + multiple LLMs ensemble as reward model.
- **Self-MoA**: <https://huggingface.co/papers/2502.00674>: aggregating outputs from one top model beats mixing diverse models on AlpacaEval 2.0 (+6.6%) + average +3.8% across MMLU/CRUX/MATH. **Sequential variant**: aggregate large N outputs over rounds — same effectiveness as all-at-once.
- **Implication for V14**: for self-spawn (one model many instances), **Self-MoA is the right MoA**. Sequential ≈ pheromone trail.

---

## 5. Open-Source Swarm Frameworks — Steal Their Wire-Format

### 5.1 OpenAI Swarm + Agents SDK (Oct 2024 → Mar 2025)
- **Repo**: <https://github.com/openai/swarm> (educational); production: Agents SDK.
- **API surface**: agent = system-prompt + functions; **handoff** = function returning a different agent. Two primitives, that's the entire API.
- **Use for V14**: the `<spawn>` token IS a handoff. SDK trace format is good SFT input.

### 5.2 Anthropic Sub-Agents (90% perf claim, 2025)
- **Reference**: <https://www.codewithseb.com/blog/claude-code-sub-agents-multi-agent-systems-guide>
- **Pattern**: each sub-agent is a registered persona; orchestrator calls Task tool; Task tool returns sub-agent output.
- **Use for V14**: persona definitions = role cards. Train the model to emit role-card spec FIRST, then spawn against it.

### 5.3 Google A2A Protocol (Apr 2025, Linux Foundation)
- **Spec**: <https://a2a-protocol.org/latest/specification/>
- **Mechanism**: Agent Card (JSON capability advertisement) + Task object lifecycle + secure message exchange.
- **50+ partners**: Atlassian, Box, Cohere, Intuit, LangChain, MongoDB, PayPal, Salesforce, SAP, ServiceNow.
- **Use for V14**: training data for agent-discovery — model emits its own Agent Card on demand: `<agent-card capabilities="search,code,review" cost-per-call="500-tokens">…</agent-card>`. Production interop = free.

### 5.4 MCP (Anthropic, 2024 → 2025)
- **Latest**: structured content + output schemas + OAuth (Jun 2025).
- **Use for V14**: tool calls in spawned workers should match MCP envelope. Constrained decoding ensures schema compliance.

### 5.5 Swarms (kyegomez)
- **Repo**: <https://github.com/kyegomez/swarms>
- **Patterns**: HierarchicalSwarm (director-worker), ConcurrentWorkflow (parallel exec), SequentialWorkflow (chain), DynamicAgentRearrangement.
- **Use for V14**: each pattern = one training example template. Dynamic-rearrangement traces especially valuable.

### 5.6 GPTSwarm (ICML 2024 oral)
- **arXiv**: <https://arxiv.org/html/2402.16823v3> — GitHub: <https://github.com/metauto-ai/GPTSwarm>
- **Mechanism**: agents-as-graph; node optimization (prompts) + edge optimization (connectivity).
- **Use for V14**: GPTSwarm's optimization traces are gold for MaAS-style training.

### 5.7 MaAS (ICML 2025 Oral, multi-agent architecture search)
- **arXiv**: <https://arxiv.org/abs/2502.04180> — GitHub: <https://github.com/bingreeky/MaAS>
- **Mechanism**: agentic supernet — probabilistic distribution over architectures; query-conditioned MoE controller samples architecture per query.
- **Result**: 6–45% of inference cost vs. handcrafted; +0.54–11.82% accuracy.
- **Use for V14**: training signal — model emits `<topology choice="hierarchical-3"|"gossip"|"blackboard">` early; reward = task-success / cost. Distill MaAS controller into base weights.

### 5.8 OpenHands SDK (Nov 2025)
- **arXiv**: <https://arxiv.org/html/2511.03690v1> — Site: <https://openhands.dev/>
- **SWE-Bench Verified**: 72% (Claude Sonnet 4.5 extended thinking).
- **Native parallel-agent**: large-codebase SDK maps deps and orchestrates parallel changes without conflicts.
- **Use for V14**: parallel-coder sub-agent template. SWE-Gym already converted ADP-format.

### 5.9 ReDel / AgentScope / AgentVerse — large-scale runtime
- **AgentScope**: <https://huggingface.co/papers/2407.17789> — actor-based distributed; scales to many devices; web monitor.
- **AgentVerse**: <https://github.com/OpenBMB/AgentVerse> — task-solving + simulation; emergent behaviors.
- **Use for V14**: their captured logs (when public) are the best 100+-agent trajectory source.

### 5.10 Devin Cognition Multi-Devin (2025)
- **URL**: <https://cognition.ai/blog/devin-annual-performance-review-2025>
- **Mechanism**: managed Devins — parent dispatches isolated VM children; cloud IDE supports many parallel.
- **Use for V14**: Devin's task-management UI = the schema we need. Each managed Devin reports back via structured updates → maps to `<await>` consumes structured `<status>…</status>`.

---

## 6. Massive-Parallel Specifics (100+ concurrent)

### 6.1 PolySwarm (50-persona market trading, 2026)
- **arXiv**: <https://arxiv.org/html/2604.03888v1>
- **Setup**: 50 diverse personas trade on Polymarket; async exec pipeline; paper + live modes.
- **Lesson**: heterogeneity at scale matters more than homogeneous N. Personas DIFFER → specialization → market efficiency.

### 6.2 MyAntFarm.ai (Incident Response, Nov 2025)
- **arXiv**: <https://arxiv.org/abs/2511.15755>
- **Result**: 100% actionable rec rate vs. 1.7% single-agent — multi-agent fundamentally changes outcome distribution.
- **Use for V14**: incident-response is a specific high-value domain to capture — devops-aligned with Surrogate-1 owner.

### 6.3 ChatDev MacNet (1000+ agents)
- **Mechanism**: DAG topology lets each node only see direct ancestors → context never overflows.
- **Use for V14**: train DAG-style traces — agent only references its parent's `<spawn-id>`, not the full history.

### 6.4 Recursive Language Models (Alex Zhang, Oct 2025)
- **URL**: <https://alexzhang13.github.io/blog/2025/rlm/>
- **Mechanism**: language model recursively decomposes input + interacts with prompt as a *variable* in REPL. Symbolic recursion bypasses output-length limits.
- **Use for V14**: `<recurse depth="N">…</recurse>` token. Sub-agent receives a slice of parent's context as a variable, not as text.

### 6.5 LLM-Powered Swarms — Conceptual Stretch? (Jun 2025)
- **arXiv**: <https://arxiv.org/html/2506.14496v1>
- **Critique**: "swarm" branding often overclaims. True swarm needs (a) local-only info, (b) emergent global behavior, (c) no central control. Most LLM-MAS fail (a) or (c).
- **Use for V14**: this is the dimension SwarmBench measures. We must EXPLICITLY train on local-only-info traces (k×k view + local comms) — not just whole-context dispatch.

---

## 7. Datasets — TRACES of Real Multi-Agent Execution

### 7.1 Tier-A: Datasets with explicit dispatch + aggregate

| Dataset | Size | Format | Multi-agent? | URL |
|---------|------|--------|--------------|-----|
| **microsoft/orca-agentinstruct-1M-v1** | 1M | chat-template | Multi-agent flow (synth) | <https://huggingface.co/datasets/microsoft/orca-agentinstruct-1M-v1> |
| **neulab/agent-data-collection (ADP)** | 1.3M | unified action/observation/message | 13 datasets unified | <https://huggingface.co/datasets/neulab/agent-data-collection> |
| **camel-ai/ai_society** | ~100K | role-pair conversations | 2-agent pairs | <https://huggingface.co/datasets/camel-ai/ai_society> |
| **6cf/swarmbench** | 5 task envs | local-view + comms logs | Decentralized N agents | <https://huggingface.co/datasets/6cf/swarmbench> |
| **lambda/hermes-agent-reasoning-traces** | multi-turn | tool-call w/ reasoning | Single-agent w/ tools | <https://huggingface.co/datasets/lambda/hermes-agent-reasoning-traces> |
| **PatronusAI/TRAIL** | 148 traces / 841 errors | OpenTelemetry / OpenInference | Multi-agent w/ failure annotations | <https://huggingface.co/datasets/PatronusAI/TRAIL> |
| **nebius/SWE-agent-trajectories** | 80,036 | SWE-agent format | Coder traces | <https://huggingface.co/datasets/nebius/SWE-agent-trajectories> |
| **open-thoughts/OpenThoughts-Agent-v1-SFT** | 15,200 | GLM-4.6 + Terminus-2 harness | Agentic | <https://huggingface.co/datasets/open-thoughts/OpenThoughts-Agent-v1-SFT> |
| **Multiverse-1K** | 1K | Map/Process/Reduce structure | Parallel decomposition | <https://huggingface.co/Multiverse4FM/Multiverse-32B> |
| **WenyiWU0111/CoMEM-agent-memory-trajectories** | varies | memory-augmented agent | Long-horizon | <https://huggingface.co/datasets/WenyiWU0111/CoMEM-agent-memory-trajectories> |
| **obaydata/mcp-agent-trajectory-benchmark** | 49 | ATIF v1.2 (MCP-style) | MCP traces | <https://huggingface.co/datasets/obaydata/mcp-agent-trajectory-benchmark> |
| **DeepNLP/Coding-Agent-Github-2025-Feb** | varied | GitHub agent runs | Coding agent traces | <https://huggingface.co/datasets/DeepNLP/Coding-Agent-Github-2025-Feb> |

### 7.2 Tier-B: 10+ agent swarm-specific traces (rare, but exists)

| Dataset / Source | What | Where to get |
|------------------|------|--------------|
| **MegaAgent traces** | 590-agent national-policy run | <https://github.com/Xtra-Computing/MegaAgent> (run yourself; logs reproducible) |
| **ChatDev MacNet 1k-agent runs** | DAG topology @ 1000 agents | <https://github.com/OpenBMB/ChatDev> (run + log) |
| **AgentScope 100k-agent simulation** | Actor-distributed runs; web monitor logs | <https://github.com/agentscope-ai/agentscope> |
| **AgentVerse simulation logs** | Emergent collaboration traces | <https://github.com/OpenBMB/AgentVerse> |
| **Swarms framework concurrent traces** | HierarchicalSwarm + ConcurrentWorkflow runs | <https://github.com/kyegomez/swarms> |
| **OWL Workforce GAIA traces** | Planner-Coordinator-Worker | <https://github.com/camel-ai/owl> |

> **Reality**: NO public 10K+ trace dump of 100-agent swarms exists ready-to-train as of May 2026. **You have to GENERATE them** by running these frameworks against your task corpus and capturing the logs. This is exactly what AgentInstruct + ADP did at smaller scale; V14 must run the same playbook for swarm traces.

### 7.3 Tier-C: Synthesis pipeline (the only feasible 100-agent trace source)

```
1. Pick 5 canonical task families (research, code, debug, audit, plan).
2. For each, run MegaAgent OR ChatDev MacNet OR AgentScope with 50-300 agents.
3. Log: full message stream + role assignments + spawning structure.
4. Convert logs → V14 token format (see §11).
5. Filter: keep only successful runs (reward shaping); discard low-quality.
6. Add adversarial traces: inject 5-15% byzantine workers; train weighted-vote.
7. Dataset target: 30K hierarchical (depth ≥3) + 20K decentralized + 10K stigmergic.
```

---

## 8. Reward Functions for Swarm Coordination

### 8.1 Composite reward (training-time)

```
R = w_task * task_success           # 0/1 ground-truth on benchmark
  + w_eff  * (1 - tokens_used / token_budget)
  + w_par  * parallelism_score      # tokens-in-parallel / tokens-total
  + w_coord * coordination_bonus    # see 8.2
  - w_red  * redundancy_penalty     # see 8.3
  - w_byz  * byzantine_acceptance   # see 8.4
```

Recommended starting weights: w_task=1.0, w_eff=0.2, w_par=0.3, w_coord=0.4, w_red=0.3, w_byz=0.5.

### 8.2 Coordination bonus
- +λ for every `<await>` that gathers ≥2 successful workers in parallel.
- +λ for every `<spawn>` whose spawned worker contributes uniquely (judged by overlap-score < 0.3 with siblings).
- +λ for every `<role-card>` whose declared capabilities match actual usage in the spawned worker.

### 8.3 Redundancy penalty
- −μ for every spawn whose output is ≥0.7 cosine-similar to a sibling (waste).
- −μ for >2-deep hierarchy when task is single-task (over-engineered).

### 8.4 Byzantine acceptance
- −ν when model accepts a worker output that contradicts ground truth AND the model didn't use `<vote>` weighting.
- This trains the model to USE WBFT/Fortytwo-style weighting on suspicious workers.

### 8.5 Critic-language reward (LangMARL pattern)
- Centralized language critic emits text critique on full trajectory.
- Critique converted via small reward-model (qwen3.5:2b suffices) to scalar.
- Per-agent advantage = critique-mention-of-agent contribution.

### 8.6 Pairwise Bradley-Terry (Fortytwo)
- Sample 2 sibling-worker outputs; have judge pick winner.
- Update both with BT update (+log p_win, −log p_lose).
- This is the simplest possible swarm-aware preference signal — directly addresses the "which sibling deserves credit" problem.

### 8.7 MARTI tree-search reward
- Tree node = (spawn graph state, message log).
- Expand by adding sibling spawn or refining existing.
- Backpropagate value from leaf rollouts (task success).
- Train with PPO over node-action pairs.

---

## 9. SwarmBench — Calibration Eval

Use these 5 tasks as the V14 swarm-quality eval:
1. **Pursuit** — coordinated chase under partial observation.
2. **Synchronization** — agents must align actions in time.
3. **Foraging** — find resource → return to nest → coordinate task allocation.
4. **Flocking** — alignment + separation + cohesion.
5. **Transport** — joint physical effort on large object.

**Pre-V14 baseline (current frontier zero-shot, May 2025 paper)**:
- All current LLMs significantly struggle with long-range planning + adaptive strategy under decentralization.
- Behavioral flexibility explains 24.5% of score variance.

**V14 target**: substantial improvement on at least 3 of 5 tasks via SFT on synthesized swarm traces + MARFT critic.

Other relevant benchmarks:
- **TheAgentCompany** — 175 real-world tasks; current best: Claude 30% (with partial credit 34.4%). Long-horizon office work.
- **GAIA** — OWL achieved 69.70%.
- **HLE-Full with tools** — Kimi K2.6 leads at 54.0.
- **GPQA Diamond** — Fortytwo swarm 85.90% (vs. 68.69% majority).
- **AgentBench (THUDM)** — comprehensive agent eval.

---

## 10. T4×2 Feasibility Notes

| Component | T4×2 16GB ea (32GB total) verdict |
|-----------|-----------------------------------|
| 27B base SFT, LoRA r=16 | Tight but doable with 4-bit quant + offload (DeepSpeed ZeRO-3 + CPU offload). ≈8-10 tok/s training. |
| 27B base full-finetune | NO — needs 4×A100. Stick with LoRA. |
| 100-agent inference simulation | Run vLLM single instance + 100 *logical* agents share one model serving — no multi-replica needed (KV cache shared). |
| MARFT/MARTI rollout | Use offline rollouts: generate trajectories on Lightning H200/Modal → cache → train LoRA on T4×2. |
| Self-MoA inference | Sequential variant ≈ N forward passes on same model; T4×2 handles N=8–16 fine. |
| Pairwise BT rerank | T4×2 + qwen3.5:9b judge runs locally at ≈3-4 tok/s → use only at val time, not every step. |
| Pheromone field | Pure key-value dict — no compute. |
| Critic (LangMARL) | qwen3.5:9b on T4×2 second card while base trains on first → manageable. |

**Pipeline**: spawn-aware SFT on T4×2 (LoRA) → upload → MARFT rollouts on H200 (rented) → download advantages → second LoRA pass on T4×2.

---

## 11. Wire-Into-V14+ for Swarm

This section is the actionable part. Everything above informs HOW; this section is the WHAT-TO-CHANGE.

### 11.1 Token vocab extension (delta from V13)

V13 tokens (keep): `<spawn>`, `</spawn>`, `<await>`, `</await>`, `<aggregate>`, `</aggregate>`.

V14+ tokens (NEW — add 14 special tokens):

| Token | Purpose | Example |
|-------|---------|---------|
| `<broadcast topic="X">…</broadcast>` | One-to-many message | Used by tier-1 to all tier-2 admins |
| `<bid task-id="X" cost="N" conf="0.8">…</bid>` | Worker bid in contract-net | Worker proposes to handle task |
| `<award winner="agent-7">` | Manager awards bid | Closes contract-net round |
| `<vote method="bradley-terry|wbft" weight="0.7">…</vote>` | Weighted vote | BFT consensus |
| `<pheromone topic="X" strength="0.8" decay="0.9">…</pheromone>` | Stigmergic write | Public hint to other agents |
| `<read-pheromone topic="X" min-strength="0.3">` | Stigmergic read | Pull recent traces |
| `<gossip from="A" to="B,C" topic="X">…</gossip>` | P2P message | Decentralized communication |
| `<barrier id="N" wait-for="agent-1,agent-2">` | Synchronization point | Block until all listed complete |
| `<role-card name="X" tier="N" capabilities="…" cost-per-call="N">…</role-card>` | Capability advertisement | A2A-style |
| `<topology choice="hierarchical|gossip|blackboard|dag|puppeteer">` | Topology selection | Emitted at task start |
| `<blackboard-write key="X">…</blackboard-write>` | Shared state write | PC-Agent style |
| `<blackboard-read key="X">` | Shared state read | |
| `<recurse depth="N">…</recurse>` | RLM-style recursive call | Sub-agent gets sliced context |
| `<critique target="agent-3" rating="0.6">…</critique>` | LangMARL critic | Trajectory critique in language |

Total special tokens added in V14: **20** (V13: 6 → V14: 26).

Tokenizer note: register all as a single block in `tokenizer_config.json`. Pre-train cosine warmup of 200 steps to align embeddings (V13 lessons applied).

### 11.2 Dataset additions (delta from V13's 200K mix)

| Dataset / synthesis | Size target | Generation source | Format conversion |
|---------------------|-------------|-------------------|-------------------|
| **Hierarchical-3 traces** | 30K | MegaAgent runs (boss/admin/worker) on 5 task families | Map levels → tier=1/2/3 in `<role-card>` |
| **Decentralized swarm** | 20K | SwarmBench env runs + synthetic gossip rollouts | Map local-view + comms → `<gossip>` + `<pheromone>` |
| **Stigmergic** | 10K | AMRO-S routing traces + pressure-field synth | Re-tag direct messages → `<pheromone>` |
| **Auction / contract-net** | 10K | DALA + MarketBench replay | Map bid/award → `<bid>` + `<award>` |
| **Byzantine-resilient** | 10K | Synth: 90% honest + 10% byzantine workers; manager weighted-vote | Map weighted aggregation → `<vote method="wbft">` |
| **Blackboard** | 10K | PC-Agent style blackboard runs | Read/write tokens |
| **Self-MoA sequential** | 10K | Generate 16 candidates per query, then sequential aggregate | `<aggregate method="self-moa">` |
| **Long-horizon (Kimi-style)** | 5K | 4000-step coding rollouts (sub-sample) | Recursive `<spawn>` chains |

Total V14 swarm-specific addition: **~105K traces** on top of V13's 200K.

### 11.3 Reward function (consolidated)

```python
# pseudocode for the reward used in MARFT/MARTI rollouts
def swarm_reward(traj, task_success, judge_pairs):
    R = 0.0
    R += 1.0 * task_success                                          # primary
    R += 0.2 * efficiency(traj.tokens, traj.budget)                  # token frugality
    R += 0.3 * parallelism_score(traj.spawn_graph)                   # parallel breadth
    R += 0.4 * coordination_bonus(traj)                              # see 8.2
    R -= 0.3 * redundancy_penalty(traj)                              # 8.3
    R -= 0.5 * byzantine_acceptance(traj)                            # 8.4
    R += 0.3 * sum(bradley_terry_update(p) for p in judge_pairs)     # 8.6
    R += 0.2 * critic_language_score(traj)                           # LangMARL
    return R
```

### 11.4 Inference runtime additions (≤200 LOC parser)

```python
# Pseudocode of the V14 dispatcher (extends V13's <spawn>/<await>/<aggregate> parser)
class SwarmDispatcher:
    def __init__(self, model, max_concurrent=300):  # Kimi-scale
        self.model = model
        self.blackboard = {}                # blackboard-write/read backing
        self.pheromone = PheromoneField()  # decay-aware key-value
        self.bids = []                      # active contract-net rounds
        self.barriers = {}                  # barrier-id → set of waited agents
        self.rolecards = {}                 # name → capabilities

    async def dispatch_one(self, msg):
        match parse_token(msg):
            case Spawn(role, parent_id):    return await self.spawn(role, parent_id)
            case Broadcast(topic, body):    return self.broadcast(topic, body)
            case Bid(task_id, cost, conf):  self.bids.append(...); return None
            case Award(winner):             return self.award(winner)
            case Vote(method, weight, body): return self.vote(method, weight, body)
            case Pheromone(topic, strength, decay): self.pheromone.write(topic, strength, decay)
            case ReadPheromone(topic, min_s): return self.pheromone.read(topic, min_s)
            case Gossip(from_, to, topic, body): return self.send_gossip(from_, to, topic, body)
            case Barrier(id_, wait_for):    return await self.wait_barrier(id_, wait_for)
            case RoleCard(name, tier, caps, cost): self.rolecards[name] = (tier, caps, cost)
            case Topology(choice):          self.set_topology(choice)
            case BlackboardWrite(key, val): self.blackboard[key] = val
            case BlackboardRead(key):       return self.blackboard.get(key)
            case Recurse(depth, ctx):       return await self.recurse(depth, ctx)
            case Critique(target, rating, body): self.log_critique(target, rating, body)
```

The pheromone field has 2 ops: write (with decay rate) + read (min-strength filter). Each turn, multiply all strengths by their per-pheromone decay constant. Threshold below ε ⇒ delete.

### 11.5 Topology training schedule

Hour 0–1: SFT on V13 base mix + new vocab cold-start.
Hour 1–4: SFT on hierarchical-3 traces (30K).
Hour 4–6: SFT on decentralized swarm (20K).
Hour 6–7: SFT on stigmergic (10K).
Hour 7–8: SFT on auction + byzantine + blackboard (30K total).
Hour 8: Save LoRA-1, upload.
Hour 8–24 (rented H200): MARFT/MARTI tree-search RL with composite reward.
Hour 24–32: LoRA-2 merge into LoRA-1.
Hour 32+: SwarmBench eval; iterate.

### 11.6 Validation gates (must pass before merging into main)

1. **Vocab**: tokenizer round-trip exact for all 26 special tokens.
2. **Parser**: 200 synthetic traces parse without errors, 100% span coverage.
3. **Hierarchical-3**: ≥80% of generations include exactly 3 tiers when prompted with depth=3 task.
4. **Decentralized**: model abstains from emitting `<spawn>` when prompted with local-view + gossip-only constraints.
5. **Pheromone decay**: pheromone strengths halve per N turns (assert via log analysis).
6. **Byzantine**: when 1-of-5 workers is corrupted (synth), model emits `<vote weight="…">` that down-weights corrupted output ≥70% of the time.
7. **SwarmBench delta**: improvement on ≥3 of 5 tasks vs. V13 baseline.
8. **Anthropic-style breadth eval**: +20% over V13 on broad research queries.
9. **Cost**: total inference cost per query ≤2× V13 (avoid unbounded swarm spawning).

### 11.7 Non-goals for V14
- Train novel base architecture (use Qwen3.5/Qwen3.6 as base).
- Replace V13's `<spawn>` semantics (extend, don't break).
- 1000+ agents (Kimi territory; needs distributed runtime infrastructure beyond T4×2).
- Real-time RL (offline only; rollouts on rented compute).

---

## 12. Open Questions / TODOs

- [ ] Quantify whether stigmergic + blackboard subsumes pure direct-message at N≥50, or if direct still needed for rapid response. Run ablation post-V14 ship.
- [ ] Decide whether `<role-card>` tier labels are emergent (model decides) or labeled (training-time fixed). Lean: emergent, with bias from synthetic mix.
- [ ] Self-MoA sequential variant — at what N does it plateau? Test {4, 8, 16, 32, 64}.
- [ ] BT vs. WBFT vs. simple-mean: which one ships as default `<aggregate>` semantics? Ablate on TheAgentCompany.
- [ ] Worker-context-window — pass full parent context, or RLM-style sliced variable? Lean: sliced for >5 hops, full ≤5.
- [ ] LangMARL critic — same model, second LoRA? or separate qwen3.5:9b? Test both; lean separate.

---

## 13. Sources / Citations

### Frontier proof-points
- Anthropic, *How we built our multi-agent research system* (Jun 2025): <https://www.anthropic.com/engineering/multi-agent-research-system>
- Moonshot AI, *Kimi K2.6* (Apr 2026): <https://kimi-k2.org/blog/24-kimi-k2-6-release>
- Cognition Labs, *Devin's 2025 Performance Review* (2025): <https://cognition.ai/blog/devin-annual-performance-review-2025>

### Hierarchical
- MegaAgent, ACL 2025 Findings: <https://arxiv.org/abs/2408.09955> + <https://github.com/Xtra-Computing/MegaAgent>
- OWL Workforce, NeurIPS 2025: <https://arxiv.org/abs/2505.23885> + <https://github.com/camel-ai/owl>
- HiAgent, ACL 2025: <https://arxiv.org/abs/2408.09559>
- Society-of-Mind revisit (Mindstorms): <https://arxiv.org/abs/2305.17066>

### Decentralized / Swarm
- SwarmBench (May 2025): <https://arxiv.org/abs/2505.04364> + <https://github.com/RUC-GSAI/YuLan-SwarmIntell>
- Byzantine-Robust Decentralized Coordination (Jul 2025): <https://arxiv.org/abs/2507.14928>
- WBFT (May 2025): <https://repositum.tuwien.at/bitstream/20.500.12708/217904/1/Luo%20Haoxiang%20-%202025-05-08>
- Fortytwo (Oct 2025): <https://arxiv.org/abs/2510.24801>
- FREE-MAD (AAMAS 2025): <https://arxiv.org/pdf/2509.11035>
- LLM-Powered Swarms (Jun 2025): <https://arxiv.org/html/2506.14496v1>

### Stigmergic / Pheromone
- AMRO-S (2026): <https://arxiv.org/html/2603.12933>
- Emergent Coordination via Pressure Fields: <https://arxiv.org/html/2601.08129v3>
- Multi-Agent Systems Powered by LLMs in Swarm Intelligence: <https://arxiv.org/abs/2503.03800>

### Contract Net / Auctions
- DALA (2026): <https://arxiv.org/html/2511.13193v1>
- MarketBench (2026): <https://arxiv.org/html/2604.23897>
- Agent Contracts (COINE 2026): <https://arxiv.org/html/2601.08815>

### MARL / RL
- MARFT (Apr 2025): <https://arxiv.org/abs/2504.16129> + <https://github.com/jwliao-ai/MARFT>
- MARTI (May 2025, ICLR 2026): <https://github.com/TsinghuaC3I/MARTI> + <https://openreview.net/forum?id=E7jZqo0A50>
- MAPoRL2 (ACL 2025): <https://aclanthology.org/2025.acl-long.1459.pdf>
- LangMARL (2026): <https://arxiv.org/html/2604.00722>
- CoLLM-CC / CoLLM-DC (2026): <https://arxiv.org/html/2601.21972>
- MALT (Dec 2024): <https://arxiv.org/abs/2412.01928>
- Self-MoA: <https://huggingface.co/papers/2502.00674>
- MoAA (ICML 2025): <https://www.together.ai/blog/moaa>

### Frameworks / SDKs
- OpenAI Swarm: <https://github.com/openai/swarm>
- A2A Protocol (Linux Foundation, Apr 2025): <https://a2a-protocol.org/latest/specification/>
- Swarms (kyegomez): <https://github.com/kyegomez/swarms>
- GPTSwarm (ICML 2024 oral): <https://arxiv.org/html/2402.16823v3> + <https://github.com/metauto-ai/GPTSwarm>
- MaAS (ICML 2025 oral): <https://arxiv.org/abs/2502.04180> + <https://github.com/bingreeky/MaAS>
- ChatDev: <https://github.com/OpenBMB/ChatDev>
- AgentVerse: <https://github.com/OpenBMB/AgentVerse>
- AgentScope (large-scale): <https://huggingface.co/papers/2407.17789>
- OpenHands SDK (Nov 2025): <https://arxiv.org/html/2511.03690v1>

### Datasets
- microsoft/orca-agentinstruct-1M-v1: <https://huggingface.co/datasets/microsoft/orca-agentinstruct-1M-v1>
- neulab/agent-data-collection (ADP): <https://huggingface.co/datasets/neulab/agent-data-collection>
- camel-ai/ai_society: <https://huggingface.co/datasets/camel-ai/ai_society>
- 6cf/swarmbench: <https://huggingface.co/datasets/6cf/swarmbench>
- nebius/SWE-agent-trajectories: <https://huggingface.co/datasets/nebius/SWE-agent-trajectories>
- PatronusAI/TRAIL: <https://huggingface.co/datasets/PatronusAI/TRAIL>
- lambda/hermes-agent-reasoning-traces: <https://huggingface.co/datasets/lambda/hermes-agent-reasoning-traces>
- Multiverse-1K: <https://github.com/Infini-AI-Lab/Multiverse>

### Benchmarks
- TheAgentCompany (2024): <https://arxiv.org/abs/2412.14161>
- AgentBench (THUDM, ICLR 2024): <https://github.com/THUDM/AgentBench>
- HLE-Full (cited in Kimi K2.6 release)
- GAIA (cited in OWL Workforce)
- GPQA Diamond (cited in Fortytwo)

### Misc
- Recursive Language Models (Oct 2025): <https://alexzhang13.github.io/blog/2025/rlm/>
- DyLAN (COLM 2024): <https://arxiv.org/abs/2310.02170>
- PRefLexOR (Oct 2024): <https://arxiv.org/abs/2410.12375>
- PolySwarm (2026): <https://arxiv.org/html/2604.03888v1>
- MyAntFarm.ai (Nov 2025): <https://arxiv.org/abs/2511.15755>
- Multi-Agent Collaboration Survey (Jan 2025): <https://arxiv.org/html/2501.06322v1>
- AgentTrek (ICLR 2025 Spotlight): <https://github.com/xlang-ai/AgentTrek>
- Agent Data Protocol (ADP, 2025): <https://arxiv.org/html/2510.24702v1>
- Blackboard Architecture for LLM-MAS (Jul 2025): <https://arxiv.org/html/2507.01701v1>
- Memory in LLM-based MAS Survey (Dec 2025): <https://www.techrxiv.org/users/1007269/articles/1367390>

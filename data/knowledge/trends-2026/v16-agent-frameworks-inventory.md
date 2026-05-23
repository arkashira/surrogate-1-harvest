---
name: V16 Agent Frameworks Inventory — comprehensive catalog
description: Every agent framework / runtime / multi-agent system 2024-2026. What we can adopt, what we can mine for training data, what special-token formats are compatible with V15's 28-token set. For Surrogate-1 V16 trainer planning.
tags: [v16, agent-frameworks, multi-agent, training-data, special-tokens, surrogate-1, 2026]
created: 2026-05-01
supersedes: [[v14-swarm-agents-at-scale]] (extends, does not replace)
related: [[v13-multi-agent-baked-in]], [[v14-arxiv-github-sweep-may2026]]
---

# V16 Agent Frameworks Inventory — comprehensive catalog (2024–2026)

> **For Surrogate-1 V16 trainer**: which frameworks to mine for training-data, which protocols to encode as special tokens, which benchmarks to add to `bench-v1-vs-v15.sh`.
>
> **V15 baseline = 28 multi-agent special tokens**: `<spawn>`, `</spawn>`, `<await/>`, `<aggregate>`, `</aggregate>`, `<worker_result>`, `</worker_result>`, `<plan/>`, `<broadcast>`, `</broadcast>`, `<bid>`, `</bid>`, `<award/>`, `<vote>`, `</vote>`, `<pheromone>`, `</pheromone>`, `<read_pheromone/>`, `<gossip>`, `</gossip>`, `<barrier/>`, `<role_card>`, `</role_card>`, `<topology/>`, `<blackboard_write>`, `</blackboard_write>`, `<blackboard_read/>`, `<recurse>`, `</recurse>`, `<critique>`, `</critique>` (= 30 listed but model registers 28 after dedupe; counts include opening + closing).

## Section 1 — Master Catalog (35 frameworks)

| # | Framework | Vendor | License | Last Update | Pattern | Active? | Repo / Site |
|---|---|---|---|---|---|---|---|
| 1 | **Microsoft Agent Framework 1.0** | Microsoft | MIT | 2026-04-03 GA | Hierarchical + graph workflow + A2A + MCP | YES — replaces AutoGen + Semantic Kernel | [microsoft/agent-framework](https://github.com/microsoft/agent-framework) |
| 2 | **AutoGen v0.5** | Microsoft | MIT | 2026-Q1 (maintenance) | Async event-driven multi-agent | maintenance — innovation flows to MAF | [microsoft/autogen](https://github.com/microsoft/autogen) |
| 3 | **Semantic Kernel** | Microsoft | MIT | merged into MAF 2026 | Plugin/skill orchestration | merged | [microsoft/semantic-kernel](https://github.com/microsoft/semantic-kernel) |
| 4 | **Magentic-One** | Microsoft | MIT | 2026-Q1 | Orchestrator + WebSurfer + FileSurfer + Coder + ComputerTerminal | active (in MAF) | [arxiv/2411.04468](https://arxiv.org/abs/2411.04468) |
| 5 | **TypeAgent** | Microsoft | MIT | 2026 active | Schema-first natural-language UI; TypeChat schemas | active research | [microsoft/TypeAgent](https://github.com/microsoft/TypeAgent) |
| 6 | **AgentScope 1.x** | Alibaba Tongyi | Apache-2.0 | 2026-04 (Studio + A2A added 2025-12) | ReAct + distributed + A2A + Java + visual | YES — production | [agentscope-ai/agentscope](https://github.com/agentscope-ai/agentscope) |
| 7 | **CAMEL-AI** | EleutherAI-style consortium | Apache-2.0 | 2026-03-23 | Role-playing dialogues; data-generation focus | YES | [camel-ai/camel](https://github.com/camel-ai/camel) |
| 8 | **OASIS** | CAMEL-AI | Apache-2.0 | 2025-12 | 1M-agent social simulation | YES | [camel-ai/oasis](https://github.com/camel-ai/oasis) |
| 9 | **OWL** (Optimized Workforce Learning) | CAMEL-AI | Apache-2.0 | 2026-04 | Hierarchical Workforce: Planner + Coordinator + Workers; #1 OSS GAIA = 69.09% | YES — NeurIPS 2025 | [camel-ai/owl](https://github.com/camel-ai/owl) |
| 10 | **MetaGPT** | FoundationAgents | MIT | 2026 active | 5-role software-company (PM + Architect + PjM + Eng + QA); shared message pool | YES | [FoundationAgents/MetaGPT](https://github.com/FoundationAgents/MetaGPT) |
| 11 | **CrewAI** | crewAIInc | MIT | 2026 active (AMP platform) | Role-playing crew + tracing + AMP | YES — commercial | [crewAIInc/crewAI](https://github.com/crewAIInc/crewAI) |
| 12 | **LangGraph 1.0** | LangChain | MIT | 2026 v1.0 GA | Stateful graph orchestration; durable state + HITL + LangSmith tracing | YES — production | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) |
| 13 | **Letta (was MemGPT)** | Letta Inc | Apache-2.0 | 2026 active | Memory-tier agents (Core/Recall/Archival); Letta Code; Letta Evals | YES | [letta-ai/letta](https://github.com/letta-ai/letta) |
| 14 | **OpenAI Agents SDK** | OpenAI | Apache-2.0 | 2025-03 (replaces Swarm); 2026 active | Routines + handoffs + guardrails + tracing + sessions | YES — supported | [openai/openai-agents-python](https://github.com/openai/openai-agents-python) |
| 15 | **OpenAI Swarm** | OpenAI Solution team | MIT | 2024 (frozen) | Educational primitive — Agent + handoff | superseded | [openai/swarm](https://github.com/openai/swarm) |
| 16 | **Pydantic AI 1.85** | Pydantic team | MIT | 2026-04-22 | Type-safe agent + structured output + Logfire tracing | YES — 16.5K stars | [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai) |
| 17 | **smolagents** | HuggingFace | Apache-2.0 | 2026 active (succeeds transformers.agents) | Code-as-action: agent writes Python; <1K LOC core; Hub-shareable tools | YES — official HF path | [huggingface/smolagents](https://github.com/huggingface/smolagents) |
| 18 | **Swarms** (kyegomez) | The-Swarm-Corporation | MIT | 2026 active | Hierarchical/parallel/sequential/graph + microservices | YES — enterprise marketing | [kyegomez/swarms](https://github.com/kyegomez/swarms) |
| 19 | **AgentVerse** | OpenBMB | Apache-2.0 | 2024 paper, maintenance | Task-solving + simulation; 4 datasets evaluated | low activity | [OpenBMB/AgentVerse](https://github.com/OpenBMB/AgentVerse) |
| 20 | **OpenAgents (XLang)** | XLang Lab | Apache-2.0 | COLM 2024; superseded by OpenCUA | Data + Plugins + Web agents | succeeded by OpenCUA | [xlang-ai/OpenAgents](https://github.com/xlang-ai/OpenAgents) |
| 21 | **OpenCUA** | XLang Lab | Apache-2.0 | 2025 (active 2026) | Computer-use agents; AgentNet + AgentNetTool + AgentNetBench | YES | [xlang-ai/OpenCUA](https://github.com/xlang-ai/OpenCUA) |
| 22 | **OpenHands (was OpenDevin)** | OpenHands collective | MIT | 2026 (Index Jan 2026) | Generalist coder + ComputerInteraction; SWE-bench Verified 53%+ with Claude 4.5 | YES — production | [OpenHands/OpenHands](https://github.com/OpenHands/OpenHands) |
| 23 | **Devika** | stitionai | MIT | low activity since 2024 | Agentic SE clone of Devin | abandoned-ish | [stitionai/devika](https://github.com/stitionai/devika) |
| 24 | **Aider** | aider-AI | Apache-2.0 | 2026 active; 39K stars; 4.1M installs; 15B tokens/week | Pair-programmer terminal | YES — pioneer | [Aider-AI/aider](https://github.com/Aider-AI/aider) |
| 25 | **Cline 3.58** | cline | Apache-2.0 | 2026-02 native subagents | IDE-native (VSCode/JetBrains/Neovim/Emacs); read-only subagents w/ separate ctx | YES | [cline/cline](https://github.com/cline/cline) |
| 26 | **Continue** | continuedev | Apache-2.0 | 2026 active | BYOM IDE assistant | YES | [continuedev/continue](https://github.com/continuedev/continue) |
| 27 | **Claude Agent SDK** (was Claude Code SDK) | Anthropic | proprietary harness + permissive SDK | 2026 (Claude Managed Agents launched 2026-04-08) | Subagents + skills + sessions + parallel MCP | YES — primary | [anthropics/claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python) |
| 28 | **BeeAI Framework** | IBM (Linux Fdn governance) | Apache-2.0 | 2026 active | Multi-agent w/ Granite + Llama; ACP protocol (extends MCP) | YES — Linux Foundation | [i-am-bee/beeai-framework](https://github.com/i-am-bee/beeai-framework) |
| 29 | **Haystack 2 + Hayhooks** | deepset | Apache-2.0 | 2026 active | Modular pipelines + agents; Hayhooks = MCP/REST exposure | YES | [deepset-ai/haystack](https://github.com/deepset-ai/haystack) |
| 30 | **Mirascope** | Mirascope | MIT | 2026 active | LLM "anti-framework"; structured outputs; multi-provider | YES | [Mirascope/mirascope](https://github.com/Mirascope/mirascope) |
| 31 | **Marvin** | PrefectHQ | Apache-2.0 | 2026 maintenance | AI fns + entity extraction | low | (Prefect ecosystem) |
| 32 | **Vertex AI Agent Builder / Gemini Enterprise Agent Platform / ADK** | Google | Apache-2.0 (ADK) + commercial platform | 2026 Cloud Next rebrand | ADK code-first multi-language (Py/Go/Java/TS) + Agent Studio low-code; native A2A | YES — production | [google-adk](https://google.github.io/adk-docs/a2a/) |
| 33 | **Bedrock AgentCore** | AWS | commercial | 2026-Q1/Q2 (multiple GA in Mar–Apr 2026) | Managed runtime + AG-UI + stateful MCP + shell exec + Node.js + session storage | YES — production | [aws.amazon.com/bedrock/agentcore](https://aws.amazon.com/bedrock/agentcore/) |
| 34 | **NVIDIA AI-Q + NeMo Agent Toolkit** | NVIDIA | Apache-2.0 | 2026-03 GTC | LangChain hybrid (frontier orch + Nemotron heavy lifting); OpenShell guardrails; cuOpt | YES | [build.nvidia.com/nvidia/aiq](https://build.nvidia.com/nvidia/aiq) |
| 35 | **Cohere North** | Cohere | commercial (model-private deploy) | 2025 GA, 2026 active | Enterprise agent on Command; MCP-enabled; private deploy | YES | [cohere.com](https://cohere.com/) |
| 36 | **ReDel** (research) | UPenn | MIT | 2024 EMNLP (research-quality) | Recursive multi-agent; DelegateOne/DelegateWait | research | [zhudotexe/redel](https://github.com/zhudotexe/redel) |
| 37 | **AgentTuning / AgentLM (THUDM)** | Tsinghua KEG | Apache-2.0 | 2024 paper (referenced in 2026 work) | Hybrid instruction-tune; AgentInstruct 1,866 trajectories | trainer | [THUDM/AgentTuning](https://github.com/THUDM/AgentTuning) |

## Section 2 — Public Training-Data Dumps (mine these directly)

The big breakthrough vs V15 research: **multiple frameworks now ship their trace logs**. We can train on real agent runs, not synthetic.

### Tier-1 (production-quality, license-clean, commercial-clean) — DO MINE

| # | Dataset | Size | Source | License | What's in it | Action item for V16 |
|---|---|---|---|---|---|---|
| 1 | **SWE-smith-trajectories** | 5,017 traj × multi-turn (used to train SWE-agent-LM-32B → 40.2% SWE-bench Verified) | NeurIPS 2025 D&B Spotlight | Apache-2.0 | Real GitHub issue → patch trajectories from Qwen 2.5 Coder Instruct on 50K SWE-smith tasks | extend `merge_external()` in trainer-v15 → +5K pairs |
| 2 | **SWE-rebench-OpenHands-Trajectories** (Nebius) | 80,036 trajectories | HF: `nebius/SWE-rebench-openhands-trajectories` | Apache-2.0 | Multi-turn agent traj; Qwen3-Coder-480B + OpenHands v0.54 scaffold | bump V9 OpenDevin slot from 5K → 15-20K |
| 3 | **SWE-agent-trajectories** (Nebius) | 80,036 trajectories | HF: `nebius/SWE-agent-trajectories` | Apache-2.0 | Various models as action generators | dedup against #2 then add ~10K |
| 4 | **AgentNet** (OpenCUA) | 22,536 human-annotated computer-use tasks across Win/macOS/Ubuntu | HF: `xlangai/AgentNet` | Apache-2.0 | 200+ apps, 190+ websites; reflective CoT synthesis | NEW corpus for V16 — **GUI-agent capability** (~10-15K pairs distilled) |
| 5 | **AgentInstruct** (THUDM) | 1,866 high-quality multi-task trajectories across 6 agent tasks | HF: `THUDM/AgentInstruct` | Apache-2.0 | Hybrid agent + general training proven to generalize | already in V8 small slot — bump to full 1,866 |
| 6 | **TRAIL** (PatronusAI) | 148 traces (118 GAIA + 30 SWE-Bench), 1,987 OTel spans, 575 with errors | HF: `PatronusAI/TRAIL` | research-permissive | OpenTelemetry-instrumented multi-agent spans + error labels | NEW: train on debugging-agent-traces (300 pairs) |
| 7 | **τ-bench (sierra-research)** | historical_trajectories for retail + airline (multi-turn tool-agent-user) | sierra-research/tau-bench (GitHub) | MIT | Tool-Agent-User dialog traces, policy-aware | NEW: ~3K dialog traces for tool-following discipline |
| 8 | **τ²-bench** | enterprise scenarios | sierra-research/tau2-bench | MIT | Multi-domain tool-following | NEW: extend τ-bench inclusion |
| 9 | **OASIS social-simulation logs** | 1M-agent simulations | camel-ai/oasis | Apache-2.0 | Information-spread, polarization traces | NICHE — not in V16 default mix |
| 10 | **CAMEL synthetic-data tools (CoT, Self-Instruct)** | scalable | camel-ai/camel | Apache-2.0 | Pipelines to **GENERATE** training data on-demand | **WIRE INTO V16 trainer** — generate role-specific data |
| 11 | **agentlm-7b/13b/70b checkpoints** | weights | HF: `zai-org/agentlm-*` | research/Llama-derivative | Reference distillation target | distill onto our 14B/32B base |
| 12 | **SWE-Gym** | 2,438 task instances + agent traj | HF: `SWE-Gym` | Apache-2.0 | ICML 2025 — proven +14% gain on SWE-bench Verified | Already in V9 slot — keep |
| 13 | **DeepSWE traces** (Together) | RL-trained coding-agent traj | togethercomputer | Apache-2.0 | Fully open SOTA coding-agent | NEW: ~5-10K pairs |
| 14 | **HuggingFace agent-trace-viewer dumps** | various | HF: changelog/agent-trace-viewer | various | Trace-viewer-format dumps | scan for matching license; opportunistic |
| 15 | **AutoGen public datasets** (`lukaskellerstein/autogen`, `tosin2013/autogen`, `robkayinto/autogen-python`) | community | HF | various | community-curated AutoGen runs | low-priority; quality-mixed |

### Tier-2 (frameworks with PUBLIC APIS to GENERATE traces) — for synthetic expansion

| Framework | API to generate traces | License of traces produced | Approach |
|---|---|---|---|
| **CAMEL-AI** | `camel-ai` Python lib has Chain-of-Thought + Self-Instruct generators | output is yours (Apache-2.0 framework) | Run on V16 task corpus → produce role-playing pairs |
| **AgentScope-Studio** | OpenTelemetry export of agent runs | yours | Run on `cdk-infrastructure/` workflows |
| **smolagents** | code-action agent runs are deterministic + Hub-shareable | Apache-2.0 | Generate code-agent corpus on RD-Excise repos |
| **OpenHands** | Run on real or synthetic GitHub issues; export trajectories | MIT | Run on Excise + DevOps repos; collect 5-10K |
| **Letta Evals** | Open-source eval framework | Apache-2.0 | Generate stateful-agent eval traces |
| **AGENT.SO / Cline** | Per-subagent trace JSON in `~/.claude/tasks/` etc. | yours | Mine YOUR existing Cline/Claude Code transcripts (4-12K pairs available locally) |

### Tier-3 (closed but public eval suites — ADD TO BENCH not trainer)

| Bench | Domain | Format | Add to `bench-v1-vs-v15.sh`? |
|---|---|---|---|
| **GAIA** | General assistant (165 val Qs) | Princeton HAL leaderboard | YES — already gold-standard |
| **WebArena** | 812 web-nav tasks across 5 sites | CMU | YES — for GUI-agent capability |
| **τ-bench** + **τ²-bench** | Tool-Agent-User dialogue | Sierra | YES — primary tool-following |
| **SWE-bench Verified** | Coder | Princeton | YES — already in V15 |
| **SWE-bench Pro** | 2,000+ contamination-free | 2026 release | YES — must add |
| **AgentBench** (THUDM) | 8 environments | ICLR'24 | already in V14 |
| **OSWorld + AgentNetBench** | Computer-use | xlang | YES if we add GUI capability |
| **OpenHands Index** | issue resolution + greenfield + frontend + testing | OpenHands 2026-01 | YES — broad real-world |
| **Holistic Agent Leaderboard (HAL)** | Princeton, multi-bench harness | princeton-pli/hal-harness | YES — single harness for many |
| **TRAIL** | trace-debugging | Patronus | YES — train AND eval |

## Section 3 — Special-Token Format Compatibility Matrix vs V15 28-token set

| V15 token | Equivalent in framework | Compatible? | Notes |
|---|---|---|---|
| `<spawn>...</spawn>` | AutoGen `register_agent` event; AgentScope `Pipeline.spawn`; OpenAI Agents `handoff`; ReDel `DelegateOne` | YES | All converge on tag-style; emit during traj |
| `<await/>` | Agent Framework "Workflow" wait state; LangGraph node await; AgentScope `barrier` | YES | maps to `<barrier/>` in some |
| `<aggregate>...</aggregate>` | LangGraph `END` reducer; AutoGen `GroupChatManager` aggregate; OWL Coordinator | YES | one-to-one mapping |
| `<worker_result>...</worker_result>` | OpenAI Agents `Result.final_output`; ReDel child-agent return | YES | identical semantics |
| `<plan/>` | MetaGPT plan; LangGraph plan node; Magentic-One Orchestrator plan; OWL Planner | YES | every framework has this |
| `<broadcast>...</broadcast>` | A2A broadcast; Swarms swarm-broadcast; Cohere North multi-agent broadcast | YES | A2A v1.2 standardized |
| `<bid>...</bid>` + `<award/>` | Contract-net protocol; less common in frameworks but research-attested | partial | Swarms supports auctions; AgentScope Marketplace planned |
| `<vote>...</vote>` | Byzantine voting in research papers; AutoGen GroupChat majority | YES | implement as judge-loop |
| `<pheromone>` + `<read_pheromone/>` | Stigmergic coord — in research literature, not yet standard | NEW | V15 leads here |
| `<gossip>...</gossip>` | A2A gossip in v1.2 spec (cryptographically signed agent cards) | YES | aligns with A2A 1.2 |
| `<barrier/>` | LangGraph barrier; Agent Framework Workflow checkpoint | YES | identical |
| `<role_card>...</role_card>` | OpenAI Agents `Agent(instructions=...)`; CAMEL system_message; CrewAI Agent role; A2A "agent card" | YES | A2A's agent_card is the new lingua franca — **encode A2A agent_card directly inside V16 token** |
| `<topology/>` | Swarms `topology` directive; AgentScope distributed_topology | YES | |
| `<blackboard_write>` + `<blackboard_read/>` | Letta Archival Memory; AgentScope shared memory; MCP filesystem server as blackboard | YES | maps to MCP-filesystem in production |
| `<recurse>...</recurse>` | ReDel recursive delegation | YES | direct match |
| `<critique>...</critique>` | Reflexion store; Magentic-One re-plan; Letta self-edit | YES | universal in Reflexion-style frameworks |

### Recommended V16 token additions (NEW — to align with 2026 protocol convergence)

| New token | Reason | Compatibility |
|---|---|---|
| `<mcp_call>...</mcp_call>` | MCP is now Linux-Foundation standard for tool access — 200+ servers; explicit MCP framing helps tokenizer | MCP 2026 spec |
| `<a2a_envelope>...</a2a_envelope>` | A2A 1.2 cryptographically-signed agent message envelope | A2A 1.2 |
| `<ag_ui_event/>` | AG-UI streaming event (SSE-style) for user-facing agents | Bedrock AgentCore integrates this |
| `<acp_request>...</acp_request>` | IBM ACP (Agent Communication Protocol) — extends MCP for agent-to-agent | BeeAI |
| `<reflection>...</reflection>` | Letta-style memory reflection + Reflexion store; distinct from `<critique>` (which is peer review) | universal |
| `<tool_schema/>` | Pydantic AI / Mirascope / smolagents structured-output schema declaration | type-safety convergence |
| `<code_action>...</code_action>` | smolagents code-as-action paradigm (30% step reduction proven) | smolagents |
| `<session_id/>` | OpenAI Agents SDK sessions; Letta agent_id; Bedrock AgentCore session storage | universal in 2026 |
| `<guardrail>...</guardrail>` | OpenAI Agents SDK + NVIDIA OpenShell + AgentCore policy controls | universal |

→ +9 new tokens, total = **37 tokens for V16** (was 28 in V15). Init by mean-of-existing.

## Section 4 — Patterns to Adopt (cross-framework synthesis)

### 4A. Hierarchical Workforce (OWL/Magentic-One)
- **Single planner-coordinator-worker pattern wins** — OWL #1 OSS GAIA at 69.09%; Magentic-One competitive on GAIA + AssistantBench + WebArena
- All recent frameworks converged here: Microsoft (Magentic-One), CAMEL (OWL), Google ADK (multi-agent), AWS AgentCore (managed)
- → **V16 default topology** = hierarchical 3-tier (Navigator-plan / Coordinator / Workers)

### 4B. Code-as-Action (smolagents)
- 30% step + LLM-call reduction; superior on complex benchmarks
- Composability via Python control-flow primitives (loops, conditionals, function nesting)
- → **V16 must train CodeAgent style alongside JSON-tool-call**

### 4C. Memory-tier (Letta/MemGPT)
- Three tiers: Core (in-context) / Recall (searchable history) / Archival (cold)
- Filesystem-as-memory (Letta Filesystem 2026 release)
- → **V16: include Letta Filesystem-style memory traces** (already maps to V15 blackboard tokens)

### 4D. Trace-debugging (TRAIL)
- 575 of 1,987 OTel spans labeled with error type → train an "agent trace debugger" capability
- → **V16: add Sherlock-role specialty in agent-trace-debugging** (extends V9 Sherlock)

### 4E. Type-safe outputs (Pydantic AI / Mirascope / smolagents)
- Schema-validated structured output is the 2026 production standard
- Pydantic AI: model defines JSON schema → LLM cannot deviate
- → **V16: train on schema-conformant outputs as Phase D** (was tool-use Phase C)

### 4F. Protocol stack convergence (MCP + A2A + AG-UI + ACP)
- MCP — agent ↔ tools (Linux Foundation, 200+ servers)
- A2A — agent ↔ agent (Linux Foundation Agentic AI Foundation, v1.2 with crypto-signed cards, 150+ orgs in production)
- AG-UI — agent ↔ user (SSE-streaming, in MAF + Bedrock AgentCore)
- ACP — agent communication protocol (BeeAI; extends MCP for inter-agent)
- → **V16: bake protocol fluency into model** (special tokens above + protocol-aware traces)

### 4G. Data-generation as a first-class skill (CAMEL-AI)
- CAMEL: scaling-laws-of-agents through data generation
- Synthetic + Self-Instruct + CoT pipelines bundled with the framework
- → **V16 trainer should ship its own data-generation runtime** built on CAMEL's design

### 4H. Distributed + cross-framework (AgentScope + A2A)
- AgentScope-Runtime supports cross-framework orchestration via A2A (LangGraph + CrewAI + BeeAI all interop)
- Service discovery via Nacos in distributed deploy
- → **V16: include cross-framework dispatch traces in training mix**

## Section 5 — Anti-Patterns (avoid these in V16)

1. **Pure JSON tool-call without code-action option** — proven 30% worse than code-action on complex tasks. V16 must train both.
2. **Single shared message pool without role-cards** (early MetaGPT/AgentVerse) — leads to message flooding. Use role-card-filtered subscriptions.
3. **Decentralized Byzantine voting WITHOUT a planner** — research-attested but real-world OWL/Magentic-One show planner is needed.
4. **Trace-blind training** — V8 problem. Always train on real OTel-style spans, not just instruction pairs (V9 fixes this; V16 doubles down).
5. **Closed-source-only inspiration** — avoid; license-poison risk. All Tier-1 datasets in §2 are Apache-2.0 / MIT / public-domain.
6. **Synchronous blocking handoffs everywhere** — OpenAI Swarm pattern blocks parent. Real frameworks (Letta, AgentScope) prefer async + DelegateWait.
7. **Tag-flooding without grouping** — V13 had 8 tokens, V15 has 28 — each one earned its place. Don't add tokens without ablation evidence.
8. **Naive ReAct loops** — superseded by hierarchical Workforce. ReAct is a building block, not a framework default.
9. **Frozen frameworks** (OpenAI Swarm, Devika, AgentVerse) — DO NOT mine traces from abandoned codebases as training-quality.
10. **Per-token retraining** — adding tokens requires `resize_token_embeddings` + mean-init. Don't skip.

## Section 6 — Top 5 Frameworks to Mine for V16 Training Data

Ranked by quality × license-cleanliness × volume × format-compatibility with V15 trainer:

| Rank | Framework | Why | Estimated pairs to extract | Pipeline |
|---|---|---|---|---|
| **1** | **SWE-bench/SWE-smith ecosystem** (SWE-Gym, SWE-smith, SWE-rebench-OpenHands-trajectories, SWE-agent-trajectories, DeepSWE) | Largest license-clean coding-agent traj corpus on Earth (~165K trajectories combined); proven SOTA fine-tune target | 50-80K pairs after dedup | extend `merge_external` in `bin/v2/build-knowledge-corpus.sh` |
| **2** | **OpenHands** | Real GitHub issues; permissive MIT; integrates with AgentCore Runtime, Cline, Claude Code; Index Jan 2026 broadens to greenfield + frontend + testing | 15-30K pairs | run OpenHands on Excise + DevOps repos; collect trajectories |
| **3** | **AgentNet (OpenCUA)** | The ONLY large-scale computer-use trajectory dataset (22.6K) under Apache-2.0; needed for V16 GUI-agent capability | 10-15K distilled pairs | extract action sequences + reflective CoT |
| **4** | **CAMEL-AI** (incl. OWL Workforce + OASIS) | Best multi-agent role-playing data; OWL is #1 OSS on GAIA; CAMEL also ships data-generation tools so we can synthesize unlimited pairs | 20-30K (10K real + 10-20K synthesized) | wire `camel.synthetic` into trainer |
| **5** | **τ-bench + τ²-bench historical trajectories** | Tool-Agent-User multi-turn; policy-aware; lets V16 learn customer-service-style discipline | 3-5K dialogue traces | direct ingest from `historical_trajectories/` |
| **+ Local mining bonus** | **Your own Claude Code + Cline + Codex + Gemini transcripts** | Personalized to YOUR workflow; full context fit | 5-15K already-existing locally | `~/.claude/projects/**/memory/` + `~/.claude/tasks/` extraction script |

## Section 7 — Frameworks with Public APIs to GENERATE Training Data

| Framework | API | What we can run | Output license |
|---|---|---|---|
| **smolagents** | `CodeAgent.run(task)` | Run on Excise + DevOps + Surrogate-1 codebase tasks → code-action traces | Apache-2.0 |
| **OpenHands** | `runtime.run_session()` | Run on real Excise/RD/DevOps GitHub issues | MIT |
| **CAMEL-AI** | `camel.synthetic.{cot, self_instruct}` | Generate role-playing dialogues for our 6 V9 roles | Apache-2.0 |
| **AgentScope** | OTel export of any session | Run pipelines on cdk-infrastructure/ → distributed-agent traces | Apache-2.0 |
| **Letta** | Letta Evals + agent run logs | Stateful-agent traces with memory-tier annotations | Apache-2.0 |
| **Pydantic AI** | Logfire trace export | Type-safe agent runs on local tasks | MIT |
| **OpenAI Agents SDK** | Built-in tracing dashboard export | Routine + handoff traces (note: production = paid OAI; cheap = local LLM) | Apache-2.0 (SDK) |
| **Claude Agent SDK** | `list_subagents()` + `get_subagent_messages()` | YOUR existing sessions = mineable now | yours |
| **Magentic-One (in MAF)** | AutoGenBench + Studio | Run on GAIA-style tasks → orchestrator-decomposed plans | MIT |
| **OWL** | `Workforce.run(task)` | Multi-agent task automation traces | Apache-2.0 |

## Section 8 — Surrogate-1 V16 Concrete Action Items

### A. Trainer changes (`surrogate-1-train-v16-*.py`)
1. Bump `MULTI_AGENT_TOKENS` from 28 → 37 (add the 9 new from §3)
2. Add `merge_external()` calls for SWE-smith-trajectories (5K), SWE-rebench-OpenHands (15K), AgentNet-distilled (10K), TRAIL (300), τ-bench (3K), DeepSWE (5K) — total +38.3K trajectories
3. Wire CAMEL synthetic generators for role-playing pair augmentation
4. Add code-action examples (smolagents-style) alongside JSON-tool-call (~10K pairs)
5. Add memory-tier traces (Letta-style) — 5K pairs
6. Add MCP/A2A protocol-aware traces — synthesize 3K pairs from spec + examples
7. Embed agent_card-style role descriptions inside `<role_card>` token content (A2A 1.2-aligned)

### B. Bench changes (`bin/v2/bench-v1-vs-v15.sh` → rename to `bench-v1-vs-v16.sh`)
1. Add SWE-bench Pro (contamination-free 2,000 problems)
2. Add WebArena (812 tasks, 5 sites)
3. Add τ-bench + τ²-bench (Sierra)
4. Add OpenHands Index (issue resolution + greenfield + frontend + testing)
5. Add OSWorld + AgentNetBench (computer-use)
6. Add TRAIL (trace-debugging — train AND eval)
7. Use Princeton HAL harness as the runner (single harness for many)

### C. Knowledge updates
1. This file → cited in `~/.claude/memory/knowledge_index.md` as `[v16-agent-frameworks-inventory]`
2. Update `[[v13-multi-agent-baked-in]]` cross-link to point to V16 spec
3. Add A2A 1.2 + AG-UI + ACP to `[[knowledge/]]` topical files

### D. Skill mining script
- `~/.claude/bin/mine-claude-cline-traces.sh` (NEW): walk `~/.claude/projects/**/memory/`, `~/.claude/tasks/`, extract user-turn + assistant-turn pairs, dedupe MinHash, output JSONL — yields 5-15K personal training pairs

## Section 9 — License Compatibility Note

For axentx commercial deployment of Surrogate-1:
- **Apache-2.0 / MIT / BSD** — all fine; just preserve attribution in NOTICE
- **CC-BY-4.0** — fine for derivative training (knowledge files only; not weights)
- **Llama-derivative checkpoints** (agentlm-7b/13b/70b) — must comply with Llama 2/3 license; use as inspiration only, do NOT copy weights
- **research-permissive** (TRAIL, ReDel) — check per-paper; usually fine for training/research, ask before commercial deploy
- **proprietary harnesses** (Claude Code, OpenAI Agents SDK production runs) — your OWN traces are yours; OpenAI/Anthropic outputs follow their respective ToS (not training competitor models on outputs without permission)

## Section 10 — Per-framework deep notes (training-relevant detail)

### 10.1 Microsoft Agent Framework 1.0 (Apr 2026 GA)
- Unifies AutoGen + Semantic Kernel under `Microsoft.Extensions.AI`. .NET + Python first-class.
- First-party connectors: Microsoft Foundry, Azure OpenAI, OpenAI, Anthropic Claude, Amazon Bedrock, Google Gemini, Ollama.
- Middleware hooks at every execution stage (content-safety, logging, compliance, custom).
- Graph-based deterministic Workflows + native AG-UI integration (ASP.NET Core middleware `Microsoft.Agents.AI.Hosting.AGUI`).
- → Mineable: not directly (proprietary cloud); but the SDK's open trace format is standardized and compatible with our V15 tags.

### 10.2 AgentScope 1.x (Alibaba, Apache-2.0)
- 2025-12 added A2A protocol + TTS; 2026-01 added DB + memory compression; 2026-03-30 launched CoPaw v1.0.0 (CoPaw-Flash-9B agentic fine-tune of Qwen3.5-9B).
- AgentScope-Studio: dual-view message streams + ReAct state tracing + OpenTelemetry.
- AgentScope-Runtime v1.0 = native multi-agent collaboration with cross-framework orchestration via A2A.
- Java SDK exists (`agentscope-java`).
- → V16 mining: run our own pipelines through AgentScope-Studio → harvest OTel traces → convert to V15-token format. Estimate ~10K pairs feasible.

### 10.3 CAMEL-AI + OWL + OASIS (Apache-2.0)
- CAMEL: "scaling laws of agents" thesis = data + agents co-scale.
- OWL = optimized workforce learning; #1 OSS on GAIA at 69.09%; NeurIPS 2025; Workforce = Planner + Coordinator + Workers (3-tier).
- OASIS = 1M-agent social simulator (Twitter/Reddit-like); rule-based + LLM agents.
- Built-in synthesis: CoT, Self-Instruct, multi-hop QA, complex reasoning paths.
- → V16 mining: heavy. Wire `camel.synthetic.cot.generate(role, task)` into trainer for unbounded role-pair generation.

### 10.4 OpenHands (was OpenDevin, MIT)
- Production-grade agent SDK; 53%+ on SWE-bench Verified with Claude 4.5.
- 2026-01 launched OpenHands Index = broader eval (issue resolution + greenfield + frontend + testing).
- Sandboxed runtime; multi-agent coordination; benchmark harness `OpenHands/benchmarks`.
- → V16 mining: run on Excise + RD + DevOps + Surrogate-1 repos with synthetic issues; expect 5-10K real trajectories.

### 10.5 Letta (MemGPT 2, Apache-2.0)
- Three memory tiers: Core (in-context, like RAM) / Recall (searchable history, like disk cache) / Archival (cold storage, queried via tools).
- Letta Filesystem (2026 release) = files-as-memory.
- Letta Code = memory-first coding agent (separate from main Letta).
- Letta Evals = open-source eval framework for stateful agents.
- → V16 mining: Letta Eval logs + Letta Code traces. Memory-tier annotations directly map to V15 `<blackboard_*>` and new `<reflection>` tokens.

### 10.6 OpenAI Agents SDK (Apache-2.0; 2025-03 → 2026 active)
- Production successor to Swarm. Routines + handoffs + guardrails + tracing + sessions.
- Tracing: comprehensive event log (LLM gens, tool calls, handoffs, guardrails, custom). Traces dashboard.
- Guardrails run **in parallel** with execution; fail-fast pattern (cheap-model guardrail blocks expensive-model exec).
- Sessions = persistent memory layer.
- → V16 mining: SDK is FOSS but production traces require OAI account; use SDK abstractions in our trainer to format outputs identically.

### 10.7 Pydantic AI 1.85 (MIT, 2026-04-22)
- Type-safe = Pydantic schema → JSON-schema → LLM cannot deviate. Schema is single source of truth.
- Streamed structured outputs (continuous validation).
- Durable execution (preserves progress across API failures + restarts).
- Built-in Logfire observability.
- → V16 mining: schema-conformance training data; trace export from Logfire = JSONL consumable by trainer.

### 10.8 smolagents (Apache-2.0, HuggingFace)
- <1,000 LOC core. Code-action paradigm (CodeAgent writes Python; ToolCallingAgent does JSON).
- 30% step + LLM-call reduction proven on complex benches.
- Multi-modal (text/vision/video/audio).
- Sandboxed exec via Blaxel/E2B/Modal/Docker/Pyodide+Deno.
- Hub-shareable tools + agents.
- → V16 mining: run smolagents CodeAgent on Excise + DevOps tasks; collect Python action traces; ~5-10K pairs.

### 10.9 LangGraph 1.0 (MIT, 2026 GA)
- Stateful graph orchestration; durable state; built-in persistence (save/resume); HITL pause-points.
- LangSmith automatic tracing (every node, edge, state mutation).
- Custom span metadata (critic_score, iteration_count, retrieval_round, token_budget_used).
- Trusted by Klarna, Replit, Elastic.
- → V16 mining: LangSmith trace export = JSON; map nodes to `<spawn>`, edges to `<await/>`, state to `<blackboard_*>`.

### 10.10 Bedrock AgentCore (AWS, commercial; 2026 Q1-Q2 GA cluster)
- 2026-03-10: stateful MCP server features (elicitation, sampling, progress notifications).
- 2026-03-13: AG-UI protocol support.
- 2026-03-17: `InvokeAgentRuntimeCommand` for direct shell exec.
- 2026-03-25: managed session storage (persistent agent filesystem state, preview).
- 2026-04: Node.js direct deploy + bidirectional streaming + SigV4/OAuth2.
- 2026-04-22: managed harness (preview) + AgentCore CLI + AgentCore skills for coding assistants.
- → V16 alignment: keep MCP + AG-UI tokens; Bedrock AgentCore is the deployment target for Surrogate-1 SaaS.

### 10.11 NVIDIA AI-Q + Agent Toolkit (Apache-2.0 OSS pieces; GTC 2026-03-16)
- AI-Q Blueprint = LangChain-based agentic search; tops DeepResearch Bench.
- Hybrid orchestration (frontier model orchestrates; Nemotron does heavy lifting; cuts query cost ~50%).
- Components: Nemotron (open agentic-reasoning models), AI-Q (perceive/reason/act on enterprise knowledge), OpenShell (policy guardrails), cuOpt (optimization skills).
- 17 enterprise adopters at GTC: Adobe, Salesforce, SAP + 14 others.
- → V16 alignment: Nemotron-RL-Super already in V14 trainer mix; AI-Q's hybrid pattern → train the model to know when to delegate to a frontier orchestrator.

### 10.12 Cohere North (commercial)
- Powered by Command-variant trained for enterprise reasoning.
- Connects to Gmail, Slack, Salesforce, Outlook, Linear; integrates any MCP server.
- Private deploy (Model Vault); secure enterprise mode.
- → V16: secondary; not a training-data source (closed); inspiration for enterprise-deploy pattern.

### 10.13 BeeAI Framework (IBM, Linux Fdn, Apache-2.0)
- TypeScript + Python, multi-agent.
- ACP (Agent Communication Protocol) extends MCP for inter-agent messaging.
- Bridges LangGraph + CrewAI + BeeAI in one runtime.
- Successor to original Bee single-agent framework.
- → V16 mining: ACP-formatted messages = directly mappable to new `<acp_request>` token.

### 10.14 Anthropic Claude Agent SDK (mixed: SDK permissive, Claude harness proprietary)
- Renamed from Claude Code SDK — broader vision beyond coding.
- Subagents (parallel + isolated context); skills (YAML frontmatter `SKILL.md`); session-persistence.
- 2026-04: subagent transcript helpers (`list_subagents()`, `get_subagent_messages()`).
- 2026-04: parallel MCP server reconnection; Claude Managed Agents launched 2026-04-08.
- → V16 mining (BIG): YOUR existing Claude Code transcripts in `~/.claude/projects/**/memory/` are mineable now. Skills directory `~/.claude/skills/` + `~/Documents/Obsidian Vault/AI-Hub/skills/` = ~68 community + 9 anthropic skills with example invocations.

### 10.15 Cline 3.58 (Apache-2.0, 2026-02 native subagents)
- Read-only subagents w/ separate context windows (cannot write/destroy/MCP).
- Per-subagent token + cost tracking.
- IDE coverage: VSCode, JetBrains, Neovim, Emacs.
- Cline CLI (return to primitives).
- → V16 mining: Cline session JSON in workspace `.cline/` dirs.

### 10.16 Vertex AI Agent Builder / Gemini Enterprise Agent Platform / ADK (Apache-2.0 ADK)
- 2026 Cloud Next: Vertex AI rebranded to Gemini Enterprise Agent Platform; Agentspace consolidated.
- ADK = code-first multi-language (Python, Go, Java, TypeScript) at v1.0.
- Native A2A; native MCP.
- Bundles ADK + Agent Studio (low-code) + 200+ models (Gemini + Claude + others) + Agent Engine (managed runtime) + persistent memory + governance.
- Migration deadline: deprecated SDK modules retire 2026-06-24.
- → V16: ADK code samples for multi-language are training-relevant; ADK trace export = compatible.

## Section 11 — Open Questions / Research Gaps for V17

1. **Stigmergic coordination data**: V15 added `<pheromone>` tokens but real-world pheromone-style multi-agent traces are scarce. Need synthetic generation from research papers (V14 listed). Action: generate 1-3K synthetic stigmergic traces in V16 trainer.
2. **Byzantine voting in production frameworks**: only research-attested as of 2026-05. Watch for AgentScope or Swarms adding native `<vote>` semantics.
3. **A2A v1.3+ features**: payment-rails / cross-org commerce. May add `<settle>` / `<invoice/>` tokens in V17 if 2026-Q3 release ships them.
4. **Agent Data Protocol (ADP)** [arxiv 2510.24702] — proposes unifying schema across 1.27M trajectories from 13 datasets. Track for V17 ingestion.
5. **Open trace format standardization**: OTel + AG-UI + A2A still diverge slightly. Watch for unified `agent-otel` schema.

## See Also

- [[v13-multi-agent-baked-in]] — original 8-token V13 spec
- [[v14-swarm-agents-at-scale]] — V15's 28-token expansion
- [[v14-arxiv-github-sweep-may2026]] — research backing for tokens
- [[surrogate-1-v9-spec]] — 6-role + 12-dataset baseline
- [[surrogate-1-v10-rev2-spec]] — knowledge-corpora + role-data
- [[devsecops-sre-agentic]] — agentic-bench corpus reference
- [[../../patterns/MOC|Knowledge Graph Hub]]

## Sources (research 2026-05-01)

- [Microsoft Agent Framework 1.0 GA (Apr 2026)](https://devblogs.microsoft.com/agent-framework/microsoft-agent-framework-version-1-0/)
- [AutoGen v0.4 reimagined](https://devblogs.microsoft.com/autogen/autogen-reimagined-launching-autogen-0-4/)
- [AgentScope 1.0 paper](https://arxiv.org/html/2508.16279v1) + [agentscope-ai/agentscope](https://github.com/agentscope-ai/agentscope)
- [CAMEL-AI](https://github.com/camel-ai/camel) + [OWL paper](https://arxiv.org/abs/2505.23885) + [OASIS](https://github.com/camel-ai/oasis)
- [MetaGPT](https://github.com/FoundationAgents/MetaGPT)
- [CrewAI tracing docs](https://docs.crewai.com/en/observability/tracing)
- [LangGraph 1.0 announcement](https://blog.langchain.com/langchain-langgraph-1dot0/)
- [Letta (MemGPT)](https://github.com/letta-ai/letta)
- [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) + [OpenAI Swarm (frozen)](https://github.com/openai/swarm)
- [Pydantic AI](https://ai.pydantic.dev/)
- [smolagents](https://github.com/huggingface/smolagents)
- [Swarms (kyegomez)](https://github.com/kyegomez/swarms)
- [AgentVerse](https://github.com/OpenBMB/AgentVerse)
- [OpenAgents (XLang)](https://github.com/xlang-ai/OpenAgents) + [OpenCUA](https://github.com/xlang-ai/OpenCUA)
- [OpenHands](https://github.com/OpenHands/OpenHands) + [OpenHands Index Jan 2026](https://openhands.dev/blog/openhands-index)
- [Cline native subagents 2026-02](https://docs.cline.bot/features/subagents)
- [Aider](https://github.com/Aider-AI/aider)
- [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) + [Claude Managed Agents 2026-04-08](https://anthemcreation.com/en/artificial-intelligence/claude-managed-agents-anthropic-ai/)
- [BeeAI Framework](https://github.com/i-am-bee/beeai-framework)
- [Haystack 2 + Hayhooks](https://github.com/deepset-ai/haystack)
- [Mirascope](https://github.com/Mirascope/mirascope)
- [Vertex AI Agent Builder / Gemini Enterprise / ADK](https://cloud.google.com/products/agent-builder)
- [Bedrock AgentCore (Mar–Apr 2026 GA cluster)](https://aws.amazon.com/bedrock/agentcore/)
- [NVIDIA AI-Q + Agent Toolkit (GTC 2026)](https://nvidianews.nvidia.com/news/ai-agents)
- [Magentic-One](https://www.microsoft.com/en-us/research/articles/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/)
- [TypeAgent](https://github.com/microsoft/TypeAgent)
- [Model Context Protocol roadmap 2026](https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/) + [MCP servers list](https://github.com/modelcontextprotocol/servers)
- [A2A 1.2 (Linux Fdn governed, 150+ orgs prod)](https://a2a-protocol.org/latest/) + [A2A protocol announcement](https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/)
- [AG-UI protocol](https://docs.ag-ui.com/introduction)
- [TRAIL dataset](https://huggingface.co/datasets/PatronusAI/TRAIL)
- [SWE-smith / SWE-Gym / SWE-rebench / SWE-agent-trajectories](https://huggingface.co/SWE-bench)
- [AgentNet (22.6K computer-use)](https://huggingface.co/datasets/xlangai/AgentNet)
- [AgentTuning + AgentInstruct](https://thudm.github.io/AgentTuning/)
- [τ-bench / τ²-bench (Sierra)](https://github.com/sierra-research/tau-bench)
- [ReDel recursive multi-agent](https://github.com/zhudotexe/redel)
- [Princeton HAL harness](https://github.com/princeton-pli/hal-harness)
- [Agent Data Protocol (unifying datasets) 2025](https://arxiv.org/html/2510.24702)

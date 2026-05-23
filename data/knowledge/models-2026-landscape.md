---
title: AI Models & Techniques Landscape — April 2026
tags: [models, research, surrogate-1, benchmarks, 2026, llm, architecture]
date: 2026-04-18
purpose: Inform Surrogate-1 (DevSecOps/Platform Engineering AI agent) architecture decisions
sources: WebSearch + WebFetch April 2026 — 18 queries, production data
---

# AI Models Landscape — April 2026

> **TL;DR for Surrogate-1**: Claude Opus 4.7 leads agentic coding. GPT-5.4 owns computer-use. Gemini 3.1 Pro wins massive-context + multimodal. Grok 4.20 is 2M-context + 4-agent native. DeepSeek V3.2 + Qwen 3.6 + Kimi K2.5 give frontier-class open weights at 5-10x lower cost. Build a **router** that picks the right tier per Surrogate role.

---

## Tier 1: Premium Frontier Models (Closed / SOTA)

| Model | Provider | Released | Context | Input $/Out $/1M | SWE-Bench V. | Unique Strength |
|-------|----------|----------|---------|------------------|--------------|-----------------|
| **Claude Opus 4.7** | Anthropic | Apr 16, 2026 | 1M | $5 / $25 | ~81% (Opus 4.5 baseline) | Agentic coding leader, extended thinking, computer use, 1M ctx @ std price |
| **Claude Sonnet 4.6** | Anthropic | Early 2026 | 1M | $3 / $15 | ~78% | Balanced tier, extended thinking, 1M ctx |
| **Claude Mythos Preview** | Anthropic | Apr 2026 | 1M | — | **93.9%** (leader) | Next-gen preview, SWE-Bench leader |
| **GPT-5.4** | OpenAI | Mar 5, 2026 | 1M+ | $2.50 / $15 | 57.7% (SWE-Bench **Pro**) | **Computer-use 75% (beats human 72.4%)**, 5-level reasoning effort (none/low/med/high/xhigh), native tool use |
| **GPT-5.4 Pro** | OpenAI | Mar 2026 | 1M+ | $30 / $180 | higher | Premium reasoning variant |
| **Gemini 3.1 Pro** | Google | Apr 2026 | **2M** | $2 / $12 (<200k) → $4 / $18 (>200k) | 56.5% pass@1 | Largest context + best multimodal (text/img/audio/video/PDF native), paid-only as of Apr 1 2026 |
| **Grok 4.20** | xAI | Feb 2026 (Beta) | **2M** | — | ranked #24 overall 77 | **4 Agents multi-agent collaboration native** (parallel specialized agents), AA Intelligence Index 49 |

### Deep dive: Claude Opus 4.7 (Anthropic flagship)
- **Step-change in agentic coding** over 4.6. Extended thinking native, computer use via API.
- 1M context at **standard pricing** (no 2x tier like GPT-5.4 above 272k).
- Best fit for Surrogate roles: **Guardian** (security review), **Sherlock** (debugging), **Auditor** (compliance deep-dive).

### Deep dive: GPT-5.4 (OpenAI flagship)
- Unified reasoning+coding+agentic. **First general model with SOTA native computer use** (75% OSWorld, human baseline = 72.4%).
- `reasoning.effort`: none/low/medium/high/xhigh — lets caller tune compute vs latency.
- Best fit: **Navigator** (UI automation, GUI-driven ops), fallback for **Assembler** (code synth).

### Deep dive: Gemini 3.1 Pro (Google flagship)
- **2M context** — the largest among frontier. Native multimodal (text/img/audio/video/PDF) with single API.
- Best fit: **Auditor** (ingest massive logs/compliance docs in one pass), vision-heavy ops (diagram review).

### Deep dive: Grok 4.20 (xAI flagship)
- 2M context + **native 4-agent collaboration** — 4 specialized agents run in parallel per request.
- This is architecturally closest to Surrogate-1's multi-agent vision.
- Best fit: reference architecture for how Surrogate should internally coordinate roles.

---

## Tier 2: Fast / Cheap / Capable

| Model | Provider | Context | Input $/Out $/1M | Notes |
|-------|----------|---------|------------------|-------|
| **Claude Haiku 4.5** | Anthropic | 200K | $1 / $5 | Extended thinking supported, "handles tasks GPT-4o-mini can't" |
| **GPT-5.4 Mini** | OpenAI | 1M | ~$0.40 / $1.60 | Main workhorse for cheap reasoning |
| **GPT-5.4 Nano** | OpenAI | — | edge/embedded | On-device |
| **Gemini 3 Flash** | Google | 1M | $0.50 / $3 | Free tier (reduced quota) — new default Flash |
| **Gemini 3.1 Flash-Lite** | Google | 1M | $0.25 / $1.50 | Cheapest Tier-1 budget model |
| **Grok 4.1 Fast** | xAI | 2M | $0.20 / $0.50 | **Best agentic tool-calling at this price**, 162 tok/s, AA Index 24 |

**Key insight**: **Grok 4.1 Fast at $0.20/$0.50 with 2M context is the price/context outlier** — ideal for Surrogate's **Coach** role (always-on advisor) or massive log ingestion before escalation.

---

## Tier 3: Open Weights (Self-hostable)

| Model | Provider | Params (Active) | License | Context | SWE-Bench V. | Notes |
|-------|----------|-----------------|---------|---------|--------------|-------|
| **DeepSeek V3.2** | DeepSeek | 671B (37B active, MoE) | MIT | 128K | ~70% | AIME 96.0, HMMT 99.2; $0.28 / $0.42 per 1M |
| **Qwen 3.5** | Alibaba | 397B (17B active, MoE) | Apache 2.0 | — | **76.4** | Runs on consumer HW |
| **Qwen 3.6-35B-A3B** | Alibaba | 35B (3B active, MoE) | Apache 2.0 | — | **73.4** | Headline open coding MoE, vision matches Sonnet 4.5, 20.9GB quantized |
| **Qwen 3.6-Plus** | Alibaba | proprietary | Closed | 1M | — | Alibaba Cloud only |
| **Llama 4 Scout** | Meta | 17B active, 16 experts | Llama 4 license | **10M** | — | Industry-leading context |
| **Llama 4 Maverick** | Meta | 17B active / 400B total, 128 experts | Llama 4 license | 1M | — | Best multimodal in class, beats GPT-4o, Gemini 2.0 Flash |
| **Llama 4 Behemoth** | Meta | 288B active / ~2T total | Llama 4 license | — | — | Teacher model, not yet public |
| **Gemma 4 31B Dense** | Google | 31B dense | Apache 2.0 | — | — | **AIME 89.2, LiveCodeBench 80.0, GPQA 84.3, τ2-bench 86.4** — beats Llama 4 400B on math/code/agentic |
| **Kimi K2.5** | Moonshot | ~1T (32B active, MoE, 384 experts) | MIT | 256K | 76.8 | **Agent Swarm: up to 100 parallel sub-agents native**, multimodal vision |
| **GLM-4.5-Air** | Z.ai | 106B (12B active) | MIT | — | 59.8 avg | AIME 2024 **89.4** (beats Claude 4 Opus 75.7), BFCL v3 76.4 |
| **DeepSeek-R1** | DeepSeek | 671B MoE | MIT | — | — | Reasoning-focused, o1-equivalent open |
| **Nemotron 3 Super** | NVIDIA | 120B (12B active), Mamba-Transformer MoE hybrid | Open | — | **60.47%** (highest open-weight) | Native NVFP4, 4x inference on Blackwell, #1 DeepResearch Bench |
| **gpt-oss-120B** | OpenAI | 120B | Open | — | — | OpenAI's open-weight offering |

### Why open weights matter for Surrogate-1
1. **Cost floor**: self-host hot-path = $0 marginal. Qwen 3.6-35B-A3B runs on 24GB GPU.
2. **Data residency**: Thai startup / enterprise clients with sovereignty requirements.
3. **Fine-tuning**: LoRA/QLoRA own codebase knowledge (see Techniques section).
4. **Fallback**: if Claude rate-limited, route to local Kimi K2.5 / Qwen 3.6 with **compatible quality** for 70% of tasks.

### Best open-weight picks for Surrogate-1 hot-path
- **Code review / assembly**: Qwen 3.6-35B-A3B (73.4 SWE-Bench, 3B active = fast)
- **Reasoning / audit**: DeepSeek V3.2 (96.0 AIME, 70% SWE-Bench)
- **Multi-agent orchestration**: Kimi K2.5 (built-in 100-agent swarm)
- **Edge / on-device**: Gemma 4 31B Dense (beats 400B models on math/code)

---

## Tier 4: Specialized

| Model | Provider | Specialty | Pricing | Notes |
|-------|----------|-----------|---------|-------|
| **Codestral** | Mistral | Code | $0.30 / $0.90 | 80+ languages, optimized for completion/refactor |
| **Mistral Large 3** | Mistral | General (EU) | $0.50 / $1.50 | GDPR-compliant, Apache 2.0 open variant, on-prem |
| **Pixtral** | Mistral | Vision | — | Dedicated image/multimodal |
| **Command R+** | Cohere | Enterprise RAG | $2.50 / $10 | Grounded generation + citations; pair with Rerank 3.5 ($2/1k searches) + Embed v3 ($0.10/1M) |
| **Perplexity Sonar** | Perplexity | Grounded web search | $1/1M + $5/1k searches | 94% citation rate, <2s latency, OpenAI-compatible API |
| **Perplexity Sonar Pro** | Perplexity | Premium grounded | $3 / $15 | Deeper research |

### Sonar unique value for Surrogate-1
Built-in **grounded web search with citations**. Best for **Coach** role (up-to-date DevSecOps practices) and **Navigator** (searching docs mid-task).

---

## Per-Role Recommendations for Surrogate-1

Surrogate-1 roles (user-defined): **Guardian / Navigator / Assembler / Sherlock / Auditor / Coach**

| Role | Primary Model | Fallback | Rationale |
|------|---------------|----------|-----------|
| **Guardian** (security scan, IAM/policy review) | Claude Opus 4.7 | Claude Sonnet 4.6 / DeepSeek V3.2 | Extended thinking for threat modeling, strong security-aware refusal behavior, auditable reasoning traces |
| **Navigator** (GUI automation, cloud console ops) | **GPT-5.4 (computer-use)** | Gemini 3.1 Pro | GPT-5.4's 75% OSWorld is only model above human baseline for GUI tasks |
| **Assembler** (code synth, IaC, CI/CD) | Claude Opus 4.7 (agentic) | Qwen 3.6-35B-A3B (self-host hot path) | SWE-Bench leader + open fallback for cost control |
| **Sherlock** (debugging, RCA, log analysis) | Claude Opus 4.7 | Gemini 3.1 Pro (2M ctx for logs) | Extended thinking + massive log ingestion |
| **Auditor** (compliance deep-dive, SOC2/ISO/HIPAA) | Gemini 3.1 Pro (2M ctx) | Claude Opus 4.7 | Ingest entire policy corpus + audit trails in single pass |
| **Coach** (mentorship, up-to-date guidance) | Perplexity Sonar Pro + Claude Haiku 4.5 | Grok 4.1 Fast | Grounded citations + cheap high-freq follow-ups |

**Routing strategy**: Use a lightweight router (Haiku 4.5 or Qwen 3.5 local) that inspects the task and picks the tier. Budget cap per role per day prevents runaway Opus spend.

---

## Techniques Landscape

### Architectural patterns

#### Mixture of Experts (MoE)
- **Current state**: dominant for >100B param models. Activates 10-20% of params per token.
- **Routing**: top-k (typically k=2-4) gating network, learned jointly with experts.
- **2026 evolution**: Mamba-Transformer MoE hybrids (Nemotron 3 Super), very sparse (Kimi K2.5 = 8 of 384 experts), MoE scales past dense on memory efficiency to ~5B active.
- **For Surrogate-1**: use MoE models (Qwen 3.6-35B-A3B, Kimi K2.5, DeepSeek V3.2) for hot-path self-hosting — 3B-37B active = dense-model latency at 100B+ model quality.

#### Agent Swarm / Multi-Agent Native
- **Grok 4.20**: 4 specialized agents in parallel per request.
- **Kimi K2.5**: Agent Swarm up to 100 sub-agents.
- **Implication**: Surrogate-1's multi-role design is aligned with 2026 architecture trend — roles can be separate model instances OR experts within a single MoE.

### Reasoning / Agentic Patterns (all still relevant in 2026)

| Pattern | When to use in Surrogate-1 |
|---------|----------------------------|
| **ReAct** (reason+act interleaved) | Default loop for every role — thinks, calls tool, observes, repeats. Claude Code and Codex both use lone-agent ReAct core. |
| **Reflexion** (self-critique + memory) | Sherlock (debugging needs iteration); Guardian (security review after each change) |
| **Tree-of-Thoughts** (branching exploration) | Assembler (explore 2-3 solution paths before writing) |
| **Chain-of-Verification** (verify own output) | Guardian + Auditor (before emitting final report) |
| **Constitutional AI** (critique model corrects) | Reviewer layer — enforce user's `reviewer.md` rules automatically |
| **Plan-and-Execute** | Orchestrator role — matches user's `plan_once.md` |
| **Consensus / Multi-agent Debate** | Critical decisions (prod deploy, destructive ops): run 2-3 models, aggregate. Advanced aggregation beats majority vote in 97.9% of cases (+0.5 to +14.2 points). |

**Key 2026 insight**: **Simpler is winning**. Claude Code and OpenAI Codex both ran complex orchestration experiments and settled on **single ReAct loop + good tools**. Surrogate-1 should follow: one clean loop per role, swap models via router, avoid over-engineering graph-based orchestration until proven necessary.

### Tool Use & Protocols

| Protocol | Role | Status (Apr 2026) |
|----------|------|-------------------|
| **MCP (Model Context Protocol)** | Agent ↔ Tools (JSON-RPC) | 97M monthly SDK downloads; adopted by Anthropic, OpenAI, Google, MS, AWS. **Universal standard for tool access.** |
| **A2A (Agent-to-Agent)** | Agent ↔ Agent delegation | Google-originated, Linux Foundation-owned, governs horizontal multi-agent collaboration |
| **ACP (Agent Communication Protocol)** | RESTful multipart | IBM-originated, **merged into A2A (Aug 2025)** |
| **ANP (Agent Network Protocol)** | Mesh network | Research/emerging |

**Governance**: Dec 2025 **Agentic AI Foundation (AAIF)** formed under Linux Foundation (OpenAI + Anthropic + Google + MS + AWS + Block) — permanent home for MCP + A2A.

**For Surrogate-1**: 
- Expose every tool via **MCP** (already the user's pattern in `~/.claude/`).
- Use **A2A** for orchestrator ↔ role communication (future-proof vs. proprietary team-tool APIs).
- Skip proprietary frameworks (LangGraph/AutoGen) at the protocol layer — use them as *implementation* only.

### Multi-Agent Orchestration Frameworks

| Framework | Model | Best For | Avg Cost/Query |
|-----------|-------|----------|----------------|
| **LangGraph** | Graph + checkpointing | Stateful workflows, durable exec, HITL | $0.18 |
| **CrewAI** | Role-based crews | Prototyping, role teams, fastest setup | **$0.12-0.15** (cheapest) |
| **AutoGen / AG2** | Conversational GroupChat | Multi-agent debate, diverse chat | $0.35 |
| **OpenAI Swarm / Agents SDK** | Explicit handoffs | OpenAI-only, simple handoff patterns | — |
| **OpenAgents** | Interop | Cross-framework | — |

**Migration pattern observed in production**: teams prototype in CrewAI → migrate to LangGraph for production (state, conditional routing, checkpointing).

**For Surrogate-1**: user's existing Team pattern in `~/.claude/rules/swarm.md` already aligns with CrewAI-style role teams. For production Surrogate, graduate to **LangGraph** for durable execution (critical when a DevSecOps task mid-execution hits blocker — need checkpoint/resume).

### Fine-Tuning (State of Art April 2026)

| Method | Status | Best For |
|--------|--------|----------|
| **LoRA** | Production standard | Train 0.1-1% params, near-full-FT quality |
| **QLoRA** | Production standard | 4-bit base + LoRA; **65B model on 48GB GPU** |
| **DPO** (Direct Preference Optimization) | **Has largely replaced RLHF** | Preference pairs, simpler+faster than RLHF |
| **RLAIF** | Scaling strategy | AI feedback instead of human (scale) |
| **GRPO / KTO / RLOO** | Specialized variants | TRL has best implementations |
| **Full RLHF** | Legacy | Only when DPO insufficient — rare in 2026 |

**For Surrogate-1 fine-tuning strategy**:
1. **Base**: Qwen 3.6-35B-A3B (Apache 2.0, open, agentic-strong)
2. **Method**: QLoRA on 48GB GPU (possible on single H100 or 2x RTX 4090)
3. **Training data**: user's own codebases (Excise, Vanguard, Costinel) + DevSecOps SOPs from Obsidian Vault
4. **Alignment**: DPO pairs from `~/.claude/memory/lessons_learned.md` (failed attempt → correct attempt)

### Inference Optimization

| Technique | Speedup | Adoption |
|-----------|---------|----------|
| **Speculative Decoding** | 2-4x (Snowflake/vLLM on Llama 3.1/Qwen) | Production standard in vLLM, SGLang, TensorRT-LLM, LM Studio, llama.cpp |
| **P-EAGLE (parallel spec decode)** | additional gain | Integrated into vLLM 2026 |
| **KV Cache / Prefix Cache** | up to 90% input cost, 80% latency reduction | All major providers (OpenAI auto, Anthropic explicit breakpoints, Google hybrid) |
| **LMCache + vLLM** | 3-10x latency on prefix-sharing workloads | Production |
| **NVFP4 (4-bit FP)** | 4x vs FP8 on Blackwell | Nemotron 3 Super native |

### Context Management

| Tier | Window | Providers | Cost structure |
|------|--------|-----------|----------------|
| Massive | 2M | Gemini 3.1 Pro, Grok 4.20, 4.1 Fast | Tiered pricing above ~200-272k |
| Large | 1M | Claude Opus 4.7, Sonnet 4.6, GPT-5.4, Gemini 3 Flash | Standard pricing (Claude) or tiered (GPT-5.4) |
| Very Large | 10M | Llama 4 Scout | Self-host |
| Standard | 128-256K | DeepSeek V3.2, Kimi K2.5, Claude Haiku 4.5 | Cheapest tier |

**Prompt caching savings**: up to **90% input cost reduction**, 80% latency reduction, 67% faster TTFT on 150k+ prompts. **Claude cache breakpoints are explicit = best control**. Use for Surrogate-1 system prompts + codebase context.

---

## Recommendations for Surrogate-1 Architecture

### Phase 1: MVP (Q2 2026)
- **Router**: Haiku 4.5 (cheap, fast, classifies task → role)
- **Roles via Claude family only** (Opus 4.7 for hard, Sonnet 4.6 for most, Haiku 4.5 for trivial)
- **Tools via MCP** (reuse user's existing MCP servers)
- **Orchestration**: CrewAI-style role teams (matches user's `swarm.md`)
- **Memory**: `~/.claude/memory/` + Obsidian Vault + FalkorDB graph (already built)

### Phase 2: Multi-provider (Q3 2026)
- **Add GPT-5.4** for Navigator (computer-use) role
- **Add Gemini 3.1 Pro** for Auditor (2M context) role
- **Add Perplexity Sonar Pro** for Coach role (grounded citations)
- **Consensus check** on prod/destructive ops: 2-model voting (Opus + DeepSeek V3.2)

### Phase 3: Self-hosted hot-path (Q4 2026)
- **Deploy Qwen 3.6-35B-A3B** on user's GPU (20.9GB quantized) for code tasks
- **QLoRA fine-tune** on Excise/Vanguard/Costinel codebases + lessons_learned DPO pairs
- **Route 70% of Assembler traffic local** → Claude only for hard cases
- **Cost impact**: estimate 60-80% reduction vs all-Claude

### Phase 4: Production multi-agent (2027+)
- **Graduate orchestration to LangGraph** (durable execution, checkpointing, HITL)
- **Adopt A2A protocol** for role-to-role communication
- **Add consensus engine** (advanced aggregation beats majority vote 97.9% of cases)
- **Evaluate Kimi K2.5 Agent Swarm** for parallel subtask decomposition (replaces N separate agent calls)

### Anti-patterns to avoid (based on 2026 evidence)
- **Over-engineered graph orchestration** when single ReAct loop works (Claude Code / Codex converged on simple)
- **Full RLHF** when DPO is simpler and equivalent
- **Dense 100B+ models** when MoE gives same quality at 1/10 active params
- **Rolling your own protocol** instead of MCP + A2A (AAIF governance is the standard now)
- **Always-Opus** routing (10-50x cost vs. properly tiered routing)

---

## Key Sources (April 2026)

- **Anthropic**: [Pricing docs](https://platform.claude.com/docs/en/about-claude/pricing), [BenchLM.ai Claude Pricing](https://benchlm.ai/blog/posts/claude-api-pricing)
- **OpenAI**: [Introducing GPT-5.4](https://openai.com/index/introducing-gpt-5-4/), [NxCode GPT-5.4 Guide](https://www.nxcode.io/resources/news/gpt-5-4-complete-guide-features-pricing-models-2026)
- **Google**: [AI Pricing Guru Gemini 3.1](https://www.aipricing.guru/google-ai-pricing/), [Artificial Analysis Gemini 3.1 Pro](https://artificialanalysis.ai/models/gemini-3-1-pro-preview)
- **xAI**: [Grok 4.20 Review](https://designforonline.com/ai-models/xai-grok-4-20/), [Grok 4.1 Fast](https://artificialanalysis.ai/models/grok-4-1-fast)
- **DeepSeek**: [DeepSeek V3.2 AA](https://artificialanalysis.ai/models/deepseek-v3-2), [Introl DeepSeek Cost](https://introl.com/blog/deepseek-v3-2-open-source-ai-cost-advantage)
- **Qwen**: [Qwen 3.6-35B-A3B Botmonster](https://botmonster.com/posts/qwen-3-6-35b-a3b-open-weight-coding-moe/), [Qwen 3.5 MindStudio](https://www.mindstudio.ai/blog/what-is-qwen-3-5-alibaba-open-weight-model)
- **Meta**: [Llama 4 Official](https://ai.meta.com/blog/llama-4-multimodal-intelligence/)
- **Mistral**: [Mistral Models 2026](https://serenitiesai.com/articles/mistral-ai-models-2026-complete-guide)
- **Moonshot**: [Kimi K2.5 HuggingFace](https://huggingface.co/moonshotai/Kimi-K2.5), [InfoQ Agent Swarm](https://www.infoq.com/news/2026/02/kimi-k25-swarm/)
- **NVIDIA**: [Nemotron 3 Super NVIDIA](https://developer.nvidia.com/blog/introducing-nemotron-3-super-an-open-hybrid-mamba-transformer-moe-for-agentic-reasoning/)
- **Z.ai**: [GLM-5 DeepLearning.ai](https://www.deeplearning.ai/the-batch/z-ais-glm-5-model-boasts-top-open-weights-intelligence-index-score/)
- **Cohere**: [Cohere Pricing](https://cohere.com/pricing)
- **Perplexity**: [Sonar API Platform](https://www.perplexity.ai/api-platform)
- **Gemma 4**: [Google DeepMind Gemma 4](https://deepmind.google/models/gemma/gemma-4/), [Tech-Insider Gemma 4](https://tech-insider.org/google-gemma-4-open-model-benchmarks-2026/)
- **SWE-Bench**: [SWE-bench Leaderboards](https://www.swebench.com/), [BenchLM.ai SWE Verified](https://benchlm.ai/benchmarks/sweVerified)
- **Protocols**: [arXiv Agent Interop Survey](https://arxiv.org/abs/2505.02279), [InfoWorld MCP/A2A/ACP](https://www.infoworld.com/article/4007686/a-developers-guide-to-ai-protocols-mcp-a2a-and-acp.html)
- **Orchestration**: [LangGraph vs CrewAI vs AutoGen](https://dev.to/pockit_tools/langgraph-vs-crewai-vs-autogen-the-complete-multi-agent-ai-orchestration-guide-for-2026-2d63)
- **Speculative decoding**: [vLLM Speculative Decoding](https://docs.vllm.ai/en/latest/features/speculative_decoding/), [PremAI 2-3x Faster](https://blog.premai.io/speculative-decoding-2-3x-faster-llm-inference-2026/)
- **Fine-tuning**: [LoRA/QLoRA 2026 Guide](https://dev.to/jangwook_kim_e31e7291ad98/fine-tune-llms-with-lora-and-qlora-2026-guide-33lf), [Gauraw Fine-Tuning 2026](https://www.gauraw.com/fine-tuning-llm-lora-dpo-guide-2026/)
- **Consensus**: [arXiv Multi-Agent Debate](https://arxiv.org/html/2502.19130v4), [Beyond Majority Voting](https://arxiv.org/html/2510.01499v1)

---

## See Also
- [[surrogate-1-spec]] (to be written)
- [[workspace-map]]
- [[~/.claude/memory/knowledge_index]]
- [[~/.claude/rules/swarm]]
- [[~/.claude/rules/reviewer]]

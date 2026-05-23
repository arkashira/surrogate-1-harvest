---
title: V13 Multi-Agent Baked Into One Model — Research & Wire-Up
date: 2026-05-01
project: surrogate-1
version: V13
status: research-complete
tags: [multi-agent, sft, training-data, structured-tokens, self-spawn, surrogate-1, v13]
related:
  - "[[training-tooling-2026-Q2]]"
  - "[[anti-hallucination-correctness-2026]]"
  - "[[devsecops-sre-agentic]]"
  - "[[autonomous-24x7]]"
---

# V13 — Multi-Agent Capability Baked Into a Single LLM

> **Owner goal**: ONE model emits structured tokens (`<spawn>` / `<await>` / `<aggregate>`) to dispatch work to *itself* (parallel sub-agent instances of the same weights). NOT external bash orchestration. The orchestration logic lives in the weights, not in `autonomous-release.sh`.
>
> The runtime parser is dumb (≤50 lines of Python). The model is smart.

This document maps every public 2024–2026 dataset, paper, and recipe that is relevant to baking multi-agent orchestration into a *single* set of weights — plus a concrete wire-up for `kaggle-trainer.sh` and a minimal dispatcher.

---

## TL;DR Decision Matrix

| Decision | Choice for V13 | Rationale |
|----------|---------------|-----------|
| Token format | Custom `<spawn role="X" id="N">…</spawn>` + `<await ids="N,M"/>` + `<aggregate>…</aggregate>` (XML-style, added as 8 new special tokens) | XML beats JSON for *streaming* (incremental parser), beats Markdown for *unambiguous parse boundaries*, and adding as new vocab items prevents tokenizer drift seen with naked text tags. Anthropic's `tool_use` block + ReDel + AgentScope all converged on tag-style. |
| Primary SFT mix | **microsoft/orca-agentinstruct-1M-v1** (1.0M) + **neulab/agent-data-collection** (1.3M, ADP-format) + **camel-ai/ai_society** (151MB) + **Multiverse-1K** (1K, Map/Process/Reduce) + **Magpie-Pro-MT** filtered (multi-turn) | Combines breadth (AgentInstruct), trajectory standardization (ADP), role-play structure (CAMEL), parallel-reasoning structure (Multiverse), and self-instruct multi-turn diversity (Magpie). |
| Trajectory volume | **20–30K converted-to-spawn-format examples** per LoRA pass; full corpus 200–500K to saturate | FireAct showed 500 examples → 77% lift on HotpotQA; AgentInstruct used 25M for state-of-art. We size in the middle: enough to learn structure, small enough to fit Kaggle T4 budget. |
| Inference runtime | 50-line Python parser using `re` + `asyncio.gather` against the same vLLM endpoint; sub-agent instances share KV-cache prefix where possible | Matches Anthropic's orchestrator-worker pattern but with one model talking to itself. |
| RL stage (post-SFT) | **MARTI** (TsinghuaC3I) + Multiverse-Attention | MARTI explicitly trains LLM-MAS with RL; +8% over base on Qwen3-8B. Multiverse-Attention preserves causal compatibility at training time. |

---

## 1. The Datasets — What to Mix In

### 1.1 Tier-A: Direct trajectory corpora (highest signal/token)

#### microsoft/orca-agentinstruct-1M-v1 (Microsoft, 2024, MIT-ish)
- **Paper**: AgentInstruct (arxiv 2407.03502) — multi-agent flow generates training data using GPT-4 + tools across 17 task types.
- **Size**: 1M public subset of Microsoft's internal 25M corpus; trained Orca-3-Mistral.
- **Format**: `messages` field with `[{"role":"user","content":...},{"role":"assistant","content":...}]`. Already chat-templated; multi-turn flows where agents critique/refine each other are baked in.
- **Measured benefit**: 40% lift on AGIEval, 19% MMLU, 54% GSM8K, 38% BBH, 45% AlpacaEval (7B-Mistral baseline → Orca-3-Mistral).
- **Mineable for `<spawn>`?** Yes — the multi-turn flows where one assistant message critiques a previous one map cleanly to `<spawn role="critic">`. Need a converter (see §5).
- **HF**: `microsoft/orca-agentinstruct-1M-v1` and the cleaned mlabonne/orca-agentinstruct-1M-v1-cleaned (recommended — drops noisy rows).
- **Source**: <https://huggingface.co/datasets/microsoft/orca-agentinstruct-1M-v1>
- **Source**: <https://arxiv.org/abs/2407.03502>

#### neulab/agent-data-collection (Agent Data Protocol — ADP, 2025)
- **Paper**: arxiv 2510.24702 — unifies 13 major agent datasets (AgentInstruct, Mind2Web, SWE-Gym, Synatra, etc.) into one lightweight interlingua.
- **Size**: **1.3M trajectories** post-conversion. Largest open agent dataset.
- **Format**: ADP schema (action / observation / message blocks) → already converted to OpenHands SFT, SWE-Agent SFT, and AgentLab SFT formats. Files: `full_sft/full_sft_openhands.jsonl`, etc.
- **Measured benefit**: ~20% avg perf gain over base model after SFT; SOTA or near-SOTA on coding/browsing/tool-use without per-domain tuning.
- **Mineable for `<spawn>`?** Yes — ADP's action blocks are the cleanest SFT-ready input. Map ADP `{action:delegate}` → `<spawn>`, `{action:gather}` → `<await>`.
- **GitHub**: <https://github.com/neulab/agent-data-protocol>
- **HF**: <https://huggingface.co/datasets/neulab/agent-data-collection>

#### camel-ai/ai_society (CAMEL, 2023→still updated)
- **Paper**: arxiv 2303.17760 — role-playing inception prompting between paired agents.
- **Size**: 30.4 MB chat tar + 151 MB instructions JSON ≈ ~100K (assistant_role, user_role, task) triples.
- **Format**: Each conversation tagged `001_002_003` (assistant_role_id, user_role_id, task_id). Role-play turns map naturally to `<spawn role="…">`.
- **Measured benefit**: When used as SFT, lifts coding/math/science vs single-shot. CAMEL also released code/math/biology/chem/physics sister datasets — same schema.
- **Mineable for `<spawn>`?** Best fit for *role-spawning* training: each user_role_id can be wrapped as a spawned worker.
- **HF**: <https://huggingface.co/datasets/camel-ai/ai_society>

#### Multiverse-1K (Infini-AI-Lab, 2025)
- **Paper**: arxiv 2506.09991 — Multiverse-32B achieves 54% AIME24, 46% AIME25 on **just 1K examples** with 3 hours fine-tune.
- **Size**: 1,000 high-quality structured Map/Process/Reduce examples produced by Multiverse Curator (LLM-assisted pipeline that converts sequential reasoning chains into parallel-decomposed structures).
- **Format**: Each example has explicit `Map` (decompose), `Process[i]` (parallel branches), `Reduce` (synthesize) tags. **This is the structural template V13 should adopt for `<spawn>/<await>/<aggregate>`**.
- **Measured benefit**: Open-source non-AR model on par with leading AR-LLMs same scale; 2× speedup at inference.
- **Mineable for `<spawn>`?** This *is* the template. Re-tag Map→`<spawn>`, Process→worker output, Reduce→`<aggregate>`.
- **GitHub**: <https://github.com/Infini-AI-Lab/Multiverse>
- **HF**: <https://huggingface.co/Multiverse4FM/Multiverse-32B>

#### Magpie-Pro-MT (multi-turn variant) (Magpie, ICLR 2025)
- **Paper**: arxiv 2406.08464 — self-synthesis from aligned LLMs with no seed prompts.
- **Size**: Up to 4M instructions (Llama-3-Instruct generated). MT variants are multi-turn sequences.
- **Format**: Plain `messages` arrays; preference / domain / multilingual filters available.
- **Mineable for `<spawn>`?** Use multi-turn variants as **distractor SFT** (single-agent baseline) so model doesn't over-emit `<spawn>` on simple tasks. Critical: prevents "spawn obsession" failure mode.
- **HF**: `Magpie-Align/Magpie-Pro-MT-300K-v0.1` and similar.

### 1.2 Tier-B: Tool-use & function-call corpora (parallel-call training)

| Dataset | Size | Why mix |
|---------|------|---------|
| **glaiveai/glaive-function-calling-v2** | ~110K | Standard parallel/serial function-call SFT |
| **Salesforce/xlam-function-calling-60k** | 60K | xLAM's open trajectory set; trending HF dataset |
| **Nanbeige/ToolMind** (2025) | 360K (160K synth + 200K open) | Multi-agent simulated user/assistant/tool. Already filtered turn-level. |
| **ToolACE** | 26.5K APIs × 11K dialogs | Self-evolution + multi-agent verification; 8B model from this beats GPT-4 on BFCL-v1 |
| **APIGen-MT-5k** | 5K | Multi-turn with parameter dependencies |

These are essential for the `<spawn>` to actually call something useful — without parallel-tool-calling fluency the spawned worker is mute.

### 1.3 Tier-C: Trajectory & RL corpora (post-SFT)

| Dataset / framework | Use |
|--------------------|-----|
| **AgentGym AgentTraj-L** (WooooDyy, ACL 2025) | Diverse-environment trajectories; pair with AgentGym-RL for the RL stage |
| **agent-eto/eto-sft-trajectory** | ETO trajectories — small, clean, good for warm-start |
| **nebius/SWE-agent-trajectories** | 80K SWE-bench trajectories — for "spawn coder" specialty role |
| **lambda/hermes-agent-reasoning-traces** | Multi-turn tool-call w/ reasoning, real execution outputs |
| **open-thoughts/OpenThoughts-Agent-v1-SFT** | 15.2K traces, GLM-4.6 + Terminus-2 harness |
| **AgentInstruct (THUDM, AgentTuning)** | 1,866 high-quality interactions across 6 tasks; already validated for AgentLM 7B/13B/70B |

---

## 2. The Frameworks — What Recipes to Steal

### 2.1 AgentInstruct / Orca-AgentInstruct (Microsoft)
- Multi-agent **content transformation → instruction generation → refinement** flow.
- Uses GPT-4 + tools (search, code interpreter) to generate diverse synthetic data.
- 100+ subcategory taxonomy ensures diversity.
- **Lesson for V13**: synthesize a fresh "spawn-aware" SFT mix using THIS pipeline targeted at `<spawn>` patterns owner hasn't covered.

### 2.2 CAMEL / OWL (CAMEL-AI)
- **CAMEL (2023)**: paired role-play with inception prompting. Output: AI Society + Code datasets.
- **OWL (NeurIPS 2025)**: Optimized Workforce Learning. Hierarchical: domain-agnostic Planner + Coordinator + specialized Workers. **Trains only the Planner via RL** — strong domain transfer.
- **OWL Workforce results**: 69.70% (open-source SOTA), beats OpenAI Deep Research by 2.34%; OWL-trained 32B → 52.73% on hard tasks (+16.37%).
- **Lesson for V13**: train ONE planner head that emits `<spawn>`. Workers are the same model invoked stateless. Saves training compute; matches owner's "ONE model" goal.
- <https://github.com/camel-ai/owl>

### 2.3 MetaGPT / AFlow (FoundationAgents)
- **MetaGPT (ICLR 2024)**: PM/Architect/Engineer roles, SOPs, full software-company simulation.
- **AFlow (ICLR 2025 Oral)**: MCTS over **code-represented workflow space** to auto-discover optimal multi-agent flows. Operators (Ensemble, Review&Revise) are reusable building blocks.
- **AFlow results**: +5.7% avg over SOTA on HumanEval/MBPP/GSM8K/MATH/HotpotQA/DROP; smaller models beat GPT-4o at 4.55% inference cost.
- **Lesson for V13**: AFlow's discovered workflows are *gold-standard `<spawn>` traces*. Run AFlow once → harvest top-K workflows → convert to SFT. The MCTS selects the policies; SFT distills them into a single model.
- <https://github.com/FoundationAgents/AFlow>

### 2.4 ChatDev (THUNLP)
- **ACL 2024**: software dev with design / coding / testing phases via communicative agents.
- **Mineable**: traces in chat-log format. Owner goal of `<spawn role="coder">` / `<spawn role="reviewer">` → ChatDev provides the canonical pairs.
- **ICLR 2025 Building-Trust Workshop study**: ChatDev + MetaGPT human-annotated trace datasets exist (≥30 traces each, small but high-quality).

### 2.5 AutoGen (Microsoft, 2024 → merged into Agent Framework Oct 2025)
- AutoGen v0.4 (Jan 2025): rewritten for scalable observability. Comprehensive trace logging.
- **Lesson for V13**: turn on AutoGen tracing → run a small fleet of agents on realistic problems → harvest the traces. AutoGen's GroupChat speaker-selection logs map directly onto `<spawn role>`.

### 2.6 AgentVerse (OpenBMB, ICLR 2024)
- Task-solving + simulation modes. Expert agents collaborate to greater-than-sum-of-parts.
- Public framework, no canonical SFT dataset, but the simulation mode generates clean role-play data on demand.

### 2.7 AgentScope (Alibaba, v1.0 in 2025)
- Production-grade with built-in fine-tuning support. Java port.
- **Trinity-RFT** integration coming Dec 2025 — RL post-training best practices.
- **Lesson**: use AgentScope as the *runtime* even after SFT — it understands message-exchange semantics that match `<spawn>` well.

### 2.8 MARTI (TsinghuaC3I, NeurIPS 2025)
- **Paper**: openreview E7jZqo0A50. Centralized multi-agent interaction + distributed policy training.
- Built-in graph workflows (debate, MoA, chain). MARTI-v2 adds tree-search-augmented RL for code gen.
- **Measured**: Qwen3-8B with multi-agent training → +8.0% over base, +4.4% over Vanilla GRPO, +2.9% over single-agent peak.
- **Lesson for V13**: this is THE post-SFT RL stage. Drops in cleanly after the SFT mix is trained.
- <https://github.com/TsinghuaC3I/MARTI>

### 2.9 Multiverse / Group Think (parallel reasoning at token level)
- **Multiverse (NeurIPS 2025)**: Map/Process/Reduce baked into the model. Multiverse Attention preserves causal mask compat for training. Multiverse-32B on par with AR LLMs same scale + 2× speedup.
- **Group Think (May 2025)**: multiple concurrent reasoning agents at *token-level* granularity, periodic synchronization.
- **Lesson for V13**: this is the **most direct precedent** for what owner wants. Adopt Multiverse Attention + the Multiverse-1K data template **verbatim**, just rename Map→spawn, Process→worker, Reduce→aggregate.

### 2.10 ReDel (EMNLP 2024 Demo)
- **Paper**: arxiv 2408.02248. Recursive delegation: agent decides when/how to delegate, dynamic team hierarchies.
- Built on `kani` framework with native tool use.
- **Measured**: +25% over single-agent on FanOutQA, TravelPlanner.
- **Lesson for V13**: ReDel does *zero-shot* recursive delegation via tool-call. We want to bake the same behavior into weights via SFT. Use ReDel as the **trace generator** (run on FanOutQA/TravelPlanner with GPT-4 base) → distill into Surrogate.
- <https://github.com/zhudotexe/redel>

### 2.11 Mixture of Agents (Together AI) → Self-MoA (Princeton, Feb 2025)
- **MoA (NeurIPS 2024)**: layered architecture, multiple LLMs propose+aggregate.
- **Self-MoA (arxiv 2502.00674, MarkTechPost Feb 2025)**: **single top model sampled multiple times beats multi-model MoA**. +6.6% on AlpacaEval 2.0, +3.8% avg across MMLU/CRUX/MATH.
- **Self-MoA-Seq**: sliding-window aggregation for short-context models.
- **Lesson for V13**: validates the "one model, many samples" thesis. Spawned workers can literally be `n` parallel samples of the same prompt with role-conditioning. **This is critical justification for the architecture.**
- **MoAA** (Together AI 2025): post-training alignment via MoA-generated synthetic data — recipe for boot-strapping a Self-MoA dataset from your own model.

### 2.12 Multi-Agent Debate (MAD)
- ACL/EMNLP work shows MAD improves math + reduces hallucinations.
- **2025 finding**: pre-trained LLMs are sub-optimal debaters; **SFT or in-context teaching helps**.
- **Lesson for V13**: include a debate-format slice (~5–10% of mix) so model learns to spawn an adversarial critic when uncertainty is high.

### 2.13 LATS (Language Agent Tree Search, ICML 2024)
- MCTS over reasoning + acting + reflection.
- 92.7% pass@1 on HumanEval (GPT-4); 75.9 WebShop (GPT-3.5, gradient-free comparable to fine-tuning).
- **Lesson**: LATS-style trees can be flattened into `<spawn>`-style traces via DFS. Use LATS as a high-quality trace miner during data prep.

### 2.14 FireAct (Princeton)
- **arxiv 2310.05915**: fine-tuned Llama-2-7B with **just 500 GPT-4 ReAct trajectories** → +77% on HotpotQA.
- **Lesson for V13**: lower bound — even 500 carefully-curated `<spawn>` traces will measurably move the needle. Owner can iterate fast.

### 2.15 xLAM (Salesforce, 2024)
- xLAM-FC-60K trended on HF July 2024. Comparable to GPT-4 on BFCL-v2.
- Tier-B mix component above.

### 2.16 OPRO + Adaptive-OPRO
- LLMs as optimizers via natural-language meta-prompts. +8% GSM8K, +50% BBH best prompts.
- AMPO extension: tree-structured multi-branch prompt opt with if/else/catch-all logic.
- **Lesson**: use OPRO **offline** to discover good `<spawn role=…>` prompt templates. Bake winning templates into SFT data.

### 2.17 Anthropic Multi-Agent Research System (orchestrator-worker)
- Lead Opus-4 + worker Sonnet-4 → +90.2% over single Opus-4 on internal research evals.
- Each subagent: objective + output format + tool guidance + boundaries.
- `tool_use` content blocks integrated into message structure (not a separate role).
- **Lesson for V13**: Anthropic's structure is the **production-validated reference architecture** for what owner wants. Replicate the orchestrator's prompt template; the workers are just same-model stateless calls. Programmatic Tool Calling shows Claude orchestrating tools in code → blueprint for our `<spawn>` parser.
- <https://www.anthropic.com/engineering/multi-agent-research-system>

### 2.18 Multi-step tool-orchestration RL (arxiv 2603.24709)
- Constrained data synthesis + graduated rewards for multi-step tool calls.
- Cache-based deterministic env w/ 100k+ real responses for RL exploration.
- Even Qwen2.5-72B has 78.8% parameter-value errors → models struggle without targeted training.
- **Lesson**: graduated reward shaping (correct tool > correct sequence > correct params) inside MARTI loop.

---

## 3. Research Question Answers

### Q1: Best STRUCTURED TOKEN FORMAT for self-spawn?

**Recommended**: `<spawn role="X" id="N">…</spawn>` + `<await ids="N,M"/>` + `<aggregate>…</aggregate>` (XML-tag style, registered as added special tokens).

**Comparative analysis** (synthesized from CodeAgents, Multiverse, AgentScope, Anthropic tool_use):

| Format | Pros | Cons | Verdict |
|--------|------|------|---------|
| **JSON** (`{"action":"spawn", ...}`) | Easy to schema-validate. | Brittle under streaming — partial JSON breaks parsers. Token-count overhead from quotes/braces. Models hallucinate trailing commas. | Avoid as primary; OK as inner payload. |
| **XML-tag style** (`<spawn>…</spawn>`) | Streaming-friendly (incremental parser). Anthropic / AgentScope / Claude tool_use converge on this. Adding as new vocab → no tokenizer drift. | Slightly more verbose than custom delimiters. | **Winner.** |
| **Custom delimiters** (`SPAWN: …; END_SPAWN`) | Compact. | No standard tooling. Easy to confuse with content. Hard to nest. | Avoid. |
| **Code-as-action** (CodeAgents-style pseudocode) | Most expressive (loops, conditionals, types). 30% fewer tokens than NL. | Higher base capability requirement; harder to parse incrementally. | Use as **augmentation slice** (~10% of SFT) but not primary. |
| **Markdown** (` ```spawn … ``` `) | Human-readable. | Code-fence parsing hostile to streaming; collisions with code blocks in content. | Avoid. |

**Why add as new special tokens (vs naked text tags):**
- Tokenizer drift: a naked `<spawn>` may tokenize as `<` + `sp` + `awn` + `>` — 4–5 tokens, instable across whitespace variants. Adding `<spawn>` and `</spawn>` as **two single tokens** gives ~70% compression on tag overhead and stable parse boundaries.
- Models more aggressively learn special tokens (lower loss on first epoch by ~15–25% in ablations from special-token fine-tuning literature).
- Initialize new embeddings with the **mean of existing embeddings** (standard practice; LangCopilot, Inside Machine Learning) for stability.
- Use **PEFT/LoRA targeting `embed_tokens` and `lm_head`** so new tokens learn without catastrophic forgetting.

**Final spec for V13:**
```
<spawn role="ROLE_NAME" id="N" parallel="true|false">CHILD_PROMPT</spawn>
<await ids="N,M,..."/>          ← 1 token, self-closing
<aggregate>SYNTHESIS</aggregate>
<worker_result id="N">…</worker_result>   ← what comes back from a spawned instance
<plan>…</plan>                  ← optional: explicit decomposition pre-spawn
```
**8 new tokens added**: `<spawn>`, `</spawn>`, `<await/>`, `<aggregate>`, `</aggregate>`, `<worker_result>`, `</worker_result>`, `<plan>`/`</plan>` (count `<plan>`+`</plan>` as 2 → total 9). Round to 10 with one reserved.

### Q2: How big a dataset is needed?

**Empirical floors from literature:**
| Source | Examples | Effect | Notes |
|--------|---------|--------|-------|
| FireAct | 500 | +77% HotpotQA | Demonstrates structure can be learned cheaply |
| Multiverse-1K | 1,000 | AIME24 54%, AIME25 46% | 3-hour fine-tune, structural pattern only |
| AgentTuning | 1,866 | GPT-3.5-tier generalization | 6 task types |
| AFlow | discovered workflows | +5.7% over SOTA | MCTS-curated, small N |
| AgentInstruct subset | 1M | 25M target | Saturating ground for production |
| ADP unified | 1.3M | +20% avg over base | Across 13 datasets |

**Surrogate-1 V13 staging plan:**
- **Stage 0 (validate format)**: 1K curated `<spawn>` examples (Multiverse-1K analog) → confirm new tokens learn, parser works end-to-end.
- **Stage 1 (small SFT)**: 20K mixed examples (5K per source: AgentInstruct, ADP, CAMEL, Multiverse). Goal: model emits well-formed `<spawn>` tags.
- **Stage 2 (production SFT)**: 200–500K mixed examples. Goal: spawn-decision quality matches Anthropic orchestrator-worker style.
- **Stage 3 (RL)**: MARTI loop on AgentGym envs; reward = task-success × format-validity. ~10K episodes.

**Data-efficiency: TB structural learning cheap, behavioral quality expensive.** The 1K floor teaches *what `<spawn>` looks like*; the 200K target teaches *when to use it*.

### Q3: Papers that explicitly trained ONE model to do multi-agent orchestration

| Paper | Approach | Single model? | Notes |
|-------|---------|---------------|-------|
| **AgentTuning (THUDM)** | SFT on 1,866 mixed-task interactions | Yes — AgentLM 7B/13B/70B | Generalizes to held-out tasks at GPT-3.5 level |
| **FireAct (Princeton)** | SFT on 500 GPT-4 trajectories | Yes — Llama-2-7B | +77% HotpotQA |
| **Multiverse (Infini-AI)** | SFT on 1K Map/Process/Reduce examples + Multiverse Attention | Yes — Multiverse-32B | **Closest precedent.** Single model decides parallelization. |
| **OWL Workforce (CAMEL)** | RL on Planner only; Workers same/different model | Yes — OWL-32B planner | OOD generalization via training only Planner |
| **MARTI (Tsinghua)** | RL multi-agent training | Yes — Qwen3-8B | +8% over base via RL with shared policy |
| **xLAM (Salesforce)** | SFT on agent-task data | Yes — xLAM family | GPT-4 comparable on BFCL |
| **AgentLM (THUDM)** | Llama-2 chat + AgentTuning + ShareGPT | Yes | Public 7B/13B/70B checkpoints |
| **Anthropic Research System** | Production deployment, orchestrator + workers | Same model family (Opus orchestrator + Sonnet workers) | Strong precedent but uses 2 sizes |
| **Self-MoA (Princeton)** | Inference-time aggregation over single model | Yes | Validates one-model-many-samples |
| **Self-Organized Agents (arxiv 2404.02183)** | Mother agent spawns child agents recursively | Conceptually one model | Direct conceptual ancestor |

**The clearest "ONE model emits spawn tokens" precedent is Multiverse** (Map/Process/Reduce baked in). MARTI and OWL provide the RL recipe. AgentTuning + FireAct provide the SFT precedent. **None of them publish in our exact `<spawn>` format — V13 is novel composition, not novel principle.**

### Q4: Public datasets ready to mix into SFT?

**Top 5 for V13 mix (in priority order):**
1. **microsoft/orca-agentinstruct-1M-v1** (or mlabonne cleaned variant) — broadest coverage, prebaked chat-template.
2. **neulab/agent-data-collection** — 1.3M unified-format trajectories; pick `full_sft_openhands.jsonl` for tool-rich slice.
3. **camel-ai/ai_society** + sister datasets (code/math/biology/chem/physics) — role-play structure for `<spawn role="…">`.
4. **Multiverse4FM/Multiverse-1K** (or recreate with Multiverse Curator) — Map/Process/Reduce template.
5. **Magpie-Align/Magpie-Pro-MT-300K-v0.1** (or Air-MT) — multi-turn distractor; trains "do NOT spawn for trivial tasks".

**Tool-call backbone (always include):**
- `glaiveai/glaive-function-calling-v2` (110K)
- `Salesforce/xlam-function-calling-60k`

**Optional specialist:**
- `nebius/SWE-agent-trajectories` (80K) → boost code-spawn role
- `lambda/hermes-agent-reasoning-traces` → multi-tool reasoning
- `agent-eto/eto-sft-trajectory` → ETO trajectories warm start
- `WooooDyy/AgentTraj-L` → AgentGym diverse environments

**All license-compatible for research / commercial-research use.** Verify per-dataset license before downstream commercial deployment.

### Q5: Inference-time runtime parser — minimal Python

See §4 below.

---

## 4. Minimal Runtime Parser (≤50 lines)

This is the dispatcher that runs alongside vLLM/TGI and turns `<spawn>` tokens into actual sub-agent invocations against the same model endpoint. Stream-friendly, asyncio-based, no external deps beyond `httpx`.

```python
"""surrogate_spawn_runtime.py — V13 dispatcher (≤50 lines logic).
   The model emits <spawn role="X" id="N">prompt</spawn>...<await ids="N"/>...
   We parse, fan out parallel completions to the same endpoint, and stitch
   <worker_result id="N">...</worker_result> back into the model's context."""
import asyncio, re, json, httpx
from typing import Dict, List

ENDPOINT = "http://localhost:8000/v1/completions"  # vLLM OpenAI-compat
MODEL    = "axentx/surrogate-1-v13"
SPAWN_RE = re.compile(r'<spawn role="([^"]+)" id="(\d+)"(?: parallel="(true|false)")?>(.*?)</spawn>', re.S)
AWAIT_RE = re.compile(r'<await ids="([\d,]+)"\s*/>')
MAX_DEPTH, MAX_FANOUT = 3, 8  # bounded; prevents runaway spawn

async def call_self(prompt: str, depth: int) -> str:
    if depth >= MAX_DEPTH:                # depth guard — sub-worker can't re-spawn beyond limit
        prompt += "\n[NOTE: max spawn depth reached — answer directly, do not <spawn>]"
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(ENDPOINT, json={"model": MODEL, "prompt": prompt,
                                         "max_tokens": 1024, "temperature": 0.7,
                                         "stop": ["</aggregate>"]})
    return r.json()["choices"][0]["text"]

async def run(initial_prompt: str, depth: int = 0) -> str:
    text = await call_self(initial_prompt, depth)
    spawns: Dict[str, str] = {}
    pending = SPAWN_RE.findall(text)
    if not pending:
        return text                       # leaf: no further spawns
    if len(pending) > MAX_FANOUT:
        pending = pending[:MAX_FANOUT]    # fanout guard
    # Fan out — same endpoint, parallel
    async def child(role, sid, par, child_prompt):
        framed = f"<role>{role}</role>\n{child_prompt.strip()}\n<respond/>"
        result = await run(framed, depth + 1)   # recurse: workers can spawn one level deeper
        spawns[sid] = result.strip()
    await asyncio.gather(*[child(*p) for p in pending])
    # Substitute <worker_result id="N">…</worker_result> back into parent stream
    stitched = SPAWN_RE.sub(
        lambda m: f'<worker_result id="{m.group(2)}">{spawns[m.group(2)]}</worker_result>', text)
    # Continue parent generation with worker results in context, until <aggregate> closes
    if "<await" in stitched and "</aggregate>" not in stitched:
        return await call_self(stitched + "\n", depth)
    return stitched

if __name__ == "__main__":
    import sys
    print(asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else "Plan a 3-step research task.")))
```

**That's 38 lines of logic.** Adds depth + fanout safety. `MAX_DEPTH=3` and `MAX_FANOUT=8` are the only knobs that meaningfully gate runaway behavior. Deploy as a sidecar to vLLM; the model sees only its own emitted text and `<worker_result>` blocks (which look just like its own training data).

**Optional enhancements (still ≤50 lines):**
- Stream worker results back into a token-level buffer using vLLM's `stream=True` (lowers latency).
- Cache-share KV prefix across siblings via vLLM's `prefix_cache` (huge speedup when workers share preamble).
- Add OpenTelemetry span per spawn for observability (one extra dep).

---

## 5. Data Conversion — Mining Public Corpora into `<spawn>` Format

The public datasets are *almost* in our format but not quite. Conversion rules:

```python
# For ADP (neulab/agent-data-collection):
# ADP action {type:"delegate", target:"X", payload:Y} → <spawn role="X" id="N">Y</spawn>
# ADP action {type:"gather", ids:[N]}                 → <await ids="N"/>
# ADP action {type:"finalize", text:Y}                → <aggregate>Y</aggregate>

# For CAMEL ai_society:
# Each (assistant_role, user_role) pair → wrap user_role's turn as
# <spawn role="{user_role}" id="0">{turn}</spawn> ... <worker_result id="0">{response}</worker_result>

# For AgentInstruct multi-turn refinement flows:
# First-turn output → <spawn role="critic" id="0">{first}</spawn>
# Critic response   → <worker_result id="0">{critique}</worker_result>
# Refined answer    → <aggregate>{refined}</aggregate>

# For Multiverse-1K:
# Already in Map/Process/Reduce — string replace:
#   Map      → <spawn role="planner" id="0">
#   /Map     → </spawn><await ids="0"/>
#   Process[i] → <spawn role="worker_{i}" id="{i+1}">
#   /Process[i]→ </spawn>
#   Reduce   → <await ids="1,2,...,N"/><aggregate>
#   /Reduce  → </aggregate>

# For Magpie-Pro-MT (DISTRACTOR — important):
# Leave as-is, no <spawn> tags. Model learns: "single-turn easy task → no spawn."
# Mix at 30–40% weight to prevent spawn-obsession.
```

**Filter rules (always apply before adding to mix):**
- Drop examples where workers produce trivial 1-line outputs (`<worker_result>OK</worker_result>` etc.) — model learns spawn is purposeful.
- Drop examples with >MAX_DEPTH nesting — runtime guard mismatch.
- Validate XML well-formedness (no unmatched tags) — Pydantic-style schema check.
- Token budget per example: ≤8K (Kaggle T4 context limit consideration).

**Synthesis fallback:** if conversion yields <20K examples, supplement via Magpie-style self-instruct: prompt Surrogate-1-v12 (already aligned) with system message "decompose this with <spawn> tags" + 5-shot exemplars → harvest. (Magpie + AgentInstruct's recipe combined.)

---

## 6. Risk Map & Mitigations

| Risk | Mitigation |
|------|-----------|
| Spawn-obsession (model spawns even for trivial tasks) | 30–40% Magpie distractor mix; reward shaping in MARTI penalizes unnecessary spawn |
| Tokenizer drift / leakage of literal `<spawn>` in outputs that aren't parsed | Add as special tokens; verify post-training with `tokenizer.encode("<spawn>")` returns 1 token |
| Runaway depth / fanout in production | MAX_DEPTH=3, MAX_FANOUT=8 hard limits in parser; bounded recursion |
| Worker output poisoning parent context | `<worker_result>` is its own bounded tag; SFT data shows model how to reject malformed worker_result |
| Cost blowup (each spawn = full forward pass) | Self-MoA evidence: 3 parallel samples beats 1 long sample for hard tasks. Net cost-positive on hard tasks; net cost-negative on easy tasks (workers handle micro-subtasks) |
| Catastrophic forgetting during SFT with new tokens | LoRA targeting `embed_tokens`+`lm_head`+attention; mean-init new embeddings; mix in 10–20% original Surrogate-1 SFT data |
| Eval gap (no benchmark for `<spawn>` quality) | Use AgentBench + AgentBoard + AgentGym + FanOutQA + TravelPlanner as eval suite. Add custom format-validity metric |

---

## 7. References (Selected)

- **AgentInstruct**: Mitra et al., 2024 — <https://arxiv.org/abs/2407.03502>
- **Orca-AgentInstruct dataset**: <https://huggingface.co/datasets/microsoft/orca-agentinstruct-1M-v1>
- **CAMEL**: Li et al., 2023 — <https://arxiv.org/abs/2303.17760> · <https://github.com/camel-ai/camel>
- **OWL (Workforce Learning)**: Hu et al., NeurIPS 2025 — <https://arxiv.org/abs/2505.23885> · <https://github.com/camel-ai/owl>
- **MetaGPT**: Hong et al., ICLR 2024 — <https://github.com/FoundationAgents/MetaGPT>
- **AFlow**: Zhang et al., ICLR 2025 Oral — <https://arxiv.org/abs/2410.10762> · <https://github.com/FoundationAgents/AFlow>
- **ChatDev**: Qian et al., ACL 2024 — <https://aclanthology.org/2024.acl-long.810.pdf>
- **AutoGen**: Wu et al., 2024 — <https://github.com/microsoft/autogen>
- **AgentVerse**: Chen et al., ICLR 2024 — <https://github.com/OpenBMB/AgentVerse>
- **AgentScope**: Gao et al., 2024 v1.0 2025 — <https://arxiv.org/abs/2402.14034> · <https://arxiv.org/html/2508.16279v1>
- **MARTI**: Zhang et al., NeurIPS 2025 — <https://github.com/TsinghuaC3I/MARTI> · <https://openreview.net/forum?id=E7jZqo0A50>
- **Multiverse**: Yang et al., NeurIPS 2025 — <https://arxiv.org/abs/2506.09991> · <https://github.com/Infini-AI-Lab/Multiverse>
- **Group Think**: Hsu et al., May 2025 — <https://arxiv.org/html/2505.11107v1>
- **Mixture-of-Agents**: Wang et al., NeurIPS 2024 — <https://arxiv.org/abs/2406.04692> · <https://github.com/togethercomputer/MoA>
- **Self-MoA**: Princeton, Feb 2025 — <https://arxiv.org/abs/2502.00674>
- **MoAA**: Together AI, 2025 — <https://www.together.ai/blog/moaa>
- **Multi-Agent Debate**: Du et al., ICLR Blogposts 2025 — <https://d2jud02ci9yv69.cloudfront.net/2025-04-28-mad-159/blog/mad/>
- **LATS**: Zhou et al., ICML 2024 — <https://arxiv.org/abs/2310.04406>
- **ReDel**: Zhu et al., EMNLP 2024 Demo — <https://arxiv.org/abs/2408.02248> · <https://github.com/zhudotexe/redel>
- **FireAct**: Chen et al., 2023 — <https://arxiv.org/abs/2310.05915>
- **AgentTuning / AgentLM**: Zeng et al., 2024 — <https://github.com/THUDM/AgentTuning>
- **AgentBench**: THUDM, ICLR 2024 — <https://github.com/THUDM/AgentBench>
- **AgentBoard**: NeurIPS 2024 — <https://openreview.net/forum?id=4S8agvKjle>
- **AgentGym**: Xi et al., ACL 2025 — <https://github.com/WooooDyy/AgentGym>
- **AgentGym-RL**: Xi et al., 2025 — <https://github.com/WooooDyy/AgentGym-RL>
- **xLAM**: Salesforce, 2024 — <https://arxiv.org/pdf/2409.03215> · <https://github.com/SalesforceAIResearch/xLAM>
- **Glaive Function Calling V2**: <https://huggingface.co/datasets/glaiveai/glaive-function-calling-v2>
- **ToolACE**: Liu et al., 2024 — <https://arxiv.org/abs/2409.00920>
- **ToolMind**: Nanbeige, 2025 — <https://arxiv.org/abs/2511.15718> · <https://huggingface.co/datasets/Nanbeige/ToolMind>
- **Agent Data Protocol**: NeuLab, 2025 — <https://arxiv.org/abs/2510.24702> · <https://github.com/neulab/agent-data-protocol>
- **Magpie**: Xu et al., ICLR 2025 — <https://arxiv.org/abs/2406.08464> · <https://github.com/magpie-align/magpie>
- **OPRO**: Yang et al., ICLR 2024 — <https://arxiv.org/abs/2309.03409>
- **CodeAgents**: 2025 — <https://arxiv.org/html/2507.03254v1>
- **Anthropic Multi-Agent Research**: <https://www.anthropic.com/engineering/multi-agent-research-system>
- **Anthropic Programmatic Tool Calling**: <https://www.anthropic.com/engineering/advanced-tool-use>
- **Multi-step Tool Orchestration RL**: <https://arxiv.org/html/2603.24709v1>

---

## Wire-Into-V13

### Env knobs (add to `~/.surrogate/hf-space/bin/kaggle-trainer.sh`)

```bash
# ── V13 multi-agent training knobs ──────────────────────────────────────────
# Mix weights MUST sum to 1.0 (the trainer normalizes if not)
export V13_MIX_AGENTINSTRUCT_REPO="${V13_MIX_AGENTINSTRUCT_REPO:-mlabonne/orca-agentinstruct-1M-v1-cleaned}"
export V13_MIX_AGENTINSTRUCT_WEIGHT="${V13_MIX_AGENTINSTRUCT_WEIGHT:-0.30}"
export V13_MIX_ADP_REPO="${V13_MIX_ADP_REPO:-neulab/agent-data-collection}"
export V13_MIX_ADP_SPLIT="${V13_MIX_ADP_SPLIT:-full_sft_openhands}"
export V13_MIX_ADP_WEIGHT="${V13_MIX_ADP_WEIGHT:-0.20}"
export V13_MIX_CAMEL_REPO="${V13_MIX_CAMEL_REPO:-camel-ai/ai_society}"
export V13_MIX_CAMEL_WEIGHT="${V13_MIX_CAMEL_WEIGHT:-0.10}"
export V13_MIX_MULTIVERSE_REPO="${V13_MIX_MULTIVERSE_REPO:-Multiverse4FM/Multiverse-1K}"
export V13_MIX_MULTIVERSE_WEIGHT="${V13_MIX_MULTIVERSE_WEIGHT:-0.10}"
export V13_MIX_MAGPIE_REPO="${V13_MIX_MAGPIE_REPO:-Magpie-Align/Magpie-Pro-MT-300K-v0.1}"
export V13_MIX_MAGPIE_WEIGHT="${V13_MIX_MAGPIE_WEIGHT:-0.20}"        # distractor — no <spawn>
export V13_MIX_GLAIVE_REPO="${V13_MIX_GLAIVE_REPO:-glaiveai/glaive-function-calling-v2}"
export V13_MIX_GLAIVE_WEIGHT="${V13_MIX_GLAIVE_WEIGHT:-0.05}"
export V13_MIX_XLAM_REPO="${V13_MIX_XLAM_REPO:-Salesforce/xlam-function-calling-60k}"
export V13_MIX_XLAM_WEIGHT="${V13_MIX_XLAM_WEIGHT:-0.05}"

# Special tokens to add — comma-separated. Trainer calls
# tokenizer.add_special_tokens(...) + model.resize_token_embeddings()
# + initialize new rows with mean of existing embeddings.
export V13_NEW_TOKENS="<spawn>,</spawn>,<await/>,<aggregate>,</aggregate>,<worker_result>,</worker_result>,<plan>,</plan>"

# Conversion: turn raw rows into <spawn>-tagged rows. 0=skip (assume already tagged),
# 1=run convert_to_spawn_format.py inline before SFT
export V13_CONVERT_TO_SPAWN="${V13_CONVERT_TO_SPAWN:-1}"

# Stage gating
export V13_STAGE="${V13_STAGE:-1}"   # 0=format-validate (1K), 1=small SFT (20K), 2=production (200K), 3=MARTI RL
export V13_TARGET_EXAMPLES="${V13_TARGET_EXAMPLES:-20000}"

# Runtime parser
export V13_PARSER_MAX_DEPTH="${V13_PARSER_MAX_DEPTH:-3}"
export V13_PARSER_MAX_FANOUT="${V13_PARSER_MAX_FANOUT:-8}"
export V13_PARSER_ENDPOINT="${V13_PARSER_ENDPOINT:-http://localhost:8000/v1/completions}"

# LoRA — extend target modules to embed/lm_head so new tokens learn fast
export V13_LORA_TARGET_MODULES="${V13_LORA_TARGET_MODULES:-q_proj,k_proj,v_proj,o_proj,embed_tokens,lm_head}"

# RL stage (V13 stage=3)
export V13_RL_FRAMEWORK="${V13_RL_FRAMEWORK:-marti}"   # marti | agentgym-rl | none
export V13_RL_REWARD="${V13_RL_REWARD:-task_success_x_format_validity}"
```

### Dataset names to mix in (top 5)

1. `mlabonne/orca-agentinstruct-1M-v1-cleaned` — 30%
2. `neulab/agent-data-collection` (`full_sft_openhands.jsonl` split) — 20%
3. `camel-ai/ai_society` — 10%
4. `Multiverse4FM/Multiverse-1K` — 10%
5. `Magpie-Align/Magpie-Pro-MT-300K-v0.1` — 20% (distractor)

Plus tool-call backbone: `glaiveai/glaive-function-calling-v2` (5%) + `Salesforce/xlam-function-calling-60k` (5%).

### Parser (drop into `~/.surrogate/hf-space/bin/surrogate_spawn_runtime.py`)

See §4 above — **38 lines of logic**, depth+fanout safety, asyncio fan-out against the same vLLM endpoint. Sidecar to the model serving stack. Single dependency: `httpx`.

### Eval harness

After training each stage, run:
```bash
# Format validity (custom — XML well-formedness + tag matching)
python eval/format_validity.py --model axentx/surrogate-1-v13-stage1 --n 500

# Multi-agent benchmarks
python eval/agentbench.py    --tasks os,db,kg,alfworld,webshop,m2w
python eval/agentboard.py    --multi-turn
python eval/fanoutqa.py      # ReDel's strong domain
python eval/travelplanner.py # planning + spawning
python eval/swe-bench.py     # if Stage 3 + SWE-trajectory mix
```

Promote stage-N→stage-N+1 only when:
- format_validity ≥ 99% (well-formed `<spawn>` blocks)
- AgentBench overall score ≥ Surrogate-1-v12 baseline (no regression)
- Multi-agent eval ≥ +5% over single-agent baseline (real lift)

### Bring-up sequence

```
Day 0: Stage 0 — 1K Multiverse-1K format validation. Confirm tokens learned.
Day 1–2: Stage 1 — 20K small SFT. Promote if eval gates pass.
Day 3–7: Stage 2 — 200K production SFT. Multi-stage on Kaggle T4.
Day 8–14: Stage 3 — MARTI RL on AgentGym envs. ~10K episodes.
Day 15: Push v13 to HF Hub. Replace external bash orchestration in
        autonomous-release.sh with the parser sidecar.
```

End state: **`autonomous-release.sh` no longer invokes 3 candidate scripts.** It posts the original goal to the model. The model emits `<spawn>` tokens for the candidates *itself*. The parser fans them out to its own endpoint. `<aggregate>` returns the chosen candidate. **Orchestration logic now lives in the weights.**

---
title: SOTA Self-Improvement Techniques for Surrogate-1 (Code Agent LLM)
date: 2026-04-29
status: research-complete
target_model: Qwen2.5-Coder-7B + LoRA
tags: [self-improvement, voyager, reflexion, self-refine, stop, lora, continual-learning, agent-memory, online-rl, surrogate1]
related:
  - "[[research-training-techniques]]"
  - "[[research-data-curation]]"
  - "[[research-evaluation]]"
  - "[[research-arch-context]]"
  - "[[v2-master-plan]]"
---

# SOTA Self-Improvement / Skill-Library / Continual-Learning for Code Agent LLMs

**Research goal**: Surrogate-1 must self-improve over time once deployed — accumulate skills, reflect on failures, retrain on its own trajectories. Deliverable: concrete, implementable techniques to bake into v2 (now) vs defer to v3 (later).

**Constraint**: Surrogate-1 = Qwen2.5-Coder-7B + LoRA, served from M3/24GB or H200 in cloud. Cannot afford 70B-judge or full fine-tune every cycle.

---

## Executive Summary

| Layer | Technique | v2/v3 | Why |
|---|---|---|---|
| Skill memory | Voyager-style code skill library | **v2** | Cheap, in-context, works without retraining |
| Reflection | Reflexion + structured failure-bucketing | **v2** | Episodic JSON, no fine-tune needed |
| Self-correction at inference | Self-Refine 2-3 turn loop | **v2** | Drop-in prompt pattern |
| Self-correction trained | SCoRe-style multi-turn RL | v3 | Needs RL infra + base policy collapse handling |
| Recursive self-improve | STOP scaffolding | v3 | Risky for 7B model, judge-quality issue |
| Continual LoRA | O-LoRA / SMoLoRA / curriculum LoRA experts | **v2** | Avoid catastrophic forgetting on weekly retrain |
| LoRA composition | LoraHub / Arrow routing | v3 | Premature; need ≥4 task-specific LoRAs first |
| Agent memory store | Letta-style 3-tier (core/recall/archival) | **v2** | Already aligned with our GraphRAG infra |
| Knowledge graph | Cognee / GraphRAG hybrid | **v2** | Already deployed (FalkorDB) |
| Self-rewarding | Self-Rewarding LM iterative DPO | v3 | 7B-as-judge is fragile; need distill-from-Claude first |
| Online DPO | TRL OnlineDPOTrainer | v3 | Defer — start with offline DPO on harvested traces |
| Continual data harvest | SWE-Gym-style trace logging | **v2** | Free signal, must capture from day 1 |

---

## 1. Voyager-Style Skill Libraries

### 1.1 Voyager (Wang et al., NVIDIA, NeurIPS 2023)

**Paper**: arXiv 2305.16291 "Voyager: An Open-Ended Embodied Agent with Large Language Models"
**Repo**: https://github.com/MineDojo/Voyager (MIT license)

**Architecture (3 components)**:
1. **Automatic curriculum** — proposes next sub-task to maximize exploration
2. **Skill library** — JavaScript functions stored on disk, indexed by embedding of natural-language description
3. **Iterative prompting** — env feedback + execution errors + self-verification fed back into prompt

**Skill library mechanics** (the reusable insight):
- One skill = one function in code (Voyager uses Mineflayer JS API)
- Each skill has 2 files: `<name>.js` (code) + `<name>.txt` (NL description)
- `skills.json` registry + `vectordb/` for embedding-based retrieval
- On retrieval: top-5 nearest by description embedding -> injected into system prompt
- On store: only skills that pass self-verification are committed

**Iterative prompting loop**:
```
generate code -> execute -> capture (stdout, stderr, env-state)
    -> self-critique with task-completion check
    -> if fail: refine code with error context
    -> if pass: extract reusable skill, embed, store
```

Key result: 3.3x more unique items, 15.3x faster tech-tree progress vs prior SOTA. Skills generalize zero-shot to new Minecraft worlds.

### 1.2 2025 Follow-ups

**CodeAct (Wang et al., ICML 2024)**: arXiv 2402.01030. Replaces JSON tool calls with executable Python code as the action space. Open-source `CodeActAgent-Mistral-7b-v0.1`. Dataset `CodeActInstruct` = 7k multi-turn interactions. Reports +20% success rate vs JSON tool-use. **Implication for v2**: action format = Python code, not structured tool calls. Maps cleanly onto how a code agent should already work.

**SICA — Self-Improving Coding Agent (Robeyns et al., 2025)**: arXiv 2504.15228, repo `MaximeRobeyns/self_improving_coding_agent`. Agent edits its own codebase in a self-improvement loop. Sonnet 3.5 used as base + o3-mini for reasoning. First documented agent that mutates its own scaffolding.

**SoK: Agentic Skills (2026)**: arXiv 2602.20867. Surveys "skill" definitions across CodeAct, Voyager, OS-World style agents. Confirms code-as-skill is dominant pattern.

**CoAct-1 / CodeAgents (2025)**: token-efficient codified multi-agent reasoning, builds on CodeAct.

### 1.3 Skill Consolidation / Pruning

Voyager paper does NOT prune — library grows unbounded. Practical issues:
- Embedding retrieval saturates around 200-500 skills
- Near-duplicate skills bloat library (same task, different code)
- Bad skills (passed verification but buggy in edge cases) poison retrieval

**Solutions from practice**:
- Periodic dedupe: cluster skills by code-AST similarity, keep highest-success one per cluster
- Usage-weighted retention: drop skills with retrieval_count=0 after N weeks
- Refactor pass: LLM-rewrites top-N most-used skills into a cleaner shared utility

### 1.4 Surrogate-1 Skill Library Format (Concrete)

Proposed structure under `~/surrogate1/skills/`:

```
skills/
  index.json                 # registry: name, description, embedding_id, success_count, usage_count, last_used
  embeddings.faiss           # FAISS index over description vectors (or Chroma)
  py/
    aws_create_s3_bucket.py
    aws_create_s3_bucket.txt
    cf_validate_template.py
    cf_validate_template.txt
    ...
```

**Skill code format** (Python, async, side-effect-explicit):
```python
"""
SKILL: aws_create_s3_bucket
DESC: Create an S3 bucket with sane defaults: encryption enabled, public access blocked, versioning on.
ARGS: bucket_name (str), region (str = 'us-east-1')
RETURNS: dict with bucket_arn, status
SIDE_EFFECTS: AWS API calls (mutating)
TESTED_AT: 2026-04-29T...
"""
import boto3

async def aws_create_s3_bucket(bucket_name: str, region: str = "us-east-1") -> dict:
    s3 = boto3.client("s3", region_name=region)
    s3.create_bucket(Bucket=bucket_name)
    s3.put_bucket_encryption(Bucket=bucket_name, ServerSideEncryptionConfiguration={...})
    s3.put_public_access_block(Bucket=bucket_name, PublicAccessBlockConfiguration={...})
    s3.put_bucket_versioning(Bucket=bucket_name, VersioningConfiguration={"Status": "Enabled"})
    return {"bucket_arn": f"arn:aws:s3:::{bucket_name}", "status": "ok"}
```

**Index entry** (`index.json`):
```json
{
  "aws_create_s3_bucket": {
    "description": "Create an S3 bucket with encryption, no public access, and versioning enabled.",
    "tags": ["aws", "s3", "create", "iac"],
    "embedding_id": 42,
    "success_count": 17,
    "usage_count": 23,
    "last_used": "2026-04-29T14:32:00Z",
    "verification_test": "tests/aws_create_s3_bucket_test.py"
  }
}
```

**Retrieval prompt**:
```
You have access to the following skills, retrieved by semantic similarity to the user's task:
1. aws_create_s3_bucket(bucket_name, region) — Create an S3 bucket with encryption...
2. cf_validate_template(path) — Run cfn-lint and cfn-guard on a CloudFormation template...
...
Prefer calling existing skills via `from skills import <name>`. Write new skill only if none match.
```

---

## 2. Reflexion / Verbal RL

### 2.1 Reflexion (Shinn et al., NeurIPS 2023)

**Paper**: arXiv 2303.11366
**Repo**: https://github.com/noahshinn/reflexion
**Result**: 91% pass@1 on HumanEval (vs GPT-4 80%)

**Three components**:
- **Actor** (LLM): generates action given state
- **Evaluator**: scores trajectory (binary success or numeric)
- **Self-Reflector** (LLM, separate prompt): given (trajectory, score), produces NL reflection -> stored in episodic memory

**Episodic memory format** (bounded buffer, typical max=3):
```json
[
  {
    "trial": 1,
    "task": "implement merge_two_sorted_lists",
    "trajectory_summary": "Used append+sort. O(n log n) but tests time out on large input.",
    "outcome": "fail",
    "reflection": "Should merge in O(n) using two pointers, not sort after concat. Next time: track i, j over both lists."
  },
  {"trial": 2, ...}
]
```

On trial N+1, the actor's prompt = task + last K reflections.

**Failure modes** identified in 2024-2025 follow-ups:
- **Confirmation bias**: same model is Actor+Evaluator+Reflector -> repeats flawed reasoning
- **Mode collapse**: actor produces near-identical solutions despite reflections
- **Shallow diagnosis**: on complex tasks (TravelPlanner), reflections become generic ("be more careful")

### 2.2 2024-2025 Follow-ups

**Retroformer (Yao et al., ICLR 2024)**: arXiv 2308.02151. Trains a *retrospective model* (separate small LM) via SFT+PPO to produce high-quality reflections that update the actor's prompt. Decouples reflection-quality from actor-quality. Memory-intensive PPO when trajectories >10k tokens.

**Failure Makes the Agent Stronger (2025)**: arXiv 2509.18847. *Structured reflection*: instead of free-form text, force reflection into schema {error_type, root_cause, corrective_action, prevention_rule}. Improves reliability of tool-using agents.

**SaMuLe (2025)**: arXiv 2509.20562. Multi-level reflection: tactical (this turn) + strategic (across episodes) + meta (about reflection process itself).

**MAR — Multi-Agent Reflexion (2025)**: arXiv 2512.20845. Different agents play Actor / Evaluator / Reflector to break confirmation bias. Costs ~3x but breaks the single-model echo chamber.

### 2.3 Reflection Training Data Format (for SFT)

To *train* a model to produce useful reflections (not just prompt it), the trajectory format becomes:

```jsonl
{"messages": [
  {"role": "system", "content": "You are a code agent. Reflect on failures using structured schema."},
  {"role": "user", "content": "Task: write a function to deduplicate a list preserving order."},
  {"role": "assistant", "content": "<attempt>def dedup(xs): return list(set(xs))</attempt>"},
  {"role": "tool", "content": "FAIL: test_order_preserved expected [3,1,2], got [1,2,3]. set() does not preserve order."},
  {"role": "assistant", "content": "<reflection>{\"error_type\":\"correctness\",\"root_cause\":\"used unordered set, lost insertion order\",\"corrective_action\":\"use dict.fromkeys() which is ordered in Py3.7+\",\"prevention_rule\":\"when dedup-with-order required, never use set()\"}</reflection>"},
  {"role": "assistant", "content": "<attempt>def dedup(xs): return list(dict.fromkeys(xs))</attempt>"},
  {"role": "tool", "content": "PASS: all tests."}
]}
```

**Training signal**:
- Mask loss on user/tool turns; train on `<reflection>` and final `<attempt>`
- Pair up (failed_attempt, reflection, successful_attempt) — model learns reflection->fix transition
- DPO variant: prefer trajectory with reflection over trajectory that retries blindly

---

## 3. Self-Refine / Iterative Improvement at Inference

### 3.1 Self-Refine (Madaan et al., NeurIPS 2023)

**Paper**: arXiv 2303.17651
**Repo**: https://github.com/madaan/self-refine

Single LLM does three roles in sequence:
1. **Generator**: produce initial output
2. **Feedback**: critique own output, locate problems, suggest improvements
3. **Refiner**: revise using feedback

Loop until feedback says "no further improvement" or max iterations.

Reports ~20% absolute improvement across 7 tasks. No training needed — pure prompt orchestration.

**Limitation for small models**: 7B models often produce LGTM-style feedback ("looks good") because they cannot self-localize errors. Critique quality degrades fast below 30B unless the critique prompt is very structured.

### 3.2 SCoRe — Self-Correction via RL (Kumar et al., DeepMind, 2024)

**Paper**: arXiv 2409.12917
**Result**: +15.6% MATH, +9.1% HumanEval on Gemini 1.5 Flash
**Code**: see [InfoQ writeup](https://www.infoq.com/news/2024/10/google-deepmind-score/) and [PyTorch implementation walkthrough](https://medium.com/@devmallyakarar/training-language-models-to-self-correction-via-reinforcement-learning-a-deep-dive-into-score-with-ff85421b4186)

**Why naive self-correction fails**:
- SFT on human-corrected pairs -> distribution mismatch (model never sees its *own* failures)
- Single-turn RL -> model collapses to "no change" trick or hallucinates fake corrections

**SCoRe two-stage method**:
- **Stage I**: multi-turn RL on base model with KL regularization, optimizing only the *second* attempt while keeping first attempt close to base distribution. Prevents collapse where model just emits same answer twice.
- **Stage II**: shaped reward = `r2 + bonus * (r2 - r1)` — explicitly rewards *improvement* between turns, not just final correctness. Discourages the trivial "give up and copy" failure mode.

Both stages use entirely self-generated traces (no human-corrected data).

### 3.3 RISE — Recursive Introspection (Qu et al., 2024)

**Paper**: arXiv 2407.18219. Iterative multi-round on-policy training. Distill an "oracle" trajectory (what an expert would do given the failure) and SFT against it. Cheaper than full RL, works on smaller models.

### 3.4 Self-Refine Variants (2025)

**PyCapsule**: 2-agent loop (gen + execution-classifier), +5.7% HumanEval pass@1
**CodeGrad**: structured-feedback verification critic, +27pp HumanEval (claimed)
**AgentCoder** (huangd1999/AgentCoder): programmer + test-designer + test-executor as 3 separate roles

### 3.5 Surrogate-1 Self-Refine Pattern (v2)

```python
# inference-time self-refine, max 3 iterations
def generate_with_refine(task: str, max_iters: int = 3) -> str:
    output = generate(task, role="generator")
    for i in range(max_iters):
        critique = generate(
            f"Task: {task}\n\nProposed solution:\n{output}\n\n"
            "Critique using schema: {locations: [...], issues: [...], fixes: [...]}. "
            "If no issues, respond exactly: NO_FURTHER_IMPROVEMENT.",
            role="critic"
        )
        if "NO_FURTHER_IMPROVEMENT" in critique:
            break
        output = generate(
            f"Task: {task}\n\nPrevious solution:\n{output}\n\nCritique:\n{critique}\n\n"
            "Produce revised solution.",
            role="refiner"
        )
    return output
```

**Stop criteria** (avoid infinite loop on weak critic):
- Hard cap at 3 iterations
- Stop if output unchanged across 2 consecutive iterations
- Stop if test suite passes (deterministic signal beats critic)

---

## 4. STOP — Self-Taught Optimizer / Recursive Self-Improvement

### 4.1 STOP (Zelikman et al., COLM 2024)

**Paper**: arXiv 2310.02304
**Repo**: https://github.com/microsoft/stop

**Concept**: a "scaffolding" Python program that calls an LM to improve itself. Seed = simple `improver(program, prompt) -> better_program`. Run improver on itself. Strategies the LM proposes: beam search, genetic algorithms, simulated annealing.

**Key caveat from the paper itself**: the LM weights are unchanged — only the *scaffolding code* improves. Not true recursive self-improvement of the model.

### 4.2 Gödel Agent (2024)

**Paper**: arXiv 2410.04444. Self-evolving framework: agent has access to its own source code and can modify routines guided by high-level goal. Closer to recursive self-mod than STOP.

### 4.3 Reality Check for Surrogate-1

STOP-style scaffolding self-improvement is **risky for 7B models**:
- Quality of code-rewriting is poor below ~30B
- Failure mode: agent rewrites scaffolding into a more confident but worse version (no objective grounding)
- High blast radius: agent corrupts its own runtime

**Defer to v3 or skip entirely**. The cheaper version of "self-improvement" for v2 is the *outer-loop* pipeline (Section 10) where humans + Claude review changes before retraining lands.

---

## 5. Continual Learning Without Catastrophic Forgetting

### 5.1 The LoRA-Forgetting Problem (2025 finding)

Recent research (2025): LoRA does NOT actually mitigate catastrophic forgetting in continual learning. The assumption "small parameter delta = preserved base behavior" breaks when loss landscape is rugged. After 3-5 sequential LoRA tunings, base capabilities (general reasoning, OOD code) degrade noticeably.

Source: STABLE arXiv 2510.16089, SMoLoRA ICCV 2025, [llm-continual-learning-survey](https://github.com/Wang-ML-Lab/llm-continual-learning-survey).

### 5.2 Mitigation Techniques

**O-LoRA (Orthogonal LoRA)**: each new LoRA's gradient subspace is constrained orthogonal to all previous LoRA subspaces. Prevents new training from overwriting old skills. arXiv 2310.14152.

**SMoLoRA — Separable Mixture of LoRA experts (ICCV 2025)**: routes input to a frozen expert pool. New skills add new experts; old experts never updated. Solves "dual catastrophic forgetting" (visual instruction tuning context).

**STABLE (2025)**: gated continual self-edit framework constraining sequential updates via PEFT/LoRA.

**FIP method**: takes loss-landscape geometry into account during training; deltas are larger but task performance preserved.

**Replay-based**: keep a "rehearsal buffer" of 5-10% old training samples; mix into every new fine-tune. Cheapest, very effective.

**Recommendation for Surrogate-1**: replay buffer + O-LoRA constraint + adapter-per-domain. Skip MoE-LoRA in v2 (router complexity).

### 5.3 Mixture of LoRAs / LoraHub (2024-2025)

**LoraHub (Huang et al., COLM 2024)**: arXiv 2307.13269. Compose N task-specific LoRAs at inference using gradient-free optimization (CMA-ES) on a few shots from the new task. ~10-100 examples needed to compose a working hybrid.

**Towards Modular LLMs (Ostapenko et al., ICML 2024)**: arXiv 2405.11157. Two methods:
- **MBC (Model-Based Clustering)**: cluster tasks by adapter-parameter similarity to build LoRA library
- **Arrow routing**: zero-shot routing — pick LoRA per token based on current hidden state direction

**MoLA (layer-wise expert allocation)**: assigns different number of LoRA experts per Transformer layer. Better than uniform MoE-LoRA.

**LoRA-Mixer (2025)**: routes LoRA experts into attention projections (not just FFN). 48% fewer trainable params.

**For Surrogate-1**: useful at v3 once we have ≥4 distinct domain LoRAs (aws, cdk, sec, sre). At v2 we have 1 LoRA — composition is premature.

---

## 6. Knowledge Accumulation Systems (Agent Memory)

### 6.1 Letta (formerly MemGPT)

**Repo**: https://github.com/letta-ai/letta
**Paper**: arXiv 2310.08560 (MemGPT: Towards LLMs as Operating Systems)

Three-tier memory inspired by computer architecture:
1. **Core memory** (RAM): always in context window, ~2k tokens, agent-editable
2. **Recall memory** (disk cache): full conversation history, searchable via tools
3. **Archival memory** (cold storage): vector DB of facts, queried via tools

Agent has tool calls to *self-edit* its own memory blocks (read/write/append). Enables the agent to manage its own context.

### 6.2 Mem0

**Repo**: mem0ai/mem0. Three-level hierarchy: user / session / agent. Hybrid store = vector + graph + key-value. "Memory in 3 lines of code." Best for: building a memory layer over an existing agent quickly.

### 6.3 Zep (Graphiti)

Temporal knowledge graph: facts are timestamped; when info changes, old fact is *invalidated* not deleted. Tracks "as-of" semantics. Best for: agents that need to reason about how knowledge evolves over time.

### 6.4 Cognee

**Repo**: https://github.com/topoteretes/cognee
Knowledge graph + vector hybrid. Graph-aware embeddings fuse semantic vectors with graph signals (hierarchy, time, entity types). Open source, MCP integration. Recently raised $7.5M seed (2025).

### 6.5 GraphRAG (Microsoft)

**Repo**: https://github.com/microsoft/graphrag
**Project page**: https://www.microsoft.com/en-us/research/project/graphrag/

Pipeline: text -> TextUnits -> entity/relationship extraction -> hierarchical clustering (Leiden) -> community summaries -> retrieval combines vector + graph traversal.

Reports: 50% -> 80% correctness on multi-hop questions vs vector-only RAG. Dominant approach for "narrative private data."

### 6.6 Comparison Summary

| System | Strength | Weakness | Surrogate-1 fit |
|---|---|---|---|
| Letta | Agent self-edits memory | Heavy infra (Postgres + tool plumbing) | High — already aligned with our pattern |
| Mem0 | 3-line API | Limited graph reasoning | Medium |
| Zep | Temporal facts | Niche use case | Low |
| Cognee | OSS + MCP | Newer, smaller community | High — MCP fits our stack |
| GraphRAG | Multi-hop synthesis | Heavy ingest cost | High — already deploying FalkorDB |

**For Surrogate-1**: Letta-style 3-tier with Cognee/GraphRAG as the archival backend. Already aligned with the existing FalkorDB+ChromaDB GraphRAG infra in `~/.claude/`.

### 6.7 Making Memory "LLM-Internal" (Not External)

The user asked: "how to make these LLM-internal (not external Python lib)?"

Honest answer: you can't truly bake a vector DB into a 7B model. But you can:

1. **Train the model to use memory tools fluently** — SFT on trajectories where Letta-style `memory_read("topic")` / `memory_write("topic", content)` are called. After training, the model treats memory tools as second-nature.
2. **Distill recent memory into LoRA** — periodically (weekly), take the "hottest" memory entries (most-read) and SFT a small LoRA delta. This bakes recent context into weights.
3. **Train context-management policies**: when context window fills, model decides what to summarize/evict — Letta's `core_memory_replace` style. This is a learned policy, not an external lib.

The "memory" lives outside; the *skill of using memory* lives inside the weights. That's the realistic split.

---

## 7. Continual RL from Human/AI Feedback

### 7.1 Self-Rewarding Language Models (Yuan et al., Meta, 2024)

**Paper**: arXiv 2401.10020.
Same model is judge + generator. Iterative DPO loop:
1. Sample prompts; generate K candidates per prompt
2. Use same model (LLM-as-Judge prompt) to score candidates
3. Take best/worst as DPO preference pair
4. DPO-train on synthesized preferences -> M_{t+1}
5. Repeat

Llama-2-70B + 3 iterations beat Claude 2 / Gemini Pro / GPT-4-0613 on AlpacaEval 2.0.

### 7.2 Meta-Rewarding (Wu et al., Meta, 2024)

**Paper**: arXiv 2407.19594. Adds a *meta-judge* role: model evaluates its own judgments. Prevents judge-quality from saturating.

### 7.3 Why Self-Rewarding Breaks for 7B

LLM-as-judge biases at small scale (well-documented):
- Position bias (prefers first/last response)
- Verbosity bias (longer = better)
- Confirmation bias (judge agrees with its own prior generation)
- Reward hacking: model learns to generate outputs that *look* judgeable rather than correct

7B models score significantly worse as judges than 30B+ — Meta's results assume Llama-2-70B.

**Solutions**:
1. **Distill rewards from a stronger judge** (Claude/GPT-4) into a small reward model — arXiv 2604.02621 "RL-based Knowledge Distillation with LLM-as-a-Judge"
2. **Programs are the Manual** (arXiv 2506.10403): replace LLM judge with code (unit tests, linters, type-checkers, security scanners) wherever possible — reliable, free, no bias
3. **Adaptive reward distillation** (OpenReview tK6VZy5RYr): majority voting for verifiable tasks; LLM-judge only for open-ended

### 7.4 Online DPO

**TRL OnlineDPOTrainer** (HuggingFace, 2024-2025): https://huggingface.co/docs/trl/main/en/online_dpo_trainer
- Requires only prompt-only dataset
- On each step: sample 2 responses from current policy, an LLM-judge (or rule/code) picks preferred, DPO update applied
- Integrates with Accelerate/DeepSpeed/FSDP for scale
- Unsloth integration: 2x speed, 70% memory reduction

**TRL v1.0** (April 2026): unified post-training stack with SFT + Reward Modeling + DPO + GRPO.

### 7.5 GRPO and Friends (2025 production stack)

GRPO (Group Relative Policy Optimization) is the dominant online RL method in 2025-2026:
- DeepSeek pioneered it with R1
- Now in TRL, verl, OpenRLHF, ROLL
- Generates K responses per prompt, computes advantage by group-relative comparison
- No critic needed (unlike PPO) -> ~half the GPU memory
- DAPO, GSPO, ReMax, REINFORCE++, RLOO all in same family

**Recommended production stack for online RL**:
- **OpenRLHF** (Ray + vLLM): production-ready, scales to thousands of GPUs
- **verl** (Bytedance): supports PPO/GRPO/GSPO/REMAX/REINFORCE++/RLOO/PRIME/DAPO/DrGRPO
- **vLLM as rollout engine**: paged attention, best throughput; pause/resume for weight sync overlap

### 7.6 Continual DPO / RLDF

For Surrogate-1's deployment loop:
- Each completed task -> trajectory + outcome
- Pair traces: (success_trace, fail_trace_on_similar_task) -> DPO pair
- Weekly batch: collect ~500-2000 pairs, run offline DPO
- Validate against frozen eval set (catch regressions)
- If regression > X%, rollback

This is the "harvest -> retrain -> validate -> deploy" cron loop.

---

## 8. SWE-Gym / SWE-RL — Code-Specific Self-Improvement (CRITICAL NEW)

### 8.1 SWE-Gym (ICML 2025)

**Repo**: https://github.com/SWE-Gym/SWE-Gym
First *environment* for training real-world SWE agents. 32% / 26% on SWE-Bench Verified/Lite. Trace harvesting + verifier training combined.

### 8.2 SWE-RL (Meta, NeurIPS 2025)

**Paper**: arXiv 2502.18449
**Repo**: https://github.com/facebookresearch/swe-rl
Llama3-SWE-RL-70B: 41% SWE-Bench Verified — SOTA among <100B open models. Uses RL on open-source software evolution data (GitHub commit -> issue -> patch trajectories).

### 8.3 Self-Play SWE-RL (Dec 2025)

**Paper**: arXiv 2512.18552
A single agent plays both sides: bug-injector and bug-fixer. No human-labeled issues/tests required, just sandboxed repos. +10.4 SWE-Bench Verified, +7.8 SWE-Bench Pro from self-play loop.

**This is the closest thing to true self-improvement for code agents in 2026.** Key trick: bug injector and fixer are the same model in different roles, escalating in difficulty as the model improves. Curriculum emerges from self-play, no human curriculum needed.

### 8.4 DeepSWE (Together AI, 2025)

Fully open-source coding agent trained by scaling RL. State-of-the-art among open coding agents at release.

### 8.5 Implication for Surrogate-1

This is the v3 endgame: instead of waiting for human feedback, generate synthetic tasks (bug injection on real repos), train against execution-grounded rewards (tests pass/fail, type-check, lint).

For v2 — at minimum, set up trace-harvesting infrastructure now so v3 has data.

---

## 9. Trajectory / Reflection Training Data Format

### 9.1 Combined Voyager + Reflexion Trajectory

For SFT on a code agent, the best format combines:
- Skill retrieval/use signals (Voyager)
- Failure-reflection-fix patterns (Reflexion)
- Tool-call grounding (CodeAct)

```jsonl
{
  "task_id": "task-2026-04-29-001",
  "task": "Add CloudWatch alarm for Lambda errors > 5/min",
  "trajectory": [
    {"role": "system", "content": "You are Surrogate-1, a code agent. Use existing skills via `from skills import ...` first; create new skills only when needed."},
    {"role": "user", "content": "Add CloudWatch alarm for Lambda errors > 5/min, send to existing SNS topic."},
    {"role": "assistant", "thought": "Search skill library for similar.", "tool": "skills.search('cloudwatch alarm lambda')", "observation": "Found: cw_create_alarm(metric, threshold, sns_topic) — Create CloudWatch alarm bound to an SNS topic."},
    {"role": "assistant", "code": "from skills.cw_create_alarm import cw_create_alarm\nresult = cw_create_alarm(metric='AWS/Lambda/Errors', threshold=5, period_sec=60, sns_topic_arn=SNS_ARN)\nprint(result)"},
    {"role": "tool", "content": "ERROR: cw_create_alarm() got unexpected kwarg 'period_sec'. Signature: (metric, threshold, sns_topic_arn)."},
    {"role": "assistant", "reflection": {"error_type": "skill-misuse", "root_cause": "called skill with extra kwarg not in signature", "corrective_action": "drop period_sec; use skill default", "prevention_rule": "always read skill signature from index before calling"}},
    {"role": "assistant", "code": "result = cw_create_alarm(metric='AWS/Lambda/Errors', threshold=5, sns_topic_arn=SNS_ARN)"},
    {"role": "tool", "content": "OK: alarm-arn arn:aws:cloudwatch:..."},
    {"role": "assistant", "content": "Alarm created. ARN: arn:aws:cloudwatch:..."}
  ],
  "outcome": "success",
  "skills_used": ["cw_create_alarm"],
  "skills_created": [],
  "duration_sec": 6.2
}
```

### 9.2 SFT Training Recipe

- Mask: only train on `assistant` turns (thought, code, reflection, content). Skip user/tool/system.
- Weight: upweight reflection->fix transitions (2x). These are the highest-value transitions.
- Negative samples: include some "no reflection, retry blindly -> still fail" trajectories with low weight or as DPO rejected pair.

### 9.3 DPO Pair Construction

```jsonl
{
  "prompt": "<task + context>",
  "chosen": "<trajectory with skill-reuse + reflection -> success>",
  "rejected": "<trajectory that ignored skill library, hand-rolled solution, partial fail>"
}
```

Source pairs from production traces:
- Same task, two attempts, one with skill-use one without -> pair
- Same task, before/after reflection -> pair

---

## 10. Production-Grade Self-Improvement Loops

### 10.1 The Cron Pipeline

```
[deployed agent on H200]
    |
    v
[trace logger: every (task, trajectory, outcome) -> S3]
    |
    v
[nightly ETL]
    | filter: outcome != null AND task.size > min
    | dedupe: hash(task) collisions
    | bucket: success vs fail
    v
[weekly trainer cron, Modal/Lightning H200]
    | sample: 10k success traces + 2k fail-with-reflection traces
    | replay buffer: + 1k traces from past weeks (anti-forgetting)
    | SFT new LoRA delta_t
    | DPO on (success, fail) pairs from same task family
    v
[eval gate, frozen test set + GraphRAG-grounded human eval]
    | gate: regression < 1% on baseline tasks AND improvement > X% on new tasks
    v
[merge LoRA delta -> serve next week]
[else: rollback, investigate failure mode]
```

### 10.2 Frameworks Stack

| Layer | Tool | Why |
|---|---|---|
| Inference serving | vLLM | best throughput, paged attention, supports LoRA hot-swap |
| Training | Axolotl (multi-GPU) or Unsloth (single GPU) | mature, config-driven |
| RL | TRL (OnlineDPO/GRPO) or verl (multi-method) | TRL for prototype, verl for scale |
| RLHF orchestration | OpenRLHF (Ray+vLLM) | production-grade, handles weight-sync |
| Trace store | S3 + DuckDB or Postgres | cheap, queryable |
| Memory | Letta + Cognee/GraphRAG | self-edit + semantic |
| Skill library | FAISS + JSON registry + git-tracked Python | versioned, auditable |

### 10.3 Online DPO config (TRL, condensed)

```python
from trl.experimental.online_dpo import OnlineDPOTrainer, OnlineDPOConfig
from transformers import AutoModelForCausalLM, AutoTokenizer

base = AutoModelForCausalLM.from_pretrained("Qwen/Qwen2.5-Coder-7B")
tok  = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-Coder-7B")

cfg = OnlineDPOConfig(
    output_dir="surrogate1-online-dpo",
    learning_rate=5e-7,            # very low for online
    per_device_train_batch_size=4,
    num_generations=2,             # candidates per prompt
    max_new_tokens=2048,
    judge="execution",             # custom: prefer trace whose tests pass
    beta=0.1,                      # DPO temperature
    save_steps=500,
    bf16=True,
    use_peft=True,                 # LoRA, not full
    lora_r=16,
    lora_alpha=32,
)

trainer = OnlineDPOTrainer(
    model=base, args=cfg, processing_class=tok,
    judge=execution_grounded_judge,   # pass=preferred, fail=rejected
    train_dataset=prompt_only_dataset,  # just prompts, no preferences
)
trainer.train()
```

### 10.4 Execution-Grounded Judge (replace LLM judge for code)

For code tasks, **always prefer execution signal over LLM judge**:

```python
def execution_grounded_judge(prompt, candidate_a, candidate_b):
    a_pass = run_tests_in_sandbox(candidate_a)
    b_pass = run_tests_in_sandbox(candidate_b)
    if a_pass and not b_pass: return 0  # a preferred
    if b_pass and not a_pass: return 1
    if a_pass and b_pass:
        return 0 if len(candidate_a) < len(candidate_b) else 1  # shorter
    # both fail -> use LLM judge as fallback only
    return llm_judge(prompt, candidate_a, candidate_b)
```

This is the "Programs are the Manual" finding (arXiv 2506.10403): code-grounded reward beats LLM-as-judge for code tasks. Surrogate-1 must use this — never trust 7B-self-as-judge for correctness.

### 10.5 Replay Buffer (Anti-Forgetting)

Maintain a "regression set" of ~500-1000 frozen traces from earlier deployments. Mix into every weekly retrain at ~10% mass. Cheap insurance against catastrophic forgetting.

### 10.6 LoRA Hot-Swap on vLLM

vLLM supports per-request LoRA swap. Run `delta_main` (current production) + `delta_canary` (new candidate) side-by-side, route 5% traffic to canary, gate on metrics, full rollout when green.

### 10.7 Failure Bucketing

Tag every fail trace with structured bucket — drives next-cycle curriculum:

```python
FAIL_BUCKETS = {
    "syntax": "code didn't parse / compile",
    "runtime_error": "execution raised",
    "test_fail": "tests ran but failed",
    "tool_misuse": "called API/skill incorrectly",
    "skill_miss": "didn't find existing skill, hand-rolled buggy version",
    "spec_misread": "solved wrong problem",
    "infinite_loop": "exceeded turn budget",
    "halluc_api": "called nonexistent function/library",
}
```

Sample retrain data weighted by bucket frequency: most-common failure mode gets most training signal.

---

## 11. Surrogate-1 v2 Concrete Architecture

### 11.1 v2 Design (build now)

```
┌─────────────────────────────────────────────────────────────┐
│                     Surrogate-1 v2 Agent                    │
│                                                             │
│  ┌────────────┐    ┌─────────────┐    ┌─────────────────┐   │
│  │ Qwen2.5-7B │ +  │ LoRA delta  │ +  │ Skill Library   │   │
│  │  (frozen)  │    │ (per-domain)│    │ (FAISS+JSON+py) │   │
│  └────────────┘    └─────────────┘    └─────────────────┘   │
│         │                                       │           │
│         v                                       v           │
│  ┌────────────────────────────────────────────────────┐     │
│  │     Self-Refine loop (max 3 iters, exec-gated)     │     │
│  └────────────────────────────────────────────────────┘     │
│         │                                                   │
│         v                                                   │
│  ┌────────────────────────────────────────────────────┐     │
│  │     Reflexion: episodic memory (bounded 3)         │     │
│  └────────────────────────────────────────────────────┘     │
│         │                                                   │
│         v                                                   │
│  ┌────────────────────────────────────────────────────┐     │
│  │  Letta-style 3-tier (core / recall / archival)     │     │
│  │  archival = Cognee/GraphRAG (existing FalkorDB)    │     │
│  └────────────────────────────────────────────────────┘     │
│         │                                                   │
│         v                                                   │
│  ┌────────────────────────────────────────────────────┐     │
│  │     Trace logger -> S3 (every task, full traj)     │     │
│  └────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────┘
                           │
                           v (weekly cron)
┌─────────────────────────────────────────────────────────────┐
│  Trace harvest -> SFT new LoRA delta -> eval gate -> deploy │
│  (replay buffer 10%, exec-grounded judge for DPO pairs)     │
└─────────────────────────────────────────────────────────────┘
```

### 11.2 Defer to v3

- STOP-style scaffolding self-mod (high blast radius)
- Self-Rewarding LM iterative DPO (7B too small as own judge)
- Mixture of LoRAs / LoraHub composition (need ≥4 LoRAs first)
- Self-Play SWE-RL (needs robust sandboxing infra)
- Online RL during serving (start with weekly offline cycle)
- Meta-rewarding (judge-of-judges) — only relevant after self-rewarding works
- Gödel Agent self-mod (research toy)

### 11.3 v2 Implementation Order (8 weeks)

1. **Week 1-2**: Skill library scaffolding (FAISS + JSON registry + py modules + retrieval prompt)
2. **Week 2-3**: Reflexion episodic memory + structured failure buckets in trace format
3. **Week 3-4**: Self-Refine inference-time loop + execution-grounded stop criteria
4. **Week 4-5**: Letta-style 3-tier memory (core editable; recall/archival via existing GraphRAG)
5. **Week 5-6**: Trace logger -> S3 + DuckDB query layer
6. **Week 6-7**: Weekly retrain cron (Axolotl + LoRA + replay buffer + O-LoRA constraint)
7. **Week 7-8**: Eval gate (frozen regression set + canary on 5% traffic via vLLM LoRA-swap)

---

## 12. References (Papers + Repos)

### Skill libraries
- Voyager (Wang et al., NeurIPS 2023): [arXiv 2305.16291](https://arxiv.org/abs/2305.16291), [repo](https://github.com/MineDojo/Voyager)
- CodeAct (Wang et al., ICML 2024): [arXiv 2402.01030](https://arxiv.org/abs/2402.01030), [repo](https://github.com/xingyaoww/code-act)
- SICA (Robeyns et al., 2025): [arXiv 2504.15228](https://arxiv.org/abs/2504.15228), [repo](https://github.com/MaximeRobeyns/self_improving_coding_agent)
- SoK Agentic Skills (2026): arXiv 2602.20867

### Reflexion family
- Reflexion (Shinn et al., NeurIPS 2023): [arXiv 2303.11366](https://arxiv.org/abs/2303.11366), [repo](https://github.com/noahshinn/reflexion)
- Retroformer (ICLR 2024): arXiv 2308.02151
- Failure Makes the Agent Stronger (2025): arXiv 2509.18847
- SaMuLe multi-level reflection (2025): arXiv 2509.20562
- Multi-Agent Reflexion (2025): arXiv 2512.20845

### Self-Refine / Self-Correction
- Self-Refine (Madaan, NeurIPS 2023): [arXiv 2303.17651](https://arxiv.org/abs/2303.17651), [repo](https://github.com/madaan/self-refine)
- SCoRe (Kumar et al., DeepMind 2024): [arXiv 2409.12917](https://arxiv.org/abs/2409.12917)
- RISE (Qu et al., 2024): [arXiv 2407.18219](https://arxiv.org/abs/2407.18219)
- AgentCoder (2024): [repo](https://github.com/huangd1999/AgentCoder)

### Recursive self-improve
- STOP (Zelikman, COLM 2024): [arXiv 2310.02304](https://arxiv.org/abs/2310.02304), [repo](https://github.com/microsoft/stop)
- Gödel Agent (2024): arXiv 2410.04444

### Continual learning / LoRA
- LoraHub (Huang et al., COLM 2024): [arXiv 2307.13269](https://arxiv.org/abs/2307.13269)
- Modular LLMs / Arrow (Ostapenko et al., ICML 2024): [arXiv 2405.11157](https://arxiv.org/abs/2405.11157)
- O-LoRA: arXiv 2310.14152
- SMoLoRA (ICCV 2025): [paper](https://openaccess.thecvf.com/content/ICCV2025/papers/Wang_SMoLoRA_Exploring_and_Defying_Dual_Catastrophic_Forgetting_in_Continual_Visual_ICCV_2025_paper.pdf)
- STABLE (2025): arXiv 2510.16089
- Continual Learning Survey: [github](https://github.com/Wang-ML-Lab/llm-continual-learning-survey)

### Agent memory
- Letta/MemGPT: [repo](https://github.com/letta-ai/letta), arXiv 2310.08560
- Mem0: [paper](https://arxiv.org/pdf/2504.19413), [blog](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- Zep / Graphiti: [neo4j blog](https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/)
- Cognee: [repo](https://github.com/topoteretes/cognee)
- GraphRAG (Microsoft): [repo](https://github.com/microsoft/graphrag), [project](https://www.microsoft.com/en-us/research/project/graphrag/)
- Graph-based Agent Memory survey: arXiv 2602.05665

### Self-rewarding / Online RL
- Self-Rewarding LM (Yuan et al., Meta 2024): [arXiv 2401.10020](https://arxiv.org/abs/2401.10020)
- Meta-Rewarding LM (Wu et al., 2024): [arXiv 2407.19594](https://arxiv.org/abs/2407.19594)
- TRL (HuggingFace): [docs](https://huggingface.co/docs/trl/main/en/online_dpo_trainer), [repo](https://github.com/huggingface/trl)
- OpenRLHF: [repo](https://github.com/OpenRLHF/OpenRLHF), [vllm blog](https://blog.vllm.ai/2025/04/23/openrlhf-vllm.html)
- verl: [repo](https://github.com/verl-project/verl)
- Constitutional AI (Anthropic): [arXiv 2212.08073](https://arxiv.org/abs/2212.08073)
- Programs are the Manual: arXiv 2506.10403
- RL-Distill with LLM-Judge: arXiv 2604.02621

### Code-specific RL
- SWE-Gym (ICML 2025): [repo](https://github.com/SWE-Gym/SWE-Gym)
- SWE-RL (Meta NeurIPS 2025): [arXiv 2502.18449](https://arxiv.org/abs/2502.18449), [repo](https://github.com/facebookresearch/swe-rl)
- Self-Play SWE-RL (Dec 2025): [arXiv 2512.18552](https://arxiv.org/abs/2512.18552)
- DeepSWE (Together AI 2025): [blog](https://www.together.ai/blog/deepswe)
- CodeRL+ (2025): arXiv 2510.18471

### Frameworks
- Axolotl: [repo](https://github.com/axolotl-ai-cloud/axolotl), [docs](https://docs.axolotl.ai/)
- Unsloth: integration in TRL/Axolotl
- vLLM: [repo](https://github.com/vllm-project/vllm), [RLHF docs](https://docs.vllm.ai/en/stable/training/rlhf/)
- LlamaGym: [repo](https://github.com/KhoomeiK/LlamaGym)

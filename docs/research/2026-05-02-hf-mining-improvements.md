---
date: 2026-05-02
topic: HuggingFace ecosystem mining for axentx + surrogate-1 + hermes improvements
source: HF API trending sort, downloads, likes
tags: [research, datasets, models, spaces, surrogate-1, hermes, axentx]
---

# HF mining — top picks to improve our 3 stacks

Mined HF API on 2026-05-02 (sort=likes/downloads, top 30 per category).
Pruned to items NOT already harvested by V19 trainer + ranked by impact.

---

## 🥇 Datasets to add to surrogate-1 training (highest ROI)

### Tier S — Hermes/agent themed (perfect fit)

| Dataset | ⭐ | ⬇ | Why we should add |
|---|---:|---:|---|
| `lambda/hermes-agent-reasoning-traces` | 272 | 8,681 | Hermes-themed agent reasoning traces — direct namesake fit |
| `interstellarninja/hermes_reasoning_tool_use` | 164 | 1,054 | Multi-turn tool-use traces from Hermes ecosystem |
| `interstellarninja/tool-use-multiturn-reasoning` | 30 | 250 | Same author family, multi-turn refinement |

### Tier A — Big agent corpora

| Dataset | ⭐ | ⬇ | Why |
|---|---:|---:|---|
| `Agent-Ark/Toucan-1.5M` | 211 | 4,317 | **1.5M agent traces** — biggest single pool we don't have |
| `nvidia/Nemotron-Agentic-v1` | 166 | 3,873 | NVIDIA's curated agentic RL data |
| `agentica-org/DeepScaleR-Preview-Dataset` | 198 | 12,023 | DeepScaleR RL preference data — DPO/GRPO-ready |
| `nvidia/Nemotron-RL-Agentic-Conversational-Tool-Use-Pivot-v1` | 17 | 1,076 | Multi-turn tool-use w/ pivoting — rare data class |
| `allenai/Dolci-Instruct-SFT-Tool-Use` | 16 | 545 | Allen AI tool-use SFT |

### Tier A — Reasoning (frontier-distilled)

| Dataset | ⭐ | ⬇ | Why |
|---|---:|---:|---|
| `FreedomIntelligence/medical-o1-reasoning-SFT` | 1088 | 7,694 | **Top-rated reasoning dataset** o1-style traces |
| `nohurry/Opus-4.6-Reasoning-3000x-filtered` | 568 | 8,527 | **Claude 4.6 Opus distilled** — 3000 tasks × N attempts (premium) |
| `Crownelius/Opus-4.6-Reasoning-3300x` | 293 | 4,047 | Same theme, larger sample |
| `TeichAI/claude-4.5-opus-high-reasoning-250x` | 387 | 2,980 | Claude 4.5 high-reasoning |
| `Alibaba-Apsara/Superior-Reasoning-SFT-gpt-oss-120b` | 348 | 2,922 | Alibaba's GPT-OSS-120B-distilled reasoning |
| `microsoft/rStar-Coder` | 241 | 6,170 | Microsoft rStar reasoning-for-code |
| `m-a-p/CodeFeedback-Filtered-Instruction` | 194 | 25,373 | Filtered Code-Feedback (we have unfiltered) |

### Tier B — Foundation / pretraining-grade

| Dataset | ⭐ | ⬇ | Why |
|---|---:|---:|---|
| `HuggingFaceFW/fineweb` | 2775 | 677,412 | THE foundation web corpus |
| `m-a-p/FineFineWeb` | 128 | 419,679 | Filtered FineWeb (smaller, higher quality) |
| `LLM360/TxT360` | 253 | 936,415 | LLM360's trillion-token blend |
| `HuggingFaceFW/finephrase` | 107 | 493,889 | Phrasal version for instruction-following |
| `fineinstructions/fineinstructions_nemotron` | 7 | 920,683 | NVIDIA Nemotron-distilled instructions, **1B+ pairs** |

### Already harvested (V19 trainer log confirms)
- `microsoft/orca-agentinstruct-1M-v1` ✅ (40k merged)
- `m-a-p/Code-Feedback` ✅ (8k merged)
- `R2E-Gym` ✅ (6.4k)
- `Tulu3-IF-Persona` ✅ (16k)
- `OpenR1-Math-220k` ✅ (16k)
- `Bespoke-Stratos` ✅ (10k)

---

## 🥈 Models worth evaluating (alternative bases / teachers)

### Frontier MoE (compute-cheap)
| Model | ⭐ | Why |
|---|---:|---|
| `Qwen/Qwen3.6-35B-A3B` | 1550 | **Newest Qwen3.6** (2026-04 release), 35B params / 3B active — fits T4×2 budget when using LoRA |
| `Qwen/Qwen3.5-35B-A3B` | 1412 | Stable Qwen3.5 MoE — already in trainer alias list |
| `Qwen/Qwen3.5-397B-A17B` | 1473 | Top-end MoE, too big for T4 but candidate for H100 phase |

### Already-distilled teachers (skip our own distillation pass)
| Model | ⭐ | Why |
|---|---:|---|
| `Jackrong/Qwen3.5-27B-Claude-4.6-Opus-Reasoning-Distilled` | 2809 | **Already distilled from Claude 4.6 Opus** — use as teacher in next phase |

### Alternate bases
| Model | ⭐ | Why |
|---|---:|---|
| `google/gemma-4-31B-it` | 2464 | Top trending Gemma, alternate base if Qwen tokenizer issues |
| `meta-llama/Llama-3.3-70B-Instruct` | 2748 | Already used as fallback in our 11-LLM chain via Groq |
| `deepseek-ai/DeepSeek-V4-Pro` | 3363 | Frontier — too big to fine-tune locally, use via API |

---

## 🥉 Spaces — tools to study or self-host

### Eval frameworks (axentx benchmarks)
| Space | ⭐ | What |
|---|---:|---|
| `open-llm-leaderboard/open_llm_leaderboard` | 13977 | Standard LLM eval — fork pattern for our axentx-eval-50 |
| `mteb/leaderboard` | 7335 | Embedding eval (when we ship embeddings adapter) |
| `lmarena-ai/arena-leaderboard` | 4869 | Chatbot Arena (head-to-head) |
| `galileo-ai/agent-leaderboard` | 448 | **Agent-specific eval** — directly relevant for axentx |
| `gorilla-llm/berkeley-function-calling-leaderboard` | 124 | Function-calling bench (BFCL) — should add to bench-v1-vs-vN |
| `Nexusflow/Nexus_Function_Calling_Leaderboard` | 95 | Alt function-calling bench |
| `OpenEvals/evaluation-guidebook` | 312 | Eval best-practices doc |

### Training playbooks (must-read)
| Space | ⭐ | What |
|---|---:|---|
| `nanotron/ultrascale-playbook` | 3819 | **Large-scale training tricks** — read before V20 |
| `HuggingFaceTB/smol-training-playbook` | 3136 | **Small-scale fine-tune tricks** — directly applicable to our T4×2 |

### Agent tooling
| Space | ⭐ | What |
|---|---:|---|
| `smolagents/computer-agent` | 984 | Reference computer-use agent — pattern for axentx |
| `akhaliq/anycoder` | 3237 | Multi-language coding agent |
| `agents-course/First_agent_template` | 675 | Starter template for agent development |

---

## 🎯 Concrete actions (priority order)

### Action 1 — Add Tier S+A datasets to Hermes registry (impact: +30-50% effective training data)

Append these IDs to `dynamic-datasets.json` on Hermes Space (or D1 once migrated):
```
lambda/hermes-agent-reasoning-traces           cap=100000  schema=messages
interstellarninja/hermes_reasoning_tool_use    cap=50000   schema=messages
Agent-Ark/Toucan-1.5M                          cap=300000  schema=messages
nvidia/Nemotron-Agentic-v1                     cap=100000  schema=messages
agentica-org/DeepScaleR-Preview-Dataset        cap=100000  schema=messages
FreedomIntelligence/medical-o1-reasoning-SFT   cap=200000  schema=instr-resp
nohurry/Opus-4.6-Reasoning-3000x-filtered      cap=100000  schema=messages
Alibaba-Apsara/Superior-Reasoning-SFT-gpt-oss-120b  cap=100000  schema=instr-resp
microsoft/rStar-Coder                          cap=100000  schema=messages
m-a-p/CodeFeedback-Filtered-Instruction        cap=200000  schema=instr-resp
m-a-p/FineFineWeb                              cap=500000  schema=text
```

### Action 2 — Read playbooks before V20 design

- [ ] `nanotron/ultrascale-playbook` → harvest tricks for V20 H100 phase
- [ ] `HuggingFaceTB/smol-training-playbook` → squeeze T4×2 V19 better

### Action 3 — Add 2 evals to bench-v1-vs-vN.sh

- [ ] BFCL function-calling eval (gorilla-llm leaderboard format)
- [ ] Agent leaderboard eval (galileo-ai)

### Action 4 — Consider distillation chain

After V19 #9 finishes, evaluate vs `Jackrong/Qwen3.5-27B-Claude-4.6-Opus-Reasoning-Distilled` as a teacher. If teacher >> student, run distillation pass. (Avoids needing to call Claude API ourselves for distillation — use this pre-distilled model.)

---

## Sources
- HF API: `/api/datasets?sort={likes,downloads}&direction=-1`
- HF API: `/api/models?search=<term>&sort=likes`
- HF API: `/api/spaces?sort=likes&direction=-1`
- Snapshot time: 2026-05-02T05:00Z

Limits: HF rate-limits the tree/files endpoints (we hit 429/500 on big repos);
search/sort endpoints are fine. The `usedStorage` field is partial — for real
file-size totals use `huggingface_hub.dataset_info(files_metadata=True)` which
sums actual blob sizes (LFS-aware).

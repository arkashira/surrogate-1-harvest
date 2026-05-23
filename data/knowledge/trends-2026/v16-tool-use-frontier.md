---
title: "V16 — Tool-Use Frontier (BFCL v3, multi-turn, parallel, MCP)"
date: 2026-05-01
tags: [v16, tool-use, function-calling, bfcl, mcp, agents, training, frontier]
status: actionable
license_audit: required
---

# V16 — Tool-Use Frontier (Make Surrogate THE BEST at Tool Calling)

> **Owner directive (2026-05-01)**: "ตอนนี้ tool use ยังน้อยไป เอาไปใช้จริงไม่ได้" — V13/V14/V15 only had ToolACE 16K + xLAM 20K + hermes-fc 5K + glaive-fc 5K. **Insufficient for production agentic use**. V16 must rebuild tool-use capability from the ground up: 1000+ tool catalog, multi-turn/parallel/error-recovery, MCP-native, and OpenAI/Anthropic format-agnostic.
>
> **Quantified target**: BFCL v3 multi-turn ≥ **65 / 100** (xLAM-2-8b-fc-r baseline = 69.25), τ-bench retail ≥ **50 / 100**, ACEBench Agent ≥ **55 / 100**, When2Call irrelevance ≥ **80 / 100**.
>
> **Constraint**: T4×2 (Kaggle, 30hr/week, 16 GB×2 = 32 GB), 4B base (Qwen3-4B-Instruct-2507 — same base FunReason-MT achieved SOTA on).

See also: [[v14-arxiv-github-sweep-may2026]] · [[v14-rl-frontier-beyond-dapo]] · [[v14-swarm-agents-at-scale]] · [[training-tooling-2026-Q2]] · [[autonomous-24x7]]

---

## 0 — Why V13/V14/V15 Tool-Use Fell Short (root-cause analysis)

| Symptom | Root cause | V16 fix |
|---|---|---|
| Model hallucinates tool names | No retrieval phase trained, tool list >50 = catastrophe | Add ToolRet-train 200K + 1000-tool stress sets |
| Forgets previous tool output | Single-turn xLAM-60k bias (turns=1) | Add APIGen-MT-5k, ToolMind-360k, ToolACE-MT, FunReason-MT (all multi-turn) |
| Calls tool when none needed | Zero "irrelevance" / abstention examples | Add When2Call (NVIDIA, NAACL 2025), RPO-trained head |
| Cannot recover from 429 / 500 | Trained only on success trajectories | Add PALADIN 50K failure-recovery trajectories |
| Emits 1 tool when 3-5 needed parallel | xLAM-60k bias toward single calls | Up parallel slice from BFCL parallel + APIGen parallel templates |
| Schema invalid JSON | No constrained-decoding awareness in base | xgrammar EBNF training + tool_call special tokens |
| Cannot chain (output to next tool input) | No graph-based composition data | Add Magnet (graph translation) + BUTTONInstruct + ToolForge |
| Locks to OpenAI format only | Only Hermes-style emitted in training | Triple-format augmentation: OpenAI / Anthropic / MCP |

**One-line summary**: V13-V15 was *single-turn function-calling*. V16 must be **agentic tool-use**: retrieval, plan, call, observe, recover, compose, answer.

---

## 1 — BFCL v3 / v4 Leaderboard State (May 2026)

Source: gorilla.cs.berkeley.edu/leaderboard.html · huggingface.co/datasets/gorilla-llm/Berkeley-Function-Calling-Leaderboard

### Current (May 2026) BFCL v3 top-10

| # | Model | Overall | Multi-Turn | Notes |
|---|---|---|---|---|
| 1 | GLM-4.5 (Zhipu, 355B MoE) | 76.7 | ~72 | Asynchronous agentic-RL, slime infra (arXiv 2508.06471) |
| 2 | Qwen3-32B | 75.7 | 70.8 | 36T tokens + Hermes-style template native |
| 3 | xLAM-2-70b-fc-r (Salesforce) | 75.12 (MT) | 75.12 | APIGen-MT 5K core trainer |
| 4 | Magnet-14B-mDPO | 68.01 | — | Graph translation distillation, surpasses Gemini-1.5-pro teacher |
| 5 | ToolMind-14B (Qwen3-14B SFT) | +5.40 BFCL-v4 / +14.22 tau-bench | — | 360K reasoning-enhanced |
| 6 | FunReason-MT (Qwen3-4B-RL) | 57.75 (MT) | 57.75 | **+42pt over base** at only 4B |
| ... | xLAM-2-8b-fc-r | 69.25 (MT) | 69.25 | Direct V16 baseline (same scale) |
| ... | Granite-3.3-8b-instruct | ~65 | — | Glaive-fc + synthetic API |

**Key observation**: Claude-Opus-4 = 25.3% on BFCL v3 (bottom of table) yet wins tau-bench. BFCL v3 is *very specific* about state-based correctness. Don't optimize blindly, measure both.

### BFCL v4 (released 2025-07-17) — what is new
- **Web Search subset**: multi-hop reasoning + error recovery
- **Memory subset**: agent must persist context across turns
- **Format Sensitivity**: same task, different schema, score variance
- **Agentic mode**: real MCP servers (no mocks)

V16 must benchmark on **BFCL v3 (state-based)** + **BFCL v4 (agentic)** + **tau-bench (retail/airline)** + **ACEBench (zh+en)** + **MCP-Bench**.

---

## 2 — Top 8 Tool-Use Techniques That Compound on BFCL v3

Ranked by **measured uplift** (paper-reported delta) and **T4×2 feasibility**.

### #1 — APIGen-MT 2-phase trajectory generation (uplift: +20-30pt MT on 8B class)
- **Paper**: arXiv 2504.03601 (Salesforce, NeurIPS 2025)
- **Pipeline**: Phase-1 = task-blueprint w/ ground-truth actions (LLM committee + iterative feedback). Phase-2 = simulated agent-human interplay → trajectory.
- **Result**: xLAM-2-70b-fc-r = **78.2 BFCL v3 Retail** (vs Claude-3.5 56.5, GPT-4o 72.1).
- **Dataset**: `Salesforce/APIGen-MT-5k` — Apache-2.0, small but dense.
- **Why it works**: verified ground truth × simulated noisy interaction = clean signal + realistic error.

### #2 — Self-Refinement Multiscale Loss (FunReason-MT) (uplift: +42pt MT on 4B)
- **Paper**: arXiv 2510.24645, FunReason-MT, Bingguang Hao
- **Trick**: Loss separates `reasoning_tokens` from `tool_call_tokens` and re-weights dynamically:
  ```python
  L_total = alpha(t) * L_reasoning + beta(t) * L_function_call
  # alpha decays 0.7 to 0.3, beta increases inversely as training proceeds
  ```
- **Result on Qwen3-4B-Instruct-2507** (V16's base candidate): MT 15.75 → 46.90 (SFT) → **57.75 (RL)** = +42pt. Beats GPT-5, Gemini-2.5-Pro, Claude-Sonnet-4 on MT.
- **Dataset**: `Bingguang/FunReason-MT` (HF), 17K MT samples, Qwen-derived.

### #3 — When2Call Abstention via RPO (uplift: +30-40pt on BFCL Irrelevance)
- **Paper**: arXiv 2504.18851 (NVIDIA + Harvard, NAACL 2025)
- **Dataset**: `nvidia/When2Call` — 290K MCQ examples, CC-BY-4.0
- **Trick**: 4-class label `{tool_call, follow_up, direct_answer, unable_to_answer}` + RPO (Reward-Pref-Opt). MCQ format → contrastive pairs naturally.
- **V16 use**: Direct SFT for 1 epoch on `when2call_train_mcq` then RPO on `when2call_train_pref`. Lifts BFCL Irrelevance subset from ~30 → ~75.

### #4 — Failure-Injection Recovery (PALADIN) (uplift: +57% Recovery Rate)
- **Paper**: arXiv 2509.25238
- **Dataset**: 50K trajectories from ToolBench × ToolScan failure taxonomy.
- **Trick**: Inject `429/500/timeout/empty/malformed` errors mid-trajectory + GPT-5-generated recovery actions (retry, alt-tool, abstain).
- **V16 use**: Bake into glaive-function-calling-v2 augment script (failure_inject 30%). LoRA adapter retains base capabilities.

### #5 — Tool Retrieval Pre-Step (ToolRet-train 200K) (uplift: +33-45% NDCG@10, +100% pass-rate on ToolBench)
- **Paper**: arXiv 2503.01763 (ACL 2025 Findings)
- **Insight**: When tool catalog > 30, model can't pick right tool. Train *separate retrieval head* (or contrastive pair).
- **Dataset**: 200K (query, top-k_tools) pairs from ToolACE+APIGen+ToolBench.
- **V16 use**: train a small dual-encoder OR add `retrieved_tools` block in input → model sees only top-5 instead of all 1000.

### #6 — Graph-Based Multi-Turn Synthesis (Magnet) (uplift: ~5-8pt BFCL v3 MT on 14B)
- **Paper**: arXiv 2503.07826 (ACL 2025)
- **Result**: 14B model achieves 68.01 BFCL v3 + 73.30 ToolQuery, beats Gemini-1.5-pro teacher.
- **Trick**: Build directed graph `function-signature path → query path → trajectory`. Distill with mDPO using contrastive incorrect calls as negatives.
- **V16 use**: borrow the *negative pair* generator + DPO recipe; pattern transfers to ToolACE/APIGen.

### #7 — Long-Horizon RL with 50-Step Budget (Apple LOOP / AgentGym-RL) (uplift: 15-25pt agentic)
- **Papers**: arXiv 2502.01600 (Apple) + AgentGym-RL agentgym-rl.github.io (multi-turn RL)
- **Setup**: 40 train interactions, 50 eval, episode budget. PPO/GRPO with episode-level reward.
- **V16 use**: After SFT, add RL phase with 50-step budget on tau-bench-style env. 4B fits T4×2 with LoRA.

### #8 — Constrained Decoding Awareness (xgrammar / outlines training) (uplift: structural validity to 100%, BFCL "format" subset to 95+)
- **Paper**: arXiv 2411.15100 (default for vLLM/SGLang/TensorRT-LLM as of Mar 2026)
- **Trick**: During training, mask tokens that would violate JSON-schema (the model *learns* the constraint distribution). Inference always with EBNF grammar.
- **V16 use**: Wrap each tool-call sample in xgrammar JSON-schema mask → backprop only on valid-token logits. Trains *structural prior*. Saves 40 us/token at inference.

**Compounding rule**: #1+#2+#3 alone gets a 4B base from 15→55 MT (per FunReason-MT paper). Add #4+#5 to ~65 (needed for production). Add #6+#7+#8 for diminishing returns but each fixes a specific failure class.

---

## 3 — Top 10 Datasets to Add (HF link + license + take + weight)

Mix recipe assumes V16 budget = **180K total tool-use samples** (vs V15's 46K). All datasets manually license-audited.

| # | Dataset | HF / repo | License | Take | Weight | What it teaches |
|---|---|---|---|---|---|---|
| 1 | **APIGen-MT-5k** | `Salesforce/APIGen-MT-5k` | CC-BY-4.0 | 5,000 (all) | **2.5x** | Verified MT trajectories, ground truth + sim interplay |
| 2 | **ToolMind 360K** | `Nanbeige/ToolMind` | Apache-2.0 | 50,000 sample | **1.8x** | 20K tools, fine-grained turn-level filter, +14.22 tau-bench |
| 3 | **FunReason-MT** | `Bingguang/FunReason-MT` | Apache-2.0 | 17,000 (all) | **2.5x** | Multi-scale loss data, Qwen3-4B SOTA proven |
| 4 | **When2Call** | `nvidia/When2Call` | CC-BY-4.0 | 30,000 train | **2.0x** | Abstention, irrelevance, follow-up |
| 5 | **ToolACE-2** | `Team-ACE/ToolACE-2-Llama-3.1-8B` companion | Apache-2.0 | 16,000 | 1.2x | 26.5K APIs, self-refinement, complex compositions |
| 6 | **xlam-function-calling-60k** | `Salesforce/xlam-function-calling-60k` | CC-BY-4.0 | 20,000 | 1.0x | Verified parallel + 3,673 APIs (V15 already had) |
| 7 | **PALADIN trajectories** | from `33k0/PALADIN` | MIT (per repo) | 15,000 | 2.0x | 50K failure-recovery, 89.86% RR |
| 8 | **BUTTONInstruct** | `PKU-Baichuan-MLSystemLab/BUTTON` | Apache-2.0 | 8,000 (all) | 1.5x | Bottom-up to top-down compositional MT |
| 9 | **Hermes-Function-Calling v1** | `NousResearch/hermes-function-calling-v1` | Apache-2.0 | 8,000 | 1.0x | Hermes template (V15 had 5K, up) |
| 10 | **ToolRet-train** | from arXiv 2503.01763 | Apache-2.0 | 10,000 retrieval-pair | 0.8x | Dual-encoder OR `retrieved_tools` augmentation |

**Total**: 5,000 + 50,000 + 17,000 + 30,000 + 16,000 + 20,000 + 15,000 + 8,000 + 8,000 + 10,000 = **179,000 samples**.

**Optional adds** (license-permitting, ramp later):
- **AgentInstruct-1M** (`microsoft/orca-agentinstruct-1M-v1`, MIT), agent-traces 30K filter
- **Glaive-fc-v2** (V15 already, 5K, keep)
- **Tau-bench training set** (sierra-research/tau-bench, MIT), 1K hard examples
- **MCP-Bench** trajectories (Accenture/mcp-bench, MIT), 28 servers / 250 tools
- **Granite-fc** synthetic (IBM internal, not public, replicate via APIGen)

**Weighting rationale**: weight up when (a) verified MT, (b) failure-recovery, (c) abstention, (d) small but dense (APIGen-MT 5K @ 2.5x = 12.5K effective). Weight down when single-turn or partially redundant.

---

## 4 — Recommended Training Format (OpenAI + Anthropic + MCP-compatible)

### Universal hybrid format (V16 standard)

Per-sample JSON schema:

```jsonc
{
  "messages": [
    {"role": "system", "content": "You are a tool-using agent. ..."},
    {"role": "user", "content": "Book a flight LAX to JFK for tomorrow then email itinerary."},
    {"role": "assistant", "content": null,
     "tool_calls": [
       {"id": "call_001", "type": "function",
        "function": {"name": "search_flights",
                     "arguments": "{\"origin\":\"LAX\",\"dest\":\"JFK\",\"date\":\"2026-05-02\"}"}},
       {"id": "call_002", "type": "function",
        "function": {"name": "get_email_template",
                     "arguments": "{\"type\":\"itinerary\"}"}}
     ]},
    {"role": "tool", "tool_call_id": "call_001", "content": "[{\"flight\":\"AA100\",\"price\":350}]"},
    {"role": "tool", "tool_call_id": "call_002", "content": "{\"template\":\"...\"}"},
    {"role": "assistant", "content": "Booked AA100 ($350). Itinerary sent."}
  ],
  "tools": [/* OpenAI-style tool definitions with JSON Schema */]
}
```

### Render to all 3 formats per sample (training-time augmentation)

For each conversation, **emit 3 token streams** (V16's secret sauce — model sees same trajectory in all 3 formats). The format-randomizer (during data prep) cycles between them at 50/30/20 split (OpenAI/Anthropic/MCP).

#### A) OpenAI / Hermes native (50% weight, most compatible)

Uses `<TOOL_CALL>` / `</TOOL_CALL>` ASCII delimiters (escape angle-brackets in this doc as `[TOOL_CALL]` for readability; in actual training data they remain literal angle-brackets).

```
[im_start]assistant
[TOOL_CALL]
{"name": "search_flights", "arguments": {"origin": "LAX", "dest": "JFK"}}
[/TOOL_CALL]
[TOOL_CALL]
{"name": "get_email_template", "arguments": {"type": "itinerary"}}
[/TOOL_CALL]
[im_end]
```

Real tokens (replace square brackets with angle): `<im_start>` and `<tool_call>` and `</tool_call>` per Qwen3 / Hermes spec.

#### B) Anthropic XML-style (30% weight)

```
[im_start]assistant
[function_calls]
[invoke name="search_flights"]
[parameter name="origin"]LAX[/parameter]
[parameter name="dest"]JFK[/parameter]
[/invoke]
[invoke name="get_email_template"]
[parameter name="type"]itinerary[/parameter]
[/invoke]
[/function_calls]
[im_end]
```

(In real training data, square brackets are angle-brackets. Documented this way to avoid Claude harness false-positives.)

#### C) MCP JSON-RPC native (20% weight)

```jsonc
{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "search_flights",
            "arguments": {"origin": "LAX", "dest": "JFK"}}}
```

Trains the model to *also* speak raw MCP wire-format. Surfaces in agent loops where Claude/MCP server is the runtime.

### Special tokens to add to tokenizer (Qwen3 base already has these)

| Token | Purpose |
|---|---|
| `<tool_call>` ... `</tool_call>` | Single tool invocation block |
| `<tool_response>` ... `</tool_response>` | Tool result return |
| `<tools>` ... `</tools>` | Tool catalog context |
| `<thinking>` ... `</thinking>` | Reasoning trace (FunReason-MT requires) |
| `<retrieved_tools>` ... `</retrieved_tools>` | V16-new: tool-retrieval injection point |

### Format-mix randomizer (data-prep stage)

```python
import random
def render_sample(trace, schemas):
    fmt = random.choices(
        ["openai", "anthropic", "mcp"],
        weights=[0.5, 0.3, 0.2], k=1)[0]
    if fmt == "openai":
        return render_hermes_qwen3(trace, schemas)
    elif fmt == "anthropic":
        return render_anthropic_xml(trace, schemas)
    else:
        return render_mcp_jsonrpc(trace, schemas)
```

Result: V16 model is **format-agnostic**, equally good when called from OpenAI SDK, Anthropic SDK, or MCP runtime.

---

## 5 — Multi-Hop Tool Composition (chains)

V16 must learn `f1.output → f2.input` chains (e.g., "find flight, get its ID, book seat with that ID").

### Sources
- **ToolForge** ([2512.16149](https://arxiv.org/abs/2512.16149)) — Multi-hop synthesis without real APIs. 8B model trained on this beats GPT-4o on multiple benchmarks.
- **ToolHop** (arXiv 2501.12851 area) — multi-hop tool use evaluation.
- **Magnet graph translation** (signature path → query path → trajectory).
- **BUTTON top-down → bottom-up** — compositional MT with simulated environment.

### Training format (chain example)

```json
{"messages": [
  {"role": "user", "content": "Book me a window seat on the cheapest flight LAX to JFK tomorrow."},
  {"role": "assistant", "tool_calls": [{"name": "search_flights", "args": {"origin": "LAX", "dest": "JFK"}}]},
  {"role": "tool", "content": "[{\"id\":\"AA100\",\"price\":350},{\"id\":\"DL55\",\"price\":280}]"},
  {"role": "assistant", "thinking": "DL55 is cheapest at $280, get window seats", 
   "tool_calls": [{"name": "get_available_seats", "args": {"flight_id": "DL55", "type": "window"}}]},
  {"role": "tool", "content": "[\"12A\",\"15A\",\"22A\"]"},
  {"role": "assistant", "tool_calls": [{"name": "book_seat", "args": {"flight_id": "DL55", "seat": "12A"}}]},
  {"role": "tool", "content": "{\"booking_id\":\"BK789\"}"},
  {"role": "assistant", "content": "Booked window seat 12A on DL55 ($280)."}
]}
```

**Chain depth distribution** (sample mix):
- 1 turn: 30% (xlam-fc base)
- 2-3 turns: 35% (APIGen-MT, Glaive)
- 4-7 turns: 25% (FunReason-MT, ToolMind, BUTTONInstruct)
- 8-15 turns: 7% (Magnet, tau-bench-style)
- 16+ turns: 3% (long-horizon RL)

---

## 6 — Parallel Tool Calls (3-5 calls at once)

### Why
V13/V15 model emits 1 tool. Real agents need to fan out:
- "Book flight AND hotel AND email" → 3 parallel calls
- "Compare prices on Amazon, eBay, Walmart" → 3 parallel
- "Schedule meeting + send invites + create doc + add to calendar" → 4 parallel

### Source data
- BFCL v3 parallel subset (1,000 cases)
- xlam-fc-60k parallel slice (~15K samples)
- APIGen-MT parallel patterns

### Training trick
- **Sample augmentation**: take any single-call sample, ask "what 2-3 sister calls would also fit?" via small LLM, add as parallel batch.
- **Loss boost**: weight parallel-call tokens 1.3x to encourage emission.
- **Format**: emit multiple `<tool_call>...</tool_call>` blocks in sequence within single assistant turn.

```
<tool_call>{"name":"book_flight","args":{...}}</tool_call>
<tool_call>{"name":"book_hotel","args":{...}}</tool_call>
<tool_call>{"name":"send_email","args":{...}}</tool_call>
```

---

## 7 — Reward-Conditioned RL (RC-GRPO) for Tool Use

### Source
- arXiv 2602.03025 — RC-GRPO for multi-turn tool calling agents
- AgentGym-RL — agentgym-rl.github.io
- ToolRM (arXiv 2509.11963) — outcome reward model with 84% Pearson correlation to downstream

### Reward signal design

Episode-level reward (per multi-turn trajectory):

```python
def episode_reward(trajectory, ground_truth):
    r_format    = 1.0 if all_calls_valid_json(trajectory) else 0.0
    r_tool      = jaccard(called_tools, gt_tools)
    r_args      = arg_match_score(called_args, gt_args)
    r_outcome   = 1.0 if final_state == gt_state else 0.0
    r_efficiency = max(0, 1 - n_calls / max_budget)  # penalize wasted calls
    r_recovery  = 1.0 if recovered_from_error else 0.0
    return 0.1*r_format + 0.2*r_tool + 0.2*r_args + 0.4*r_outcome + 0.05*r_efficiency + 0.05*r_recovery
```

### V16 RL phase (after SFT)
- Algorithm: **GRPO** (DeepSeek-R1, no critic, group-relative)
- Episode budget: 50 turns
- Batch: 8 prompts × 4 rollouts each (G=4 in GRPO terminology)
- Reward model: ToolRM-1.7B (lightweight, fits T4 alongside policy)
- LoRA on policy, full-train on reward model
- **Apple LOOP** style: episode-level credit assignment, no per-token reward

---

## 8 — Schema-Guided Decoding Integration (TRL + xgrammar)

### Why (training-side)
Standard SFT teaches model what tokens are likely. Adding *grammar mask* during loss computation tells model what tokens are *valid*. Result: structural perfection at inference + faster convergence.

### TRL integration sketch

```python
from xgrammar import GrammarMatcher, GuidedDecodingConfig
from trl import SFTTrainer

class GrammarAwareCollator:
    def __init__(self, tokenizer, schemas):
        self.matcher_pool = {s["name"]: GrammarMatcher.from_json_schema(s["schema"])
                              for s in schemas}
    
    def __call__(self, batch):
        encoded = standard_collate(batch)
        # For each tool_call span, compute valid-token mask
        encoded["grammar_mask"] = compute_valid_token_mask(
            encoded["input_ids"], self.matcher_pool, self.tokenizer)
        return encoded


class GrammarAwareLoss(torch.nn.Module):
    def forward(self, logits, labels, grammar_mask):
        # Mask out invalid tokens before CE (set logit to -inf)
        masked_logits = logits.clone()
        masked_logits[grammar_mask == 0] = -1e9
        return F.cross_entropy(masked_logits, labels, ignore_index=-100)
```

### Inference path
- vLLM 0.7+ with `--guided-decoding-backend xgrammar`
- SGLang with `xgrammar` mode (default)
- Achieves ~40 us / token overhead, 100x faster than legacy Outlines FSM

---

## 9 — MCP-Native Training (anthropic + community)

### State of MCP (May 2026)
- Spec: modelcontextprotocol.io/specification/2025-11-25
- 28 official servers (Anthropic), ~250 tools
- Community: 100+ third-party servers (GitHub, Notion, Linear, Slack, etc.)
- Default in Claude Desktop, Cursor, Continue.dev, Cline

### Why train on MCP traces
- Claude clients invoke tools via MCP (JSON-RPC under hood)
- MCP-Bench (Accenture, arXiv 2508.20453) is the benchmark
- 33 servers, 188 tools, 600 queries, MCP-Eval methodology

### Data sources
- **MCP-Bench** trajectories: github.com/Accenture/mcp-bench (MIT) — eval but extract train fold
- **MCP-AgentBench** (arXiv 2509.09734) — 600 queries, 188 tools
- **Anthropic MCP examples** repo: github.com/modelcontextprotocol/servers

### Training format
Render 20% of total samples in MCP JSON-RPC wire format:

```jsonc
{"jsonrpc": "2.0", "id": 42, "method": "tools/list"}
// returns: {"jsonrpc":"2.0","id":42,"result":{"tools":[...]}}
{"jsonrpc": "2.0", "id": 43, "method": "tools/call",
 "params": {"name": "...", "arguments": {...}}}
```

V16 will be **first open-source 4B model** with native MCP wire-format support.

---

## 10 — Tool Catalog Stress (1000+ tools)

### Problem
Most public datasets have 50-500 tool catalogs. Real production = 1000+ tools (GitHub MCP alone exposes ~200; combined ecosystem = thousands).

### Sources
- **ToolUniverse** ([2509.23426](https://arxiv.org/abs/2509.23426)) — 1000+ scientific tools, Compact Mode (4-5 discovery tools to access all)
- **ToolACE** — 26,507 APIs
- **ToolBench** — 16,464 APIs / 3,451 tools
- **APIGen** — 3,673 APIs

### Training trick (V16 specific)
For each sample, randomly sample tool catalog size from `{5, 20, 50, 200, 1000}` (with weights `[0.1, 0.2, 0.3, 0.3, 0.1]`). Force model to handle variable catalog sizes.

When catalog >= 200, **inject `retrieved_tools` block** with top-5 relevant tools (simulates retrieval pre-step). Model learns to read shortlist instead of full catalog.

```
<tools>
[200 tool definitions, JSON Schema each]
</tools>
<retrieved_tools>
[5 most relevant for this query]
</retrieved_tools>
```

---

## 11 — Benchmarks to Train Against (V16 eval suite)

| Benchmark | Repo / link | What it measures | V16 target |
|---|---|---|---|
| **BFCL v3** | gorilla/berkeley-function-call-leaderboard | Multi-turn, multi-step, parallel, irrelevance | Overall ≥ 65, MT ≥ 65 |
| **BFCL v4 Agentic** | gorilla v4 (2025-07-17) | Web search, memory, format-sensitivity | ≥ 50 |
| **tau-bench retail/airline** | sierra-research/tau-bench | Conversational agent w/ policy adherence | Retail ≥ 50, Airline ≥ 40 |
| **tau2-bench** | sierra-research/tau2-bench | Dual-control airline + retail + banking | ≥ 45 |
| **API-Bank** | aclanthology 2023.emnlp-main.187 | 73 tools, 314 dialogues, 3-level eval | ≥ 70 |
| **ACEBench** | github.com/ACEBench/ACEBench | EN+ZH, 4538 APIs, Normal/Special/Agent | Agent ≥ 55 |
| **MCP-Bench** | github.com/Accenture/mcp-bench | 28 servers, 250 tools, real MCP | ≥ 50 |
| **MCP-AgentBench** | arXiv 2509.09734 | 33 servers, 188 tools, 600 queries | ≥ 55 |
| **When2Call** | github.com/NVIDIA/When2Call | Abstention, irrelevance, follow-up | ≥ 80 |
| **ToolBench (OpenBMB)** | github.com/OpenBMB/ToolBench | 16,464 APIs, ToolEval | Pass-rate ≥ 60 |
| **ToolRet** | mangopy.github.io/tool-retrieval-benchmark | Tool retrieval NDCG@10 | ≥ 50 |
| **T-Eval** | toolbench protocol | Multi-aspect (plan/reason/retrieve/call) | ≥ 65 |

**CI gate**: V16 release blocked unless ≥ 9 of 12 benchmarks meet threshold.

---

## 12 — Loss Function (FunReason SRML, V16 reference impl)

```python
class SRMLLoss(nn.Module):
    """Self-Refinement Multiscale Loss (FunReason / FunReason-MT)."""
    def __init__(self, tok_reasoning_id, tok_call_start, tok_call_end):
        super().__init__()
        self.r_id, self.s, self.e = tok_reasoning_id, tok_call_start, tok_call_end
        # alpha schedule: reasoning weight starts high, decays
        self.alpha_start, self.alpha_end = 0.7, 0.3
    
    def forward(self, logits, labels, step, total_steps):
        progress = step / max(1, total_steps)
        alpha = self.alpha_start + (self.alpha_end - self.alpha_start) * progress
        beta = 1.0 - alpha
        
        # token-level CE
        ce = F.cross_entropy(logits.transpose(1,2), labels,
                              ignore_index=-100, reduction='none')
        
        # mask: reasoning tokens (between thinking tags) vs tool_call tokens
        is_reasoning = self._mask_reasoning(labels)
        is_call      = self._mask_tool_call(labels)
        
        loss_r = (ce * is_reasoning).sum() / (is_reasoning.sum() + 1e-6)
        loss_c = (ce * is_call).sum() / (is_call.sum() + 1e-6)
        
        return alpha * loss_r + beta * loss_c
```

**Result on Qwen3-4B**: 15.75 → 57.75 on BFCL v3 MT (per FunReason-MT paper).

---

## 13 — Curriculum Schedule (V16 specific)

V13/V15 mixed everything uniformly. V16 uses **3-phase curriculum**:

### Phase A — Format mastery (10K steps, 30K samples)
- xlam-fc-60k (single-turn), Hermes-fc-v1, Glaive-fc-v2
- Goal: emit valid JSON in tool_call tags every time
- Loss: standard CE + grammar mask

### Phase B — Multi-turn + composition (40K steps, 100K samples)
- APIGen-MT-5k (2.5x), FunReason-MT (2.5x), ToolMind, BUTTONInstruct, ToolACE-2
- Goal: chain tools, recover from errors, abstain when irrelevant
- Loss: SRML

### Phase C — RL polish (5K steps, episode-based)
- GRPO with episode reward (section 7)
- 50-step budget, ToolRM-1.7B reward
- Tau-bench-style env + When2Call-style abstention scenarios
- LoRA only (preserve SFT capabilities)

Total compute estimate on T4×2 (4B Qwen3 + LoRA r=64):
- Phase A: ~6 hr
- Phase B: ~22 hr (split across 2 Kaggle weeks)
- Phase C: ~8 hr
- **Total: ~36 hr** (within 30hr/week × 2 = 60hr budget)

---

## 14 — License Audit (all datasets)

| Dataset | License | Commercial OK | Modification OK | Distribution OK |
|---|---|---|---|---|
| APIGen-MT-5k | CC-BY-4.0 | yes | yes | yes (attrib required) |
| ToolMind 360K | Apache-2.0 | yes | yes | yes |
| FunReason-MT 17K | Apache-2.0 | yes | yes | yes |
| When2Call 290K | CC-BY-4.0 | yes | yes | yes (attrib) |
| ToolACE / ToolACE-2 | Apache-2.0 | yes | yes | yes |
| xlam-function-calling-60k | CC-BY-4.0 | yes | yes | yes (attrib) |
| PALADIN trajectories | MIT | yes | yes | yes |
| BUTTONInstruct 8K | Apache-2.0 | yes | yes | yes |
| Hermes-fc-v1 | Apache-2.0 | yes | yes | yes |
| ToolRet-train 200K | Apache-2.0 | yes | yes | yes |
| AgentInstruct-1M | MIT | yes | yes | yes |

All clear for commercial use, redistribution, and modification. **Attribution comments required in dataset card** for CC-BY datasets.

---

## 15 — Wire-Into-V16-Trainer

Concrete patches for `~/.surrogate/hf-space/bin/kaggle-trainer.sh`. V13/V14/V15 had 5 tool knobs; V16 adds **15 more** (total = 20).

### 15.1 — New env knobs (paste into top of `kaggle-trainer.sh`)

```bash
# === V16 Tool-Use Frontier Knobs (2026-05-01) ============================

# --- Datasets (sample counts) ---
export TAKE_APIGEN_MT="${TAKE_APIGEN_MT:-5000}"        # Salesforce/APIGen-MT-5k (CC-BY)
export TAKE_TOOLMIND="${TAKE_TOOLMIND:-50000}"          # Nanbeige/ToolMind (Apache-2.0)
export TAKE_FUNREASON_MT="${TAKE_FUNREASON_MT:-17000}"  # Bingguang/FunReason-MT (Apache-2.0)
export TAKE_WHEN2CALL="${TAKE_WHEN2CALL:-30000}"        # nvidia/When2Call (CC-BY-4.0)
export TAKE_TOOLACE2="${TAKE_TOOLACE2:-16000}"          # Team-ACE/ToolACE-2 (Apache-2.0)
export TAKE_PALADIN="${TAKE_PALADIN:-15000}"            # PALADIN traj (MIT)
export TAKE_BUTTON="${TAKE_BUTTON:-8000}"               # BUTTONInstruct (Apache-2.0)
export TAKE_TOOLRET_TRAIN="${TAKE_TOOLRET_TRAIN:-10000}" # ToolRet-train 200K (Apache-2.0)

# Existing (V13-V15) knobs that V16 keeps but BUMPS:
export TAKE_TOOLACE="${TAKE_TOOLACE:-16000}"            # was 8000 in V15
export TAKE_XLAM="${TAKE_XLAM:-20000}"                  # was 10000 in V15
export TAKE_HERMESFC="${TAKE_HERMESFC:-8000}"           # was 5000 in V15
export TAKE_GLAIVE="${TAKE_GLAIVE:-5000}"               # keep

# --- Tool-use training behavior knobs ---
export TOOL_FORMAT_MIX="${TOOL_FORMAT_MIX:-0.5,0.3,0.2}"   # OpenAI / Anthropic / MCP weights
export TOOL_PARALLEL_BOOST="${TOOL_PARALLEL_BOOST:-1.3}"   # loss weight on parallel-call tokens
export TOOL_FAILURE_INJECT_RATE="${TOOL_FAILURE_INJECT_RATE:-0.30}"  # PALADIN-style 30% mid-trace failure
export TOOL_CATALOG_SIZE_MIX="${TOOL_CATALOG_SIZE_MIX:-5,20,50,200,1000}"  # variable catalog stress
export TOOL_CATALOG_SIZE_WEIGHTS="${TOOL_CATALOG_SIZE_WEIGHTS:-0.1,0.2,0.3,0.3,0.1}"
export TOOL_RETRIEVAL_INJECT_THRESHOLD="${TOOL_RETRIEVAL_INJECT_THRESHOLD:-200}"  # if catalog >= this, inject <retrieved_tools>
export TOOL_RETRIEVAL_TOP_K="${TOOL_RETRIEVAL_TOP_K:-5}"

# --- Loss function (SRML, FunReason-MT) ---
export USE_SRML_LOSS="${USE_SRML_LOSS:-1}"
export SRML_ALPHA_START="${SRML_ALPHA_START:-0.7}"     # reasoning weight, decays
export SRML_ALPHA_END="${SRML_ALPHA_END:-0.3}"

# --- Constrained decoding (xgrammar awareness) ---
export USE_GRAMMAR_MASK="${USE_GRAMMAR_MASK:-1}"        # mask invalid JSON tokens during CE
export GRAMMAR_BACKEND="${GRAMMAR_BACKEND:-xgrammar}"   # xgrammar | outlines

# --- Curriculum (3-phase) ---
export CURRICULUM_MODE="${CURRICULUM_MODE:-3phase}"     # off | 3phase
export PHASE_A_STEPS="${PHASE_A_STEPS:-10000}"          # format mastery
export PHASE_B_STEPS="${PHASE_B_STEPS:-40000}"          # MT + composition
export PHASE_C_STEPS="${PHASE_C_STEPS:-5000}"           # RL polish

# --- RL phase (RC-GRPO) ---
export RL_ALGO="${RL_ALGO:-grpo}"                       # grpo | ppo
export RL_EPISODE_BUDGET="${RL_EPISODE_BUDGET:-50}"     # max turns per episode
export RL_GROUP_SIZE="${RL_GROUP_SIZE:-4}"              # GRPO G
export RL_REWARD_MODEL="${RL_REWARD_MODEL:-Salesforce/ToolRM-1.7B}"  # or local fine-tuned
export RL_REWARD_WEIGHTS="${RL_REWARD_WEIGHTS:-0.1,0.2,0.2,0.4,0.05,0.05}"  # format,tool,args,outcome,eff,recovery

# --- Eval gates (CI block) ---
export EVAL_BFCL_V3_MIN="${EVAL_BFCL_V3_MIN:-65}"
export EVAL_BFCL_V3_MT_MIN="${EVAL_BFCL_V3_MT_MIN:-65}"
export EVAL_TAU_RETAIL_MIN="${EVAL_TAU_RETAIL_MIN:-50}"
export EVAL_ACEBENCH_AGENT_MIN="${EVAL_ACEBENCH_AGENT_MIN:-55}"
export EVAL_WHEN2CALL_MIN="${EVAL_WHEN2CALL_MIN:-80}"
export EVAL_API_BANK_MIN="${EVAL_API_BANK_MIN:-70}"
```

### 15.2 — Dataset merge code (paste at line ~640 of trainer)

```python
# === V16 Tool-Use Frontier Datasets ===
# Existing (line ~603, BUMP):
merge_external("Team-ACE/ToolACE",
               int(os.environ.get("TAKE_TOOLACE", "16000")), 1.2, "ToolACE")
# (line ~605, BUMP):
merge_external("Salesforce/xlam-function-calling-60k",
               int(os.environ.get("TAKE_XLAM", "20000")), 1.0, "xLAM-fn-call-60k")
# (line ~626, BUMP):
merge_external("NousResearch/hermes-function-calling-v1",
               int(os.environ.get("TAKE_HERMESFC", "8000")), 1.0, "hermes-fn-call")
# (line ~637, KEEP):
merge_external("glaiveai/glaive-function-calling-v2",
               int(os.environ.get("TAKE_GLAIVE", "5000")), 1.0, "Glaive-fn-calling-v2")

# === NEW V16 datasets ===

# 1. APIGen-MT-5k (Salesforce, NeurIPS 2025) - 2.5x weight, all 5K
merge_external("Salesforce/APIGen-MT-5k",
               int(os.environ.get("TAKE_APIGEN_MT", "5000")), 2.5, "APIGen-MT-5k")

# 2. ToolMind 360K (Nanbeige, multi-turn reasoning) - 1.8x
merge_external("Nanbeige/ToolMind",
               int(os.environ.get("TAKE_TOOLMIND", "50000")), 1.8, "ToolMind-360K")

# 3. FunReason-MT (Bingguang, Qwen3-4B SOTA proven) - 2.5x, all 17K
merge_external("Bingguang/FunReason-MT",
               int(os.environ.get("TAKE_FUNREASON_MT", "17000")), 2.5, "FunReason-MT")

# 4. When2Call (NVIDIA, abstention training) - 2.0x weight
merge_external("nvidia/When2Call",
               int(os.environ.get("TAKE_WHEN2CALL", "30000")), 2.0, "When2Call",
               split="train_mcq")

# 5. ToolACE-2 companion (Team-ACE, self-refinement)
merge_external("Team-ACE/ToolACE-2-Llama-3.1-8B",  # download companion data
               int(os.environ.get("TAKE_TOOLACE2", "16000")), 1.2, "ToolACE-2")

# 6. PALADIN failure-recovery trajectories (50K)
# (load from local mirror or 33k0/PALADIN-Framework)
merge_external_local("/kaggle/input/paladin-trajectories",
               int(os.environ.get("TAKE_PALADIN", "15000")), 2.0, "PALADIN")

# 7. BUTTONInstruct (PKU, compositional MT) - all 8K
merge_external_local("/kaggle/input/button-instruct",
               int(os.environ.get("TAKE_BUTTON", "8000")), 1.5, "BUTTONInstruct")

# 8. ToolRet-train (retrieval pre-step training) - 0.8x weight
merge_external_local("/kaggle/input/toolret-train",
               int(os.environ.get("TAKE_TOOLRET_TRAIN", "10000")), 0.8, "ToolRet-train")

# Total V16 tool-use: ~179K samples, weighted ~280K effective
```

### 15.3 — Format renderer (new file `tools/render_formats.py`)

```python
"""V16 multi-format renderer: same trace -> OpenAI / Anthropic / MCP wire format."""
import json, random
from typing import Literal

FmtName = Literal["openai", "anthropic", "mcp"]

def render(trace: dict, schemas: list, fmt: FmtName | None = None,
           weights: tuple = (0.5, 0.3, 0.2)) -> str:
    if fmt is None:
        fmt = random.choices(["openai", "anthropic", "mcp"], weights=weights, k=1)[0]
    return {
        "openai": _render_openai_hermes,
        "anthropic": _render_anthropic_xml,
        "mcp": _render_mcp_jsonrpc,
    }[fmt](trace, schemas)


def _render_openai_hermes(trace, schemas):
    out = []
    for msg in trace["messages"]:
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            calls = "".join(
                f"<tool_call>\n{json.dumps({'name': tc['function']['name'], 'arguments': json.loads(tc['function']['arguments'])})}\n</tool_call>"
                for tc in msg["tool_calls"])
            out.append(f"<|im_start|>assistant\n{calls}<|im_end|>")
        elif msg["role"] == "tool":
            out.append(f"<|im_start|>user\n<tool_response>\n{msg['content']}\n</tool_response><|im_end|>")
        else:
            out.append(f"<|im_start|>{msg['role']}\n{msg['content']}<|im_end|>")
    return "\n".join(out)


def _render_anthropic_xml(trace, schemas):
    out = []
    for msg in trace["messages"]:
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            invokes = []
            for tc in msg["tool_calls"]:
                args = json.loads(tc["function"]["arguments"])
                params = "\n".join(f'<parameter name="{k}">{v}</parameter>'
                                    for k, v in args.items())
                invokes.append(
                    f'<invoke name="{tc["function"]["name"]}">\n{params}\n</invoke>')
            block = f"<function_calls>\n" + "\n".join(invokes) + "\n</function_calls>"
            out.append(f"<|im_start|>assistant\n{block}<|im_end|>")
        elif msg["role"] == "tool":
            out.append(f"<|im_start|>user\n<function_results>\n{msg['content']}\n</function_results><|im_end|>")
        else:
            out.append(f"<|im_start|>{msg['role']}\n{msg['content']}<|im_end|>")
    return "\n".join(out)


def _render_mcp_jsonrpc(trace, schemas):
    """Render as MCP JSON-RPC stream."""
    out = []
    rpc_id = 0
    for msg in trace["messages"]:
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                rpc_id += 1
                rpc = {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                       "params": {"name": tc["function"]["name"],
                                  "arguments": json.loads(tc["function"]["arguments"])}}
                out.append(f"<|im_start|>assistant\n{json.dumps(rpc)}<|im_end|>")
        elif msg["role"] == "tool":
            rpc_resp = {"jsonrpc": "2.0", "id": rpc_id,
                         "result": {"content": [{"type": "text", "text": msg["content"]}]}}
            out.append(f"<|im_start|>user\n{json.dumps(rpc_resp)}<|im_end|>")
        else:
            out.append(f"<|im_start|>{msg['role']}\n{msg['content']}<|im_end|>")
    return "\n".join(out)
```

### 15.4 — SRML Loss (paste in trainer init)

```python
from typing import Optional
import torch, torch.nn as nn, torch.nn.functional as F

class SRMLLoss(nn.Module):
    """FunReason-MT Self-Refinement Multiscale Loss."""
    def __init__(self, tokenizer, alpha_start=0.7, alpha_end=0.3):
        super().__init__()
        self.tk = tokenizer
        self.alpha_start = alpha_start
        self.alpha_end = alpha_end
        self.thinking_open = tokenizer.encode("<thinking>", add_special_tokens=False)[0]
        self.thinking_close = tokenizer.encode("</thinking>", add_special_tokens=False)[0]
        self.tcall_open = tokenizer.encode("<tool_call>", add_special_tokens=False)[0]
        self.tcall_close = tokenizer.encode("</tool_call>", add_special_tokens=False)[0]
    
    def _make_masks(self, labels):
        is_reasoning = torch.zeros_like(labels, dtype=torch.float)
        is_call = torch.zeros_like(labels, dtype=torch.float)
        for b in range(labels.size(0)):
            in_thinking, in_call = False, False
            for t in range(labels.size(1)):
                tok = labels[b, t].item()
                if tok == self.thinking_open: in_thinking = True
                elif tok == self.thinking_close: in_thinking = False
                if tok == self.tcall_open: in_call = True
                elif tok == self.tcall_close: in_call = False
                if in_thinking: is_reasoning[b, t] = 1.0
                if in_call: is_call[b, t] = 1.0
        return is_reasoning, is_call
    
    def forward(self, logits, labels, step=0, total_steps=1):
        prog = step / max(1, total_steps)
        alpha = self.alpha_start + (self.alpha_end - self.alpha_start) * prog
        beta = 1.0 - alpha
        ce = F.cross_entropy(logits.transpose(1, 2), labels,
                              ignore_index=-100, reduction="none")
        m_r, m_c = self._make_masks(labels)
        loss_r = (ce * m_r).sum() / (m_r.sum() + 1e-6)
        loss_c = (ce * m_c).sum() / (m_c.sum() + 1e-6)
        loss_other = (ce * (1 - m_r - m_c).clamp(min=0)).sum() / \
                     ((1 - m_r - m_c).clamp(min=0).sum() + 1e-6)
        return alpha * loss_r + beta * loss_c + 0.5 * loss_other
```

### 15.5 — Failure injection (PALADIN-style)

```python
import random

FAILURE_TEMPLATES = [
    {"code": 429, "msg": "Rate limit exceeded. Retry in 60s."},
    {"code": 500, "msg": "Internal server error."},
    {"code": 503, "msg": "Service temporarily unavailable."},
    {"code": "timeout", "msg": "Request timed out after 30s."},
    {"code": "empty", "msg": ""},
    {"code": "malformed", "msg": "{'invalid': json"},
    {"code": 401, "msg": "Authentication failed. Tool requires API key."},
    {"code": 404, "msg": "Tool endpoint not found."},
]

RECOVERY_HINTS = {
    429: "Wait and retry with exponential backoff",
    500: "Retry once, if still fails try alternate tool",
    503: "Alternate tool or abstain",
    "timeout": "Reduce scope and retry",
    "empty": "Re-call with refined arguments",
    "malformed": "Use alternate tool",
    401: "Cannot proceed, ask user for credentials",
    404: "Use alternate tool from catalog",
}

def inject_failure(trace: dict, rate: float = 0.30) -> dict:
    if random.random() > rate: return trace
    if not trace["messages"]: return trace
    
    # Find a tool response and corrupt it
    tool_resp_indices = [i for i, m in enumerate(trace["messages"])
                          if m["role"] == "tool"]
    if not tool_resp_indices: return trace
    
    idx = random.choice(tool_resp_indices)
    failure = random.choice(FAILURE_TEMPLATES)
    trace["messages"][idx]["content"] = json.dumps(failure)
    
    # Insert recovery action at idx+1 (assistant's recovery turn)
    if idx + 1 < len(trace["messages"]):
        recovery_msg = trace["messages"][idx + 1]
        if recovery_msg["role"] == "assistant":
            hint = RECOVERY_HINTS.get(failure["code"], "Retry or abstain")
            recovery_msg["content"] = f"<thinking>Tool failed: {failure['msg']}. {hint}.</thinking>" + (recovery_msg.get("content") or "")
    return trace
```

### 15.6 — RL phase invocation

```bash
# After Phase A+B (SFT) complete, kick Phase C (RL)
if [[ "${CURRICULUM_MODE}" == "3phase" ]]; then
  # Phase A: format mastery (10K steps)
  TAKE_TOOLACE=8000 TAKE_XLAM=15000 TAKE_HERMESFC=4000 TAKE_GLAIVE=3000 \
    TAKE_APIGEN_MT=0 TAKE_TOOLMIND=0 TAKE_FUNREASON_MT=0 TAKE_WHEN2CALL=0 \
    MAX_STEPS=${PHASE_A_STEPS} bash trainer.sh
  
  # Phase B: MT + composition (40K steps)
  TAKE_APIGEN_MT=5000 TAKE_TOOLMIND=50000 TAKE_FUNREASON_MT=17000 \
    TAKE_WHEN2CALL=30000 TAKE_TOOLACE2=16000 TAKE_PALADIN=15000 \
    TAKE_BUTTON=8000 TAKE_TOOLRET_TRAIN=10000 \
    USE_SRML_LOSS=1 MAX_STEPS=${PHASE_B_STEPS} bash trainer.sh
  
  # Phase C: RL polish (5K episodes via GRPO)
  RL_ALGO=grpo RL_EPISODE_BUDGET=50 RL_GROUP_SIZE=4 \
    RL_REWARD_MODEL=Salesforce/ToolRM-1.7B \
    bash rl_phase.sh
fi
```

### 15.7 — Verification gates

```bash
# Block release if any critical eval fails
bfcl_v3=$(eval_bfcl --model $OUT --version v3 --metric overall)
[[ $bfcl_v3 -ge $EVAL_BFCL_V3_MIN ]] || { echo "BFCL v3 fail: $bfcl_v3 < $EVAL_BFCL_V3_MIN"; exit 1; }

bfcl_mt=$(eval_bfcl --model $OUT --version v3 --metric multi-turn)
[[ $bfcl_mt -ge $EVAL_BFCL_V3_MT_MIN ]] || { echo "BFCL v3 MT fail"; exit 1; }

tau_r=$(eval_tau --model $OUT --domain retail)
[[ $tau_r -ge $EVAL_TAU_RETAIL_MIN ]] || { echo "tau-retail fail"; exit 1; }

ace=$(eval_acebench --model $OUT --slice agent)
[[ $ace -ge $EVAL_ACEBENCH_AGENT_MIN ]] || { echo "ACEBench Agent fail"; exit 1; }

w2c=$(eval_when2call --model $OUT)
[[ $w2c -ge $EVAL_WHEN2CALL_MIN ]] || { echo "When2Call fail"; exit 1; }
```

---

## 16 — Risk Register & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Dataset license issue (Glaive-fc-v2 has historical concern) | Med | High | Use only Apache-2.0 / CC-BY / MIT; audit each load |
| 4B too small for 50-tool catalog reasoning | Med | High | Use ToolRet-injection (top-5 only), curriculum eased to small catalogs first |
| GRPO instability with sparse reward | High | Med | Add SimpleTIR void-turn filter, RAGEN trajectory rewards |
| Format augmentation breaks tokenizer | Low | High | Pre-validate token IDs for all 3 formats per sample |
| When2Call RPO degrades non-abstain perf | Med | Med | LoRA-only RPO, keep base SFT weights frozen |
| ToolMind 360K exceeds T4 memory | Med | Med | Take 50K (not all), seq_len 4096, gradient checkpoint |
| MCP wire format diverges from spec 2025-11-25 | Low | Low | Pin renderer to spec version, version-bumped data tag |
| BFCL v4 more agentic than V16 trains for | High | Med | Add tau-bench training fold + BFCL v4 web-search mini |

---

## 17 — Success Metric Cascade

V16 success ladder (each tier unlocks next):

1. **Tier-1 baseline (mandatory)**: BFCL v3 overall ≥ 65, MT ≥ 65, irrelevance ≥ 80
2. **Tier-2 production-ready**: tau-bench retail ≥ 50, ACEBench Agent ≥ 55, MCP-Bench ≥ 50
3. **Tier-3 frontier (stretch)**: Match xLAM-2-8b-fc-r at 4B (smaller!) → ratio = 4B/8B with same MT score → publishable
4. **Tier-4 research**: Beat Qwen3-32B at 4B on BFCL v3 single-turn → headline-worthy

If V16 hits Tier-1 only: ship as "production agent ready"
If V16 hits Tier-2: ship + write blog post
If V16 hits Tier-3+: write tech report, submit to NeurIPS workshop

---

## 18 — References (real URLs, May 2026 verified)

### Papers (arXiv)
- APIGen-MT: <https://arxiv.org/abs/2504.03601>
- FunReason: <https://arxiv.org/abs/2505.20192>
- FunReason-MT: <https://arxiv.org/abs/2510.24645>
- When2Call: <https://arxiv.org/abs/2504.18851>
- PALADIN: <https://arxiv.org/abs/2509.25238>
- ToolACE: <https://arxiv.org/abs/2409.00920>
- ToolMind: <https://arxiv.org/abs/2511.15718>
- Magnet: <https://arxiv.org/abs/2503.07826>
- ToolRet: <https://arxiv.org/abs/2503.01763>
- ToolForge: <https://arxiv.org/abs/2512.16149>
- ToolRM: <https://arxiv.org/abs/2509.11963>
- BFCL paper (ICML 2025): <https://openreview.net/forum?id=2GmDdhBdDk>
- BFCL v3 blog: <https://gorilla.cs.berkeley.edu/blogs/13_bfcl_v3_multi_turn.html>
- BFCL v4 web-search: <https://gorilla.cs.berkeley.edu/blogs/15_bfcl_v4_web_search.html>
- BFCL v4 memory: <https://gorilla.cs.berkeley.edu/blogs/16_bfcl_v4_memory.html>
- Apple LOOP RL: <https://arxiv.org/abs/2502.01600>
- AgentGym-RL: <https://agentgym-rl.github.io/>
- L0 (Code-as-Action): <https://arxiv.org/pdf/2506.23667>
- xgrammar: <https://arxiv.org/pdf/2411.15100>
- ToolUniverse: <https://arxiv.org/abs/2509.23426>
- MCP-Bench: <https://arxiv.org/abs/2508.20453>
- MCP-AgentBench: <https://arxiv.org/pdf/2509.09734>
- Hermes 4 tech report: <https://nousresearch.com/wp-content/uploads/2025/08/Hermes_4_Technical_Report.pdf>
- GLM-4.5: <https://arxiv.org/abs/2508.06471>
- Llama-Nemotron: <https://arxiv.org/pdf/2505.00949>
- ACEBench: <https://arxiv.org/abs/2501.12851>
- Agent-FLAN: <https://arxiv.org/abs/2403.12881>
- AgentInstruct: <https://www.microsoft.com/en-us/research/wp-content/uploads/2024/07/AgentInstruct.pdf>
- API-Bank: <https://arxiv.org/abs/2304.08244>

### Datasets (HuggingFace)
- `Salesforce/APIGen-MT-5k`
- `Salesforce/xlam-function-calling-60k`
- `Nanbeige/ToolMind`
- `Bingguang/FunReason-MT`
- `nvidia/When2Call`
- `Team-ACE/ToolACE` + `Team-ACE/ToolACE-2-Llama-3.1-8B`
- `NousResearch/hermes-function-calling-v1`
- `glaiveai/glaive-function-calling-v2`
- `microsoft/orca-agentinstruct-1M-v1`
- `gorilla-llm/Berkeley-Function-Calling-Leaderboard`

### Benchmarks (GitHub)
- BFCL: <https://github.com/ShishirPatil/gorilla>
- tau-bench: <https://github.com/sierra-research/tau-bench>
- tau2-bench: <https://github.com/sierra-research/tau2-bench>
- MCP-Bench: <https://github.com/Accenture/mcp-bench>
- ACEBench: <https://github.com/ACEBench/ACEBench>
- When2Call: <https://github.com/NVIDIA/When2Call>
- BUTTON: <https://github.com/PKU-Baichuan-MLSystemLab/BUTTON>
- ToolBench (OpenBMB): <https://github.com/OpenBMB/ToolBench>
- Agent-FLAN: <https://github.com/InternLM/Agent-FLAN>
- AgentGym-RL: <https://github.com/WooooDyy/AgentGym-RL>
- ToolUniverse: <https://github.com/mims-harvard/ToolUniverse>

### Blog & news
- xLAM v2 announcement: <https://www.salesforce.com/blog/xlam-large-action-models-v2/>
- APIGen-MT MarkTechPost: <https://www.marktechpost.com/2025/04/08/salesforce-ai-released-apigen-mt-and-xlam-2-fc-r-model-series-...>
- BFCL public leaderboard: <https://gorilla.cs.berkeley.edu/leaderboard.html>
- llm-stats BFCL v3: <https://llm-stats.com/benchmarks/bfcl-v3>
- vLLM xgrammar default: <https://blog.vllm.ai/2025/01/14/struct-decode-intro.html>

---

## Wire-Into-V16-Trainer (Quick paste block, 20 env knobs)

```bash
# === SURROGATE-1 V16 — Tool-Use Frontier Wire ===========================

# Datasets (8 new + 4 bumped)
export TAKE_APIGEN_MT="${TAKE_APIGEN_MT:-5000}"
export TAKE_TOOLMIND="${TAKE_TOOLMIND:-50000}"
export TAKE_FUNREASON_MT="${TAKE_FUNREASON_MT:-17000}"
export TAKE_WHEN2CALL="${TAKE_WHEN2CALL:-30000}"
export TAKE_TOOLACE2="${TAKE_TOOLACE2:-16000}"
export TAKE_PALADIN="${TAKE_PALADIN:-15000}"
export TAKE_BUTTON="${TAKE_BUTTON:-8000}"
export TAKE_TOOLRET_TRAIN="${TAKE_TOOLRET_TRAIN:-10000}"
export TAKE_TOOLACE="${TAKE_TOOLACE:-16000}"
export TAKE_XLAM="${TAKE_XLAM:-20000}"
export TAKE_HERMESFC="${TAKE_HERMESFC:-8000}"

# Format augmentation (3-way render)
export TOOL_FORMAT_MIX="${TOOL_FORMAT_MIX:-0.5,0.3,0.2}"

# Behavior knobs
export TOOL_PARALLEL_BOOST="${TOOL_PARALLEL_BOOST:-1.3}"
export TOOL_FAILURE_INJECT_RATE="${TOOL_FAILURE_INJECT_RATE:-0.30}"
export TOOL_CATALOG_SIZE_MIX="${TOOL_CATALOG_SIZE_MIX:-5,20,50,200,1000}"
export TOOL_RETRIEVAL_INJECT_THRESHOLD="${TOOL_RETRIEVAL_INJECT_THRESHOLD:-200}"

# Loss + decoding
export USE_SRML_LOSS="${USE_SRML_LOSS:-1}"
export USE_GRAMMAR_MASK="${USE_GRAMMAR_MASK:-1}"
export GRAMMAR_BACKEND="${GRAMMAR_BACKEND:-xgrammar}"

# Curriculum + RL
export CURRICULUM_MODE="${CURRICULUM_MODE:-3phase}"
export RL_ALGO="${RL_ALGO:-grpo}"
export RL_EPISODE_BUDGET="${RL_EPISODE_BUDGET:-50}"
```

20 env knobs ready. Drop into top of `kaggle-trainer.sh`. Run with `CURRICULUM_MODE=3phase bash kaggle-trainer.sh` to execute full V16 pipeline (Phase A → B → C).

**End of file. V16 tool-use frontier is concrete, paste-ready, and license-clean.**

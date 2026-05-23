---
title: Surrogate-1 — SOTA Tool-Use & Function-Calling Research (2025-2026)
date: 2026-04-29
tags: [surrogate-1, tool-use, function-calling, agent, BFCL, MCP, hermes, qwen2.5-coder]
status: research-complete
parent: v2-master-plan.md
purpose: Make Surrogate-1 a standalone agent — call tools as well as Claude / GPT-4o / Kimi K2.6
---

# Tool-Use Research for Surrogate-1 v2 (no Hermes orchestrator)

## TL;DR

Surrogate-1 v2 must be a **native** tool-using agent. Goal: hit BFCL v3 ≥ 75 / multi-turn ≥ 50, parity with closed Claude 4 Sonnet at the 7B scale.

**Path**:
1. Train on **Hermes-style `<tool_call>` XML format** — already supported by Qwen2.5-Coder tokenizer; vLLM has a `hermes` parser.
2. Mix **5 high-quality datasets**: Hermes-FC v1, xLAM-60k, APIGen-MT-5k, ToolMind-369k, Toucan-1.5M (sample 80k), When2Call-15k.
3. Train **6 reasoning patterns**: ReAct, Plan-and-Execute, parallel calls, multi-turn, refusal/clarify, code-exec.
4. Add **MCP awareness** (tool registry as system prompt, JSON-RPC discovery).
5. Eval on **BFCL v3/v4 + tau-bench + SWE-Bench-Verified-lite + custom DevSecOps tools**.

**Expected score lift**: BFCL v3 from ~30 (base Qwen2.5-Coder-7B) → ~70-75 (post-FT). Multi-turn from ~20 → ~45-50.

---

## 1. Native tool-use protocols 2025-2026

### 1.1 The four format families

There are essentially **4 formats** in the wild as of 2026-04. Surrogate-1 should target Hermes (most ecosystem support) and emit a JSON-compatible alias for OpenAI users.

| Format | Vendor | Marker | Parser support |
|--------|--------|--------|----------------|
| **Hermes XML** | NousResearch (de-facto standard) | `<tool_call>{...}</tool_call>` | vLLM, SGLang, llama.cpp, LM Studio |
| **OpenAI JSON** | OpenAI | `tool_calls: [{id, function:{name, arguments}}]` | OpenAI SDK, all proxies (LiteLLM) |
| **Anthropic XML+JSON** | Anthropic | `<tool_use>{name, input}</tool_use>` (in content blocks) | Anthropic SDK, LiteLLM passthrough |
| **Qwen2.5-Coder `<tools>`** | QwenLM | `<tools>{...}</tools>` (different from Hermes!) | custom vLLM parser (hanXen) |

**CRITICAL FINDING**: Qwen2.5-Coder's stock chat template ignores Hermes `<tool_call>` even though Qwen2.5 (non-Coder) uses Hermes natively. The Coder variant uses `<tools>` tags. **Solution**: replace tokenizer chat_template with Hermes during fine-tune so post-FT Qwen2.5-Coder-7B emits `<tool_call>` cleanly + works with `vllm serve --tool-call-parser hermes`. (Sources: hanXen/vllm-qwen2.5-coder-tool-parser, qwen.readthedocs.io.)

### 1.2 Hermes XML — the format Surrogate-1 will emit

System prompt (template — embed all tools in `<tools>`):

```
You are a function calling AI model. You are provided with function signatures within
<tools></tools> XML tags. You may call one or more functions to assist with the user query.
Don't make assumptions about what values to plug into functions.

<tools>
[
  {"type": "function", "function": {"name": "get_weather", "description": "Get current weather",
   "parameters": {"type": "object", "properties": {"location": {"type": "string"}},
   "required": ["location"]}}}
]
</tools>

For each function call return a json object with function name and arguments
within <tool_call></tool_call> XML tags as follows:
<tool_call>
{"name": <function-name>, "arguments": <args-dict>}
</tool_call>
```

Single tool call:
```
<tool_call>
{"name": "get_weather", "arguments": {"location": "Bangkok"}}
</tool_call>
```

Parallel tool calls (independent — emit multiple back-to-back):
```
<tool_call>
{"name": "get_weather", "arguments": {"location": "Bangkok"}}
</tool_call>
<tool_call>
{"name": "get_weather", "arguments": {"location": "Tokyo"}}
</tool_call>
```

Tool response (from execution layer, before next assistant turn):
```
<tool_response>
{"name": "get_weather", "content": {"temperature_c": 32, "humidity": 78}}
</tool_response>
```

Why this is the right format for Surrogate-1:
- vLLM has a stable `hermes` parser (tested with Qwen3, just patch the chat_template for Coder variant)
- Tokens `<tool_call>` and `</tool_call>` are added vocabulary tokens in Hermes-tuned tokenizers — easy streaming parse
- `<tool_response>` tag plays well with `function` role conversion in OpenAI-compatible endpoints
- 90% of public datasets (Hermes-FC, xLAM-sharegpt, Toucan, ToolMind) use this exact tag set

### 1.3 Tool choice modes

These three are minimum-viable behaviors. Surrogate-1 must train to honor each:

| Mode | Wire format hint | Behavior to teach |
|------|------------------|-------------------|
| `auto` | (default) | Model decides if to call. Teach to refuse/answer when no tool fits. |
| `any` | `tool_choice: "any"` (Anthropic) / `"required"` (OpenAI) | Always emit ≥1 `<tool_call>`. Used in agents that loop until done. |
| `forced` | `tool_choice: {"type": "function", "name": "..."}` | Emit exactly that tool. Fewer training examples needed; pin via system prompt. |

OpenAI strict mode `parallel_tool_calls: false` is also a must-handle — train ~10% of the data with single-tool constraint (system prompt: "emit exactly one tool call per turn").

### 1.4 Parallel function calling

OpenAI: up to 128 tools per request. Anthropic: up to 64 tools per request. Both support **parallel emission** in a single assistant turn.

Training pattern: Hermes emits multiple `<tool_call>...</tool_call>` blocks back-to-back (no separator) in the assistant message. Examples:

```
<tool_call>{"name": "get_weather", "arguments": {"location": "Bangkok"}}</tool_call>
<tool_call>{"name": "get_weather", "arguments": {"location": "Tokyo"}}</tool_call>
<tool_call>{"name": "get_weather", "arguments": {"location": "London"}}</tool_call>
```

Then the **next user turn** has 3 `<tool_response>` blocks (one per call), in matching order.

This is the **hardest** thing to train — most datasets have <5% parallel examples. Surrogate-1 needs explicit parallel data; xLAM-60k has them (`answers` array with multiple items).

### 1.5 Streaming function calls

For deployment via vLLM:
- Tokens `<tool_call>` and `</tool_call>` are **added tokens** → tokenizer treats them as single tokens, easy to detect on the fly
- Anthropic uses SSE; OpenAI uses delta accumulation (`tool_calls[].function.arguments` chunks concatenated)
- vLLM's hermes parser auto-handles streaming — buffer until close tag, emit OpenAI-format delta

No training change needed for streaming — it's a runtime concern.

### 1.6 Model Context Protocol (MCP) — the open standard

**Status (April 2026)**: MCP was donated by Anthropic to the **Agentic AI Foundation (AAIF)** under Linux Foundation in **December 2025**. Co-founded by Anthropic, Block, OpenAI; supported by Google, Microsoft, AWS, Cloudflare. Latest spec: 2025-11-25. ~10,000 public MCP servers (PulseMCP tracks 8.5k, FastMCP 1.8k+). LiteLLM supports MCP protocol 2025-11-25 since v1.80.18.

Three primitives MCP server exposes:
- **tools** — callable functions (model-controlled)
- **resources** — readable content (file-like, app-controlled)
- **prompts** — reusable templates (user-invoked)

Capability negotiation handshake on connect (`initialize` JSON-RPC).

**For Surrogate-1**: We don't need to make Surrogate-1 a full MCP **client** (LiteLLM can act as a gateway and convert MCP tools to OpenAI-format `tools[]`). What Surrogate-1 needs is:
1. Awareness that tool definitions can come from a **discovery step** ("call `tools/list` first")
2. Ability to handle very long tool lists (50+ tools in system prompt) — train with up to 60 tools per example
3. Tolerance for tool descriptions in JSON-RPC envelope — strip `jsonrpc:"2.0"` wrapping

**Training data implication**: Toucan-1.5M is **synthesized from real MCP servers** — it's the perfect match for MCP-aware training. Use it as the primary multi-turn dataset.

### 1.7 Qwen2.5-Coder-7B compatibility (the gotcha)

From `QwenLM/Qwen3-Coder issue #180` and `Qwen2.5-Coder-7B-Instruct discussion #22`:
- **Base** Qwen2.5-Coder-7B-Instruct: tool calling **does not work reliably**. Hallucinates JSON keys.
- **Fix path A (no FT)**: use `hanXen/vllm-qwen2.5-coder-tool-parser` (custom `<tools>` parser + few-shot system prompt injection). Achieves 100% format compliance on test.
- **Fix path B (FT)**: replace chat_template, fine-tune on Hermes format, push as `axentx/surrogate-1-coder-7b-tool-fc`.

**Surrogate-1 chooses Path B**: fine-tune chat_template + add 100k FC training samples. Result: works with stock vLLM `hermes` parser.

---

## 2. Tool-use training datasets (top 10 for 2026)

### 2.1 The shortlist (curated for Surrogate-1)

| # | Dataset | Size | License | Format | Sample for v2 |
|---|---------|------|---------|--------|---------------|
| 1 | `NousResearch/hermes-function-calling-v1` | 7.93k | Apache-2.0 | ShareGPT (system, human, gpt, tool) | full |
| 2 | `Salesforce/xlam-function-calling-60k` | 60k | CC-BY-4.0 | JSON {query, tools, answers} | 30k filtered |
| 3 | `Salesforce/APIGen-MT-5k` | 5k | CC-BY-4.0 | ShareGPT multi-turn | full |
| 4 | `Nanbeige/ToolMind` | 369k | Apache-2.0 | ShareGPT + tools | 50k diversity-sampled |
| 5 | `Agent-Ark/Toucan-1.5M` | 1.5M | Apache-2.0 | ShareGPT multi-turn from real MCP | 80k from `single-turn-original` + 30k `multi-turn` |
| 6 | `nvidia/When2Call` | 15k SFT + pref | Apache-2.0 | multiple-choice + free | full SFT |
| 7 | `glaiveai/glaive-function-calling-v2` | ~113k | MIT | ChatML w/ `<functioncall>` | 20k (legacy fallback) |
| 8 | `interstellarninja/hermes_reasoning_tool_use` | 51k | Apache-2.0 | ShareGPT + reasoning | 20k |
| 9 | `microsoft/orca-agentinstruct-1M-v1` (tool subset) | ~150k of 1M is tool-related | MIT | mixed | 30k tool subset |
| 10 | `nvidia/Nemotron-SWE-v1` | 59k SWE-Agent traj | Apache-2.0 | OpenHands trajectories | 10k SWE only |

**Total**: ~285k samples across single-turn FC, multi-turn agent, refusal/decision, MCP-derived, and SWE-Bench-style code-edit traces.

### 2.2 Format details for top 5

#### 2.2.1 Hermes-FC v1 (the gold standard)

```json
{
  "id": "f37cd...",
  "conversations": [
    {"from": "system", "value": "You are a function calling AI model... <tools>[...]</tools>"},
    {"from": "human", "value": "Fetch the stock fundamentals data for Tesla (TSLA)"},
    {"from": "gpt", "value": "<tool_call>\n{\"name\": \"get_stock_fundamentals\", \"arguments\": {\"symbol\": \"TSLA\"}}\n</tool_call>"},
    {"from": "tool", "value": "<tool_response>\n{\"name\": \"get_stock_fundamentals\", \"content\": {\"symbol\": \"TSLA\", ...}}\n</tool_response>"},
    {"from": "gpt", "value": "Tesla's PE ratio is 49.6..."}
  ],
  "category": "Finance",
  "subcategory": "Stock Analysis",
  "task": "Fundamental Analysis"
}
```

5 sub-datasets inside:
- `func_calling_singleturn`
- `func_calling` (multi-turn)
- `glaive_func_calling` (cleaned 5k Glaive)
- `json_mode_singleturn`
- `json_mode_agentic`

Updated 2025-02-07.

#### 2.2.2 xLAM-60k (Salesforce APIGen)

JSON format (raw):
```json
{
  "query": "Find sum of multiples of 3,5 in [1,1000] and product of first 5 primes.",
  "tools": [
    {"name": "math_toolkit.sum_of_multiples", "description": "...", 
     "parameters": {"lower_limit": {"type": "int", "description": "...", "required": true}, ...}},
    {"name": "math_toolkit.product_of_primes", "description": "...", 
     "parameters": {"count": {"type": "int", "description": "...", "required": true}}}
  ],
  "answers": [
    {"name": "math_toolkit.sum_of_multiples", "arguments": {"lower_limit": 1, "upper_limit": 1000, "multiples": [3,5]}},
    {"name": "math_toolkit.product_of_primes", "arguments": {"count": 5}}
  ]
}
```

**Strengths**: 3,673 unique APIs across 21 categories; 3-stage verification (format + execution + semantic); ~95% correct rate.
**Weaknesses**: Single-turn only; no `<tool_call>` markup — needs conversion.

xlam-2 / APIGen-MT-5k extends to multi-turn with simulated user-agent dialogues.

#### 2.2.3 ToolMind-369k

Schema:
```json
{
  "conversations": [
    {"role": "user", "content": "What's the time in NYC?"},
    {"role": "assistant", "content": "...", 
     "tool_calls": [{"function": {"name": "get_current_time", 
                                   "arguments": {"timezone": "America/New_York"}}}]}
  ],
  "tools": [{"type": "function", "function": {"name": "get_current_time", 
            "description": "...", "parameters": {...}}}]
}
```

**Splits**: `graph_syn_datasets` (163k, synthesized via function correlation graph), `open_datasets` (205k from xlam + 6 others). 
**Filter**: turn-level + trajectory-level.
**Lift**: Qwen3-8B FT on ToolMind: +4.69% overall; Qwen3-14B: +5.40%.

#### 2.2.4 Toucan-1.5M (the MCP-grounded one)

Subsets: `single-turn-original` (28.3k), `single-turn-diversify` (15.8k), `irrelevant` (40k, refusal), `multi-turn` (35.2k), plus 1.4M+ raw trajectories from 3 teacher models (Qwen3-32B, Kimi-K2, GPT-OSS-120B) × 2 frameworks.

Columns:
- `messages` (string, chat-formatted with Hermes tools in system prompt)
- `available_tools` (string, JSON list)
- `target_tools` (string, e.g., `Server_Name::Tool_Name`)
- `subset_name` (`single-turn-original` | `irrelevant` | `single-turn-diversify` | `multi-turn`)

**For Surrogate-1**: prefer the `Kimi-K2` subset (519k) because Kimi K2 is the strongest tool-use teacher in the open. Sample 50k single-turn + 30k multi-turn = 80k.

#### 2.2.5 When2Call (the refusal/clarify dataset)

NVIDIA, NAACL 2025. 15k SFT + DPO pref pairs. 4 behaviors:
1. Generate a tool call
2. Ask follow-up question
3. Admit unable to answer
4. Answer directly (test: should be marked wrong since all questions require tools)

**Critical**: This is the single best dataset to fix the most common failure mode (model hallucinating arguments instead of asking) — most other datasets only have positive examples. Result with DPO on When2Call > SFT alone.

### 2.3 Specific arXiv papers + recipes

| Paper | arXiv | Year | Take-away |
|-------|-------|------|-----------|
| ToolLLM | 2307.16789 | 2023 | DFSDT > ReAct for trajectory annotation; 16k APIs |
| Toolformer | 2302.04761 | 2023 | Self-supervised: model writes its own training data |
| Gorilla / APIBench | 2305.15334 | 2023 | RLHF on retriever-aware tool use |
| Granite-FC | 2407.00121 | 2024 | 7-task multi-task training for FC; QLoRA r=8/α=32 worked |
| APIGen | 2406.18518 | 2024 | 3-stage verification (format/exec/semantic) for synthesis |
| APIGen-MT | 2504.03601 | 2025 | Two-phase: blueprint → simulated dialogue; SOTA on tau-bench |
| Hermes 4 Tech Report | (Aug 2025) | 2025 | ChatML + `<tool_call>` tags as added tokens |
| Toucan | 2510.01179 | 2025 | 1.5M trajectories from 495 real MCP servers |
| ToolMind | 2511.15718 | 2025 | 360k samples; +5.40% on Qwen3-14B |
| When2Call | 2504.18851 | 2025 | DPO > SFT for refusal/clarify decisions |
| Kimi K2 Tech | 2507.20534 | 2025 | Stable tool use across 200-300 sequential calls |

---

## 3. Reasoning frameworks built INTO the model

The agent runtime should not orchestrate — the model itself does. Train these 6 patterns:

### 3.1 ReAct (Reason + Act) — baseline pattern

Pattern: `Thought: ... | Action: <tool_call> | Observation: <tool_response>` cycles.

Training row example:
```json
{"messages": [
  {"role": "system", "content": "You are a ReAct agent. <tools>[...]</tools>"},
  {"role": "user", "content": "What's the population density of Tokyo?"},
  {"role": "assistant", "content": "Thought: I need population and area of Tokyo separately, then divide.\n<tool_call>\n{\"name\": \"get_population\", \"arguments\": {\"city\": \"Tokyo\"}}\n</tool_call>"},
  {"role": "tool", "content": "<tool_response>\n{\"name\": \"get_population\", \"content\": 13960000}\n</tool_response>"},
  {"role": "assistant", "content": "Thought: Now I need the area.\n<tool_call>\n{\"name\": \"get_area\", \"arguments\": {\"city\": \"Tokyo\"}}\n</tool_call>"},
  {"role": "tool", "content": "<tool_response>\n{\"name\": \"get_area\", \"content\": 2194}\n</tool_response>"},
  {"role": "assistant", "content": "Thought: 13,960,000 / 2,194 ≈ 6,363/km².\nFinal answer: Tokyo's population density is approximately 6,363 people per km²."}
]}
```

**Source**: Yao et al., 2022, arXiv 2210.03629.

### 3.2 Plan-and-Execute — for long-horizon tasks

Pattern: explicit `plan` block listing all steps before executing any. Used by LangChain Deep Agents (March 2026).

Training prompt addition:
```
Before any tool calls, output a `<plan>` block listing all steps. Then execute one step at a time.
```

Example:
```
<plan>
1. get_population(Tokyo)
2. get_area(Tokyo)
3. divide step1 / step2
</plan>
<tool_call>{"name": "get_population", "arguments": {"city": "Tokyo"}}</tool_call>
```

**Why important**: ReAct degrades over long sequences (>10 turns). Plan-and-Execute keeps full task in working memory. Toucan-1.5M `multi-turn` subset has many examples.

### 3.3 Toolformer — self-taught (skip for v2, info for v3)

Self-supervised: model annotates raw text with synthetic API calls, filters by perplexity drop. **Not for v2** — needs heavy compute. Use as v3 evolution: have v2 model generate v3's tool training data (loop).

### 3.4 Self-Ask — decompose questions into sub-questions

Pattern:
```
Are there follow-up questions?: Yes
Follow-up: Who founded SpaceX?
<tool_call>{"name": "search", "arguments": {"q": "SpaceX founder"}}</tool_call>
<tool_response>{"content": "Elon Musk"}</tool_response>
Follow-up: When was Elon Musk born?
...
Final answer: 1971
```

Encode as ChatML: each Follow-up = new assistant turn. Improves multi-hop QA.

### 3.5 Chain-of-Tools — composition

Pattern: output of tool A → input of tool B in same turn. Hard for small models.

```
<tool_call>{"name": "get_user_id", "arguments": {"email": "x@y.com"}}</tool_call>
<tool_response>{"content": {"user_id": 12345}}</tool_response>
<tool_call>{"name": "get_orders", "arguments": {"user_id": 12345}}</tool_call>
```

Toucan multi-turn + xLAM-60k both have this pattern. Mark in metadata as `chain_depth: 3`.

### 3.6 Refusal / clarification (trained via When2Call DPO)

When tools are inadequate, model must say so or ask follow-up. Without training, model **hallucinates plausible-looking arguments**.

Training pair (DPO):
- **Chosen**: "I don't have a tool to query private databases. Could you provide the data directly?"
- **Rejected**: `<tool_call>{"name": "search", "arguments": {"q": "private DB query"}}</tool_call>` (using wrong tool)

---

## 4. Benchmarks for tool use (2026)

### 4.1 BFCL (Berkeley Function Calling Leaderboard) — the primary

Versions:
- **v1** (2024): AST evaluation, single-turn
- **v2** (2024): enterprise + community functions
- **v3** (Sep 2024): **multi-turn + multi-step** (1000 test cases, 4 domains: vehicle, trading, travel, file system)
- **v4** (2025): **agentic** (web search, memory, format sensitivity)

Top scores as of April 2026 (BFCL v3 overall):
- GLM-4.5 (Zhipu): 0.778 (best open)
- Claude Opus 4.1: 70.36% on agentic
- Claude Sonnet 4: 70.29%
- Llama 3.1 405B Instruct: 0.885 on legacy v1 metric
- Average across all models: 0.717

**For Surrogate-1**: target BFCL v3 ≥ 70 overall, multi-turn ≥ 50. Realistic for 7B post-FT.

Run command:
```bash
git clone https://github.com/ShishirPatil/gorilla
cd gorilla/berkeley-function-call-leaderboard
pip install -e .
bfcl generate --model Qwen/Qwen2.5-Coder-7B-Instruct --test-category all
bfcl evaluate --model Qwen/Qwen2.5-Coder-7B-Instruct --test-category all
```

After FT:
```bash
bfcl generate --model axentx/surrogate-1-coder-7b-tool-fc --test-category all
```

### 4.2 tau-bench / tau2-bench / tau3-bench

- **tau-bench** (Sierra, June 2024): 2 domains (retail, airline), simulated user
- **tau2-bench** (June 2025): + telecom domain, dual-control (user + agent both have tools)
- **tau3-bench** (early 2026): + banking, voice modality, 75+ task fixes

**Pass^k metric**: probability of consistent success across k trials. Even GPT-4o pass^8 < 25% on retail.

For Surrogate-1: target pass^1 ≥ 30 on tau-retail. Stretch: pass^4 ≥ 15.

### 4.3 ACEBench, MCP-Universe, TRAJECT-Bench

- **ACEBench**: Chinese-leaning function calling. Kimi K2 = 76.5
- **MCP-Universe**: end-to-end on real MCP servers. Toucan-trained models lead Pareto frontier
- **TRAJECT-Bench** (Oct 2025): trajectory-aware, models degrade past 7 tools → critical for Surrogate-1

### 4.4 SWE-Bench Verified (code-edit agent benchmark)

For agentic SWE specifically. Top scores:
- Kimi K2: 65.8 (non-thinking)
- Kimi-Dev (agentless): 60.4
- SWE-Gym 32B + OpenHands: ~33 (open small)
- Claude 4 Sonnet: 72%+

For Surrogate-1: target SWE-Bench-Verified-lite ≥ 15 (lite = 300 task subset). Below that, defer SWE work to v3.

---

## 5. MCP integration plan for Surrogate-1

Surrogate-1 will NOT be a full MCP client SDK (LiteLLM does that). Instead, train these 3 capabilities:

### 5.1 Tool registry awareness (system prompt with 50+ tools)

Train rows where `<tools>` block has 30-60 tools. Most current data has 1-5. Use Toucan's `available_tools` field which contains the full MCP server's tool list.

```
<tools>
[
  {"type":"function","function":{"name":"github_create_issue", ...}},
  {"type":"function","function":{"name":"github_list_repos", ...}},
  {"type":"function","function":{"name":"slack_send_message", ...}},
  ... 47 more ...
]
</tools>
```

Critical metric: **tool retrieval recall@1** in 50-tool corpus ≥ 85%. Current Qwen2.5-Coder-7B base ~ 50%.

### 5.2 Discover-then-call (2-stage)

Pattern: model first calls `tools/list` (or `discover_tools`) to see what's available, then uses returned schema. Train ~5% of data with this pattern.

```
<tool_call>{"name": "discover_tools", "arguments": {"query": "weather"}}</tool_call>
<tool_response>{"tools": [{"name": "get_weather", "description": "..."}]}</tool_response>
<tool_call>{"name": "get_weather", "arguments": {"location": "Bangkok"}}</tool_call>
```

### 5.3 JSON-RPC envelope tolerance

When MCP gateway wraps tool calls in JSON-RPC, model gets:
```
<tool_response>
{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"...result..."}]}}
</tool_response>
```

Train model to read both `result.content[].text` (MCP envelope) AND plain `content` (no envelope). Currently models break on the envelope.

---

## 6. Code execution as a tool

### 6.1 The 3 essential tools to teach

| Tool name | What it does | Sandbox |
|-----------|--------------|---------|
| `python_exec` | Run arbitrary Python in sandbox, return stdout/stderr | E2B / Modal / firejail |
| `bash_exec` | Run shell command (read-only by default) | firejail / docker |
| `file_write` | Write file to ephemeral workspace | tmpfs |
| `file_read` | Read file (also from /code/) | filesystem readonly |
| `grep` | Search codebase | ripgrep |

These 5 cover ~90% of agent-style tasks (Devin, Manus, Aider, OpenHands all converge here).

### 6.2 Training: WHEN to use python_exec vs other tools

Decision rule (encode in training data):
- Math/data analysis → `python_exec`
- File modification → `file_write` (precise) or `python_exec` (heuristic edits)
- Code search → `grep` first, NOT `python_exec` (cheaper)
- Shell operations (test runner, git) → `bash_exec`
- Simple lookups (dictionary, list) → answer directly, NO tool

Training data sources:
- `nvidia/Nemotron-SWE-v1` — 59k OpenHands trajectories, real tool use across `python_exec`/`file_*`/`grep`
- `SWE-Gym/OpenHands-Sampled-Trajectories` — 2.4k success trajectories (smaller but high-quality)
- `nebius/SWE-rebench-openhands-trajectories` — 67k trajectories (Dec 2025)

Sample row (Nemotron-SWE-v1 style):
```json
{"messages": [
  {"role": "system", "content": "You are a SWE agent. Tools: python_exec, file_read, file_write, grep, bash_exec."},
  {"role": "user", "content": "Fix the bug in src/auth.py — login fails for emails with '+' in them."},
  {"role": "assistant", "content": "I'll first locate the relevant code.\n<tool_call>{\"name\": \"grep\", \"arguments\": {\"pattern\": \"def login\", \"path\": \"src/\"}}</tool_call>"},
  {"role": "tool", "content": "<tool_response>{\"result\": \"src/auth.py:42:def login(email: str, password: str):\"}</tool_response>"},
  {"role": "assistant", "content": "<tool_call>{\"name\": \"file_read\", \"arguments\": {\"path\": \"src/auth.py\", \"line_start\": 40, \"line_end\": 60}}</tool_call>"},
  {"role": "tool", "content": "<tool_response>{\"result\": \"40: def login(email: str, password: str):\\n41:     # ...\\n42:     email = urllib.parse.unquote(email)\\n...\"}</tool_response>"},
  {"role": "assistant", "content": "Found it — line 42 unescapes URL-encoded '+' to space. Fix:\n<tool_call>{\"name\": \"file_write\", \"arguments\": {\"path\": \"src/auth.py\", \"old\": \"email = urllib.parse.unquote(email)\", \"new\": \"email = urllib.parse.unquote_plus(email)\"}}</tool_call>"},
  {"role": "tool", "content": "<tool_response>{\"result\": \"OK, 1 replacement\"}</tool_response>"},
  {"role": "assistant", "content": "<tool_call>{\"name\": \"bash_exec\", \"arguments\": {\"cmd\": \"pytest tests/test_auth.py::test_login_email_plus\"}}</tool_call>"},
  {"role": "tool", "content": "<tool_response>{\"result\": \"PASSED\"}</tool_response>"},
  {"role": "assistant", "content": "Fix complete. The bug was using unquote() instead of unquote_plus()."}
]}
```

### 6.3 Sandbox options for inference (no training change)

| Option | Cost | Latency | Notes |
|--------|------|---------|-------|
| **E2B Code Interpreter** | $0.05/vCPU-hr | ~200ms cold | Firecracker microVM, prod-ready |
| **Modal sandboxes** | pay-per-second | ~500ms cold | Python-native |
| **Local firejail** | $0 | ~10ms | Linux only, less isolation |
| **OpenAI Code Interpreter via API** | included in OpenAI quota | varies | Vendor-locked |
| **Bedrock AgentCore Code Interpreter** | AWS pricing | varies | Useful for axentx infra |

For Surrogate-1 dev/QA: **E2B free tier** (100 hr/mo). Prod: **firejail on training compute** (free).

---

## 7. Multi-step tool calls — trajectory training

### 7.1 SWE-Agent / OpenHands trajectories

The 3 best public datasets:

| Dataset | Size | Teacher model | Notes |
|---------|------|---------------|-------|
| `nvidia/Nemotron-SWE-v1` | 59k | Qwen3-Coder-480B-A35B | OpenHands + SWE-Agent + Mini-SWE-Agent |
| `nebius/SWE-rebench-openhands-trajectories` | 67k | mixed | Dec 2025, agent-tested |
| `SWE-Gym/OpenHands-Sampled-Trajectories` | 2.4k | GPT-4o + Claude 3.5 Sonnet | high-quality, ICML 2025 |

**ROI check**: SWE-Gym showed +14% absolute on SWE-Bench Verified with just 491 success trajectories. **A few hundred high-quality multi-step trajectories outweigh 100k single-turn**. Surrogate-1 v2 should include the full SWE-Gym (2.4k) + ~5k cherry-picked Nemotron-SWE-v1.

### 7.2 How to teach planning + retry + recovery

Training data must include **failure recovery patterns**, not just success:

```json
{"messages": [
  {"role": "user", "content": "Read config.json"},
  {"role": "assistant", "content": "<tool_call>{\"name\": \"file_read\", \"arguments\": {\"path\": \"config.json\"}}</tool_call>"},
  {"role": "tool", "content": "<tool_response>{\"error\": \"FileNotFoundError: config.json\"}</tool_response>"},
  {"role": "assistant", "content": "File not at root. Searching:\n<tool_call>{\"name\": \"grep\", \"arguments\": {\"pattern\": \"config.json\", \"path\": \".\"}}</tool_call>"},
  {"role": "tool", "content": "<tool_response>{\"result\": \"./src/config/config.json\"}</tool_response>"},
  {"role": "assistant", "content": "<tool_call>{\"name\": \"file_read\", \"arguments\": {\"path\": \"src/config/config.json\"}}</tool_call>"}
]}
```

Toucan-1.5M includes "trajectories where tool calls fail" as feature (not bug). This is **gold** for training error recovery — ~30% of v2's multi-turn data should have ≥1 failed tool call followed by recovery.

### 7.3 Trajectory length distribution

Train mix:
- 1-turn (simple FC): 40%
- 2-3 turn (chain): 30%
- 4-7 turn (medium): 20%
- 8-15 turn (long agent): 8%
- 15+ turn (true agentic): 2%

Sample-pack to 8192 ctx (matches v2 master plan). Long trajectories truncate; train with mid-truncation strategy (keep system + first 2 + last 5 turns).

---

## 8. Surrogate-1 v2 — concrete training data construction

### 8.1 The recipe — 100k samples

```
30k Hermes-FC v1 (full)          — gold-standard format
20k xLAM-60k (filtered)          — diverse APIs, parallel calls
20k Toucan-1.5M (Kimi-K2 subset) — MCP-grounded, multi-turn
15k When2Call SFT                 — refusal/clarify
10k ToolMind (graph_syn)          — reasoning chains
 5k Nemotron-SWE-v1 (cherry)      — code-exec trajectories
 2k SWE-Gym (full)                — high-quality SWE
==
~102k samples, ~85% Hermes XML format already, 15% needs conversion
```

### 8.2 Conversion pipeline

```python
# bin/lib/tool_use_converter.py
from datasets import load_dataset
import json

def to_hermes_format(record, source: str) -> dict:
    """Convert any tool-use dataset row to canonical Hermes ShareGPT."""
    if source == "xlam":
        # xlam: {query, tools, answers}
        tools = json.loads(record["tools"]) if isinstance(record["tools"], str) else record["tools"]
        answers = json.loads(record["answers"]) if isinstance(record["answers"], str) else record["answers"]
        
        sys_msg = (
            "You are a function calling AI model. Function signatures within "
            "<tools></tools>:\n<tools>\n" + 
            json.dumps([{"type": "function", "function": t} for t in tools], indent=2) +
            "\n</tools>\nReturn calls within <tool_call></tool_call>."
        )
        
        # Concatenate parallel calls
        tool_calls = "\n".join(
            f"<tool_call>\n{json.dumps(a)}\n</tool_call>" for a in answers
        )
        
        return {
            "conversations": [
                {"from": "system", "value": sys_msg},
                {"from": "human", "value": record["query"]},
                {"from": "gpt", "value": tool_calls},
            ]
        }
    
    elif source == "hermes_fc_v1":
        return record  # already in target format
    
    elif source == "toolmind":
        # ToolMind uses OpenAI roles + tool_calls field
        convs = []
        for msg in record["conversations"]:
            if msg["role"] == "user":
                convs.append({"from": "human", "value": msg["content"]})
            elif msg["role"] == "assistant":
                if msg.get("tool_calls"):
                    parts = []
                    if msg.get("content"):
                        parts.append(msg["content"])
                    for tc in msg["tool_calls"]:
                        parts.append(
                            f"<tool_call>\n{json.dumps(tc['function'])}\n</tool_call>"
                        )
                    convs.append({"from": "gpt", "value": "\n".join(parts)})
                else:
                    convs.append({"from": "gpt", "value": msg["content"]})
            elif msg["role"] == "tool":
                convs.append({
                    "from": "tool",
                    "value": f"<tool_response>\n{msg['content']}\n</tool_response>"
                })
        # Prepend system with tools
        sys_msg = "You are a function calling AI model. <tools>" + \
                  json.dumps(record["tools"]) + "</tools>"
        convs.insert(0, {"from": "system", "value": sys_msg})
        return {"conversations": convs}
    
    elif source == "toucan":
        # Already Hermes-formatted in messages field
        return {"conversations": parse_toucan_messages(record["messages"])}
    
    elif source == "when2call":
        # Multiple choice + free response — convert MC to "ask follow-up" pattern
        ...
    
    raise ValueError(f"unknown source {source}")
```

### 8.3 Augmentation: synthetic parallel-call examples

Most datasets are <5% parallel. Augment by combining 2 single-turn rows with disjoint tools:

```python
def synthesize_parallel(row_a, row_b):
    # Combine queries, merge tools, emit both calls in one turn
    return {
        "conversations": [
            {"from": "system", "value": merge_tools(row_a.system, row_b.system)},
            {"from": "human", "value": f"{row_a.query} Also, {row_b.query}"},
            {"from": "gpt", "value": row_a.tool_call + "\n" + row_b.tool_call}
        ]
    }
```

Generate 5k synthetic parallel rows. Target: 10% of training data has parallel calls.

---

## 9. Axolotl config snippets

### 9.1 Stage 1.5: Tool-use SFT (insert between v2's stage 1 SFT and stage 2 DPO)

```yaml
# axolotl-config/stage1_5-tool-fc.yml
# Run AFTER stage 1 SFT (general code), BEFORE stage 2 DPO (general code)
base_model: axentx/surrogate-1-coder-7b-v2-sft   # output of v2 stage 1
model_type: AutoModelForCausalLM
adapter: lora
load_in_4bit: true

# CRITICAL: replace chat_template to use Hermes XML
chat_template: chatml
tokens:                                                # add Hermes special tokens
  - "<tool_call>"
  - "</tool_call>"
  - "<tool_response>"
  - "</tool_response>"

datasets:
  - path: data/v2-tool-use-100k.jsonl                  # the merged dataset above
    type: chat_template
    field_messages: conversations
    message_property_mappings:
      role: from
      content: value
    roles:
      user: ["human", "user"]
      assistant: ["gpt", "assistant"]
      system: ["system"]
      tool: ["tool"]                                    # tool role recognized

# LoRA settings inherit from v2 stage 1 (all-linear + DoRA)
lora_r: 32
lora_alpha: 64
lora_dropout: 0.05
peft_use_dora: true
lora_target_modules:
  - q_proj
  - k_proj
  - v_proj
  - o_proj
  - gate_proj
  - up_proj
  - down_proj

sequence_len: 8192                                      # YaRN 4× from v2
sample_packing: true
rope_theta: 1000000.0
rope_scaling:
  type: yarn
  factor: 4.0
  original_max_position_embeddings: 32768

num_epochs: 2                                           # less than stage 1 — narrower task
micro_batch_size: 2
gradient_accumulation_steps: 4
learning_rate: 1.0e-4                                   # half of stage 1 (preserve general code)
lr_scheduler: cosine
warmup_ratio: 0.05
optimizer: adamw_torch_fused
bf16: true
gradient_checkpointing: true
flash_attention: true

hub_model_id: axentx/surrogate-1-coder-7b-v2-tool-fc
hub_strategy: every_save
push_to_hub: true

# Save tokenizer with new tokens
save_safetensors: true
output_dir: ./outputs/v2-tool-fc
```

### 9.2 Stage 2.5: When2Call DPO (between stage 2 DPO general and v2 release)

```yaml
# axolotl-config/stage2_5-when2call-dpo.yml
base_model: axentx/surrogate-1-coder-7b-v2-tool-fc
adapter: lora

rl: dpo
rl_beta: 0.1
dpo_loss_type: sigmoid

datasets:
  - path: nvidia/When2Call
    name: train_pref                                    # the pref subset
    type: dpo
    field_chosen: chosen
    field_rejected: rejected

learning_rate: 5.0e-6                                   # very low for DPO
lr_scheduler: constant
num_epochs: 1
warmup_ratio: 0.0
sequence_len: 4096                                      # shorter — pref pairs

hub_model_id: axentx/surrogate-1-coder-7b-v2-final
```

### 9.3 Inference (vLLM with Hermes parser, post-FT)

```bash
# After merging LoRA → full weights for vLLM
python -m axolotl.cli.merge_lora axolotl-config/stage2_5-when2call-dpo.yml --output_dir ./merged

# Serve with hermes parser (works because we trained Hermes format)
vllm serve ./merged \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.9
```

---

## 10. Expected score lift

Conservative estimate (Surrogate-1 v2 = Qwen2.5-Coder-7B + LoRA on 100k tool-use SFT + When2Call DPO):

| Benchmark | Base Qwen2.5-Coder-7B | Surrogate-1 v2 (target) | Notes |
|-----------|-----------------------|-------------------------|-------|
| BFCL v1 (single-turn AST) | ~30 (broken FC) | 80-85 | Format alone fixes ~50pt |
| BFCL v3 overall | ~25 | 70-75 | Multi-turn lift via Toucan |
| BFCL v3 multi-turn | ~15 | 45-50 | Realistic for 7B |
| BFCL v4 agentic | ~10 | 35-45 | Hard, but Toucan + SWE-Gym helps |
| tau-bench retail | ~5 | 25-30 | tau is brutal |
| tau-bench airline | ~5 | 20-25 | even harder |
| ACEBench (En) | ~30 | 60-65 | xLAM diverse APIs help |
| MCP-Universe | unmeasured | Pareto frontier (per Toucan paper) | new bench |
| When2Call | ~40 | 75-80 | DPO directly targets this |
| SWE-Bench-Verified-lite | ~3 | 10-15 | small budget on SWE data |
| HumanEval+ | 88.4 (base) | ≥85 | hold (no regression) |
| LiveCodeBench v6 | ~37 | ≥42 (v2 main goal) | code held by stage 1 SFT |

**Risk**: 7B may cap at ~70 BFCL v3 even with perfect data. If we plateau, escalation = Qwen3-Coder-30B-A3B base (v2.5).

---

## 11. References (arXiv + repo URLs)

Format: `Paper / Repo — arXiv | URL`

- ToolLLM — 2307.16789 | https://github.com/OpenBMB/ToolBench
- Toolformer — 2302.04761
- Gorilla / APIBench — 2305.15334 | https://gorilla.cs.berkeley.edu
- BFCL paper — OpenReview 2GmDdhBdDk (ICML 2025) | https://gorilla.cs.berkeley.edu/leaderboard.html
- Granite-FC — 2407.00121 (EMNLP 2024 Industry)
- APIGen — 2406.18518
- APIGen-MT — 2504.03601
- ReAct — 2210.03629
- Plan-and-Execute — LangChain blog (https://blog.langchain.com/planning-agents/)
- A3T (ReAct + ActRe) — 2403.14589
- Hermes 3 — 2408.11857
- Hermes 4 Tech Report — Aug 2025 (nousresearch.com)
- Toucan — 2510.01179 | https://huggingface.co/datasets/Agent-Ark/Toucan-1.5M
- ToolMind — 2511.15718 | https://huggingface.co/datasets/Nanbeige/ToolMind
- When2Call — 2504.18851 (NAACL 2025) | https://github.com/NVIDIA/When2Call
- tau-bench — 2406.12045 | https://github.com/sierra-research/tau-bench
- tau2-bench — 2506.07982 | https://github.com/sierra-research/tau2-bench
- Kimi K2 Tech — 2507.20534
- TRAJECT-Bench — 2510.04550
- T1 dataset — 2505.16986
- AgentTrek — https://agenttrek.github.io/
- SWE-Gym — 2412.21139 (ICML 2025) | https://github.com/swe-gym/swe-gym
- Kimi-Dev — 2509.23045
- MCP spec — https://modelcontextprotocol.io/specification/2025-11-25
- Hermes-FC v1 — https://huggingface.co/datasets/NousResearch/hermes-function-calling-v1
- xLAM-60k — https://huggingface.co/datasets/Salesforce/xlam-function-calling-60k
- xLAM github — https://github.com/SalesforceAIResearch/xLAM
- Glaive FC v2 — https://huggingface.co/datasets/glaiveai/glaive-function-calling-v2
- Nemotron-SWE-v1 — https://huggingface.co/datasets/nvidia/Nemotron-SWE-v1
- SWE-rebench OH traj — https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories
- Orca-AgentInstruct — https://huggingface.co/datasets/microsoft/orca-agentinstruct-1M-v1
- vLLM Qwen2.5-Coder tool parser — https://github.com/hanXen/vllm-qwen2.5-coder-tool-parser
- LiteLLM MCP docs — https://docs.litellm.ai/docs/mcp

---

## 12. Open questions / risks

1. **Does Hermes XML degrade Qwen2.5-Coder's code generation ability?** Stage 1 SFT (general code) must precede stage 1.5 (tool FC) so code knowledge is locked in first. If LCB v6 drops >3pp after stage 1.5, abort and retry with lower LR.

2. **MCP envelope handling — does training data have it?** Toucan-1.5M is MCP-grounded but the messages field shows pre-stripped tool responses. Need to add 5-10k synthetic examples WITH the JSON-RPC envelope so model learns to peel it.

3. **Tool retrieval at scale (100+ tools)** — Surrogate-1 may need a lightweight retriever (BGE-M3) to pre-filter tools before stuffing in system prompt. Defer to v3 (out of v2 scope).

4. **Format compatibility with axentx infra** — does Hermes parser work with our LiteLLM gateway? Test pre-train with stub tool-FC LoRA on 1k samples → verify e2e through gateway → then commit to full FT.

5. **Streaming via vLLM** — `<tool_call>` is added token, but only if we add it during FT (axolotl `tokens:` field). Verify tokenizer.json after merge to confirm. If not added, stream parsing breaks mid-tag.

---

## 13. Out-of-scope (defer to v3)

- **Toolformer self-supervised expansion** — let v2 generate v3's data
- **GRPO + sandbox rewards** (Prowler/Trivy graders for DevSecOps tools) — v3 plan
- **Browser-use / Computer-use** trajectories (Fara-7B / Manus / Claude in Chrome) — v3, requires VM infra
- **Full MCP client SDK in model** — LiteLLM gateway is enough for v2
- **Multi-modal tool calls** (image as tool input) — out of scope for code-focused agent
- **Reasoning models (DeepSeek R1-style think tags)** — defer; thinking tokens × tool tokens = trajectory bloat
- **xlam-2 7B-FC-r weights as starting point** — possible alt base, but loses Qwen2.5-Coder's HumanEval 88.4 strength

---

## End of file

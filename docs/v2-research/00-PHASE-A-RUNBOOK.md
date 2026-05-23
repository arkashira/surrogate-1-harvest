# Surrogate-1 v2 — Phase A Runbook (Ready to Execute)

**Goal**: Ship `axentx/surrogate-1-coder-7b-v2-mvp` in 4 weeks, $400 cash, free Lightning H200.

---

## Pre-flight Checklist

### Account/billing
- [ ] HF PRO subscribed (`surrogate1` user, $9/mo)
- [ ] Wasabi S3-compatible bucket created (`axentx-surrogate-1-data`, ~$6/mo)
- [ ] Lightning ashirapit verified (web onboarding) OR ashiradevops quota refreshed (next month)
- [ ] Anthropic API credit ≥$300 (for synth orchestrator traces)

### Infrastructure
- [ ] Sanitizer deployed (commit 1dfdc54 — DONE)
- [ ] Sanitizer also runs on Mac for any local data prep
- [ ] Wasabi access keys saved in ~/.hermes/.env as `WASABI_ACCESS_KEY` + `WASABI_SECRET`
- [ ] HF dataset repo `axentx/surrogate-1-v2-train` created (private)

---

## Day-by-Day Execution

### Week 1 — Data Pipeline

**Day 1**: Mirror datasets → Wasabi
```bash
# On HF Space (NOT on Mac), use existing dataset-mirror.sh + new sanitizer:
SOURCES=(
    "microsoft/rStar-Coder|30000"
    "nvidia/OpenCodeReasoning-2|20000"
    "nvidia/OpenCodeInstruct|10000"        # filter avg_test_score>=0.7
    "inclusionAI/Ling-Coder-SFT|10000"
    "OpenCoder-LLM/opc-sft-stage1|5000"
    "OpenCoder-LLM/opc-sft-stage2|5000"
    "bigcode/self-oss-instruct-sc2-exec-filter-50k|50000"
    "m-a-p/CodeFeedback-Filtered-Instruction|10000"
)
# Use HF Space because mirror needs HF API which gets rate limited from Mac
# Output: Wasabi bucket axentx-surrogate-1-data/raw/<source>/
```

**Day 2-3**: Tool-use datasets
```bash
TOOL_SOURCES=(
    "NousResearch/hermes-function-calling-v1|7930"
    "Salesforce/xlam-function-calling-60k|30000"
    "Agent-Ark/Toucan-1.5M|80000"           # Kimi-K2 subset
    "nvidia/When2Call|15000"
    "Nanbeige/ToolMind|10000"
    "nvidia/Nemotron-SWE-v1|5000"
    "SWE-Gym/OpenHands-Sampled-Trajectories|2400"
)
# Convert all → Hermes XML format (chat_template <tool_call>...</tool_call>)
```

**Day 4**: Multi-agent datasets
```bash
AGENT_SOURCES=(
    "lambda/hermes-agent-reasoning-traces|14000"
    "nebius/SWE-agent-trajectories|5000"
    "SWE-Gym/SWE-Gym|400"                   # successful only
    "microsoft/orca-agentinstruct-1M-v1|1500"
)
```

**Day 5**: Synthesize 500 orchestrator→subagent traces
```python
# Use Anthropic API (Claude Opus 4 + Sonnet 4) — ~$200
import anthropic
client = anthropic.Anthropic()
SCENARIOS = [load 500 startup tasks]
for scenario in SCENARIOS:
    # Step 1: Opus generates orchestrator plan with subagent spawns
    plan = opus.create(...)
    # Step 2: Sonnet plays each subagent with own context
    subagent_outputs = [sonnet.create(...) for s in plan.subagents]
    # Step 3: Opus aggregates results
    final = opus.create(...)
    # Save trajectory in ChatML format
```

**Day 6**: DPO data construction
```bash
DPO_SOURCES=(
    "Vezora/Code-Preference-Pairs|55000"           # bug/no-bug
    "argilla/distilabel-capybara-dpo-7k-binarized|7000"
    "nvidia/When2Call|15000"                       # train_pref
)
# Plus: rejection-sampled exec-graded
# Sample 4 completions per prompt @ temp=1.0 from base, run pytest+lint+security
# Pairs = (passing, failing) where applicable
```

**Day 7**: Sanitize + Dedup + Decontaminate
```bash
# Pipeline
1. SHA-256 exact dedup
2. MinHash LSH 256-perm 5-gram threshold 0.7 (datatrove)
3. Decontaminate vs HumanEval+/MBPP+/LiveCodeBench/SWE-Bench
4. Apply sanitize.py (10 categories + PII NER + secrets)
5. AST validity (tree-sitter)
6. Stack-Edu classifier threshold 3
7. OpenCoder heuristics ~100 rules
# Push final → axentx/surrogate-1-v2-train (private HF) + Wasabi backup
```

**Day 7 deliverable**: ~250K curated training samples, sanitized, decontaminated.

---

### Week 2 — Stage 1 SFT + Tool-SFT + Multi-Agent SFT

**Day 8-9**: Stage 1 — Code SFT
```yaml
# Lightning H200 if quota; else RunPod spot ~$80
# /tmp/v2-stage1.yaml
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
load_in_4bit: true
adapter: lora
lora_r: 64
lora_alpha: 128
lora_dropout: 0.05
peft_use_dora: true
lora_target_modules: [q_proj, k_proj, v_proj, o_proj, gate_proj, up_proj, down_proj]

sequence_len: 32768
sample_packing: true
rope_theta: 1000000.0
rope_scaling:
  type: yarn
  factor: 4.0
  original_max_position_embeddings: 32768

datasets:
  - path: axentx/surrogate-1-v2-train
    type: chat_template
    field_messages: messages

num_epochs: 3
micro_batch_size: 1
gradient_accumulation_steps: 16
learning_rate: 1.0e-4
lr_scheduler: cosine
warmup_ratio: 0.03
optimizer: adamw_torch_fused
bf16: true
gradient_checkpointing: true
flash_attention: true
liger_kernel: true     # 30-40% memory reduction

hub_model_id: axentx/surrogate-1-coder-7b-v2-sft
hub_strategy: every_save
push_to_hub: true
```
**ETA**: ~12-15 hr H200 → push `axentx/surrogate-1-coder-7b-v2-sft`

**Day 10**: Stage 1.5 — Tool-Use SFT (continue from Stage 1 LoRA)
```yaml
# /tmp/v2-stage15.yaml
base_model: axentx/surrogate-1-coder-7b-v2-sft
adapter: lora    # continue same LoRA
# Same r=64, all-linear, DoRA

datasets:
  - path: axentx/surrogate-1-v2-tools  # 102K Hermes-XML formatted
    type: chat_template
    field_messages: messages

num_epochs: 2
learning_rate: 1.0e-4
hub_model_id: axentx/surrogate-1-coder-7b-v2-toolsft
```
**ETA**: ~8 hr → push toolsft

**Day 11-12**: Stage 1.6 — Multi-Agent SFT
```yaml
# /tmp/v2-stage16.yaml
base_model: axentx/surrogate-1-coder-7b-v2-toolsft
adapter: lora

# Tools to teach via system prompt
system_prompt: |
  You are Surrogate-1. You have these tools available:
  - spawn_subagent(role: str, prompt: str, max_steps: int = 10)
  - receive_results(subagent_id: str)
  - scratchpad_write(key: str, value: str)
  - scratchpad_read(key: str)
  - skill_recall(query: str) -> top_5_skills
  - reflexion_log(error_type, root_cause, prevention)
  - code_exec(language, code)
  - file_read, file_edit (unified diff)
  - shell_exec, search_repo
  
  Decision rules:
  - If task has 3+ independent steps → spawn 2-5 subagents in parallel
  - If task is sequential → solo with self-refine
  - If irreversible action (rm -rf, terraform destroy, payments) → ALWAYS ask
  - If confidence < 0.6 → ask user

datasets:
  - path: axentx/surrogate-1-v2-agent     # 20K + 500 synth orchestrator
    type: chat_template

num_epochs: 2
learning_rate: 1.0e-4
hub_model_id: axentx/surrogate-1-coder-7b-v2-agent
```
**ETA**: ~10 hr → push agent

**Day 13**: Eval Tier 1 smoke tests
```bash
# Don't regress base
evalplus.evaluate --model axentx/surrogate-1-coder-7b-v2-agent --dataset humaneval --backend vllm --greedy
# Target ≥84%
evalplus.evaluate --model axentx/surrogate-1-coder-7b-v2-agent --dataset mbpp --backend vllm --greedy
# Target ≥75%

# Primary metric
python -m lcb_runner.runner.main --model axentx/... --release_version release_v6
# Target ≥42%

# Tool use
gorilla-cli bfcl --model axentx/... --test-category all --backend vllm
# Target overall ≥70

# Long context
ruler eval --model axentx/... --max-len 32768 --tasks all
# Target ≥90
```

**Day 14**: Iterate or proceed
- If smoke tests pass → proceed to Stage 2 (Day 15)
- If regression → diagnose (data quality? hyperparam? overtraining?)

---

### Week 3 — Stage 2 + 2.5 DPO

**Day 15-16**: Build exec-graded preference data
```python
# For 5K hard prompts from training set:
# - Sample 4 completions each from agent model @ temp=1.0
# - Run pytest, hadolint, tflint, semgrep, prowler
# - Pairs = (highest-validator-score, lowest-validator-score)
# Output: 20K-50K pairs to axentx/surrogate-1-v2-dpo-codeexec
```

**Day 17**: Stage 2 — Code DPO
```yaml
# /tmp/v2-stage2.yaml
base_model: axentx/surrogate-1-coder-7b-v2-agent
adapter: lora    # continue

rl: dpo
rl_beta: 0.1
dpo_loss_type: focused           # 2502.11475
dpo_label_smoothing: 0.0

datasets:
  - path: Vezora/Code-Preference-Pairs
    type: dpo
  - path: argilla/distilabel-capybara-dpo-7k-binarized
    type: dpo
  - path: axentx/surrogate-1-v2-dpo-codeexec
    type: dpo

learning_rate: 5e-6
lr_scheduler: constant
num_epochs: 1
hub_model_id: axentx/surrogate-1-coder-7b-v2-dpo
```
**ETA**: ~5 hr → push dpo

**Day 18**: Stage 2.5 — Tool DPO
```yaml
# /tmp/v2-stage25.yaml
base_model: axentx/surrogate-1-coder-7b-v2-dpo
rl: dpo

datasets:
  - path: nvidia/When2Call/train_pref  # refusal vs forced-tool-use
    type: dpo

learning_rate: 5e-6
num_epochs: 1
hub_model_id: axentx/surrogate-1-coder-7b-v2-mvp     # final MVP push
```
**ETA**: ~3 hr → final push

**Day 19-21**: Full Tier-1 + Tier-2 evals
```bash
# Tier 1 (every checkpoint)
- EvalPlus HumanEval+ + MBPP+
- LiveCodeBench v6
- BFCL v3
- RULER 32K + 128K

# Tier 2 (monthly)
- SWE-Bench Lite
- Custom DevSecOps eval (Dockerfile/K8s/TF/Bash/CVE × 280 tasks)
- GAIA Level 1
- 100-sample no-leak spot check
```

---

### Week 4 — Iterate or Phase B

**Day 22-28**: Triage + decide

**If targets hit (LCB ≥42% + SWE-Bench Lite ≥25% + BFCL ≥70 + no leaks)**:
- Tag `axentx/surrogate-1-coder-7b-v2-mvp` as official MVP
- Update outcome.md + knowledge_index.md
- Plan Phase B (cluster expertise) — start Week 5

**If targets miss**:
- Identify weakest area (data quality? hyperparam? not enough epochs?)
- Re-train specific stage with adjustment
- Don't blast all stages — pinpoint fix

---

## Phase A Total Resource Estimate

| Item | Cost | Free? |
|------|------|-------|
| H200 compute ~50-60 hr | $0-200 | Lightning quota |
| Synth orchestrator (Claude API) | $200 | no |
| Wasabi storage | $6/mo | no |
| HF PRO | $9/mo | no |
| **Total cash** | **~$400 + $15/mo** | |

---

## Risk Register

| Risk | Probability | Mitigation |
|------|-------------|------------|
| Free Lightning quota runs out mid-training | Med | RunPod spot H100 backup ~$2/hr |
| HF API rate limit during data load | Med | Use Wasabi mirror; HF PRO 20× helps |
| LCB v6 doesn't improve | Med | Re-curate data; check for over-training |
| Multi-agent SFT destabilizes prior capabilities | Low-Med | Eval gate; rollback if regression >5% |
| Synth orchestrator data quality poor | Med | Sample inspect 50 traces; require dual-validator (Opus + Sonnet) |
| Tool-use trains but BFCL low | Med | Verify Hermes XML format roundtrip; check chat_template |
| Sanitizer over-filters | Low | Stats from first 100K rows; tune patterns |

---

## Success Definition (Phase A done)

- [ ] `axentx/surrogate-1-coder-7b-v2-mvp` pushed to HF Hub
- [ ] HumanEval+ ≥84%, MBPP+ ≥75% (no regression)
- [ ] **LiveCodeBench v6 ≥42%** (primary)
- [ ] **SWE-Bench Lite ≥25%** (primary)
- [ ] **BFCL v3 overall ≥70** (primary)
- [ ] RULER @ 32K ≥90
- [ ] No data leakage in 100 random inference samples
- [ ] outcome.md + knowledge_index.md updated

Then → Phase B (cluster expertise) starts Week 5.

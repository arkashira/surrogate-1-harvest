---
title: Surrogate-1 v2 Master Plan — FINAL Synthesis (10 Research Streams, 9,061 lines)
date: 2026-04-29
tags: [surrogate-1, v2, master-plan, training, agent, hallucination, long-context, sdlc, self-improve]
status: ready-to-execute
research_corpus:
  - research-training-techniques.md (981 lines)
  - research-data-curation.md (980 lines)
  - research-arch-context.md (623 lines)
  - research-evaluation.md (762 lines)
  - research-tool-use.md (907 lines)
  - research-multi-agent.md (1,058 lines)
  - research-hallucination.md (1,272 lines)
  - research-long-context.md (837 lines)
  - research-sdlc-agentic.md (817 lines)
  - research-self-improve.md (824 lines)
  - v1-eval-vs-base.md (143 lines)
total_research_lines: 9,061
---

# Surrogate-1 v2 Master Plan — FINAL

**Vision**: Standalone DevSecOps coding agent matching frontier capability tiers in 7B parameters via:
- Multi-stage training (SFT → DPO → RLVR)
- Built-in tool calling, multi-agent orchestration, self-improvement
- Long-context up to 128K via YaRN+DCA
- Hallucination defense-in-depth (4 layers)
- SDLC mastery (validator-graded RL)

---

## TL;DR — Single-glance v2 spec

| Dimension | v1 | **v2** | Source |
|-----------|----|----|--------|
| **Base** | Qwen2.5-Coder-7B-Instruct | same (fix pipeline first) | arch agent |
| **Quant + adapter** | bnb-4bit + LoRA r=32 | bnb-4bit + **LoRA r=64 + DoRA + all-linear** | training agent |
| **Training context** | 2,048 | **32,768** (YaRN factor=4) | long-context agent |
| **Inference context** | 2K | **128K (YaRN+DCA via vLLM)** | long-context agent |
| **Stages** | SFT 1ep | **CPT-FIM → SFT 3ep → Tool-SFT → DPO → Tool-DPO → RLVR** | training + tool agents |
| **Training data** | 1,329 polluted | **~250K curated (cleaned, deduped, sanitized)** | data + SDLC + tool |
| **Tool use** | none | **Hermes XML, BFCL 70-75 target** | tool-use agent |
| **Multi-agent** | none | **2-5 subagents, GAIA L1 20-30%** | multi-agent agent |
| **Hallucination control** | none | **4-layer: XGrammar + repo-RAG + TruthRL + SelfCheckGPT-NLI** | hallucination agent |
| **Self-improvement** | none | **Voyager skills + Reflexion + Self-Refine + Letta memory** | self-improve agent |
| **Sanitization** | leaked paths | **10 categories + PII + secrets** (commit 1dfdc54) | data agent |
| **Eval** | informal | **EvalPlus + LiveCodeBench v6 + SWE-Bench Lite + custom DevSecOps + BFCL v3 + RULER** | eval agent |

### Headline target metrics
| Metric | v1 | v2 conservative | v2 stretch |
|--------|----|----|----|
| HumanEval+ | ~84% | ≥84% (no regress) | ≥85% |
| MBPP+ | ~75% | ≥75% | ≥78% |
| **LiveCodeBench v6** | ~38% | **42-45%** | **45-48%** |
| **SWE-Bench Lite Pass@1** | ~5% | **25-30%** | **32-37%** |
| **BFCL v3 overall** | ~25 | **70-75** | **78-82** |
| **GAIA Level 1** | — | **20-30%** | **30-40%** |
| **RULER @ 32K** | — | **90+** | 92+ |
| **RULER @ 128K** | — | **80+** | 85+ |
| **CodeHalu rate** | ~25% | **<8%** | <5% |
| **Phantom imports** | ~22% | **<5%** | <3% |
| **Calibration AUC** | ~0.55 | **>0.85** | >0.90 |
| **Compile rate** | ~85% | **100%** | 100% (XGrammar) |
| **DevSecOps lint+sec clean** | — | **65%+** | 75%+ |

### Cost summary
- **Compute**: ~30-40 GPU-hr Lightning H200 = **$80-200** (or free if quota)
- **Optional GRPO RL**: +~$120 (Lightning H200, 24hr)
- **Synthetic data gen** (Claude Opus orchestrator traces): **~$200**
- **Total cash**: **$300-500** one-time + $14-20/mo (HF PRO + Wasabi)
- **Timeline**: 3-4 weeks calendar (1 engineer)

---

## 1. Multi-Stage Training Pipeline

### Stage 0 — CPT-FIM (optional, +5-10% on cross-file)
```yaml
data: bigcode/the-stack-v2-smol-ids (FIM tokens, 100M-500M tokens)
context: 32K
adapter: LoRA r=32 (separate adapter, not merged into final)
epochs: 1
lr: 5e-5
goal: Repo-level + cross-file dependency awareness
```
**Skip if budget tight** — direct SFT works.

### Stage 1 — General Code SFT (3 epochs, all-linear LoRA + DoRA)
```yaml
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
load_in_4bit: true
adapter: lora
lora_r: 64                                        # was 32 — bigger for richer learning
lora_alpha: 128                                   # alpha = 2*r heuristic
lora_dropout: 0.05
peft_use_dora: true                               # +5-10% over plain LoRA
lora_target_modules:
  - q_proj
  - k_proj
  - v_proj
  - o_proj
  - gate_proj
  - up_proj
  - down_proj                                     # all-linear

# Context extension via YaRN (16× from 2048 to 32768)
sequence_len: 32768
sample_packing: true
rope_theta: 1000000.0
rope_scaling:
  type: yarn
  factor: 4.0
  original_max_position_embeddings: 32768

# Training
num_epochs: 3                                     # was 1
micro_batch_size: 1                               # tighter at 32K
gradient_accumulation_steps: 16
learning_rate: 1.0e-4                             # was 2e-4 (gentler at higher rank)
lr_scheduler: cosine
warmup_ratio: 0.03
optimizer: adamw_torch_fused
bf16: true
gradient_checkpointing: true
flash_attention: true                             # FA3 if H100/H200, FA2 on L40S
liger_kernel: true                                # 30-40% memory reduction

# Curriculum: 60% long-context (≥16K) + 40% short
```

**Datasets** (mixed, ~95K samples post-sanitize-dedup):
1. `microsoft/rStar-Coder` → 30K (LCB-best 7B-class data)
2. `nvidia/OpenCodeReasoning-2` → 20K (R1 reasoning chains)
3. `nvidia/OpenCodeInstruct` → 10K (filter `average_test_score≥0.7`)
4. `inclusionAI/Ling-Coder-SFT` → 10K (multi-lingual)
5. `OpenCoder-LLM/opc-sft-stage1+2` → 10K
6. `bigcode/self-oss-instruct-sc2-exec-filter-50k` → 50K (exec-verified)
7. `m-a-p/CodeFeedback-Filtered-Instruction` → 10K (complexity ≥4/5)
8. axentx OWN data (sanitized) → 5K-10K (DevSecOps domain bias)

**ETA**: ~12-15 hr Lightning H200 → push `axentx/surrogate-1-coder-7b-v2-sft`

### Stage 1.5 — Tool-Use SFT (insert after Stage 1, +50pp BFCL)
```yaml
# Continue from Stage 1 LoRA
data_format: Hermes XML (chat_template + <tool_call>...</tool_call>)
context: 32K

# 102K tool-use samples
datasets:
  - NousResearch/hermes-function-calling-v1: 7.93K (gold-standard)
  - Salesforce/xlam-function-calling-60k: 30K (3,673 APIs)
  - Agent-Ark/Toucan-1.5M: 80K (single + multi-turn, MCP-grounded)
  - nvidia/When2Call: 15K (refusal/clarify failure modes)
  - Nanbeige/ToolMind: 10K (graph-syn reasoning chains)
  - nvidia/Nemotron-SWE-v1: 5K (code-exec trajectories)
  - SWE-Gym/OpenHands-Sampled-Trajectories: 2.4K

epochs: 2
lr: 1e-4
adapter: same all-linear DoRA from Stage 1
```
**ETA**: ~8 hr H200 → push `axentx/surrogate-1-coder-7b-v2-toolsft`

### Stage 1.6 — Multi-Agent SFT (the secret sauce — orchestrator pattern)
```yaml
# Continue from Stage 1.5
data: Hermes Agent Reasoning Traces (14K) +
      Nebius SWE-agent-trajectories filtered (5K) +
      SWE-Gym successful (400) +
      Synth Claude Opus 4 + Sonnet 4 orchestrator→subagent traces (500)
context: 16K-32K
epochs: 2
lr: 1e-4

# Tools to teach in system prompt:
tools_taught:
  - spawn_subagent(role, prompt, max_steps)
  - receive_results(subagent_id)
  - scratchpad_write(key, value)
  - scratchpad_read(key)
  - skill_recall(query)         # Voyager
  - reflexion_log(error, root_cause, prevention)
  - code_exec(language, code)
  - file_read(path)
  - file_edit(path, diff)
  - shell_exec(cmd)
  - search_repo(query)
```
**ETA**: ~10 hr H200 → push `axentx/surrogate-1-coder-7b-v2-agent`

### Stage 2 — Code DPO (1 epoch, after SFT consolidates)
```yaml
adapter: continue from Stage 1.6
rl: dpo
rl_beta: 0.1
dpo_loss_type: focused                            # 2502.11475 (Focused-DPO)
dpo_label_smoothing: 0.0

datasets:
  - path: Vezora/Code-Preference-Pairs            # 55K bug/no-bug
    type: dpo
  - path: argilla/distilabel-capybara-dpo-7k-binarized
    type: dpo

# Exec-grounded preferences: tests pass = preferred (override LLM judge)
custom_data: rejection-sampled from Stage 1 model with Prowler/Trivy/cfn-guard graders

learning_rate: 5e-6                               # 20× lower than SFT
lr_scheduler: constant
num_epochs: 1
```
**ETA**: ~5 hr H200

### Stage 2.5 — Tool-Use DPO (When2Call preferences)
```yaml
data: nvidia/When2Call train_pref (refusal vs forced-tool-use)
epochs: 1
lr: 5e-6
beta: 0.1
```
**ETA**: ~3 hr

### Stage 3 — RLVR (optional, $120 add-on for stretch targets)
```yaml
algorithm: GRPO (DeepSeek-R1 / Qwen3 style)
rewards: validator-graded composite
  test_pass: +1.0      # E2B/Modal sandbox runs pytest
  lint_clean: +0.3     # hadolint/tflint/actionlint/shellcheck/kubeconform
  security_clean: +0.3 # semgrep/checkov/cfn-guard/cfn-nag
  cite_correct: +0.2   # repo-RAG citation valid
  no_phantom: +0.2     # imports/APIs all real
  honest_idk: 0.0      # neutral (TruthRL ternary)
  confident_wrong: -1.0 # heavy penalty
data: SWE-Gym + R2E-Gym + custom DevSecOps (~10K rollouts)
ETA: ~24 hr H200
```
**Push**: `axentx/surrogate-1-coder-7b-v2-rlvr`

---

## 2. Hallucination Defense-in-Depth (4 layers)

### Layer 1 — XGrammar constrained decoding (FREE, week 1)
```bash
# vLLM 2026-04+ default backend
vllm serve axentx/surrogate-1-coder-7b-v2 \
  --guided-decoding-backend xgrammar
```
- **JSON schema** for tool calls (no malformed function calls)
- **Regex** for code blocks (Python AST validity)
- **Result**: 100% syntax errors eliminated, 30% phantom imports prevented at decode

### Layer 2 — Repo-map + doc-RAG cite-or-abstain
```python
# At inference, before model generates:
1. tree-sitter scan repo → symbol map
2. Embed user query → top-K relevant files
3. System prompt: "Cite [file.py:Lx-Ly] or say 'I don't know' + suggest verification"
```
- **Result**: DeepSeek-Coder went 20% → 2% hallucination (paper 2512.12117)
- Stack: tree-sitter + bge-base-en-v1.5 + LlamaIndex

### Layer 3 — TruthRL ternary (in Stage 3 RLVR)
- See Stage 3 reward function above
- **Result**: -28.9% hallucination on Qwen 7B (TruthRL paper Sept 2025)

### Layer 4 — SelfCheckGPT-NLI gate (inference, runs on Mac M3)
```python
# Sample N=5 responses → measure NLI contradiction
if avg_contradiction > 0.6:
    response = "Note: this answer is uncertain. Verify with: " + suggest_test()
```
- **Result**: -30% confident-wrong rate, 100MB NLI model

### Hallucination metrics target
| Metric | Mechanism |
|--------|-----------|
| 0 syntax errors | XGrammar |
| Phantom imports <5% | XGrammar + RAG + TruthRL |
| Wrong API signatures <10% | repo-RAG + TruthRL |
| CodeHalu <8% | All 4 layers |
| Calibration AUC >0.85 | TruthRL ternary |

---

## 3. Long-Context Stack (Train 32K, Serve 128K)

### Training (Stage 1, fits L40S 48GB)
- Sequence length: **32,768**
- LoRA r=32-64 (start 32, scale to 64 if memory allows)
- Liger Kernel: 30-40% memory reduction
- Unsloth gradient checkpointing
- Sample packing: 2-3× throughput
- FlashAttention 2 (FA3 if H100+)

### Serving (vLLM + DCA = 4× extension free)
```bash
VLLM_ATTENTION_BACKEND=DUAL_CHUNK_FLASH_ATTN \
vllm serve axentx/surrogate-1-coder-7b-v2 \
  --max-model-len 131072 \
  --rope-scaling '{"type":"yarn","factor":4.0,"original_max_position_embeddings":32768}'
```
+ MInference for 3-7× prefill speedup at long context

### Data curriculum
- **60% long ≥16K** (repo-concat + FIM tokens + topo sort by import graph)
- **40% short** (HumanEval-style)
- **NExtLong hard-negative interleaving** (ICML 2025 — synth > human)
- Total volume: 100M-500M tokens

### RULER targets
| Context | Target | Reference |
|---------|--------|-----------|
| 32K | 90+ | LongRoPE2 LLaMA3-8B = 82.0 |
| 128K | 80+ | Qwen2.5-7B-1M = 84.7 |

---

## 4. Self-Improvement Stack (bake into v2)

### 4.1 Voyager skill library
```python
# ~/.surrogate/skills/<category>/<slug>/SKILL.md format
# FAISS index over NL descriptions
# Only commit skill if exec_pass + self_verify_pass
```
- Tools: `skill_recall(query) → top-5 skills` injected into context
- Versioned in git, success/usage counts tracked
- Foundation for everything else

### 4.2 Reflexion bounded buffer (3 reflections, structured)
```json
{
  "error_type": "TypeError",
  "root_cause": "missing import statsmodels",
  "corrective_action": "added 'import statsmodels.api as sm'",
  "prevention_rule": "always check imports against requirements.txt"
}
```
NOT free-form ("be more careful") — structured fields force learnable patterns.

### 4.3 Self-Refine 3-iter inference loop
```
Generate → Critique → Refine → exec_test
if test_pass: stop
elif iter < 3: refine again
else: return best with confidence flag
```

### 4.4 Letta 3-tier memory
- **Core** (in-context, agent-editable via tools)
- **Recall** (S3 trace log, queryable)
- **Archival** (FalkorDB GraphRAG + Cognee for long-term knowledge)

### 4.5 Trace harvester (every task → S3)
Compound learning signal — even if v2 doesn't retrain on it, v3 will.

### 4.6 Weekly LoRA retrain cron
```yaml
cron: "0 3 * * 0"   # Sunday 3am
trainer: Axolotl + Online DPO
data: replay buffer (10% old + 90% new traces)
constraint: O-LoRA orthogonal regularization (anti-forgetting)
gate: eval against frozen regression set; if regression > 5%, abort
deploy: canary 5% via vLLM LoRA-swap
```

---

## 5. Tool Use & Multi-Agent (built into model)

### Tool format: Hermes XML (vLLM stock parser)
```xml
<tool_call>
{"name": "code_exec", "arguments": {"language": "python", "code": "..."}}
</tool_call>
<tool_response>
{"name": "code_exec", "content": {"stdout": "...", "stderr": "...", "exit": 0}}
</tool_response>
```

### Tools to teach (system prompt contract)
```
spawn_subagent(role, prompt, max_steps) → subagent_id
receive_results(subagent_id) → output
scratchpad_write(key, value), scratchpad_read(key)
skill_recall(query) → top-5 skills
reflexion_log(error_type, root_cause, prevention)
code_exec(language, code) → {stdout, stderr, exit}
file_read(path), file_edit(path, unified_diff)
shell_exec(cmd) → output
search_repo(query) → matches with citations
```

### Decision rule (encoded in SFT system prompt)
- If task can be parallelized (3+ independent steps) → spawn 2-5 subagents
- If task is sequential → solo with self-refine
- If task requires verification → use code_exec + tests

### BFCL v3 targets
| Sub-bench | v1 (base) | v2 SFT | v2 SFT+DPO |
|-----------|-----------|--------|------------|
| single-turn | 30 | 75 | **85** |
| overall | 25 | 65 | **75** |
| multi-turn | 15 | 40 | **50** |

---

## 6. Evaluation Pipeline

### Tier 1 — every checkpoint (~3 GPU-hr)
```bash
# Smoke (don't regress base)
evalplus.evaluate --model M --dataset humaneval --backend vllm --greedy
evalplus.evaluate --model M --dataset mbpp --backend vllm --greedy

# Primary progress metric (post-cutoff = no contamination)
python -m lcb_runner.runner.main --model M --release_version release_v6

# Tool use
gorilla-cli bfcl --model M --test-category all --backend vllm

# Long context
ruler eval --model M --max-len 32768 --tasks all
```

### Tier 2 — monthly (~15 GPU-hr)
```bash
# DevSecOps domain (our custom)
python eval/devsecops_eval.py --model M --validators docker,kubeval,tflint,actionlint,semgrep,cfn-guard

# Repo-scale
python -m swebench.harness.run_evaluation \
  --predictions_path preds/v2.jsonl \
  --max_workers 8 \
  --dataset_name SWE-Bench/Lite

# Multi-agent
python -m gaia_runner.run --model M --level 1
```

### Tier 3 — defer to v3
- SWE-Bench Verified (full)
- BigCodeBench
- Aider Polyglot
- LiveCodeArena
- SWE-Bench Pro (Scale AI)

### Honest progress signal
- ✅ LiveCodeBench v6 ↑ = real (post-cutoff)
- ⚠️ HumanEval+ ↑ >5pp = SUSPICIOUS (overfitting check needed)
- ✅ Custom DevSecOps ↑ = mission alignment
- ✅ BFCL v3 ↑ = tool capability gain
- ✅ RULER ↑ = context retention
- ✅ CodeHalu ↓ = honesty win

---

## 7. Sanitization (already deployed — commit 1dfdc54)

10 categories integrated into `dataset-mirror.sh` + `dataset-enrich.sh`:
1. LLM provider tags
2. Internal FS paths
3. Internal directory names
4. Daemon names
5. axentx repo identifiers
6. Token-shaped strings
7. Env var leaks
8. Discord webhooks
9. Internal commit prefixes
10. JWT-shaped tokens

Plus PII (BigCode starpii NER) + secret detection (Yelp detect-secrets) + low-quality (refusals, char spam).

---

## 8. Compute & Cost Plan

### Free tier (preferred)
- Lightning AI: 80 GPU-hr/mo free × 2 accounts (ashiradevops + ashirapit verified)
- Kaggle: 30 hr T4×2/wk free
- HF Spaces ZeroGPU (with HF PRO $9/mo): on-demand A10G/H100

### Paid (one-time)
- **Synthetic data gen** (Claude API): ~$200 (orchestrator traces + judge labels)
- **Compute extra** (if free tier exhausted): ~$80-200 (Lightning paid or RunPod spot H100)
- **GRPO RL** (Stage 3 stretch): +$120

### Recurring
- **HF PRO**: $9/mo (20× rate limit, ZeroGPU, LFS quota)
- **Wasabi storage**: ~$6/mo (1 TB, 0 egress)
- **Total**: $15/mo

### Total v2 budget
- Cash one-time: **$300-500**
- Recurring: $15/mo
- GPU-hr: ~30-40 free + ~24 if RLVR

---

## 9. Timeline (4-week sprint)

### Week 1 — Data
- Day 1-2: Download 8 datasets (~250K rows)
- Day 3: Sanitize + dedup (MinHash 256-perm) + decontaminate vs eval suites
- Day 4-5: Stack-Edu classifier filter + AST validity
- Day 6-7: Generate 500 orchestrator traces via Claude Opus 4 + multi-agent synth (~$200)

### Week 2 — Train Stages 1-1.6
- Day 8-9: Stage 1 SFT 32K context (3 epochs, ~12 hr H200)
- Day 10: Stage 1.5 Tool-SFT (~8 hr)
- Day 11-12: Stage 1.6 Multi-agent SFT (~10 hr)
- Day 13-14: EvalPlus + LiveCodeBench v6 + BFCL smoke tests

### Week 3 — DPO + Eval
- Day 15-16: Build exec-graded preference data (~50K pairs)
- Day 17: Stage 2 Code DPO (~5 hr)
- Day 18: Stage 2.5 Tool DPO (~3 hr)
- Day 19-21: Full Tier-1 + Tier-2 evals + custom DevSecOps eval

### Week 4 — Iterate or RLVR
- If targets hit: ship v2 + write outcome.md + plan v2.5 (Qwen3-Coder-30B-A3B)
- If targets miss: Stage 3 RLVR ~$120 + 24 hr → v2-rlvr push

---

## 10. v2 Definition of Done (15 items)

### Data
- [x] Sanitizer deployed (commit 1dfdc54)
- [ ] 250K curated dataset assembled (sanitize + dedup + decontaminate + filter)
- [ ] 500 synth orchestrator traces generated (Claude)
- [ ] 50K exec-graded DPO pairs

### Training
- [ ] Stage 0 CPT-FIM (optional)
- [ ] Stage 1 SFT 3 epochs at 32K context
- [ ] Stage 1.5 Tool-SFT (Hermes format)
- [ ] Stage 1.6 Multi-agent SFT (orchestrator pattern)
- [ ] Stage 2 Code DPO (Focused-DPO)
- [ ] Stage 2.5 Tool DPO (When2Call)
- [ ] Stage 3 RLVR (optional, stretch)

### Eval
- [ ] EvalPlus HumanEval+ ≥84%, MBPP+ ≥75% (no regression)
- [ ] **LiveCodeBench v6 ≥42%** (primary)
- [ ] **SWE-Bench Lite Pass@1 ≥25%** (primary)
- [ ] **BFCL v3 overall ≥70** (primary)
- [ ] RULER @ 32K ≥90, @ 128K ≥80
- [ ] CodeHalu rate <8%
- [ ] DevSecOps custom eval baseline + ≥65% lint+sec clean

### Infrastructure
- [ ] Push to `axentx/surrogate-1-coder-7b-v2-{sft,toolsft,agent,dpo,rlvr}`
- [ ] Deploy vLLM serve with XGrammar + DCA + MInference
- [ ] Voyager skill library bootstrap (~50 seed skills)
- [ ] Letta memory backend integrated (Cognee + FalkorDB)
- [ ] Trace harvester pipeline (S3/Wasabi)
- [ ] Weekly LoRA retrain cron + canary deploy

### Documentation
- [ ] outcome.md updated with v2 results + lessons
- [ ] knowledge_index.md appended (10+ patterns)
- [ ] README.md for `axentx/surrogate-1-coder-7b-v2-rlvr` HF Hub repo

---

## 11. v3 Roadmap (deferred features)

| Feature | Why deferred | When |
|---------|--------------|------|
| Qwen3-Coder-30B-A3B base | needs 8× H100 ($300-500/run) | v3 (2-3 months) |
| GRPO + sandbox rewards | RLVR infra heavy | v3 |
| Self-Rewarding LM (judge own) | needs >30B judge | v3+ (after v3 has 30B base) |
| LoraHub composition | needs ≥4 domain LoRAs | v4 |
| Self-Play SWE-RL | sandbox infra heavy | v3 |
| Online DPO during serving | start weekly offline first | v3 |
| Meta-rewarding | after self-rewarding works | v4 |
| 1M context training | needs 120GB VRAM run | v3+ |
| STOP self-modifying scaffolding | blast radius too high | v4 |

---

## 12. References (consolidated)

10 research files in this folder + 9,061 lines of analysis + 200+ paper citations. Top arxiv IDs:

**Training**: 2402.19173 (StarCoder2), 2406.11931 (DeepSeek-Coder-V2), 2411.04905 (OpenCoder), 2505.21297 (rStar-Coder), 2502.11475 (Focused-DPO), 2310.02304 (STOP), 2506.11425 (Agent-RLVR), 2503.18455 (SEAlign)

**Tool use**: NousResearch/hermes-function-calling-v1, Salesforce/xlam, Toucan-1.5M, When2Call

**Multi-agent**: 2509.23045 (Kimi-Dev), 2511.05951 (Klear-AgentForge), 2510.04206 (AgentRL), Anthropic Multi-Agent Research

**Hallucination**: 2509.23045 (TruthRL), 2406.04692 (MoA), arxiv 2512.12117 (DeepSeek RAG citation)

**Long context**: 2501.15383 (Qwen2.5-1M), 2502.20082 (LongRoPE2), 2309.00071 (YaRN), 2501.12766 (NExtLong)

**SDLC**: 2412.21139 (SWE-Gym), 2504.21798 (SWE-smith), 2504.07164 (R2E-Gym), Together DeepSWE, 2509.25455 (PIPer), 2402.01030 (CodeAct)

**Self-improve**: 2305.16291 (Voyager), 2303.11366 (Reflexion), 2303.17651 (Self-Refine), 2409.12917 (SCoRe), 2401.10020 (Self-Rewarding)

**Eval**: 2404.06654 (RULER), 2410.02694 (HELMET), LiveCodeBench, SWE-Bench Verified

---

**Status**: ready to execute. Awaits user decision on HF PRO ($9/mo) + Wasabi (~$6/mo) subscription = $15/mo total to enable Stage 1 build.

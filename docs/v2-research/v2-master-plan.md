---
title: Surrogate-1 v2 Master Plan — Synthesis of 4 Research Streams
date: 2026-04-29
tags: [surrogate-1, v2, master-plan, training, data, eval, architecture]
status: ready-to-execute
research_files:
  - research-training-techniques.md (981 lines, 30+ papers)
  - research-data-curation.md (980 lines, 35+ tools/datasets)
  - research-arch-context.md (623 lines, 3 implementation recipes)
  - research-evaluation.md (762 lines, full benchmark commands)
  - v1-eval-vs-base.md (143 lines, qualitative comparison)
---

# Surrogate-1 v2 — Master Plan

## TL;DR (60 sec read)

| Dimension | v1 | v2 plan | Expected lift |
|-----------|----|---------| --------------|
| **Dataset size** | 1,329 samples | **50K-100K** (curated) | 38× more signal |
| **Dataset sources** | axentx scrape (polluted) | rStar-Coder + OpenCodeReasoning + OpenCodeInstruct + 7 more | quality + diversity |
| **Training method** | SFT 1 epoch | **SFT 3 epochs + DPO 1 epoch + DoRA + all-linear LoRA** | +20-40% |
| **Context length** | 2,048 | **8,192 (YaRN 4×)** | 4× repo capacity |
| **Base model** | Qwen2.5-Coder-7B | same (fix pipeline first) | flat |
| **Sanitization** | none → leaked paths | 10 pollution categories + PII NER + secrets | 0 leak |
| **Eval rigor** | informal | **EvalPlus HE+/MBPP+ + LiveCodeBench v6 + custom DevSecOps** | reproducible |
| **Target LCB v6** | ~37.6% (= base) | **≥42%** | +4.4 pp |
| **Compute cost** | free Lightning L40S 12 hr | 1 HF PRO + Wasabi + Lightning free | ~$15/mo + 30 GPU hr |

---

## 1. Dataset Strategy — Replace 80% of Burst-Generated Noise

### Top 10 datasets (replace v1 ~1.3K with curated ~80K)

| # | Source | Size | Why | Filter |
|---|--------|------|-----|--------|
| 1 | `microsoft/rStar-Coder` | 1.4M | **+39pt LiveCodeBench** on 7B-class | take 30K random |
| 2 | `nvidia/OpenCodeReasoning-2` | 2.5M | R1-generated reasoning chains | take 20K |
| 3 | `nvidia/OpenCodeInstruct` | 5M | has `average_test_score` per row | filter ≥0.7 → take 10K |
| 4 | `inclusionAI/Ling-Coder-SFT` | 4.48M | 20 langs (EN+CN coverage) | take 10K |
| 5 | `OpenCoder-LLM/opc-sft-stage1` | 2M | transparent recipe, dev-favorite | take 5K |
| 6 | `OpenCoder-LLM/opc-sft-stage2` | 2.5M | DevSecOps-leaning topics | take 5K |
| 7 | `bigcode/self-oss-instruct-sc2-exec-filter-50k` | 50K | execution-verified, HE 72.6 | full |
| 8 | `m-a-p/CodeFeedback-Filtered-Instruction` | 157K | complexity ≥4/5 | take 10K |
| 9 | `SWE-Gym/SWE-Gym` | 2.4K | real-repo agent tasks | full |
| 10 | `Vezora/Code-Preference-Pairs` | 55K | bug/no-bug for DPO stage | full (DPO use) |

**Plus**: axentx OWN data (deduped + sanitized) ~5-10K rows for DevSecOps domain bias.

**Total**: ~95K SFT samples + 55K preference pairs (DPO)

### Data Pipeline (non-negotiable order)

```
Step 1. Exact SHA-256 dedup
Step 2. MinHash LSH (256 perm, 5-gram, threshold 0.7) — via datatrove
Step 3. Decontamination vs HumanEval+/MBPP+/LiveCodeBench/SWE-Bench
Step 4. 3-layer sanitization:
        a. Regex (10 pollution patterns from sanitize.py)
        b. BigCode starpii NER (NAME/EMAIL/KEY/PASSWORD/IP/USERNAME)
        c. Yelp detect-secrets (AWS/GitHub/Slack/Stripe/20+ plugins)
Step 5. OpenCoder heuristics (~100 rules — opc_data_filtering)
Step 6. tree-sitter AST validity (drop unparseable code)
Step 7. Stack-Edu classifier (threshold 3) — quality scoring
Step 8. test-execution filter (subset that has tests)
Step 9. Diversity sampling (round-robin across categories)
Step 10. DPO data: Focused-DPO localized loss (only diff tokens)
```

### Sanitization regex set (already deployed in `bin/lib/sanitize.py`)

10 categories:
1. LLM provider tags (`# generated via cerebras:...`)
2. Internal FS paths (`/home/hermes/`, `/data/state/`)
3. Internal directory names (`agentic-discovery/`, `enriched/<slug>/`)
4. Daemon names (`dataset-mirror`, `bulk-ingest-parallel`)
5. axentx repo identifiers
6. Token-shaped strings (HF/Anthropic/OpenAI/Kaggle/etc.)
7. Env var leaks (HF_TOKEN=..., LIGHTNING_API_KEY=...)
8. Discord webhooks
9. Internal commit message prefixes
10. JWT-shaped tokens (Brev, etc.)

Plus: PII (email/phone/SSN/AWS/Stripe) + low-quality (refusals, char spam)

---

## 2. Training Stack

### Stage 1 — SFT with all-linear LoRA + DoRA (free 15-25% lift)

```yaml
# Axolotl config: stage1-sft.yml
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
model_type: AutoModelForCausalLM

load_in_4bit: true
adapter: lora
lora_r: 32
lora_alpha: 64
lora_dropout: 0.05
peft_use_dora: true                          # +5-10% over plain LoRA
lora_target_modules:                          # ALL-LINEAR (was: q/k/v/o + MLP only)
  - q_proj
  - k_proj
  - v_proj
  - o_proj
  - gate_proj
  - up_proj
  - down_proj
                                              # NOTE: do NOT include lm_head/embed_tokens
                                              # (rsLoRA needed if r>32)

# Context extension via YaRN (4× from 2048 to 8192)
sequence_len: 8192
sample_packing: true                          # critical for efficiency
rope_theta: 1000000.0
rope_scaling:
  type: yarn
  factor: 4.0
  original_max_position_embeddings: 32768

# Training
num_epochs: 3                                 # was 1 — bigger lift than rank↑
micro_batch_size: 2
gradient_accumulation_steps: 4
learning_rate: 2.0e-4
lr_scheduler: cosine
warmup_ratio: 0.03
optimizer: adamw_torch_fused
bf16: true
gradient_checkpointing: true
flash_attention: true                         # FA3 if H100/H200, FA2 on T4/L40S

# Hub
hub_model_id: axentx/surrogate-1-coder-7b-v2-sft
hub_strategy: every_save
push_to_hub: true
```

ETA on Lightning L40S: **8-12 hr** for 95K samples × 3 epochs (vs. v1's 12 min for 1.3K × 1).

### Stage 2 — DPO on 55K preference pairs (+5-15% lift)

```yaml
# Axolotl config: stage2-dpo.yml
base_model: axentx/surrogate-1-coder-7b-v2-sft   # output of stage 1
adapter: lora                                          # continue from SFT LoRA
# Same LoRA config as stage 1

rl: dpo
rl_beta: 0.1
dpo_loss_type: sigmoid                                 # baseline; later try focused
dpo_label_smoothing: 0.0

datasets:
  - path: Vezora/Code-Preference-Pairs                 # 55K bug/no-bug
    type: dpo
  - path: argilla/distilabel-capybara-dpo-7k-binarized
    type: dpo

learning_rate: 1.0e-5                                  # 20× lower than SFT
lr_scheduler: constant                                 # NOT cosine
num_epochs: 1                                          # 1 epoch only
warmup_ratio: 0.0

hub_model_id: axentx/surrogate-1-coder-7b-v2-dpo
```

ETA: 4-6 hr on L40S.

### Why this stack (not other options)

✅ **Use**: SFT all-linear + DoRA + DPO sigmoid + YaRN 4×

❌ **Skip**:
- ORPO (you've already SFT'd, no benefit over DPO)
- RLEF (too heavy for v2 — defer to v3)
- Self-rewarding 7B-judge (too noisy below 70B)
- LoRA rank > 32 without rsLoRA (instability)
- Full fine-tune (overkill, expensive, no clear lift over QLoRA)

🎯 **v3 plan (deferred)**: GRPO + sandbox rewards (Prowler/Trivy/cfn-guard graders)

---

## 3. Architecture & Context

| Choice | Selection | Reasoning |
|--------|-----------|-----------|
| **Base model** | Qwen2.5-Coder-7B (stay) | HumanEval 88.4 already, fix pipeline first |
| **Quantization** | bnb 4-bit (nf4 + double) | proven on L40S |
| **Adapter** | LoRA r=32 + DoRA + all-linear | best free win |
| **Context** | 8192 (YaRN factor=4) | 4× v1, fits L40S 48GB |
| **Attention** | FA2 (T4/L40S) / FA3 (H100+) | well-supported |
| **Sample packing** | true | 2-3× throughput |

### v2.5 plan (after v2 ships)

- Base: **Qwen3-Coder-30B-A3B** (3.3B active / 30.5B MoE)
- Native context: 256K, YaRN to 1M
- SWE-Bench Verified: 51.6% (best open small-MoE)
- Compute requirement: 8× H100/H200 → **paid only** (~$300-500/training run)
- Defer until v2 proves the data pipeline + DPO loop works

---

## 4. Evaluation Pipeline

### Tier 1 — Run every checkpoint (~75 min on T4)

```bash
# 1. EvalPlus HumanEval+ (smoke test — should NOT regress)
pip install evalplus[vllm]
evalplus.evaluate \
  --model axentx/surrogate-1-coder-7b-v2-dpo \
  --dataset humaneval \
  --backend vllm \
  --greedy
# Target: ≥84% (don't regress base)

# 2. EvalPlus MBPP+ (smoke test)
evalplus.evaluate \
  --model axentx/surrogate-1-coder-7b-v2-dpo \
  --dataset mbpp \
  --backend vllm \
  --greedy
# Target: ≥75%

# 3. LiveCodeBench v6 (PRIMARY METRIC — no contamination)
git clone https://github.com/LiveCodeBench/LiveCodeBench
cd LiveCodeBench
python -m lcb_runner.runner.main \
  --model axentx/surrogate-1-coder-7b-v2-dpo \
  --scenario codegeneration \
  --evaluate \
  --release_version release_v6
# Target: v1=38% → v2≥42% → v3≥45%
```

### Tier 2 — Run monthly

```bash
# 4. Custom DevSecOps eval set (~280 tasks)
# Build from post-Q1-2026 GitHub commits, MinHash-filtered against training corpus
# Tasks: Dockerfile / K8s manifest / Terraform / Bash / CVE detection
# Validators: docker build, kubeval, terraform plan, actionlint, semgrep

python eval/devsecops_eval.py \
  --model axentx/surrogate-1-coder-7b-v2-dpo \
  --eval-set evals/devsecops-v1.jsonl \
  --validators docker,kubeval,tflint,actionlint,semgrep
# Establish v1 baseline → target v2 +15pp absolute
```

### Tier 3 — Defer to v3

- SWE-Bench Lite (60 GB Docker, agent scaffolding required)
- Aider Polyglot (multi-language)
- BigCodeBench (function-level realistic)

### Honest progress signal

- ✅ LiveCodeBench up = real improvement (no contamination possible — v6 is post-2024-09)
- ⚠️ HumanEval+ up by >5pp = SUSPICIOUS (overfit/contamination)
- ✅ DevSecOps custom eval up = direct mission alignment

---

## 5. Compute Plan

### Training (1-time per version)
- Stage 1 SFT: ~10 hr Lightning L40S
- Stage 2 DPO: ~5 hr Lightning L40S
- **Total v2 training: 15 hr** (fits free 80hr/mo Lightning quota)

### Evaluation (per checkpoint)
- 3 EvalPlus + LCB v6 = ~2 hr per checkpoint
- 11 checkpoints/month = 22 GPU hr (fits Lightning free)

### Storage (Wasabi $5.99/TB/mo + $0 egress)
- Training data: ~150 GB
- Adapter checkpoints: ~3 GB × 5 = 15 GB
- Eval outputs: ~5 GB
- **Total ~200 GB = $1.20/mo on Wasabi** (round to $6/mo for headroom)

### HF PRO ($9/mo)
- 20× API rate limit
- ZeroGPU access (free A10G/H100 for inference Spaces)
- LFS quota raise

### **Total cost estimate**
- **$15/mo** ongoing (HF PRO + Wasabi)
- **$0 compute** (Lightning + Kaggle free tiers)
- **One-time**: 0 (no paid GPU for v2)

---

## 6. Timeline (rough)

| Phase | Duration | What |
|-------|----------|------|
| **Week 1** | 7d | Build dataset (download + dedup + sanitize + filter) |
| **Week 2** | 3d | Stage 1 SFT training + EvalPlus smoke test |
| **Week 2** | 2d | Stage 2 DPO training + LiveCodeBench eval |
| **Week 2** | 2d | Custom DevSecOps eval set construction + run |
| **Week 3** | 7d | Iterate based on metrics: re-train w/ adjusted hyperparams |
| **End of W3** | — | v2 SHIPPED — target: LCB v6 ≥ 42% |

---

## 7. Risk Register

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Sanitizer over-filters (drops good rows) | Medium | High | Tune patterns based on stats from first 100K rows; add allowlist for legit conceptual mentions of "daemon", "/home" |
| LCB v6 doesn't improve (overfitting to SFT data) | Medium | High | Add strong regularization, use DPO to "shape" not "memorize" |
| DPO destabilizes (loss explodes) | Low | High | Use sigmoid loss + low lr=1e-5 + constant LR; abort if KL>5 |
| HF PRO doesn't unlock enough | Low | Med | Wasabi gives storage backbone independent of HF |
| Base model insufficient | Low | High | v2.5 has Qwen3-Coder-30B-A3B option ($300-500 paid) |

---

## 8. v2 Definition of Done

- [ ] Sanitizer integrated in dataset-mirror + dataset-enrich (DONE — commit 1dfdc54)
- [ ] Curated dataset assembled (~95K SFT + 55K DPO)
- [ ] Stage 1 SFT training complete on Lightning L40S
- [ ] Stage 2 DPO training complete
- [ ] Pushed to `axentx/surrogate-1-coder-7b-v2-dpo`
- [ ] EvalPlus HumanEval+ ≥84%, MBPP+ ≥75% (no regression)
- [ ] **LiveCodeBench v6 ≥42%** (primary success metric)
- [ ] Custom DevSecOps eval baseline established
- [ ] No data leakage in 100 random inference samples (manual spot check)
- [ ] Documentation updated: outcome.md, knowledge_index.md, README.md

---

## References

Full citations (50+ papers, tools, datasets) in the 4 research files:
- `research-training-techniques.md` (981 lines)
- `research-data-curation.md` (980 lines)
- `research-arch-context.md` (623 lines)
- `research-evaluation.md` (762 lines)

Key arXiv IDs: 2402.19173 (StarCoder2), 2406.11931 (DeepSeek-Coder-V2), 2411.04905 (OpenCoder), 2505.21297 (rStar-Coder), 2502.11475 (Focused-DPO), 2406.17557 (FineWeb), 2402.10038 (RS-DPO), 2411.15124 (Tulu 3).

---
title: Surrogate-1 v2 Master Plan FINAL2 — Complete Synthesis (16 Research Streams, 18,000+ lines)
date: 2026-04-29
tags: [surrogate-1, v2, master-plan, agent, multi-role, sre, soc, cloud, ai-eng, startup, autonomous]
status: ready-to-execute (phased)
research_corpus:
  round_1:
    - research-training-techniques.md (981 lines)
    - research-data-curation.md (980 lines)
    - research-arch-context.md (623 lines)
    - research-evaluation.md (762 lines)
  round_2:
    - research-tool-use.md (907 lines)
    - research-multi-agent.md (1,058 lines)
    - research-hallucination.md (1,272 lines)
    - research-long-context.md (837 lines)
    - research-sdlc-agentic.md (817 lines)
    - research-self-improve.md (824 lines)
  round_3:
    - research-startup-roles.md (1,143 lines)
    - research-sre-sla.md (1,250 lines)
    - research-soc-security.md (1,713 lines)
    - research-autonomous-agents.md (1,645 lines)
    - research-cloud-platform.md (1,454 lines)
    - research-ai-eng.md (1,706 lines)
  prior:
    - v1-eval-vs-base.md (143 lines)
    - agent-audit.md (~600 lines)
total_research_lines: 18,000+
---

# Surrogate-1 v2 — Complete Master Plan

## VISION (consolidated)

A single 7B model + LoRA adapters that embodies an entire software startup:
- **Builds, ships, and improves products** end-to-end (autonomous coding loops, no human prompting)
- **Validates markets** + **builds business plans** + **handles GTM**
- **Operates cloud infrastructure 24/7** (SRE: SLA/SLI/SLO/Error Budget)
- **Defends + complies** (SOC analyst, threat hunting, compliance crosswalk)
- **Builds AI products itself** (recursive: AI engineer for AI products)
- **40+ professional roles internally** across Engineering / Product / GTM / Finance / Legal / Ops

**Honest scope**: 7B + LoRA = "**Chief of Staff + Junior team across all functions**" — NOT "1 model = entire team replacement". Founder still owns: term-sheet negotiation, senior hiring, pivot decisions, deep customer empathy.

---

## CONSOLIDATED TARGET METRICS

### Code-quality (from Round 1+2)
| Metric | v1 | v2 conservative | v2 stretch |
|--------|-----|------|------|
| HumanEval+ | 70-84% | ≥84% | ≥85% |
| MBPP+ | 75% | ≥75% | ≥78% |
| **LiveCodeBench v6** | 38% | **42-45%** | 45-48% |
| **SWE-Bench Lite** | 5% | **25-30%** | 32-37% |
| **SWE-Bench Pro** | — | 8-10% | **12-15%** (half GPT-5's 23%) |
| Compile rate | 85% | **100%** (XGrammar) | 100% |
| **CodeHalu rate** | 25% | **<8%** | <5% |
| Phantom imports | 22% | <5% | <3% |
| Calibration AUC | 0.55 | **>0.85** | >0.90 |

### Tool use + multi-agent (from Round 2)
| Metric | v2 target |
|--------|-----------|
| **BFCL v3 overall** | **70-75** |
| BFCL single-turn | 80-85 |
| BFCL multi-turn | 45-50 |
| **GAIA Level 1** | **20-30%** |
| GAIA Level 2 | 8-15% |
| τ-retail pass^1 | 25-30 |
| When2Call | 75-80 |

### Long context (from Round 2)
| Metric | v2 target | Reference |
|--------|-----------|-----------|
| **RULER @ 32K** | **90+** | LongRoPE2 LLaMA3-8B = 82.0 |
| **RULER @ 128K** | **80+** | Qwen2.5-7B-1M = 84.7 |

### SDLC + DevOps (from Round 2+3)
| Metric | v2 target |
|--------|-----------|
| **DevSecOps custom eval** | **65%+** lint+sec clean |
| **Cloud Eval Tier 3** (multi-file project) | **60%** |
| **Cloud Eval overall** | **65%** (vs base 38%) |

### SRE / SOC (from Round 3)
| Metric | v2 target |
|--------|-----------|
| AIOpsLab detection+localization | parity GPT-4o |
| AIOpsLab mitigation | parity Claude Sonnet |
| **CyberMetric** | **≥75%** |
| CTI-Bench | ≥65% |
| CyberSOCEval | ≥55% |
| CISSP practice | ≥70% |
| Sigma rule synthesis | ≥70% lint-pass / ≥50% semantic |
| AWS-CDK secure review | ≥75% |
| IR runbook recall | ≥75% |
| Safety post-training | ≥80% |

### AI Engineering (from Round 3)
| Metric | v2 MVP | v2 full | Senior |
|--------|--------|---------|--------|
| `ai_eng_composite` (100 tasks × 14 capabilities) | **60%** | **70%** | 90% |

### Continuous autonomy (from Round 3)
| Metric | v2 target |
|--------|-----------|
| **Surrogate Continuous Bench** (5 scenarios) | **40%** |
| 30-day soft launch (founder goals shipped) | **≥8/10** |
| Founder time required | **≤5h/week** |

### Startup brain (from Round 3)
| Cluster | Metric | Target |
|---------|--------|--------|
| Eng-build / eng-ops | SWE-Bench Verified | ≥35% |
| Eng-build / eng-ops | τ-bench | ≥50% |
| Eng-build / eng-ops | WebArena | ≥30% |
| Product/UX | PRD jury | ≥4.2/5 |
| Product/UX | UX-handoff | ≥0.8 completeness |
| GTM | Cold email predicted reply | ≥4% |
| GTM | Blog rubric | ≥4.2/5 |
| Customer support | Bitext intent accuracy | ≥92% |
| Finance | FinanceBench | ≥35% |
| Compliance | SOC2/ISO/GDPR jury | ≥4.5/5 |
| Multi-role debate | Blind preference vs expert | ≥45% |

---

## ARCHITECTURE (consolidated)

### Base + Adapter Strategy

**Base**: `Qwen/Qwen2.5-Coder-7B-Instruct` (32K native, 88.4 HumanEval, Apache-2)

**Adapter strategy** = **9 LoRA clusters** (LoraHub composition at inference time):
1. `eng-build` — coding, frontend/backend, mobile
2. `eng-ops` — DevOps, SRE, cloud, IaC, K8s
3. `eng-sec` — DevSecOps, threat hunt, compliance
4. `eng-ai` — MLOps, RAG, fine-tune, serve
5. `product-ux` — PM, UX, design, PRDs
6. `gtm` — marketing, sales, growth, content
7. `finance-legal` — accounting, fundraising, contracts
8. `compliance` — SOC2/ISO/GDPR/HIPAA crosswalk
9. `meta-orchestrator` — multi-role debate + autonomous loops

LoRA r=64, all-linear targets, DoRA enabled. Each cluster trained independently then composed via Arrow / LoraHub at inference.

### Context strategy
- **Train at 32K** (base supports natively)
- **Inference at 128K** via YaRN+DCA (vLLM `DUAL_CHUNK_FLASH_ATTN`) + MInference 3-7× prefill speedup
- Skip 1M for v2 (needs 120GB VRAM just to RUN)

### Inference stack (deployment)
```bash
# vLLM with full optimization
VLLM_ATTENTION_BACKEND=DUAL_CHUNK_FLASH_ATTN \
vllm serve axentx/surrogate-1-coder-7b-v2 \
  --max-model-len 131072 \
  --rope-scaling '{"type":"yarn","factor":4.0,"original_max_position_embeddings":32768}' \
  --guided-decoding-backend xgrammar \
  --enable-lora \
  --max-loras 9 \
  --max-lora-rank 64 \
  --tool-call-parser hermes
```

---

## TRAINING PIPELINE (10 stages, phased)

### Phase A — v2.0 (4 weeks, ship MVP)

**Stage 0**: CPT continued pretraining (optional, +5-10%)
- 100M-500M tokens FIM (the-stack-v2-smol-ids) + repo-concat with topo-sorted imports
- 200M tokens cyber-corpus subset (Primus-FineWeb 2.57B → 200M sample)
- 200M tokens SRE-text (Google SRE Book + Workbook + Honeycomb + SREcon transcripts)
- LoRA r=32 separate adapter (not merged into final)

**Stage 1**: Code SFT 3 epochs at 32K (95K curated)
- rStar-Coder 30K, OpenCodeReasoning-2 20K, OpenCodeInstruct 10K (test_score≥0.7)
- Ling-Coder-SFT 10K, OpenCoder-LLM stage1+2 10K
- self-oss-instruct-sc2-exec-filter-50K
- m-a-p/CodeFeedback 10K (complexity≥4/5)
- axentx OWN data 5-10K (sanitized)
- ETA: ~12-15 hr H200

**Stage 1.5**: Tool-Use SFT (102K)
- NousResearch/hermes-function-calling-v1 7.93K (gold)
- xLAM-function-calling-60k 30K, Toucan-1.5M Kimi-K2 80K
- nvidia/When2Call 15K (refusal/clarify)
- Nanbeige/ToolMind 10K (graph-syn)
- nvidia/Nemotron-SWE-v1 5K, SWE-Gym/OpenHands trajectories 2.4K
- Format: Hermes XML
- ETA: ~8 hr H200

**Stage 1.6**: Multi-Agent SFT (20K + 500 synth)
- Hermes Agent Reasoning Traces 14K
- Nebius SWE-agent-trajectories filtered 5K
- SWE-Gym successful 400
- **500 synth orchestrator→subagent traces from Claude Opus 4 + Sonnet 4** (~$200) ← secret sauce
- Orca-AgentInstruct anchor 1.5K
- ETA: ~10 hr H200

**Stage 2**: Code DPO Focused-DPO (55K)
- Vezora/Code-Preference-Pairs 55K
- argilla/distilabel-capybara-dpo-7k-binarized
- Custom rejection-sampled exec-graded (Prowler/Trivy/cfn-guard graders)
- ETA: ~5 hr

**Stage 2.5**: Tool DPO (15K)
- nvidia/When2Call train_pref
- ETA: ~3 hr

**Phase A total ETA**: ~35-50 GPU-hr Lightning H200 + ~$200 synth data + Stage 0 optional

### Phase B — v2.1 (4 weeks, add domain expertise)

**Stage 3**: Cluster-specific SFT (parallel adapter training)

For each cluster trained independently then composed via LoraHub:

**3a. eng-ops (SRE/Cloud)** — 28-35K SFT + 5K DPO
- danluu postmortems 1K + Cloudflare/AWS/GCP incidents
- Scoutflo SRE Playbooks 5K (K8s troubleshooting)
- PromQL/LogQL pairs 3K
- OpenSLO YAMLs 1K (Sloth-generated)
- Multi-window multi-burn alert rules 1K
- Runbooks 5K (AWS SSM Automation + synth)
- **Sandboxed kubectl/argo/awscli rollouts in kind cluster 10K** ← critical
- Cost trade-offs 500
- IaC generation 100K (40% of cloud cluster — TF/OpenTofu/CDK/Pulumi/Bicep/Crossplane v2)
- K8s authoring 50K (Helm 4/Kustomize/ArgoCD/Karpenter/Gateway API)
- Cloud arch Q&A 35K (cert-prep + Well-Architected)
- FinOps cost optimization 25K (2025 Scopes)
- IDP patterns 25K (Backstage/Score/Humanitec)

**3b. eng-sec (DevSecOps/SOC)** — 100K SFT
- Primus-Instruct + CyberLLMInstruct (safety-filtered)
- HackMentor (defensive-only)
- Sigma synth 10K (ATT&CK→rule)
- Compliance crosswalk Q&A 5K (SOC2↔ISO↔HIPAA↔GDPR)
- AWS-CDK secure-code review 8K (codebase-aware)
- IR runbook step gen 5K (top-20 scenarios)
- LLM Top 10 defense 3K
- Bilingual TH+EN cyber Q&A 5K
- Plus: WildJailbreak + ai4privacy/pii-masking-200K + 20K custom safety-refusal pairs ← CRITICAL
- Reasoning distill from o1+R1: Primus-Reasoning 4K (+15.8% CISSP lift)

**3c. eng-ai (AI engineering)** — 180K
- LLM apps 50K (LangChain/LlamaIndex/Anthropic SDK + cookbook + synth)
- RAG 30K (Self-RAG/CRAG/HippoRAG2 + custom hybrid)
- Fine-tune 25K (TRL/PEFT/Axolotl/Unsloth + DPO/GRPO/SimPO)
- Serving 20K (vLLM/SGLang/TensorRT + multi-LoRA configs + quantization)
- MLOps 20K (MLflow/Kubeflow/Feast + drift/monitoring)
- Eval 15K (RAGAS/lm-eval/Braintrust/Garak/PyRIT)
- Agents 20K (LangGraph/CrewAI/AutoGen/DSPy programmatic prompting)

**3d. product-ux + gtm + finance-legal + compliance** — Startup brain (Round 3)
- 30K synthetic PRDs (teacher-generated)
- ProductHunt scrape + Cagan/Lenny templates
- 20K UX-handoff specs
- Bitext customer-support sets 50K
- Ad-copy datasets (smangrul, PeterBrendan, RafaM97)
- 6K cold-email sequences (4-email × 14-day)
- 30K blog briefs
- FinanceBench train slice + FinGPT-fineval
- 15K SaaS-metrics QA
- 5K legal-clause library
- 5K compliance control-row corpus (SOC2/ISO27001/HIPAA/GDPR)

**3e. meta-orchestrator** — Multi-role debate corpus
- **100K 8-turn debates synthesized via CAMEL pattern** over 1,000 startup scenarios × 4-6 rotating roles ← novel asset
- ~25M tokens, trains internal voice-switching natively

### Phase C — v2.2 (2-4 weeks, RL polish)

**Stage 4**: GRPO RLVR with validators (~$120-500 compute)
- Algorithm: GRPO (DeepSeek-R1 / Qwen3 style) via DAPO + verl framework
- Rewards (composite, validator-graded):
  - test_pass: +1.0 (E2B/Modal sandbox runs pytest)
  - lint_clean: +0.3 (hadolint/tflint/actionlint/shellcheck/kubeconform)
  - security_clean: +0.3 (semgrep/checkov/cfn-guard/cfn-nag)
  - cite_correct: +0.2 (repo-RAG citation valid)
  - no_phantom: +0.2 (imports/APIs all real)
  - honest_idk: 0.0 (TruthRL ternary neutral)
  - confident_wrong: -1.0 (heavy penalty)
- Data: SWE-Gym + R2E-Gym + custom DevSecOps + SRE sandboxed traces (~10K rollouts)
- ScalingInter-RL: start 5-step horizons → expand to 50

**Stage 5**: TruthRL + RLEF (continued GRPO with hallucination-control reward)
- TruthRL ternary reward (Sept 2025 paper, -28.9% halluc on Qwen 7B)
- CodeRL+ execution-semantics alignment (+3-4pp HumanEval+)
- 3-turn execution feedback loops, β=0.05 KL

---

## HALLUCINATION DEFENSE-IN-DEPTH (4 layers)

**Already covered in Round 2**:
1. **XGrammar** constrained decoding (vLLM 2026-04 default) — 0 syntax errors + 5× TPOT
2. **Repo-map + doc-RAG cite-or-abstain** (tree-sitter + bge-base + LlamaIndex) — DeepSeek 20%→2%
3. **TruthRL ternary** in Stage 4 GRPO — -28.9%
4. **SelfCheckGPT-NLI inference gate** — -30% confident-wrong, runs on Mac M3

---

## SELF-IMPROVEMENT STACK (built into v2)

**Already covered in Round 2**:
1. **Voyager skill library** (FAISS, exec-pass committed only)
2. **Reflexion bounded buffer** (3 reflections, structured schema)
3. **Self-Refine 3-iter loop** (exec-grounded stop)
4. **Letta 3-tier memory** (core + recall + archival via FalkorDB+Cognee GraphRAG)
5. **Trace harvester** (every task → S3/Wasabi)
6. **Weekly LoRA retrain cron** (Online DPO + replay buffer + O-LoRA + canary)
7. **Exec-grounded DPO judge** (tests pass = preferred)

---

## CONTINUOUS AUTONOMOUS LOOP (built into v2)

**From Round 3 (Devin/Manus/Cline pattern)**:
- LangGraph: planner/executor/reflector/judge nodes + SqliteSaver checkpointer
- Memory: **Letta + Cognee GraphRAG** (codebase as knowledge graph)
- Triggers: **Inngest** (cron + GitHub/Sentry/Datadog webhooks)
- Borrow:
  - **Manus** `todo.md` externalized state + virtual FS
  - **Devin** Green/Yellow/Red confidence + Interactive Planning pre-flight
  - **Cursor** planner-worker hierarchy (no peer coordination — proven 1M-LOC weeks-long)
  - **OpenHands** event-sourced replay
  - **Aider** git-as-persistence (every action = commit, revertible)

**Decision rules**:
- Rule-layer: irreversible / budget / policy gates
- Confidence-layer: act if >0.95 prod / >0.85 dev; ask if <0.6
- Hardcoded checklist: `rm -rf` / force-push / `terraform destroy` / payments → always ask

**🚨 Hard safety boundary** (from SRE agent):
- Autonomous = **read-only diagnosis + idempotent low-risk fixes only**
- Mutations across service boundary, state deletion, IaC apply, cost > $10 → **always require human approval**

---

## EVALUATION SUITE (Tier 1-3)

### Tier 1 — every checkpoint (~3 GPU-hr)
- EvalPlus HumanEval+ + MBPP+ (smoke, don't regress)
- LiveCodeBench v6 (primary code quality)
- BFCL v3 (tool use)
- RULER @ 32K, 128K (long context)

### Tier 2 — monthly (~15 GPU-hr)
- SWE-Bench Lite + SWE-Bench Verified subset
- Custom DevSecOps eval 280 tasks (Dockerfile/K8s/TF/Bash/CVE) — niche win
- Surrogate Cloud Eval ~630 prompts (5 tiers)
- AIOpsLab (SRE)
- CyberMetric + CTI-Bench + CyberSOCEval (security)
- ai_eng_composite 100 tasks (AI eng)
- GAIA Level 1
- Multi-role debate (200 scenarios, blind judge)
- PRD/UX/blog/cold-email rubrics (LLM jury)
- Surrogate Continuous Bench 5 scenarios (autonomy)

### Tier 3 — quarterly
- SWE-Bench Pro (Scale AI)
- BigCodeBench
- Aider Polyglot
- 30-day soft launch test (founder goals shipped, time required)

---

## SANITIZATION (deployed — commit 1dfdc54)

10 categories already integrated. Plus:
- **Layer 2**: BigCode `starpii` NER (NAME/EMAIL/KEY/PASSWORD/IP/USERNAME)
- **Layer 3**: Yelp `detect-secrets` (AWS/GitHub/Slack/Stripe/20+ plugins)
- **Layer 4** (NEW): Safety post-training restoration (WildJailbreak + 20K refusal pairs) — fix the 0.95→0.15 collapse from CyberLLMInstruct paper

---

## COST + TIMELINE (REVISED — honest)

### Phase A — v2.0 MVP (4 weeks)
- Compute: ~50 GPU-hr H200 = $200 (or free Lightning quota if available)
- Synthetic data: $200 (Claude Opus orchestrator traces) + $0 (existing free LLM bursts for other synth)
- Storage: Wasabi 1 TB = $6/mo
- HF PRO: $9/mo
- **Total Phase A**: ~$400 cash + $15/mo

### Phase B — v2.1 cluster expertise (4 weeks)
- Compute: ~80 GPU-hr (5 clusters × 15 hr) = $300-500
- Synthetic data: $500-1500 (multi-role debate, PRDs, cold emails, sigma rules generation via Claude/GPT-4o)
- **Total Phase B**: ~$800-2000

### Phase C — v2.2 RL polish (2-4 weeks)
- GRPO + RLVR: 24 hr × 8× H100 = ~$300-1200 (RunPod spot or paid Lightning)
- TruthRL: ~$200
- **Total Phase C**: ~$500-1400

### **Grand Total v2 build**: $1,700-3,800 cash + $15/mo recurring + 10-12 weeks calendar

(vs original v2 estimate of $300-500 — scope expanded 5-8× with Round 3 additions)

### Per-LoRA-cluster fine-tune budget (Phase B)
- Each cluster ~$60-200 + 10-15 hr H200
- Can be parallelized across 9 clusters if budget allows

---

## HONEST SCOPE — what v2 will NOT do

From SOC agent (literally said):
- ❌ Autonomous IR (Incident Response with action authority)
- ❌ Real-time threat triage (sub-second)
- ❌ Malware reverse engineering
- ❌ Exploit / 0-day generation
- ❌ Active Directory attack chain execution
- ❌ Certified pentesting

From SRE agent:
- ❌ State-mutating actions across service boundary without approval
- ❌ IaC apply without human review
- ❌ Cost > $10 actions
- ❌ State deletion (DB drop, etc.)

From startup-brain agent:
- ❌ Term-sheet negotiation (founder owns)
- ❌ Senior hiring decisions
- ❌ Pivot decisions
- ❌ Deep customer empathy / qualitative research interpretation
- ❌ Visionary product strategy

From autonomous loops agent:
- ❌ Continuous run on user's Mac (training needs cloud H100)
- ❌ Beyond 50-step horizons (start with 5)

**Realistic positioning**: Surrogate-1 v2 = **"Chief of Staff + Junior Team across 9 functions, with hard safety boundaries"**. Founder-replacing or SR-engineer-replacing claims are **over-promised**.

---

## v2 DEFINITION OF DONE (consolidated checklist)

### Data + Sanitization
- [x] Sanitizer deployed (commit 1dfdc54) — 10 categories
- [ ] BigCode starpii NER + Yelp detect-secrets layered
- [ ] WildJailbreak + 20K refusal pairs (safety post-training)
- [ ] ~1M curated samples assembled (250K Phase A + 800K Phase B)
- [ ] 100K multi-role debate corpus synthesized (~$300)
- [ ] 500 orchestrator traces from Claude Opus (~$200)

### Training (Phase A — MVP)
- [ ] Stage 0 CPT (optional, 100M-500M tokens)
- [ ] Stage 1 Code SFT 3 epochs at 32K
- [ ] Stage 1.5 Tool-SFT (Hermes XML)
- [ ] Stage 1.6 Multi-Agent SFT (orchestrator pattern)
- [ ] Stage 2 Code DPO (Focused-DPO)
- [ ] Stage 2.5 Tool DPO

### Training (Phase B — Cluster expertise)
- [ ] Stage 3a eng-ops cluster
- [ ] Stage 3b eng-sec cluster
- [ ] Stage 3c eng-ai cluster
- [ ] Stage 3d product-ux + gtm + finance + compliance clusters
- [ ] Stage 3e meta-orchestrator (multi-role debate)
- [ ] LoraHub composition setup

### Training (Phase C — RL)
- [ ] Stage 4 GRPO RLVR with validators
- [ ] Stage 5 TruthRL + RLEF

### Infrastructure
- [ ] vLLM serving with XGrammar + DCA + MInference + multi-LoRA
- [ ] Voyager skill library bootstrap (~50 seed skills)
- [ ] Letta + Cognee GraphRAG memory backend
- [ ] Inngest trigger system (cron + webhooks)
- [ ] Trace harvester pipeline (Wasabi)
- [ ] Weekly LoRA retrain cron with canary
- [ ] Decision-rule + confidence-layer gating

### Eval (must pass)
- [ ] HumanEval+ ≥84%, MBPP+ ≥75%
- [ ] **LiveCodeBench v6 ≥42%**
- [ ] **SWE-Bench Lite ≥25%**
- [ ] **BFCL v3 overall ≥70**
- [ ] RULER @ 32K ≥90, @ 128K ≥80
- [ ] CodeHalu rate <8%
- [ ] DevSecOps custom ≥65% lint+sec
- [ ] CyberMetric ≥75%
- [ ] AIOpsLab parity GPT-4o detection+localization
- [ ] ai_eng_composite ≥60% MVP / ≥70% full
- [ ] Multi-role debate ≥45% blind preference
- [ ] Surrogate Continuous Bench ≥40%
- [ ] **30-day soft launch ≥8/10 founder goals**, founder time ≤5h/week

### Documentation
- [ ] outcome.md per phase
- [ ] knowledge_index.md ≥30 patterns appended
- [ ] HF Hub README for each LoRA cluster
- [ ] User runbook (how to deploy + use Surrogate-1 v2)

---

## v3 ROADMAP (deferred)

| Feature | Why deferred | Phase |
|---------|--------------|-------|
| **Qwen3-Coder-30B-A3B base** (256K native, 1M YaRN, MoE) | needs 8× H200 (~$300-500/run) | v3 |
| **Self-Rewarding LM iterative DPO** | needs >30B judge (distill from Claude first) | v3+ |
| **STOP self-modifying scaffolding** | blast radius too high for 7B | v4 |
| **Self-Play SWE-RL bug-injector/fixer** | needs robust sandbox infra | v3 |
| **Online DPO during serving** | start weekly offline first | v3 |
| **Meta-rewarding (judge-of-judges)** | only after self-rewarding works | v4 |
| **1M context training** | needs 120GB VRAM run + 10B+ tokens data | v3+ |
| **Browser/computer use multi-modal** | Qwen2.5-VL or successor | v3+ |
| **R1-style thinking + tools** | needs more orchestrator data | v3 |
| **LoraHub Arrow composition (production)** | need ≥4 mature domain LoRAs | v3 |

---

## KEY SOURCES (consolidated)

200+ paper citations across 16 research files. Top 30 high-leverage:

**Training**: arxiv 2402.19173 (StarCoder2), 2406.11931 (DeepSeek-Coder-V2), 2411.04905 (OpenCoder), 2505.21297 (rStar-Coder), 2502.11475 (Focused-DPO), 2310.02304 (STOP), 2506.11425 (Agent-RLVR), 2503.18455 (SEAlign), 2503.14476 (DAPO)

**Tool/Multi-agent**: NousResearch hermes-fc-v1, Salesforce/xlam, Toucan-1.5M, When2Call, Anthropic Multi-Agent Research, Klear-AgentForge, AgentRL

**Hallucination**: 2509.23045 (TruthRL), 2406.04692 (MoA), arxiv 2512.12117 (DeepSeek RAG citation), 2410.02089 (RLEF)

**Long context**: 2501.15383 (Qwen2.5-1M), 2502.20082 (LongRoPE2), 2309.00071 (YaRN), 2501.12766 (NExtLong), 2407.02490 (MInference)

**SDLC/SWE**: 2412.21139 (SWE-Gym), 2504.21798 (SWE-smith), 2504.07164 (R2E-Gym), 2509.16941 (SWE-Bench Pro), 2402.01030 (CodeAct), 2407.16741 (OpenHands)

**SRE**: Google SRE Book/Workbook, AIOpsLab MLSys 2025, ITBench ICML 2025, OpenSLO

**Security**: 2503.09334 (CyberLLMInstruct), 2506.11791 (SEC-bench), CyberSOCEval (Meta+CrowdStrike Sept 2025), Primus dataset (Trend Micro)

**Cloud**: terraform-aws-modules + Backstage + AWS Well-Architected + Crossplane v2 + IaC-Eval + CodeFuse DevOps-Eval

**AI Eng**: Self-RAG/CRAG/HippoRAG2 papers + DSPy + LangGraph + RAGAS + Garak/PyRIT

**Self-improvement**: 2305.16291 (Voyager), 2303.11366 (Reflexion), 2303.17651 (Self-Refine), 2409.12917 (SCoRe), 2401.10020 (Self-Rewarding), Letta v1, Cognee, MS GraphRAG

**Autonomy**: Devin annual review, Manus architecture, Cursor scaling agents, Agent Data Protocol 2510.24702

**Eval**: 2404.06654 (RULER), 2410.02694 (HELMET), LiveCodeBench, SWE-Bench Verified, AIOpsLab, CyberSOCEval

---

**Status**: ready to execute Phase A. Awaits user decision on:
1. **HF PRO + Wasabi** ($15/mo) — unblocks Phase A
2. **Phase B+C compute budget** ($1,300-3,500 one-time) — phased with Phase A results

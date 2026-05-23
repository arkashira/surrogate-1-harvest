---
tags: [surrogate-1, v9, sre-specialist, training-spec]
created: 2026-05-01
status: design — implementation starts now
supersedes: v8 (which was dataset-light + harness-heavy)
applies-to: axentx/surrogate-1-coder-14B-v1.5-sre OR axentx/surrogate-1-coder-32B-v2-sre
---

# Surrogate-1 V9 — SRE-Specialist Trainer Spec

> Goal: bake SENIOR-LEVEL SRE/DevSecOps engineer capability INTO the
> model weights, so calling Surrogate-1 from any harness yields the
> same quality as having a real expert on call 24×7.

---

## What V8 actually was vs what V9 is

| Aspect | V8 (current) | V9 (this spec) |
|---|---|---|
| Knowledge corpora baked in | 0 (all data was instruction pairs) | **15 corpora** distilled to ~80K Q&A pairs |
| Role personas | 0 (single generic system prompt) | **6 role-specific** prompt+data buckets (~6K pairs) |
| Trajectory data | 5 datasets, ~30K pairs | **12 datasets**, ~120K pairs |
| Total training signal | ~50-100K pairs | **~250-300K pairs** (3× more) |
| RL technique | GRPO scaffolded (default OFF) | **GRPO default ON** + DPO Phase 3 + Constitutional AI |
| Hallucination control | none explicit | **TruthRL + Reflexion store** |
| Long context | 8K trained, YaRN ×2 serve | **32K trained** with YaRN-aware curriculum |
| Per-role eval | none | **6 role evals × 50 cases = 300** + cloud-ops + o11y |
| Compute target | Kaggle T4×2 16+16 GB | **Civo L40S 48 GB** (32B 4-bit fits cleanly) |

---

## Section A — Knowledge corpora (15 sources, distilled to Q&A)

Each corpus pulled from public docs, distilled by frontier-model
(Cerebras/Groq when in quota) into instruction pairs the 7B/14B/32B can
absorb. ~5K pairs per corpus → ~75K total.

| # | Source | Pairs | License | Why |
|---|---|---|---|---|
| 1 | AWS service docs (S3/EC2/RDS/Lambda/IAM/VPC/CloudWatch core 30 services) | 8K | AWS docs, public | senior-level cloud reasoning |
| 2 | Kubernetes docs (kubectl + objects + networking + storage) | 5K | CC-BY-4.0 | K8s ops |
| 3 | Terraform + CloudFormation + CDK + Pulumi | 5K | MPL-2.0 + others | IaC fluency |
| 4 | Prometheus + Grafana + Loki + Tempo + OTel | 5K | Apache-2.0 | observability |
| 5 | Google SRE Workbook + Production-Ready Microservices | 4K | derivative use | SRE patterns |
| 6 | Postmortem corpus (PagerDuty + HashiCorp + Zalando + Stripe public) | 3K | derivative | incident reasoning |
| 7 | CVE + EPSS + KEV (top 5K vulns 2024-2026) | 5K | NVD public | security knowledge |
| 8 | MITRE ATT&CK Enterprise (16 tactics × 84 techniques + mitigations) | 4K | CC-BY-4.0 | adversary modeling |
| 9 | CIS Benchmarks (AWS + GCP + Azure + K8s + Linux + Windows) | 5K | CC-BY-NC | hardening |
| 10 | NIST 800-53 r5 + 800-171 + 800-190 | 4K | public domain | controls mapping |
| 11 | SLSA + SBOM + Sigstore + in-toto | 3K | CC-BY | supply chain |
| 12 | Cilium + eBPF + Hubble | 3K | Apache-2.0 | network observability |
| 13 | FinOps Foundation framework + cost optimization | 3K | CC-BY | cost reasoning |
| 14 | SOC2 + PCI-DSS + HIPAA + GDPR controls | 3K | derivative | compliance reasoning |
| 15 | Cloud security: Prowler + ScoutSuite + Wiz playbooks | 4K | derivative | scanner-aware |

**Pipeline**: `bin/v2/build-knowledge-corpus.sh` (NEW) — pulls each source,
distills via Cerebras/Groq Mixtral 8×22B, deduplicates with MinHash,
pushes to `axentx/surrogate-1-knowledge-{aws,k8s,iac,o11y,...}` HF
datasets. Trainer streams from these via `merge_external()`.

---

## Section B — Trajectory expansion (12 datasets, ~120K pairs)

| # | Source | Already in V8 | Add for V9 |
|---|---|---|---|
| 1 | ToolACE | ✓ 8K | bump to 16K |
| 2 | Multi-IaC-Eval | ✓ 5K | bump to 10K (all 3 formats) |
| 3 | xLAM-fn-call-60k | ✓ 10K | bump to 20K |
| 4 | ITBench-Trajectories | ✓ 3K | bump to 6K (all categories) |
| 5 | Code-Feedback | ✓ 8K | bump to 12K |
| 6 | SWE-Gym | ❌ | NEW: 15K (real PR/issue traces) |
| 7 | OpenDevin trajectories | ❌ | NEW: 5K (run on cdk-infrastructure/) |
| 8 | Cline traces | ❌ | NEW: 3K |
| 9 | Aider traces | ❌ | NEW: 3K |
| 10 | NL2Bash++ | ❌ | NEW: 5K (shell command corpus) |
| 11 | AIOps incident traces (Microsoft AzureIncident dataset) | ❌ | NEW: 5K |
| 12 | Magpie self-instruct | partial | bump to 20K |

**Pipeline**: extend `kaggle-trainer.sh` `merge_external()` calls.

---

## Section C — 6 Role personas (system-prompt + data buckets)

Each role gets:
- Specific system prompt
- 1K specialized training pairs (synthesized via frontier-model on role-specific scenarios)
- A persona-specific section of the bench

| Role | System prompt seed | Specialty training data |
|---|---|---|
| **Guardian** (security) | "You are Guardian, a senior security engineer focused on threat detection, vulnerability management, and incident containment. Cite CVE/MITRE/CIS." | CVE patches + CWE explanations + Prowler findings + ATT&CK mappings |
| **Navigator** (planner) | "You are Navigator, a senior architect designing multi-step deployments. Output spec.md → plan.md → checklist.md (Spec-Driven Dev)." | Architecture decisions + ADRs + plan documents |
| **Assembler** (builder) | "You are Assembler, a senior platform engineer turning plans into IaC + CI/CD pipelines that pass cfn-guard/tfsec/checkov." | Multi-IaC + Terraform modules + Helm charts |
| **Sherlock** (incident) | "You are Sherlock, a senior SRE doing root-cause analysis. Read logs/metrics/traces, propose 5-Whys + blast radius." | Postmortem corpus + log analysis + trace correlation |
| **Auditor** (compliance) | "You are Auditor, a compliance engineer mapping changes to SOC2/PCI/HIPAA/NIST controls. Output evidence trail." | Controls mappings + audit reports |
| **Coach** (mentor) | "You are Coach, a senior engineer teaching juniors. Explain concepts at the right level, suggest best practices, and link to docs." | Teaching dialogues + best-practice explanations |

**Implementation**: `bin/v2/generate-role-personas.py` (NEW) — for each
role, take 1K seed scenarios from arkship `decisions/` + public corpora,
prompt frontier model to write expert response, save as
`axentx/surrogate-1-roles-{guardian,navigator,...}` HF datasets.

Trainer adds role-specific system prompt randomly during training so
inference-time the same model can wear any of 6 hats based on system prompt.

---

## Section D — Training techniques (V9 enables what V8 deferred)

| # | Technique | V8 status | V9 status |
|---|---|---|---|
| 1 | LoRA r=64 + DoRA + RSLoRA + LoftQ/PiSSA/CorDA | ✓ | ✓ + try `loftq+pissa` hybrid by default |
| 2 | Spectrum-lite (top-70%) | ✓ | ✓ + **Spectrum proper** (SNR-based, Hayou) |
| 3 | LoRA+ (lr_B = 16·lr_A) | ✓ | ✓ |
| 4 | NEFTune α=5 | ✓ | ✓ |
| 5 | cosine_with_restarts × 3 | ✓ | ✓ |
| 6 | Sample packing | ✓ | ✓ |
| 7 | AdamW 8-bit paged | ✓ | ✓ + try APOLLO-Mini for memory |
| 8 | Magpie self-instruct | partial | bump to 20K |
| 9 | Active-learning teachable filter | ✓ | ✓ |
| 10 | **GRPO + execution-pass reward** | scaffolded OFF | **DEFAULT ON** w/ proper sandbox |
| 11 | **DPO/SimPO Phase 3** | ❌ | NEW: trained on outcomes.jsonl pref pairs |
| 12 | **KTO unpaired** | ❌ | NEW: every outcome label used |
| 13 | **Constitutional AI v2** | ❌ | NEW: SRE constitution as RLAIF reward |
| 14 | **Reflexion store** | ❌ | NEW: failure → correction pairs in training |
| 15 | **Self-Rewarding LM** | ❌ | NEW: model judges its own outputs |
| 16 | **TruthRL** | ❌ | NEW: penalize hallucinated APIs/CVEs |
| 17 | **DyT model surgery** | ❌ | NEW: post-train depth pruning ~10% smaller |
| 18 | **YaRN-trained 32K context** | serve-only | NEW: train at 32K with YaRN-aware curriculum |
| 19 | **SDFT continual** | ❌ | NEW: prevents catastrophic forgetting on refresh |
| 20 | **Knowledge distillation from frontier** | ❌ | NEW: when Cerebras/Groq quota allows |
| 21 | **MCTS-style decoding** | ❌ | inference-side, not trainer; defer to V9.5 |

---

## Section E — Per-role + cloud evals (axentx-eval-300)

Replace single axentx-eval-50 with a 6-role × 50-case suite:

| Eval | Cases | Source |
|---|---|---|
| Guardian-50 | 50 | CVE patch tasks + Prowler finding remediation |
| Navigator-50 | 50 | Architecture decisions w/ ADR scoring |
| Assembler-50 | 50 | IaC tasks (must pass cfn-guard/tfsec) |
| Sherlock-50 | 50 | Incident logs → root-cause + fix |
| Auditor-50 | 50 | Compliance gap → controls mapping |
| Coach-50 | 50 | Junior question → expert explanation |
| **axentx-eval-300 total** | **300** | union |

Plus public benches we already had + new ones:

| Eval | What it measures | Add to V9 |
|---|---|---|
| HumanEval+, MBPP+, LCB v6, BFCL v3, RULER, SWE-Bench, Multi-IaC, ITBench-lite | already in V8 | ✓ |
| **CloudOpsBench** (452 K8s digital-twin scenarios, Feb 2026) | end-to-end cloud ops | NEW |
| **O11yBench** (63 PromQL/LogQL/TraceQL, Apr 2026) | observability fluency | NEW |
| **AIOps-Lab RCA** | microservice fault localization | NEW |
| **Postmortem-quality eval** (Zalando-style judge) | incident writing | NEW custom |

---

## Section F — Compute + timeline

V8 fits Kaggle T4×2 because data was small (~50-100K pairs). V9 with
~250-300K pairs + GRPO Phase 2 + DPO Phase 3 + 32K context training **does
not fit T4×2**.

Realistic options:

| Path | Compute | Cost | Time | Pros / Cons |
|---|---|---|---|---|
| **A. V9 on Civo L40S 48 GB** | $250 reserved | ~24-48 hr SFT + ~12 hr GRPO + ~8 hr DPO | YES — uses the budget pee already set aside | recommended |
| B. V9 trimmed for Kaggle | T4×2 free | 8-12 hr | only fits 7B + 100K pairs (less than V8 promise) | back-step |
| C. V9 split across multiple Kaggle runs | T4×2 free | 30+ hr human-managed | painful manual orchestration | not recommended |

**Recommended: A (Civo)** — exactly what the $250 was reserved for.

Civo timeline:
- **T+0 .. T+4 hr** : data prep (knowledge corpora distillation via Cerebras/Groq + role-persona generation + dedup + push to Hub) — LOCAL, not Civo
- **T+4 .. T+30 hr** : Civo L40S 48 GB SFT phase (~250K pairs × 2 epochs)
- **T+30 .. T+42 hr** : GRPO Phase 2 (execution rewards, smaller batch)
- **T+42 .. T+50 hr** : DPO Phase 3 (preference pairs from outcomes.jsonl + KTO labels)
- **T+50 .. T+58 hr** : Constitutional AI / Self-Rewarding final polish
- **T+58 hr** : push `axentx/surrogate-1-coder-14B-v2-sre` (if 14B base) or `-32B-v2-sre`

**Total Civo cost** : ~$50-80 of the $250 budget for 14B, ~$120-180 for 32B.

---

## Section G — What we keep from V8 (don't throw away)

The harness IS valuable, even if V8-the-model wasn't. Keep:

1. **verifier-ensemble.py** + 14 HardGuards — runtime safety, framework-agnostic
2. **autonomous-sre.sh** + chicken-and-egg fix (skip LLM diagnosis when LLM is the broken thing)
3. **autonomous-release.sh** + CISC voting — applies to V9 the same way
4. **self-improve.sh** — flywheel feeds V9.1, V9.2 future rounds
5. **watchdog.sh** — stayed correct, killed cascade as designed
6. **idempotency.py** — no rework
7. **bench-v1-vs-v15.sh** — extend with new evals, keep structure
8. **Phase A arkship cleanup** — 5 services migrated + 39 docs archived stays
9. **post-bench-decide.sh** A/B/C dispatcher — extend with V9 thresholds

**Phase B arkship work (95 file diff/merge)** waits for V9 because V8 is
not capable enough; V9 with all role personas + Multi-IaC + SWE-Gym
trajectories is the right tool for the job.

---

## Section H — Deliverables (this session forward)

Implementation order:

1. ✓ This spec doc (you're reading it)
2. → `bin/v2/build-knowledge-corpus.sh` — distill 15 corpora via Cerebras/Groq into HF datasets
3. → `bin/v2/generate-role-personas.py` — 6K role-specific pairs synthesized
4. → `bin/v2/v9-trainer.sh` — comprehensive trainer (extends kaggle-trainer.sh, targets Civo)
5. → `bin/v2/civo-train-launcher.sh` — already exists; extend to V9 config
6. → `bin/v2/build-axentx-eval-300.py` — per-role bench expansion
7. → `bin/v2/bench-v1-vs-v15.sh` — add CloudOpsBench + O11yBench + AIOps-Lab
8. → Re-arm overnight pipeline AFTER V9 deploys, with chicken-and-egg fix

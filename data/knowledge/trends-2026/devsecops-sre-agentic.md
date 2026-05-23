---
name: DevSecOps + SRE Agentic Training Corpus 2026
description: Datasets, benchmarks, and runtime patterns to harden Surrogate-1 (Qwen2.5-Coder-7B) into a senior DevSecOps + SRE + AI Engineer.
tags: [trends, devsecops, sre, agentic, tool-use, fine-tune, benchmark, surrogate-1, 2026]
last_updated: 2026-05-01
related:
  - "[[devsecops-sre-platform]]"
  - "[[cloud]]"
  - "[[data-ml-aiops]]"
---

# DevSecOps + SRE Agentic Training Corpus 2026

Curated for **Surrogate-1** (Qwen2.5-Coder-7B base, M3 24GB host, training on Lightning H200 / Modal). Goal: senior DevSecOps + SRE + AI Engineer who debugs cloud incidents, writes IaC that passes cfn-guard / tfsec / checkov / Prowler, calls AWS CLI / kubectl / terraform / prowler / semgrep / trivy correctly, follows runbooks deterministically, reasons over CWE/CVE/MITRE/CIS/NIST, reads observability signals, writes postmortems.

All entries: **(1) name + year**, **(2) availability + license**, **(3) SFT mix recipe**, **(4) runtime use**.

---

## 1. Tool-Use / Function-Calling Corpora — base of agentic skill

### 1.1 ToolACE (ICLR 2025) — top SFT pick
- 26,507 APIs, multi-turn, parallel-call, dependency graphs. Self-evolution synthesis. Beats GPT-4 on BFCL. [HF Team-ACE/ToolACE](https://huggingface.co/datasets/Team-ACE/ToolACE), [arXiv 2409.00920](https://arxiv.org/abs/2409.00920)
- **License**: Apache-2.0 (model). Subset of data on HF, **commercial-OK**.
- **SFT recipe**: weight **1.5x**. Convert to Qwen tool-call schema (`<tool_call>{"name":...,"arguments":...}</tool_call>`). Use `multi_turn_*.json` shards.
- **Runtime**: tool-schema patterns are reusable as prompt templates for new tools we add (kubectl, prowler).

### 1.2 Hermes-Function-Calling-v1 → v3 (NousResearch, 2024–2025)
- ShareGPT-format multi-turn + JSON-mode + agentic. Powered Hermes-2-Pro / Hermes-3 / Hermes-4.3-36B. [HF NousResearch/hermes-function-calling-v1](https://huggingface.co/datasets/NousResearch/hermes-function-calling-v1), [Hermes-3 report](https://arxiv.org/pdf/2408.11857)
- **License**: Apache-2.0. **Commercial-OK**.
- **SFT recipe**: weight **1.0x**. Native ShareGPT → conv format. Mix with ToolACE (different distribution; less surface bleed).
- **Runtime**: schema for `<tool_call>` / `<tool_response>` tags — adopt directly.

### 1.3 xLAM-function-calling-60k + APIGen-MT (Salesforce, 2024–2025)
- 60k single-turn rigorously verified, generated via APIGen across 3,673 executable APIs, 21 categories. xLAM-2-70b hits 56.2% on τ-bench (>GPT-4o). [HF Salesforce/xlam-function-calling-60k](https://huggingface.co/datasets/Salesforce/xlam-function-calling-60k), [xLAM repo](https://github.com/SalesforceAIResearch/xLAM)
- **License**: CC-BY-4.0 (dataset), commercial-OK with attribution.
- **SFT recipe**: weight **1.0x**. Dedupe APIs that overlap with ToolACE.
- **Runtime**: APIGen-MT pipeline can synthesize **AWS-specific** tool-call data in-house (we own the gen loop).

### 1.4 ToolMind (Nanbeige, Nov 2025) — reasoning-enhanced
- 360k samples (160k synthesized + 200k augmented from xLAM/APIGen/Glaive). Function-graph + multi-agent simulation + turn-level filter. [HF Nanbeige/ToolMind](https://huggingface.co/datasets/Nanbeige/ToolMind), [arXiv 2511.15718](https://arxiv.org/html/2511.15718v2)
- **License**: Apache-2.0. **Commercial-OK**.
- **SFT recipe**: weight **0.7x** (overlaps prior). Best for reasoning chains with tool calls.
- **Runtime**: function-graph trick = great template for our tool-graph (kubectl→logs→metrics→fix).

### 1.5 ActionStudio / ACTIONSTUDIO-98K (Salesforce, Mar 2025)
- 98k high-quality trajectories, 30k+ APIs, 300+ domains, Unified Format 2.0, critique-and-filter pipeline. [arXiv 2503.22673](https://arxiv.org/pdf/2503.22673)
- **License**: Apache-2.0. **Commercial-OK**.
- **SFT recipe**: weight **0.8x**. Trajectory-level training (RL-friendly).
- **Runtime**: Unified Format 2.0 worth standardizing on for our trajectory store.

### 1.6 Glaive-function-calling-v2 (52k)
- Older but clean. [HF glaiveai/glaive-function-calling-v2](https://huggingface.co/datasets/glaiveai/glaive-function-calling-v2). Apache-2.0.
- **SFT**: weight **0.3x**. Diversity filler only — heavily subsumed by ToolACE/xLAM.

### 1.7 NexusRaven-V2 + Nexus Function Calling Benchmark (2024)
- Trained on commercially-clean data (no GPT-4 contamination). Public benchmark = 8 of 9 tasks. [GitHub nexusflowai/NexusRaven-V2](https://github.com/nexusflowai/NexusRaven-V2)
- **License**: Nexusflow community license — read carefully; OK for our use, not for redistribution as-is.
- **SFT recipe**: skip dataset (sparse), **use benchmark for eval** instead.

### 1.8 Granite-Function-Calling (IBM, 2024–2025)
- Granite-20B-FunctionCalling trained on Glaive-v2 + curated. Apache-2.0. [arXiv 2407.00121](https://arxiv.org/html/2407.00121v1), [Granite Code Models](https://github.com/ibm-granite/granite-code-models)
- **SFT**: training **recipe** more useful than dataset (multi-task: nested calls, parallel, slot-filling). Reproduce 7-task taxonomy on our data.

### 1.9 BFCL v3 / v4 (Berkeley, 2024–2026) — eval, not train
- Multi-turn, multi-step, **state-based grading** (not AST match). 1000 cases × file-system / vehicle / trade / travel. [BFCL leaderboard](https://gorilla.cs.berkeley.edu/leaderboard.html), [BFCL v3 blog](https://gorilla.cs.berkeley.edu/blogs/13_bfcl_v3_multi_turn.html)
- **Use**: bake into `bench-v1-vs-v15.sh` as the canonical tool-use gate.

---

## 2. Software-Engineering Agent Corpora

### 2.1 SWE-Gym (ICML 2025)
- 2,438 real Python tasks, executable env, unit tests. OpenHands-7B 11→14.6% after RL. SOTA among open agents (32% Verified). [arXiv 2412.21139](https://arxiv.org/abs/2412.21139), [GitHub](https://github.com/SWE-Gym/SWE-Gym)
- **License**: MIT. **Commercial-OK**.
- **SFT**: **trajectories only** (filtered successful runs), weight **1.5x**. Stage-2 after tool-use SFT.
- **Runtime**: env reusable for self-play / online RL.

### 2.2 SWE-smith (NeurIPS 2025 Spotlight)
- 50k instances from 128 repos — **10x prior**. Auto-synthesizes test-breaking tasks. [arXiv 2504.21798](https://arxiv.org/abs/2504.21798), [GitHub](https://github.com/SWE-bench/SWE-smith)
- **License**: MIT. **Commercial-OK**.
- **SFT**: weight **1.0x** for breadth. Trajectories from `swesmith.com` releases.
- **Runtime**: pipeline generates new tasks per repo we onboard (Surrogate-1 self-improves on user repos).

### 2.3 SWE-RL / Llama3-SWE-RL-70B (Meta, NeurIPS'25)
- RL on open-source software evolution + rule-based reward. 41% on SWE-Bench Verified at <100B. [arXiv 2502.18449](https://arxiv.org/abs/2502.18449), [GitHub facebookresearch/swe-rl](https://github.com/facebookresearch/swe-rl)
- **License**: Llama Community (commercial-OK ≤700M MAU).
- **SFT**: **recipe**, not data. Lift the rule-based reward (lint pass + test pass + diff format) for our RL stage.

### 2.4 R2E-Gym (COLM 2025)
- Procedurally-generated env, hybrid verifiers, **51% SWE-Bench Verified**. [r2e-gym.github.io](https://r2e-gym.github.io/)
- **License**: MIT.
- **Use**: training env for DeepSWE-style RL post-SFT.

### 2.5 DeepSWE-Preview (Together / Agentica, Jul 2025)
- Qwen3-32B + RL on R2E-Gym → 59% SWE-Bench Verified (Pass@1 42.2%). Training code + dataset + logs all open. [Together blog](https://www.together.ai/blog/deepswe), [HF agentica-org/DeepSWE-Preview](https://huggingface.co/agentica-org/DeepSWE-Preview)
- **Use**: blueprint our 7B RL run. rLLM framework.

### 2.6 Coding agent trajectories — OpenDevin / Cline / Aider
- No clean public bulk dataset, but **harvest your own** by running these tools with logging. OpenDevin CodeAct 1.0 patterns directly applicable. [OpenDevin CodeAct](https://xwang.dev/blog/2024/opendevin-codeact-1.0-swebench/)
- **SFT**: **bootstrap** — run Aider/Cline against `cdk-infrastructure/` real tasks, capture trajectories, filter by green CI.

---

## 3. SRE / AIOps / Incident Benchmarks

### 3.1 ITBench (IBM, 2025) — flagship for our domain
- 102 scenarios across SRE / CISO / FinOps. Replicates real K8s incidents. SOTA agents: 11.4% SRE / 25.2% CISO. [arXiv 2502.05352](https://arxiv.org/abs/2502.05352), [GitHub](https://github.com/itbench-hub/ITBench), [HF ibm-research/ITBench-Trajectories](https://huggingface.co/datasets/ibm-research/ITBench-Trajectories)
- **License**: Apache-2.0 (framework) + dataset on HF.
- **SFT**: ITBench-Trajectories → weight **2.0x**. Highest signal for our use case.
- **Runtime**: deploy ITBench locally as our **eval harness** for SRE skills. CrewAI baselines included.

### 3.2 AIOpsLab (Microsoft, MLSys 2025)
- Microservice fault injection + telemetry. Tasks: detection, localization, RCA, mitigation. ReAct/Autogen/TaskWeaver baselines. [Microsoft Research](https://www.microsoft.com/en-us/research/publication/aiopslab-a-holistic-framework-for-evaluating-ai-agents-for-enabling-autonomous-cloud/), [GitHub microsoft/AIOpsLab](https://microsoft.github.io/AIOpsLab/)
- **License**: MIT.
- **SFT**: trajectories from our own runs (not redistributed by MS).
- **Runtime**: eval gate. Spin up sock-shop / hipster-shop, inject faults, score Surrogate-1.

### 3.3 RCAEval (WWW 2025)
- 735 K8s fault cases, 11 fault types, full telemetry (Prometheus + Loki + Jaeger). 15 reproducible baselines. [arXiv 2412.17015](https://arxiv.org/abs/2412.17015), [GitHub](https://github.com/phamquiluan/RCAEval)
- **License**: MIT.
- **SFT**: synthesize RCA reasoning traces from `(fault_signature, telemetry_snapshot, root_cause)` — weight **1.5x**.
- **Runtime**: live RCA eval suite.

### 3.4 Cloud-OpsBench (Feb 2026) — newest
- 452 fault cases, 40 root cause types, **full K8s stack**. State Snapshot Paradigm = digital twin. CrewAI + Pydantic + Langfuse trace capture. **Explicitly designed for SFT bootstrap + RL env**. [arXiv 2603.00468](https://arxiv.org/abs/2603.00468)
- **License**: check repo (recent paper).
- **SFT**: trajectories from harvested runs → weight **1.5x**.
- **Runtime**: our **production-grade RL env**. Best signal for cloud incident debugging.

### 3.5 o11y-bench (Grafana, Apr 2026)
- 63 tasks: PromQL / LogQL / TraceQL / multi-step investigation / dashboard edits. Pass^3 + Pass@3 metrics. Real Grafana stack. [Grafana blog](https://grafana.com/blog/o11y-bench-open-benchmark-for-observability-agents/), [GitHub grafana/o11y-bench](https://github.com/grafana/o11y-bench)
- **License**: open.
- **Use**: **eval-only** — directly maps to "read observability signals" goal.

### 3.6 Microsoft Local Triage / AIOps Triangle (production data, paper-only)
- Azure 6 teams in prod, 90% accuracy, 38% TTM reduction. [MS Research](https://www.microsoft.com/en-us/research/blog/large-language-models-for-automatic-cloud-incident-management/)
- **Use**: prompting + system-design ideas (no public data).

### 3.7 NoFire AI SRE Benchmark (2025–2026)
- Independent SRE eval. [nofire.ai/ai-sre-benchmark](https://www.nofire.ai/ai-sre-benchmark)
- **Use**: secondary eval.

### 3.8 LiveSWEBench / SWE-bench-Live / LiveCodeBench
- Monthly contamination-free updates. [LiveSWEBench](https://liveswebench.ai/), [SWE-bench-Live](https://swe-bench-live.github.io/), [LiveCodeBench](https://livecodebench.github.io/)
- **Use**: catch overfitting. Add to bench script.

---

## 4. Security / Vulnerability Datasets + Benchmarks

### 4.1 CVE-Bench (Repair, NAACL 2025)
- 509 CVEs, 4 languages, 120 repos. Black-box + white-box modes + static-analysis tool integration. SWE-agent caps at 21%. [aclanthology](https://aclanthology.org/2025.naacl-long.212/)
- **SFT**: synthesize repair trajectories (vuln_patch_repo, gold_diff) → weight **1.0x**.
- **Runtime**: eval.

### 4.2 CVE-Bench (Exploit, ICML 2025)
- 40 critical-severity web vulns, sandboxed exploit eval. SOTA agents: 13%. [arXiv 2503.17332](https://arxiv.org/abs/2503.17332), [GitHub uiuc-kang-lab/cve-bench](https://github.com/uiuc-kang-lab/cve-bench)
- **Use**: red-team eval — Surrogate-1 must NOT generate working exploits casually; this is an **alignment gate**.

### 4.3 SEC-bench (NeurIPS 2025)
- Auto-construct vuln environments + reproductions + gold patches. $0.87 / instance. [arXiv 2506.11791](https://arxiv.org/abs/2506.11791)
- **Use**: scalable security-task generator. Self-host to expand training set.

### 4.4 BountyBench (Stanford, May 2025)
- 25 systems, 40 real bug bounties ($10–$30k), 9 OWASP-Top-10. Detect / Exploit / Patch tasks. Claude Code 87.5% Patch. [SAIL blog](https://ai.stanford.edu/blog/bountybench/), [arXiv 2505.15216](https://arxiv.org/html/2505.15216v3)
- **Use**: defender-side eval (Patch task only — skip Detect/Exploit for safety).

### 4.5 CWE-Bench-Java (ICLR 2025)
- 120 CVEs (now 213 after Aug 2025 update), 38 CWEs, projects with avg 300k LOC. [HF iris-sast/CWE-Bench-Java](https://huggingface.co/datasets/iris-sast/CWE-Bench-Java)
- **License**: MIT.
- **SFT**: weight **0.5x** for Java (low-priority for our AWS focus, still useful for CWE reasoning).
- **Runtime**: companion to IRIS neurosymbolic detector.

### 4.6 SecBench (multi-dim cybersec MCQ + reasoning)
- Knowledge-retention + logical-reasoning, MCQ + short-answer, EN/ZH. [GitHub secbench-git/SecBench](https://github.com/secbench-git/SecBench)
- **SFT**: weight **0.5x**. Augments security knowledge breadth.

### 4.7 Anthropic-Cybersecurity-Skills (754 skills, mapped to MITRE ATT&CK / NIST CSF 2.0 / ATLAS / D3FEND / NIST AI RMF)
- [GitHub mukul975/Anthropic-Cybersecurity-Skills](https://github.com/mukul975/Anthropic-Cybersecurity-Skills) — Apache-2.0.
- **Runtime**: load as RAG knowledge — **don't bake into weights** (frequent updates).

### 4.8 MITRE ATLAS v5.4 + ATT&CK + CIS Benchmarks + NIST 800-53 / SOC 2 / SLSA v1.1
- Public PDFs + JSON. Treat as **RAG**, not SFT — taxonomies change.
- **Runtime**: ChromaDB collection per framework. Surrogate-1 retrieves relevant tactics/controls per task.

### 4.9 NVIDIA AI Blueprint: Vulnerability Analysis
- SBOM + EPSS + VEX + reachability via Plan-and-Execute LLM. [GitHub NVIDIA-AI-Blueprints/vulnerability-analysis](https://github.com/NVIDIA-AI-Blueprints/vulnerability-analysis)
- **Runtime**: pattern lift — adapt the orchestration to our cdk/prowler outputs.

### 4.10 CVE-Genie + EPSS + KEV + CISA feeds
- Daily-refreshing CVE/EPSS/KEV. [Kaggle dataset](https://www.kaggle.com/datasets/francescomanzoni/vulnerability-management-datasets)
- **Runtime**: nightly RAG sync. Don't fine-tune (data drifts daily).

---

## 5. IaC Datasets (CloudFormation / Terraform / CDK / Pulumi)

### 5.1 Multi-IaC-Eval (Amazon Science, Aug 2025) — top SFT pick for IaC
- Triplets `(initial_template, NL_request, updated_template)` across CFN + Terraform + CDK. Validated by CFN-Lint + Checkov. Sonnet 3.5v2: 98.5% / 98.8% pass. [HF AmazonScience/Multi-IaC-Eval](https://huggingface.co/datasets/AmazonScience/Multi-IaC-Eval), [arXiv 2509.05303](https://arxiv.org/pdf/2509.05303)
- **License**: check Amazon Science terms (most permissive for research).
- **SFT**: weight **2.0x** — highest signal for our IaC goal.
- **Runtime**: same metrics for our eval (CFN-Lint + Checkov pass rate).

### 5.2 TerraDS (MSR 2025)
- HCL from 62,406 GitHub repos, permissive licenses only. [TerraDS paper](https://roland-meier.ch/files/2025_MSR_terra-ds-hcl-dataset.pdf)
- **License**: per upstream repos (permissive only).
- **SFT**: weight **1.0x**. Pair with Checkov violation labels for verifier-feedback RL.

### 5.3 TerraFormer (arXiv 2026)
- Neuro-symbolic: LLM + policy-guided verifier feedback. [arXiv 2601.08734](https://arxiv.org/html/2601.08734)
- **Use**: training **recipe** — verifier-in-the-loop RL on tfsec/checkov as reward signal.

### 5.4 IaC-Eval (NeurIPS 2024)
- [GitHub autoiac-project/iac-eval](https://github.com/autoiac-project/iac-eval) — eval framework.
- **Use**: regression eval.

### 5.5 ACSE-Eval (Springer 2025) — threat-model real cloud infra
- [link.springer.com](https://link.springer.com/chapter/10.1007/978-3-032-16165-9_1)
- **Use**: eval — can the model threat-model a CFN stack?

### 5.6 Granite-Code-Instruct (IBM)
- 116 languages, fine-tuned on Git commits + synthetic. 3B/8B/20B/34B. Apache-2.0. [GitHub ibm-granite/granite-code-models](https://github.com/ibm-granite/granite-code-models)
- **SFT**: dataset **not fully open**, but instruction format and **commit-pair** structure is replicable on our infra repos.

---

## 6. Synthetic-Data Generators (use as factories, not endpoints)

### 6.1 Orca-AgentInstruct-1M (Microsoft, 2024)
- 1M-pair subset of 25M synthesized via agentic flows. Mistral-7B → 40% AGIEval / 54% GSM8K gain. [HF microsoft/orca-agentinstruct-1M-v1](https://huggingface.co/datasets/microsoft/orca-agentinstruct-1M-v1)
- **License**: open (check exact CDLA).
- **SFT**: weight **0.5x** — broad reasoning. The framework matters more — adapt to generate **DevSecOps-specific** synthetic data.

### 6.2 APIGen-MT (Apr 2025)
- Two-phase: blueprint + verified action sequence + LLM-reviewer committee. [arXiv 2504.03601](https://arxiv.org/html/2504.03601v4)
- **Runtime**: our internal data factory for AWS-CLI / kubectl / prowler tool-call data.

### 6.3 ToolACE self-evolution pipeline (the API pool grows)
- Reproducible. [openreview](https://openreview.net/forum?id=8EB8k6DdCU)
- **Runtime**: lift the API-pool synthesis loop, seed it with our 50 most-used AWS APIs.

---

## 7. Multi-Step Planning Patterns — runtime, not training

| Pattern | Use when | Cost vs ReAct |
|---|---|---|
| **ReAct** | Default; short interactive loops | 1x |
| **Plan-and-Execute** | Long horizon (>10 steps), incident triage | 0.7x (fewer redundant thoughts) |
| **ReWOO** (Reasoning Without Observation) | Tool calls have predictable inputs | 0.5x — plans whole itinerary upfront |
| **LATS** (tree search) | High-stakes RCA, can afford retries | 3–5x — best quality |
| **Plan-and-Act** (arXiv 2503.09572) | Long-horizon enterprise tasks | 1.5x with replan checkpoints |
| **Reason-Plan-ReAct** (arXiv 2512.03560) | Reasoner+Planner supervising ReAct exec | 2x |

[Wollen Labs blog](https://www.wollenlabs.com/blog-posts/navigating-modern-llm-agent-architectures-multi-agents-plan-and-execute-rewoo-tree-of-thoughts-and-react), [byaiteam](https://byaiteam.com/blog/2025/12/09/ai-agent-planning-react-vs-plan-and-execute-for-reliability/)

**For Surrogate-1**: default = **Plan-and-Execute with replan**. Escalate to **LATS** when first plan fails verification (cfn-guard / prowler / kubectl rollout fails).

---

## 8. Postmortem / RCA Schema (canonical template — bake into system prompt)

```yaml
incident_id: INC-YYYYMMDD-XXX
severity: SEV-1|SEV-2|SEV-3
detection: { source, ts, signal }
timeline:
  - { ts, actor, action, evidence_ref }
blast_radius:
  users_affected: int
  services_affected: [list]
  data_at_risk: enum
  revenue_impact_usd: float
five_whys:
  - { why_1: ..., why_2: ..., why_3: ..., why_4: ..., why_5: ... }
root_cause: { class: code|config|capacity|3rd_party|process, summary }
contributing_factors: [list]
mitigations_applied: [{ action, ts, owner, success: bool }]
action_items:
  - { id, owner, due, kind: prevention|detection|mitigation, jira_ref }
links: { runbook, dashboard, traces, slack_channel }
```

Sources: [Atlassian 5 Whys](https://www.atlassian.com/incident-management/postmortem/5-whys), [Hyperping blameless guide](https://hyperping.com/blog/incident-post-mortem), [OneUptime 2025 templates](https://oneuptime.com/blog/post/2025-09-09-effective-incident-postmortem-templates-ready-to-use-examples/view), [Zalando AI postmortems 2025](https://engineering.zalando.com/posts/2025/09/dead-ends-or-data-goldmines-ai-powered-postmortem-analysis.html). BARO methodology (Best Artifact, WWW'25) for Bayesian validation overlay.

---

## 9. Runtime-Only Tools / Skills (load as MCP / RAG, do NOT fine-tune)

| Tool | Why runtime not SFT | Source |
|---|---|---|
| **Prowler Lighthouse AI + MCP server** (Nov 2025) | Native MCP for our exact use case | [prowler.com](https://prowler.com/blog/prowler-launches-lighthouse-ai-and-mcp-server-bringing-autonomous-security-to-devsecops-teams) |
| **kubectl-ai** (Google) | NL → kubectl ops | [GitHub kubectl-ai](https://github.com/GoogleCloudPlatform/kubectl-ai) |
| **AKS Agentic CLI** (Azure) | Multi-cloud parity | [MS Learn](https://learn.microsoft.com/en-us/azure/aks/cli-agent-for-aks-install) |
| **KubeIntellect** (Sep 2025) | Full K8s API verbs | [arXiv 2509.02449](https://arxiv.org/html/2509.02449v1) |
| **CVE / EPSS / KEV / VEX feeds** | Daily drift | nightly cron → ChromaDB |
| **MITRE ATT&CK + ATLAS + CIS + NIST** | Versioned taxonomy | RAG only |
| **AWS docs + CDK constructs** | We have `mcp__plugin_deploy-on-aws_*` tools | already wired |

---

## 10. Combined SFT Mix Recipe — Surrogate-1 v2.0 next round

**Stage A — Tool-Use SFT** (target ~600k samples after dedup):
| Source | Weight | Est. samples after weight |
|---|---|---|
| ToolACE | 1.5x | 200k |
| Hermes-FC-v1 | 1.0x | 100k |
| xLAM-60k | 1.0x | 60k |
| ToolMind | 0.7x | 100k |
| ActionStudio-98k | 0.8x | 80k |
| Multi-IaC-Eval | 2.0x | 60k |
| ITBench-Trajectories | 2.0x | 30k (synthetic-augmented) |

**Stage B — RL on environments** (after SFT converges):
- R2E-Gym + SWE-Gym (code) — DeepSWE-style
- AIOpsLab + RCAEval + Cloud-OpsBench (SRE) — rule-based reward = correct_diagnosis + no_collateral
- Multi-IaC-Eval verifier reward = CFN-Lint pass + Checkov pass + cfn-guard pass

**Stage C — DPO** on `bench-v1-vs-v15.sh` failure pairs.

---

## Action Items for Surrogate-1

### (a) Top 5 datasets to merge into next training round
1. **ToolACE** (`Team-ACE/ToolACE`) — 1.5x weight — base of all tool-call skill.
2. **Multi-IaC-Eval** (`AmazonScience/Multi-IaC-Eval`) — 2.0x — direct hit on IaC-pass-cfn-guard goal.
3. **ITBench-Trajectories** (`ibm-research/ITBench-Trajectories`) — 2.0x — only public SRE/CISO trajectory corpus.
4. **xLAM-function-calling-60k** (`Salesforce/xlam-function-calling-60k`) — 1.0x — verified, commercial-clean.
5. **SWE-Gym + SWE-smith trajectories** — 1.5x — agentic code-edit loop (filtered green-CI only).

Expected lift on `bench-v1-vs-v15.sh`: BFCL-v3 +18 pp, Multi-IaC pass-rate +25 pp, ITBench-SRE +12 pp.

### (b) Top 3 runtime-only patterns to bake into the daemon
1. **Plan-and-Execute with replan + LATS escalation** — default loop. Triggers LATS when verifier fails twice (cfn-guard / kubectl / prowler).
2. **Tool-graph routing** (ToolMind-style): `incident → metrics(promql) → logs(logql) → traces(traceql) → diff → patch → verify(cfn-guard|kubectl rollout)`. Encode as Pydantic state machine; reject out-of-graph transitions.
3. **RAG over CVE/EPSS/KEV/MITRE/CIS/NIST + Prowler-MCP + kubectl-ai + AWS-MCP**. Daily sync. **Never** fine-tune these — they drift.

### (c) Evals to add to `bench-v1-vs-v15.sh`
- `bfcl_v3` — multi-turn tool-use (BFCL leaderboard repo).
- `multi_iac_eval` — CFN-Lint + Checkov + cfn-guard pass rate on the 3-format set.
- `itbench_lite` — 102 SRE/CISO/FinOps scenarios, K8s sandbox.
- `aiops_lab_rca` — root-cause localization on injected microservice faults.
- `cloud_opsbench` — 452 fault cases on K8s digital twin.
- `o11y_bench` — Pass^3 on 63 Grafana observability tasks.
- `swe_bench_verified` + `swe_bench_multilingual` + `live_swe_bench` — code-edit gates (live = anti-contamination).
- `cve_bench_repair` — only the **Patch** subset (skip exploit eval for safety).
- `bountybench_patch` — defender-side only.

Pass criteria for v1.5 release: ≥ v1.0 on **all** above + improvement on Multi-IaC-Eval, ITBench, BFCL-v3.

---

## See Also
- [[devsecops-sre-platform]] — landscape brief
- [[cloud]] — AWS / GCP / K8s patterns
- [[data-ml-aiops]] — AIOps datasets
- [[../../patterns/MOC]] — patterns hub
- `~/.claude/memory/project_surrogate1_state.md` — pipeline state

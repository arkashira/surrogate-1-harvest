---
title: SOTA SDLC + Agentic Software Engineering — Research Brief for Surrogate-1 v2
date: 2026-04-29
project: surrogate-1
phase: research
audience: ML training pipeline, DevSecOps fine-tune
base_model: Qwen2.5-Coder-7B + LoRA
target: SWE-Bench Lite ≥ 35%, beat Copilot/Cursor on DevSecOps tasks
status: draft-v1
tags: [research, agentic-coding, swe-bench, devsecops, rl, sft, dpo, lora, qwen-coder]
---

# Surrogate-1 v2 — Research: SOTA SDLC + Agentic Software Engineering (2025-2026)

> Goal: turn Qwen2.5-Coder-7B + LoRA into the **best open-weight DevSecOps coding agent** by combining
> the latest agentic-trajectory training data, RL-from-execution-feedback, and domain-specific
> validators (hadolint, tflint, kubeval, semgrep, cfn-guard).

---

## 0. Executive summary (TL;DR for v2 plan)

| Knob | v1 (today) | v2 (planned) | Lift |
|---|---|---|---|
| Base model | Qwen2.5-Coder-7B | Qwen2.5-Coder-7B (kept; LoRA only) | – |
| Training paradigm | SFT only, generic data | **SFT (correct trajectories) → DPO (reject failed) → RLVR (lint/test reward)** | +10–18% pass@1 |
| Trajectories | none (single-shot) | **SWE-Gym 491 + SWE-smith 26K subset + SEAlign tree-pairs** | repo-level skill |
| Context | 8K | **32K** (Qwen2.5-Coder native) — fits typical agent rollout | repo-aware |
| Tool grammar | none | **CodeAct unified action space** (bash + Python + edit + finish) | aligns with OpenHands/SWE-agent |
| DevSecOps reward | none | **hadolint + tflint + actionlint + shellcheck + cfn-guard + semgrep + kubeconform** binary rewards | domain edge |
| Eval set | HumanEval | **SWE-Bench Lite + SWE-Bench-Live + custom Dockerfile-Eval/TF-Eval/K8s-Eval** | honest score |

Realistic v2 SWE-Bench Lite target: **30–35%** Pass@1 (matches Skywork-SWE-7B-class with ~10K trajectories,
beats Qwen2.5-Coder-7B base by ~25 absolute points). On DevSecOps-specific eval (Dockerfile/TF/K8s) we
target **>60% lint-clean rate** + **>50% security-clean rate** — neither Copilot nor Cursor optimize
for these niches.

---

## 1. Frontier SWE Agents 2025-2026 — Landscape

### 1.1 Leaderboard snapshot (April 2026)

| Agent / model | SWE-Bench Verified | SWE-Bench Pro | Notes |
|---|---|---|---|
| Claude Mythos Preview | 93.9% | n/a | proprietary, not reproducible |
| Claude Opus 4.7 (Adaptive) | 87.6% | 64.3% | leader, generally available |
| GPT-5.3 Codex | 85.0% | – | OpenAI |
| Gemini 3.1 Pro | 80.6% | – | Google |
| MiniMax M2.5 | 80.2% | – | **top open-weight** |
| GLM-5 (Zhipu) | 77.8% | – | 744B params |
| Kimi K2.5 | 76.8% | – | open weights |
| DeepSeek-R1 (reasoning) | 76.3% | – | open weights |
| **DeepSWE (Qwen3-32B + RL)** | **59.0%** (test-time scaling), 42.2% Pass@1 | – | fully open recipe |
| **Skywork-SWE-32B** | **47.0%** (TTS), 38.0% Pass@1 | – | OpenHands scaffold |
| **Devstral 2 (Mistral-Small-3.1)** | 46.8% | – | runs on RTX 4090 |
| **SWE-agent-LM-32B (SWE-smith)** | 40.2% | – | fully open data + model |
| **OpenHands LM-32B (SWE-Gym)** | 37.2% | – | open recipe |
| Agentless + Claude-3.5 | 50.8% | – | non-agentic baseline |
| Qwen2.5-Coder-32B (zero-shot) | ~9% | – | what we have to beat with 7B |

> SWE-Bench Verified is **contaminated** (Anthropic/OpenAI stopped reporting). Always cross-check against
> SWE-Bench Pro and SWE-Bench-Live for honest numbers.

### 1.2 Agent architecture taxonomy

#### a. SWE-agent (Princeton, NeurIPS 2024)
- **Innovation**: Agent-Computer Interface (ACI). Built command verbs `find_file`, `goto`, `edit`,
  `submit` instead of raw shell — language models work better with curated tool surface.
- **Repo**: github.com/SWE-agent/SWE-agent. Heavily extended by EnIGMA (cybersecurity, ICML 2025).
- **Take for Surrogate-1**: keep ACI tool grammar minimal & verbose; train on action token vocabulary
  not raw bash. Mirror SWE-agent's `edit START:END` + windowed file view.

#### b. OpenHands / OpenDevin (ICLR 2025, V1 SDK Nov 2025)
- **Innovation**: CodeAct unified action space — **all actions are executable Python/bash blocks**.
  No JSON tool schema; the model writes Python that calls `str_replace_editor`, `execute_bash`,
  `browse`. Single grammar for everything.
- **V1 architecture**: event-sourced state, deterministic replay, immutable agent config, MCP-typed tools,
  workspace abstraction (local Docker / remote VNC / browser). Repo: github.com/OpenHands/OpenHands.
- **CodeAct paper** (ICML 2024, Wang et al., arXiv 2402.01030): up to **+20% success vs JSON tool calls**.
- **Take for Surrogate-1**: train trajectories in CodeAct format. One action grammar = simpler tokenization,
  fewer mode-switches, reuses Python pre-training.

#### c. Devin (Cognition AI, March 2024 → 2026)
- **Closed-source**, but disclosed architecture: long-horizon planner + executor + browser + IDE +
  internal scratchpad ("think out loud"). Devin 2.2 (2026) added Linux desktop (Figma/Photoshop).
- **Performance review (2025)**: 67% PR merge rate (up from 34%), 4× faster, 2× resource efficiency.
- **Take**: multi-agent split (planner / coder / verifier) > monolith.

#### d. Aider (open source, evergreen 2025)
- **Innovation 1**: **repo-map** with tree-sitter ASTs + NetworkX PageRank ranking. Personalize PageRank
  by chat context → only most-relevant identifiers in token budget. (aider.chat/2023/10/22/repomap.html)
- **Innovation 2**: **architect mode** — main model proposes solution in prose; "editor model" turns it
  into editor-diff (or whole-file). Separates reasoning from edit format. Reduced lazy-coding 3X.
- **Innovation 3**: **unified-diff edit format** with line numbers omitted, treated as search-replace.
  GPT-4-Turbo lazy-comment rate dropped from 12/89 → 4/89 tasks.
- **Take for Surrogate-1**: ship a tiny RepoMap implementation + train the model to consume it. Train
  unified-diff edits — most data-efficient edit format. Architect/editor split as inference-time scaffold.

#### e. Cursor Composer / Background Agent (April 2026, Cursor 3)
- **Background Agent**: agent runs in cloud VM, branches off, opens PR. Migration local↔cloud mid-task.
- **Composer training**: Cursor adapted their Background Agent VM scheduler for RL training rollouts at scale.
- **Take**: "agent worktree" pattern — every rollout in fresh container is now standard. Match this with
  Modal sandboxes or E2B for v2 training.

#### f. Cline / Roo Code (VSCode extensions)
- Cline: monolithic system prompt, AST-first repo indexing, MCP support.
- Roo Code (fork): mode system (Code/Architect/Ask/Debug/Custom) — toggle agent role per task.
- **Take**: train a single base, but ship **mode prompts** for v2: code, infra, security-review, review.

#### g. Augment Code Context Engine (Feb 2026 GA)
- Semantic index across **400K+ files**, MCP-exposed (`Context Engine MCP`). Adding it to Cursor/Codex
  reportedly **+30–80% quality**. Take: at *inference* time, give Surrogate-1 a vector store + symbol
  graph; it doesn't need to memorize repos.

#### h. GitHub Copilot Coding Agent (2026)
- Process inside GitHub Actions runner. Reads `AGENTS.md`, branches `copilot/issue-*`, three-layer
  security scan (CodeQL + secret scan + dep review). Issue → PR closed loop.
- **Take**: respect AGENTS.md spec. Train Surrogate-1 to read AGENTS.md FIRST as system prompt.

#### i. Manus AI (Butterfly Effect, March 2025)
- Multi-agent orchestrator (planner / researcher / coder / verifier). Backed by Claude 3.5 + fine-tuned
  Qwen. Acquired by Meta Dec 2025; deal blocked April 2026.
- **Take**: multi-role split is a real production pattern; v2 inference layer should expose it.

#### j. Agentless (Xia et al., July 2024 — STILL RELEVANT)
- **No-agent-loop** approach: 3 phases — localize (file→class→function), repair (multi-sample patch),
  validate (regression tests + reproduction tests). With Claude-3.5: **50.8% on SWE-Bench Verified**.
- **Take**: Agentless is the *strongest non-agent baseline*. Train Surrogate-1 to also do
  Agentless-style hierarchical localize → patch when an agent loop isn't needed. Cheaper at inference.

---

## 2. Training datasets (repository-level)

### 2.1 SWE-Bench / SWE-Bench Verified / Lite (Princeton, 2023+)
- **2,294 issue-PR pairs** from 12 Python repos. Verified = 500 human-validated, Lite = 300 simpler.
- Each instance = repo + base commit + problem statement + golden patch + test_patch + FAIL_TO_PASS + PASS_TO_PASS.
- **Critical caveat**: Verified is contaminated; OpenAI dropped it. Use SWE-Bench Pro + SWE-Bench-Live.

#### Instance schema (verbatim from HF dataset):
```json
{
  "instance_id": "getmoto__moto-7365",
  "repo": "getmoto/moto",
  "problem_statement": "DynamoDB's `update_item` performs floating-point arithmetic ...",
  "base_commit": "7f6c9cb1deafb280fe7fcc7551c38e397f11a706",
  "patch": "diff --git a/moto/dynamodb/models/dynamo_type.py ...",
  "test_patch": "diff --git a/tests/test_dynamodb/...",
  "FAIL_TO_PASS": ["tests/...::test_update_item_add_float"],
  "PASS_TO_PASS": ["tests/...::test_update_different_map_elements"],
  "version": "5.0",
  "created_at": "2024-02-19 20:29:03"
}
```

### 2.2 SWE-Gym (ICML 2025) — Berkeley/CMU
- **2,438 real Python tasks** across 11 repos, runtime envs + unit tests + NL spec.
- Trained 32B Qwen-2.5-Coder with **only 491 trajectories** sampled from GPT-4o + Claude-3.5 → **+12.3%
  on SWE-Bench Lite, +13.6% on Verified** (final 32.0 / 26.0).
- Reproducible recipe (from `docs/OpenHands.md`):
  ```bash
  # Rollout trajectories on training set
  ./scripts/rollout-swe-train-full.sh llm.my-oss-model my_exp 1
  # Evaluate
  ./scripts/eval-swetrain-full-rollout.sh ... output.jsonl 32
  # Train policy
  N_GPUS=8 modal run scripts/training/openhands/train_torchtune_full.py \
    --config configs/policy/1116-sonnet-4o-491i-32k-qwen25_coder_32b_full-lr1e-4.yaml
  ```
- Hyperparameters disclosed: torchtune full-FT, **lr=1e-4**, batch=8, **ctx=32768**, **5 epochs max**.
- HF: `SWE-Gym/SWE-Gym` (2438 instances), `SWE-Gym/Verifier-Trajectories`.
- **Take for v2**: this is the cheapest known recipe to lift open-weight by ~12 points. Use as Stage-1.

### 2.3 SWE-smith (NeurIPS 2025 D&B Spotlight) — Stanford/Princeton/Alibaba
- **52,000 task instances** from **128 repos** + **26,000 agent trajectories** (5K from SWE-agent-LM-32B).
- Pipeline: build env → synthesize tasks that break tests → filter → generate NL issue.
- Trained model `SWE-agent-LM-32B`: **40.2% Pass@1 on Verified** — SOTA for fully-open data.
- Repo: github.com/SWE-bench/SWE-smith. HF: `SWE-bench/SWE-smith`.
- **Take**: scale order-of-magnitude beyond SWE-Gym. Use SWE-smith trajectories as Stage-2.

### 2.4 R2E-Gym (COLM 2025) — UC Berkeley
- **8,100 tasks across 13 repos** via **SWE-GEN** synthesis (no human PRs/issues required) — back-translates
  commit → issue, auto-generates validation tests.
- **Hybrid verifiers**: execution-based (binary test) + execution-free (LM scorer). Combined → **51% on
  SWE-Bench Verified** at 26 rollouts.
- HF: `R2E-Gym/R2E-Gym-Subset`. **Used by DeepSWE** (4,500-task filtered subset for RL training).

### 2.5 Multi-SWE-Bench (NeurIPS 2025) — ByteDance
- **1,632 instances across 7 languages** (Java, TS, JS, Go, Rust, C, C++) + 4,723 RL-instances.
- Curated by 68 expert annotators. **Critical for DevSecOps** since Go/TS/JS dominate infra tooling.
- Repo: github.com/multi-swe-bench/multi-swe-bench.
- **Take for v2**: include Go (kubectl/operators), TS (CDK), Rust (cfn-guard) instances. Gives Surrogate-1
  multilingual edge over Python-only SWE-bench-trained competitors.

### 2.6 SWE-Bench-Live (Microsoft, NeurIPS 2025 D&B)
- **1,319+ tasks since 2024**, **monthly refresh** via `RepoLaunch` automated pipeline. **93 repos**.
- HF: `SWE-bench-Live/SWE-bench-Live`.
- **Take**: only contamination-resistant eval. Always benchmark v2 here.

### 2.7 SWE-rebench (Nebius, NeurIPS 2025)
- **21,000 interactive Python SWE tasks**, RL-grade. Continuous extraction.
- Confirmed: open models score *lower* on rebench vs Verified → confirms contamination on Verified.
- arXiv: 2505.20411.

### 2.8 Skywork-SWE dataset (June 2025)
- **10,169 tasks from 2,531 repos**. **8,209 runtime-validated trajectories**.
- Demonstrated **data scaling law**: doubling data → +3–4% pass@1, no saturation at 8K trajectories.
- Trained Skywork-SWE-32B (38.0% / 47.0% TTS).

### 2.9 RepoBench (ICLR 2024)
- 10,345 Python + 14,956 Java repos for training; 1,075 Python + 594 Java held-out for eval.
- Three subtasks: retrieval, single-line completion, full pipeline. Cross-file context required.
- **Take**: pretrain-light source for cross-file completion skills.

### 2.10 CrossCodeEval (NeurIPS 2023)
- **10K examples / 1K repos / Python·Java·TS·C#**. Static-analysis-filtered to *require* cross-file context.
- Repo: github.com/amazon-science/cceval. **Take**: best held-out generalization signal.

### 2.11 The Stack v2 (BigCode)
- **3B+ files / 600+ languages** organized **by repository** (not file shuffle). HF: `bigcode/the-stack-v2`.
- Variants: `the-stack-v2-train-full-ids`, `the-stack-v2-train-smol-ids` (17 langs).
- **Take**: Surrogate-1's continued-pretrain corpus for repo-aware FIM. Use `smol-ids` for budget runs.

### 2.12 ADP V1 (Agent Data Protocol, Oct 2025) — CMU/OSU
- Unified format: 13 datasets → 1.3M trajectories in single schema.
- `Trajectory = list[Action | Observation]` with action types `{api_action, code_action, message_action}`,
  observation types `{text_observation, web_observation}`.
- ~20% avg gain when training on ADP unified vs raw. arXiv: 2510.24702.
- **Take**: convert all our SWE-Gym + SWE-smith + custom DevSecOps trajectories to ADP. Single SFT script.

---

## 3. Training paradigms (SFT / DPO / RL)

### 3.1 SFT-only (baseline) — SWE-Gym recipe
```yaml
# torchtune full-FT or LoRA config
model: Qwen2.5-Coder-7B-Instruct
dataset: SWE-Gym/Trajectories  # 491-trajectory subset
batch_size: 8
learning_rate: 1e-4
max_seq_len: 32768
epochs: 3-5
optimizer: AdamW
warmup: 0.03
```
**Expected lift**: +10-15 absolute points on SWE-Bench Lite for a 7B model (extrapolating from Skywork's
log-linear law: 491 trajectories ≈ +10pts; 8K trajectories ≈ +25pts).

### 3.2 SFT → DPO (SEAlign, March 2025)
- **Method**: rollout multiple trajectories → build trajectory-tree → MCTS-score nodes → extract
  *partial-trajectory pairs* with significant reward delta → DPO on critical actions only.
- arXiv 2503.18455. Better than vanilla DPO because preference signal is dense (per-decision).
- **Take for v2**: at end of Stage-1 SFT, sample 8 trajectories per SWE-Gym task, label by test
  pass/fail, take all partial-trajectory pairs where one leads to pass and one to fail → DPO loss.

### 3.3 RLVR — Reinforcement Learning from Verifiable Rewards
- **RLEF** (ICML 2025, arXiv 2410.02089): PPO with composite reward
  `R = r(s,a) - β·log[π/ρ]`, where r = +1 (pass), -1 (fail), -0.2 (invalid syntax mid-turn). β=0.05.
  PPO: lr=2e-7, ε=0.2, 4 updates × 256 seqs / cycle, 1024 rollouts / iter. Multi-turn (≤3 attempts).
- **Agent-RLVR** (Scale, June 2025, arXiv 2506.11425): adds **agent guidance** — when reward sparse,
  inject hints/strategic-plans → agent retries → policy update on guided trajectories.
  Lifts Qwen-2.5-72B from **9.4% → 22.4% (with TT reward model: 27.8%)** on SWE-Bench Verified.
- **DeepSWE GRPO++** (Together, July 2025): tweaks over GRPO — Clip-High, no-KL, no-reward-std, length
  norm, RLOO advantage, compact filtering, no entropy loss. **+20 SWE-Bench-Verified pts in 200 RL steps.**

### 3.4 DeepSWE recipe — full fully-open RL
- **Base**: Qwen3-32B (thinking mode on)
- **Data**: R2E-Gym **4,500 tasks** (filtered to avoid Verified contamination)
- **Algorithm**: GRPO++ in `rLLM` framework
- **Reward**: sparse — `+1` if patch passes selected tests within 5 min, `0` otherwise
- **Hardware**: **64 H100s × 6 days**
- **Tools**: bash, file_search, file_edit, finish (4 ACI verbs)
- **Result**: **42.2% Pass@1, 59% Pass@16 with hybrid verifier**
- HF: `agentica-org/DeepSWE-Preview`. Repo: github.com/agentica-project/rllm.
- **Why this matters for v2**: this is the **canonical fully-open recipe**. Surrogate-1's DevSecOps
  edge = same recipe but reward = `pass_test ∨ lint_clean ∨ security_clean`.

### 3.5 ToolTrain (ByteDance, August 2025)
- Two-stage: **rejection-sampled SFT + tool-integrated RL** for *issue localization*.
- 32B model surpasses Claude-3.7 on function-level localization. arXiv 2508.03012.
- **Take**: use ToolTrain's recipe to specialize Surrogate-1's localization head before patch generation.

### 3.6 LocAgent (ACL 2025)
- Builds **directed heterogeneous graph** (files / classes / functions as nodes, calls / imports as edges).
- Sparse indexing → multi-hop search → fine-tuned Qwen-2.5-Coder-Instruct-32B → **92.7% file-level
  localization, +12% Pass@10**.
- Repo: github.com/gersteinlab/LocAgent.
- **Take for v2**: train Surrogate-1 on graph-walk action sequences (`walk(node, hops=2)`).

### 3.7 RLEF reward formulation (verbatim, used as canonical RL spec)
```
R(s_t, a_t) = r(s_t, a_t) - β · log[π(a_t|c_t) / ρ(a_t|c_t)]
where r = +1 if all tests pass at episode end
      r = -1 if tests fail at episode end
      r = -0.2 if generated code is syntactically invalid mid-turn
β   = 0.05  # KL penalty
PPO: lr=2e-7, AdamW, weight_decay=0.1, ε=0.2
     4 updates / collection cycle on 256 seqs each
     1024 rollouts / iteration
     response-based (not token-level) value
     turn limit = 3
```

### 3.8 STOP — Self-Taught Optimizer (Microsoft, 2024)
- Seed "improver" program that calls LLM to improve target program → run improver on itself → recursively.
- GPT-4 generates beam-search / GA / SA improvers. NOT recursive self-improvement of weights, but of
  the scaffold program.
- **Take**: orthogonal at inference; can run STOP as a wrapper around Surrogate-1 to evolve its prompt.

---

## 4. DevSecOps domain — validators as graders

This is **Surrogate-1's moat**. Generic SWE agents don't optimize for these.

### 4.1 Validator inventory + reward signal
| Tool | Domain | Output | Reward function |
|---|---|---|---|
| `hadolint` | Dockerfile | rule violations (DL/SC codes) + severity | `+1` if 0 errors AND 0 high-severity warnings; else `-1` |
| `tflint` | Terraform | provider issues + style | `+1` if exit 0; partial credit per fixed warning |
| `terraform validate` | Terraform | syntax/schema | `+1` if exit 0 |
| `terraform plan` | Terraform | apply preview | `+1` if exits 0 with non-empty plan |
| `checkov` | TF/CFN/K8s/Dockerfile | 1000+ policy checks | `+1` if all critical checks pass |
| `cfn-guard` | CloudFormation | DSL rule eval | `+1` if `cfn-guard validate` passes |
| `cfn-nag` | CloudFormation | 140+ insecure patterns | `+1` if no failing rules |
| `kubeval` / `kubeconform` | K8s manifests | OpenAPI schema | `+1` if exit 0 |
| `kube-linter` | K8s/Helm | best-practice rules | partial credit |
| `actionlint` | GH Actions YAML | workflow correctness + shellcheck inline | `+1` if exit 0 |
| `shellcheck` | Bash scripts | rule violations | `+1` if 0 issues, `-1` if any, `-1` if empty |
| `semgrep` | source code | YAML rules + 20K Pro rules | `+1` if no high-severity findings |
| `bandit` | Python security | 80 plugins | `+1` if no high-severity |
| `prowler` | AWS/Azure/GCP/K8s posture | hundreds of checks | `+1` if pass rate ≥ threshold |
| `trivy` | container/image/IaC | CVE + misconfig | `+1` if no CRITICAL/HIGH |

> **Precedent (PIPer paper, Sept 2025, arXiv 2509.25455)**: shellcheck reward (binary `1`/`0`/`-1`) +
> RLVR with REINFORCE++ on Qwen3-8B → matches Qwen3-32B and GPT-4o on EnvBench-Python.
> Training: 4×H200, batch=64, 15 epochs = 45 steps, lr=5e-6 (shellcheck/judge), 3e-6 (heuristic).
> **This is direct evidence our DevSecOps-RL plan works at 8B-class scale.**

### 4.2 Validator-as-grader sketch (Python)

```python
# /scripts/validators.py — used as RLVR reward
import subprocess, json, tempfile, pathlib

def reward_dockerfile(dockerfile_text: str) -> float:
    with tempfile.NamedTemporaryFile("w", suffix=".dockerfile", delete=False) as f:
        f.write(dockerfile_text); path = f.name
    proc = subprocess.run(
        ["hadolint", "--format", "json", path],
        capture_output=True, text=True
    )
    issues = json.loads(proc.stdout or "[]")
    has_error = any(i["level"] == "error" for i in issues)
    high_warns = sum(1 for i in issues if i["level"] in {"warning"} and i.get("code","").startswith("DL30"))
    if has_error: return -1.0
    if high_warns > 3: return -0.5
    return 1.0 if not issues else 0.5

def reward_terraform(tf_dir: str) -> float:
    init = subprocess.run(["terraform", "-chdir", tf_dir, "init", "-backend=false"], capture_output=True)
    if init.returncode != 0: return -1.0
    val = subprocess.run(["terraform", "-chdir", tf_dir, "validate"], capture_output=True)
    if val.returncode != 0: return -0.5
    lint = subprocess.run(["tflint", "--chdir", tf_dir, "--format", "json"], capture_output=True, text=True)
    issues = json.loads(lint.stdout or "{}").get("issues", [])
    sec = subprocess.run(["checkov", "-d", tf_dir, "--quiet", "-o", "json"], capture_output=True, text=True)
    sec_failed = json.loads(sec.stdout or "{}").get("results", {}).get("failed_checks", [])
    sec_high = sum(1 for c in sec_failed if c.get("severity") in {"HIGH", "CRITICAL"})
    score = 1.0
    score -= 0.05 * len(issues)
    score -= 0.2 * sec_high
    return max(score, -1.0)

def reward_k8s(manifest_text: str) -> float:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(manifest_text); path = f.name
    kc = subprocess.run(["kubeconform", "-strict", "-summary", path], capture_output=True)
    if kc.returncode != 0: return -1.0
    kl = subprocess.run(["kube-linter", "lint", path, "--format", "json"], capture_output=True, text=True)
    findings = json.loads(kl.stdout or "{}").get("Reports", [])
    return 1.0 if not findings else max(0.0, 1.0 - 0.1 * len(findings))

def reward_github_action(workflow_text: str) -> float:
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
        f.write(workflow_text); path = f.name
    proc = subprocess.run(["actionlint", "-format", "{{json .}}", path],
                          capture_output=True, text=True)
    issues = json.loads(proc.stdout or "[]")
    return 1.0 if not issues else max(-1.0, -0.1 * len(issues))

def reward_bash(script: str) -> float:
    proc = subprocess.run(["shellcheck", "-f", "json", "-"],
                          input=script, capture_output=True, text=True)
    issues = json.loads(proc.stdout or "[]")
    if not script.strip(): return -1.0
    return 1.0 if not issues else 0.0

def reward_cloudformation(template_text: str, rules_dir: str = "./guard-rules") -> float:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(template_text); path = f.name
    cg = subprocess.run(["cfn-guard", "validate", "-d", path, "-r", rules_dir],
                        capture_output=True)
    if cg.returncode != 0: return -1.0
    nag = subprocess.run(["cfn_nag_scan", "--input-path", path, "--output-format", "json"],
                         capture_output=True, text=True)
    issues = json.loads(nag.stdout or "[]")
    fail = sum(1 for r in issues for v in r.get("file_results", {}).get("violations", [])
               if v.get("type") == "FAIL")
    return 1.0 if fail == 0 else max(-1.0, -0.2 * fail)
```

### 4.3 Combined DevSecOps reward composition for v2 RL
```python
def composite_reward(domain: str, output: str, env_path: str = None) -> float:
    # weighted: lint 0.3 / security 0.4 / functional 0.3
    if domain == "dockerfile":
        lint_r = reward_dockerfile(output)
        # functional: try `docker build` → success / size <500MB / non-root
        return 0.5 * lint_r + 0.5 * reward_docker_build(output)
    if domain == "terraform":
        return 0.4 * reward_terraform(env_path) + 0.6 * reward_tf_plan_apply(env_path)
    if domain == "k8s":
        return 0.5 * reward_k8s(output) + 0.5 * reward_helm_template(output)
    if domain == "github_actions":
        return reward_github_action(output)
    if domain == "bash":
        return reward_bash(output)
    if domain == "cloudformation":
        return reward_cloudformation(output)
    raise ValueError(f"unknown domain {domain}")
```

---

## 5. Tool orchestration training — agent action grammar

### 5.1 CodeAct unified action space (recommended)
- Single grammar: model emits a Python code block. Executor runs it, returns stdout/stderr.
- Built-in functions exposed to model: `str_replace_editor`, `execute_bash`, `execute_python`,
  `browse(url)`, `finish(message)`.
- Why best: aligns with pre-training (Python is everywhere), zero JSON parsing overhead, control flow
  and variables for free, single-grammar simplifies tokenization.

### 5.2 Trajectory JSONL example (CodeAct format, training-ready)

```jsonl
{"instance_id":"getmoto__moto-7365","messages":[{"role":"system","content":"You are a software engineer. Edit the codebase to make FAIL_TO_PASS tests pass without breaking PASS_TO_PASS tests. Wrap each action in <execute_python> or <execute_bash> tags."},{"role":"user","content":"Repo: getmoto/moto\nProblem: DynamoDB update_item with ADD does float arithmetic, should use Decimal.\nRun the failing test first to confirm:"},{"role":"assistant","content":"I'll start by reading the test and the implementation.\n<execute_bash>\nfind . -path ./node_modules -prune -o -name 'dynamo_type.py' -print\ncat tests/test_dynamodb/test_dynamodb_update_expressions.py | head -120\n</execute_bash>"},{"role":"tool","content":"./moto/dynamodb/models/dynamo_type.py\n# test file: shows test_update_item_add_float expects Decimal('3.5')"},{"role":"assistant","content":"Now read the model file and locate the ADD handler.\n<execute_bash>\ngrep -n 'def add\\|class DynamoType\\|import decimal' moto/dynamodb/models/dynamo_type.py\n</execute_bash>"},{"role":"tool","content":"3:import decimal\n42:    def __add__(self, other):\n45:        return DynamoType(...)"},{"role":"assistant","content":"<execute_python>\nfile_editor.str_replace(\n    path='moto/dynamodb/models/dynamo_type.py',\n    old_str='import decimal',\n    new_str='from decimal import Decimal'\n)\nfile_editor.str_replace(\n    path='moto/dynamodb/models/dynamo_type.py',\n    old_str='        return float(self.value) + float(other.value)',\n    new_str='        return Decimal(self.value) + Decimal(other.value)'\n)\n</execute_python>"},{"role":"tool","content":"Edit applied. 2 replacements."},{"role":"assistant","content":"<execute_bash>\npytest tests/test_dynamodb/test_dynamodb_update_expressions.py::test_update_item_add_float -x\n</execute_bash>"},{"role":"tool","content":"PASSED in 0.8s"},{"role":"assistant","content":"<execute_bash>\npytest tests/test_dynamodb/ -x --timeout 60\n</execute_bash>"},{"role":"tool","content":"412 passed in 89s"},{"role":"assistant","content":"<finish>Done. Replaced float arithmetic with Decimal in DynamoType.__add__.</finish>"}],"reward":1.0,"meta":{"source":"swe-gym","trajectory_len":12,"tokens":4321}}
```

### 5.3 Aider-style unified-diff edit grammar (alternative)
```
TEXT BEFORE
TEXT AFTER

```diff
--- moto/dynamodb/models/dynamo_type.py
+++ moto/dynamodb/models/dynamo_type.py
@@
-import decimal
+from decimal import Decimal
@@
-        return float(self.value) + float(other.value)
+        return Decimal(self.value) + Decimal(other.value)
```
```
- Hunk headers without line numbers (treated as search-replace).
- Train-time augmentation: convert SWE-smith trajectories to BOTH CodeAct AND unified-diff format → 2x data + edit-format-agnostic model.

### 5.4 SWE-agent ACI grammar (legacy / verb-based)
- Verbs: `find_file <name>`, `goto <line>`, `edit <start>:<end> << EOF ... EOF`, `submit`.
- Less tokens per turn but doesn't reuse Python pre-training as well as CodeAct. Skip for v2 unless you
  need a separate small-context inference scaffold.

### 5.5 Action grammar verifier (training-time)
```python
import re

def parse_codeact_action(text: str) -> dict | None:
    for tag in ("execute_python", "execute_bash", "finish"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, flags=re.DOTALL)
        if m: return {"action": tag, "body": m.group(1).strip()}
    return None  # malformed → reward -0.2 in RLEF style

def is_valid_unified_diff(text: str) -> bool:
    return bool(re.search(r"^--- ", text, re.M)) and bool(re.search(r"^\+\+\+ ", text, re.M))
```

---

## 6. Repository-aware training

### 6.1 Aider RepoMap technique (port for Surrogate-1)
1. Parse all source files with tree-sitter → extract symbol defs / refs (Python: classes, functions,
   methods; HCL: resources/modules; Dockerfile: stages; YAML: top-level keys).
2. Build directed graph (file → symbols → callers).
3. Run **personalized PageRank** seeded from the files mentioned in the task / chat / changelog.
4. Trim to token budget (~4K), include only top-N most relevant identifiers as compact summary.

```python
# lite repo map (Aider-style)
import networkx as nx, tree_sitter, json, pathlib

def build_repo_graph(root: pathlib.Path) -> nx.DiGraph:
    g = nx.DiGraph()
    for fp in root.rglob("*.py"):
        symbols = ts_extract_symbols(fp)  # uses tree-sitter
        for sym in symbols:
            g.add_node(f"{fp}::{sym.name}", kind=sym.kind, file=str(fp))
            for ref in sym.refs:
                g.add_edge(f"{fp}::{sym.name}", ref.target)
    return g

def repo_map(g: nx.DiGraph, seeds: list[str], budget_tokens: int = 4000) -> str:
    pers = {n: (1.0 if any(s in n for s in seeds) else 0.0) for n in g.nodes}
    pr = nx.pagerank(g, personalization=pers, alpha=0.85)
    top = sorted(pr, key=pr.get, reverse=True)
    out, t = [], 0
    for node in top:
        line = format_symbol(g.nodes[node])
        if t + token_count(line) > budget_tokens: break
        out.append(line); t += token_count(line)
    return "\n".join(out)
```

### 6.2 LocAgent graph-walk action
Train Surrogate-1 to emit `<walk node='moto/dynamodb' hops=2 filter='function'/>` actions. Localize before patch — empirically yields **+12% Pass@10**.

### 6.3 Cross-file context augmentation
Use **CrossCodeEval** training split + **RepoBench** train split for FIM (fill-in-the-middle) continued
pretraining at ctx=32K. ~2-3% lift on cross-file tasks before SFT.

---

## 7. Knowledge integration sources

### 7.1 Documentation grounding
- AWS docs (CloudFormation, Lambda, IAM) — scrape via the AWS knowledge MCP we already have.
- HashiCorp docs (Terraform), Kubernetes docs, Docker docs, GitHub Actions reference.
- Convert to (question, answer-with-citation) pairs for SFT.

### 7.2 Stack Overflow filtered
- Take **accepted answers**, **score ≥ 5**, **DevOps tags only**: `terraform`, `kubernetes`, `docker`,
  `github-actions`, `aws-cloudformation`, `bash`, `helm`.
- Pair `(question, top_answer)` → SFT.

### 7.3 GitHub commit messages → diff (intent → patch)
- Filter commits with conventional-commit prefixes (`fix:`, `feat:`, `refactor:`).
- Pair `(commit_message, full_diff)` → SFT for diff-understanding (precedent in CodeFuse-CommitEval).

### 7.4 Issue → PR pairs (real-world)
- Mine GitHub for closed issues with linked merged PRs, in DevSecOps-relevant repos
  (terraform, kubernetes, hashicorp/*, kubernetes/*, hadolint/*, semgrep/*, prowler-cloud/*, aquasecurity/*).
- Format as SWE-Bench-style instances with our own `FAIL_TO_PASS`.

### 7.5 AGENTS.md grounding
At inference, Surrogate-1 reads project's `AGENTS.md` first (if exists) — same convention used by Codex,
Cursor, Claude Code, Copilot Coding Agent. Spec stewarded by Linux Foundation's Agentic AI Foundation.

---

## 8. Eval set construction — Surrogate-1 specific

### 8.1 Public benchmarks (must run)
- **SWE-Bench Lite** (300 instances) — fast iteration loop.
- **SWE-Bench Verified** (500) — comparable to literature, accept contamination caveat.
- **SWE-Bench-Live** (1,319+, monthly) — uncontaminated truth.
- **SWE-Bench Pro** (1,865, public + held-out) — long-horizon honest score.
- **Multi-SWE-Bench** (1,632 across 7 langs) — multilingual edge.
- **CrossCodeEval** (10K) — cross-file completion.
- **RepoBench-P** — full pipeline cross-file.

### 8.2 Custom DevSecOps eval (Surrogate-1's moat)

#### Dockerfile-Eval (NEW — 200 prompts × 3 difficulty)
Each prompt: NL spec → produce Dockerfile.
Graders:
1. `hadolint` (lint) — clean ≥ 1 high-severity finding ⇒ fail
2. `docker build` (functional) — must build without error
3. `image size < 500 MB`
4. `runs as non-root` (USER set, EXPOSE non-privileged)
5. `multi-stage` if app has compile step

```python
# eval/dockerfile_eval.py
def grade_dockerfile(dockerfile: str, prompt_meta: dict) -> dict:
    score = {}
    score["hadolint"] = reward_dockerfile(dockerfile) > 0
    img = build_image(dockerfile)  # docker build, returns image id or None
    score["builds"] = img is not None
    if img:
        size = inspect_size(img)
        score["size_ok"] = size < 500 * 1024 * 1024
        score["non_root"] = "USER " in dockerfile and "USER root" not in dockerfile
    return score
```

#### Terraform-Eval (NEW — 200 prompts)
NL spec → produce `.tf` files.
Graders: `terraform init -backend=false` + `validate` + `plan` + `tflint` + `checkov` (HIGH+CRITICAL).

#### K8s-Eval (NEW — 150 prompts)
NL spec → manifest. Graders: `kubeconform -strict` + `kube-linter` + best-practice score
(resources set, probes set, security context non-root, no `:latest`, no host network).

#### CFN-Eval (NEW — 100 prompts)
NL spec → CloudFormation YAML/JSON. Graders: `cfn-lint` + `cfn-guard` (against AWS-managed rules) +
`cfn-nag`.

#### GHA-Eval (NEW — 80 prompts)
NL spec → `.github/workflows/*.yml`. Graders: `actionlint` + `shellcheck` (inline) + secrets-via-OIDC
heuristic + minimal-permissions heuristic.

#### Bash-Eval (NEW — 100 prompts)
NL spec → bash script. Graders: `shellcheck` + functional (run in container, exit code 0 with mocked deps).

#### Eval set construction script
```python
# eval/build_devsecops_eval.py
PROMPTS = [
    {"id":"dock-001","domain":"dockerfile","prompt":"Multi-stage Dockerfile for a Go service that listens on 8080, runs as non-root, alpine final stage."},
    {"id":"dock-002","domain":"dockerfile","prompt":"Python 3.12 FastAPI app with poetry, slim image, healthcheck on /healthz."},
    # ... 200 dockerfile prompts curated from real GH issues, blog posts, docs ...
    {"id":"tf-001","domain":"terraform","prompt":"AWS S3 bucket with versioning, KMS encryption, server-side logging to a separate bucket, public access blocked."},
    {"id":"tf-002","domain":"terraform","prompt":"VPC with 3 public + 3 private subnets across 3 AZs, NAT gateways, IGW."},
    # ... 200 tf prompts ...
]

if __name__ == "__main__":
    for p in PROMPTS:
        out = surrogate1_generate(p["prompt"])
        score = GRADERS[p["domain"]](out, p)
        print(json.dumps({"id": p["id"], **score}))
```

### 8.3 Pass thresholds for v2 ship-ready

| Eval | v1 (current) | v2 target | Stretch |
|---|---|---|---|
| SWE-Bench Lite | ~5% | **30%** | 35% |
| SWE-Bench Verified | ~9% | 28% | 35% |
| SWE-Bench-Live | unknown | 25% | 30% |
| Dockerfile-Eval (all-pass) | 30% | **65%** | 80% |
| Terraform-Eval | unknown | **55%** | 70% |
| K8s-Eval | unknown | 60% | 75% |
| CFN-Eval | unknown | 50% | 65% |
| Bash-Eval | unknown | 70% | 85% |

---

## 9. Concrete v2 implementation — full plan

### 9.1 Stage-0: Continued pretraining (cheap, optional)
- **Data**: bigcode/the-stack-v2-train-smol-ids, filter to `[python, hcl, dockerfile, yaml, bash, go, rust, typescript]`,
  organized **by repo** (preserve cross-file).
- **Method**: FIM at ctx=32K, ~5B tokens, **LoRA r=64 on attn+MLP**, 1 epoch.
- **Cost**: 3-5 days × 1× H100 (since LoRA only). Skip if budget tight.

### 9.2 Stage-1: SFT on agentic trajectories (REQUIRED)
- **Data sources, ADP-unified**:
  - SWE-Gym 491 successful trajectories (canonical)
  - SWE-smith 26K trajectories (filtered to reward > 0)
  - SEAlign trajectory-tree top branches (build from Stage-1.5)
  - **Custom DevSecOps-trajectories** (we synthesize — see 9.3)
- **Format**: ADP V1 → CodeAct system prompt + tool-result observations.
- **Loss**: assistant-tokens-only cross-entropy. Mask system / user / tool.
- **Hyperparameters**:
  - LoRA r=64, α=128, dropout=0.05, target = `q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj`
  - lr=1e-4, AdamW, β2=0.95, weight_decay=0.01
  - batch=8 (grad accum 4 → effective 32)
  - max_seq_len=32768 (Qwen2.5-Coder native)
  - 3 epochs
  - bf16
- **Cost**: ~2 days × 4× H100 (LoRA-only). Or 7-10 days on Lightning H200 (per user's portable_context).

### 9.3 Stage-1.5: Synthesize DevSecOps trajectories (NEW, our IP)
- **Procedure**: for each domain (dockerfile / tf / k8s / cfn / gha / bash):
  1. Take 500 NL prompts from real GH issues + tutorials + AWS docs.
  2. Roll out Claude-Sonnet-4.5 (or Qwen3-Coder-480B via free tier) → produce trajectory.
  3. Run validators → keep only trajectories with reward = 1.0 (lint-clean + security-clean + functional).
  4. Convert to ADP CodeAct format. ~3K trajectories per domain × 6 domains = ~18K trajectories.
- This is the **unique data** that makes Surrogate-1 better than Copilot/Cursor at DevSecOps.

### 9.4 Stage-2: DPO on critical actions (SEAlign style)
- Sample 8 trajectories per task post-Stage-1.
- Score with composite reward (test pass + lint + security).
- Build trajectory tree, extract partial-trajectory pairs where one branch leads to reward=1, other to ≤0.
- DPO training:
  - β=0.1, lr=5e-6, 1 epoch, ~10K pairs
  - LoRA r=32 (smaller), keep frozen base
- **Expected lift**: +3-5 points pass@1.

### 9.5 Stage-3: RLVR (DevSecOps + SWE) (the ambitious stage)
- **Algo**: GRPO++ from DeepSWE / RLEF.
- **Reward**: composite — `0.5 × test_pass + 0.5 × validator_score(domain)`.
- **Environment**: E2B sandboxes OR Modal sandboxes (per user's existing infra) — fresh container per rollout.
- **Tasks**: 4500 R2E-Gym subset + 2000 SWE-Bench-Live recent + 1500 our DevSecOps-trajectories (re-played).
- **Hyperparameters** (RLEF spec, downsized to LoRA on 7B):
  - PPO/GRPO++, lr=2e-7, β=0.05, ε=0.2
  - 4 updates × 256 seqs / cycle, 1024 rollouts / iter
  - max ctx=32K, max turns=15
  - LoRA r=32, train only LoRA + value head
- **Cost**: 5-10 days × 8× H100 OR equivalent on H200/Modal.
- **Expected**: +10-15 pass@1 for SWE-Bench Lite.

### 9.6 Inference scaffold (orthogonal to training, sometimes free wins)
- **Architect/Editor split** (Aider): Surrogate-1 plans in prose → Surrogate-1 with editor-only prompt
  emits unified diff. ~5-10% lift, no training change.
- **Hybrid verifier** (R2E-Gym): execution-based filter + LM-scorer ranker → +7-8 points.
- **Best-of-N** at K=8 or K=16 → +10-20 points (cost = K× inference).
- **Repo-map** with PageRank → injects into system prompt at start of episode.

### 9.7 Stack
- Training: torchtune (SWE-Gym precedent) OR Unsloth (4-bit LoRA, fast on M3-class) OR axolotl.
- RL: rLLM (DeepSWE) or verl (DeepSeek native) — both support GRPO++.
- Sandbox: E2B for budget, Modal for scale. Spin up via current Modal account (user has).
- Tracking: WandB.
- Eval harness: SWE-bench official + our DevSecOps eval scripts.

### 9.8 Minimum viable v2 (cheapest path to ship)
If only 1 stage budget:
1. **Stage-1 SFT only**: SWE-Gym 491 + 5K SWE-smith filtered + 6K our DevSecOps trajectories = 12K total.
2. LoRA r=64, 3 epochs, 32K ctx, lr=1e-4.
3. ~3 days on 4× H100 OR 7 days on Lightning H200.
4. Expected: SWE-Bench Lite ~25%, DevSecOps eval ~55% — already better than any 7B-class open agent.

---

## 10. Key references — papers, repos, datasets

### Papers
- Agentless: arXiv 2407.01489 (Xia et al., 2024)
- SWE-agent: arXiv 2405.15793 (Princeton, NeurIPS 2024)
- OpenHands: arXiv 2407.16741 (ICLR 2025)
- CodeAct: arXiv 2402.01030 (Wang et al., ICML 2024)
- SWE-Gym: arXiv 2412.21139 (ICML 2025)
- SWE-smith: arXiv 2504.21798 (NeurIPS 2025 D&B Spotlight)
- R2E-Gym: arXiv 2504.07164 (COLM 2025)
- Multi-SWE-Bench: arXiv 2504.02605 (NeurIPS 2025)
- SWE-Bench-Live: arXiv 2505.23419 (NeurIPS 2025)
- SWE-rebench: arXiv 2505.20411 (NeurIPS 2025)
- SWE-Bench Pro: arXiv 2509.16941 (Scale AI, 2025)
- RLEF: arXiv 2410.02089 (Meta, ICML 2025)
- Agent-RLVR: arXiv 2506.11425 (Scale, 2025)
- SEAlign: arXiv 2503.18455 (March 2025)
- ADP: arXiv 2510.24702 (Oct 2025)
- LocAgent: arXiv 2503.09089 (ACL 2025)
- ToolTrain: arXiv 2508.03012 (ByteDance, Aug 2025)
- DeepSWE: together.ai/blog/deepswe (July 2025)
- Skywork-SWE: arXiv 2506.19290 (June 2025)
- Devstral: arXiv 2509.25193 (Sept 2025)
- PIPer: arXiv 2509.25455 (Sept 2025)
- CodeRL+: arXiv 2510.18471 (2025)
- STOP: arXiv 2310.02304 (Microsoft, 2023, revised 2024)
- GraphCodeBERT: arXiv 2009.08366 (ICLR 2021, foundational for graph code reps)
- CrossCodeEval: arXiv 2310.11248 (NeurIPS 2023)
- RepoBench: arXiv 2306.03091 (ICLR 2024)

### Repos
- github.com/SWE-bench/SWE-bench
- github.com/SWE-bench/SWE-smith
- github.com/SWE-Gym/SWE-Gym
- github.com/SWE-agent/SWE-agent
- github.com/OpenHands/OpenHands
- github.com/OpenAutoCoder/Agentless
- github.com/R2E-Gym/R2E-Gym
- github.com/multi-swe-bench/multi-swe-bench
- github.com/microsoft/SWE-bench-Live
- github.com/agentica-project/rllm (DeepSWE)
- github.com/gersteinlab/LocAgent
- github.com/microsoft/stop
- github.com/Aider-AI/aider
- github.com/cline/cline
- github.com/RooCodeInc/Roo-Code
- github.com/rhysd/actionlint
- github.com/hadolint/hadolint
- github.com/terraform-linters/tflint
- github.com/yannh/kubeconform
- github.com/stackrox/kube-linter
- github.com/aquasecurity/trivy
- github.com/prowler-cloud/prowler
- github.com/stelligent/cfn_nag
- github.com/aws-cloudformation/cloudformation-guard
- github.com/agentsmd/agents.md

### Datasets (HuggingFace)
- princeton-nlp/SWE-bench, princeton-nlp/SWE-bench_Lite, princeton-nlp/SWE-bench_Verified
- SWE-Gym/SWE-Gym, SWE-Gym/Verifier-Trajectories
- SWE-bench/SWE-smith
- R2E-Gym/R2E-Gym-Subset
- SWE-bench-Live/SWE-bench-Live
- bigcode/the-stack-v2, bigcode/the-stack-v2-train-smol-ids
- Skywork/Skywork-SWE-32B (model)
- agentica-org/DeepSWE-Preview (model)
- mistralai/Devstral-Small-2-24B-Instruct-2512 (model)
- OpenHands/openhands-lm-32b-v0.1 (model)

### Specs / standards
- AGENTS.md spec — agents.md (Linux Foundation Agentic AI Foundation)
- ADP V1 — arXiv 2510.24702 (canonical agent trajectory schema)

---

## 11. Open questions for next iteration
1. Can we ship Surrogate-1-7B with **only Stage-1 SFT** (12K trajectories) and still credibly beat
   Devstral-Small-24B on DevSecOps tasks? (Hypothesis: yes, on niche eval; no, on general SWE-Bench.)
2. Should we keep Qwen2.5-Coder-7B base or upgrade to Qwen3-Coder-Next when GA? (Trade-off: Qwen3 has
   thinking-mode + better RL stability; Qwen2.5 is mature and same family as Skywork-SWE.)
3. Do we ship architect/editor as two LoRA adapters on the same base, or one merged adapter?
4. Can we publish our DevSecOps-trajectory dataset on HF (legal: scraped from open repos + Claude-rolled
   under fair-use for SFT).

---

> End of brief. Hand off to: Stage-1 SFT data builder + Modal trainer.

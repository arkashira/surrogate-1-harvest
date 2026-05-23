---
name: Autonomous 24×7 Agent Architecture (Surrogate-1 Reference)
description: Safety-first patterns for fully-autonomous code/SRE agents — verify-before-act, sandboxed exec, canary+rollback, hard guardrails. No-human-in-loop reference.
tags: [trends, autonomous-agents, sre, safety, guardrails, sandboxing, canary, surrogate-1, 2026]
last_updated: 2026-05-01
related:
  - "[[surrogate-latest-improvements-2026]]"
  - "[[devsecops-sre-platform]]"
  - "[[anti-hallucination-playbook]]"
---

# Autonomous 24×7 Agent Architecture — Surrogate-1 Reference

Goal: Qwen2.5-Coder-7B agent runs unattended on prod cloud infra, ships code, heals incidents, **never makes a wrong destructive action**. If unsure → queue / abstain / rollback. Hard refuse on known-bad patterns.

Design axiom: **trust = verify² × sandbox × idempotency × rollback**. A 7B model alone is not trustworthy; the system around it must be.

---

## 1. Self-Verification Before Action

### 1.1 Reflexion (Shinn 2023, refined 2025)
- **One-line**: agent generates verbal self-reflection on failure → episodic memory → next attempt informed.
- **Paper**: [arxiv 2303.11366](https://arxiv.org/pdf/2303.11366) | extended in [MAR 2025](https://arxiv.org/html/2512.20845v1)
- **Threat**: hallucination, drift, repeating same failed action.
- **Wire-in**: script-level. After every failed action (test fail, plan rejected) Surrogate-1 writes `reflection.md` keyed by task-hash, prepended on retry.
- **Cost**: +1 LLM call per failure (~2k tokens). Latency negligible. Dev: 1 day.

### 1.2 Self-Refine (NeurIPS '23)
- Generate → critique → refine, single model. **Caveat**: 2024 NeurIPS RISE paper: "Self-Refine **degrades** GSM8K/MATH" — naïve self-critique unreliable. ([Can LLMs Critique?](https://evjang.com/2023/03/26/self-reflection.html))
- Use **only** with external verifier signal (test suite, lint, type-check), not pure self-judgment.

### 1.3 CRITIC / Verifier-then-Generate
- LLM verifies via external tools (calculators, search, executors) before committing.
- 2025 evolution: [AutoPyVerifier](https://arxiv.org/html/2604.22937) — compact executable verifiers; [DoVer](https://huggingface.co/papers/2512.06749) — intervention-driven debug for multi-agent; [CGI](https://arxiv.org/html/2503.16024v2) — actor+critic loop.
- **Wire-in**: inference-level. Every code patch → run unit test in sandbox → only if green, mark for next gate.
- **Cost**: 2-5× exec budget for tests. Worth it.

### 1.4 Self-Consistency + Confidence-Informed (CISC, ACL 2025)
- Sample N reasoning paths, pick majority — but [CISC](https://arxiv.org/pdf/2502.06233) prioritizes high-confidence paths, **46% fewer samples** needed.
- [CER (ACL 2025)](https://datasciocean.com/en/paper-intro/cer/): process-confidence (per-token logprobs during reasoning) > final-answer confidence.
- **Wire-in**: Surrogate-1 samples 3 plans, takes weighted-by-process-confidence vote. If top-2 disagree → abstain → queue for human.
- **Cost**: 3× inference per decision. Cheap on Qwen-7B local.

---

## 2. Multi-Agent Debate / Consensus

### 2.1 AutoGen (Microsoft, multi-agent debate pattern)
- [Multi-Agent Debate doc](https://microsoft.github.io/autogen/stable//user-guide/core-user-guide/design-patterns/multi-agent-debate.html) — agents exchange/refine across turns.
- **Threat**: shared blind spots in single-agent reasoning.
- **Wire-in**: 2-agent debate at decision points (proposer + adversary). Adversary's job = find the wrong-action case.
- **Cost**: 2× LLM calls per gated decision. Use only on destructive ops.

### 2.2 ChatDev / MetaGPT
- Role-specialized agents (CEO/CTO/Programmer/Reviewer/Tester). [ChatDev paper](https://arxiv.org/html/2307.07924v5).
- **Caveat 2026**: [Multi-Agent in Production (Apr 2026)](https://medium.com/@Micheal-Lanham/multi-agent-in-production-in-2026-what-actually-survived-f86de8bb1cd1) — single atomic falsehood spreads as system-level false consensus across MetaGPT/CrewAI/AutoGen. Don't blindly trust agent debate; **always external-verify**.
- **Wire-in**: Use 2-role minimum (Patcher + Reviewer), not 7. Smaller surface.

### 2.3 MAR — Multi-Agent Reflexion (2025)
- [arxiv 2512.20845](https://arxiv.org/html/2512.20845v1) — Reflexion + diverse critic personas. More stable than single reflexion.

---

## 3. Sandboxed / Isolated Execution

### 3.1 Comparison Matrix (2026, [Northflank benchmarks](https://northflank.com/blog/top-ai-sandbox-platforms-for-code-execution))

| Tool | Isolation | Cold-start | Cost | Use-case |
|---|---|---|---|---|
| **E2B** | Firecracker microVM | ~150ms | $$ | per-task code exec, kernel isolation |
| **Modal** | gVisor (syscall filter) | ~200ms | $$ | longer jobs, GPU |
| **Daytona** | Docker (shared kernel) | 27-90ms | $ | persistent dev workspace, **cheapest** |
| **Firecracker** (raw) | microVM | ~125ms | self-hosted | full control |
| **gVisor** (raw) | user-space syscall sandbox | low | self-hosted | container-grade |
| **BubbleWrap** | unprivileged user-NS | <50ms | free | local dev, weakest |

- 2026 consensus ([Sub: Container-is-not-sandbox](https://emirb.github.io/blog/microvm-2026/)): standard runc containers cannot safely run untrusted AI-generated code. AWS/Azure/GCP control planes have migrated to microVMs.
- **Recommendation for Surrogate-1**: **Daytona** for cheapest (~$0.0003/min) Docker workspace + **E2B** as escalation tier when patch touches OS-level files. gVisor as middle ground if self-hosting.
- **Wire-in**: every `Bash` / `Edit` action runs inside sandbox. Only after sandbox-test green does the change get applied to real infra (two-phase).

### 3.2 Inspect Sandboxing Toolkit (UK AISI, 2025)
- [aisi.gov.uk Inspect](https://inspect.aisi.org.uk/) — open-source eval framework + sandbox provider used by US CAISI, METR, Apollo.
- **Wire-in**: use Inspect's sandbox abstraction so we can swap E2B↔Daytona↔Modal without rewriting agent.

---

## 4. Canary Deploy + Auto-Rollback

### 4.1 Argo Rollouts
- [argoproj.github.io/rollouts](https://argoproj.github.io/rollouts/) — Kubernetes CRDs for canary, blue-green, progressive delivery. AnalysisTemplate queries metrics → auto-rollback on degradation.
- **Threat**: false-execute, regression slip-through.
- **Wire-in**: every Surrogate-1 patch → ArgoCD PR → Rollouts CRD with 5%→25%→50%→100% steps, each gated by SLO analysis.

### 4.2 Flagger
- [docs.flagger.app](https://linkerd.io/2-edge/tasks/flagger/) — service-mesh native (Linkerd/Istio).
- Stronger for traffic-shifting; weaker for non-mesh workloads.

### 4.3 Kayenta (Netflix/Google)
- [Kayenta](https://cloud.google.com/blog/products/gcp/introducing-kayenta-an-open-automated-canary-analysis-tool-from-google-and-netflix) — statistical canary analysis, Mann-Whitney U on metric streams.
- **Wire-in**: Kayenta judgment as last gate before promote. Score < 95 → abort.
- **Cost**: ~$5/mo per pipeline (Spinnaker dep). Skip if K8s-only → Argo Rollouts is enough.

### 4.4 Statistical Anomaly Gate (2026)
- [openobserve guide](https://openobserve.ai/blog/ai-anomaly-detection-guide/) — replace fixed thresholds with learned baselines.
- **Wire-in**: post-deploy 5min window — error-rate / p99-latency / saturation must stay within 2σ of pre-deploy baseline. Else `kubectl argo rollouts abort`.

---

## 5. Guardrails (Hard Refuse Layer)

### 5.1 NeMo Guardrails (NVIDIA)
- [github.com/NVIDIA-NeMo/Guardrails](https://github.com/NVIDIA-NeMo/Guardrails) — programmable rails (Colang), input/output/tool-call filters.
- [paper 2310.10501](https://arxiv.org/abs/2310.10501).
- **Wire-in**: input rail blocks `rm -rf /`, `DROP TABLE`, `terraform destroy`-on-prod. Output rail validates JSON schema before tool exec.

### 5.2 Guardrails AI (output validation)
- [guardrailsai.com](https://guardrailsai.com/blog/nemoguardrails-integration) — Python validators + Hub of 100+ pre-built (PII, toxicity, regex, JSON-schema).
- **Wire-in**: every plan-output passes through `ToxicLanguage`, `ValidJson`, `ProvenanceLLM` validators. Fail → re-prompt.

### 5.3 LlamaFirewall (Meta, 2025)
- [arxiv 2505.03574](https://arxiv.org/html/2505.03574v1) — 3-layer system: PromptGuard 2 (jailbreak detector), AlignmentChecks (chain-of-thought auditor), CodeShield (static-analysis on generated code).
- **Threat**: prompt-injection, agent misalignment, insecure code.
- **Wire-in**: CodeShield runs on every patch before sandbox-test. Catches SQLi, hardcoded secrets, eval(), shell-injection.

### 5.4 NIST AI RMF + Agentic Profile (2025-2026)
- [NIST AI RMF](https://www.nist.gov/itl/ai-risk-<REDACTED>); March 2025 update added model provenance, third-party assessment. [Cloud Security Alliance Agentic Profile](https://labs.cloudsecurityalliance.org/agentic/agentic-nist-ai-rmf-profile-v1/) — runtime behavioral governance, delegation chain accountability.
- [AAGATE](https://verifywise.ai/blog/state-of-ai-governance-regulations-united-states-2026) translates RMF → K8s-native runtime enforcement.
- **Wire-in**: emit NIST RMF mappings as part of audit-log per action.

### 5.5 AISI Inspect (UK, 2024-2026)
- [inspect.aisi.org.uk](https://inspect.aisi.org.uk/) — eval harness; 200+ pre-built evals; integrates sandboxing.
- **Wire-in**: nightly Surrogate-1 self-eval against held-out failure suite. Regression ≥5% → freeze deploys + alert.

---

## 6. Action Confidence Calibration

### 6.1 Predict-and-Defer / SelectLLM (ICLR 2025)
- [SelectLLM](https://openreview.net/forum?id=JJPAy8mvrQ) — finetuning + selective prediction; calibrated coverage/risk trade-off.
- [Trust or Escalate (ICLR 2025)](https://proceedings.iclr.cc/paper_files/paper/2025/file/08dabd5345b37fffcbe335bd578b15a0-Paper-Conference.pdf) — λ-threshold calibration with human-agreement guarantee.
- **Wire-in**: Surrogate-1 outputs `(action, confidence)`. Confidence < τ_destructive (0.95) → defer → queue. τ_read-only = 0.7.

### 6.2 I-CALM — Incentivized Abstention (2026)
- [arxiv 2604.03904](https://arxiv.org/html/2604.03904v1) — abstention triggers tool-use / retrieval / escalation rather than forced best-guess.
- **Wire-in**: when uncertain, agent's first option is `tool: search_runbook` not `tool: kubectl apply`.

### 6.3 Logprob-to-confidence Mapping
- Token-level logprobs → calibrated probability via temperature scaling on held-out set.
- Cheap; 1× extra forward-pass during eval, free during inference.

### 6.4 Abstention Survey (TACL 2025)
- [Know Your Limits](https://aclanthology.org/2025.tacl-1.26.pdf) — query/model/values triad; standard taxonomy.
- **Hard rule**: 3 of 14 abstention triggers must be checked: (a) ambiguous goal, (b) low intrinsic confidence, (c) human-values conflict (destructive without backup).

---

## 7. Idempotency-by-Design

### 7.1 Two-Phase Architecture (2026 standard)
- Phase A: **analysis** — read-only, produces structured plan. No write access.
- Phase B: **publishing** — applies plan with idempotency key + identity validation. Re-runs are no-ops.
- [Idempotent AI Agents](https://www.buildmvpfast.com/blog/idempotent-ai-agent-retry-safe-patterns-production-workflow-2026) — Temporal/durable-exec pattern.
- **Wire-in**: every action has `idempotency_key = sha256(plan_json)`. State store: SQLite with `INSERT OR IGNORE`.

### 7.2 Dry-Run + Diff
- Terraform `plan`, `kubectl diff`, `aws cloudformation create-change-set` — never apply without diff committed.
- **Wire-in**: hard rule — agent **MUST** post diff to log before any apply. Reviewer agent checks diff hash matches plan hash.

### 7.3 Event-Sourced Changes
- All actions append to log (`actions.jsonl`). Replay = rebuild state.
- Enables time-travel rollback ("Agent Rewind" pattern from [BlackBox AI](https://www.blackbox.ai/blog/from-prompt-to-production-how-ai-agents-ship-code-without-human-intervention)).

---

## 8. Failure-Mode Catalog (Hard-Refuse Rules)

Drawn from [Clyro 5 Failure Modes](https://clyro.dev/blog/the-5-ai-agent-failure-modes-why-they-fail-in-production/), [MAST taxonomy](https://arxiv.org/pdf/2503.13657), [ICLR 2026 Agent Error Taxonomy](https://openreview.net/forum?id=PFR4E8583W), [MITRE ATLAS v5.4](https://atlas.mitre.org/) (16 tactics / 84 techniques as of Feb 2026).

| Failure mode | Frequency (Clyro 591 incidents) | Hard-refuse rule |
|---|---|---|
| Context blindness | 31.6% | Refuse if last_telemetry_age > 5min |
| Rogue actions | 30.3% | Refuse if action ∉ allowlist |
| Silent degradation | 24.9% | Refuse if eval-suite drift > 5% |
| Memory corruption | 8.1% | Refuse if memory-hash mismatch |
| Runaway execution | 5.1% | Refuse if iteration_count > 20 |
| Tool misuse (most common agent-specific) | -- | Refuse if tool-arg schema-validation fails |

### Hard-coded refuse list (NEVER run):
1. `rm -rf /`, `rm -rf $HOME`, `rm -rf *`
2. `DROP DATABASE`, `DROP TABLE` on prod (regex prefix `prd_*`)
3. `terraform destroy` on prod workspace
4. `aws ec2 terminate-instances` without backup-tag verification
5. `kubectl delete ns` on `prod-*` namespaces
6. `git push --force` on `main`/`master`
7. `chmod 777`, `chown -R` on system paths
8. Unsigned `helm install` from non-allowlist registry
9. IAM policy with `"Action": "*", "Resource": "*"`
10. Any DB delete without preceding `pg_dump`/`mysqldump` success in last 1h
11. DNS apex record change on prod zones
12. Disabling MFA / removing sec-group rule from prod SG
13. Deploy that fails LlamaFirewall CodeShield
14. Action where confidence < 0.95 on destructive class

**These are ALWAYS "ask human" — no exception, no override.**

---

## 9. Long-Horizon Planning + Verification

### 9.1 Behavior Trees + LLM (2024-2025)
- [LLM-as-BT-Planner](https://arxiv.org/html/2409.10444v2) — LLMs generate BTs; modular, reactive.
- [Code-BT (IJCAI 2025)](https://www.ijcai.org/proceedings/2025/0980.pdf) — code-driven BT generation.
- **Why over FSM**: BTs handle reactivity (interrupts, retries) without state-explosion.

### 9.2 ReAcTree (Hierarchical Agent Trees, 2025)
- [arxiv 2511.02424](https://arxiv.org/abs/2511.02424) — dynamic tree with LLM agent nodes + control-flow nodes. WAH-NL benchmark: 61% goal success vs ReAct 31% with Qwen 2.5 72B.
- **Wire-in**: Surrogate-1 plans = ReAcTree. Each node = explicit checkpoint with verification. Failed leaf → reflect → retry parent.

### 9.3 EmboTeam — LLM + PDDL + BT Cascade (2026)
- [arxiv 2601.11063](https://arxiv.org/html/2601.11063) — LLM semantic understanding → PDDL formal planner → BT reactive control.
- **Wire-in**: high-stakes ops (DB migrations, region failover) — generate PDDL, validate with planner before exec.

### 9.4 HTN (Hierarchical Task Network)
- [HPlan @ ICAPS 2025](https://icaps25.icaps-conference.org/files/HPlan/HPlanProceedings-2025.pdf) — probabilistic HTN, multi-level abstraction.
- Use for repetitive ops runbooks (decompose: "rotate cert" → 7 sub-tasks, each verified).

---

## 10. Watchdog / Loop Monitor

### 10.1 Observability stack (2026)
- [arize.com observability](https://arize.com/blog/best-ai-observability-tools-for-autonomous-agents-in-2026/), [Braintrust buyers guide](https://www.braintrust.dev/articles/best-ai-observability-tools-2026), [Maxim](https://www.getmaxim.ai/articles/top-5-ai-agent-observability-platforms-in-2026/).
- Track: tool-call latency, success rate, cost/token, plan-vs-actual drift.

### 10.2 Watchdog Agent Pattern
- Separate process tails Surrogate-1's `actions.jsonl`. Independent model (different vendor: Sonnet 4.5).
- Hard kill (`SIGTERM` + freeze) on:
  - 3+ consecutive failed actions
  - Loop-detect (same action 5× in <5min)
  - Audit-log gap > 30s (agent stuck/crashed)
  - Tool-call rate > 100/min (runaway)
- **Wire-in**: `surrogate-watchdog.sh` running under systemd, separate VM, can revoke Surrogate-1's IAM credentials via STS.

### 10.3 Autonomous Observability Agents
- [computer.org Autonomous Observability](https://www.computer.org/publications/tech-news/community-voices/autonomous-observability-ai-agents) — metric agents + learning agents. **Independent** from primary agent.

---

## 11. Computer-Use Safe Modes (Anthropic 2025-2026)

- [Hardening ChatGPT Atlas vs prompt injection](https://openai.com/index/hardening-atlas-against-prompt-injection/) — RL-trained adversary. Pattern applicable to any browser/computer-use agent.
- Claude Opus 4.5 ([Anthropic transparency hub](https://www.anthropic.com/transparency/model-report)): 88.39% refusal rate on harmful computer-use tasks; 1.4% prompt-injection success in Chrome ext (vs 10.8% on Sonnet 4.5).
- **Patterns to copy for Surrogate-1**:
  1. **Domain allowlist** — tool calls only target allowlisted hosts (CSV: prod K8s API, AWS endpoints, GitHub).
  2. **Read-before-write confirmation** — every page-state-changing action validated against last-read state hash. Stale → abort.
  3. **Adversarial RL eval** — automated red-team prompts every 24h. Regress > 1% → freeze.
  4. **Action labels in audit log** — `(read|write|exec|destructive)`, `destructive` always queued.

---

## 12. Constitutional Principles (Hard Guards)

Encoded as Python checks, **NOT** prompted into the LLM. (LLM-internal constitution is unreliable.)

```python
class HardGuards:
    def check(self, action: Action) -> Verdict:
        # Anti-data-loss
        if action.type == 'delete_db' and not has_recent_backup(action.target, hours=1):
            return REFUSE("no backup in last 1h")
        # Anti-runaway
        if self.recent_actions(seconds=300).count(action) >= 5:
            return REFUSE("loop detected")
        # Anti-privilege-escalation
        if action.iam_diff.grants_wildcard():
            return REFUSE("wildcard IAM forbidden")
        # Anti-blind-prod
        if action.target_env == 'prod' and not action.preceded_by('staging_green'):
            return REFUSE("must pass staging first")
        # Anti-stale-context
        if time.time() - action.context_fetched_at > 300:
            return REFUSE("context > 5min stale")
        # Confidence floor
        if action.is_destructive and action.confidence < 0.95:
            return DEFER("queue human review")
        return ALLOW
```

---

## 13. Threat → Mitigation Quick Map

| Threat | Layer | Tool/Pattern |
|---|---|---|
| Hallucinated action | Inference | Self-Consistency + CISC, Reflexion |
| Wrong tool / wrong args | Pre-exec | Guardrails AI schema, LlamaFirewall CodeShield |
| Prompt injection | Input | NeMo Guardrails input rail, PromptGuard 2 |
| Untested code → prod | Exec | Sandbox (Daytona/E2B) + test gate |
| Bad deploy → outage | Post-deploy | Argo Rollouts canary + SLO gate + auto-rollback |
| Loop / runaway | Runtime | Watchdog agent, iteration cap |
| Drift / silent regression | Daily | AISI Inspect eval suite |
| Destructive without backup | Hard-guard | HardGuards.check() |
| Stale context | Hard-guard | context-age check |
| Multi-agent false consensus | Architecture | Keep agents ≤3, external verifier mandatory |

---

## 14. Cost Summary (Surrogate-1 budget per autonomous action)

| Step | Time | Cost |
|---|---|---|
| Plan (Qwen-7B local) | 2-5s | $0 |
| Self-consistency 3× | 6-15s | $0 |
| Guardrails AI validate | 100ms | $0 |
| LlamaFirewall CodeShield | 500ms | $0 |
| Sandbox exec (Daytona) | 5-30s | $0.0003-0.001 |
| Argo Rollouts canary (15min progressive) | 15min | infra-only |
| Watchdog overhead | continuous | ~$5/mo VM |
| **Total per action** | ~30s decision + 15min deploy | **~$0.001 + canary infra** |

Compare: 1 wrong destructive action = potentially $10k+ outage. ROI on safety stack: massive.

---

## Architecture Skeleton for `autonomous-sre.sh` / `autonomous-release.sh`

```bash
#!/usr/bin/env bash
# autonomous-sre.sh — Surrogate-1 24×7 self-healing daemon
# autonomous-release.sh — Surrogate-1 autonomous code-ship pipeline
set -euo pipefail

SURROGATE_HOME="${SURROGATE_HOME:-$HOME/.surrogate}"
ACTIONS_LOG="$SURROGATE_HOME/actions.jsonl"
CONF_THRESHOLD_DESTRUCTIVE="0.95"
CONF_THRESHOLD_READONLY="0.7"
MAX_ITERATIONS_PER_INCIDENT=20
SANDBOX_BACKEND="${SANDBOX_BACKEND:-daytona}"   # daytona|e2b|modal|gvisor

# === LAYER 0: Phase 0 pattern match (do not re-solve known incidents) ===
pattern_match() {
  local incident="$1"
  grep -i "$incident" "$SURROGATE_HOME/knowledge_index.md" | head -3
}

# === LAYER 1: Telemetry fetch + freshness check ===
fetch_context() {
  local now=$(date +%s)
  local last=$(jq -r '.fetched_at' "$SURROGATE_HOME/context.json" 2>/dev/null || echo 0)
  if (( now - last > 300 )); then
    promtool query instant ... > "$SURROGATE_HOME/context.json"
  fi
}

# === LAYER 2: Plan with self-consistency ===
plan() {
  for i in 1 2 3; do
    qwen-run "/dev plan --task=$1" > "$SURROGATE_HOME/plan-$i.json"
  done
  python3 cisc_vote.py plan-1 plan-2 plan-3 > "$SURROGATE_HOME/plan.json"
  # If top-2 disagree → queue
  jq -e '.consensus == true' "$SURROGATE_HOME/plan.json" || queue_human "$1"
}

# === LAYER 3: Hard guards (Python) ===
hard_guards() {
  python3 -m surrogate.guards check "$SURROGATE_HOME/plan.json" || {
    log_refuse "hard-guard-fail"; exit 2
  }
}

# === LAYER 4: Guardrails AI + LlamaFirewall CodeShield ===
validate_output() {
  guardrails validate --rail prod.rail "$SURROGATE_HOME/plan.json"
  llamafirewall codeshield "$SURROGATE_HOME/patch.diff"
}

# === LAYER 5: Sandbox exec — dry-run + tests ===
sandbox_test() {
  case "$SANDBOX_BACKEND" in
    daytona) daytona create --image surrogate-test --exec "make test" ;;
    e2b)     e2b run --template surrogate-test "$SURROGATE_HOME/patch.diff" ;;
    modal)   modal run sandbox.py::test ;;
  esac
}

# === LAYER 6: Two-phase apply ===
apply_change() {
  local idem_key=$(sha256sum "$SURROGATE_HOME/plan.json" | cut -d' ' -f1)
  if grep -q "$idem_key" "$ACTIONS_LOG"; then
    log_skip "idempotent-already-applied"; return 0
  fi
  # Phase A: change-set / dry-run
  terraform plan -out=tfplan
  kubectl diff -f manifest.yaml > diff.txt
  # Phase B: apply if reviewer-agent OK
  if reviewer_agent verify diff.txt; then
    terraform apply tfplan
    kubectl apply -f manifest.yaml
    echo "{\"key\":\"$idem_key\",\"ts\":\"$(date -Iseconds)\",\"action\":\"apply\"}" >> "$ACTIONS_LOG"
  fi
}

# === LAYER 7: Canary + auto-rollback ===
progressive_deploy() {
  argocd app sync myapp --strategy=canary
  # Argo Rollouts handles 5%→25%→50%→100% with AnalysisTemplate
  argo rollouts get rollout myapp --watch --timeout=15m || {
    argo rollouts abort myapp
    argo rollouts undo myapp
    log_refuse "canary-failed-auto-rollback"
    return 1
  }
}

# === LAYER 8: Reflect + log lesson ===
reflect_on_outcome() {
  local outcome="$1"
  qwen-run "/dev reflect --outcome=$outcome --plan=$SURROGATE_HOME/plan.json" \
    >> "$SURROGATE_HOME/reflections.md"
  python3 update_knowledge_index.py
}

# === MAIN LOOP ===
main_loop() {
  local i=0
  while (( i < MAX_ITERATIONS_PER_INCIDENT )); do
    fetch_context
    pattern_match "$INCIDENT" && { apply_known_fix; break; }
    plan "$INCIDENT"
    hard_guards
    validate_output
    sandbox_test || { reflect_on_outcome "sandbox-fail"; ((i++)); continue; }
    apply_change || { reflect_on_outcome "apply-fail"; ((i++)); continue; }
    progressive_deploy && { reflect_on_outcome "success"; break; } \
                       || { reflect_on_outcome "canary-fail"; ((i++)); }
  done
  (( i >= MAX_ITERATIONS_PER_INCIDENT )) && watchdog_freeze "max-iterations"
}

# === WATCHDOG (separate process via systemd) ===
# watchdog observes ACTIONS_LOG; freezes Surrogate-1 IAM if anomalies.

main_loop "$@"
```

**Companion files**:
- `surrogate/guards.py` — HardGuards class (section 12)
- `prod.rail` — Guardrails AI / NeMo rules
- `cisc_vote.py` — confidence-weighted self-consistency
- `surrogate-watchdog.sh` — systemd-managed observer process
- `eval-nightly.sh` — AISI Inspect daily regression suite

---
title: "Surrogate-1 v2 — Autonomous Agent Research (No-Prompting Continuous Loop)"
date: 2026-04-29
tags: [autonomous-agents, surrogate-1, devin, manus, cline, openhands, codeact, planning, memory, training, swe-bench]
status: research-complete
target: Qwen2.5-Coder-7B + LoRA (Surrogate-1 v2)
---

# Autonomous-Agent Research for Surrogate-1 v2

> Goal: teach a 7B model to run continuously like Devin/Manus/Cursor-Composer/Cline-Autopilot — plan, execute, verify, improve, ship, monitor — **without** an outer Claude-Code loop asking permission at every step.

This is the research backbone. Companion files:
- `v2-master-plan-FINAL.md` — overall plan
- `research-self-improve.md` — self-improvement specifics
- `research-multi-agent.md` — orchestration patterns

---

## 0. Executive summary (one screen)

| Pillar | Pattern | Surrogate-1 application |
|---|---|---|
| Action format | **CodeAct** (Python > JSON tool calls) | Train Surrogate-1 to emit Python actions inline |
| Outer loop | **think → plan → act → observe → reflect** | LoRA adapter fluent in this 5-token rhythm |
| Long horizon | **Manus todo.md externalized** | File-based state, not context-window state |
| Memory | **Letta v1 / Cognee GraphRAG** | Core (RAM) + archival (graph) + episodic |
| Self-improvement | **Ralph Wiggum + Reflexion verbal RL** | Per-task lessons appended, no weight updates |
| Training | **DAPO / GRPO RL on SWE-Gym + R2E-Gym** | Following DeepSWE recipe (Qwen3-32B → 59% SWE) |
| Decision rules | **Devin Green/Yellow/Red confidence** | Trained scoring + reversibility gate |
| Infrastructure | **LangGraph + checkpointer** | SQLite checkpointer for resume-on-crash |
| Eval | **SWE-Bench Pro long-horizon** | Target: 25%+ on multi-file (~hours-of-human-work) tasks |
| Dataset | **R2E-Gym 4.5k SWE tasks + AgentTrek 6k web + ADP 1.3M trajectories** | Plus synthetic continuous-improvement rollouts via Claude Opus |

Eval target for v2: **40% autonomous-task-completion** on a custom Surrogate Continuous Bench (define below) — a 7B model is realistically held to ~half of frontier (DeepSWE 59% SWE-Bench Verified, GPT-5 23.3% on SWE-Bench Pro).

---

## 1. Continuous autonomous agents — 2025-2026 SOTA landscape

### 1.1 Devin (Cognition AI) — full architecture

Devin is the **closed-source reference design** for "AI software engineer." 2025 published architectural points:

**Sandbox stack**
- Shell (long-lived bash session, not single-shot)
- Editor (syntax-aware diff, not blind overwrite)
- Browser (Playwright-driven for docs, Stack Overflow, internal wikis)
- Persistent workspace (mounted volumes, survives session restart)

**Planning approach**
- 2025 added **Interactive Planning**: human reviews high-level roadmap *before* execution begins. This is the single biggest reliability win — pre-flight alignment beats mid-flight correction.
- Decisions tagged **Green / Yellow / Red** by self-estimated success likelihood. Green = ship, Yellow = ask, Red = abort or escalate.

**Codebase understanding**
- "DeepWiki" — pre-indexed codebase knowledge graph (constructed offline, queried during sessions). Handles 5M-line COBOL repos and 500GB monorepos. **Key insight**: indexing is decoupled from acting. Same trick GitHub Copilot uses with semantic graph.

**Multi-agent**
- April 2025 release: each Devin instance in its own VM. User can spin up "fleet" — dozens of parallel Devins on independent tasks. No shared mutable state; coordination via human review.

**Real-world data point** (Cognition annual review 2025): Devin produces 25% of Cognition's own code. 4x faster than humans on tasks with clear specs; 2x more efficient resource use. **Fails on**: ambiguous requirements, mid-task pivot, non-verifiable outcomes.

### 1.2 Manus AI — externalized memory + CodeAct

Manus is the **most-detailed publicly-reverse-engineered architecture** (Renschni gist + arXiv 2505.02024).

**Agent loop**
```
analyze → plan → execute → observe → repeat
```
One tool action per iteration. Each cycle's observation appended to event stream.

**Planner module**
- Decomposes high-level objective into ordered steps
- Plans injected as special **"Plan" events** in context
- Keeps agent goal-aligned — prevents drift

**`todo.md` mechanism** ⭐ (most important for Surrogate-1)
- Live markdown checklist persisted to virtual file system
- Agent **ticks off** completed steps after each iteration
- Survives context-window truncation — agent re-reads it on every loop

**Virtual file system** (externalized memory)
- Long documents drafted incrementally to disk, not held in context
- Research findings dumped as files, then file-summary loaded on demand
- Effectively bypasses 200k-token context limits

**CodeAct paradigm** (executable Python > tool JSON)
- Instead of `SEARCH(query="x")`, emits `results = web.search("x"); print(results[:3])`
- Single action can chain: search → filter → save → continue
- Sandbox executes Python directly; LLM just writes code

**Parallel sub-agents**
- Specialized: web research / coding / data analysis / writer
- Each in isolated sandbox; orchestrator coordinates
- Prompt structure modularized: `<tool_use_rules>`, `<shell_rules>`, `<browser_rules>`, etc.

**Knowledge retrieval**
- RAG via "Knowledge events" injected into context (read-only)
- Claims must be verified by clicking through (not trusting search snippets)

### 1.3 Cline + Cursor 2.0 + Augment — IDE-side autopilot

| Tool | Autopilot mode | Multi-agent | Long-running |
|---|---|---|---|
| **Cline** | `-y` flag = no UI prompts; `Shift+Tab` toggles auto-approve | Single agent | Hours, IDE-bound |
| **Cline CLI 2.0** (Feb 2025) | Full stdin/stdout pipeline; treat as Unix tool | Single | Compose with bash |
| **Cursor 2.0 / Composer** | "Cloud Agents" 99.9% reliability, instant startup | Up to 8 parallel agents per prompt via git worktrees | **Weeks** on 1M-line projects |
| **Augment Auggie** | Cloud Remote Agents in virtualized OS | Single, deep | Hours |

**Cursor's planner-worker pattern** (from cursor.com/blog/scaling-agents):
- **Planners** explore codebase, generate tasks, can spawn sub-planners (recursive)
- **Workers** "grind on assigned task until done, push changes" — no inter-worker coordination
- **Judge** evaluates progress at cycle end, decides whether to continue
- Eliminated "integrator role" — workers handle conflicts independently
- Tried flat-peer + locking → bottleneck (20 agents acted like 2-3)
- Tried optimistic concurrency → workers became risk-averse without hierarchy
- Hierarchy + worker-isolation = stable scale to 100s of agents on 1M-line codebases

**Lesson**: For Surrogate-1, **don't try peer coordination**. Use planner-worker hierarchy from day 1.

### 1.4 OpenHands (formerly OpenDevin) — open SDK

ICLR 2025 paper. Most-detailed open implementation.

**V0 → V1 evolution**
- V0: monolithic, sandbox-centric
- V1: modular SDK — clear boundaries, opt-in sandboxing, reusable agent/tool/workspace packages
- **Event-sourced state model with deterministic replay** (replay an agent run from event log = perfect reproduction)
- **Immutable agent config** + typed tool system + MCP integration

**Agent step function** (canonical OpenHands loop):
```python
def step(state: State) -> Action:
    # state = history of (action, observation, cost, metadata)
    # returns next action: shell cmd, Python exec, browser nav, sub-agent call
    return agent.policy(state)
```

**AgentSkills library** — utilities not in plain bash/python:
- `edit_file`, scrolling functions for partial-file viewing
- `parse_image`, `parse_pdf` (multimodal docs)
- File search, regex utilities

**Multi-agent**: hierarchical. Built-in delegation primitives + standardized vocabulary for "roles."

OpenHands SWE-bench: ~53% with Claude Sonnet via CodeAct.

### 1.5 Aider — git-native pair programming

**Architect mode** — two-model split:
- "Architect" model proposes high-level plan
- "Editor" model converts plan to file edits
- `--auto-accept-architect=true` (default) → no confirmation needed

**Git is the persistence layer**:
- Every AI edit = automatic commit with descriptive message
- Sessions ≈ branches; revertible per-commit
- `git log` is the agent trace

For Surrogate-1: borrow this. **Every successful action = git commit. Failed action = uncommitted; revert.**

### 1.6 SmolAgents (HuggingFace) — code-first minimalism

- ~1,000 LOC total agent logic
- Code Agents: write Python actions, not JSON tool calls (CodeAct alignment)
- Sandboxes: E2B / Modal / Docker / Pyodide+Deno WASM
- Model-agnostic — works with local Qwen via Ollama or LiteLLM
- Hub integration — share/pull tools and agents

**Why interesting for Surrogate-1**: smolest production-grade agent loop. If we can't reproduce ~50 lines of agent loop, we shouldn't be doing this.

### 1.7 Comparison table — "no-prompt" capability

| Agent | Self-direct planning | Long sessions | Memory persistence | Auto-deploy | Open source |
|---|---|---|---|---|---|
| Devin | yes (Interactive Planning, then Green-Yellow-Red autonomy) | days | DeepWiki + workspace | yes | no |
| Manus | yes (Planner module + todo.md) | hours-days | Virtual FS | partial | partial reverse-engineered |
| Cursor Composer | yes (planner-worker) | **weeks** | git worktrees + RAG | partial | no |
| Cline | partial (autopilot via flag) | hours | session memory only | manual | yes |
| OpenHands | yes (event-sourced) | hours | event log + file system | partial | yes |
| Aider | partial (architect mode) | hours | git history | manual | yes |
| SmolAgents | yes (any) | depends on host | none built-in | depends | yes |
| **Surrogate-1 v2 target** | yes (todo.md + planner-worker) | days (resume-on-crash) | Letta + GraphRAG | canary auto-rollback | yes |

---

## 2. Goal-directed long-horizon planning

### 2.1 PDDL (Planning Domain Definition Language)

Classical AI planning. Express world as:
- **Predicates**: `(at robot kitchen)`, `(holding agent key)`
- **Actions**: preconditions + effects
- **Goal**: predicate to make true

**LLMs as planning formalizers** (ACL 2025 survey, aclanthology.org/2025.findings-acl.1291): LLM **doesn't plan directly**. It translates natural language → PDDL, hands to classical planner (Fast-Downward, FF), classical planner returns optimal sequence, LLM grounds steps back into actions.

**LaMMA-P** (ICRA 2025): combined LLM reasoning + heuristic search planner. Multi-agent long-horizon. SOTA on long-horizon allocation tasks.

**Why this matters for Surrogate-1**: 7B model planning 50-step task is unreliable. Use 7B to **emit PDDL + use classical solver** for the spine; let 7B handle individual action grounding.

### 2.2 HTN (Hierarchical Task Networks)

- High-level "task" decomposed into subtasks
- Subtasks decomposed further until "primitive actions" (executable)
- Planner picks decomposition that satisfies preconditions + ordering

**Recent**: "Towards a General Framework for HTN Modeling with LLMs" (ICAPS HPlan 2025). LLM proposes decompositions; HTN solver verifies feasibility.

**HiTAMP** (ICRA 2025 workshop): hierarchical LLM-modulo planner. Re-plans on motion verification violations.

For software engineering: HTN naturally maps to "feature → implement modules → write tests → integrate." Each level a different abstraction.

### 2.3 Tree of Goals + LATS

**LATS** (Language Agent Tree Search, ICML 2024 — still SOTA pattern in 2025-2026):
- Nodes = states; edges = candidate actions
- MCTS-style: select → expand → simulate → backpropagate
- Value function = LM self-evaluation
- Self-reflection on rollout failure
- **HumanEval pass@1 = 92.7% with GPT-4** (gradient-free)
- Subsumes Reflexion + Tree-of-Thoughts + Plan-and-Execute

**Implementation sketch**:
```python
def lats_plan(goal, max_iters=50):
    root = Node(state=initial_state, goal=goal)
    for _ in range(max_iters):
        leaf = select(root)              # UCB1 over (visits, value)
        children = expand(leaf, llm)     # LM proposes 3-5 actions
        for child in children:
            obs = simulate(child)         # rollout + LM-judge
            value = evaluate(child, obs, llm)
            backpropagate(child, value)
        if root.best_child().value > 0.95:
            return root.best_path()
    return root.best_path()  # best-effort
```

For Surrogate-1: **probably overkill** for v2. Plan-and-execute simpler. Save LATS for v3 when 7B can self-evaluate reliably.

### 2.4 Plan-and-Execute (LangGraph canonical pattern)

```python
# state graph
graph = StateGraph(PlanState)
graph.add_node("planner", make_plan)      # LLM produces step list
graph.add_node("executor", execute_step)  # LLM executes one step + tools
graph.add_node("replanner", revise_plan)  # if step failed
graph.add_edge("planner", "executor")
graph.add_conditional_edges(
    "executor",
    lambda s: "done" if s.done else "replanner" if s.failed else "executor",
)
graph.add_edge("replanner", "executor")
```

**This is what Surrogate-1 v2 should fundamentally be.** Three nodes. Survives crashes via LangGraph's `SqliteSaver` checkpointer.

### 2.5 Decompose-then-execute vs Interleave

| Approach | Pros | Cons | When to use |
|---|---|---|---|
| Decompose first (Manus-style) | Predictable, reviewable | Brittle if env changes | Spec-clear tasks |
| Interleave (ReAct-style) | Adaptive | Drift risk | Discovery tasks |
| Hybrid (LATS / plan-replan) | Both | Compute heavy | Long, fuzzy goals |

For shipping software: **decompose first**, replan on failure. This matches Devin's Interactive Planning lesson.

---

## 3. Self-directed work queue (internal "TodoWrite")

### 3.1 Manus todo.md — file-based work queue

```markdown
# Goal: Implement OAuth2 login flow

## Tasks
- [x] Read existing auth code in src/auth/
- [x] Add OAuth2 dependency to package.json
- [ ] Write OAuth2 callback handler in src/auth/oauth.ts (in_progress)
- [ ] Add session storage in src/auth/session.ts
- [ ] Write unit tests in tests/auth/oauth.test.ts
- [ ] Write integration tests
- [ ] Update README.md with new login flow

## Notes
- Library choice: passport-oauth2 (battle-tested, 8M+ downloads)
- Session store: redis (already in stack)
- Failure: previous attempt with auth0-js had CORS issues
```

**Why this works**:
- Survives context truncation (re-read on every loop)
- Single source of truth for state
- Diff-able via git
- Human can edit between iterations

### 3.2 prd.json — machine-readable variant (Ralph Wiggum)

```json
{
  "tasks": [
    {
      "id": "T-001",
      "story": "User can log in with Google OAuth",
      "acceptance": [
        "POST /auth/oauth-callback returns 200 on valid code",
        "Session cookie set with httpOnly + secure",
        "GET /me returns user identity after login"
      ],
      "depends_on": [],
      "status": "in_progress",
      "passes": false,
      "attempts": 0,
      "max_attempts": 3
    },
    {
      "id": "T-002",
      "story": "User session persists across requests",
      "acceptance": ["Session valid for 7 days", "Refresh token rotates"],
      "depends_on": ["T-001"],
      "status": "pending",
      "passes": false,
      "attempts": 0,
      "max_attempts": 3
    }
  ],
  "max_iterations": 100,
  "max_idle_iterations": 5
}
```

### 3.3 Priority queue + dynamic re-ranking

```python
def select_next_task(prd, agent_memory):
    candidates = [t for t in prd.tasks
                  if t.status == "pending"
                  and all(prd[d].passes for d in t.depends_on)
                  and t.attempts < t.max_attempts]
    if not candidates:
        return None
    # Score: prefer high-impact + low-risk + recently-blocked
    def score(t):
        return (
            t.business_value          # 0..10 from PRD
            - t.estimated_complexity  # 0..10
            + (5 if t.attempts > 0 else 0)  # retry recently-touched
        )
    return max(candidates, key=score)
```

### 3.4 Dependency graph + DAG topological sort

```python
import networkx as nx

def build_dag(prd):
    g = nx.DiGraph()
    for t in prd.tasks:
        g.add_node(t.id)
        for dep in t.depends_on:
            g.add_edge(dep, t.id)
    if not nx.is_directed_acyclic_graph(g):
        raise ValueError("Cycle in PRD!")
    return list(nx.topological_sort(g))
```

### 3.5 Skip / defer / abort decision logic

```python
def decide_task_disposition(task, env):
    if task.attempts >= task.max_attempts:
        # Tried too many times — escalate
        return "ABORT_AND_ASK_HUMAN"
    if task.blocked_by_external:  # API outage, missing dep, etc.
        return "DEFER"
    if env.budget_exhausted:
        return "DEFER_NEXT_RUN"
    if task.requires_secret and not env.has_secret(task.required_secret):
        return "ABORT_AND_ASK_HUMAN"
    return "EXECUTE"
```

### 3.6 When to ask human vs decide self (Devin Green/Yellow/Red)

| Signal | Color | Action |
|---|---|---|
| Confidence > 0.9, reversible | Green | Act |
| Confidence 0.6-0.9, reversible | Green-yellow | Act + log |
| Confidence < 0.6, reversible | Yellow | Act with shadow review |
| Any confidence, irreversible | Red | Always ask |
| Cost > $X | Red | Always ask |
| 2 prior failures on same task | Red | Always ask |

**Reversibility checklist**:
- Reads any file → fully reversible
- Edits files in feature branch → revertible via git
- Edits files on main → semi-reversible (revert PR)
- `rm -rf` / `DROP TABLE` / `terraform destroy` → irreversible — always ask
- Sends email / makes payment / posts public → irreversible — always ask

---

## 4. Self-improvement of project code

### 4.1 Static-analysis loop (the cheapest signal)

```python
def static_quality_loop(repo, max_passes=10):
    for _ in range(max_passes):
        issues = []
        issues += run_linter(repo, "ruff", "--fix")    # Python
        issues += run_typecheck(repo, "mypy")
        issues += run_security(repo, "bandit")
        issues += run_complexity(repo, "lizard", threshold=15)
        if not issues:
            return "clean"
        for issue in prioritize(issues):
            propose_fix = agent.fix(issue, repo)
            if validate(propose_fix, repo):
                git_commit(propose_fix)
            else:
                log_failure(issue)
    return "max_passes_reached"
```

**Tools per language**:
- Python: ruff, mypy, bandit, lizard, vulture (dead-code), refurb, pyupgrade
- TypeScript: ESLint, tsc --strict, depcheck, knip, ts-prune
- Go: golangci-lint, gosec, ineffassign, staticcheck
- Rust: clippy, cargo-audit, cargo-deny

### 4.2 Test-coverage discovery + writing missing tests

```python
def coverage_loop(repo):
    cov = run_coverage(repo)  # pytest --cov, c8, go test -cover
    uncovered = cov.missing_lines()
    for func in find_uncovered_functions(uncovered):
        # generate test
        test_code = agent.generate_test(
            target_function=func,
            existing_tests=read_test_dir(repo, of=func),
            style_guide=AGENTS.md
        )
        if test_runs_and_passes(test_code) and improves_coverage(test_code):
            git_commit(test_code)
        else:
            mark_as_attempted(func)
```

**Reference 2025 work**: **Self-Refining Programming Agents (SPA)** — pytest + coverage.py + Radon + Pylint as feedback loop. Tools like **Codium PR-Agent** auto-generate test suites for PRs.

### 4.3 Performance profiling + optimization

```python
def perf_loop(repo, hot_path_threshold_ms=100):
    profile = run_profiler(repo)  # py-spy / pprof / cargo-flamegraph
    hot_funcs = profile.functions_above(hot_path_threshold_ms)
    for func in hot_funcs:
        candidates = agent.propose_optimizations(func, profile)
        # candidates = list of {patch, expected_speedup, risk}
        for c in sorted(candidates, key=lambda x: -x.expected_speedup):
            with git_branch_isolation():
                apply(c.patch)
                if all_tests_still_pass() and benchmark_improved(c, threshold=0.1):
                    git_commit(c.patch)
                    break
```

**Risk gate**: never optimize without a microbenchmark. Otherwise model hallucinates speedups.

### 4.4 Refactoring suggestions

Trigger when:
- Function complexity (cyclomatic) > 15
- File > 500 LOC
- Duplicate code blocks (jscpd, simian)
- Function with > 5 parameters
- Class with > 20 methods

**SonarQube MCP** (2025 tool) feeds these signals to AI; AI proposes refactor; gated by test pass.

### 4.5 Dependency upgrade with safety checks

```python
def deps_upgrade_loop(repo):
    outdated = run_outdated_check(repo)  # npm outdated / pip list --outdated
    for dep in sorted(outdated, key=lambda x: x.security_severity, reverse=True):
        with git_branch(f"deps/{dep.name}"):
            if dep.is_major_bump:
                changelog = fetch_changelog(dep)
                breaking = agent.find_breaking_changes(changelog)
                if breaking:
                    agent.apply_migration(breaking, repo)
            update_dep(dep)
            if run_full_test_suite(repo).all_pass():
                open_pr(branch=f"deps/{dep.name}")
            else:
                git_reset()  # don't merge broken deps
```

### 4.6 Documentation generation

- Function docstrings: target uncovered functions, generate from signature + body
- README sections: when CLI flags / API endpoints change → regen relevant section
- Architecture diagrams: parse imports → mermaid graph → embed in docs
- Changelog: from `git log` since last tag → group by conventional-commits → CHANGELOG.md

### 4.7 Auto-edit loop performance gain (key paper)

"Self-Programming AI: Code-Learning Agents for Autonomous Refactoring" (2025): LLM coding agents equipped with basic coding tools that **edit themselves** achieved **17% to 53% gains** on benchmark tasks across iterations. The autoreg pattern: agent reviews own past actions, identifies inefficiencies, modifies own prompts/tools/heuristics.

**Surrogate-1 application**: store every successful trajectory; periodically run a "reflect" pass that produces meta-rules ("when faced with X, prefer pattern Y"). Append to AGENTS.md. Next sessions inherit learnings.

---

## 5. Feature discovery / enrichment (proactive idea generation)

### 5.1 Competitive analysis

```python
def competitor_scan(project):
    competitors = config.competitors  # list of URLs
    for c in competitors:
        latest = fetch_changelog(c)
        for feature in extract_features(latest):
            if feature_not_in_project(feature, project):
                idea = {
                    "title": f"Adopt {feature.name} (seen in {c})",
                    "rationale": feature.user_value,
                    "effort_estimate": estimate(feature, project),
                    "source": c,
                    "confidence": "yellow"
                }
                propose_to_backlog(idea)
```

### 5.2 User feedback mining

Sources: app store reviews, support tickets, github issues, twitter mentions, discord.

```python
def feedback_mine(project):
    raw = collect([app_store, github_issues, support_tix])
    clusters = embed_then_cluster(raw, n_clusters=20)
    for cluster in clusters:
        if cluster.size > threshold:
            theme = agent.summarize(cluster.docs)
            if theme.is_actionable():
                propose_to_backlog({
                    "title": theme.title,
                    "user_voices": cluster.docs[:5],
                    "estimated_users_affected": cluster.size,
                })
```

### 5.3 Telemetry-driven (drop-off → fix)

```python
def funnel_optimize(project):
    funnel = fetch_analytics(["amplitude", "posthog", "datadog_rum"])
    biggest_drop = max(funnel.steps, key=lambda s: s.drop_pct)
    hypotheses = agent.diagnose(
        step=biggest_drop,
        recent_changes=git.log_around(biggest_drop.timestamp),
    )
    for h in hypotheses:
        propose_to_backlog({
            "title": f"Hypothesis: {h.summary}",
            "validation_plan": h.test_plan,
        })
```

### 5.4 Trend monitoring (HN, ProductHunt, X)

Polls cron-driven hourly. LLM filters for relevance to project domain. Propose-PRs only if **clear user value** detected — guards against trend-chasing.

### 5.5 Auto-PRD generation

Devin Interactive Planning shows: ambiguity is biggest failure. Auto-PRD writes:
- User story: "As an [X], I want [Y], so that [Z]"
- Acceptance criteria (verifiable)
- Out-of-scope (explicit non-goals)
- Test plan (manual + automated)
- Rollback plan

Then human approves before any code touched.

---

## 6. Continuous deploy + monitor loop

### 6.1 Auto-deploy on green test

```python
@on_event("test.passed")
def auto_deploy(event):
    if event.branch != "main":
        return
    if event.coverage < 0.80:
        return alert("coverage below threshold")
    deploy_canary(event.commit, traffic_pct=5)
    schedule_check(event.commit, "+10min")
```

### 6.2 Canary + auto-rollback

Pattern from Argo Rollouts / Flagger / Vercel self-driving deployments:

```python
def canary_health_check(commit, baseline_metrics):
    canary = fetch_metrics(commit, window="last 10min")
    regressions = []
    for k in ["p50_latency_ms", "p99_latency_ms", "error_rate", "5xx_rate"]:
        if canary[k] > baseline_metrics[k] * 1.1:  # 10% threshold
            regressions.append(k)
    if regressions:
        rollback(commit)
        alert(f"Canary failed on {regressions}")
        agent.investigate(commit, regressions)
    else:
        promote_canary(commit, traffic_pct=100)
```

### 6.3 Self-monitor logs/metrics for anomalies

```python
@cron("* * * * *")  # every minute
def anomaly_watch():
    metrics = fetch_recent_metrics(window="5min")
    anomalies = anomaly_detector.scan(metrics)
    for a in anomalies:
        if a.severity == "critical":
            spawn_incident_agent(a)
        elif a.severity == "warning":
            log_for_review(a)
```

Datadog Watchdog AIOps does this commercially. Open-source equivalents: PromQL alerts + custom anomaly detector trained on historical metrics.

### 6.4 Auto-incident-response loop

```python
def incident_agent(anomaly):
    state = "diagnose"
    while True:
        if state == "diagnose":
            cause = agent.find_root_cause(anomaly, recent_changes, logs)
            state = "remediate"
        elif state == "remediate":
            fix = agent.propose_fix(cause)
            if fix.is_revert:
                git_revert(fix.commit_sha)
            elif fix.is_config_change:
                apply_with_canary(fix)
            elif fix.is_code_change:
                open_pr(fix)
                state = "wait_human_review"
            else:
                state = "escalate"
        elif state == "escalate":
            page_human()
            return
        elif state == "wait_human_review":
            await_pr_decision()
            return
```

### 6.5 2025 production examples
- **Vercel Agent**: AI-based PR review + auto-patches, validated in real-world before applied
- **AWS App Runner**: traffic-shaped canary built-in
- **Datadog Watchdog**: ML-based anomaly detection + RCA
- **Rootly**: 70% MTTR cut via automated postmortems + remediation suggestions
- **DataDome DomeScribe**: autogenerates first-draft postmortems
- **Self-driving deployments** (Vercel): canary + auto-rollback on regression

---

## 7. Long-running agent infrastructure

### 7.1 Cron-driven tasks

```python
# scheduled_tasks.py
@cron("0 * * * *")
def hourly_codebase_scan():
    # static analysis loop
    pass

@cron("0 6 * * 1")  # Mondays 6am
def weekly_dependency_upgrade():
    pass

@cron("*/15 * * * *")
def quarter_hourly_competitor_check():
    pass
```

### 7.2 Event-driven (webhooks)

```python
@webhook("github.push")
def on_push(event):
    if event.branch == "main":
        spawn_agent("ci_loop", commit=event.head)

@webhook("sentry.error")
def on_error(event):
    if event.level >= "warning":
        spawn_agent("incident_agent", error=event)

@webhook("datadog.alert")
def on_alert(event):
    spawn_agent("monitor_agent", alert=event)
```

### 7.3 Agent supervisor patterns (Erlang/OTP-style)

From "Supervisor Trees and Fault Tolerance Patterns for AI Agent Systems" (Zylos, March 2026):

```
SupervisorAgent
├── PlannerAgent
├── WorkerPool (5-20 agents)
│   ├── WorkerAgent-1
│   ├── WorkerAgent-2
│   └── ...
├── JudgeAgent
└── MemoryAgent (writes to Letta/Cognee)
```

**Supervisor responsibilities**:
- Monitor child agent health (heartbeat every 30s)
- Restart on crash with exponential backoff (5s, 10s, 20s, 40s, max 5min)
- Stop after 3 failed restarts → escalate
- Validate child state before resuming (replay from event log)

### 7.4 Restart-on-crash

LangGraph: `MemorySaver` / `SqliteSaver` / `PostgresSaver` checkpointers. Persist state after every node. Resume by `thread_id`.

```python
from langgraph.checkpoint.sqlite import SqliteSaver
checkpointer = SqliteSaver.from_conn_string("/var/agent/state.db")
graph = workflow.compile(checkpointer=checkpointer)

# resume after crash
config = {"configurable": {"thread_id": "session-42"}}
graph.invoke(None, config)  # picks up where it left off
```

Microsoft Durable Task extension: same idea on Azure Storage; survives process restart and machine migration.

### 7.5 Resource budget enforcement

```python
class BudgetGuard:
    def __init__(self, max_tokens, max_dollars, max_wallclock_min):
        self.spent_tokens = 0
        self.spent_dollars = 0.0
        self.start_time = time.time()
        self.max = (max_tokens, max_dollars, max_wallclock_min)
    
    def check(self):
        if self.spent_tokens > self.max[0]:
            raise BudgetExhausted("token")
        if self.spent_dollars > self.max[1]:
            raise BudgetExhausted("dollar")
        if (time.time() - self.start_time) / 60 > self.max[2]:
            raise BudgetExhausted("wallclock")
    
    def deduct(self, tokens, dollars):
        self.spent_tokens += tokens
        self.spent_dollars += dollars
        self.check()
```

### 7.6 Specific frameworks

| Framework | Strength | Use for Surrogate-1 |
|---|---|---|
| **Inngest** | Step functions, durable execution, replay | Event-driven webhook agents |
| **Temporal** | Mission-critical durability, long workflows | Production deploy pipelines |
| **Trigger.dev** | TypeScript-first, great DX | Scheduled scans + competitor poll |
| **Restate** | Distributed durable + state | Multi-agent coordination |
| **LangGraph** | Native LLM agents, checkpointers | The agent loop itself |

**Recommended Surrogate-1 v2 stack**:
- Inngest for triggers (cron + webhook)
- LangGraph (with SqliteSaver) for agent loop
- Letta for stateful memory across sessions

### 7.7 19-day unattended supervisor case study

DEV.to article ("I let an AI Agent Supervisor Run Unattended for 19 Days"): observed patterns:
- Restart cooldown: 5min between restarts; stop after 3 fails → escalate
- Memory leak in agent context after ~6h → mandatory restart at 12h
- Agent picked up tasks from queue → drained queue → idled 2-3hr → re-checked
- Telemetry: token spend, error rate, task completion %, idle %
- **80% of crashes** were external API failures (rate limits, network) — needed retry logic, not agent fixes

---

## 8. Memory + state for continuous agents

### 8.1 Sticky session memory (across days/weeks)

**Letta v1** (rebuilt from MemGPT, 2025):
- Core memory (always in context, like RAM): user info, current task summary
- Recall memory (in DB, retrieved on demand): past conversations
- Archival memory (long-term, vector + graph): facts, patterns, lessons
- Persisted to PostgreSQL — never lost even after eviction
- **Context Constitution** (2025): principles for how agents manage context

### 8.2 MemGPT — original paper

UC Berkeley Sky Computing Lab. Paradigm: **LLM-as-OS**. Model manages its own RAM/disk. Specifically:
- **Function calls** to read/write archival memory
- **Self-trigger** to compress conversation when context fills
- **Page faults** when needed memory not in context

This is the conceptual ancestor of every "stateful agent" today.

### 8.3 Cognee (GraphRAG for project memory)

- Combines vector embeddings + knowledge graph
- 14 retrieval modes; default = `GRAPH_COMPLETION` (vector → graph traversal → context)
- Hooks into Claude Code lifecycle: SessionStart, PostToolUse, UserPromptSubmit, PreCompact, SessionEnd
- 1M+ pipelines/month in production at 70+ companies (2025)
- Stores triplets (subject-relation-object) extracted from any data
- $7.5M seed funding (2025)

**Why GraphRAG > flat RAG** (key insight): code questions are relational. "What functions call `authenticate`?" "What modules depend on `auth.service`?" Vector similarity gives *semantically similar* results. Graph traversal gives *structurally connected* results. **Code understanding needs both.**

### 8.4 Project-scoped context

```
project_memory/
├── AGENTS.md              # patterns, conventions, gotchas
├── CHANGELOG.md           # auto-generated decisions log
├── todo.md                # current task list
├── progress.txt           # iteration log
├── prd.json               # machine-readable backlog
├── lessons/               # accumulated specific lessons
│   ├── auth-bugs.md
│   └── deploy-issues.md
└── graph/                 # cognee-managed
    └── triplets.db
```

### 8.5 Surrogate-1 v2 memory stack

```
┌─────────────────────────────────┐
│  CONTEXT WINDOW (8-32k tokens)  │   ← what model sees per call
│  - System prompt                │
│  - todo.md                      │
│  - last 5 (action, observation) │
│  - relevant chunks from RAG     │
└─────────────────────────────────┘
           ↑           ↓
┌─────────────────────────────────┐
│  CORE MEMORY (Letta)            │   ← always-loaded summary
│  - project metadata             │
│  - user preferences             │
│  - active goal                  │
└─────────────────────────────────┘
           ↑           ↓
┌─────────────────────────────────┐
│  ARCHIVAL (Cognee GraphRAG)     │   ← on-demand retrieval
│  - codebase graph               │
│  - past trajectories            │
│  - lessons learned              │
└─────────────────────────────────┘
```

### 8.6 Top-10 AI memory products 2026 (Bobur Medium, 2026)
1. **Letta** (memory-first agents)
2. **Cognee** (GraphRAG)
3. **Mem0** (developer-friendly)
4. **Zep** (graph-based, Memgraph backend)
5. **MotorHead** (open source)
6. **Pinecone Memory** (vector-first)
7. **Weaviate** (hybrid)
8. **LangMem** (LangGraph-native)
9. **Memstack** (newer entrant)
10. **GraphAware Hume** (enterprise)

---

## 9. Decision rules — act vs ask vs wait

### 9.1 Confidence thresholds

```python
def decide(action, confidence, env):
    if action.is_irreversible:
        return "ASK_HUMAN"
    if env.is_production and confidence < 0.95:
        return "ASK_HUMAN"
    if env.cost_estimate > env.dollar_threshold:
        return "ASK_HUMAN"
    if confidence > 0.85:
        return "ACT"
    if confidence > 0.6:
        return "ACT_WITH_SHADOW_LOG"  # act but flag for review
    return "DEFER"
```

### 9.2 Reversibility checks

| Action | Reversibility | Decision |
|---|---|---|
| Read file | full | act |
| Write file (new) | git revert | act |
| Write file (overwrite tracked) | git revert | act |
| `rm` tracked file | git revert | act with care |
| `rm -rf` untracked | NONE | ask |
| `git push --force` | NONE | ask |
| `terraform destroy` | NONE | ask |
| `DROP TABLE` | NONE (without backup) | ask |
| Send email/SMS | NONE | ask |
| Make payment | NONE | ask |
| Deploy to prod (canary) | rollback | act with health gate |
| Deploy to prod (full) | redeploy old version | act with health gate |

### 9.3 Cost gates

- LLM tokens: budget per session ($X), per day ($Y), per task ($Z)
- Cloud spend: budget per session ($A), per day ($B)
- Wallclock: budget per task (Z minutes), per session (T hours)
- API quota: track per provider (OpenAI: requests/min, Stripe: requests/sec)

### 9.4 Trust boundaries

```
Trust tier 1 (read-only):       file read, web fetch, git log, list
Trust tier 2 (write to sandbox): git checkout new branch, write file in branch
Trust tier 3 (write to repo):    merge to main (only after CI green)
Trust tier 4 (write to prod):    deploy (only after canary green)
Trust tier 5 (irreversible):     destroy, payment, send-message → always human
```

### 9.5 Async approval workflows

For yellow-tier actions: agent posts decision draft to Slack/PR, waits N minutes for human ACK or NACK; defaults to NACK (safe).

Sample message:
```
🟡 Agent decision pending review
Action: Upgrade `lodash` 4.17.20 → 4.17.21 (security patch)
Risk: 3/10 (patch version, 0 breaking changes in changelog)
Tests: ALL PASS (147/147)
Auto-deploy in: 30 minutes unless someone reacts ❌
```

---

## 10. Failure handling without human

### 10.1 Self-recovery from errors

Common failure modes and responses:

| Failure | Detection | Response |
|---|---|---|
| Test fails | exit code != 0 | re-run; if persists, agent.diagnose() + propose fix |
| Compile error | stderr | parse error; locate file:line; agent fixes |
| Type error | mypy/tsc | parse; locate; agent fixes |
| Lint error | ruff/eslint | --fix or agent fixes |
| API rate limit | 429 | exponential backoff (1s, 2s, 4s, 8s, 16s, max 60s) |
| Network error | timeout | retry 3x; if all fail → defer |
| OOM | killed by OS | restart agent with smaller batch |
| Infinite loop | wallclock budget | abort; mark as failed; log |
| Hallucinated tool call | tool_not_found | retry with reminder of available tools |

### 10.2 Retry with backoff + jitter

```python
def retry_with_jitter(fn, max_retries=5):
    for attempt in range(max_retries):
        try:
            return fn()
        except RetryableError as e:
            wait = min(60, 2 ** attempt) + random.uniform(0, 1)
            time.sleep(wait)
    raise MaxRetriesExceeded()
```

### 10.3 Fallback to alternative approach

```python
def execute_with_fallback(task, strategies):
    for strategy in strategies:
        try:
            result = strategy.execute(task)
            if validate(result):
                return result
        except Exception as e:
            log(f"Strategy {strategy.name} failed: {e}")
    return None  # all strategies exhausted
```

Example: implementing OAuth → strategies = [passport-oauth2, auth0-js, manual-impl]. Try preferred; if fails, fallback in order.

### 10.4 When to abort + ask

- 3 consecutive failures on same task → ABORT
- Safety violation (attempts to bypass gate) → ABORT
- External dependency permanently broken → ABORT
- Budget exhausted with no progress → ABORT

Abort artifact: structured failure report (cause, attempts, evidence, suggested-next-steps for human).

### 10.5 Postmortem on failures (auto-generated)

```python
def auto_postmortem(failure):
    return {
        "incident": failure.summary,
        "timeline": failure.event_log,
        "root_cause": agent.find_root_cause(failure),
        "what_we_tried": failure.attempts,
        "what_worked_before": git.find_similar_past_resolutions(failure),
        "proposed_fix": agent.propose_fix(failure),
        "lessons": agent.extract_lessons(failure),
    }
```

Append lessons to `AGENTS.md` so next sessions inherit. **DataDome DomeScribe** does this commercially.

---

## 11. Specific 2025-2026 papers (annotated)

### 11.1 SWE-Gym (ICML 2025)
**arxiv 2412.21139** — Training Software Engineering Agents and Verifiers
- 2,438 real-world Python tasks; each = codebase + runtime + tests + NL spec
- Trained agents got +19% absolute on SWE-Bench Verified
- Inference-time scaling with verifiers: 32% on SWE-Bench Verified, 26% on Lite
- **Open-weight SOTA at time** for SWE agents

### 11.2 R2E-Gym (COLM 2025)
**github.com/R2E-Gym/R2E-Gym** — Procedural environment generation + hybrid verifiers for scaling open-weights SWE agents
- Scaled approach to generate training environments
- DeepSWE was trained on 4,500 R2E-Gym tasks across 6 days on 64 H100s

### 11.3 DeepSWE (Together.ai, July 2025)
**together.ai/blog/deepswe** — Fully open RL coding agent
- Qwen3-32B base + pure RL via rLLM framework
- **59% on SWE-Bench Verified with test-time scaling** (42.2% Pass@1, 71.0% Pass@16)
- Open: dataset, code, training, eval logs
- Emergent behaviors: anticipates edge cases, runs regression tests proactively

### 11.4 Training Long-Context Multi-Turn SWE Agents with RL (Aug 2025)
**arxiv 2508.03501** — applied to Qwen2.5-72B-Instruct
- Two-stage: rejection fine-tuning (RFT) + DAPO RL
- 11% → 20% (after RFT) → 39% (after RL)
- Reward = test pass + format adherence
- **This recipe is directly applicable to Surrogate-1 7B**

### 11.5 AgentGym-RL (Sept 2025)
**arxiv 2509.08755** — ScalingInter-RL approach
- Stage 1: short-horizon (3-5 step) tasks for foundational policy
- Stage 2: progressively expand horizon (10, 20, 50 steps)
- **Critical insight**: don't train long-horizon from scratch. Bootstrap from short.
- Modular framework supporting GRPO, PPO, DAPO

### 11.6 SWE-Bench Pro (Sept 2025, Scale AI)
**arxiv 2509.16941** — Long-horizon enterprise tasks
- 1,865 problems from 41 actively-maintained repos (business apps, B2B, dev tools)
- Reference solutions: avg 107 LOC across 4.1 files
- Top score (GPT-5) = 23.3% Pass@1
- **This is the realistic target for Surrogate-1 v2** (don't aim for SWE-Bench Verified saturation)

### 11.7 Manus AI architecture analysis (May 2025)
**arxiv 2505.02024** — From Mind to Machine: Manus AI Autonomous Agent
- Detailed reverse-engineering of Manus
- CodeAct + todo.md + virtual file system + parallel sub-agents

### 11.8 Reflexion (NeurIPS 2023, still SOTA pattern in 2026)
**arxiv 2303.11366** — Verbal RL for agents
- Actor + Evaluator + Self-Reflection components
- Episodic memory of reflections; no weight updates
- HumanEval pass@1 jump with reflection trials

### 11.9 LATS (ICML 2024)
**arxiv 2310.04406** — Language Agent Tree Search
- MCTS-style agent search
- Subsumes Reflexion + ToT + plan-execute
- HumanEval pass@1 = 92.7% with GPT-4

### 11.10 DAPO (Mar 2025, ByteDance Seed)
**arxiv 2503.14476** — Open-source RL system at scale
- 4 techniques: Clip-Higher, Dynamic Sampling, Token-level Policy Gradient Loss, Overlong Reward Shaping
- AIME 2024: 50 points on Qwen2.5-32B (vs DeepSeek-R1-Zero-Qwen-32B 47)
- Built on `verl` framework

### 11.11 CodeAct (ICML 2024)
**arxiv 2402.01030** — Executable Code Actions Elicit Better LLM Agents
- 7k multi-turn interactions in CodeActInstruct
- Up to 20% higher success rate vs JSON-tool agents
- CodeActAgent (fine-tuned Llama2/Mistral) self-debugs

### 11.12 Voyager (NeurIPS 2023, Minecraft)
**arxiv 2305.16291** — first lifelong-learning embodied LLM agent
- Skill library (executable code, retrievable by embedding)
- Automatic curriculum
- Iterative prompting with env feedback + self-verification
- **Skills as code, not natural language** — sidesteps LLM "remembering" ambiguity

### 11.13 Agent Data Protocol (Oct 2025)
**arxiv 2510.24702** — Unifying agent training datasets
- 1.3M trajectories in unified Action/Observation format
- ADP Dataset V1 = largest public agent training set

### 11.14 AgentTrek (ICLR 2025)
**openreview EEgYUccwsV** — Trajectory synthesis from web tutorials
- 6,000 trajectories generated by replaying tutorials
- Used to fine-tune Qwen2.5 7B and 32B

### 11.15 ATLAS (ACL 2025)
**aclanthology 2025.findings-acl.1299** — Agent Tuning via Learning Critical Steps
- Fine-tune only on critical (high-information) trajectory steps
- Outperforms full-trajectory fine-tuning

---

## 12. Training data for autonomous behavior

### 12.1 Public continuous-run agent traces (rare!)

These are scarce because most agent runs are private.

Available:
- **SWE-Gym** trajectories (open) — multi-step, real fixes
- **R2E-Gym** procedurally generated — scaled
- **OpenHands public traces** — some posted to HF
- **DeepSWE training logs** — open
- **AgentTrek synthesized** — 6k web tasks
- **Agent Data Protocol** — 1.3M unified trajectories
- **CodeActInstruct** — 7k

### 12.2 GitHub commit histories (one project's evolution)

Mine from: any active OSS project's git log:
- Issue → PR → review comments → merged commits
- "Implementation arc" of a feature, naturally long-horizon
- Failure cases captured: reverts, hotfixes, "fix bug from #123"

```python
def mine_continuous_trajectory(repo, issue_number):
    issue = github.get_issue(repo, issue_number)
    related_prs = github.find_related_prs(issue)
    trajectory = []
    for pr in related_prs:
        for commit in pr.commits:
            for file_change in commit.files:
                trajectory.append({
                    "thought": commit.message,  # human's reasoning
                    "action": diff_to_action(file_change),
                    "observation": ci_result(commit),
                })
    return trajectory
```

### 12.3 Open-source project READMEs + roadmaps

Plain-English roadmap = labeled goal decomposition. Project's commit history = trajectory.

### 12.4 Synthesize: feed Claude Opus a project, generate continuous-improvement rollouts

This is the **highest-leverage path** for Surrogate-1.

```python
def synthesize_trajectory(project_path, n_episodes=100):
    for ep in range(n_episodes):
        # Pick a goal automatically
        goal = claude_opus.invent_realistic_goal(project_path)
        # Run an autonomous loop with Claude Opus as agent
        trajectory = []
        state = "start"
        while not done:
            thought = claude_opus.think(goal, state, trajectory[-5:])
            action = claude_opus.act(thought, state)
            obs = sandbox.execute(action)
            trajectory.append((thought, action, obs))
            state = update_state(state, action, obs)
            if claude_opus.check_done(goal, state):
                done = True
        save_trajectory(trajectory, success=success)
```

**Volume target**: 10k+ synthetic trajectories. Quality > quantity. Use Claude Opus 4.7 for diversity.

**Cost estimate**: avg trajectory = 50 steps × 2k tokens = 100k tokens. 10k trajectories = 1B tokens. At Claude Opus pricing ($15/M input, $75/M output) ≈ $30k-50k. Use **Sonnet** or batch API → cut to ~$5-10k.

### 12.5 2025 datasets (long-horizon agent benchmarks)

| Dataset | Domain | Size | Use for training? | Use for eval? |
|---|---|---|---|---|
| SWE-Gym | SWE | 2,438 | yes | overfit risk |
| R2E-Gym | SWE | 4,500+ | yes | yes |
| SWE-Bench Verified | SWE | 500 | held-out | yes |
| SWE-Bench Pro | SWE long-horizon | 1,865 | held-out | **primary eval** |
| GAIA | general assistant | 450 | held-out | yes |
| AgentBench | 8 environments | varied | yes | yes |
| BrowseGym | web | varied | yes | yes |
| WebArena | web nav | 812 | held-out | yes |
| OSWorld | OS computer-use | 369 | held-out | yes |
| AgentTrek | web from tutorials | 6,000 | yes | no |
| ADP Dataset V1 | unified | 1.3M | yes | no |

### 12.6 Negative samples (NAT method, NAACL 2025)
Mining failures explicitly. Train on (thought, wrong-action, error) tuples, contrastive with (thought, right-action, success). Improves robustness to unfamiliar errors.

---

## 13. Architecture for Surrogate-1 v2 — full pseudo-code

### 13.1 Top-level loop

```python
# surrogate_loop.py
import asyncio
from langgraph.graph import StateGraph
from langgraph.checkpoint.sqlite import SqliteSaver
from letta import Letta
from cognee import Cognee
from inngest import Inngest

inngest = Inngest(app_id="surrogate-1")
checkpointer = SqliteSaver.from_conn_string("/var/surrogate/state.db")
memory = Letta(storage="postgres://...")
graph_mem = Cognee(graph_db="falkordb://localhost")

class State(TypedDict):
    goal: str
    todo: list  # parsed from todo.md
    history: list  # (thought, action, observation)
    iteration: int
    confidence: float
    failures: list

# === NODES ===

def planner(state: State) -> State:
    """Read project + goal; produce/update todo.md."""
    project_ctx = graph_mem.query_project_summary()
    plan = surrogate.plan(state.goal, project_ctx, state.todo)
    write_todo_md(plan)
    state.todo = plan
    return state

def executor(state: State) -> State:
    """Pick next task; execute one CodeAct iteration."""
    next_task = select_next_task(state.todo, state.failures)
    if next_task is None:
        state.done = True
        return state
    
    thought = surrogate.think(next_task, state.history[-5:], graph_mem)
    action = surrogate.act_codeact(thought, next_task)  # Python code
    
    # Decision gate
    decision = decide(action, surrogate.confidence, env)
    if decision == "ASK_HUMAN":
        request_human_review(action, thought)
        state.paused = True
        return state
    
    obs = sandbox.execute(action)
    state.history.append((thought, action, obs))
    state.iteration += 1
    
    if obs.error:
        state.failures.append((next_task.id, obs.error))
    elif obs.task_complete:
        mark_done(state.todo, next_task)
    
    # Persist intermediate to memory
    memory.append_recall((thought, action, obs))
    if obs.task_complete:
        graph_mem.ingest_lesson(thought, action, next_task)
    
    return state

def reflector(state: State) -> State:
    """After failure or N iters, reflect verbally (Reflexion)."""
    if not state.failures and state.iteration % 10 != 0:
        return state
    reflection = surrogate.reflect(state.history, state.failures)
    append_to_lessons(reflection)
    memory.upsert_core(f"Latest reflection: {reflection.summary}")
    state.failures = []  # clear after reflecting
    return state

def judge(state: State) -> State:
    """End-of-cycle evaluation: continue, done, escalate?"""
    eval_ = surrogate.evaluate(state.goal, state.history, state.todo)
    if eval_.goal_satisfied:
        state.done = True
    elif eval_.deadlock:
        state.escalate = True
    elif eval_.budget_exhausted:
        state.defer = True
    return state

# === EDGES ===
graph = StateGraph(State)
graph.add_node("planner", planner)
graph.add_node("executor", executor)
graph.add_node("reflector", reflector)
graph.add_node("judge", judge)

graph.add_edge("planner", "executor")
graph.add_conditional_edges("executor", lambda s:
    "judge" if s.done else
    "reflector" if s.failures else
    "executor"  # loop back
)
graph.add_edge("reflector", "executor")
graph.add_conditional_edges("judge", lambda s:
    "END" if s.done or s.escalate or s.defer else "planner"
)

graph.set_entry_point("planner")
app = graph.compile(checkpointer=checkpointer)

# === EVENT HANDLERS ===

@inngest.create_function(
    fn_id="surrogate-on-issue",
    trigger=inngest.TriggerEvent(event="github.issue.opened"),
)
async def on_issue(event):
    config = {"configurable": {"thread_id": f"issue-{event.issue.id}"}}
    initial_state = State(
        goal=event.issue.title + "\n\n" + event.issue.body,
        todo=[], history=[], iteration=0, confidence=0.0, failures=[],
    )
    await app.ainvoke(initial_state, config)

@inngest.create_function(
    fn_id="surrogate-cron-improve",
    trigger=inngest.TriggerCron(cron="0 2 * * *"),  # 2am daily
)
async def nightly_self_improvement(event):
    config = {"configurable": {"thread_id": f"selfimprove-{date.today()}"}}
    initial_state = State(
        goal="Run static analysis loop, write missing tests, refactor complex functions",
        todo=[], history=[], iteration=0, confidence=0.0, failures=[],
    )
    await app.ainvoke(initial_state, config)

@inngest.create_function(
    fn_id="surrogate-on-anomaly",
    trigger=inngest.TriggerEvent(event="datadog.alert.fired"),
)
async def on_anomaly(event):
    config = {"configurable": {"thread_id": f"incident-{event.alert.id}"}}
    initial_state = State(
        goal=f"Investigate and remediate alert: {event.alert.message}",
        todo=[], history=[], iteration=0, confidence=0.0, failures=[],
    )
    await app.ainvoke(initial_state, config)
```

### 13.2 CodeAct prompt format (Surrogate-1 native)

```
<system>
You are Surrogate-1, an autonomous software engineer. You write Python code as your primary action format. You have access to: shell, file edit, web fetch, git, test runners.

Format every turn as:
THOUGHT: <reasoning about current state and goal>
ACTION:
```python
# your code
```
OBSERVATION: <will be filled by environment>

After observation, decide: continue, ask, or done.

Goal: {goal}
Active task: {next_task}
Recent history (last 5):
{history_summary}
Lessons from past sessions (top-k from graph):
{retrieved_lessons}
</system>
```

### 13.3 Decision gate implementation

```python
class DecisionGate:
    def __init__(self, env, policy):
        self.env = env
        self.policy = policy  # learned or rule-based
    
    def decide(self, action, thought, confidence):
        # Rule layer
        if self._is_irreversible(action):
            return Gate(decision="ASK_HUMAN", reason="irreversible")
        if self._exceeds_budget(action):
            return Gate(decision="DEFER", reason="budget")
        if self._violates_policy(action):
            return Gate(decision="ABORT", reason="policy")
        
        # Confidence layer
        if self.env.is_production:
            if confidence < 0.95:
                return Gate(decision="ASK_HUMAN", reason="prod_low_conf")
        if confidence < 0.6:
            return Gate(decision="DEFER", reason="low_conf")
        
        return Gate(decision="ACT", reason="green")
    
    def _is_irreversible(self, action):
        risky_patterns = [
            r"rm\s+-rf\s+/",  # rm rf root
            r"git\s+push\s+--force",
            r"DROP\s+TABLE",
            r"terraform\s+destroy",
            r"kubectl\s+delete\s+namespace",
            r"send_email|charge_card|publish",
        ]
        for pat in risky_patterns:
            if re.search(pat, action.code):
                return True
        return False
```

---

## 14. Surrogate-1 v2 plan — concrete

### 14.1 Architecture

- Base: Qwen2.5-Coder-7B (already chosen, fits 24GB)
- Adapter: LoRA r=64, target all attn + mlp
- Action format: CodeAct (Python inline)
- Loop: planner-executor-reflector-judge (LangGraph)
- Memory: Letta (Postgres) + Cognee (FalkorDB graph)
- Infra: Inngest triggers + LangGraph SqliteSaver

### 14.2 Training stages

**Stage 0: Inherit from base**
- Qwen2.5-Coder-7B already strong on isolated coding (HumanEval 76)

**Stage 1: SFT on agent trajectories**
- ADP Dataset V1 subset (filter to coding) ≈ 200k trajectories
- AgentTrek 6k web trajectories
- CodeActInstruct 7k
- Synthetic via Claude Sonnet: 5k continuous-improvement rollouts
- Format: thought-action-observation. Train on critical steps only (ATLAS method).

**Stage 2: RFT (rejection fine-tuning) on R2E-Gym**
- 4,500 SWE tasks; rollout 10x per task; keep only successful trajectories
- Trains format adherence + tool use

**Stage 3: RL via DAPO**
- verl framework + custom reward
- Reward = test pass + format + step efficiency penalty
- ScalingInter-RL: start at 5-step horizons, expand to 50-step
- 30k tasks total; ~10 days on 8x H100s

**Stage 4: Eval + iterate**
- SWE-Bench Verified (held-out)
- SWE-Bench Pro (primary, long-horizon)
- Custom Surrogate Continuous Bench (define below)

### 14.3 Continuous Bench (custom eval)

Surrogate-specific bench: realistic continuous-run scenarios.

```yaml
suite:
  - name: nightly_static_analysis_loop
    setup: medium-sized real Python repo with known issues
    goal: "Run lint, fix all auto-fixable issues, write tests for uncovered functions, open PRs"
    success: PRs opened, CI green, no regressions, reasonable token use
    horizon: 50-200 steps
  
  - name: feature_implementation_from_issue
    setup: real GitHub issue
    goal: implement, write tests, open PR
    success: tests pass, PR merges (simulated reviewer)
    horizon: 100-300 steps
  
  - name: incident_response
    setup: deployed app with synthetic anomaly (latency spike)
    goal: diagnose, propose fix, deploy or escalate
    success: correct root cause + correct decision (fix vs escalate)
    horizon: 20-50 steps
  
  - name: dependency_upgrade_marathon
    setup: repo with 50 outdated deps
    goal: upgrade safely
    success: % deps upgraded with all tests passing
    horizon: 200-500 steps
  
  - name: competitor_feature_analysis
    setup: project + 3 competitor URLs
    goal: identify feature gaps, propose top 5
    success: human-rated quality of proposals
    horizon: 30-100 steps

target_scores:
  v2_launch:
    nightly_static_analysis_loop: 60% task completion
    feature_implementation_from_issue: 25%
    incident_response: 50%
    dependency_upgrade_marathon: 70% safe upgrades
    competitor_feature_analysis: 3.5/5 human rating
```

### 14.4 Eval target

**40% autonomous-task-completion** averaged across Continuous Bench. This is realistic for 7B model:
- DeepSWE 32B = 59% on SWE-Bench Verified, but our bench is harder (long-horizon)
- GPT-5 = 23.3% on SWE-Bench Pro
- 7B with strong RL should hit ~half of frontier 32B

If achieved: Surrogate-1 v2 is **runnable as-is** for nightly self-improvement, simple feature implementation from issues, basic incident triage. **NOT yet runnable** unsupervised in production for arbitrary feature work.

---

## 15. Concrete trace data examples

### 15.1 Single-step trajectory (training data format)

```jsonl
{"thread_id": "session-42", "step": 17, "thought": "The login test is failing because the OAuth callback handler doesn't set httpOnly on the session cookie. I need to update src/auth/session.ts.", "action": "```python\ncode = '''\\nimport { serialize } from 'cookie';\\nexport function setSessionCookie(res, sessionId) {\\n  const cookie = serialize('session', sessionId, {\\n    httpOnly: true,\\n    secure: true,\\n    sameSite: 'lax',\\n    maxAge: 60*60*24*7,\\n    path: '/',\\n  });\\n  res.setHeader('Set-Cookie', cookie);\\n}\\n'''\\nedit_file('src/auth/session.ts', code)\\nrun_test('tests/auth/oauth.test.ts')\\n```", "observation": "Test passed: 1/1 in oauth.test.ts. Coverage of src/auth/session.ts: 92%.", "reward": 1.0, "done_substep": true}
```

### 15.2 Long-horizon trajectory (50 steps, abbreviated)

```yaml
thread: "implement-stripe-billing"
goal: "Add Stripe-based subscription billing"
steps:
  1: { thought: "Read existing payment code", action: read_dir("src/payments"), obs: "empty dir" }
  2: { thought: "Need to add Stripe SDK", action: pip_install("stripe"), obs: "ok" }
  3: { thought: "Create payment service skeleton", action: write_file("src/payments/stripe.py", "..."), obs: "ok" }
  4: { thought: "Add webhook endpoint", action: write_file("src/api/webhooks.py", "..."), obs: "ok" }
  5: { thought: "Test webhook signature verification", action: run_test, obs: "FAIL: invalid signature" }
  6: { thought: "Reflexion: I forgot to set STRIPE_WEBHOOK_SECRET in test env", action: edit_test_setup, obs: "ok" }
  7: { thought: "Re-run test", action: run_test, obs: "PASS" }
  ...
  47: { thought: "Final integration test", action: run_full_suite, obs: "PASS 217/217" }
  48: { thought: "Open PR", action: gh_pr_create("Add Stripe billing"), obs: "PR #123 opened" }
  49: { thought: "Update CHANGELOG", action: edit_file("CHANGELOG.md"), obs: "ok" }
  50: { thought: "Goal satisfied", action: mark_done, obs: "ok" }
final_reward: 1.0
duration: 4h 12min
tokens: 1.2M
```

### 15.3 Reflexion event

```yaml
event_type: reflection
trigger: 3 consecutive test failures on T-007
trajectory_segment: [step_42, step_43, step_44]
reflection_text: |
  Pattern observed: I keep trying to mock `stripe.Webhook.construct_event` 
  with simple stubs, but it requires a real signature. The fix is to use 
  the official Stripe test fixtures from `stripe.util.TestFixtures.webhook_event(...)`.
  
  Add to AGENTS.md under "Gotchas":
  - Stripe webhook tests require official test fixtures, not stubs.
  - Set STRIPE_WEBHOOK_SECRET in test env via conftest.py.
  - signature header format: `t=<timestamp>,v1=<hex>`.

action_taken: append_to_AGENTS_md
next_attempt_uses_lesson: true
result: PASS on next attempt
```

---

## 16. Key open questions for Surrogate-1 v2

1. **Synthetic trajectory generation cost**: 10k Claude Sonnet trajectories ≈ $5k. Worth it? Probably yes — biggest leverage point.

2. **DAPO vs simpler GRPO**: DAPO is bleeding edge. Start with GRPO via verl, upgrade if reward signal noisy.

3. **Letta + Cognee integration overhead**: real engineering work. Could start with simpler `progress.txt` + git, add memory layer in v3.

4. **Sandbox**: e2b vs modal vs Docker-local. Local Docker for dev; e2b/modal for prod elasticity.

5. **24GB memory constraint**: 7B + LoRA fits in 24GB. Inference with 32k context is OK. Training requires offload to 80GB H100s.

6. **Deploy story for Surrogate-1**: where does it run? User's M3 (24GB) for dev, Lightning H200 / Modal for training. Production inference: cheap CPU + LoRA-served via vLLM, or 4090 dedicated.

7. **Eval gaming risk**: SWE-bench / WebArena are gameable (recent paper, "How We Broke Top AI Agent Benchmarks"). Need custom held-out bench for Surrogate-1.

8. **Continuous improvement vs catastrophic forgetting**: if we keep fine-tuning, base capability degrades. Solution: freeze base + always-add LoRA layers per domain, or use adapter-stacking.

---

## 17. Recommended reading order (for implementation)

1. CodeAct paper (foundational action format)
2. OpenHands SDK paper (canonical loop)
3. Manus arxiv 2505.02024 (file-system pattern)
4. SWE-Gym ICML 2025 (training data)
5. DeepSWE blog (full RL recipe)
6. DAPO paper (RL details)
7. Letta v1 blog (memory architecture)
8. Cognee blog (GraphRAG for code)
9. Devin annual review 2025 (real-world failure modes)
10. Cursor scaling-agents blog (planner-worker pattern)

---

## 18. v2 plan — 300-word summary (for caller)

**Architecture**: Qwen2.5-Coder-7B + LoRA, action format = CodeAct (Python > JSON). Loop = LangGraph with planner / executor / reflector / judge nodes. State persisted via SqliteSaver (resume-on-crash). Memory = Letta (core+recall+archival) + Cognee GraphRAG (codebase graph). Triggers = Inngest cron + GitHub/Sentry/Datadog webhooks.

**Borrow from frontier**: Manus `todo.md` externalized state + virtual file system; Devin Green-Yellow-Red confidence + Interactive Planning; Cursor planner-worker hierarchy (no peer coordination); OpenHands event-sourced replay; Aider git-as-persistence (every action = commit).

**Training pipeline (4 stages)**:
1. SFT on ADP Dataset V1 + AgentTrek + CodeActInstruct + 5k synthetic continuous-rollouts via Claude Sonnet (cost ~$5k)
2. RFT on R2E-Gym (4.5k SWE tasks, 10x rollout, keep successes)
3. RL via DAPO on verl with ScalingInter-RL (start 5-step, scale to 50-step horizons)
4. Reward = test pass + format adherence + step efficiency penalty

**Datasets**: SWE-Gym (2.4k), R2E-Gym (4.5k), AgentTrek (6k), ADP V1 subset (~200k filtered), CodeActInstruct (7k), synthetic Claude rollouts (5-10k), GitHub mined trajectories (10k+ from public OSS).

**Decision rules**: rule-layer for irreversible/budget gates → confidence-layer (>0.95 prod, >0.85 dev = act; <0.6 = ask). Reflexion verbal reflection on failures, appended to AGENTS.md. Auto-postmortem on incidents (DataDome DomeScribe pattern).

**Eval target**: **40% autonomous-task-completion** on custom Surrogate Continuous Bench (5 scenarios: nightly static-analysis, feature implementation from issue, incident response, dependency upgrade marathon, competitor feature analysis). Held-out: SWE-Bench Pro (target 12-15% — half of GPT-5's 23.3%).

**Compute**: training on Lightning H200 / Modal (8x H100 for RL stage, ~10 days). Inference on user's M3 (24GB) via vLLM serving LoRA. Production: 4090 or cheap H100 spot.

**Infrastructure**: LangGraph + Inngest + Letta-Postgres + Cognee-FalkorDB + e2b sandbox. ~$200-500/mo OpEx for production. Resume-on-crash automatic via SqliteSaver. Supervisor pattern with 5min restart cooldown, 3-fail escalation.

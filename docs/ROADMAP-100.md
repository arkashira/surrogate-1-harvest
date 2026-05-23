# ROADMAP-100 — axentx + surrogate-1 + hermes

**Date**: 2026-05-02
**Scope**: 50 MUST-HAVE + 50 NICE-TO-HAVE features filling gaps from current state
**Counts** (verified): MUST=50, NICE=50

**Current state baseline** (NOT re-listed below — already shipped):
- GCP e2-micro / 22 daemons / 12-stage agent pipeline / 5 auto-bot commits live
- CF Worker+D1+KV+Queues+Cron+Vectorize(1819 chunks)+Workers AI+Pages+Hyperdrive
- Supabase work queue / 168 cron jobs / FOR UPDATE SKIP LOCKED
- HF Hub 9.56TB / Kaggle V19 trainer / 6 HF Spaces / Hermes LOW_MEM
- 11→12 LLM chain / Cursor Worker auth+audit+metrics / /dash / RAG-over-knowledge
- Reviewer dynamic threshold / Discord Thai+stack-aware / agent-decisions-to-pairs

Legend: **S**=≤4h, **M**=½–1d, **L**=1–2d. Tags: `[MUST]` `[NICE]`.

---

## Category 1 — Software-development side (improve velocity/quality of code agents)

| # | Tag | Name | Why (1-2 sentences) | Touched stack | Complexity | Depends-on |
|---|-----|------|---------------------|---------------|-----------:|------------|
| 1 | [MUST] | Per-project knowledge-index | Each axentx repo (Costinel/Vanguard/AxiomOps/Arkship/Surrogate/Workio) has different conventions; one global index pollutes context. Sharded index keyed by `project_slug` lets dev-agent load only relevant patterns and cuts prompt size 60-80%. | dev prompt loader + new D1 table `project_knowledge_index` | M | none |
| 2 | [MUST] | Adapter eval gate before HF Hub publish | We push trained adapters to HF Hub without auto-evaluating against held-out set; bad adapters silently ship and pollute downstream training. Block publish until perplexity + win-rate vs base ≥ baseline by ≥3%. | Kaggle V19 trainer post-step + new GitHub Action `eval_adapter.yml` | L | held-out test set repo |
| 3 | [MUST] | Branch protection on all 6 axentx repos | Auto-bot pushes to `main` directly; one bad reviewer pass corrupts history. Require PR + 1 approval (or auto-bot label) + status check. | GitHub Actions API one-shot script in harvest repo | S | GH PAT in secret |
| 4 | [MUST] | Reviewer rubric versioning | Reviewer prompt evolves; old commits were judged by old rubric, mixing training quality. Pin rubric version to each verdict triple so SFT/DPO data filtering is deterministic. | reviewer agent + D1 column `rubric_version` on agent_decisions | S | none |
| 5 | [MUST] | Test-first dev mode flag | dev-agent currently writes code then tests; flipping order on `*-core` packages catches regressions faster and creates better DPO pairs (test → fail → fix). | dev prompt + new env `TEST_FIRST_PROJECTS` allowlist | S | none |
| 6 | [MUST] | Lint+typecheck gate before reviewer | Reviewer wastes attempts on lint errors that pre-commit could catch; gate blocks reviewer entirely until `eslint --max-warnings 0` and `tsc --noEmit` pass per project's package.json scripts. | dev → pre-reviewer hook in pipeline | S | per-project lint config |
| 7 | [MUST] | Diff size cap with auto-split | dev-agent occasionally produces 800-line diffs that reviewer can't faithfully assess; cap at 250 LOC per commit and force agent to split into sequential PRs. | dev wrapper + git hook | M | none |
| 8 | [MUST] | Codeowners auto-routing per project | Reviewer applies same rubric to TS/Python/SQL; CODEOWNERS file with stack-specific reviewer profiles routes diff-by-diff. | `.github/CODEOWNERS` + reviewer prompt branch | S | branch protection (#3) |
| 9 | [MUST] | Spec-driven scaffold from PRD | PRD agent outputs prose; dev-agent re-derives structure each time. Auto-generate `spec.md`+`plan.md`+`checklist.md` skeleton from PRD so dev starts with structure. | PRD post-processor + new template repo | M | none |
| 10 | [MUST] | Conventional Commits enforced | Mixed commit styles break changelog generation and DPO triple parsing. Enforce `<type>(<scope>): <subject>` via commitlint hook on auto-bot pushes. | commit-agent + commitlint config | S | none |
| 11 | [NICE] | Mutation testing on critical packages | Coverage % lies; mutation score reveals weak tests. Run Stryker (TS) / mutmut (Python) nightly on `*-core` and gate adapter publish on score ≥ 60%. | new GH Action + reviewer signal | L | adapter eval gate (#2) |
| 12 | [NICE] | Visual regression for Workio UI | Workio ships React; CSS regressions slip through unit tests. Playwright + Percy nightly. | new GH Action | M | Workio repo build |
| 13 | [NICE] | Auto-generated API docs from OpenAPI | Dev-agent writes OpenAPI specs but no rendered docs; generate Redoc + publish to CF Pages on each axentx PR. | CI step + Pages site | M | none |
| 14 | [NICE] | Codemod library for cross-repo refactor | Refactoring same pattern across 6 repos manually wastes attempts. Library of jscodeshift + libcst recipes invokable from dev-agent. | new repo `axentx/codemods` | L | per-project knowledge-index (#1) |
| 15 | [NICE] | Property-based tests for parsers | Cursor parser, prompt template renderer, work-queue claim logic — all benefit from fast-check / Hypothesis. | dev-agent test scaffold | M | none |
| 16 | [NICE] | Snapshot tests for prompt templates | Prompts drift silently and change agent behavior; snapshot the rendered prompt for each role and require approved diff. | new test suite in harvest repo | S | none |
| 17 | [NICE] | Storybook for shared UI components | Workio + future Vanguard UI need shared component catalog; reduces design-implementation drift. | new package `@axentx/ui` | L | none |
| 18 | [NICE] | Pre-PR test impact analysis | Run only tests that import changed files; cuts CI from 8min to ~1min on small PRs. | jest --findRelatedTests / pytest-testmon | M | none |
| 19 | [NICE] | Automatic dependency update PRs | Renovate/Dependabot equivalent self-hosted on Worker — no GitHub App quota. Weekly batch. | new Worker route + Cron Trigger | M | none |
| 20 | [NICE] | Refactor-suggesting reviewer mode | Reviewer flags duplicated logic across 2+ files in same diff and proposes extraction; today it only validates correctness. | reviewer prompt addendum | S | per-project knowledge-index (#1) |

**Subtotal Cat 1: MUST=10, NICE=10**

---

## Category 2 — Product-discovery side (improve what we ship)

| # | Tag | Name | Why | Touched stack | Complexity | Depends-on |
|---|-----|------|-----|---------------|-----------:|------------|
| 21 | [MUST] | Idea deduplication before research | research×3 fanout sometimes investigates near-identical ideas; embedding-distance check against last 30 days of bd outputs catches duplicates pre-spend. | research wrapper + Vectorize query | S | none |
| 22 | [MUST] | bd→PRD traceability IDs | Cannot trace which bd insight produced which PRD; auto-stamped `discovery_id` UUID propagated through bd → design → business → marketing → prd → dev. | D1 column added to each agent_decisions stage row | S | none |
| 23 | [MUST] | Customer-discovery interview loop | Discovery is purely AI-generated; one weekly synthetic interview pass via Discord poll to ≥1 real human (you) injects ground truth before PRD freezes. | Discord bot poll + Supabase result queue | M | none |
| 24 | [MUST] | Competitive landscape auto-snapshot | bd reasons in vacuum; weekly scrape (CF Browser Rendering or playwright HF Space) of top-5 competitors per vertical → embed → bd reads at decision time. | new Worker scraper + Vectorize namespace | L | none |
| 25 | [MUST] | Kill criteria in PRD template | PRDs ship without explicit "abandon if X"; force agent to set 3 measurable kill conditions (e.g. "abandon if MAU < 10 by week 4") into every PRD. | prd template + reviewer check | S | none |
| 26 | [MUST] | Marketing claims fact-check | marketing-agent invents stats ("3x faster"); cross-check against bd evidence vector store and block PRD if claim has no source. | reviewer addendum specific to marketing output | M | competitive snapshot (#24) |
| 27 | [NICE] | Persona library with realistic constraints | design-thinking-agent generates fresh personas each run; pinned library of 15 realistic personas (verified against DAU data when available) keeps designs comparable. | new repo asset + design prompt addendum | M | none |
| 28 | [NICE] | Wizard-of-Oz prototype generator | Before dev builds real feature, marketing-agent generates landing page + signup form on CF Pages to gauge real interest. | new template + Pages deploy | L | none |
| 29 | [NICE] | Pricing experiment harness | business-agent picks pricing once; harness rotates 3 price points behind feature flag for 7 days, picks winner by conversion. | new D1 table + Worker A/B middleware | L | analytics events |
| 30 | [NICE] | Discovery retro every Friday | bd→prd cycle has no learning loop; Friday cron summarizes which discoveries shipped, which were killed, why; appended to bd's prompt context. | scheduler cron + bd prompt | M | traceability IDs (#22) |
| 31 | [NICE] | Anti-pattern detector for ideas | Auto-flag ideas matching known failure patterns ("AI for X" oversaturation, regulated-industry quick-win, etc.) with weighted score. | bd post-filter + curated YAML rules | S | none |
| 32 | [NICE] | Customer-quote extraction from Discord | Real user messages in Discord support thread → extract quotes → feed back to bd as voice-of-customer. | discord-bot + Vectorize | M | discord history |

**Subtotal Cat 2: MUST=6, NICE=6**

---

## Category 3 — Operations / observability

| # | Tag | Name | Why | Touched stack | Complexity | Depends-on |
|---|-----|------|-----|---------------|-----------:|------------|
| 33 | [MUST] | Structured JSON logs everywhere | Daemons currently mix print() and logger; ship-to-CF-Logs pipeline can't parse. Standardize `{ts,level,trace_id,daemon,event,...}` schema. | all 22 daemons logger init | M | none |
| 34 | [MUST] | Trace ID propagation through pipeline | Cannot correlate "PRD decision X → dev commit Y → reviewer attempt Z"; UUID generated at scheduler entry, threaded via Supabase task payload + Worker headers + git trailer. | scheduler + worker + commit-agent + D1 table `traces` | M | structured logs (#33) |
| 35 | [MUST] | SLO per pipeline stage | "Pipeline is healthy" undefined; declare SLOs (e.g. dev ≥95% success in 24h window, reviewer p95 ≤ 90s, hermes 0 missed cron in 7d) with burn-rate alerts. | new D1 view + Discord alert rule | M | structured logs (#33) |
| 36 | [MUST] | Synthetic canary every 15 min | space_health probes existence; canary submits a tiny known task end-to-end through entire 12-stage pipeline and measures full path. | new Worker route + Supabase tracker | M | trace ID (#34) |
| 37 | [MUST] | Runbook per known incident pattern | Watchdog alerts but humans (you) re-derive fix each time; markdown runbooks indexed by alert.code, linked in alert payload. | new repo dir `runbooks/` + alert formatter | M | none |
| 38 | [NICE] | On-call rotation (single-person fail-safe) | You are SPOF; even with single dev, define explicit "if Ashira unreachable >24h, watchdog escalates to email + auto-pauses non-critical daemons". | watchdog config + Discord webhook | S | none |
| 39 | [MUST] | Backup of D1 + Supabase to HF Hub | D1 has no point-in-time recovery on free tier; nightly dump → encrypt → push to private HF dataset. Supabase has 7d, but want 90d archive. | new daemon + GPG key | M | HF token + GPG key |
| 40 | [MUST] | Disaster-recovery drill quarterly | Backups exist but never restored; quarterly cron creates fresh D1 from backup and runs assertion suite. | new GH Action + drill script | L | backups (#39) |
| 41 | [MUST] | LLM provider fallback testing | 12-provider chain assumed working; weekly canary forces each to fail (mock 429) and confirms next-in-chain succeeds. | new test in harvest CI | M | provider chain config |
| 42 | [NICE] | Distributed tracing UI on /dash | Trace ID exists but reading raw is painful; render gantt on /dash from `traces` table. | /dash extension | M | trace ID (#34) |
| 43 | [NICE] | Anomaly detection on metrics | space_health latency drift slow → caught by EWMA + 3σ rule, not threshold. | metrics worker + D1 view | M | metrics history ≥7d |
| 44 | [NICE] | Public status page | Trust signal for future users; CF Pages site reading from space_health + SLO state. | new CF Pages site | M | SLO (#35) |
| 45 | [NICE] | PagerDuty-equivalent escalation tree | Single Discord channel for everything → critical alerts buried; tiered routing: critical→Discord+email+SMS, warning→Discord, info→log only. | watchdog router | M | runbooks (#37) |
| 46 | [NICE] | Time-travel debugging via decision replay | agent_decisions captures inputs+outputs but no replay tool; CLI takes decision_id and re-runs in isolated sandbox to reproduce bug. | new CLI in harvest repo | L | rubric versioning (#4) |
| 47 | [NICE] | Self-healing playbook execution | self-heal currently restarts; extend with allowlisted remediations (clear KV cache, rotate provider, reseed task) chosen by symptom. | self-heal config DSL | L | runbooks (#37) |
| 48 | [NICE] | Dependency-on-dependency dashboard | When CF Worker AI is degraded, which daemons are blocked? Static graph rendered + live status overlay. | /dash extension | M | structured logs (#33) |

**Subtotal Cat 3: MUST=8, NICE=8**

---

## Category 4 — Training data + model

| # | Tag | Name | Why | Touched stack | Complexity | Depends-on |
|---|-----|------|-----|---------------|-----------:|------------|
| 49 | [MUST] | DPO pair quality scoring | agent-decisions-to-pairs emits triples but not all are useful; scorer ranks by reviewer-disagreement-magnitude × outcome-clarity, retain top 70%. | new daemon + HF dataset filter | M | rubric versioning (#4) |
| 50 | [MUST] | Held-out eval suite versioned | No frozen eval; freeze 500 prompts (50 per agent role) in `eval/v1/` repo, baseline base model perf, gate adapters. | new repo `axentx/evals` | L | none |
| 51 | [MUST] | Train/val/test split deterministic | Random split each run leaks test → train across resumes; hash(prompt) % 100 with `<5 test, 5-10 val, ≥10 train`. | trainer V20 patch | S | none |
| 52 | [MUST] | PII scrubber on training data | agent inputs sometimes contain Discord usernames, repo paths, Supabase IDs; regex+entity scrubber before HF push. | new daemon stage | M | none |
| 53 | [MUST] | License audit on training data sources | Code copied from third-party libs into pairs may carry GPL; license sniffer (pkg + path) flags or excludes. | new daemon stage | L | none |
| 54 | [MUST] | Adapter version metadata card | Published adapters have no model card; auto-generated YAML (eval scores, training params, dataset hash, base model SHA). | trainer post-step | S | held-out eval (#50) |
| 55 | [MUST] | Reward model bootstrap from triples | Verdict triples are reviewer's binary good/bad; train tiny reward model (BERT-like) for fast filtering before DPO. | new Kaggle notebook | L | DPO quality scoring (#49) |
| 56 | [NICE] | Active-learning loop | Reward model picks lowest-confidence pairs for human review; you label 20/week, model improves. | new Discord bot command | L | reward model (#55) |
| 57 | [NICE] | Adapter merging (TIES/DARE) | Per-project adapters fragment; weekly merge into "axentx-generalist" via TIES-merging recipe. | new Kaggle notebook | M | held-out eval (#50) |
| 58 | [NICE] | Quantized adapter benchmarks | GGUF Q4_K_M vs Q5 vs FP16 latency/quality table per release; informs Hermes Space LOW_MEM choice. | new Kaggle notebook | M | held-out eval (#50) |
| 59 | [NICE] | Synthetic hard-negative miner | DPO needs convincing-but-wrong rejects; mine from rejected reviewer attempts that scored close to threshold. | trainer preprocess | M | rubric versioning (#4) |
| 60 | [NICE] | Curriculum-by-difficulty schedule | Easy pairs first, hard last (loss-on-base-model proxy); literature shows +1-3% on small-data regimes. | trainer V20 patch | M | none |
| 61 | [NICE] | Multi-stack mixture experiment | Train one adapter per language (TS/Python/SQL) vs one mixed; report perplexity gap. | new Kaggle notebook | L | held-out eval (#50) |
| 62 | [NICE] | Direct-from-D1 streaming dataloader | Currently dump D1 → HF → Kaggle; cut middle step with Hyperdrive stream into Kaggle. | trainer rewrite | L | Hyperdrive validated |

**Subtotal Cat 4: MUST=7, NICE=7**

---

## Category 5 — Cost / efficiency

| # | Tag | Name | Why | Touched stack | Complexity | Depends-on |
|---|-----|------|-----|---------------|-----------:|------------|
| 63 | [MUST] | Cost dashboard pulling all sources | Zero visibility into CF/GCP/Supabase/HF/Kaggle spend; dashboard with daily fetch via each provider's billing API + per-feature attribution. | new Worker route + 5 fetchers | L | provider API tokens |
| 64 | [MUST] | Token budget per agent role | Reviewer occasionally consumes 30k context for trivial review; per-role daily token cap with circuit breaker. | LLM gateway middleware | M | structured logs (#33) |
| 65 | [MUST] | Provider routing by cost-quality Pareto | All requests go round-robin; route based on (cost_per_1k_token, quality_score) per task type. Reviewer needs quality, summarizer doesn't. | LLM gateway routing rules | M | provider chain |
| 66 | [MUST] | Dead daemon hibernation | Some daemons run 24/7 but only do work hourly; sleep until next cron tick reduces e2-micro CPU 40%. | systemd ExecStart change + watchdog | S | none |
| 67 | [MUST] | KV cache hit-rate tracking | 60s KV cache assumed effective; instrument hit/miss + bytes-saved → tune TTL per route. | cursor Worker middleware | S | structured logs (#33) |
| 68 | [NICE] | Adaptive provider TTL | Free-tier providers exhaust at different times; track per-provider exhaustion histogram and route around. | LLM gateway | M | provider routing (#65) |
| 69 | [NICE] | Compression on HF dataset uploads | 9.56TB of mostly-text; zstd-19 nightly recompress could halve storage (HF allows). | new daemon + HF API | M | none |
| 70 | [NICE] | D1 read replica via KV mirror | Hot read paths repeatedly hit D1; mirror to KV for paths with high read:write ratio (audit, knowledge). | Worker middleware | M | hit-rate tracking (#67) |
| 71 | [NICE] | Spot Kaggle session reuse | Each training run cold-starts Kaggle; persistent dataset for venv + model cache reduces 8min boot to 1min. | trainer init | M | none |
| 72 | [NICE] | Workers AI vs external LLM auto-decide | Workers AI has free quota but lower quality on hard prompts; auto-classifier picks per-request. | LLM gateway | L | provider routing (#65) |

**Subtotal Cat 5: MUST=5, NICE=5**

---

## Category 6 — Security / compliance

| # | Tag | Name | Why | Touched stack | Complexity | Depends-on |
|---|-----|------|-----|---------------|-----------:|------------|
| 73 | [MUST] | Secret rotation calendar | HF token, Supabase anon, CF API key, Kaggle key, Discord webhook — none rotated since creation. Quarterly rotation cron + automated update across daemons. | new daemon + Supabase secret table | L | secret store consolidation |
| 74 | [MUST] | Secret scanner pre-push | Auto-bot commits could leak secrets; gitleaks pre-receive hook on harvest repo + axentx repos. | GH Action + commit-agent | S | none |
| 75 | [MUST] | Single secret store (1Password CLI or Doppler) | Secrets sprinkled in .env, GH secrets, CF Vars, Supabase env; consolidate with single source-of-truth + sync daemon. | new sync daemon | L | none |
| 76 | [MUST] | Cursor service rate-limit per IP | /cursor accepts unlimited requests per IP; CF rate-limit binding 60req/min default + override per known. | Worker config | S | none |
| 77 | [MUST] | Audit log immutability | audit table is regular D1 row, mutable by anyone with token; mirror to append-only HF dataset hourly. | audit middleware + new daemon | M | structured logs (#33) |
| 78 | [MUST] | Threat model document for solution | No documented STRIDE analysis; one-time exercise + biannual refresh, output committed. | new doc in harvest repo | M | none |
| 79 | [NICE] | SBOM per axentx project | Supply chain risk untracked; CycloneDX SBOM generated per PR + diff'd for new deps. | GH Action | S | none |
| 80 | [NICE] | Dependency CVE monitoring | osv-scanner nightly + alert if CVSS≥7 in transitive dep. | new GH Action | S | SBOM (#79) |
| 81 | [NICE] | Encrypted backups at rest | Backups (#39) are GPG-encrypted but recovery key stored alongside; move recovery key to separate HW (Yubikey) with documented break-glass. | docs + key ceremony | M | backups (#39) |
| 82 | [NICE] | Workers binding least-privilege audit | Cursor Worker may have D1 write where read suffices; quarterly review of bindings. | manual + CLI script | S | none |

**Subtotal Cat 6: MUST=6, NICE=4**

---

## Category 7 — Developer experience (for future engineers)

| # | Tag | Name | Why | Touched stack | Complexity | Depends-on |
|---|-----|------|-----|---------------|-----------:|------------|
| 83 | [MUST] | One-command local dev (`make dev`) | New engineer faces 22 daemons with no bring-up doc; `make dev` boots minimal subset (worker + 1 hermes + 1 dev) with seeded D1 fixture. | new Makefile + docker-compose | L | none |
| 84 | [MUST] | Architecture diagram in repo | Mental model lives only in your head; auto-generated C4 diagrams (Container + Component) committed. | drawio + GH Action render | M | none |
| 85 | [MUST] | CONTRIBUTING.md with PR checklist | No onboarding doc; templates for bug, feature, runbook. | new file | S | none |
| 86 | [MUST] | API reference for cursor service | /cursor undocumented; OpenAPI spec + Redoc on Pages. | API docs (#13) generalized | S | none |
| 87 | [NICE] | devcontainer.json for VS Code | New laptop setup = 1h; devcontainer with all tools preinstalled cuts to 5min. | new file | S | local dev (#83) |
| 88 | [NICE] | Postman/Bruno collection | Manual cursor API testing painful; checked-in collection with auth examples. | new dir | S | API reference (#86) |
| 89 | [NICE] | Agent role README per daemon | Each daemon has prompt but no human-readable contract (inputs, outputs, idempotency, retries); one-page README per role. | docs in harvest | M | none |
| 90 | [NICE] | "Day in the life" walkthrough video | Loom-style screencast of one full feature trip (idea→commit→eval); future engineers grok in 10min vs days. | manual recording | S | local dev (#83) |

**Subtotal Cat 7: MUST=4, NICE=4**

---

## Category 8 — Business / monetization

| # | Tag | Name | Why | Touched stack | Complexity | Depends-on |
|---|-----|------|-----|---------------|-----------:|------------|
| 91 | [MUST] | Self-serve API with metered billing | Cursor service runs but has no payment path; Stripe + CF Worker meter per request, free 100/day, paid tiers. | new Worker route + Stripe | L | rate limit (#76) |
| 92 | [MUST] | Public landing page on Pages | CF Pages provisioned but empty; one-page explainer + waitlist signup feeds Mailchimp. | Pages site | M | none |
| 93 | [MUST] | Customer support inbox to Discord | No support email; `support@axentx.com` → Cloudflare Email Routing → Discord channel + KV ticket store. | Email Routing + Worker | M | discord-bot |
| 94 | [MUST] | Terms of Service + Privacy Policy | Required to take payment; templates from Termly/iubenda customized + legal review. | Pages site | S | landing page (#92) |
| 95 | [NICE] | Referral program | Existing users invite others for free quota bumps; `referral_code` table + Worker. | new Worker route | M | metered billing (#91) |
| 96 | [NICE] | Usage-based upgrade nudges | When user hits 80% of free tier, in-product banner offers upgrade. | dash + cursor middleware | S | metered billing (#91) |
| 97 | [NICE] | Multi-tenant org accounts | Single users only today; orgs with shared quota + RBAC. | D1 schema + auth | L | metered billing (#91) |
| 98 | [NICE] | Affiliate tracking pixel | Marketing wants to attribute landing-page conversions; UTM + first-party pixel via Worker. | landing + Worker | S | landing page (#92) |
| 99 | [NICE] | Public pricing page A/B harness | Pricing page conversion uncertain; integrate with #29 pricing experiment harness. | landing site | S | pricing harness (#29) |
| 100 | [NICE] | Annual plan discount + invoicing | Stripe subscriptions support annual; offer 20% off + invoice generation. | Stripe config | M | metered billing (#91) |

**Subtotal Cat 8: MUST=3, NICE=7**

---

## Totals (verification)

| Category | MUST | NICE |
|---|---:|---:|
| 1. Software-development | 10 | 10 |
| 2. Product-discovery | 6 | 6 |
| 3. Operations / observability | 8 | 8 |
| 4. Training data + model | 7 | 7 |
| 5. Cost / efficiency | 5 | 5 |
| 6. Security / compliance | 6 | 4 |
| 7. Developer experience | 4 | 4 |
| 8. Business / monetization | 3 | 7 |
| **Total** | **50** | **50** |

---

## Tuning recommendations per existing agent

Each agent gets 1–3 specific, actionable improvements grounded in current behavior. Format: `Issue` → `Tune`.

### research (×3 fanout)
1. **Issue**: Three parallel branches sometimes converge on same insight, wasting 2/3 tokens. **Tune**: After 30s, pre-emptively share short summary across branches; subsequent reasoning must explicitly diverge or self-terminate.
2. **Issue**: Search results vary by run, breaking reproducibility. **Tune**: Cache search results for 24h in KV keyed by query-hash; replay deterministically when re-running same idea.
3. **Issue**: Results lack source-quality scoring. **Tune**: Score each cited URL on (domain authority, recency, primary-vs-secondary) before bd reads.

### bd (business-development)
1. **Issue**: Output sometimes restates research without new synthesis. **Tune**: Reject any bd output where edit-distance to research summaries < 0.4; force re-roll with explicit "synthesize, don't summarize" instruction.
2. **Issue**: TAM/SAM/SOM hallucinated. **Tune**: Block claim unless backed by ≥1 source from research with `<URL>::<quote>` proof embedded.
3. **Issue**: Ignores prior bd retros. **Tune**: Inject last 5 retro outcomes (which discoveries shipped, killed) into prompt context (depends on #30).

### design-thinking
1. **Issue**: Generates personas with implausible combinations (CFO who codes Rust). **Tune**: Validate persona consistency via small-LLM critic + library lookup (#27); reject and re-roll if critic flags.
2. **Issue**: Empathy-map bullets too generic. **Tune**: Require ≥1 verbatim quote from customer-discovery (#23) or research-cited interview when available, else flag as low-evidence.

### business
1. **Issue**: Pricing recommendations rarely tested. **Tune**: Output must include explicit hypothesis + measurement plan that pricing harness (#29) can consume.
2. **Issue**: Unit economics computed but not stress-tested. **Tune**: Auto-run sensitivity table (CAC ±30%, LTV ±50%, churn ±10pp); reject plan if any cell breaks even.

### marketing
1. **Issue**: Invents stats ("50% faster"). **Tune**: Implement claim fact-check (#26); reject any quantitative claim without bd-evidence ID.
2. **Issue**: Tone inconsistent across same product. **Tune**: Pin brand voice card per axentx project; reviewer compares cosine-sim of new copy vs voice corpus, requires ≥0.7.
3. **Issue**: Headlines generic. **Tune**: Force generation of ≥5 headlines, then critic ranks by specificity (named outcome + numeric or named target user); pick top-2.

### prd
1. **Issue**: PRDs ship without kill criteria. **Tune**: Apply #25 (kill criteria template); reviewer blocks PR if missing.
2. **Issue**: Acceptance criteria written as wishes ("works fast"). **Tune**: Require Given/When/Then format, parsed by reviewer, rejected if any AC lacks measurable threshold.
3. **Issue**: Scope creep — PRDs grow during refinement. **Tune**: Lock initial-scope hash; subsequent edits require explicit "scope-change: yes" flag in commit + re-review.

### dev
1. **Issue**: Loads global knowledge index regardless of project; bloats context. **Tune**: Apply #1 per-project index loader.
2. **Issue**: Sometimes writes tests that mirror implementation (test passes if implementation passes, even if both wrong). **Tune**: For `*-core` packages enable test-first flag (#5) so failing test must precede impl.
3. **Issue**: Diff size occasionally 800+ LOC. **Tune**: Apply #7 cap with auto-split; refuse to commit single diff > 250 LOC unless flagged `large-diff: justified`.

### qa
1. **Issue**: Coverage % reported but not gating. **Tune**: Per-package coverage threshold in `package.json`; CI fails below threshold; track delta per PR.
2. **Issue**: Doesn't differentiate flake from real fail. **Tune**: Auto-rerun failed tests once; if pass→fail→pass pattern, mark `flaky` and open issue but don't block PR; track flake-rate per test.
3. **Issue**: No performance regression check. **Tune**: For 10 critical paths, add benchmark harness (Vitest bench / pytest-benchmark) gated on ≤10% regression vs main.

### reviewer
1. **Issue**: Sometimes spends an attempt on lint errors. **Tune**: Apply #6 lint-gate before reviewer ever sees diff.
2. **Issue**: Three-attempt cap can pass low-quality work when first two attempts crash. **Tune**: Distinguish "crash" from "fail review"; only failed-review attempts count toward cap, crashes get retry-with-backoff.
3. **Issue**: Rubric drifts silently. **Tune**: Apply #4 rubric versioning + commit rubric file alongside reviewer prompt; require explicit version bump.

### commit
1. **Issue**: Mixed commit message styles. **Tune**: Apply #10 conventional commits + commitlint; reject commit if non-conformant.
2. **Issue**: Auto-bot commits push to main directly. **Tune**: Apply #3 branch protection; auto-bot opens PR, auto-merges if all checks pass and reviewer-bot label present.
3. **Issue**: Commits include trace-irrelevant whitespace changes. **Tune**: Pre-commit `git add -p` analog limited to logical hunks; whitespace-only commits squashed into nearest logical commit.

### pm
1. **Issue**: pm doesn't see cross-pipeline dependencies. **Tune**: Inject DAG of in-flight discovery_id chains (depends on #22 traceability) so pm can reason about bottlenecks.
2. **Issue**: pm summaries are flat narrative. **Tune**: Output structured `{shipped, in-flight, blocked, killed}` JSON consumed by /dash.

### watchdog
1. **Issue**: Restarts daemon without root-cause classification. **Tune**: Before restart, capture last 200 lines of structured logs (#33) + tag with symptom; restart loop > 3 in 1h escalates.
2. **Issue**: Same alert fires repeatedly. **Tune**: Alert dedup window per `alert.code` (15min default); snooze if matching unresolved alert exists.

### self-heal
1. **Issue**: Allowed actions hardcoded. **Tune**: Promote to DSL (#47) — declarative `if symptom matches, run remediation X` with explicit safety rails.
2. **Issue**: No record of healed-by-self vs healed-by-Ashira. **Tune**: Tag every recovered alert with `recovery.actor` (self|human|timed-out) for SLO accuracy.

### discord-bot
1. **Issue**: Message-rate spike during burst incidents → Discord ratelimits. **Tune**: Token-bucket rate limiter (5/sec) with priority queue (critical jumps); batch low-priority into 1 msg/min.
2. **Issue**: Thai/English routing imperfect. **Tune**: After current detection, log mis-classifications via Discord reaction emoji (✅/❌) to a feedback queue; weekly retrain on ≥50 corrections.

### scheduler (hermes-scheduler)
1. **Issue**: Some 168 cron jobs overlap, causing brief work-queue contention. **Tune**: Add `priority` column to scheduled tasks; dispatcher picks highest-priority first within same minute slot.
2. **Issue**: No backfill on missed runs. **Tune**: On startup, read `last_run_at` per job; if older than `2 × interval`, optionally backfill once with `backfill: true` flag.

### worker (hermes-worker ×5)
1. **Issue**: Five replicas claim from same queue with `FOR UPDATE SKIP LOCKED` but occasionally one worker handles 80% of load. **Tune**: Add per-worker max-claim-per-minute to enforce fair distribution; spillover triggers more aggressive sleep-back on idle workers.
2. **Issue**: Worker holds claim during long LLM call; if worker dies, claim takes lease-timeout to release. **Tune**: Periodic heartbeat (every 30s) extends claim only while worker is alive; lease 60s instead of 600s reduces stuck-task time.
3. **Issue**: No bulkhead between agent roles; reviewer storm starves dev. **Tune**: Per-role concurrency cap (e.g. max 2 reviewers, max 1 dev per worker) to prevent single role saturating CPU on e2-micro.

---

## Cross-cutting prerequisites (build these first to unblock most features)

| Prereq | Unblocks features | Why first |
|---|---|---|
| Structured JSON logs (#33) | #34, #35, #43, #48, #64, #67 | Ops + cost work all need uniform log schema |
| Trace ID propagation (#34) | #35, #36, #42, #46 | End-to-end debugging foundation |
| Held-out eval suite (#50) | #2, #11, #54, #57, #58, #61 | Cannot gate or iterate adapters without |
| Per-project knowledge-index (#1) | #14, #20 | Reduces context bloat across all dev work |
| Branch protection (#3) | #8, #74 | Foundation for any auto-bot quality gate |
| Cost dashboard (#63) | informs prioritization of every other feature | Cannot make trade-offs blind |

**Recommended sequencing**: ship the 6 prerequisites in week 1-2, then unlock the rest in priority order driven by cost dashboard signal.

---

## Out-of-scope (intentionally excluded)

- Mobile apps (no current product needs them)
- Kubernetes migration (e2-micro adequate, premature)
- Self-hosted GitLab / Gitea (GitHub free tier fine)
- Multi-region deployment (single user, single region OK)
- Custom hardware / colo (cloud free tiers cover need)
- Custom LLM training from scratch (PEFT adequate)
- Real-time collaboration features (Workio scoped to async)

---

## Acceptance criteria + implementation hints (top-50 MUST features)

For each MUST, explicit "done when" + concrete first-step. Use this as sprint-ready checklist.

### Cat 1 — Software-development MUSTs

**#1 Per-project knowledge-index**
- Done when: dev-agent prompt size measured per-project shows ≥40% reduction vs current global; D1 table `project_knowledge_index(project_slug, pattern_id, snippet, tags, embedding)` populated for all 6 axentx repos.
- First step: split current `knowledge_index.md` by `[[wikilink]]` references mentioning project names; seed table.
- Risk: orphan patterns (no project assignment) — assign to `_global`.

**#2 Adapter eval gate before HF Hub publish**
- Done when: trainer V20+ runs `eval_adapter.py` post-train; if `held_out_perplexity` worse than baseline OR `win_rate_vs_base < 0.53` → publish blocked, Discord alert.
- First step: pick 200 prompts from `agent_decisions` with high `verdict.confidence`, freeze as `eval/v1/holdout.jsonl` in new repo.
- Risk: held-out leaks into training — use deterministic split (#51).

**#3 Branch protection on all 6 axentx repos**
- Done when: `gh api -X PUT repos/axentx/<repo>/branches/main/protection` applied to all 6 with required-reviews=1, required-status-checks=[lint,typecheck,test], dismiss-stale-reviews=true.
- First step: write `scripts/apply_branch_protection.sh` reading repo list from config.
- Risk: auto-bot pushes blocked — pair with bot label exception in #8.

**#4 Reviewer rubric versioning**
- Done when: every row in `agent_decisions` for role=reviewer has non-null `rubric_version`; rubric file pinned at `prompts/reviewer/v<N>.md` referenced by version.
- First step: add column, backfill existing rows with `v0_pre_versioning`, change reviewer prompt loader to read pinned version.

**#5 Test-first dev mode flag**
- Done when: env `TEST_FIRST_PROJECTS=Costinel,Vanguard` honored; for those, dev-agent must commit failing test first, then implementation, with separate commits.
- First step: add gate in dev-agent state machine; reject single-commit PRs for allowlisted projects.

**#6 Lint+typecheck gate before reviewer**
- Done when: pipeline runs `npm run lint && npm run typecheck` (or Python equivalent) per project; non-zero exit → reviewer not invoked, dev-agent retries.
- First step: convention `package.json` script names `lint`, `typecheck`, `test`; add to CONTRIBUTING.md.

**#7 Diff size cap with auto-split**
- Done when: dev-agent splits any single-PR change >250 LOC into N≤4 sequential PRs each ≤250 LOC; reviewer reviews each independently.
- First step: pre-commit script counts net LOC; if exceeds, fail commit with hint.
- Risk: arbitrary split harms review — heuristic: split at file boundaries first, function boundaries second.

**#8 Codeowners auto-routing per project**
- Done when: `.github/CODEOWNERS` per repo maps `*.ts → @reviewer-ts`, `*.py → @reviewer-py`, `*.sql → @reviewer-sql`; reviewer agent loads role-specific prompt addendum.
- First step: extract stack-aware sections from current reviewer prompt into named files.

**#9 Spec-driven scaffold from PRD**
- Done when: PRD agent emits triple `(spec.md, plan.md, checklist.md)` written to `sessions/<discovery_id>/`; dev-agent reads triple as starting context.
- First step: PRD prompt addendum with templates; post-processor splits output.

**#10 Conventional Commits enforced**
- Done when: commitlint config in repo root validates every commit; auto-bot commit messages match `<type>(<scope>): <subject>` 100%.
- First step: install `@commitlint/cli`, add config, wire into commit-agent.

### Cat 2 — Product-discovery MUSTs

**#21 Idea deduplication before research**
- Done when: research-agent first call queries Vectorize over last 30d of bd outputs; if cosine ≥ 0.85 → skip with rationale logged.
- First step: ensure bd outputs embedded into Vectorize namespace `bd_history`.

**#22 bd→PRD traceability IDs**
- Done when: every row in `agent_decisions` for stages bd through prd has `discovery_id` matching parent; /dash can render full trace.
- First step: add column + UUID generator in bd-agent entrypoint.

**#23 Customer-discovery interview loop**
- Done when: weekly Discord poll posted Friday 9am; ≥1 response required before PRD agent triggers; responses stored in `interviews` table.
- First step: define poll template (5 questions max, 2 multiple-choice + 3 free-text); discord-bot scheduler entry.

**#24 Competitive landscape auto-snapshot**
- Done when: weekly cron scrapes top-5 competitor URLs per vertical; markdown digest embedded into `competitive` Vectorize namespace; bd-agent prompt includes top-3 retrieved.
- First step: per-vertical YAML config of competitor URLs; CF Browser Rendering POC.
- Risk: CF Browser Rendering quota — measure cost before scaling beyond 5 verticals.

**#25 Kill criteria in PRD template**
- Done when: every PRD output includes section `## Kill Criteria` with ≥3 measurable conditions; reviewer rejects PRD without it.
- First step: PRD template + reviewer regex check.

**#26 Marketing claims fact-check**
- Done when: marketing-agent output passes fact-check sub-agent; any quantitative claim (regex `\d+%|\d+x`) has matching `bd_evidence_id`; reviewer blocks if missing.
- First step: claim extractor + Vectorize lookup over `bd_history`.

### Cat 3 — Operations MUSTs

**#33 Structured JSON logs**
- Done when: all 22 daemons emit JSON to stdout with schema `{ts, level, daemon, trace_id, event, fields...}`; logger lib shared across Python+TS code.
- First step: write `axentx-logger` package (Python+TS), update one daemon as proof, propagate.

**#34 Trace ID propagation**
- Done when: scheduler generates UUID per cron tick; threaded via Supabase `tasks.trace_id`, Worker `X-Trace-Id` header, git commit trailer `Trace-Id:`.
- First step: schema migration; logger lib reads from context var.

**#35 SLO per pipeline stage**
- Done when: SLO YAML defines targets; D1 view `slo_burn_rate` computes 1h+6h+24h windows; Discord alert fires at 2x burn rate over 1h.
- First step: write SLO doc; pick 5 most-critical stages first.

**#36 Synthetic canary every 15 min**
- Done when: cron submits known prompt → measures full path latency + correctness → records in `canary_runs`; alert if 3 consecutive fails or p95 > SLO.
- First step: pick deterministic prompt (e.g. "rename function `foo` to `bar` in repo X").

**#37 Runbook per known incident pattern**
- Done when: every alert payload includes `runbook_url`; ≥10 runbooks committed covering top alert codes by frequency.
- First step: dump 30d of alerts, group by code, write runbooks for top-10.

**#39 Backup of D1 + Supabase to HF Hub**
- Done when: nightly cron exports D1 (`wrangler d1 export`) + Supabase (`pg_dump`) → GPG encrypts → pushes to private HF dataset `axentx/backups`; retention 90d.
- First step: GPG keypair ceremony; store private key in 1Password (depends on #75).

**#40 Disaster-recovery drill quarterly**
- Done when: cron creates ephemeral D1 from latest backup → runs assertion suite (row counts, schema integrity, sample query) → publishes report.
- First step: write assertion suite (10 critical invariants); first manual drill before automating.

**#41 LLM provider fallback testing**
- Done when: per-provider mock-429 test runs weekly; verifies next-in-chain succeeds within 3s; failure → Discord alert.
- First step: add `MOCK_429_PROVIDERS` env var to LLM gateway for test mode.

### Cat 4 — Training data MUSTs

**#49 DPO pair quality scoring**
- Done when: scorer assigns `quality_score` to every triple; HF dataset uploads filter to top-70%; metric tracked weekly.
- First step: define scoring formula = `α·reviewer_disagreement + β·outcome_clarity + γ·rubric_version_recency`; tune α/β/γ on labeled sample.

**#50 Held-out eval suite versioned**
- Done when: 500 prompts (50/role × 10 roles) frozen in `eval/v1/`; baseline scored on base model; gate adapters require ≥3% lift.
- First step: stratified sample from `agent_decisions` weighted by role; manual review of 50 to ensure quality.

**#51 Train/val/test split deterministic**
- Done when: split uses `int(hashlib.sha256(prompt.encode()).hexdigest(), 16) % 100` → bucket assignment; reproducible across runs.
- First step: trainer V20 patch; smoke-test that re-running gives identical splits.

**#52 PII scrubber on training data**
- Done when: scrubber removes Discord usernames (regex `@[\w-]+`), Supabase IDs (UUIDs), email addresses, repo paths matching `/Users/Ashira/`; entity NER pass for names.
- First step: rule list + spaCy en_core_web_sm pass.

**#53 License audit on training data sources**
- Done when: each pair tagged with detected license of source files (if from a code repo); GPL-tainted excluded from public dataset.
- First step: license sniffer using SPDX patterns + path-based fallback.

**#54 Adapter version metadata card**
- Done when: every adapter pushed to HF has auto-generated `README.md` with eval scores, training params, dataset hash, base model SHA, date.
- First step: trainer post-step writes Markdown card from training config + eval output.

**#55 Reward model bootstrap from triples**
- Done when: small (≤200M param) model trained on verdict triples reaches ≥0.75 accuracy on held-out reviewer judgments.
- First step: dataset prep from `agent_decisions` reviewer rows; Kaggle notebook with DistilBERT base.

### Cat 5 — Cost MUSTs

**#63 Cost dashboard pulling all sources**
- Done when: /dash shows current month spend per provider (CF, GCP, Supabase, HF, Kaggle, Stripe inbound) with per-feature attribution where possible.
- First step: provider API tokens in 1Password; one-shot fetch per provider working manually before automating.

**#64 Token budget per agent role**
- Done when: per-role daily budget enforced at LLM gateway; circuit breaker rejects with rationale + Discord notice when exceeded.
- First step: extract usage from `audit` table; set initial budgets at p95 of last 30d × 1.2.

**#65 Provider routing by cost-quality Pareto**
- Done when: routing matrix `{role × task_type → provider_chain_priority}` defined; gateway picks per-request.
- First step: classify task_type from prompt prefix (already mostly structured by role).

**#66 Dead daemon hibernation**
- Done when: 5+ daemons identified as cron-driven-only converted to systemd `OnCalendar=` timer-units instead of always-running services; e2-micro CPU graph shows ≥30% drop.
- First step: audit current daemons; categorize event-driven vs polling.

**#67 KV cache hit-rate tracking**
- Done when: every KV read logs `cache_status: hit|miss` with route + ttl; weekly digest in /dash shows hit-rate per route.
- First step: cursor-Worker middleware update; backfill not needed.

### Cat 6 — Security MUSTs

**#73 Secret rotation calendar**
- Done when: every secret has `rotation_due_at` in store; daily check; <7d → Discord nudge; <0d → block daemons that use it from starting.
- First step: inventory current secrets (manual); set initial rotation calendar.

**#74 Secret scanner pre-push**
- Done when: gitleaks runs in pre-receive hook (or GH Action) on all axentx + harvest repos; high-entropy strings + known patterns blocked.
- First step: GH Action (cheaper than self-hosted Git server); add as required check.

**#75 Single secret store**
- Done when: all secrets sourced from 1Password CLI (or Doppler); `.env` files gitignored AND emptied; sync daemon pushes to runtime targets (CF Vars, GCP env, Supabase env).
- First step: pick tool (1Password CLI free for personal); inventory + migrate one daemon as proof.

**#76 Cursor service rate-limit per IP**
- Done when: CF rate-limit binding active; default 60req/min/IP; metrics exposed.
- First step: enable in Worker dashboard; document override mechanism.

**#77 Audit log immutability**
- Done when: hourly dump of `audit` table to append-only HF dataset (filename includes hour-bucket, never overwritten); attempted mutation in source D1 detected by hash chain.
- First step: hash-chain column in audit table; daemon writes new hash on insert.

**#78 Threat model document**
- Done when: STRIDE doc covering Spoofing/Tampering/Repudiation/Info-disclosure/DoS/Elevation per major component; review checklist in CONTRIBUTING.
- First step: 2h focused exercise with components: Cursor Worker, agent pipeline, Supabase queue, HF data, Discord bot.

### Cat 7 — DX MUSTs

**#83 One-command local dev**
- Done when: `make dev` boots minimal stack (1 worker + 1 hermes + 1 dev-agent + seeded D1 fixture + mock Supabase); ready in ≤2min on fresh checkout.
- First step: docker-compose with services; .env.example; D1 seed SQL.

**#84 Architecture diagram in repo**
- Done when: `docs/architecture/` has C4 Context + Container + Component diagrams as drawio source + rendered PNG; CI re-renders on source change.
- First step: draft Container diagram first (most useful); use deploy-on-aws skill for AWS-side bits if relevant.

**#85 CONTRIBUTING.md with PR checklist**
- Done when: file at repo root with sections: setup, branching, commit style, PR template, runbook authoring, agent contract authoring.
- First step: skeleton + iterate.

**#86 API reference for cursor service**
- Done when: OpenAPI 3.1 spec checked into repo; Redoc HTML rendered to CF Pages route `/docs/cursor`; covers all endpoints with auth examples.
- First step: extract spec from current Worker handlers manually first, then automate via decorator.

### Cat 8 — Business MUSTs

**#91 Self-serve API with metered billing**
- Done when: signup flow → API key issued → CF Worker meters per request via Stripe usage records → invoice generated monthly; free tier 100/day.
- First step: Stripe account + product + price ID; Worker middleware to check key + record usage.

**#92 Public landing page on Pages**
- Done when: `axentx.com` resolves to CF Pages site; one-page explainer + waitlist signup → Mailchimp (or substack newsletter); SSL via CF.
- First step: copy from marketing-agent output; minimal Tailwind site.

**#93 Customer support inbox to Discord**
- Done when: `support@axentx.com` → CF Email Routing → Worker parser → Discord channel + KV ticket entry; reply-from-Discord syncs back to email thread.
- First step: enable CF Email Routing; basic forwarder Worker.

**#94 Terms of Service + Privacy Policy**
- Done when: ToS + Privacy linked from landing page footer; cover data processing, retention, AI-generated-content disclaimer; reviewed by lawyer (paid 1h consult, expense ~$300).
- First step: Termly/iubenda template generated; mark sections needing legal review.

---

## Acceptance criteria + implementation hints (top NICE features by leverage)

NICE-to-haves with non-trivial implementation detail. Use when MUSTs are saturated.

**#11 Mutation testing on critical packages**
- Done when: Stryker/mutmut runs nightly on `*-core`; mutation score ≥60%; HF-published adapters require ≥60% to ship.
- First step: pick 1 core package as pilot; benchmark current score.

**#13 Auto-generated API docs from OpenAPI**
- Done when: every axentx repo with API has `openapi.yaml` checked in + Redoc rendered to Pages on PR merge.
- First step: cursor service first, then template for others.

**#19 Automatic dependency update PRs**
- Done when: weekly Worker reads each repo's `package.json` + `requirements.txt`, opens PR with patch+minor bumps; CI gates auto-merge.
- First step: outdated-detector script; run manually first week.

**#27 Persona library with realistic constraints**
- Done when: 15 personas committed to `personas/` with role/budget/pain-points/disqualifiers; design-thinking-agent picks from library by vertical.
- First step: derive from any existing customer interviews; otherwise synth + manual review.

**#28 Wizard-of-Oz prototype generator**
- Done when: marketing-agent can `generate_wizard_prototype(prd_id)` → CF Pages site live in <10min with form + analytics.
- First step: template repo with Tailwind + form-to-D1 handler.

**#29 Pricing experiment harness**
- Done when: feature flag rotates 3 prices per user-cohort; 7-day result table picks winner by signed-up-conversion × revenue.
- First step: D1 table `pricing_experiments`; basic A/B middleware.

**#42 Distributed tracing UI on /dash**
- Done when: /dash route `/trace/<trace_id>` renders Gantt; clickable spans show input/output snippets.
- First step: D1 view; D3 Gantt component on Worker-rendered page.

**#43 Anomaly detection on metrics**
- Done when: EWMA + 3σ rule applied to space_health latency, work-queue depth, error-rate per daemon; alert on persistent breach (5min window).
- First step: implement EWMA in Worker; tune σ on historical data.

**#44 Public status page**
- Done when: `status.axentx.com` shows current uptime per service + last 90d; powered by space_health + SLO state.
- First step: minimal Pages site; consume D1 view.

**#46 Time-travel debugging via decision replay**
- Done when: `replay-decision <id>` CLI re-runs agent in sandbox with same inputs; outputs diff vs original.
- First step: snapshot inputs in D1 already; sandbox runner is the new bit.

**#56 Active-learning loop**
- Done when: weekly Discord bot sends 20 lowest-confidence pairs; replies (good/bad reaction) flow back into reward model retrain.
- First step: confidence scorer (entropy on reward); poll template.

**#57 Adapter merging (TIES/DARE)**
- Done when: weekly Kaggle notebook merges per-project adapters; merged adapter eval ≥ best individual.
- First step: pick TIES-merging library; baseline on current 6 adapters.

**#69 Compression on HF dataset uploads**
- Done when: nightly daemon zstd-19 recompresses raw text in datasets; 9.56TB → projected ~5TB; HF API accepts.
- First step: pilot on smallest of 5 repos; measure before/after.

**#79 SBOM per axentx project**
- Done when: every PR generates CycloneDX SBOM as artifact; diff vs main → flag new deps in PR comment.
- First step: GH Action with `cyclonedx-bom`; comment template.

**#80 Dependency CVE monitoring**
- Done when: `osv-scanner` runs nightly; CVE ≥7.0 in transitive dep → Discord alert with affected package + recommended bump.
- First step: workflow + allowlist of known-accepted CVEs.

**#83→#90 (DX nice-to-haves)** are already self-evident from "why" clauses; no further detail needed.

**#95 Referral program**
- Done when: signup form accepts `?ref=<code>` → table; referrer credited at 50% of referee's first paid month.
- First step: schema + middleware; UI minimal.

**#96 Usage-based upgrade nudges**
- Done when: when user crosses 80% of free tier in 7d window, in-product banner shown + email sent (single send per threshold).
- First step: trigger in metering middleware.

**#97 Multi-tenant org accounts**
- Done when: org has shared quota + role-based access (admin/member); separate billing.
- First step: D1 schema migration; UI for invite flow.

**#98 Affiliate tracking pixel**
- Done when: UTM params tracked first-party; 90d attribution window; dashboard shows top sources.
- First step: Worker pixel route + KV session store.

---

## Risk register (top 10)

| Risk | Likelihood | Impact | Mitigation roadmap item |
|------|------------|--------|------------------------|
| HF rate limit on 9.56TB dataset push | High | High | #69 compression cuts size; #62 streaming bypasses upload |
| CF Worker subrequest cap exceeded under load | Medium | High | #76 rate limit; #91 metered billing aligns demand to capacity |
| GCP e2-micro CPU saturation | Medium | High | #66 hibernation; #48 dependency dashboard surfaces hot daemons |
| Supabase free-tier quota exhausted | Medium | High | #63 cost dashboard alerts; queue compression in tasks payload |
| LLM provider quota exhaustion cascade | Medium | Medium | #41 fallback testing; #65 routing by Pareto |
| Adapter pollution from bad training data | High | Medium | #2 eval gate; #49 DPO quality scoring; #52 PII scrubber |
| Auto-bot pushes corrupt main branch | Low | High | #3 branch protection; #74 secret scanner |
| Single dev (Ashira) unavailable | Medium | High | #38 fail-safe; #39 backups; #84 architecture diagram |
| Discord rate-limit during incident burst | Medium | Medium | discord-bot tuning #1 (rate limiter); #45 escalation tree |
| Customer data leak via training pairs | Low | Critical | #52 PII scrubber; #77 audit immutability; #78 threat model |

---

## Tuning recommendations per existing agent

Each agent gets 1–3 specific, actionable improvements grounded in current behavior. Format: `Issue` → `Tune`.

### research (×3 fanout)
1. **Issue**: Three parallel branches sometimes converge on same insight, wasting 2/3 tokens. **Tune**: After 30s, pre-emptively share short summary across branches; subsequent reasoning must explicitly diverge or self-terminate. **Acceptance**: ≥30% token reduction on duplicate-insight cases over 7d window.
2. **Issue**: Search results vary by run, breaking reproducibility. **Tune**: Cache search results for 24h in KV keyed by query-hash; replay deterministically when re-running same idea. **Acceptance**: Same query within 24h returns identical citations.
3. **Issue**: Results lack source-quality scoring. **Tune**: Score each cited URL on (domain authority, recency, primary-vs-secondary) before bd reads. **Acceptance**: Sources filtered by min score 0.4; tracked metric `low_quality_source_pct < 10%`.

### bd (business-development)
1. **Issue**: Output sometimes restates research without new synthesis. **Tune**: Reject any bd output where edit-distance to research summaries < 0.4; force re-roll with explicit "synthesize, don't summarize" instruction. **Acceptance**: ≤5% bd outputs flagged as restate.
2. **Issue**: TAM/SAM/SOM hallucinated. **Tune**: Block claim unless backed by ≥1 source from research with `<URL>::<quote>` proof embedded. **Acceptance**: 100% market-size claims have source pointer.
3. **Issue**: Ignores prior bd retros. **Tune**: Inject last 5 retro outcomes (which discoveries shipped, killed) into prompt context (depends on #30). **Acceptance**: bd outputs reference at least 1 prior retro when relevant pattern exists.

### design-thinking
1. **Issue**: Generates personas with implausible combinations (CFO who codes Rust). **Tune**: Validate persona consistency via small-LLM critic + library lookup (#27); reject and re-roll if critic flags. **Acceptance**: Critic-flagged personas <5%.
2. **Issue**: Empathy-map bullets too generic. **Tune**: Require ≥1 verbatim quote from customer-discovery (#23) or research-cited interview when available, else flag as low-evidence. **Acceptance**: ≥80% empathy-map entries have evidence pointer once #23 ships.

### business
1. **Issue**: Pricing recommendations rarely tested. **Tune**: Output must include explicit hypothesis + measurement plan that pricing harness (#29) can consume. **Acceptance**: 100% pricing recs have machine-parseable test plan.
2. **Issue**: Unit economics computed but not stress-tested. **Tune**: Auto-run sensitivity table (CAC ±30%, LTV ±50%, churn ±10pp); reject plan if any cell breaks even. **Acceptance**: Sensitivity table attached to every business decision row in agent_decisions.

### marketing
1. **Issue**: Invents stats ("50% faster"). **Tune**: Implement claim fact-check (#26); reject any quantitative claim without bd-evidence ID. **Acceptance**: 0 unsourced quantitative claims pass to PRD.
2. **Issue**: Tone inconsistent across same product. **Tune**: Pin brand voice card per axentx project; reviewer compares cosine-sim of new copy vs voice corpus, requires ≥0.7. **Acceptance**: Per-project voice cosine ≥0.7 on 95% of copy.
3. **Issue**: Headlines generic. **Tune**: Force generation of ≥5 headlines, then critic ranks by specificity (named outcome + numeric or named target user); pick top-2. **Acceptance**: All shipped headlines name outcome+target.

### prd
1. **Issue**: PRDs ship without kill criteria. **Tune**: Apply #25 (kill criteria template); reviewer blocks PR if missing. **Acceptance**: 100% PRDs have ≥3 kill criteria post-rollout.
2. **Issue**: Acceptance criteria written as wishes ("works fast"). **Tune**: Require Given/When/Then format, parsed by reviewer, rejected if any AC lacks measurable threshold. **Acceptance**: 100% AC parse as G/W/T with numeric threshold.
3. **Issue**: Scope creep — PRDs grow during refinement. **Tune**: Lock initial-scope hash; subsequent edits require explicit "scope-change: yes" flag in commit + re-review. **Acceptance**: Scope deltas auditable in agent_decisions.

### dev
1. **Issue**: Loads global knowledge index regardless of project; bloats context. **Tune**: Apply #1 per-project index loader. **Acceptance**: Per-project context size ≤40% of current global.
2. **Issue**: Sometimes writes tests that mirror implementation (test passes if implementation passes, even if both wrong). **Tune**: For `*-core` packages enable test-first flag (#5) so failing test must precede impl. **Acceptance**: All `*-core` PRs have test-commit before impl-commit.
3. **Issue**: Diff size occasionally 800+ LOC. **Tune**: Apply #7 cap with auto-split; refuse to commit single diff > 250 LOC unless flagged `large-diff: justified`. **Acceptance**: 95% of commits ≤250 LOC.

### qa
1. **Issue**: Coverage % reported but not gating. **Tune**: Per-package coverage threshold in `package.json`; CI fails below threshold; track delta per PR. **Acceptance**: PR fails CI if coverage drops by >2pp.
2. **Issue**: Doesn't differentiate flake from real fail. **Tune**: Auto-rerun failed tests once; if pass→fail→pass pattern, mark `flaky` and open issue but don't block PR; track flake-rate per test. **Acceptance**: Flake-rate dashboard in /dash; tests with flake>5% get auto-issue.
3. **Issue**: No performance regression check. **Tune**: For 10 critical paths, add benchmark harness (Vitest bench / pytest-benchmark) gated on ≤10% regression vs main. **Acceptance**: PR fails CI on >10% bench regression.

### reviewer
1. **Issue**: Sometimes spends an attempt on lint errors. **Tune**: Apply #6 lint-gate before reviewer ever sees diff. **Acceptance**: 0 reviewer attempts wasted on lint.
2. **Issue**: Three-attempt cap can pass low-quality work when first two attempts crash. **Tune**: Distinguish "crash" from "fail review"; only failed-review attempts count toward cap, crashes get retry-with-backoff. **Acceptance**: Crash-related cap-misuse drops to 0 in 30d.
3. **Issue**: Rubric drifts silently. **Tune**: Apply #4 rubric versioning + commit rubric file alongside reviewer prompt; require explicit version bump. **Acceptance**: Every rubric change has a version bump commit.

### commit
1. **Issue**: Mixed commit message styles. **Tune**: Apply #10 conventional commits + commitlint; reject commit if non-conformant. **Acceptance**: 100% conformance on auto-bot commits.
2. **Issue**: Auto-bot commits push to main directly. **Tune**: Apply #3 branch protection; auto-bot opens PR, auto-merges if all checks pass and reviewer-bot label present. **Acceptance**: 0 direct-to-main pushes from bot account.
3. **Issue**: Commits include trace-irrelevant whitespace changes. **Tune**: Pre-commit `git add -p` analog limited to logical hunks; whitespace-only commits squashed into nearest logical commit. **Acceptance**: Whitespace-only commits = 0.

### pm
1. **Issue**: pm doesn't see cross-pipeline dependencies. **Tune**: Inject DAG of in-flight discovery_id chains (depends on #22 traceability) so pm can reason about bottlenecks. **Acceptance**: pm output includes bottleneck section in 100% of weekly reports.
2. **Issue**: pm summaries are flat narrative. **Tune**: Output structured `{shipped, in-flight, blocked, killed}` JSON consumed by /dash. **Acceptance**: /dash renders pm output without manual parsing.

### watchdog
1. **Issue**: Restarts daemon without root-cause classification. **Tune**: Before restart, capture last 200 lines of structured logs (#33) + tag with symptom; restart loop > 3 in 1h escalates. **Acceptance**: Every restart event has symptom tag.
2. **Issue**: Same alert fires repeatedly. **Tune**: Alert dedup window per `alert.code` (15min default); snooze if matching unresolved alert exists. **Acceptance**: Alert-spam reduced ≥70% in 7d post-rollout.

### self-heal
1. **Issue**: Allowed actions hardcoded. **Tune**: Promote to DSL (#47) — declarative `if symptom matches, run remediation X` with explicit safety rails. **Acceptance**: Adding new remediation requires only YAML edit, no code change.
2. **Issue**: No record of healed-by-self vs healed-by-Ashira. **Tune**: Tag every recovered alert with `recovery.actor` (self|human|timed-out) for SLO accuracy. **Acceptance**: SLO dashboard distinguishes auto-recovered vs human-recovered.

### discord-bot
1. **Issue**: Message-rate spike during burst incidents → Discord ratelimits. **Tune**: Token-bucket rate limiter (5/sec) with priority queue (critical jumps); batch low-priority into 1 msg/min. **Acceptance**: 0 Discord 429 in 30d.
2. **Issue**: Thai/English routing imperfect. **Tune**: After current detection, log mis-classifications via Discord reaction emoji (white-check / x) to a feedback queue; weekly retrain on ≥50 corrections. **Acceptance**: Mis-classification rate <3% after 4 retrain cycles.

### scheduler (hermes-scheduler)
1. **Issue**: Some 168 cron jobs overlap, causing brief work-queue contention. **Tune**: Add `priority` column to scheduled tasks; dispatcher picks highest-priority first within same minute slot. **Acceptance**: Queue contention metric in /dash drops ≥50%.
2. **Issue**: No backfill on missed runs. **Tune**: On startup, read `last_run_at` per job; if older than `2 × interval`, optionally backfill once with `backfill: true` flag. **Acceptance**: Restart after 1h+ outage triggers documented backfill behavior.

### worker (hermes-worker ×5)
1. **Issue**: Five replicas claim from same queue with `FOR UPDATE SKIP LOCKED` but occasionally one worker handles 80% of load. **Tune**: Add per-worker max-claim-per-minute to enforce fair distribution; spillover triggers more aggressive sleep-back on idle workers. **Acceptance**: Worker load Gini coefficient <0.3.
2. **Issue**: Worker holds claim during long LLM call; if worker dies, claim takes lease-timeout to release. **Tune**: Periodic heartbeat (every 30s) extends claim only while worker is alive; lease 60s instead of 600s reduces stuck-task time. **Acceptance**: Stuck-task p95 recovery <2min.
3. **Issue**: No bulkhead between agent roles; reviewer storm starves dev. **Tune**: Per-role concurrency cap (e.g. max 2 reviewers, max 1 dev per worker) to prevent single role saturating CPU on e2-micro. **Acceptance**: Per-role-CPU envelope respected; e2-micro load1 stays <0.8 over 7d.

---

## Cross-cutting prerequisites (build these first to unblock most features)

| Prereq | Unblocks features | Why first |
|---|---|---|
| Structured JSON logs (#33) | #34, #35, #43, #48, #64, #67 | Ops + cost work all need uniform log schema |
| Trace ID propagation (#34) | #35, #36, #42, #46 | End-to-end debugging foundation |
| Held-out eval suite (#50) | #2, #11, #54, #57, #58, #61 | Cannot gate or iterate adapters without |
| Per-project knowledge-index (#1) | #14, #20 | Reduces context bloat across all dev work |
| Branch protection (#3) | #8, #74 | Foundation for any auto-bot quality gate |
| Cost dashboard (#63) | informs prioritization of every other feature | Cannot make trade-offs blind |

**Recommended sequencing**: ship the 6 prerequisites in week 1-2, then unlock the rest in priority order driven by cost dashboard signal.

---

## 12-week shipping sequence (suggested)

| Week | Theme | Items | Rationale |
|---|---|---|---|
| 1 | Observability foundation | #33, #34, #66 | Everything else needs logs+traces |
| 2 | Quality gates | #3, #6, #7, #74 | Stop quality regressions before scaling |
| 3 | Eval foundation | #50, #51, #54 | Cannot ship adapters blind |
| 4 | Cost visibility | #63, #64, #65, #67 | Make trade-offs informed |
| 5 | Discovery quality | #21, #22, #25 | Better products from better signal |
| 6 | Training quality | #2, #49, #52 | Adapters stop getting worse |
| 7 | Security baseline | #73, #75, #76, #77 | Prevent leak before scaling |
| 8 | DX onboarding | #83, #84, #85 | Enable second engineer |
| 9 | Reliability | #35, #36, #37, #39 | SLO + recovery |
| 10 | Discovery loop | #23, #24, #26 | Real-customer signal injected |
| 11 | Business prep | #92, #93, #94, #78 | Take payment legally + safely |
| 12 | Monetization | #91 + nice-to-haves | Open the door |

---

## Out-of-scope (intentionally excluded)

- Mobile apps (no current product needs them)
- Kubernetes migration (e2-micro adequate, premature)
- Self-hosted GitLab / Gitea (GitHub free tier fine)
- Multi-region deployment (single user, single region OK)
- Custom hardware / colo (cloud free tiers cover need)
- Custom LLM training from scratch (PEFT adequate)
- Real-time collaboration features (Workio scoped to async)
- Voice/video features (out of scope for current verticals)
- Blockchain/web3 integrations (no product fit identified)
- Heavy proprietary datasets (free + open sources sufficient)

---

## Open questions for human review

1. Do we want to monetize before adapter quality is proven (#91 sequencing)?
2. Is 90-day backup retention enough or do regulators (post-#94 ToS) demand more?
3. Should we prioritize adapter merging (#57) or per-stack adapters (#61) first?
4. What is the budget cap before triggering auto-pause across all paid services?
5. Do we want a public roadmap on the landing page (#92) or keep this internal?
6. How much engineering effort do we allocate to research (#11, #15) vs business (#91, #95)?
7. Should agent contracts (#89) be machine-checked (TypeScript types) or doc-only?
8. Is 250 LOC diff cap (#7) too aggressive for refactor PRs? Need exception path.

---

## Glossary (terms used above)

| Term | Definition |
|------|------------|
| Adapter | LoRA/PEFT delta on top of base LLM, distributed via HF Hub |
| Verdict triple | (prompt, accepted_response, rejected_response) used for DPO |
| DPO | Direct Preference Optimization — fine-tuning method using preference pairs |
| SFT | Supervised Fine-Tuning — standard prompt→response training |
| Discovery_id | UUID linking ideation → bd → PRD → dev → ship for traceability |
| Trace_id | UUID linking observability events for a single pipeline run |
| Rubric | Reviewer's grading criteria, versioned to keep training data clean |
| Held-out | Evaluation set never seen in training, frozen for adapter benchmarking |
| Bulkhead | Concurrency partitioning to prevent one role exhausting shared resources |
| EWMA | Exponentially Weighted Moving Average for anomaly detection |
| SBOM | Software Bill of Materials, dependency manifest for supply chain |
| STRIDE | Threat-modeling acronym (Spoof/Tamper/Repudiate/Info-disclose/DoS/Elevation) |
| Pareto routing | Picking provider on cost-quality frontier per request |
| Bot label | GitHub label `auto-bot:approved` exempting auto-bot PRs from human review |
| Burn rate | Rate at which SLO error budget is consumed |
| Wizard-of-Oz | Fake prototype shown to users to validate demand before building |

---

## Cross-reference index (feature # → category → first prerequisite)

| # | Cat | Prereq |
|---|-----|--------|
| 1 | Dev | none |
| 2 | Dev | #50 |
| 3 | Dev | none |
| 4 | Dev | none |
| 5 | Dev | none |
| 6 | Dev | none |
| 7 | Dev | none |
| 8 | Dev | #3 |
| 9 | Dev | none |
| 10 | Dev | none |
| 11 | Dev | #2 |
| 12 | Dev | none |
| 13 | Dev | none |
| 14 | Dev | #1 |
| 15 | Dev | none |
| 16 | Dev | none |
| 17 | Dev | none |
| 18 | Dev | none |
| 19 | Dev | none |
| 20 | Dev | #1 |
| 21 | Disc | none |
| 22 | Disc | none |
| 23 | Disc | none |
| 24 | Disc | none |
| 25 | Disc | none |
| 26 | Disc | #24 |
| 27 | Disc | none |
| 28 | Disc | none |
| 29 | Disc | none |
| 30 | Disc | #22 |
| 31 | Disc | none |
| 32 | Disc | none |
| 33 | Ops | none |
| 34 | Ops | #33 |
| 35 | Ops | #33 |
| 36 | Ops | #34 |
| 37 | Ops | none |
| 38 | Ops | none |
| 39 | Ops | none |
| 40 | Ops | #39 |
| 41 | Ops | none |
| 42 | Ops | #34 |
| 43 | Ops | #33 |
| 44 | Ops | #35 |
| 45 | Ops | #37 |
| 46 | Ops | #4 |
| 47 | Ops | #37 |
| 48 | Ops | #33 |
| 49 | Train | #4 |
| 50 | Train | none |
| 51 | Train | none |
| 52 | Train | none |
| 53 | Train | none |
| 54 | Train | #50 |
| 55 | Train | #49 |
| 56 | Train | #55 |
| 57 | Train | #50 |
| 58 | Train | #50 |
| 59 | Train | #4 |
| 60 | Train | none |
| 61 | Train | #50 |
| 62 | Train | none |
| 63 | Cost | none |
| 64 | Cost | #33 |
| 65 | Cost | none |
| 66 | Cost | none |
| 67 | Cost | #33 |
| 68 | Cost | #65 |
| 69 | Cost | none |
| 70 | Cost | #67 |
| 71 | Cost | none |
| 72 | Cost | #65 |
| 73 | Sec | #75 |
| 74 | Sec | none |
| 75 | Sec | none |
| 76 | Sec | none |
| 77 | Sec | #33 |
| 78 | Sec | none |
| 79 | Sec | none |
| 80 | Sec | #79 |
| 81 | Sec | #39 |
| 82 | Sec | none |
| 83 | DX | none |
| 84 | DX | none |
| 85 | DX | none |
| 86 | DX | none |
| 87 | DX | #83 |
| 88 | DX | #86 |
| 89 | DX | none |
| 90 | DX | #83 |
| 91 | Biz | #76 |
| 92 | Biz | none |
| 93 | Biz | none |
| 94 | Biz | none |
| 95 | Biz | #91 |
| 96 | Biz | #91 |
| 97 | Biz | #91 |
| 98 | Biz | #92 |
| 99 | Biz | #29 |
| 100 | Biz | #91 |

---

## Effort estimate (rough)

Sum of complexities (S=4h, M=8h, L=16h):

| Bucket | Items | Sum hours |
|---|---|---|
| MUST features | 50 | ~520h |
| NICE features | 50 | ~480h |
| Tuning improvements | 30 | ~120h |
| **Grand total** | 130 work-items | ~1,120h |

At 1 engineer × 30h/week productive coding time → ~37 weeks = ~9 months for full roadmap.
Pragmatic plan: 12 weeks for MUSTs, then re-evaluate from cost dashboard signal.

---

## Done.

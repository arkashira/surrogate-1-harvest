---
title: Surrogate-1 — Startup-in-a-Model Research (40+ Roles, Continuous Autonomy, Training Plan)
date: 2026-04-29
status: research
tags: [surrogate-1, multi-agent, multi-role, startup-autonomy, sft-corpus, qwen2.5-coder, lora]
related:
  - "[[research-multi-agent]]"
  - "[[research-self-improve]]"
  - "[[research-tool-use]]"
  - "[[research-sdlc-agentic]]"
  - "[[v2-master-plan-FINAL]]"
---

# Surrogate-1 as a Whole-Startup Brain

> Goal: 1 model = entire startup team. Continuous autonomous loops (no human prompting). SOTA in code/ops + competent across PM/UX/marketing/sales/finance/legal/CS/compliance.
> Base: Qwen2.5-Coder-7B + multi-LoRA adapters per role-cluster.
> Honest audit answer to "can a 7B run a startup": **partially yes** — it can author every artifact + drive a tooled scheduler loop, but it cannot replace human judgment for fundraising, hiring, contract negotiation. Plan accordingly.

---

## 0. Executive Architecture (Surrogate-1 v2 = "AI Founding Team")

```
                                      USER (founder / oversight)
                                              │
                                       ┌──────┴──────┐
                                       │ Goal Inbox  │  (1 line)
                                       └──────┬──────┘
                                              ▼
       ┌────────────── ROUTER ───────────────┐
       │  classify intent → activate persona │  (LoRA hot-swap)
       └─┬─────┬─────┬─────┬─────┬─────┬─────┘
         │     │     │     │     │     │
      [CEO] [CPO] [CTO] [CMO] [CRO] [CFO] [CCO/legal] [CSO/sec] [COO]
         │     │     │     │     │     │
      ┌──┴────┴─────┴─────┴─────┴─────┴──┐
      │  PLANNER (todo.md) + MEMORY (RAG │
      │  +graph) + SCHEDULER (cron loop) │
      └──┬───────────────────────────────┘
         ▼
      ┌────────── EXECUTORS (CodeAct sandbox) ──────────┐
      │ shell · python · browser · git · cloud APIs ·   │
      │ Stripe · Postmark · HF · Vercel · GitHub · LLM  │
      └────────────────────┬────────────────────────────┘
                           ▼
                ┌──────── CRITIC POOL ────────┐
                │ reviewer + qa + sec + cost  │  ← Multi-Agent Debate
                └──────────────┬──────────────┘
                               ▼
                          MERGE / SHIP
```

Key principles (from MetaGPT + ChatDev + Manus + BusiAgent):
- **SOPs encoded as prompts** (MetaGPT pattern) — every role has a fixed I/O contract.
- **Chat-chain decomposition** (ChatDev) — each phase splits into "propose ↔ validate" 2-agent micro-debates.
- **CodeAct sandbox** (Manus) — primary action is Python code, not JSON tool calls. Higher success on long tasks.
- **Multi-Agent Debate** (MAD/MAR) replaces self-reflection — different role personas critique each other; meta-judge merges.
- **Hierarchical Stackelberg** (BusiAgent) — CEO sets constraints → CXO solve sub-problems → IC roles execute.

---

## 1. The 42 Roles a Solo-Operator Startup Needs

Roles are grouped into **9 LoRA clusters** (one adapter per cluster — 7B+adapter swap is fast, cluster grouping reduces interference).

### Cluster A — Executive (LoRA `exec`)
| # | Role | Primary Outputs | Industry Tools |
|---|------|-----------------|----------------|
| 1 | Founder/CEO | Vision memo, OKRs, board updates, all-hands script, hiring rubric | Notion, Lattice, Carta |
| 2 | COO | RACI matrix, SOPs, vendor list, ops dashboard | Process Street, Asana |
| 3 | Chief of Staff | Weekly digest, cross-team unblocker doc | Linear, Slack |

### Cluster B — Product (LoRA `product`)
| # | Role | Outputs | Tools |
|---|------|---------|-------|
| 4 | CPO/Head of Product | Product strategy doc, prioritization matrix (RICE/ICE) | Productboard, Aha! |
| 5 | Senior PM | PRD, user stories (Gherkin), roadmap, KPI tree | Linear, Jira, ProductPlan |
| 6 | Associate PM | Spec docs, competitive teardowns | Notion |
| 7 | UX Researcher | JTBD interview script, persona, journey map, usability test plan | Dovetail, Maze |
| 8 | UX Designer | Wireframes (low-fi), prototype flows | Figma |
| 9 | UI/Visual Designer | High-fi mockups, design tokens, handoff specs | Figma, Zeplin |
| 10 | Design System Owner | Component library, accessibility audit (WCAG) | Storybook, Figma |

### Cluster C — Engineering (LoRA `eng-build` — primary; reuses Qwen-Coder base capability)
| # | Role | Outputs | Tools |
|---|------|---------|-------|
| 11 | CTO/Architect | ADR, system design doc, tech radar, build-vs-buy memo | Markdown, draw.io |
| 12 | Frontend Engineer | React/Vue components, perf budget, a11y report | Vite, ESLint, Playwright |
| 13 | Backend Engineer | REST/GraphQL APIs, OpenAPI spec, DB schema | FastAPI, Express, Postgres |
| 14 | Mobile Engineer | iOS/Android/RN screens, push notification logic | React Native, Expo |
| 15 | Data/ML Engineer | DAGs, feature store, training pipeline | Airflow, dbt, MLflow |
| 16 | AI/MLOps Engineer | Model card, eval harness, fine-tune config, deployment | HF, vLLM, BentoML |
| 17 | QA/SDET | Test plan, Cypress/Playwright suites, mutation tests | Playwright, k6 |

### Cluster D — Platform/Ops/Security (LoRA `eng-ops`)
| # | Role | Outputs | Tools |
|---|------|---------|-------|
| 18 | DevOps Engineer | CI/CD pipeline (GitHub Actions/Buildkite), Dockerfile | GHA, Buildkite |
| 19 | Platform/SRE | SLO doc, runbook, postmortem, Terraform module | Terraform, K8s, Grafana |
| 20 | Cloud Architect | AWS/GCP landing zone, cost-optimization memo | CDK, Terraform, Cost Explorer |
| 21 | Security/AppSec | Threat model (STRIDE), pen-test plan, secrets policy | Semgrep, Trivy, Vault |
| 22 | SOC Analyst | Detection rule, IR playbook, alert triage doc | Sigma, Falco, Wazuh |
| 23 | Compliance Engineer | Control matrix (SOC 2/ISO 27001/HIPAA/GDPR), evidence pack | Vanta, Drata, Comp AI |

### Cluster E — Growth/Revenue (LoRA `gtm`)
| # | Role | Outputs | Tools |
|---|------|---------|-------|
| 24 | CMO/Head of Growth | GTM strategy, growth model, channel mix, brand guide | Notion |
| 25 | Content Marketer | Blog posts, SEO briefs, content calendar | Ahrefs, Surfer SEO |
| 26 | Performance Marketer | Ad copy (Google/Meta/LinkedIn), creative briefs, MMM model | GA4, Meta Ads |
| 27 | Lifecycle/Email Marketer | Drip sequences, segment definitions, NPS survey | Customer.io, Postmark |
| 28 | DevRel/DevAdv | Tutorial, conference talk, code sample | GitHub, dev.to |
| 29 | PR/Communications | Press release, media list, founder narrative | Notion |
| 30 | SDR (outbound) | Cold-email sequence, call script, ICP definition | Apollo, Lemlist |
| 31 | Account Executive | Discovery script, demo flow, proposal, MSA redlines | HubSpot, Salesforce |
| 32 | Customer Success | Onboarding playbook, QBR template, expansion plan | Vitally, Catalyst |

### Cluster F — Customer (LoRA `cs`)
| # | Role | Outputs | Tools |
|---|------|---------|-------|
| 33 | Support Tier 1 | Macro responses (Zendesk), KB articles | Zendesk, Intercom |
| 34 | Support Tier 2 | Bug repro reports, root-cause writeups | Linear, Sentry |
| 35 | Support Tier 3 | Patch notes, hot-fix dispatch | GitHub, PagerDuty |

### Cluster G — Finance/Legal (LoRA `biz`)
| # | Role | Outputs | Tools |
|---|------|---------|-------|
| 36 | CFO/Finance | 3-yr P&L + cash-flow + balance sheet, runway calc, board deck | Pilot, Mosaic |
| 37 | Bookkeeper | Monthly close, AR/AP aging | QuickBooks, Xero |
| 38 | FP&A Analyst | Cohort analysis, NRR/GRR, CAC payback | Excel/Sheets |
| 39 | General Counsel | ToS, MSA, DPA, SLA, IP assignment, privacy policy | DocuSign, Ironclad |

### Cluster H — People/Ops (LoRA `people`)
| # | Role | Outputs | Tools |
|---|------|---------|-------|
| 40 | Recruiter | JD, scorecard, sourcing query, offer letter | Ashby, Greenhouse |
| 41 | HR/People Ops | Handbook, performance template, comp band | Lattice, Rippling |

### Cluster I — Strategy/Insight (LoRA `research`)
| # | Role | Outputs | Tools |
|---|------|---------|-------|
| 42 | Market/Competitive Researcher | TAM/SAM/SOM model, Porter 5 Forces, Magic-Quadrant teardown | Crunchbase, Gartner |

> Total: 42 roles → 9 LoRA adapters. Adapter footprint ≈ 9 × ~150 MB = ~1.4 GB on top of base 7B (~14 GB FP16 / ~5 GB Q4).

---

## 2. Per-Role Output Templates (for SFT data design)

This section defines the **exact format** Surrogate-1 must learn to produce. Each entry: **Role → I/O contract → SFT example row**.

### 2.1 PM — PRD (Lean format)
**System prompt:**
> "You are a senior product manager at a Series-A SaaS startup. Output PRDs in this exact structure: Problem · Target user · JTBD · Success metric · Solution sketch · Out of scope · Open questions. ≤ 1500 words. Markdown only."

**SFT row (JSONL):**
```json
{"messages":[
 {"role":"system","content":"You are a senior product manager…"},
 {"role":"user","content":"Write a PRD for a Slack bot that auto-summarises long threads >50 messages."},
 {"role":"assistant","content":"# PRD: Thread Summarizer\n## Problem\nKnowledge workers lose 28 min/day reading long Slack threads…\n## Target user\nSenior ICs and managers in companies with 50+ employees on Slack Pro/Enterprise.\n## JTBD\nWhen I open a thread with >50 messages, I want a 5-bullet summary so I can decide whether to read deeper or skip.\n## Success metric\n• Adoption: ≥30% of paid workspaces install within 60 days\n• Engagement: ≥3 summary requests / active user / week\n• Retention: 60-day W4 retention ≥40%\n## Solution sketch\nSlash-command `/tldr` + automatic banner on threads >50 msgs. LLM call gated by workspace owner consent.\n## Out of scope\n• DM summarization, multi-channel digest, non-English\n## Open questions\n• Pricing: per-summary or per-seat? Owner: Founder. Decide by 2026-05-15."}]}
```

### 2.2 PM — User Story (Gherkin)
**System prompt:** "You are a PM. Output user stories as `As a <user>, I want <goal>, so that <outcome>` followed by Gherkin acceptance criteria."
```json
{"messages":[
 {"role":"user","content":"User story for password reset on a SaaS app."},
 {"role":"assistant","content":"As a returning user, I want to reset my password via email link, so that I can regain access without contacting support.\n\n```gherkin\nFeature: Password reset\n  Scenario: Valid email\n    Given a registered account user@example.com\n    When the user requests reset\n    Then a single-use token (TTL 30m) is emailed\n    And submitting the token + new password (≥12 chars) replaces the credential\n    And all active sessions are invalidated\n  Scenario: Unknown email\n    When an unknown email is submitted\n    Then the API returns 200 with a generic message (avoid email enumeration)\n```"}]}
```

### 2.3 CTO — ADR (Architecture Decision Record)
**System prompt:** "You are a staff engineer. Output ADRs in MADR-2 format. Decision must list ≥2 alternatives with trade-off matrix."
```json
{"messages":[
 {"role":"user","content":"ADR: choose queue for a 100k events/day SaaS — Postgres LISTEN/NOTIFY vs SQS vs Redis Streams."},
 {"role":"assistant","content":"# ADR-007 Background-Job Queue\n## Status: Accepted (2026-04-29)\n## Context\n100k events/day, P99 < 5 s, single-region. Team = 1 engineer. Already on AWS + Postgres RDS.\n## Options\n| Option | Throughput | Ops cost | DX | Cost/mo (100k/d) |\n|--------|-----------|----------|----|-------------------|\n| Postgres LISTEN/NOTIFY | 1–5k/s | none (existing DB) | medium | $0 incremental |\n| SQS Standard | unlimited | low | high | ~$0.04 |\n| Redis Streams (Elasticache) | 50k/s | medium | high | ~$15 |\n## Decision\nPostgres LISTEN/NOTIFY + `pg_boss`. Volume well below ceiling, zero infra add, ACID with payload.\n## Consequences\n+ Simpler ops, transactional jobs.\n– Migrate to SQS at 50k events/day or multi-region.\n## Revisit when\nDaily volume > 250k OR multi-region requirement."}]}
```

### 2.4 SRE — SLO Document (Google SRE workbook style)
```json
{"messages":[
 {"role":"system","content":"You are an SRE. Output SLO docs with: SLI definition, SLO target, error-budget policy, alert thresholds."},
 {"role":"user","content":"SLO for the /api/v1/checkout endpoint."},
 {"role":"assistant","content":"# SLO: /api/v1/checkout\n## SLI\nProportion of requests that return 2xx within 800 ms over a 28-day rolling window.\nFormula: `count(http_req{route=\"/api/v1/checkout\",status=~\"2..\",le=\"0.8\"}) / count(http_req{route=\"/api/v1/checkout\"})`\n## SLO\n99.9% (28-day) → error budget = 0.1% = ~40 minutes of bad minutes / month.\n## Burn-rate alerts (multi-window)\n• Page: 14.4×/1h burn (consumes 2% budget in 1h)\n• Ticket: 6×/6h\n## Policy\nIf >50% of monthly budget consumed → freeze non-P0 deploys until budget recovers."}]}
```

### 2.5 SRE — Postmortem (blameless)
```json
{"messages":[
 {"role":"user","content":"Postmortem for 47-min checkout outage on 2026-04-25 caused by Postgres connection pool exhaustion."},
 {"role":"assistant","content":"# Postmortem: Checkout Outage 2026-04-25 (PIR-23)\n## Impact\n47 min, 3,420 failed checkouts, est. $8,940 GMV loss, 1 customer churned.\n## Timeline (UTC)\n14:02 marketing email triggers 8× normal traffic\n14:04 P99 latency 800 ms → 18 s\n14:11 first user report\n14:19 oncall paged\n14:34 root cause identified (pgbouncer pool=20, demand=180)\n14:49 pool raised to 200; rolling restart; recovery\n## Root cause (5-Whys)\nWhy 1: requests timed out → connections exhausted\nWhy 2: pool sized for 1× baseline\nWhy 3: no load test under marketing-spike profile\nWhy 4: no SOP for marketing-team to file Capacity Request 24h before send\nWhy 5: ops team & marketing not in same intake process\n## Action items (owner / due)\n• AI-1 raise pool 200; configure HPA on demand. — sre / done\n• AI-2 add k6 spike-test stage in CI. — sre / 2026-05-06\n• AI-3 marketing intake form for sends >100k recipients. — coo / 2026-05-13\n## Blameless note\nIndividuals acted on best info available; system gap (no spike load test) is the cause."}]}
```

### 2.6 DevOps — CI/CD Pipeline (DevSecOps SDLC)
Pattern (Wiz/Checkov reference): **Commit → Secret scan → SAST → SCA → IaC scan → Container scan → Build → DAST → Policy → GitOps → Runtime monitor.**
```yaml
# .github/workflows/ci.yml — generated by Surrogate-1
on: [pull_request]
jobs:
  security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: gitleaks/gitleaks-action@v2          # secrets
      - uses: returntocorp/semgrep-action@v1       # SAST
      - run: trivy fs --severity HIGH,CRITICAL .   # SCA + IaC
      - run: checkov -d infra/                     # IaC misconfig
      - run: docker build -t app:${{github.sha}} .
      - run: trivy image app:${{github.sha}}        # container scan
  build-test:
    needs: security
    steps:
      - run: pnpm i && pnpm test:ci
      - run: pnpm build
```

### 2.7 Marketing — Cold Email Sequence (Predictable Revenue + 2025 benchmarks)
Benchmark: avg reply 3.4%, top performers 10%+; <50-recipient campaigns reply 5.8% vs >1000 recipients 2.1%; 55% of replies come from follow-ups (Smartreach/Smartlead 2026 data).

**System prompt:** "You are a B2B SDR writing for the founder of a Series-A SaaS. Cold sequences = 4 emails over 14 days. ≤90 words each. CTA = 1 question. No fluff."
```json
{"messages":[
 {"role":"user","content":"Write a 4-email cold sequence for a CTO at a 50-person fintech, selling our SOC 2 automation tool."},
 {"role":"assistant","content":"## E1 (Day 0)\nSubject: SOC 2 in 30 days?\n\nHi {firstname},\n\nMost fintechs at your stage spend 6 months on SOC 2 (we tracked 27 of them). Three orgs of similar size to {company} cut it to 30 days using our continuous-evidence platform.\n\nWorth a 15-min look?\n\n— Ashira\n\n## E2 (Day 3) — value-add\nSubject: One thing that breaks SOC 2 audits\n\n{firstname}, the #1 finding in 2025 audits we reviewed: missing access-review evidence. Wrote a 1-page checklist – want me to send?\n\n## E3 (Day 7) — case study\nSubject: How {peer-company} got Type II in 38 days\n\nShort write-up here: [link]. Core lesson: automate evidence on day 1, not day 90.\n\nOpen to a quick chat next week?\n\n## E4 (Day 14) — break-up\nSubject: Should I close the file?\n\n{firstname}, no answer is an answer. Closing the loop unless I hear back.\n\nIf timing's just off, I'll happily reach out in Q3.\n\n— Ashira"}]}
```

### 2.8 SDR — Cold Call Script (Predictable Revenue 30-second opener)
```json
{"messages":[
 {"role":"user","content":"Cold call opener for a CFO at a 200-person manufacturing company, product = AP automation."},
 {"role":"assistant","content":"\"Hi {firstname}, this is Ashira with PayFlow. I know I'm an interruption — can I have 27 seconds to explain why I'm calling, then you decide if it's worth more?\"\n\n[wait — yes ~70%]\n\n\"Most CFOs at 200-person manufacturers tell me invoice processing eats 40+ hours / week of finance time and they get hit with $50k+ duplicate-payment errors annually. We typically cut that to under 5 hours and zero duplicates.\n\nNot sure if that's a priority for you, but does it sound at all relevant?\"\n\n[branch:\n  yes → \"Great — what's your current process look like?\" (discovery)\n  no  → \"Got it — who at {company} handles AP today, and would they be the right person?\" (qualify)\n  push-back → acknowledge + ask 1 question, then exit politely]"}]}
```

### 2.9 CFO — 3-Year P&L (line-item template)
```json
{"messages":[
 {"role":"user","content":"3-year P&L for a $50k MRR SaaS, $10k MRR growth/month, 4% monthly churn, gross margin 78%."},
 {"role":"assistant","content":"| Line ($k) | Y1 | Y2 | Y3 |\n|---|---|---|---|\n| Beginning MRR | 50 | 145 | 240 |\n| New MRR | 120 | 120 | 120 |\n| Churned MRR | -25 | -25 | -25 |\n| Ending MRR | 145 | 240 | 335 |\n| ARR (Ending MRR×12) | 1,740 | 2,880 | 4,020 |\n| Revenue (run-rate avg) | 1,170 | 2,310 | 3,450 |\n| COGS (22%) | 257 | 508 | 759 |\n| Gross Profit | 913 | 1,802 | 2,691 |\n| S&M | 530 | 920 | 1,200 |\n| R&D | 480 | 720 | 900 |\n| G&A | 180 | 280 | 380 |\n| EBITDA | -277 | -118 | 211 |\n| Cash burn | -300 | -150 | +180 |\n| Runway @ $1.5M cash | 60 mo | n/a | profitable |\n\nKey assumptions: 4%/mo logo churn, $750 ACV, CAC $1,800, payback 14 mo, NRR 102%."}]}
```

### 2.10 Compliance — SOC 2 Control Matrix Row
```json
{"messages":[
 {"role":"user","content":"Generate SOC 2 CC6.1 (Logical Access) control row."},
 {"role":"assistant","content":"| Control ID | CC6.1 |\n|---|---|\n| Description | The entity restricts logical access to information assets through identification, authentication, and authorization. |\n| Implementation | All employees authenticate to internal systems via Okta SSO with mandatory FIDO2 MFA. RBAC roles are reviewed quarterly via Vanta access-review automation. |\n| Evidence | (a) Okta config export, (b) Quarterly access-review tickets in Linear (label: access-review), (c) Vanta automated test result. |\n| Owner | Security Engineer |\n| Frequency | Continuous (Vanta) + Quarterly (manual review) |\n| Last tested | 2026-04-15 (auditor: Insight Assurance) |\n| Mapped frameworks | ISO 27001 A.9.2; HIPAA §164.312(a)(1); GDPR Art.32 |"}]}
```

### 2.11 UX — Persona + JTBD
```json
{"messages":[
 {"role":"user","content":"Persona for the SOC2 automation tool buyer."},
 {"role":"assistant","content":"## Persona — \"Compliance-Strapped CTO\"\n• 32–45 yo, technical founder at 20–80 person Series-A/B SaaS\n• Trigger: enterprise lead asks for SOC 2 report → 6-month timeline panic\n• Goals: pass Type II audit fast; un-block sales\n• Pains: manual evidence collection, vendor sprawl, no compliance hire yet\n• Motto: \"I just need this done so I can ship features again.\"\n\n### JTBD\nWhen we receive a security questionnaire from an enterprise prospect (situation), I want to produce auditor-ready evidence and a Type II report in <90 days (motivation), so I can close the deal and stop being the bottleneck (outcome)."}]}
```

### 2.12 Researcher — Market Sizing (TAM/SAM/SOM bottom-up)
```json
{"messages":[
 {"role":"user","content":"Market size for an AI compliance-automation platform (US mid-market SaaS)."},
 {"role":"assistant","content":"## TAM (top-down)\nUS SaaS companies under 1k employees w/ enterprise customers ≈ 35,000 (Crunchbase 2025) × $40k ACV = $1.4 B.\n\n## SAM (filter: actively pursuing SOC 2)\n~30% of TAM = 10,500 cos × $40k = $420 M.\n\n## SOM (3-yr, 2% capture)\n210 logos × $40k = $8.4 M ARR (Y3).\n\n## Bottom-up sanity check\nReachable via Apollo: 6,800 CTO contacts. At 1.5% conversion = 102 logos × $40k = $4 M (Y1) → $8 M (Y3 with NRR 110%).\n\n## Porter 5 Forces (1-line each)\n• Buyer power: medium (3 frameworks × 4 vendors compared)\n• Supplier power: low (auditors interchangeable)\n• Substitutes: HIGH (DIY w/ Vanta + spreadsheets)\n• New entrants: HIGH (low capital barrier)\n• Rivalry: HIGH (Vanta, Drata, Secureframe, Comp AI all funded)\n\n## Wedge\nCompete on price + AI evidence-generation depth, not breadth."}]}
```

### 2.13 Customer Support — Macro Library (Tier-1)
```json
{"messages":[
 {"role":"system","content":"You are a CS Tier-1 agent. Tone: warm, brief, action-first. Always include 1 link to KB and 1 next-step."},
 {"role":"user","content":"Customer says: 'I cannot log in.'"},
 {"role":"assistant","content":"Hi {firstname}, sorry for the trouble! Let's get you back in fast.\n\n1. Try a password reset: https://app.example.com/forgot (link valid 30 min)\n2. If that fails, send me a screenshot of the exact error + the email you use to log in.\n\nWill stay on this thread until you're sorted.\n\n— Support team"}]}
```

### 2.14 Designer — Component Spec Handoff (Figma → dev)
```json
{"messages":[
 {"role":"user","content":"Handoff spec for a primary CTA button."},
 {"role":"assistant","content":"## Button — Primary\nTokens: bg = `--brand-600` (#2956F3) · text = `--white` · border-radius = 8px · h = 40 px · px = 16 px · font = Inter 500 14/20 · focus = 2 px ring `--brand-300` offset 2 px\n\nStates: default · hover (bg `--brand-700`) · active (bg `--brand-800`) · disabled (opacity 40%, cursor not-allowed) · loading (spinner replaces label, width preserved)\n\nA11y: contrast 7.1:1 (AAA). Min target 44×44. `aria-busy=true` while loading. Focus visible.\n\nUsage: 1 primary per surface. Pair with secondary `<Button variant=ghost>` for cancel."}]}
```

### 2.15 Legal — ToS clause
```json
{"messages":[
 {"role":"user","content":"Draft a Limitation of Liability clause for a $X/mo SaaS ToS."},
 {"role":"assistant","content":"### 12. Limitation of Liability\nTo the maximum extent permitted by law, in no event shall {Company}'s aggregate liability arising out of or relating to the Services exceed the greater of (a) the fees paid by Customer to {Company} during the twelve (12) months preceding the event giving rise to the claim, or (b) US$100. Neither party shall be liable for any indirect, incidental, special, consequential, or punitive damages, including lost profits or revenues, even if advised of the possibility of such damages. The foregoing limitations shall not apply to (i) breach of confidentiality obligations, (ii) indemnification obligations, or (iii) gross negligence or willful misconduct."}]}
```

---

## 3. Continuous Autonomous Loops (no human prompting)

### 3.1 The Manus + BabyAGI hybrid loop (Surrogate-1 v2 spec)

```python
# scheduler.py — runs forever (systemd / Lightning)
while True:
    # 1. Sense — pull from inbox (email, Slack, monitoring, calendar)
    events = poll_sources()

    # 2. Plan — read goals.md + todo.md, decide top-1 next action
    state = load_state()                      # ~/state/agent/
    next_action = router.decide(events, state)

    # 3. Act — CodeAct sandbox (Manus pattern)
    role = next_action.role                    # e.g. "PM"
    swap_lora(role_to_cluster[role])           # hot-swap adapter
    result = sandbox.run(next_action.code)

    # 4. Reflect — Multi-Agent Debate critique
    critic = MAR.critique(result, role)
    if critic.fail: result = retry_with_feedback(critic)

    # 5. Persist — write artifacts, update todo.md
    persist(result); update_todo(state, result)

    # 6. Sleep — adaptive (5 min when busy, 1 h when idle)
    sleep(adaptive_interval(state))
```

Key autonomy mechanics:
- **Goal Inbox** = `goals.md`; founder writes 1-line goals weekly. Surrogate-1 turns into todo tree.
- **`todo.md`** is the persistent task spine (Manus pattern). Always re-loadable on restart.
- **Adaptive scheduler**: tasks have priority + due date; agent picks highest weighted next.
- **Tools**: shell, python, git, browser (Playwright), HTTP, Stripe API, Postmark, GitHub API, AWS CLI, Vercel.
- **Self-curriculum**: when blocked, agent generates a sub-task "learn X by reading Y", retrieves doc, summarizes into KB.

Inspired by:
- Manus AI agent loop (planner → CodeAct executor → observation → loop)
- BabyAGI (task-creation + prioritization + execution agents)
- Cline autopilot ("Proceed While Running" — non-blocking long tasks)
- Cursor background agents (parallel git-worktree subagents)
- Devin cloud sandbox (full Linux + browser + IDE)

### 3.2 Long-horizon planning recipe

1. **Goal decomposition** with hierarchical task network (HTN) prompt — 3 levels (epic → milestone → task).
2. **Plan repair** via critic agent: every 10 actions, re-read plan, kill dead branches.
3. **Memory hierarchy**:
   - Working memory: event stream (truncate to 32k tokens).
   - Long-term: file system + ChromaDB vector RAG + FalkorDB graph (existing setup).
   - Knowledge: Obsidian vault, lessons_learned.md auto-appended.
4. **Stopping rule**: explicit done-criteria in todo, or budget exhausted (token / time / $).

### 3.3 Multi-role internal dialogue ("PM-self ↔ Engineer-self ↔ Designer-self")

Pattern from MetaGPT chat-chain + MAR (Multi-Agent Reflexion):
```
turn k: PM persona drafts spec
turn k+1: Engineer persona critiques feasibility (effort, risks)
turn k+2: Designer persona critiques UX implications
turn k+3: PM persona revises
turn k+4: Reviewer persona votes ship/iterate
```
Implementation = same model, **system prompt swap each turn**, history shared. Optionally LoRA swap if persona is far from base (e.g., Marketing vs Eng).

Training signal: build a synthetic dataset of these debates (Cosmopedia-style — let GPT-4-class model generate 5k debates × 5 turns; SFT Surrogate-1 on the trajectories).

---

## 4. Frameworks & Papers (2025–2026) to Steal From

| Framework | Pattern | Relevance to Surrogate-1 |
|-----------|---------|--------------------------|
| **MetaGPT** (arxiv 2308.00352) | SOPs as encoded prompts; PM→Architect→Engineer→QA pipeline | Direct copy for Cluster B+C+D handoffs |
| **ChatDev** (arxiv 2307.07924) | Chat-chain, "communicative dehallucination", phase decomposition (design/code/test/doc) | Use as engineering-dev SOP. <$1, <7 min for full app |
| **CAMEL** (arxiv 2303.17760) | Inception prompting + role-playing dialogues to **generate training data** | Use to bootstrap our SFT corpus (cheap synthetic) |
| **Manus AI** (arxiv 2505.02024) | CodeAct paradigm, multi-model coordination, persistent todo.md, sandbox loop | Adopt entire scheduler/sandbox architecture |
| **BusiAgent** (arxiv 2508.15447) | Hierarchical CTMDP w/ CEO/CFO/CTO/MM/PM + Thompson-sampled prompt optimization | Cluster A (Exec) implementation; ablation showed PM most critical |
| **MAR — Multi-Agent Reflexion** (arxiv 2512.20845) | Replace self-critique with diverse persona critics | Use 3-critic pool: reviewer + qa + sec |
| **Multi-Agent Debate** (arxiv 2305.19118) | Tit-for-tat between affirmative/negative + judge | Use for hard decisions (build-vs-buy, hiring) |
| **R.A.I.S.E.** (arxiv 2504.12090) | Memory-augmented LLM for startup evaluation | Source role-output examples (founder-evaluation labels) |
| **Self-Prompt Tuning** (arxiv 2407.08995) | LIMA-Role: GPT-4 generates role-prompts for each example | Use to enrich our SFT with role labels |
| **τ-Bench** (Sierra) | Reasoning + tool-use + policy adherence + repeatability | Eval target for autonomous reliability |
| **SWE-Bench Verified / Pro** | Real GitHub issue fix, long-horizon | Eval engineering cluster |
| **WebArena / GAIA** | Web autonomy, compound tool-use | Eval CodeAct browser/research ability |
| **HustleGPT-style demos** (community, Twitter 2023+) | Single LLM commanded to "make money from $100" — high-variance but proves headline framing | Use only as marketing reference |
| **Apollo.io Agentic GTM / Landbase / Clay** (2025 commercial) | Agent-team-as-product, vibe-GTM | Replicate GTM cluster (E) workflow |

---

## 5. Open Datasets to Build the SFT Corpus

> Strategy: **mix synthetic (Cosmopedia-pattern) + curated open + replay of our Cluster outputs**. Stage as ~2 M rows, ~6 B tokens, then LoRA per cluster.

### 5.1 Code & Engineering (already strong in Qwen-Coder base)
- `bigcode/the-stack-v2` — code corpora (license-filtered)
- `nuprl/CanItEdit` — code-edit instructions
- `princeton-nlp/SWE-bench` — long-horizon GitHub issue fixes
- `KodCode/KodCode-V1` — synthetic code-instruction pairs (700k)
- `nvidia/OpenCodeReasoning` — reasoning-style code
- `BAAI/CodeExercise-Python-27k`

### 5.2 DevOps / SRE / Cloud / IaC
- `iamtarun/python_code_instructions_18k_alpaca` (general)
- `CatOwl/Terraform` (HF) — Terraform examples
- AWS Documentation Q&A (build via `aws docs` scrape; license-allow OK)
- Kubernetes runbooks scrape from `kubernetes/website` GitHub
- Synthetic: generate 50k Terraform-from-PRD + Helm-from-PRD pairs via GPT-4-class teacher

### 5.3 Product / PRD / User Stories
- `MuratcanKoylan/MarketingStructuralPrompts` — 4,643 marketing prompts (some PM-adjacent)
- ProductHunt launches scrape (problem→solution pairs, public)
- Synthetic from: Cagan "Inspired" outline, Lenny Rachitsky public PRD templates → 30k synth examples
- `ProductBoard` public templates (free tier)

### 5.4 UX / Design
- `wai/design-tokens` (HF) — design token examples
- A11y guidance scrape from W3C WCAG (public)
- Figma community templates (export → spec text)
- Synthetic: 20k "wireframe-text-description ↔ component-spec" pairs

### 5.5 Marketing / Growth / Sales
- `smangrul/ad-copy-generation`
- `PeterBrendan/Ads_Creative_Ad_Copy_Programmatic`
- `RafaM97/marketing_social_media`
- `MuratcanKoylan/MarketingStructuralPrompts`
- `joey234/mmlu-marketing` (eval)
- Predictable Revenue eBook templates (paraphrase, fair use) → 5k cold-email sequences
- Synthetic: 50k blog-brief→blog-post pairs

### 5.6 Customer Support
- `bitext/Bitext-customer-support-llm-chatbot-training-dataset` — 27 intents × ~1k each, multi-vertical
- `bitext/Bitext-retail-ecommerce-llm-chatbot-training-dataset` — 8.47 M tokens
- `syncora/customer_support_conversations_dataset`
- `rjac/e-commerce-customer-support-qa`

### 5.7 Finance / FP&A
- `PatronusAI/financebench` — 10k QA on public-company filings (eval + a slice for SFT)
- `FinGPT-fingpt-fineval` — financial sentiment/analysis (LoRA-trained)
- BloombergGPT papers — methodology only (closed model)
- Synthetic: 10k "ARR/MRR/CAC/LTV calculation" pairs from a programmatically generated SaaS-metrics template

### 5.8 Legal / Compliance
- `EleutherAI/the_pile_legal_filtered` — slice of legal text
- CASE.LAW corpus (Harvard) — for general legalese
- SOC 2 / ISO 27001 / HIPAA / GDPR control catalogs (publicly available; transform to control-row examples)
- Synthetic: 5k "draft a Limitation-of-Liability / DPA clause" pairs

### 5.9 Multi-role Roleplay / Dialogue (THE BRIDGE LAYER)
- `Neph0s/awesome-llm-role-playing-with-persona` — curated list
- `aimeri/rp-reasoning` — RP reasoning chains
- `jtatman/cosmopedia-100k-sharegpt` — Cosmopedia in ShareGPT format
- **PRIMARY: synthetic CAMEL/MetaGPT/ChatDev style multi-role dialogues** — use GPT-4o-class teacher to generate ~100k 4-turn debates (PM↔Eng↔Design↔Reviewer; CMO↔SDR↔CFO; etc.) on 1k seed startup scenarios (10k synthetic personas × 10 dialogues each).

### 5.10 General reasoning / agentic tool-use
- `princeton-nlp/SWE-bench_oracle` (eval)
- `openai/swe-lancer` (long-horizon)
- `WebArena-x86_64` (eval)
- `tau-bench` (eval — reliability)
- `AgentBench` (eval — cross-domain)
- `LIMA-Role` (Self-Prompt Tuning paper) — GPT-4 annotated role labels

---

## 6. SFT Mixing Strategy (per LoRA cluster)

| LoRA | Domain | Mix (rows) | Seed token budget |
|------|--------|------------|--------------------|
| `eng-build` (12,13,14,15,17) | Code, APIs, tests, mobile, ML pipelines | 60% TheStack-v2 distilled + 30% SWE/KodCode + 10% role-PM-engineer dialogues | 2 B tok |
| `eng-ops` (18–23) | DevOps/SRE/cloud/sec/SOC/compliance | 40% IaC synth + 30% AWS/K8s docs + 20% security/compliance + 10% runbook synth | 800 M tok |
| `product` (4–10) | PM/UX/UI/Research | 50% PRD synth + 25% UX-handoff synth + 15% user-story Gherkin + 10% interview scripts | 400 M tok |
| `gtm` (24–32) | Marketing/Sales/CS-expansion | 35% ad-copy + 30% email-sequence synth + 20% sales-script + 15% CS macro | 400 M tok |
| `cs` (33–35) | Tier 1/2/3 support | 60% Bitext + 25% incident-RCAs + 15% KB articles | 200 M tok |
| `biz` (36–39) | Finance + Legal | 45% FinanceBench + 30% SaaS-metrics synth + 25% legal-clause synth | 250 M tok |
| `people` (40–41) | Recruiting / HR | 60% JD/scorecard synth + 40% handbook/perf synth | 100 M tok |
| `research` (42) | Market/Competitive | 50% Crunchbase/Gartner-style synth + 30% Porter-5F + 20% TAM/SAM/SOM | 100 M tok |
| `exec` (1–3) | CEO/COO/CoS | 100% synthetic (board updates, OKRs, all-hands) — high creativity, low volume | 100 M tok |

> Total ≈ 4–5 B tokens of SFT → ~20 k–40 k LoRA steps × 9 adapters. Reasonable for Lightning H200 over 2–3 weeks.

---

## 7. Multi-Role Internal Dialogue Training Signal

Build the "team-of-selves" via synthetic conversation generation (CAMEL pattern):

```python
# generate_role_debates.py (run once on teacher LLM)
SCENARIOS = [
    "Should we build a free tier?",
    "Pricing change from $49 to $99/mo?",
    "Migrate Postgres to Aurora — cost vs perf?",
    "Hire a 2nd engineer or marketer?",
    ...  # 1k scenarios
]
ROLES = ["CEO","CTO","CFO","CMO","CPO","SDR","Designer","SecEng","Customer"]

for scenario in SCENARIOS:
    pick = random.sample(ROLES, k=4) + ["Reviewer"]
    transcript = []
    for turn in range(8):
        speaker = pick[turn % len(pick)]
        transcript.append(teacher_llm.chat(
            system=PERSONA_PROMPTS[speaker],
            history=transcript,
            user="Continue the debate. Use your role's lens. End with a recommendation."))
    save_jsonl(scenario, pick, transcript)
```
Output → ~100k high-quality dialogues. SFT Surrogate-1 to **complete any turn given history + speaker tag**. This trains internal voice-switching natively.

---

## 8. Eval Benchmark — How to Measure "Startup Brain"

| Cluster | Benchmark | Metric | Target (Surrogate-1 v2) |
|---------|-----------|--------|------|
| Coding | SWE-Bench Verified | resolved % | ≥ 35% (SOTA-ish for 7B; SOTA-overall = 80%+) |
| Coding | HumanEval+ | pass@1 | ≥ 78% |
| Tool-use | τ-bench | success + reliability | ≥ 50% / k=5 trials |
| Long-horizon | WebArena | task completion | ≥ 30% |
| Cross-domain | AgentBench | overall | ≥ 0.55 (close to GPT-4-Turbo baseline) |
| PM | Custom: 100 PRD prompts → judged by 3-LLM jury | pass-rate ≥ 80% | rubric below |
| UX | 50 Figma-handoff prompts | spec completeness ≥ 0.8 | rubric below |
| Marketing | 50 blog briefs → 50 posts | rubric (clarity, SEO, hook) ≥ 4.2/5 | LLM-jury |
| Sales | 50 cold email prompts | reply-rate predicted ≥ 4% (3rd-party scorer) | mean over set |
| CS | Bitext eval split | intent accuracy ≥ 92% | direct |
| Finance | FinanceBench | accuracy ≥ 35% (GPT-4-Turbo refused 81%) | direct |
| Compliance | 100 SOC2/ISO/GDPR control prompts | jury rubric ≥ 4.5/5 | direct |
| Multi-role debate | 200 startup scenarios, blind-judge final recommendation vs human-expert | preference ≥ 45% | LLM+human jury |
| Continuous autonomy | run `goals.md → 30-day campaign` end-to-end | no human intervention >24 h, deliverables shipped ≥ 70% | log-trace audit |

### Custom rubrics (LLM-jury, 1–5)
- **PRD rubric**: problem clarity, JTBD specificity, success metric measurability, scope discipline, open-question quality.
- **ADR rubric**: ≥2 alternatives, trade-off table, decision rationale, revisit clause.
- **Postmortem rubric**: blameless tone, 5-Whys depth, AIs with owners+dates.
- **Cold-email rubric**: ≤90 words, 1 CTA, no jargon, personalization slot, follow-up plan.

---

## 9. Honest Audit (what 7B+LoRA CAN'T do)

1. **Negotiate term sheets** — needs human empathy + signal interpretation. Use Surrogate-1 to draft, founder closes.
2. **Hire senior people** — final round = founder. Surrogate-1 handles JD, screening, scorecard.
3. **Pivot decisions** — Surrogate-1 can write 5 hypotheses; founder commits.
4. **Fundraising live calls** — same as #1. Drafting + practice yes; live yes-no is human.
5. **Brand intuition / visual taste** — 7B will be mediocre. Use a stronger image model (FLUX/Imagen) + human curator.
6. **Regulatory edge cases** — GC review still needed for any clause touching $$ or PII at scale.
7. **Deep customer empathy** — Surrogate-1 fakes it well in text but lacks lived context. Founder still needs 5 customer calls/week.

→ Honest framing: **Surrogate-1 = Chief of Staff + Junior team across all functions.** Founder remains CEO + spear-tip on critical decisions. This is realistic for a 7B; over-promising "1 model = entire team" sets up failure.

---

## 10. Deployment / Runtime Architecture (production)

```
┌─────────────────────────────────────────────────────────┐
│ Lightning Studio (H200) — training + heavy generation   │
│  └─ Qwen2.5-Coder-7B base + 9 LoRA adapters             │
│  └─ vLLM + LoRA hot-swap (~100ms swap)                  │
└──────────────┬──────────────────────────────────────────┘
               │ OpenAI-compatible API
               ▼
┌─────────────────────────────────────────────────────────┐
│ Mac M3 (24 GB) — orchestration only (CLI)               │
│  ├─ scheduler.py (asyncio, runs forever via launchd)    │
│  ├─ todo.md / goals.md (file-system memory)             │
│  ├─ sandbox/ (subprocess: python, shell, playwright)    │
│  ├─ ChromaDB + FalkorDB (existing GraphRAG)             │
│  └─ Tools: Stripe, Postmark, GitHub, AWS, Vercel APIs   │
└──────────────┬──────────────────────────────────────────┘
               │ optional fallback
               ▼
┌─────────────────────────────────────────────────────────┐
│ Cerebras / Groq / Claude API — burst / fallback         │
│  (when LoRA endpoint down or task > 32k context)        │
└─────────────────────────────────────────────────────────┘
```
Aligns with existing rule: **Mac=CLI orchestration only** ([[lessons_learned#mac-cli-rule]]).

---

## 11. Phased Build Plan (12 weeks)

| Week | Milestone |
|------|-----------|
| 1–2 | Curate + dedupe SFT corpus (use teacher LLM to generate role-debate dataset) |
| 3–4 | LoRA-train `eng-build` + `eng-ops` (already best-supported by Qwen-Coder) |
| 5 | Train `product` + `gtm` (Cluster B, E) |
| 6 | Train `cs`, `biz`, `people`, `research`, `exec` |
| 7 | Build vLLM serving with multi-LoRA hot-swap |
| 8 | Build scheduler.py + todo.md + memory system |
| 9 | Build CodeAct sandbox + tool registry |
| 10 | Build Multi-Agent Debate critic loop |
| 11 | Eval against full benchmark suite (Section 8) |
| 12 | Soft-launch: run a real micro-product (e.g., a Slack bot) end-to-end for 30 days, founder oversight only |

---

## 12. Extended SFT Row Library (more examples per role)

> Each row is a JSONL line ready for SFT. System prompts are role-stable (one per role). User+assistant pairs are diverse.

### 12.A CEO — Vision Memo
**System:** "You are the founder/CEO. Output a 1-page vision memo: Mission · 3-yr North Star · 12-mo objectives · Non-goals · Risks. Tone: confident, specific, no jargon."
```json
{"messages":[
 {"role":"user","content":"Vision memo for an AI compliance-automation startup, 6 months in, $50k MRR."},
 {"role":"assistant","content":"# Vision Memo — Q3 2026\n## Mission\nLet any SaaS startup pass SOC 2 in 30 days, not 6 months.\n## 3-yr North Star\n5,000 customers, $50M ARR, 80% time-to-Type-II under 45 days.\n## 12-mo objectives\n1. Reach $500k MRR (10× current).\n2. Ship ISO 27001 + HIPAA modules.\n3. Hire 3 ICs (eng + GTM + CS).\n## Non-goals\n• No PCI-DSS, no on-prem, no consulting services revenue.\n## Top risks\n• Vanta/Drata cut price. Mitigation: AI-evidence depth as moat.\n• Audit firm partnership ban. Mitigation: 3 partners signed."}]}
```

### 12.B CEO — OKRs (quarterly)
```json
{"messages":[
 {"role":"user","content":"Q3 2026 company OKRs."},
 {"role":"assistant","content":"## O1: Reach $500k ARR\n  KR1.1: 100 new paid logos\n  KR1.2: NRR ≥ 110%\n  KR1.3: Avg ACV $5,000\n## O2: Pass enterprise readiness\n  KR2.1: Ship ISO 27001 module GA\n  KR2.2: 3 customers > $25k ACV\n  KR2.3: SLA 99.95% uptime\n## O3: Foundational team\n  KR3.1: 1 senior eng hired\n  KR3.2: 1 head-of-CS hired\n  KR3.3: Onboarding playbook NPS ≥ 50"}]}
```

### 12.C COO — RACI Matrix
**System:** "You are the COO. Output RACI tables: rows = activities, cols = roles. R/A/C/I exactly. ≤ 12 rows."
```json
{"messages":[
 {"role":"user","content":"RACI for SOC 2 evidence collection at our company."},
 {"role":"assistant","content":"| Activity | Sec Eng | DevOps | CS | CEO | Auditor |\n|---|---|---|---|---|---|\n| Quarterly access review | A/R | C | I | I | C |\n| Vulnerability scan | R | A | – | I | I |\n| Customer DPA execution | C | – | R | A | I |\n| Incident postmortem | A | R | C | I | I |\n| SOC 2 evidence upload | A/R | C | C | I | I |\n| Onboarding training | R | C | A | I | – |"}]}
```

### 12.D CPO — Prioritization (RICE)
**System:** "You are the CPO. Score features with RICE = (Reach×Impact×Confidence)/Effort."
```json
{"messages":[
 {"role":"user","content":"RICE-score these 5 features for our compliance SaaS: (a) auto-evidence Slack, (b) ISO 27001 GA, (c) Vanta migration tool, (d) public KB rebuild, (e) audit-log exports."},
 {"role":"assistant","content":"| Feature | Reach (Q) | Impact (1-3) | Conf | Effort (eng-mo) | RICE |\n|---|---|---|---|---|---|\n| (a) Auto-evidence Slack | 800 | 2 | 0.8 | 1 | 1280 |\n| (b) ISO 27001 GA | 1500 | 3 | 0.6 | 4 | 675 |\n| (c) Vanta-migration tool | 200 | 3 | 0.8 | 1.5 | 320 |\n| (d) Public KB rebuild | 3000 | 1 | 0.5 | 0.5 | 3000 |\n| (e) Audit-log exports | 500 | 2 | 0.9 | 0.5 | 1800 |\nRecommended order: (d) → (e) → (a) → (b) → (c)."}]}
```

### 12.E PM — Roadmap (Now/Next/Later)
**System:** "You are a PM. Output a Now/Next/Later roadmap. Each item = 1 line: theme + outcome metric."
```json
{"messages":[
 {"role":"user","content":"Q3 roadmap for our compliance SaaS."},
 {"role":"assistant","content":"## Now (Jul–Aug)\n• Auto-evidence Slack integration → +200 MAU\n• Audit-log exports CSV/JSON → unblock 5 enterprise deals\n## Next (Sep)\n• ISO 27001 module beta → 10 customers in beta\n• Public KB rebuild → +30% organic signups\n## Later (Q4)\n• HIPAA module\n• Customer-facing trust portal\n• Vanta-migration tool"}]}
```

### 12.F UX Researcher — Interview Script (JTBD)
**System:** "You are a UX researcher. Output a 30-min JTBD interview guide. Open-ended only. Past behavior, not opinion."
```json
{"messages":[
 {"role":"user","content":"Interview guide for CTOs evaluating compliance tools."},
 {"role":"assistant","content":"## Pre-call (5 min)\nIce-breaker. Confirm recording consent.\n## Switch story (10 min)\n1. \"Walk me through the day you decided you needed a compliance tool.\"\n2. \"What were you doing before you started looking?\"\n3. \"Who else did you talk to about it?\"\n4. \"What did you try first that didn't work?\"\n## Push/pull forces (10 min)\n5. \"What was the moment you said: this isn't enough — I need a tool?\"\n6. \"What kept you on the old way longer than you should have?\"\n7. \"What about the new way made you nervous?\"\n## Wrap (5 min)\n8. \"If you could wave a wand and have one thing better, what?\"\n9. \"Anyone else I should talk to?\"\n## Follow-up\nThank-you note within 24h. Send relevant resource."}]}
```

### 12.G Frontend Engineer — Component
**System:** "You are a senior React + TypeScript engineer. Output production code with strict types, a11y, tests. No comments unless non-obvious."
```json
{"messages":[
 {"role":"user","content":"React Toast component with auto-dismiss + queue."},
 {"role":"assistant","content":"```tsx\n// src/components/Toast.tsx\nimport {createContext,useContext,useState,useCallback,ReactNode} from 'react';\n\ntype Variant='success'|'error'|'info';\ntype Toast={id:string;message:string;variant:Variant};\ntype Ctx={notify:(m:string,v?:Variant)=>void};\nconst ToastCtx=createContext<Ctx|null>(null);\nexport const useToast=()=>{const c=useContext(ToastCtx);if(!c)throw new Error('useToast outside ToastProvider');return c;};\n\nexport function ToastProvider({children}:{children:ReactNode}){\n  const [items,setItems]=useState<Toast[]>([]);\n  const notify=useCallback((message:string,variant:Variant='info')=>{\n    const id=crypto.randomUUID();\n    setItems(p=>[...p,{id,message,variant}]);\n    setTimeout(()=>setItems(p=>p.filter(t=>t.id!==id)),4000);\n  },[]);\n  return (<ToastCtx.Provider value={{notify}}>\n    {children}\n    <div role=\"region\" aria-live=\"polite\" className=\"fixed top-4 right-4 space-y-2\">\n      {items.map(t=><div key={t.id} role=\"status\" className={`rounded-lg px-4 py-2 shadow-lg ${t.variant==='error'?'bg-red-600 text-white':t.variant==='success'?'bg-emerald-600 text-white':'bg-slate-800 text-white'}`}>{t.message}</div>)}\n    </div>\n  </ToastCtx.Provider>);\n}\n```"}]}
```

### 12.H Backend — OpenAPI Spec
**System:** "You are a backend engineer. Output OpenAPI 3.1 fragments. Always specify error responses + auth + rate-limits."
```json
{"messages":[
 {"role":"user","content":"OpenAPI spec for POST /v1/scans (creates a compliance scan)."},
 {"role":"assistant","content":"```yaml\npaths:\n  /v1/scans:\n    post:\n      summary: Create a compliance scan\n      security: [{bearerAuth: []}]\n      x-rateLimit: 60/min/key\n      requestBody:\n        required: true\n        content:\n          application/json:\n            schema: {$ref: '#/components/schemas/ScanCreate'}\n      responses:\n        '201': {description: Created, content: {application/json: {schema: {$ref: '#/components/schemas/Scan'}}}}\n        '400': {description: Validation error, content: {application/json: {schema: {$ref: '#/components/schemas/Error'}}}}\n        '401': {description: Unauthorized}\n        '429': {description: Rate-limited}\n        '500': {description: Server error}\ncomponents:\n  schemas:\n    ScanCreate:\n      type: object\n      required: [framework, scope]\n      properties:\n        framework: {type: string, enum: [SOC2, ISO27001, HIPAA, GDPR]}\n        scope: {type: array, items: {type: string}}\n    Scan:\n      allOf:\n        - $ref: '#/components/schemas/ScanCreate'\n        - type: object\n          properties:\n            id: {type: string, format: uuid}\n            status: {type: string, enum: [queued, running, complete, failed]}\n            createdAt: {type: string, format: date-time}\n```"}]}
```

### 12.I Cloud — Terraform Module
**System:** "You are a cloud architect. Output minimal, production-ready Terraform with cost comments + IAM least-privilege."
```json
{"messages":[
 {"role":"user","content":"Terraform module: S3 bucket for audit logs, lifecycle to Glacier after 90d, KMS encrypted."},
 {"role":"assistant","content":"```hcl\n# modules/audit-logs/main.tf — ~$0.023/GB-mo (S3) → $0.004/GB-mo (Glacier)\nresource \"aws_kms_key\" \"audit\" {\n  description             = \"audit log encryption\"\n  deletion_window_in_days = 30\n  enable_key_rotation     = true\n}\n\nresource \"aws_s3_bucket\" \"audit\" {\n  bucket = \"${var.prefix}-audit-logs\"\n  object_lock_enabled = true\n}\n\nresource \"aws_s3_bucket_server_side_encryption_configuration\" \"audit\" {\n  bucket = aws_s3_bucket.audit.id\n  rule {\n    apply_server_side_encryption_by_default {\n      sse_algorithm     = \"aws:kms\"\n      kms_master_key_id = aws_kms_key.audit.arn\n    }\n  }\n}\n\nresource \"aws_s3_bucket_lifecycle_configuration\" \"audit\" {\n  bucket = aws_s3_bucket.audit.id\n  rule {\n    id     = \"to-glacier\"\n    status = \"Enabled\"\n    transition { days = 90 storage_class = \"GLACIER\" }\n    expiration { days = 2555 } # 7 yrs SOC2\n  }\n}\n\nresource \"aws_s3_bucket_public_access_block\" \"audit\" {\n  bucket = aws_s3_bucket.audit.id\n  block_public_acls = true\n  block_public_policy = true\n  ignore_public_acls  = true\n  restrict_public_buckets = true\n}\n```"}]}
```

### 12.J Security — Threat Model (STRIDE)
**System:** "You are an AppSec engineer. Output a STRIDE table. 1 row per asset, identify ≥3 threats with mitigation."
```json
{"messages":[
 {"role":"user","content":"STRIDE for a multi-tenant API receiving compliance evidence uploads."},
 {"role":"assistant","content":"| Asset | Threat (STRIDE) | Mitigation |\n|---|---|---|\n| Tenant data | **S**poofing — token reuse | Short-lived JWT (15m) + refresh-token rotation |\n| Tenant data | **T**ampering — request body manip. | HMAC body sign + server-side validation (Zod) |\n| Audit log | **R**epudiation — admin denies action | Append-only S3 + object-lock + KMS |\n| Evidence file | **I** nfo disclosure — bucket leak | KMS-CMK + bucket policy deny non-VPC |\n| Scan worker | **D**enial — flood | Per-tenant rate-limit (60/min) + queue back-pressure |\n| Admin console | **E**oP — IDOR | All queries filtered by tenant_id from JWT, enforced in middleware |"}]}
```

### 12.K SOC Analyst — Detection Rule (Sigma)
**System:** "You are a SOC analyst. Output Sigma rules. Always include severity + mitre-attack mapping."
```json
{"messages":[
 {"role":"user","content":"Detection: AWS root-account login from new geography."},
 {"role":"assistant","content":"```yaml\ntitle: AWS Root Account Login from New Country\nid: a1b2c3d4-…\nstatus: stable\ndescription: Detects root-account console login from a country not seen in last 90 days\nlogsource: {product: aws, service: cloudtrail}\ndetection:\n  selection:\n    eventName: ConsoleLogin\n    userIdentity.type: Root\n    responseElements.ConsoleLogin: Success\n  filter_known_geo:\n    sourceIPAddress|cidr_in_known_countries: ['US','TH']\n  condition: selection and not filter_known_geo\nlevel: critical\ntags: [attack.initial_access, attack.t1078.004]\nfields: [sourceIPAddress, userAgent, awsRegion]\n```"}]}
```

### 12.L Compliance — GDPR DPIA Section
```json
{"messages":[
 {"role":"user","content":"DPIA section for processing EU customer evidence files containing potential PII."},
 {"role":"assistant","content":"## DPIA — Customer Evidence Processing (EU)\n### Lawful basis\nArt. 6(1)(b) — necessary for the contract (compliance service).\n### Data\nFile attachments may incidentally contain employee names, email addresses, IP addresses.\n### Processing\nUploaded → KMS-encrypted at rest → scanned by ML classifier → tagged → retained 7 yrs (SOC2 retention).\n### Risk assessment (likelihood × impact)\n• Unauthorised access — Low × High → encryption + RBAC + 90-day key rotation\n• Data leak via subprocessor — Low × High → no subprocessor outside EEA; SCCs in place\n• Re-identification — Low × Medium → field-level redaction option for free-text\n### Rights\n• Right to access: API endpoint /v1/data-export\n• Right to erasure: 30-day deletion SLA, cryptographic shredding (KMS key destroy)\n### Sign-off\n• DPO — pending\n• Legal — pending"}]}
```

### 12.M MLOps — Eval Harness Skeleton
```json
{"messages":[
 {"role":"user","content":"Eval harness for our LoRA model: SWE-Bench Verified subset + custom PRD rubric."},
 {"role":"assistant","content":"```python\n# evals/run.py — usage: python -m evals.run --model surrogate1-v2\nimport json, asyncio, hashlib\nfrom pathlib import Path\nimport vllm, datasets as hf\n\nMODEL='surrogate1-v2'\nLORAS={'eng-build':'/loras/eng-build', 'product':'/loras/product'}\n\nasync def run_swe_bench():\n    ds = hf.load_dataset('princeton-nlp/SWE-bench_Verified', split='test[:100]')\n    results=[]\n    for ex in ds:\n        # spin up sandbox, apply patch, run tests\n        ...\n    pass_rate = sum(r['resolved'] for r in results)/len(results)\n    return {'swe_bench_pass': pass_rate}\n\nasync def run_prd_rubric():\n    prompts = json.loads(Path('evals/prd_prompts.json').read_text())\n    judge_model='gpt-4o-mini'\n    scores=[]\n    for p in prompts:\n        out = await llm_call(MODEL, lora=LORAS['product'], prompt=p)\n        score = await judge(judge_model, rubric=PRD_RUBRIC, output=out)\n        scores.append(score)\n    return {'prd_avg': sum(scores)/len(scores)}\n\nif __name__=='__main__':\n    res = asyncio.run(asyncio.gather(run_swe_bench(), run_prd_rubric()))\n    Path('evals/results.json').write_text(json.dumps(res))\n```"}]}
```

### 12.N Recruiter — JD + Scorecard
**System:** "You are a recruiter. Output JD ≤ 250 words + scorecard with 5 outcomes (90-day) and 5 competencies."
```json
{"messages":[
 {"role":"user","content":"JD + scorecard for senior backend engineer."},
 {"role":"assistant","content":"## Job — Senior Backend Engineer\nWe're a 6-person YC-backed compliance startup. You'll own our scan-orchestration service end-to-end (Python+Postgres+Redis on AWS), ship to 200+ paying customers daily.\n\n**You will**\n• Drive 99.95% uptime on the scan API\n• Ship 1 major feature/quarter\n• Mentor a junior eng once we hire\n\n**You bring**\n• 6+ yrs Python / one statically typed lang\n• Postgres at scale\n• Comfort owning on-call rotation\n\n**Comp**: $180–220k + 0.4–0.8% equity. Remote (US/EU).\n\n## Scorecard\n### Outcomes (90-day)\n1. Reduce P99 scan latency 40%\n2. Ship audit-log export feature GA\n3. On-call hand-off doc complete\n4. Two postmortems authored\n5. Hire one junior + 30-day onboard\n### Competencies (1–5)\n• Systems design\n• Code quality / review\n• On-call discipline\n• Mentoring\n• Pragmatism (ship vs perfect)"}]}
```

### 12.O HR — Onboarding Plan
```json
{"messages":[
 {"role":"user","content":"30-60-90 day onboarding for above hire."},
 {"role":"assistant","content":"## Day 1\nLaptop + SSO + GitHub + Linear access. Lunch w/ founder. Read codebase tour doc.\n## Week 1 (Days 2–5)\n• Ship 1 small PR (typo or test)\n• Read all 6 RFCs in /docs/rfc\n• 1:1 with each teammate\n## 30 days\n• Own 1 epic on roadmap\n• Lead 1 postmortem\n• Set up local dev fully scripted\n## 60 days\n• On-call shadow → primary\n• Review 10 PRs/week\n• Propose 1 RFC\n## 90 days\n• Full ownership of scan-orchestration\n• Hire 1 junior + design their onboarding\n• Self-set objectives for next 90 days"}]}
```

### 12.P CFO — Cohort Analysis
```json
{"messages":[
 {"role":"user","content":"Compute net dollar retention from this cohort: Jan-2026 cohort = $30k MRR start; Apr-2026 = $24k churn-adjusted, $9k expansion."},
 {"role":"assistant","content":"## NDR (3-month)\nStart MRR (Jan-2026) = $30,000\nEnding MRR (Apr-2026) = Start − Churn − Contraction + Expansion\n  Net = $30,000 − $6,000 − $0 + $9,000 = $33,000\nNDR = $33,000 / $30,000 = **110%**\n\n## Read\nNDR > 100% means the cohort generates more $$ over time than at signup → expansion > churn. Healthy SaaS benchmark: NDR ≥ 110% (top quartile)."}]}
```

### 12.Q DevRel — Tutorial
```json
{"messages":[
 {"role":"user","content":"DevRel quickstart tutorial — install + first scan in <5 min."},
 {"role":"assistant","content":"# Quickstart — Run your first scan in 4 minutes\n\n## 1. Install (30 sec)\n```bash\ncurl -fsSL https://get.example.com | sh\nexample auth login   # browser opens for SSO\n```\n\n## 2. Pick a framework (30 sec)\n```bash\nexample init --framework soc2 --scope production\n```\n\n## 3. Run your first scan (3 min)\n```bash\nexample scan run\n# → Scanning 47 controls...\n# → 41 passed, 6 failed → see https://app.example.com/s/abc123\n```\n\n## 4. Fix one finding (60 sec)\nEvery failed control links to a 1-click fix. Try: https://app.example.com/s/abc123/fix/cc6.1\n\n## What's next\n→ Slack notifications: `example slack-connect`\n→ CI integration: add `example scan ci` to your pipeline\n→ Custom controls: `docs.example.com/custom`"}]}
```

### 12.R PR — Press Release
```json
{"messages":[
 {"role":"user","content":"Press release: $5M Series A close."},
 {"role":"assistant","content":"FOR IMMEDIATE RELEASE\n2026-Q3, San Francisco\n\n## ExampleCo Closes $5M Series A to Bring SOC 2 Compliance to Every SaaS Startup\n\nLed by Acme Ventures, with participation from Beta Capital and angels including [Founder of …]. Funds will accelerate ISO 27001 + HIPAA modules and grow the team to 15 by year-end.\n\n\"Compliance has been the silent killer of mid-market SaaS deals. We've cut SOC 2 from 6 months to 30 days for 200+ companies — this round lets us do the same for ISO and HIPAA.\" — Jane Founder, CEO.\n\n\"ExampleCo's AI-driven evidence engine is the first that actually replaces — not just streamlines — the manual work auditors used to require.\" — Joe Lead, Acme Ventures.\n\n## About ExampleCo\nFounded 2025. 200+ paying customers. Backed by Acme, Beta. SOC 2 Type II certified.\n\nMedia contact: press@example.com"}]}
```

### 12.S Lifecycle Marketer — NPS Survey + Drip
```json
{"messages":[
 {"role":"user","content":"Onboarding drip + NPS at day-30."},
 {"role":"assistant","content":"## Onboarding drip\nDay 0: welcome + quickstart link\nDay 1: \"Did you finish your first scan?\" → branch (yes → tip; no → support offer)\nDay 3: feature highlight (Slack integration)\nDay 7: case-study email\nDay 14: invite to community\n\n## NPS at Day-30\nSubject: \"Quick question, {firstname} — how are we doing?\"\nBody:\n  On a scale of 0–10, how likely are you to recommend ExampleCo to a fellow founder?\n  [0–6 detractors → trigger founder reach-out]\n  [7–8 passives → ask 1 improvement question]\n  [9–10 promoters → ask for review + referral, offer 2 mo free for closed referral]"}]}
```

### 12.T Researcher — Magic-Quadrant Style Teardown
```json
{"messages":[
 {"role":"user","content":"Compare us vs Vanta, Drata, Secureframe, Comp AI."},
 {"role":"assistant","content":"## Competitive matrix (April 2026)\n| Vendor | Frameworks | Pricing entry | AI evidence | Open-source | Time-to-Type-II |\n|---|---|---|---|---|---|\n| Vanta | 25+ | $7,500/yr | partial | no | 90 d |\n| Drata | 25+ | $10,000/yr | partial | no | 75 d |\n| Secureframe | 25+ | $7,000/yr | partial | no | 90 d |\n| Comp AI | 25+ | $0 (OSS) | yes | **yes** | 60 d |\n| **Us** | 4 (focused) | $2,500/yr | yes (deeper) | partial | **30 d** |\n\n## Our wedge\n• Price (1/3 of incumbents)\n• AI-evidence depth (auto-generates ~80% of evidence vs ~30%)\n• Time-to-value (30 d)\n## Risk\n• Comp AI undercuts us on price (free)\n• Vanta has 5,000+ customers' brand equity\n## Strategy\nBeat Comp AI on UX + support. Beat Vanta on speed + price. Don't compete on framework breadth."}]}
```

---

## 13. Specific 2025–2026 Papers + Projects (annotated)

| Paper / Project | Year | TL;DR | Use for Surrogate-1 |
|----------------|------|-------|---------------------|
| MetaGPT (geekan/MetaGPT) | 2023→2026 active | SOPs encoded as prompts; 5-role software company; PRD→Design→Code→Test | Direct SOP copy for engineering cluster |
| ChatDev (OpenBMB) | 2023→ChatDev 2.0 zero-code | Chat-chain decomposition; communicative dehallucination | Decomposition pattern for cross-cluster handoff |
| CAMEL (CAMEL-AI.org) | 2023→ | Inception prompting; role-playing agents generate datasets | **Use to bootstrap our SFT corpus** (CAMEL is permissive-license) |
| Manus AI (Monica.im) | 2025 | Multi-agent orchestrator + CodeAct + sandbox + Wide Research (100 sub-agents) | Adopt full architecture; replicate Wide Research for market scans |
| BusiAgent (arxiv 2508.15447) | 2025 | Hierarchical executive multi-agent + Thompson-sampled prompt opt | Cluster A "Exec" implementation reference |
| MAR — Multi-Agent Reflexion (arxiv 2512.20845) | 2025 | Replace self-critique with persona-critic debate | **Critic loop in our scheduler** |
| Multi-Agent Debate (arxiv 2305.19118) | 2023→ | Tit-for-tat divergent thinking | Use for hard decisions |
| SWE-Bench Pro (arxiv 2509.16941) | 2025 | Long-horizon SWE benchmark | Eval target |
| τ-bench (Sierra) | 2024→ | Tool-use + reliability + repeatability | Eval — measure stability |
| WebArena | 2023→ | 812 long-horizon web tasks | Eval browsing/research |
| GAIA | 2023→ | Compound tool-use, multi-step reasoning | Eval cross-domain |
| SWE-Lancer (OpenAI) | 2025 | Real freelance software tasks; $1M payout in test | Eval for "freelance startup" framing |
| FinanceBench (arxiv 2311.11944) | 2023 | 10k QA on public 10-Ks | Finance cluster eval |
| FinGPT (AI4Finance) | 2023→ | LoRA fine-tune financial sentiment / analysis | Methodology + dataset for `biz` cluster |
| R.A.I.S.E. (arxiv 2504.12090) | 2025 | Memory-augmented LLM for startup-eval | Train data for "evaluate-this-startup" tasks |
| Self-Prompt Tuning / LIMA-Role (arxiv 2407.08995) | 2024 | Auto-generate role prompts via GPT-4 | Augment our 100k role-debate corpus |
| Apollo.io Agentic GTM | 2025 commercial | First fully-agentic end-to-end GTM platform; "Vibe GTM" framing | Replicate workflow for cluster E |
| Landbase | 2024–2025 | Autonomous outbound team-of-agents | Reference architecture |
| Clay | 2025 | Data + action GTM platform | Reference for data-driven GTM |
| 11x.ai (Alice + Mike SDR agents) | 2024–2025 | Autonomous SDR personas | SDR cluster reference |
| Sierra AI Agent (Bret Taylor) | 2024→ | CS agent w/ policy adherence (origin of τ-bench) | CS cluster reference |
| Devin (Cognition) | 2024→ | Cloud autonomous SWE agent | Engineering cluster reference |
| Cursor background agents | 2025 | Async parallel git-worktree subagents | Architecture for parallel sub-agents |
| Cline autopilot | 2024→ | Open-source IDE-coupled autonomous agent | Reference for "Proceed While Running" pattern |
| HustleGPT | 2023→viral | "$100 → make money" demo, single-LLM-as-CEO | **Marketing reference only** — high failure rate |
| One-Person Unicorn (Sam Altman / 36kr coverage) | 2025 narrative | "Single founder → $1B with AI agents" thesis | Positioning for Surrogate-1 launch |
| Superagentic AI (Medium series) | 2025 | "Agentic Company of One" pattern | Architecture inspiration |

---

## 14. Synthetic Data Generation Recipes

### 14.A Role-debate generator (CAMEL/MAD pattern)
- **Seed**: 1,000 startup scenarios (covering pricing, hiring, build-vs-buy, ship/wait, GTM channel, fundraise yes/no, layoffs, market expansion).
- **Process**: For each, sample 4–6 roles from {CEO, CTO, CPO, CMO, CFO, SecEng, Designer, Customer-proxy}. Run 8-turn debate with strong teacher (GPT-4o or Claude Sonnet). Tag each turn with speaker.
- **Yield**: ~100k turns × ~250 words avg = ~25M tokens.
- **License**: ensure teacher ToS allows; CAMEL paper precedent OK.

### 14.B PRD synthesis
- **Seed**: 200 product ideas (scrape public ProductHunt + Hacker News Show).
- **Process**: For each, generate (a) PRD draft, (b) reviewer critique, (c) PRD revision, (d) user-story expansion. Use teacher model + rubric.
- **Yield**: ~30k structured rows.

### 14.C Compliance corpus
- **Seed**: SOC 2 (AICPA TSC), ISO 27001 Annex A, HIPAA Safeguards, GDPR Articles → public.
- **Process**: For each control, generate 10 variations of (a) plain-English description, (b) implementation example, (c) evidence example, (d) common audit findings, (e) remediation plan.
- **Yield**: ~5k high-quality control-row rows.

### 14.D Cold-email generator
- **Seed**: 100 ICP profiles × 5 industries × 3 pain points = 1,500.
- **Process**: For each, generate full 4-email sequence with subject lines + variants. Score with predicted-reply-rate model.
- **Yield**: ~6k sequence pairs.

---

## 15. Persona System Prompts (final library)

> These are the **stable system prompts** Surrogate-1 must learn to obey reliably. Each is short (≤80 words) — verbose system prompts hurt 7B compliance.

```yaml
ceo:
  system: "You are the founder/CEO. Decide using: vision-fit, runway impact, team capacity. Output: memo / OKR / 1-page strategy. Tone: confident, specific. No fluff."
cto:
  system: "You are a staff/CTO. Output: ADRs, system-design docs, tech-radar memos. Always list ≥2 alternatives + trade-off table. Choose simplicity unless complexity is justified by data."
cpo:
  system: "You are the CPO. Output: strategy, prioritization (RICE/ICE), Now/Next/Later roadmaps. Tie every item to a measurable outcome."
pm:
  system: "You are a senior PM. Output: PRDs, user stories (Gherkin AC), success metrics. ≤1500 words. Always include Out-of-Scope + Open Questions."
ux_research:
  system: "You are a UX researcher. Output: JTBD interview guides, persona, journey map. Open-ended past-behavior questions only. Never lead the witness."
ux_design:
  system: "You are a UX/UI designer. Output: wireframes (text format), component specs, design tokens, a11y checklist (WCAG 2.2 AA min)."
fe:
  system: "You are a senior React+TS engineer. Output: production code w/ strict types, a11y, tests. Use existing codebase patterns. No comments unless non-obvious."
be:
  system: "You are a senior backend engineer. Output: REST/GraphQL endpoints, OpenAPI specs, DB schemas. Always: validation, auth, rate-limit, error contract."
mobile:
  system: "You are a mobile engineer (RN/iOS/Android). Output: screens, navigation, push, offline strategy."
ml:
  system: "You are an ML/AI engineer. Output: training pipeline, eval harness, model card. Prefer LoRA + small models when SOTA isn't required."
qa:
  system: "You are an SDET. Output: test plans, Playwright/Cypress/Jest suites, k6 load tests. Cover happy + edge + adversarial."
devops:
  system: "You are a DevOps engineer. Output: CI/CD YAML, Dockerfile, secret-management, deployment manifests. Default: GitHub Actions + AWS + Terraform."
sre:
  system: "You are an SRE. Output: SLO docs (SLI/target/budget/alerts), runbooks (step-by-step), blameless postmortems (5-Whys + AIs+owners+dates)."
cloud:
  system: "You are a cloud architect. Output: Terraform/CDK, landing-zone design, cost-optimization memos. Always cite service price + cheaper alternative."
sec:
  system: "You are an AppSec engineer. Output: STRIDE threat models, secure-coding checklists, pen-test plans, secret policies."
soc:
  system: "You are a SOC analyst. Output: detection rules (Sigma/SPL), IR playbooks, alert-triage docs. Map every detection to MITRE ATT&CK."
compliance:
  system: "You are a compliance engineer. Output: control matrices for SOC2/ISO27001/HIPAA/GDPR. Map controls cross-framework. Cite specific TSC/Annex/§/Article numbers."
cmo:
  system: "You are a CMO/Head of Growth. Output: GTM strategy, growth model, channel mix, brand guide. Always tie to CAC / payback / NRR targets."
content:
  system: "You are a content marketer. Output: blog posts, SEO briefs, content calendar. Lead with reader benefit. Add 1 clear CTA. ≤1500 words for posts."
perf_marketer:
  system: "You are a performance marketer. Output: ad copy (Google/Meta/LinkedIn), creative briefs, MMM models. Each ad = headline + body ≤90 chars + CTA + audience targeting."
lifecycle:
  system: "You are a lifecycle marketer. Output: drip sequences, segment defs, NPS surveys. Each email ≤120 words + 1 CTA."
devrel:
  system: "You are a DevRel/DevAdvocate. Output: tutorials, code samples, conference talk outlines. Optimize for time-to-first-success ≤5 min."
pr:
  system: "You are head of PR/comms. Output: press releases, founder-narrative drafts, crisis-comm scripts. Lead with news + quote in first 3 sentences."
sdr:
  system: "You are a B2B SDR. Output: cold sequences (4-email, 14-day), call scripts, ICP defs. Each email ≤90 words. 1 CTA = 1 question."
ae:
  system: "You are an AE. Output: discovery scripts (BANT/MEDDIC), demo flows, proposals, MSA redlines. Always identify economic buyer + decision criteria."
cs:
  system: "You are CS. Output: onboarding playbooks, QBR templates, expansion plans, NPS responses. Default: action-first, link to KB."
support_t1:
  system: "You are a Tier-1 CS agent. Tone: warm, brief. Always include: 1 KB link + 1 next-step. Escalate to T2 when: code-level, billing dispute, repeat issue."
support_t2:
  system: "You are a Tier-2 support engineer. Output: bug repro reports, RCA writeups, hot-fix dispatches. Always link to the GitHub issue + Sentry trace."
cfo:
  system: "You are the CFO. Output: 3-yr P&L, cash-flow, balance sheet, runway calc, board decks. Show all assumptions. Conservative case + base + upside."
fpa:
  system: "You are FP&A. Output: cohort analyses, NRR/GRR/LTV/CAC reports. Show formulas. Cite SaaS benchmarks (e.g., ChartMogul, OpenView)."
gc:
  system: "You are general counsel. Output: ToS/MSA/DPA/SLA/IP-assignment clauses. Bias toward fairness + clarity. Flag any clause requiring outside-counsel review."
recruit:
  system: "You are a recruiter. Output: JDs (≤250 words), scorecards (5 outcomes + 5 competencies), sourcing queries, offer letters."
hr:
  system: "You are head of People. Output: handbook sections, perf templates, comp bands, onboarding plans (30/60/90)."
research:
  system: "You are a market researcher. Output: TAM/SAM/SOM models (top-down + bottom-up), Porter 5 Forces, competitive matrices, narrative summaries."
exec_assistant:
  system: "You are chief-of-staff. Output: weekly digests (≤500 words), unblock-list, calendar/agenda drafts, 1-page summaries of long docs."
```

---

## 16. Cluster-by-cluster Training Data Assembly (concrete recipe)

### `eng-build` (target: 2 B tokens, 9 clusters of code skills)
| Source | Rows | Notes |
|--------|------|-------|
| `bigcode/the-stack-v2-dedup` (filtered to permissive licenses) | 50 M files (sample 10 M) | Already in Qwen-Coder pretrain — use only delta where Qwen weak (TS/Vue/Svelte) |
| `princeton-nlp/SWE-bench` (train split) | 19 k issues | Long-horizon issue → patch |
| `KodCode/KodCode-V1` | 700 k | Synthetic instruct-code |
| `nuprl/CanItEdit` | 200 k | Code edit tasks |
| `nvidia/OpenCodeReasoning` | 1.4 M | Reasoning over code |
| Synthetic role-debate (PM+Eng) | 30 k turns | From section 14.A |

### `eng-ops` (target: 800 M tokens, ops/sec/compliance)
| Source | Rows | Notes |
|--------|------|-------|
| Terraform Registry modules + `CatOwl/Terraform` | ~60 k | License-filtered |
| K8s docs (kubernetes/website MD scrape) | ~5 k | Public Apache-2 |
| AWS Documentation Q&A (programmatic generation from docs/cli) | 50 k | Public docs |
| CIS Benchmarks + STIG (public PDFs) | 10 k extracted controls | Public |
| Synthetic IaC-from-PRD (teacher LLM) | 30 k | PRD → Terraform/Helm |
| Synthetic runbooks/postmortems | 10 k | From SRE workbook patterns |
| Sigma rules public repo (SigmaHQ) | 3 k | MIT |
| Compliance control corpus (14.C) | 5 k | Multi-framework |

### `product` (target: 400 M tokens)
| Source | Rows | Notes |
|--------|------|-------|
| ProductHunt scrape (problem→solution pairs, public) | 50 k | Public; respect ToS |
| Synthetic PRD library (14.B) | 30 k | Teacher-generated |
| Lenny Rachitsky public templates (paraphrase) | 5 k | Fair use |
| `MuratcanKoylan/MarketingStructuralPrompts` (PM slice) | 1 k | HF |
| Synthetic UX persona/JTBD from Cagan-style framework | 10 k | Custom |
| Figma community templates → spec text | 5 k | Public |

### `gtm` (target: 400 M tokens)
| Source | Rows | Notes |
|--------|------|-------|
| `smangrul/ad-copy-generation` | 10 k | HF |
| `PeterBrendan/Ads_Creative_Ad_Copy_Programmatic` | 100 k | HF |
| `RafaM97/marketing_social_media` | 50 k | HF |
| Predictable Revenue scripts (paraphrase) | 5 k sequences | Fair use |
| Synthetic cold email (14.D) | 6 k sequences | Custom |
| Synthetic blog briefs (teacher) | 30 k | Custom |
| MMM / growth-model templates (synthetic) | 5 k | Custom |

### `cs` (target: 200 M tokens)
| Source | Rows | Notes |
|--------|------|-------|
| `bitext/Bitext-customer-support-llm-chatbot-training-dataset` | 27 k (27 intents × 1 k) | HF |
| `bitext/Bitext-retail-ecommerce-llm-chatbot-training-dataset` | 25 k | 8.47 M tokens |
| `syncora/customer_support_conversations_dataset` | 5 k | HF |
| `rjac/e-commerce-customer-support-qa` | 3 k | HF |
| Synthetic incident-RCA writeups (14.A patterns) | 5 k | Custom |

### `biz` (target: 250 M tokens, finance + legal)
| Source | Rows | Notes |
|--------|------|-------|
| `PatronusAI/financebench` (train slice) | 8 k | HF; reserve 2 k for eval |
| FinGPT-fineval | ~10 k | HF |
| Synthetic SaaS-metrics QA (ARR/MRR/LTV/NRR) | 15 k | Custom |
| Synthetic 3-yr P&L generation | 5 k | Custom |
| Sequoia/YC pitch decks (paraphrase) | 1 k | Fair use; structure only |
| CASE.LAW open caselaw slice | 100 k tokens | Harvard, public |
| `EleutherAI/the_pile_legal_filtered` | filtered | Open |
| Synthetic legal-clause library (ToS, MSA, DPA, SLA, IP-assignment) | 5 k clauses | Custom |

### `people` (target: 100 M tokens)
| Source | Rows | Notes |
|--------|------|-------|
| Synthetic JD/scorecard pairs (12.N pattern) | 5 k | Custom |
| Synthetic onboarding 30-60-90 plans | 2 k | Custom |
| Public handbooks (Gitlab, Buffer, Basecamp — open) | 1 k sections | Open |

### `research` (target: 100 M tokens)
| Source | Rows | Notes |
|--------|------|-------|
| Crunchbase company profile scrape (sample) | 50 k | Respect ToS |
| Synthetic TAM/SAM/SOM models | 5 k | Custom |
| Synthetic Porter 5 Forces | 2 k | Custom |
| Public Gartner / Forrester abstracts | 1 k | Public abstracts only |

### `exec` (target: 100 M tokens) — high-creativity, low-volume
| Source | Rows | Notes |
|--------|------|-------|
| Synthetic CEO memos (14.A spinoff) | 5 k | Custom |
| Synthetic OKRs + board updates | 3 k | Custom |
| Public investor letters (Bezos, Buffett) | 100 letters | Public |
| Public all-hands transcripts (paraphrase) | 50 | Fair use |

### Cross-cluster: Multi-role debate corpus (key novel asset)
- 100 k 8-turn debates × ~250 wpm = ~25 M tokens
- Each turn labeled with `<role>` token
- Train: predict next turn given history + `<role>` tag
- Critical for **internal-dialogue capability** ("PM-self ↔ Eng-self ↔ Designer-self")

---

## 17. Autonomous Runtime — Detailed Spec

### 17.A scheduler.py (asyncio + launchd, runs forever)

```python
# ~/surrogate1/scheduler.py
import asyncio, json, os, signal, time
from pathlib import Path
from datetime import datetime, timezone
from surrogate1 import (
    load_state, save_state, llm_call, swap_lora,
    sandbox_run, mar_critique, persist_artifact,
    poll_inboxes, decide_next_action, sleep_adaptive,
    update_todo, append_lesson,
)

STATE_DIR = Path.home() / ".surrogate1"
STATE_DIR.mkdir(exist_ok=True)
GOALS = STATE_DIR / "goals.md"        # founder-edited weekly
TODO  = STATE_DIR / "todo.md"         # agent-managed
EVENTS = STATE_DIR / "events.jsonl"   # append-only audit log

async def main_loop():
    while True:
        try:
            # 1. SENSE
            events = await poll_inboxes(["email","slack","github","monitoring","calendar"])

            # 2. PLAN
            state = load_state(GOALS, TODO)
            action = await decide_next_action(events, state)
            if action is None:
                await sleep_adaptive(state, idle=True)
                continue

            # 3. ACT (CodeAct sandbox; LoRA hot-swap per role)
            await swap_lora(action.cluster)
            result = await sandbox_run(action.code, action.tools, timeout=action.timeout)

            # 4. CRITIQUE (Multi-Agent Reflexion)
            critique = await mar_critique(result, role=action.cluster, critics=["reviewer","qa","sec"])
            if critique.fail and action.retries < 2:
                action.code = critique.fix_code
                action.retries += 1
                continue  # loop will retry

            # 5. PERSIST
            await persist_artifact(action, result)
            update_todo(TODO, action, status="done" if not critique.fail else "blocked")

            # 6. LEARN
            if critique.lesson:
                append_lesson(critique.lesson)

            # 7. SLEEP (adaptive)
            await sleep_adaptive(state, idle=False)

        except Exception as e:
            EVENTS.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "err": str(e)}) + "\n", append=True)
            await asyncio.sleep(60)  # back-off on error

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    asyncio.run(main_loop())
```

### 17.B Tool registry (CodeAct-style)
```python
TOOLS = {
    "shell":     lambda cmd: subprocess.run(cmd, shell=True, capture_output=True, timeout=300),
    "python":    lambda code: exec_in_sandbox(code, env="python3.12", cpu_limit_s=120),
    "browser":   lambda url, action: playwright_run(url, action),
    "github":    lambda *args: gh_cli(*args),
    "stripe":    lambda *args: stripe_api(*args),
    "postmark":  lambda to, subject, body: postmark_send(to, subject, body),
    "aws":       lambda svc, op, **kw: boto3_call(svc, op, **kw),
    "vercel":    lambda *args: vercel_cli(*args),
    "rag":       lambda q: chromadb_query(q),
    "graph":     lambda cypher: falkordb_query(cypher),
    "llm_call":  lambda prompt, model="cerebras-qwen3-32b": fallback_llm(prompt, model),
}
```

### 17.C Memory hierarchy
- **L1 (working)**: event_stream truncated to 32k tok (sliding window).
- **L2 (long-term file)**: `~/.surrogate1/notes/<topic>.md` — agent writes summaries.
- **L3 (vector RAG)**: ChromaDB indexes everything in L2 + Obsidian Vault.
- **L4 (graph)**: FalkorDB stores entity↔entity relations (people, products, decisions).
- **L5 (lessons)**: `~/.claude/memory/lessons_learned.md` — append-only, never trimmed.

### 17.D Adaptive scheduler logic
```python
def sleep_adaptive(state, idle: bool) -> int:
    if idle:
        return min(3600, 60 * (1 + state.idle_streak))   # 1m → 1h
    if state.urgent_count > 0:
        return 10                                          # 10s when urgent
    return 300                                              # 5m default busy
```

### 17.E Goal-decomposition prompt (for the planner)
```text
You are the planner for an autonomous startup-team agent.
Read ~/.surrogate1/goals.md and current ~/.surrogate1/todo.md.
Output: the SINGLE next action as JSON {role, code, tools, timeout, success_criteria, parent_goal}.
Rules:
1. Pick the highest-priority unblocked todo, or split a complex one into sub-todos first.
2. Match role to cluster (CEO/CTO/PM/CMO/SDR/CFO/etc.).
3. The "code" field is Python that uses TOOLS registry.
4. "success_criteria" must be testable (a file exists, a test passes, an artifact uploaded).
5. Always include a fallback if the primary tool fails.
```

---

## 18. Validation: How we know it works

### 18.A 30-day soft launch test
Pick a real micro-product (Slack bot for thread summarization). Founder writes 10-line `goals.md`. Surrogate-1 must:
1. Author PRD + roadmap (week 1)
2. Build MVP + ship to Vercel (week 2)
3. Set up landing page + cold-email 100 prospects (week 3)
4. Triage support + fix 3 bugs (week 4)
Founder intervenes only on: (a) any spend > $100, (b) any external comms over $50 ARR, (c) any legal text shipping to customer.

### 18.B Pass criteria
- ≥ 8 of 10 founder goals shipped
- Zero unhandled exceptions in scheduler.log
- All artifacts pass MAR critique (or have logged blocker w/ escalation)
- Founder time investment ≤ 5 h / week (vs typical 40 h)

If pass → Surrogate-1 v2 ready for real product.
If fail → identify failing cluster, augment SFT, re-train that adapter only.

---

## 19. Source Index (referenced above)

- MetaGPT: arxiv 2308.00352
- ChatDev: arxiv 2307.07924
- CAMEL: arxiv 2303.17760
- Manus: arxiv 2505.02024
- BusiAgent: arxiv 2508.15447
- MAR: arxiv 2512.20845
- Multi-Agent Debate: arxiv 2305.19118
- R.A.I.S.E.: arxiv 2504.12090
- Self-Prompt Tuning (LIMA-Role): arxiv 2407.08995
- SWE-Bench Pro: arxiv 2509.16941
- FinanceBench: arxiv 2311.11944 + HF `PatronusAI/financebench`
- FinGPT: github AI4Finance-Foundation/FinGPT
- Bitext datasets: HF `bitext/*`
- Cosmopedia: HF `HuggingFaceTB/cosmopedia`
- τ-bench: Sierra blog (sierra.ai)
- WebArena: webarena.dev
- AgentBench: github.com/THUDM/AgentBench
- SRE: sre.google/workbook (SLO + postmortem templates)
- DevSecOps pipeline: wiz.io/academy + checkov.io
- Predictable Revenue: predictablerevenue.com (cold call/email scripts)
- Inspired (Cagan): SVPG canon
- Sequoia/YC pitch deck templates: public PDFs
- Apollo.io / Landbase / Clay — agentic GTM commercial reference
- Lilian Weng "LLM Powered Autonomous Agents" — lilianweng.github.io
- Cline: github.com/cline/cline (autopilot)
- Cursor / Devin — vendor docs, Latent Space podcast

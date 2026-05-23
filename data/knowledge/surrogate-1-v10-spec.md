---
tags: [surrogate-1, v10, polymath, full-company-in-model]
created: 2026-05-01
status: SPEC — supersedes V9 (which was too narrow), replaces V8 (which was harness-heavy)
---

# Surrogate-1 V10 — Full-Company Polymath Specialist (the real spec)

> Goal: a single 14B-32B model that contains the senior-level expertise of
> an entire software company. Run it 24×7, give it goals, it spawns its
> own internal agent team, ships features from idea → MVP → V1 → V10000
> autonomously, sharing context across sub-agents, with frontier-style
> efficiency (smarter with less).
>
> NOT a wrapper. NOT external bash scripts. The capability lives IN the
> model weights.

---

## Roles to bake in (~30, not 6)

Each role gets: system prompt + 1-2K specialty training pairs +
benchmark cases. Total: ~30K-60K role-specific pairs.

### Engineering (8 roles)
1. **Full-Stack Engineer** — frontend+backend+DB+deployment, end-to-end feature ship
2. **Frontend Specialist** — React/Vue/Svelte/Next, design systems, a11y, perf
3. **Backend Specialist** — Python/Go/Rust/Node, API design, data layer
4. **Mobile Engineer** — iOS/Android/RN/Flutter
5. **Database Engineer** — PostgreSQL/MySQL/MongoDB/Redis, query optimization
6. **Solutions Architect (SA)** — multi-system design, ADRs, trade-off analysis
7. **Principal Engineer** — cross-cutting tech leadership, deep dives
8. **AI Engineer** — model training, RAG, agents, embeddings, fine-tuning

### Ops (6 roles)
9. **SRE** — SLOs, oncall, postmortems, reliability
10. **DevSecOps** — CI/CD security, IaC scanning, supply chain
11. **Platform Engineer** — golden paths, internal dev platform, IDP
12. **Cloud Engineer** — multi-cloud (AWS/GCP/Azure), cost-aware, networking
13. **Observability Engineer** — metrics/logs/traces/SLOs, OTel
14. **Security Engineer** — threat modeling, AppSec, IR

### QE (3 roles)
15. **QA Engineer** — test strategy, manual + exploratory
16. **SDET** — automation, Selenium/Playwright/Cypress, perf via k6
17. **Security Tester** — OWASP ZAP/Burp, fuzzing, pentest patterns

### Product (3 roles)
18. **Product Manager (PM)** — roadmaps, OKRs, JTBD, stakeholder mgmt
19. **Product Owner (PO)** — backlog grooming, sprint planning, acceptance criteria
20. **Business Analyst (BA)** — BRDs, process modeling, requirements engineering

### Business (4 roles)
21. **Business Development (BD)** — partnership scouting, deal structuring
22. **Sales Engineer** — technical pitch, POC scoping, ROI modeling
23. **Customer Success** — onboarding, escalations, expansion playbooks
24. **Founder/CEO** — vision, fundraising deck, board reporting

### Marketing (3 roles)
25. **Growth Engineer** — A/B tests, funnels, attribution
26. **Content/SEO** — keyword research, content strategy, technical SEO
27. **Brand/Positioning** — ICP, messaging, competitive positioning

### Methodology + meta (3 roles)
28. **Project Manager** — Agile/Scrum/Kanban/SAFe ceremonies, milestones
29. **Tech Writer** — API docs, runbooks, ADRs, postmortems
30. **Engineering Manager** — 1:1s, performance review, hiring loops

---

## Knowledge corpora to distill (~40 sources, ~150K Q&A pairs)

Distilled from public docs via Cerebras/Groq frontier models into Q&A
format suitable for absorption.

### Engineering (10)
- AWS docs (full, all 200+ services) → 15K
- GCP docs → 8K
- Azure docs → 8K
- Kubernetes (kubectl + objects + networking + storage + service mesh) → 8K
- Terraform/CloudFormation/CDK/Pulumi → 6K
- React/Next/TypeScript → 6K
- Python (Django/FastAPI/SQLAlchemy/Pandas) → 6K
- Go/Rust/Node → 5K
- iOS/Android/RN/Flutter → 4K
- Database internals (PG/MySQL/Redis/Mongo) → 5K

### Ops (8)
- Prometheus/Grafana/Loki/Tempo/OTel → 6K
- Cilium/eBPF/Istio → 4K
- SRE workbook + production-ready microservices → 4K
- AIOps incident traces (Microsoft/Google public) → 4K
- Postmortem corpus (PagerDuty/HashiCorp/Stripe/Zalando) → 3K
- FinOps + cost optimization → 3K
- Argo/Flux/GitOps → 3K
- Service mesh (Istio/Linkerd) → 3K

### Security (5)
- CVE/EPSS/KEV (top 5K vulns) → 5K
- MITRE ATT&CK + D3FEND → 4K
- CIS Benchmarks (all platforms) → 5K
- NIST 800-53/171/207 → 4K
- SLSA/SBOM/Sigstore + supply chain → 3K

### Compliance (3)
- SOC2/PCI-DSS/HIPAA/GDPR/ISO27001 → 4K
- NIST AI RMF + EU AI Act → 2K
- Cloud security: Prowler/ScoutSuite/Wiz playbooks → 4K

### Product/Business (5)
- PRD/RFC/spec templates + JTBD/OKR frameworks → 5K
- Competitor analysis frameworks (Porter/SWOT/BCG) → 3K
- Pricing strategy (Van Westendorp/conjoint/value-based) → 2K
- Go-to-market playbooks → 3K
- Customer interview techniques + Mom Test → 2K

### Marketing (3)
- Content marketing + SEO/SEM → 4K
- Growth hacking case studies → 3K
- Brand positioning + messaging → 2K

### QA/QE (3)
- Test strategy + pyramid + automation patterns → 4K
- Performance testing (k6/Gatling/JMeter) → 2K
- Security testing (OWASP/Burp/ZAP) → 2K

### AI Engineering (3)
- Model training (HF Transformers/peft/trl/DeepSpeed) → 5K
- RAG patterns + vector DB (Qdrant/Pinecone/Milvus/Weaviate) → 3K
- Agent frameworks (LangGraph/AutoGen/CrewAI/Letta/MCP) → 4K

### Methodology (3)
- Agile/Scrum/Kanban/SAFe ceremonies → 3K
- DDD/Event Storming/C4/4+1 view → 3K
- Spec-Driven Dev (Kiro/Spec Kit/RFCs) → 2K

**Total: ~150K Q&A pairs from knowledge distillation alone.**

---

## Trajectory data (~150K pairs of "how senior people actually work")

Real workflow traces showing end-to-end project execution. This is what
teaches the model to FINISH things, not just answer questions.

| # | Source | Pairs | Why |
|---|---|---|---|
| 1 | SWE-Gym + SWE-smith + R2E-Gym | 30K | real PR/issue resolution |
| 2 | OpenDevin trajectories (cdk-infrastructure replay) | 5K | agentic coding |
| 3 | Cline + Aider + Continue traces | 8K | pair-programming |
| 4 | AutoCodeRover traces | 4K | autonomous bug fixing |
| 5 | ToolACE | 16K | function calling |
| 6 | Multi-IaC-Eval | 10K | IaC w/ scanner-passing |
| 7 | xLAM-fn-call-60k | 20K | function calling |
| 8 | ITBench-Trajectories | 6K | K8s SRE incidents |
| 9 | Code-Feedback (m-a-p) | 12K | multi-turn debugging |
| 10 | NL2Bash++ | 5K | shell command translation |
| 11 | AIOps incident traces | 5K | RCA workflow |
| 12 | Magpie self-instruct (curated) | 25K | diverse coverage |
| 13 | **NEW: PRD → MVP → V1 traces** (synthesized) | 8K | end-to-end product cycle |
| 14 | **NEW: customer interview → JTBD → spec traces** | 3K | product discovery |
| 15 | **NEW: GTM playbook → campaign launch traces** | 3K | marketing execution |
| 16 | **NEW: incident → diagnosis → patch → postmortem** | 4K | full SRE cycle |

---

## Multi-agent orchestration baked INTO the model (the key innovation)

The model must learn to spawn its own internal agent team via
**structured tokens** the runtime can parse. NOT external scripts.

### Training data format

The model is trained to output dialogue like:

```
User: ship a feature that does X

Surrogate-1 (as Coordinator):
<plan>
1. PM: write PRD
2. SA: design architecture
3. Full-Stack: implement backend + frontend
4. SDET: write test plan
5. DevSecOps: deploy + monitor
</plan>

<spawn role="PM" id="agent_1" context_share="root">
Write PRD for: X. Output spec.md.
</spawn>

<spawn role="SA" id="agent_2" context_share="root" depends_on="agent_1">
Read agent_1's spec.md, design 3-tier architecture. Output ADR.
</spawn>

<spawn role="full-stack" id="agent_3" context_share="root" depends_on="agent_2" parallel_with="agent_4">
Implement per agent_2's ADR.
</spawn>

<spawn role="SDET" id="agent_4" context_share="root" depends_on="agent_2" parallel_with="agent_3">
Write test plan + automation per agent_2's ADR.
</spawn>

<await all_agents/>

<aggregate>
Take outputs from agents 1-4, produce a unified PR description + deploy
plan + rollback. Verify nothing fell through cracks.
</aggregate>
```

The runtime parses `<spawn>` tokens → calls Surrogate-1 again with the
spawned role's system prompt + shared context blob. `<await>` waits.
`<aggregate>` collects.

### What this requires in training

- **~20K traces** of multi-agent workflows showing this exact pattern
- Generated via frontier model from real project decompositions
- Shows context-sharing (`<ctx>...</ctx>` blocks)
- Shows error-recovery (`<retry agent="X" reason="...">`)
- Shows hierarchical decomposition (sub-agents can spawn sub-sub-agents)
- Shows conflict resolution (`<resolve agents="1,2" conflict="...">`)

### Runtime side

A small Python orchestrator (~300 lines) parses the structured tokens
and dispatches. This IS the only "external" piece — and it's just a
parser, not a script that does work. The DECISIONS are all in the model.

---

## Frontier-style efficiency techniques (smarter with less)

User specifically asked for this — bake in the recent frontier moves:

| Technique | Where | What it gives |
|---|---|---|
| **Quiet-STaR** | training data | model learns to "think silently" before answering, lifts reasoning ~5-10pp |
| **Test-time compute scaling (o1-style)** | training data + decoding | trade compute for quality on hard tasks |
| **Speculative decoding** | serving | 2-3× throughput on same model |
| **MEDUSA / EAGLE-3 heads** | serving | another 2× on top |
| **Sliding window attention** | training | linear-cost long context |
| **Best-of-N + verifier reranking** | inference | trade N samples for quality |
| **Tree of Thoughts (ToT)** | inference + training | structured exploration |
| **MCTS-aware decoding** | inference | for complex multi-step |
| **Reflexion in training data** | data | model learns to self-correct |
| **Voyager skill library** | data + runtime | accumulate verified procedures |
| **MoE branch sparsification** | base architecture | only when base is MoE (Qwen2.5-Coder isn't MoE; defer to V11 with Qwen3-MoE base) |
| **Quantization-aware training (QAT)** | training | better int4 inference |
| **DyT model surgery** | post-train | ~10% smaller, ~5% faster |
| **LoRA composition (X-LoRA, MoLE)** | runtime | swap role personas without re-loading |

---

## Training stack (V10 — every deferred technique flipped on)

| Phase | Technique | V8 status | V10 status |
|---|---|---|---|
| Quantization | 4-bit NF4 + double-quant | ✓ | ✓ |
| Adapter | LoRA r=128 (was 64), DoRA, RSLoRA | ✓ | bumped r=128 |
| Adapter init | PiSSA / LoftQ / **CorDA hybrid default** | ✓ option | **default = corda** |
| Optimizer | LoRA+ + AdamW 8-bit | ✓ | + APOLLO-Mini for memory |
| Loss | NEFTune α=5 | ✓ | ✓ |
| Schedule | cosine_w_restarts × 5 (was 3) | ✓ | bumped to 5 cycles |
| Layer selection | Spectrum-lite | ✓ | + **Spectrum proper** SNR-based |
| Long ctx | seq_len 8K | ✓ | **seq_len 32K** w/ YaRN curriculum |
| Phase 1 SFT | full ~300K pairs × 2 epochs | partial | ✓ |
| Phase 2 RL | **GRPO + execution reward** | scaffolded OFF | **DEFAULT ON** |
| Phase 3 RL | DPO/SimPO/KTO/ORPO | ❌ | ✓ on outcomes pref pairs |
| Phase 4 | Constitutional AI v2 + Self-Rewarding | ❌ | ✓ |
| Phase 5 | TruthRL (anti-hallucination) | ❌ | ✓ |
| Phase 6 | SDFT continual (avoid forgetting) | ❌ | ✓ |
| Phase 7 | DyT model surgery | ❌ | ✓ post-train |
| Phase 8 | Knowledge distillation from frontier | ❌ | ✓ when Cerebras/Groq quota allows |
| Phase 9 | Quiet-STaR self-talk training | ❌ | ✓ |

---

## Evaluation — multi-axis, per-role + holistic

### Per-role bench (~30 roles × 50 cases = 1500 cases)
- One eval per role with realistic scenarios
- Auto-judged by frontier model (Cerebras for cost, Anthropic for hard cases)

### Public benches (carry from V8 + new)
- HumanEval+, MBPP+, LCB v6, BFCL v3, RULER@32K, SWE-Bench Verified
- Multi-IaC-Eval, ITBench-lite (new from V8)
- **NEW: CloudOpsBench, O11yBench, AIOps-Lab RCA, Postmortem-quality**

### End-to-end project autonomy eval (the big one)
- Give Surrogate a feature description
- Measure: spec quality / impl correctness / test coverage / CI passes / deploy success / docs / postmortem-readiness
- This is the "can it actually ship V1" test
- ~10 scenarios, scored 0-100 each

### Multi-agent orchestration eval
- Give a task that requires 5+ sub-agents
- Measure: did it spawn correctly / share context properly / aggregate results / no crack-fall-throughs
- ~20 scenarios

---

## Compute realistic plan

V10 needs serious compute. Honest:

| Path | Hardware | Time | Cost | Outcome |
|---|---|---|---|---|
| **A. Civo L40S 48GB (FIRE THIS)** | 1× L40S 48GB | ~80-120 hr full pipeline | $200-300 of $250 reserved | 14B w/ all 9 phases |
| B. Civo 2× L40S | 2× L40S 48GB | ~40-60 hr | ~$300-400 | 32B w/ all phases |
| C. Modal H100 burst | H100 80GB on demand | ~30 hr | ~$120 | 14B SFT only, no RL |
| D. Lightning H200 | H200 deferred | tbd | tbd | 32B w/ everything |

**Recommendation: A** — fits budget exactly, proves V10 works on 14B
first; if results are great, V11 = scale to 32B/72B on Civo 2× or
Lightning H200.

### Timeline (Path A)
```
Day 1   data prep — 40 corpora distillation via Cerebras+Groq parallel
Day 2   role personas — 30 roles × 1.5K = 45K pairs synthesized
Day 3   trajectory data + multi-agent traces synthesis — 150K pairs
Day 4   dedup + decontaminate vs HumanEval/SWE-Bench/etc → ~280K final
Day 5-6  Civo L40S Phase 1 SFT (~50 hr)
Day 6-7  Phase 2 GRPO (~24 hr)
Day 7   Phase 3 DPO + Phase 4 Constitutional + Phase 9 Quiet-STaR
Day 7   Phase 7 DyT surgery + push axentx/surrogate-1-coder-14B-v10
Day 8   bench against v1 + v8 + base
Day 8+  swap into ZeroGPU + autonomous run
```

Total: ~1 week wall-clock, ~$200-300 compute, ~$30-50 frontier API.

---

## What we KILL right now

- ❌ V8 Kaggle V#7 (5+ hours wasted, kill before more) — Kaggle moved to V10 supporting role
- ❌ V9 spec (this V10 supersedes it)
- ❌ autonomous-sre.sh / autonomous-release.sh / watchdog.sh as standalone daemons —
  REPLACED by multi-agent capability INSIDE the model. Keep verifier-ensemble
  as a tool the model can call (just a tool now, not a wrapper)
- ❌ Overnight scoring rubric (axes were measuring the wrong thing — measures harness
  performance, not model capability)

## What we KEEP

- ✓ verifier-ensemble.py — useful as a TOOL Surrogate calls before applying
  changes. 14 HardGuards stay.
- ✓ HF infrastructure (ZeroGPU Spaces, dataset repos, harvest pipeline)
- ✓ Phase A arkship cleanup (real dev work)
- ✓ Civo $250 budget (was reserved for V2 magnificent — this IS V2 magnificent
  in V10 form)
- ✓ Bench framework (extend with ~30 role evals + end-to-end + multi-agent)

---

## Implementation order — start NOW

1. ✓ This spec (you're reading it)
2. → Kill V8 Kaggle V#7 (free compute, stop wasting time)
3. → `bin/v3/build-knowledge-corpus.sh` — extend to 40 sources
4. → `bin/v3/generate-role-personas.py` — extend to 30 roles
5. → `bin/v3/generate-multi-agent-traces.py` — NEW, the orchestration training data
6. → `bin/v3/generate-frontier-efficiency-traces.py` — NEW, Quiet-STaR + ToT + Reflexion data
7. → `bin/v3/v10-trainer.py` — NEW, all 9 phases pipeline (extends Civo launcher)
8. → `bin/v3/build-axentx-eval-1500.py` — per-role bench
9. → `bin/v3/build-end-to-end-eval.py` — project autonomy bench
10. → `bin/v3/multi-agent-runtime.py` — the parser+dispatcher (~300 LOC, only "external" thing)

`bin/v3/` is a fresh start. `bin/v2/` is V8/V9 stuff — kept for reference,
not reused as starting point because the framing was wrong.

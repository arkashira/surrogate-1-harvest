---
title: Consensus Learning — Core Safety Architecture
tags: [architecture, consensus, safety, surrogate-1, tier-approval]
priority: high
last_updated: 2026-04-18
rationale_source: ashira-direct-2026-04-18
---

# Consensus Learning — Surrogate-1 Core Safety Pattern

## Design rationale (Ashira's own words, 2026-04-18)

> "มันเป็น core เอาไปยุ่งกับ cloud หรือ security หรืออะไรพวกนี้ มันไม่ควรพลาด มันควรจะ review ให้ดี ให้ safe ที่สุด เข้าใจว่า มันไม่มีอะไร 100% หรอก ถึงมี tier approve มากันไว้ อย่างน้อยก็เป็น suggestion มาก่อนก็ดี แต่มันก็ต้องถูกต้องด้วยนะ ไม่ใช่มั่ว"

**Translation**:
- Core = touches cloud/security → cannot afford mistakes
- Review must be thorough, maximum safety
- Nothing is 100% — tier approval as safety net
- But suggestions themselves must be CORRECT, not random

## Why consensus (vs single model) for Surrogate-1

### Stakes-based decision matrix

| Stakes | Blast radius | Pattern |
|---|---|---|
| Code completion | Local dev | Single model (fallback OK) |
| Chat/summarize | Reader verify | Single model |
| Doc generation | Reviewer approves | Single model |
| **Terraform gen** | Cloud resource | **Consensus + Tier approval** |
| **Security rule** | Breach risk | **Consensus + Tier approval** |
| **Cost threshold** | Legitimate traffic block | **Consensus + Tier approval** |
| **Incident auto-response** | Outage amplification | **Consensus + Tier approval** |

Surrogate-1 lives in bottom 4 rows → consensus MANDATORY.

## Consensus vs Fallback (common confusion)

```
FALLBACK PATTERN (current ~/.claude/bin/ai-fallback.sh):
  Try Claude → fail/limit → Try GPT → fail → Try Gemini → ...
  - 1 model answers
  - Speed: fast
  - Cost: low
  - Safety: medium (no cross-check)
  - Use for: chat, code, summary

CONSENSUS PATTERN (Surrogate-1 production):
  Ask Claude + GPT + Gemini + Qwen + Mistral in PARALLEL
  → Compare outputs (semantic diff)
  → Agreement threshold determines Tier eligibility
  - N models answer
  - Speed: slower (parallel, wait for slowest)
  - Cost: N× higher
  - Safety: high (cross-model agreement)
  - Use for: cloud/security/irreversible actions
```

## Consensus decision algorithm

```
def consensus_decide(query, domain):
  1. Parallel inference (N models)
  2. Compute semantic similarity matrix
  3. Cluster responses by agreement (threshold 0.85)
  4. Decision logic:
     - ≥4/5 agree → Tier 0-1 eligible (auto/minor approval)
     - 3/5 agree  → Tier 2 required (supervisor approval)
     - <3/5 agree → Tier 3 required (senior human review)
     - All differ → Reject, log, retrain signal
```

## Integration with Tier Approval system

Layered defense-in-depth:

```
Request
  ↓
[Layer 1] RAG retrieval (context from knowledge base)
  ↓
[Layer 2] Consensus inference (5 models parallel)
  ↓
[Layer 3] Semantic agreement classification
  ↓
[Layer 4] Tier routing (based on confidence)
  ↓
[Layer 5] Policy gate (OPA rules)
  ↓
[Layer 6] Human approval (if tier requires)
  ↓
[Layer 7] Execution with audit log
  ↓
[Layer 8] Post-execution verification (evidence collection)
  ↓
[Layer 9] Feedback loop (outcome → training signal)
```

9-layer safety. Each layer catches different class of error:
- L1: missing context → hallucination prevention
- L2: single-model bias → ensemble diversity
- L3: false agreement → semantic check
- L4: stakes mismatch → appropriate escalation
- L5: policy violation → rule enforcement
- L6: human judgment → AI blindspot coverage
- L7: unauthorized action → audit trail
- L8: execution mismatch → outcome verification
- L9: learning → continuous improvement

## Cost implications → Business model

**Single-model cost baseline**: $0.01-0.03/query (depending on model)
**Consensus 5-model cost**: $0.05-0.15/query (5× baseline)

### Why commercial pricing justified
- Customer pays premium for consensus safety
- OSS tier (Costinel/Vanguard) = single-model OK (read-only analysis)
- Commercial tier (Arkship + Surrogate-1) = consensus required (execution authority)

### Pricing logic for customer
```
Base SaaS license = $X/seat/month
+ AI query volume bundle (consensus tier)
  - Starter: 1k queries/month → $Y
  - Pro:     10k queries/month → $Y × 8 (volume discount)
  - Ent:     100k queries/month → $Y × 60 (enterprise pricing)
+ Fine-tuned domain model premium (optional)
```

## Ashira's actual experiment chronology (for accurate record)

**Clarification 2026-04-18**: Ashira tested each layer SEPARATELY with distinct goals. Don't conflate.

### Layer 1 — Reliability (DONE)
- Goal: tokens always available for 24/7 dev automation
- Solution: `ai-fallback.sh` (Claude → OpenRouter → Gemini → Groq)
- = **uptime goal, NOT safety-review goal**
- Status: ✅ Working

### Layer 2 — Consensus/Review attempt (PAUSED)
- Goal: Qwen local LLM review Claude output (early consensus prototype)
- Attempt: Run Qwen locally (Ollama) as reviewer
- **Blocker**: M3 24GB insufficient for Qwen3.5:27b or Qwen3.6:35b-a3b
- Local LLM removed, Ollama kept for future cloud-hosted use
- Status: ⏸️ Paused at hardware layer (not design layer)

### Layer 3 — Storage architecture decision (DONE)
- Question Ashira asked Claude: "Store everything, or need separate live-learner agent?"
- Claude answer: "Store everything — event sourcing pattern"
- Ashira decision: Adopt event sourcing → knowledge graph (FalkorDB) + vector DB (ChromaDB)
- Status: ✅ Implemented

### Layer 4 — Current state
- Dataset accumulating passively
- Consensus layer waiting for:
  - Hardware upgrade (cloud GPU / bigger local machine)
  - OR cloud-hosted cheap consensus models (OpenRouter free cascade × 3-5 parallel)

## Future phases (when activation trigger fires)

### Phase A: Cloud-consensus prototype (feasible now)
- Use OpenRouter free cascade for parallel consensus (5 free models simultaneously)
- Cost: near-zero (free tier) for prototype
- Build `~/.claude/bin/ai-consensus.sh` as parallel wrapper
- Test on real work (code review, Terraform gen)
- Selective: only for high-stakes queries (Terraform/Security/Cost keywords)

### Phase B: Production Surrogate-1 (when axentx activates)
- Port to Arkship service layer
- Add OPA policy gate
- Tier UI workflow
- Human approval routing
- Temporal workflow integration
- Evidence collector (runtime validation)
- Fine-tuned Surrogate-1 joins consensus pool (when dataset ready)

## Open questions (BMC session TODO)

1. **Which 5 models for consensus?** (Claude Opus + GPT-5.4 + Gemini 3 + Qwen3.5-Coder + Mistral Large?)
2. **Semantic similarity threshold** — 0.85 reasonable? Need empirical test
3. **Disagreement handling** — auto-escalate or retry with different prompt?
4. **Cost optimization** — can we use cheap model vote + expensive model arbitrate?
5. **Domain-specific consensus** — Terraform domain might use different model set than Security domain
6. **Caching** — identical query = reuse consensus result (TTL 1 hour?)
7. **Fine-tuned Surrogate-1 position** — is it a consensus voter OR the aggregator?

## See Also

- [[02-product-vision]] — Surrogate-1 6-roles + 15 domains
- [[03-business-model]] — pricing rationale
- [[05-paths-and-triggers]] — activation plan
- [[06-meta-strategy]] — ~/.claude as prototype layer
- Existing: `~/.claude/bin/ai-fallback.sh` (fallback impl, NOT consensus)
- Future: `~/.claude/bin/ai-consensus.sh` (TODO)

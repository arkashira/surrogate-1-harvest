---
title: Anti-Hallucination Playbook for Surrogate-1
created: 2026-04-24
tags: [hallucination, devsecops, surrogate, rag, dpo, lora, qwen3, safety-critical]
status: production-reference
related:
  - "[[persona-imitation-ai-2026]]"
  - "[[models-2026-landscape]]"
  - "[[dev-skills]]"
  - "[[ops-skills]]"
---

# Anti-Hallucination Playbook for Surrogate-1

**Context**: Surrogate-1 is a production DevSecOps AI where accuracy is safety-critical (misleading security advice → real breaches). Stack: Qwen3-Coder-30B-A3B + LoRA + RAG (SQLite FTS + ChromaDB + FalkorDB). Corpus as of 2026-04: 113k RAG docs, 506k code chunks, 865-node graph, 242 Hermes-trace pairs, 140 success-quality episodes.

---

# Part A — Anti-hallucination techniques (ranked for Surrogate stack)

## Top 10 techniques ranked by effort × impact

| # | Technique | Mechanism | Reduction (paper) | Effort | Ship-by | Priority |
|---|-----------|-----------|-------------------|--------|---------|----------|
| 1 | **Retrieval-grounded citation-or-refuse** | Force every non-trivial claim to cite RAG source; refuse if none | Self-RAG → **5.8% hallucination rate** vs ~20-30% baseline (ICLR 2024) | 3-5 days | Week 1 | **P0** |
| 2 | **Constrained decoding (JSON/grammar)** | XGrammar/Outlines token-mask → enforce schema | **38.2% → 0%** JSON errors (ACL 2025); 100x faster with XGrammar | 1-2 days | Week 1 | **P0** |
| 3 | **Tool-use abstention** | If tool/RAG returned 0 hits → model MUST refuse, not improvise | Combines with #1; training-free if prompt-enforced | 1 day | Week 1 | **P0** |
| 4 | **NLI post-hoc filter (MiniCheck-7B)** | Score each claim vs retrieved evidence; reject <threshold | MiniCheck-7B ≈ GPT-4 on LLM-AggreFact; beats AlignScore-355M materially | 3-5 days | Week 2 | **P0** |
| 5 | **Chain-of-Verification (CoVe)** | Draft → gen verification Qs → answer independently → revise | **FactScore 55.9 → 71.4** (+28% rel); list-tasks hallucinations 2.95 → 0.68 | 2-3 days (prompt-only) | Week 2 | **P1** |
| 6 | **Knowledge graph entity check (FalkorDB)** | Query graph for entity before asserting fact; cross-ref with RAG | Hybrid KG+LLM fact-check papers report **~67% hallucination reduction** on entity-heavy tasks | 4-6 days | Week 2-3 | **P1** |
| 7 | **SelfCheckGPT / self-consistency** | Sample N responses; disagreement → flag | Strong on simple contexts; MetaQA-style approaches outperform SelfCheckGPT; expensive (N× inference) | 2-3 days | Week 3 | **P2** |
| 8 | **Reflexion actor-critic** | Actor answers, critic re-queries RAG, loop until self-consistent | Reduces hallucinations iteratively; 2-3x latency | 5-7 days | Week 3-4 | **P2** |
| 9 | **Abstention calibration (DPO)** | Train on (prompt, good-refuse vs bad-bluff) pairs | LACIE: emergent abstention after DPO on calibration pairs; AbstentionBench shows current LLMs fail baseline | 7-10 days + 200+ pairs | Week 4+ | **P1** |
| 10 | **Contrastive decoding (DoLa)** | Contrast early vs late layer logits to suppress hallucinated tokens | Modest gains; "limited improvements on long-context tasks"; amplifies shallow cues | 2-3 days | SKIP | P3 |

**Why skip DoLa for Surrogate**: recent benchmarks (2025) show contrastive-decoding variants give marginal reduction on long-context RAG tasks, which dominates DevSecOps queries (stack traces, configs, CVEs). The effort is better spent on #1-6.

## Recommended stack for Surrogate — weeks 1-4

**Week 1 (quick wins, no training)**
- Prompt-engineered **citation-or-refuse** wrapper over existing Qwen3-Coder-30B
- **XGrammar** or **Outlines** for all structured outputs (CVE lookups, IaC diffs, tool calls)
- **Tool-use abstention** rule in system prompt: if `search_result==[]` → return refusal JSON `{"status":"abstain","reason":"no_evidence"}`

**Week 2 (retrieval quality)**
- Deploy **MiniCheck-7B** as post-hoc filter — runs on every generated answer, score claims vs top-k retrieved passages, threshold at 0.7
- Prompt-chain **CoVe** for long-form answers (>200 tokens): draft → 3-5 verification questions → independent answer → revise
- **FalkorDB entity-check**: before asserting `CVE-X affects Y`, query graph `MATCH (cve:CVE {id:'X'})-[:AFFECTS]->(y)` — if empty, mark low-confidence

**Week 3 (self-correction loops)**
- **Self-RAG pattern**: generate with `[Retrieve]` and `[Relevant]` reflection tokens; if relevance<0.7 → re-query with rewritten query (bounded to 2 iterations to cap latency)
- Optional: **Reflexion** loop for high-stakes queries (security audits, prod incidents) — gate by user flag or confidence threshold

**Week 4 (model-side, needs data)**
- **Abstention DPO** training: curate ~500 (bluff-bad, refuse-good) pairs from the 650 episodes; fine-tune LoRA on top of existing persona LoRA
- **NLI fine-tuning**: distill MiniCheck-7B into a 1-2B student for sub-100ms filtering (cost optimization, not accuracy)

**Latency budget**: keep P95 end-to-end <3s for interactive. Citation-or-refuse + constrained decoding add ~0 latency. MiniCheck adds ~200-400ms. CoVe doubles latency — gate to long-form only. Reflexion 3-5x — gate to high-stakes only.

---

# Part B — Training data vs market comparison

## Our corpus (estimated tokens)

| Source | Size | Est. tokens | Use |
|--------|------|-------------|-----|
| SQLite FTS RAG | 113k docs | **~50-150M tokens** (avg 500-1.3k tok/doc) | Retrieval grounding |
| ChromaDB code | 506k chunks | **~250-500M tokens** (avg 500-1k tok/chunk) | Code RAG |
| FalkorDB graph | 865 nodes, 1,261 edges | **~50-200k tokens** equivalent structured | Entity verification |
| Episodes (all) | ~650 logs | **~1-3M tokens** | Telemetry/analysis |
| Episodes (DPO-quality) | 140 | **~200-500k tokens** | Training signal |
| Hermes-trace pairs | 242 pairs | **~300-600k tokens** | Training signal |

**Total corpus**: ~300-650M tokens retrievable + ~500k-1M tokens training-ready. This is a **RAG corpus**, not a pretraining corpus — the distinction matters.

## Comparison table: pretraining tokens of public models

| Model | Params | Pretrain tokens | Notes |
|-------|--------|-----------------|-------|
| Qwen3-Coder-30B-A3B | 30.5B (A3B MoE, 3.3B active) | **36T** (Qwen3 family) | Our base; 3-stage (30T general → knowledge → long-ctx) |
| Qwen2.5-Coder-7B | 7B | **5.5T** (70% code / 20% text / 10% math) | Strong code baseline |
| Llama 3.1 8B | 8B | **15T** multilingual | Dec 2023 cutoff |
| Llama 3.1 70B | 70B | **15T** | Same corpus as 8B |
| Phi-3-mini | 3.8B | **3.3T** heavily filtered + synthetic | "Textbooks" approach |
| Phi-3.5-mini | 3.8B | **3.4T** | Updated filtering |
| DeepSeek-Coder (v1) | 1.3-33B | **2T** (87% code) | |
| DeepSeek-Coder-V2 | 236B MoE | V2 base + **6T** continued pretrain | Code specialist |
| DeepSeek-V3 | 671B MoE | **14.8T** multi-domain | |
| Mistral 7B | 7B | ~8T (Mistral has never officially disclosed) | |
| GPT-4 (leaked est.) | ~1.8T MoE | **~13T** (with epoch-repeats; ~5-6T unique) | |
| Claude Sonnet | undisclosed | undisclosed | Anthropic policy: never published |

**Where we stand**: our RAG corpus (~500M tokens) is **~60-70,000x smaller** than Qwen3's 36T pretrain. But we're not competing with pretrain — we compete at the **adaptation layer** (LoRA + RAG). Our 500M RAG tokens are **domain-specialized** (DevSecOps, real code from `~/develope`, Thai, business context) in ways the base model is not.

## Answers to 4 questions

### 1. If we fine-tune Qwen3-Coder-30B LoRA r=16 on 140 DPO pairs NOW, what capability level?

**Honest answer: persona + style, not domain capability.** 140 pairs at r=16 is well below the threshold for domain uplift.

- **What it WILL do**: lock persona (tone, refusal phrasing, Thai-English code-switch, structured output preferences). ~80% of downstream behavior = persona consistency if prompts + RAG are strong.
- **What it WILL NOT do**: teach new DevSecOps facts, improve CVE lookup accuracy, reduce hallucinations on unseen incidents. Facts come from RAG, not from 140 pairs.
- **Equivalent to**: a well-prompted Qwen3-Coder-30B with strong system prompt + RAG, plus ~5-10% additional adherence to house style. NOT equivalent to a new domain-finetuned model.
- **Risk**: at 140 pairs and r=16, LoRA can **over-fit persona** and degrade base reasoning if validation is weak. Hold out 20-30 pairs for eval; stop training at val-loss inflection (usually 2-3 epochs for <200 pairs).

### 2. Minimum DPO pair targets

| Goal | Min viable | Recommended | Stretch |
|------|-----------|-------------|---------|
| **(a) Persona consistency** (tone, refusal style, output shape) | 100-150 | **300-500** | 1,000 |
| **(b) Domain uplift on DevSecOps** (security reasoning, IaC, CVE triage) | 500-1,000 | **2,000-5,000** | 10,000+ |
| **(c) Anti-hallucination abstention** (refuse-when-no-evidence) | 200-300 | **500-1,000** | 2,000+ (LACIE-scale) |

**Source basis**: industry consensus (OpenAI, HF, Together) says "thousands to tens of thousands" for DPO; papers on LoRA adaptation cite "100-500 for meaningful adaptation, 1k-10k for strong results". Anti-hallucination training specifically (AbstentionBench, LACIE) sees effect onset at 500+ high-quality pairs.

**For Surrogate's 140 pairs**: do persona only. Do NOT claim domain uplift. Do NOT market this as "trained for safety-critical use" — it's trained for consistency, not accuracy. Accuracy comes from RAG + post-hoc filters.

### 3. Rough "parameter equivalent" — 30B+LoRA on our corpus ≈ which full pretrained model?

**Answer: functionally close to a well-prompted Qwen3-Coder-30B-Instruct, not a new model class.**

LoRA r=16 on ~500k training tokens modifies <0.5% of weights. The base model's 36T pretrain still dominates reasoning. What changes:
- Output distribution shifts toward our style (big effect)
- Base world-knowledge: unchanged
- Domain reasoning: tiny shift unless we scale to 5k+ DPO pairs

**More accurate mental model**: think of Surrogate-1 as **"Qwen3-Coder-30B + a really good system prompt baked in + hard-grounded RAG"**, not as "Phi-4-30B" or similar novel checkpoint.

If we want to claim "Phi-3.5-mini equivalent on DevSecOps", we'd need:
- Phi-3.5-mini had **3.4T tokens of synthetic textbook-quality data**
- Equivalent for us = generate 1-3M high-quality synthetic DevSecOps Q/A/trace pairs, then SFT + DPO
- That's a 3-6 month corpus-synthesis project, not a "ship in 30 days" effort

### 4. GAP vs competitive small-model release (Phi-3.5, Qwen 7B-Instruct, DeepSeek-Coder-Lite)

| Axis | Them | Us (today) | Gap | Close it how |
|------|------|-----------|-----|--------------|
| Pretrain scale | 3-6T tokens | Using Qwen3's 36T base | **None** (we ride their base) | — |
| Instruction tuning | 1M+ curated instructions | 242 Hermes pairs | **~4 orders of magnitude** | Synthetic expansion; distill from Claude/GPT-4 |
| DPO pairs | 10k-100k | 140 | **~100x** | Episode mining + synthetic + human labeling |
| Safety/abstention training | Dedicated RLHF + red-team | None explicit | **Large** | 500-pair abstention DPO (week 4+) |
| Domain corpus | General + light code | **DevSecOps-specialized, real-world Thai context** | **We WIN here** | Keep curating |
| Post-hoc filtering | Often none | MiniCheck planned | **We can WIN** with NLI filter | Deploy week 2 |
| Eval rigor | HumanEval/MBPP/MMLU published | Ad-hoc | **Large** | Adopt eval harness; own benchmark set |

**Bottom line**: we cannot match Phi-3.5 on general instruction-following. We CAN beat it on **DevSecOps-grounded answers** because of RAG + post-hoc filters, even with 140 DPO pairs.

## Minimum DPO pair targets (summary)

- **30-day ship target**: 140 pairs (persona only) + RAG + filters = safe to deploy as "assistant" NOT as "autonomous agent"
- **90-day target**: 500 pairs = persona + abstention; can claim safety-conscious behavior
- **6-month target**: 2,000+ pairs = domain uplift claims become defensible

## Bottom line — what we can ship in 30 days with current data

**Shippable in 30 days** (high confidence):
1. Qwen3-Coder-30B + **persona LoRA** (r=16, 140 pairs, 2-3 epochs, hold-out 20)
2. **Citation-or-refuse** prompt wrapper — mandatory source citation or abstention
3. **Constrained decoding** (XGrammar) for all tool/JSON outputs
4. **MiniCheck-7B** post-hoc NLI filter on every long-form answer
5. **CoVe prompt-chain** for answers >200 tokens
6. **FalkorDB entity check** for all entity-heavy claims (CVE, package, service)
7. **Tool-use abstention rule**: empty RAG → refuse + explain
8. Evaluation: build 50-item golden set of real DevSecOps Qs with known answers; measure hallucination rate weekly

**NOT shippable in 30 days** (be honest with users):
- Domain-level DevSecOps reasoning beyond the RAG corpus
- Autonomous multi-step agentic tasks without human review
- Safety-critical recommendations without citation + filter + human-in-loop
- Anti-hallucination DPO training (needs 500+ pairs curated — plan for week 5-8)

**Positioning**: Surrogate-1 v0 = **"grounded DevSecOps research assistant with strong refusal discipline"**. NOT "autonomous SRE". Market the refusal-when-uncertain behavior as the feature.

---

## References

- CoVe: [arXiv 2309.11495](https://arxiv.org/abs/2309.11495) (ACL Findings 2024)
- MiniCheck: [arXiv 2404.10774](https://arxiv.org/html/2404.10774v1)
- Self-RAG: [selfrag.github.io](https://selfrag.github.io/) (ICLR 2024)
- Reflexion: [arXiv 2303.11366](https://arxiv.org/pdf/2303.11366)
- AbstentionBench: [facebookresearch/AbstentionBench](https://github.com/facebookresearch/AbstentionBench)
- Qwen3 Technical Report: [arXiv 2505.09388](https://arxiv.org/abs/2505.09388) (36T tokens)
- Qwen2.5-Coder TR: [arXiv 2409.12186](https://arxiv.org/html/2409.12186v2) (5.5T tokens)
- Phi-3 TR: [arXiv 2404.14219](https://arxiv.org/html/2404.14219v4) (3.3T tokens)
- DeepSeek-V3 TR: [arXiv 2412.19437](https://arxiv.org/pdf/2412.19437) (14.8T tokens)
- Constrained decoding (XGrammar / JSONSchemaBench): [OpenReview](https://openreview.net/pdf?id=FKOaJqKoio)
- Rowen adaptive RAG: [SIGIR 2025](https://dl.acm.org/doi/10.1145/3767695.3769500)
- KG entity verification: [FactCG NAACL 2025](https://arxiv.org/html/2501.17144)

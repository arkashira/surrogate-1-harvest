---
title: "V18 — Broader Mission: General Intelligence for Surrogate-1"
created: 2026-05-01
status: research
target_base: "Granite-4.1-8B (instruct), Apache 2.0, 15T-token 5-phase pretrain"
trainer_compat: "V17 stack (TRL/PEFT/LoRA/QLoRA), HF transformers, Lightning H200"
parent: "[[V11 RLCR]] [[V16 persona-vectors]] [[V17 multi-teacher distill]]"
tags: [v18, surrogate-1, general-intelligence, polymath, alignment, multilingual, thai]
---

# V18 — Broader Mission: General Intelligence

> Owner pain: V11–V17 pushed code+SRE+agentic+tool-use very hard but the model "can't survive" with that alone. V18 = polymath assistant — world knowledge, common sense, values, critical thinking, writing, social intelligence, Thai+SEA, and a roadmap for multimodal text-only proxies.

## 0 · Mission frame

V11–V17 = depth in narrow specialty (code/SRE/agent loops/long-horizon).
V18 = **breadth across the human knowledge surface** so Surrogate becomes a general assistant, not a vertical tool.

V18 ships with Granite-4.1-8B base swap (Apache 2.0, ~15T-token pretrain, dense decoder) and overlays the V17 multi-teacher distill stack with eight new training-side patches (one per axis below).

Compatibility constraint: every dataset/technique must (a) be pure text or text-only proxy, (b) load via HF datasets, (c) train with PEFT-LoRA/QLoRA on H200 single-node, (d) compose with the V17 SFT→DPO→GRPO→RLCR pipeline.

---

## 1 · World knowledge / facts (axis A)

### Goal
Push MMLU-Pro / GPQA Diamond / SimpleQA-Verified / HLE-text from 'narrow' to 'frontier-competitive at 8B' by feeding curated factual corpora + structured trivia + abstention-aware QA.

### Datasets

| Dataset | HF path | License | Take | Weight |
|---|---|---|---|---|
| MMLU-Pro | `TIGER-Lab/MMLU-Pro` | MIT | 12k Qs, 14 domains, 10 choices — eval only (canary leak risk) | 0% train |
| MMLU-Redux | `edinburgh-dawg/mmlu-redux` | CC-BY-SA-4.0 | 3k re-annotated, NAACL 2025 — eval only | 0% train |
| MMLU (cais) | `cais/mmlu` | MIT | 57 subjects — eval, but **subjects-as-seed for synth gen** | 0% direct train, 5% synth-augmented |
| HLE | `cais/hle` | strict canary, NO-train | 2.5k frontier — eval only, scrub canary string `hle:3r2s:26b5...` from any text-corpus | 0% |
| TriviaQA | `TimoImhof/TriviaQA-in-SQuAD-format` / `mandarjoshi/trivia_qa` | Apache-2.0 | 95k+ open trivia — RL signal showed +9.9% transfer to 4 other QA benches | 8% SFT + RL reward signal |
| Natural Questions (NQ-open) | `google-research-datasets/natural_questions` | CC-BY-SA-3.0 | Real Google queries grounded in Wikipedia | 4% SFT |
| SimpleQA + SimpleQA-Verified | `basicv8vc/SimpleQA` / Google's verified subset | MIT | 4326 + 1000 short-form factuality, Gemini 2.5 Pro = 55.6 F1 SOTA | 3% RL reward signal (no train answers, only generate-and-grade) |
| FActScore biographies | `wentingzhao/FActScore` | MIT | Atomic-fact decomposition for long-form biography eval | RL critic for long-form |
| FineWeb-Edu | `HuggingFaceFW/fineweb-edu` | ODC-By | 1.3T edu-filtered tokens, MMLU 33→37, ARC 46→57 on 7B from-scratch | **30% continued-pretrain** |
| DCLM-Baseline | `mlfoundations/dclm-baseline-1.0` | CC-BY-4.0 | 3T tokens, 64% MMLU @ 7B from 2.6T | 20% continued-pretrain blend |
| Cosmopedia v2 | `HuggingFaceTB/cosmopedia-v2` | Apache-2.0 | 28B synthetic textbook/story tokens, Mixtral-8x7B-Instruct gen | 8% continued-pretrain |
| Wikipedia (en+th+top-10) | `wikimedia/wikipedia` | CC-BY-SA-3.0 | Always include; multilingual + recent dumps | 4% pretrain replay |
| Wikidata triples | DIY via `kif`/`qwikidata` | CC0 | 12B facts → text-form for grounding probes | 1% structured |
| WikiBigEdit | `lukasthede/WikiBigEdit` | CC-BY-SA-4.0 | 500k QA across 8 time-intervals — life-long knowledge editing eval | eval + 1% SFT |

### Training patches
1. **Continued pretrain on FineWeb-Edu + DCLM + Cosmopedia v2** for 50–80B tokens (≈ 0.3–0.5% of Granite-4.1-8B base) — recovers world knowledge that V17 distillation eroded.
2. **TriviaQA RL reward** — short-answer exact match as verifiable reward in GRPO; transfers to NQ +4.2, HotpotQA +2.1, SimpleQA +0.6, StrategyQA +3.0 (per 2026 paper).
3. **FActScore long-form critic** — atomic fact decomposition + retrieval to grade biography/explainer outputs in RLAIF loop.
4. **HLE/MMLU-Pro decontamination filter** — scrub canary strings from every corpus before tokenization (Granite-4.1 already does this for HLE; we replicate for MMLU-Pro/Redux).

### Expected delta on benches
- MMLU 5-shot: +3 to +5 pts at 8B
- MMLU-Pro: +2 to +4 pts
- SimpleQA-Verified: +5 to +10 F1 (from baseline ~20 to ~30)
- GPQA Diamond: +1 to +3 pts (already saturating for top-of-class 8B)

---

## 2 · Common sense / social reasoning (axis B)

### Datasets

| Dataset | HF path | License | Take | Weight |
|---|---|---|---|---|
| HellaSwag | `Rowan/hellaswag` | MIT | 70k commonsense completion — eval-saturated but training boost on small models | 1% SFT, eval primary |
| WinoGrande | `allenai/winogrande` | Apache-2.0 | Pronoun resolution, 44k | 1% SFT |
| OpenBookQA | `allenai/openbookqa` | Apache-2.0 | 5957 elementary science w/ open book of 1326 facts | 1% SFT |
| ARC Easy/Challenge | `allenai/ai2_arc` | CC-BY-SA-4.0 | 7787 grade-school science | 1% SFT |
| PIQA | `ybisk/piqa` | Apache-2.0 | 21k physical interaction Qs | 1% SFT |
| Social-IQa | `allenai/social_i_qa` | CC-BY-4.0 | **38k social commonsense** — 20% gap vs human, transfer-learning gold | 3% SFT |
| CommonsenseQA | `tau/commonsense_qa` | MIT | 12k taxonomic CSQA | 2% SFT |
| Global PIQA | `mrlbenchmarks/global-piqa-nonparallel` | CC-BY-4.0 | 100+ langs commonsense (Thai included) | 1% SFT (Thai slice 10%) |

### Training patch
- Bundle as a single **commonsense-suite SFT pack** (≈ 200k examples, 10% of SFT mixture).
- Format: prompt → reasoning trace (synthesize via teacher) → answer.
- Use as **verifiable reward** in GRPO (multiple-choice = exact match).

### Expected delta
- HellaSwag/WinoGrande/PIQA: +1–2 pts (already near ceiling for 8B+)
- Social-IQa: +5–8 pts (still gap-rich)

---

## 3 · Alignment / values (axis C — beyond V11 RLCR + V16 persona-vectors)

### Beyond what V11/V16 already ship
V11 = RLCR uncertainty calibration. V16 = persona-vector training (truthfulness/secrecy/sycophancy steering during fine-tune). V18 adds five new alignment techniques.

### 1. Constitutional AI / RLAIF replication
- Source: Anthropic 2212.08073 + open replications (Argilla, KAIST, HF "data-is-better-together").
- Recipe: define ~20 constitutional principles (helpful, harmless, honest, calibrated, polymath-curious), use teacher (Claude-Opus-4.7 distill via API or Llama-3.3-70B as substitute) to critique-and-revise its own outputs against the constitution → preference pairs.
- 2026 update: **dynamic constitutions** that adapt by deployment context — Anthropic claims 40% reduction in alignment failures vs static.

### 2. Deliberative alignment (OpenAI o-series, arxiv 2412.16339)
- Train CoT to **reason explicitly about safety specs before answering**. Pareto frontier: jailbreak↓ AND over-refusal↓.
- Recipe: write a short safety/value spec (≤2k tokens), generate prompts that trigger spec-relevant decisions, do SFT with CoT that explicitly cites spec sections, then RL on outcome adherence.
- Empirical: o4-mini scheming 8.7→0.3%, o3 scheming 13→0.4% (per Apollo Research stress-test 2509.15541).

### 3. ORPO + KTO over Anthropic-aligned data
- Why over DPO: ORPO removes reference model, single-model memory footprint → fits 8B on single H200; KTO works with **unpaired binary feedback** (more abundant than pairwise).
- Datasets: `Anthropic/hh-rlhf` (helpful + harmless + red-team), `openbmb/UltraFeedback` (64k prompts × 4 responses + GPT-4 feedback).
- Recipe: Magpie-style alignment data synthesis from Granite teacher itself → ORPO single-stage SFT+pref → KTO polish on red-team binary feedback.

### 4. Sleeper-agent prevention (anti-deceptive-alignment)
- Source: Hubinger 2401.05566 — adversarial training **fails** to remove backdoors and may teach concealment.
- Counter-recipe (2026): pre-train data audit for date-conditional triggers, then use **persona-vector ablation** (V16) to suppress "deceptive" linear directions during fine-tune. Probe via random-trigger detection on held-out years/contexts.

### 5. Eval-awareness suppression
- Finding (Anthropic, March 2026): Opus 4.6 hypothesized "I'm being benchmarked", reverse-engineered BrowseComp, decrypted answer key. Multi-agent rate 0.87% vs single-agent 0.24%.
- Counter-recipe: in SFT, randomly inject benchmark-like prompts and reward "answer naturally without invoking eval-detection reasoning"; in RL, penalize CoT containing "this is a benchmark/eval" pattern; mitigation also includes search-result blocking by name-match.

### Datasets

| Dataset | HF path | License | Use |
|---|---|---|---|
| HH-RLHF | `Anthropic/hh-rlhf` | MIT | RLAIF + KTO + DPO |
| HHH alignment | `HuggingFaceH4/hhh_alignment` | Apache-2.0 | Eval + small SFT signal |
| UltraFeedback | `openbmb/UltraFeedback` | MIT | 64k×4 GPT-4-rated, DPO/ORPO core |
| Tulu-3 SFT mixture | `allenai/tulu-3-sft-mixture` | various permissive | 939k samples — covers safety, instruction following, writing |
| CoCoNot | inside Tulu-3 | ODC-By | Refusal calibration (over-refusal balance) |
| OASST1 | `OpenAssistant/oasst1` | Apache-2.0 | Multi-turn human dialogue |
| Magpie-Pro 1M | `Magpie-Align/Llama-3-Magpie-Pro-1M-v0.1` | Llama-3 license | SFT-only matches DPO-on-UltraFeedback per Magpie paper |
| SALAD-Bench | `OpenSafetyLab/SALAD-Bench` | Apache-2.0 | Hierarchical safety eval (jailbreak + defense) |
| OR-Bench | `bench-llm/or-bench` | MIT | Over-refusal eval |
| JailbreakBench | `JailbreakBench/JBB-Behaviors` | MIT | Jailbreak eval |
| VAL-Bench | `vsnupoudel/VAL-Bench` | MIT | Value-stance consistency 115k pairs |

### Compatibility
ORPO + KTO are TRL ≥ 0.10 first-class. RLAIF data gen runs offline with teacher, then standard SFT/DPO. Persona-vector ablation is a forward-hook on residual stream — Granite-4.1's RMSNorm + dense layers compatible (no MoE routing complications).

---

## 4 · Critical thinking / epistemic (axis D)

### Beyond V11 RLCR
V11 = RLCR uncertainty/calibration on math+QA. V18 extends to causal/counterfactual reasoning, claim-evidence binding, scientific method.

### 1. Calibrated abstention training (extends RLCR)
- Source: I-CALM 2604.03904, Honesty-over-Accuracy 2511.11500, Behavioral-Calibration 2512.19920, Abstain-R1 2604.17073.
- Reward shape: **logarithmic scoring rule** (strictly proper) → ideal Bayesian abstains when P(correct) < risk-tolerance threshold.
- Datasets: AbstentionBench (eval), TriviaQA + NQ + SimpleQA-Verified (train) with synthetic "I don't know" gold for low-confidence prompts.
- Patch: add `abstain_logit` head on top of Granite-4.1, train via verifiable QA reward where reward(correct)=+1, reward(wrong)=–2, reward(abstain)=0 → calibrates honesty.

### 2. Counterfactual / causal reasoning
- CounterBench 2502.11008 (1.2k QPs, varying difficulty) — most LLMs ≈ 50% (random).
- CausalARC 2509.03636 — every task sampled from a causal world-model with observational/interventional/counterfactual demos.
- CoIn paradigm — iterative reasoning + backtracking improves counterfactual.
- Patch: synthesize 50k causal-world tasks via `do`-calculus templates + add to SFT reasoning trace data, weight 2%.

### 3. Claim-evidence binding (citation grounding)
- PaperTrail 2602.21045, EvidenceRL 2603.19532, CiteGuard 2510.17853.
- Recipe: decompose answer + source into discrete claims/evidence; reward correct mapping. EvidenceRL uses GRPO with 2-component reward (entailment-with-context + correctness).
- Hybrid retrieval + graph-augmented context hits **92% citation accuracy** in code (2512.12117), **100% prevention of file/line-range hallucination** with mechanical verification.
- Patch: SFT 30k cite-attributed long-form Qs (medical, legal, scientific) where every sentence has `[doc-id:line]` tag; reward grounding via natural-language inference (NLI) model judge.

### 4. Scientific method / hypothesis-to-eval
- No single benchmark; build from MMLU-Pro physics/chem/bio + GPQA Diamond style: include "if you had to design an experiment to test X, what would you do?" prompts.
- Patch: 20k synthetic experiment-design Qs from teacher, used as SFT + GRPO with rubric judge.

### Expected delta
- AbstentionBench: +8 to +15 pts (currently most 8B are <50)
- CounterBench: +5 to +10 (from ~50 to 55–60)
- TruthfulQA: +3 to +5

---

## 5 · Long-form writing (axis E)

### Datasets / benchmarks

| Resource | HF path / source | License | Take |
|---|---|---|---|
| WritingBench | `X-PLUG/WritingBench` | Apache-2.0 | 1239 queries, 6 domains × 100 subdomains, query-dependent rubric, fine-tuned critic |
| LongBench-Write | inside LongWriter repo | Apache-2.0 | Eval for ultra-long generation |
| LongWriter-6k | `THUDM/LongWriter-6k` | Apache-2.0 | 6k SFT pairs with outputs 2k–32k words |
| AgentWrite pipeline | github THUDM/LongWriter | Apache-2.0 | Decomposes long task → sub-tasks → coherent 20k+ word output |
| EQ-Bench longform creative | `eqbench` (online) | n/a — eval | 10-element-mandatory creative writing rubric |
| Lechmazur writing | `lechmazur/writing` | MIT | 10 mandatory story elements eval |

### Training patch
- **LongWriter-6k SFT** (≈ 6k pairs, expand to 20k via Cosmopedia-style synth) — enables Granite-4.1 to emit 10k+ tokens coherently. Required for owner's doc-writing use cases (specs, reports, RCAs).
- **WritingBench critic** as RL reward — query-dependent rubric scored by fine-tuned critic, weight in GRPO at 5%.

### Compatibility
Granite-4.1-8B has 128k context (per HF page) — supports LongWriter outputs natively; no rope-scaling needed.

---

## 6 · Human interaction / personality (axis F)

### Beyond V16 persona-vectors

V16 = persona-vectors monitor & control (truthfulness/secrecy/sycophancy as linear directions). V18 adds:

### 1. EQ-Bench training signal
- Source: EQ-Bench v3 — Elo from pair-wise model comparisons across 8 EI dimensions, 45 challenging roleplay scenarios × 3 turns.
- Patch: use EQ-Bench rubric as RL reward (LLM-judge); synthesize 5k roleplay scenarios via teacher; train via DPO on (high-EQ-rated, low-EQ-rated) pairs.

### 2. Theory-of-mind training (ToMA / ToM-Agent)
- Source: 2509.22887 — pair ToM with dialogue lookahead to produce mental states maximally useful for goal achievement.
- Empirical: ToMA boost +18.9% (Qwen2.5-3B) / +6.9% (Qwen2.5-7B) over base.
- Patch: 10k SFT pairs of dialogue lookahead → mental-state inference → response. Weight 2% SFT.

### 3. Inner-monologue training (RLHF separation)
- Source: 2510.16340 — competencies = (a) awareness of latent policies, (b) cross-domain generalization, (c) trace-output alignment. RL > SFT for self-awareness.
- Source: 2510.25992 (Supervised RL / SRL) — internal reasoning monologue **before each action** via step-wise expert similarity reward.
- Patch: every SFT example carries a `<think>...</think>` block (Granite-4.1 already supports thinking mode); RL reward = (action-correct) + 0.3·(monologue-coherent).

### 4. Tone / charm consistency
- Persona-vector for "warmth + low-sycophancy" identified by V16 → bake-in via add-during-training (Anthropic showed limits trait shift while preserving general capabilities).
- Source rubric: lechmazur/writing (10 mandatory story elements) + EQ-Bench Elo.

### Expected delta
- EQ-Bench: +5–10 Elo
- ToM 0/1/2-order tasks: +6–18% (per 2509.22887 small-model results)

---

## 7 · Multilingual — Thai-first + top-10 (axis G)

### Why Thai matters
Owner's L1. Granite-4.1-8B reports good multilingual but does not advertise Thai-specific tuning. Aya Expanse 32B (Cohere) covers 23 langs but **not Thai**. Qwen3 covers 119 langs incl. Thai with 36T-token mix.

### Strategy
1. Continued pretrain Granite-4.1-8B with **Thai + SEA replay** (≈ 30B tokens).
2. SFT on Thai instruction packs.
3. DPO with Thai-translated UltraFeedback (auto-translate via Sailor2 critic).
4. Eval on Thai-specific benches.

### Datasets

| Dataset | HF path | License | Take | Weight |
|---|---|---|---|---|
| Sailor2 pretrain pool | derived from `sail/sailor2` corpus | Apache-2.0 | 500B SEA tokens (400B SEA-specific + 100B replay), 13 SEA langs incl. Thai | 25B-token replay |
| Mangosteen | `pythainlp/mangosteen` (per arxiv 2507.14664) | open Thai corpus | Pretraining Thai | 5B-token replay |
| WangchanBERTa training set | airesearch.in.th release | open | 78GB Thai cleaned (social media + news) | 2B-token replay |
| OpenThaiGPT 1.5 instructions | `openthaigpt/openthaigpt-1.5-7b-instruct` data | open | 2M+ Thai instruction pairs | 60% of Thai SFT |
| PyThaiNLP Han-Instruct | `pythainlp/han-instruct-dataset-v1.0` | CC-BY-4.0 | Multi-source Thai instructions | 15% |
| Thaweewat instruct-qa-thai-combined | `Thaweewat/instruct-qa-thai-combined` | various | Thai QA | 10% |
| SEACrowd thai_gpteacher | `SEACrowd/thai_gpteacher` | MIT | Thai instruction tuning | 5% |
| SEACrowd thai_databricks_dolly | `SEACrowd/thai_databricks_dolly` | CC-BY-3.0 | Thai dolly | 5% |
| SEACrowd m3exam (Thai slice) | `SEACrowd/m3exam` | MIT | Thai academic exams | 5% eval+train |
| OpenThaiEval | `iapp/openthaieval` | open | Thai eval | Eval only |
| TruthfulQA-multi (Thai) | `HiTZ/truthfulqa-multi` | MIT | Thai TruthfulQA | Eval only |
| XCOPA Thai | `SEACrowd/xcopa` Thai split | CC-BY-4.0 | Thai commonsense | 1% |
| XNLI Thai | `SEACrowd/xnli` Thai split | CC-BY-NC-4.0 | NLI Thai | Eval |

### Top-10 langs (cover 90% of likely user surface): zh, ja, ko, es, fr, de, vi, id, pt, ar
- Use Aya Expanse 32B as teacher for those langs (covers all 10) → Magpie-style alignment data synthesis in each language.
- Sailor2-20B as Thai+SEA teacher.

### Training patches (Thai-specific)
1. **Tokenizer extension audit** — Granite-4.1 SentencePiece coverage for Thai script. If undersized, do vocabulary-transfer from XLM-R style (PhayaThaiBERT recipe).
2. **Continued pretrain** 30B tokens Thai+SEA before SFT (matches OpenThaiGPT 1.5 recipe of "extend vocab + continual pretrain + instruction").
3. **Thai DPO** — translate UltraFeedback to Thai via Sailor2; use Sailor2-20B as judge.
4. **Thai persona-vector** — derive a Thai-warmth direction; add during fine-tune.
5. **Thai eval gate** — OpenThaiEval + TruthfulQA-Thai must regress no more than 1% from baseline before merge.

### Compatibility
Granite-4.1 BPE tokenizer is multilingual (CommonCrawl Phase 1 = 59% of pretrain). Thai script is in BMP — no surrogate-pair issues. Sailor2-20B teacher API: vLLM-compatible at H100/H200 inference cost.

---

## 8 · Multimodal text-only proxy roadmap (axis H)

### Why proxy
Owner's H200/Kaggle/Modal budget = no spare GPU for true vision encoder pretrain. Surrogate's 80% use case is doc/spec/PDF understanding → text-only proxies cover this without a vision tower.

### Roadmap (Q3–Q4 2026)
1. **PDF/document parsing as text task** — use external parser (GLM-OCR, marker, Docling) to convert PDF → markdown with explicit `<table>`, `<formula>`, `<figure>` tags + alt-text; train Surrogate to consume this format. (Multimodal OCR / MOCR pattern from 2603.13032: "treat text/charts/diagrams/tables/icons as first-class parsing targets converted to unified renderable textual representations".)
2. **Code+image proxy** — replace image with structured ASCII-art-of-DOM, code-comment description, or alt-text captions. SFT 10k pairs.
3. **Audio proxy** — Qwen3-ASR (open-source) → text transcript → consume; defer true audio modality.
4. **Layout-aware text** — preserve coordinates as `(x,y,w,h)` tags in markdown; train on 2k layout-rich docs.
5. **Visual reasoning bridge** — when needed, route to Aya Vision or InternLM-XComposer via tool-call; Surrogate stays text-native.

### Datasets

| Source | License | Take |
|---|---|---|
| GLM-OCR outputs on arXiv PDFs | Apache-2.0 (GLM-OCR) | 50k arXiv PDFs → MOCR markdown |
| Docopilot 2507.14675 | open | Document-level multimodal Qs (use text-only slice) |
| Docling/marker training pairs | MIT | OCR'd corp doc training |

### Compatibility
Pure text — drops directly into V17 SFT pipeline. No vision tower, no image preprocessor. 128k context handles 100-page PDFs.

---

## 9 · Pretraining quality filter (axis-X — applies to all)

Granite-4.1 already uses strong filtering, but for **continued pretraining** in V18 we apply:

| Filter | Source | Use |
|---|---|---|
| FineWeb-Edu classifier | `HuggingFaceFW/fineweb-edu-classifier` | Score every continued-pretrain document |
| Ultra-FineWeb verification | 2505.05427 | Efficient quality verification |
| DCLM model-based filter | `mlfoundations/dclm` | Threshold = top 25% of cluster |
| Decontamination | scrub canary strings (HLE, MMLU-Pro, BrowseComp, GPQA Diamond) and exact-match overlap with eval sets ≥ 13-gram | Mandatory |

---

## 10 · Final V18 training mixture (8B Granite-4.1 base)

### Phase 1: continued pretrain (50–80B tokens, ~7 days H200×8)
- 30% FineWeb-Edu
- 20% DCLM-Baseline
- 25% Sailor2 + Mangosteen + Thai pool
- 8% Cosmopedia v2 (synthetic textbook)
- 4% Wikipedia (en/th/top-10 mix)
- 1% Wikidata-as-text
- 12% replay from Granite-4.1 original mix (avoid catastrophic forgetting)

### Phase 2: SFT (~3M examples, ~2 days)
- 25% Tulu-3 SFT mixture
- 15% Magpie-Pro 1M (subset 200k)
- 15% Magpie-Reasoning V2 250k CoT
- 10% Thai instruction (OpenThaiGPT 1.5 + Han + Thaweewat)
- 8% commonsense suite (HellaSwag, WinoGrande, OBQA, ARC, PIQA, Social-IQa, CSQA, Global-PIQA)
- 8% world-knowledge SFT (TriviaQA, NQ, MMLU-style synth)
- 5% LongWriter-6k + WritingBench-style synth
- 3% counterfactual/causal synth
- 3% claim-evidence binding (cited long-form)
- 2% theory-of-mind (ToMA-style)
- 2% deliberative-alignment (CoT cites safety spec)
- 2% inner-monologue (every example has `<think>` block)
- 2% multilingual top-10 via Aya teacher

### Phase 3: ORPO / DPO (~3 days)
- 60% UltraFeedback
- 20% HH-RLHF helpful + harmless
- 10% Thai-translated UltraFeedback (Sailor2 judge)
- 5% EQ-Bench-style roleplay pairs
- 5% custom red-team binary (KTO branch)

### Phase 4: GRPO + RLCR + SimpleQA + EvidenceRL (~5 days)
- Math/code verifiable reward (V11 carryover)
- TriviaQA short-answer EM reward
- SimpleQA grader reward
- WritingBench critic reward
- FActScore atomic-fact long-form reward
- AbstainBench-aware abstention reward (log-scoring)
- Eval-awareness penalty (CoT shouldn't say "this is a benchmark")
- Sleeper-trigger probe (random year/context, persona-vector ablation if anomaly)

### Phase 5: persona-vector + sanity / merge
- Final pass: add (warmth, calibration, curious-polymath) persona-vectors during last 5% of fine-tune steps.
- Eval gates: MMLU-Pro ≥ baseline +2, OpenThaiEval ≥ baseline –1, EQ-Bench ≥ baseline +5, AbstainBench ≥ baseline +8, SALAD-Bench safety ≥ baseline, OR-Bench over-refusal ≤ baseline +2.

### Compute budget (H200 single-node 8×141GB)
- Pretrain: 7 days
- SFT: 2 days (LoRA r=64 α=128 on attention + MLP)
- DPO/ORPO: 3 days
- GRPO/RLCR: 5 days
- Total: ~17 days end-to-end (one full V18 run)

---

## 11 · Top-line cuts for owner's 35-line summary

### (a) Top 10 datasets for general intelligence + world knowledge
1. FineWeb-Edu (30% of continued pretrain — biggest single MMLU lift)
2. DCLM-Baseline (20%, complements FineWeb-Edu)
3. Cosmopedia v2 (8%, synthetic textbook quality)
4. Tulu-3 SFT mixture (25% of SFT phase, covers safety/IF/writing)
5. Magpie-Pro 1M + Magpie-Reasoning V2 250k (15+15% SFT)
6. UltraFeedback (60% of pref-opt phase)
7. TriviaQA + NQ + SimpleQA-Verified (RL reward signal for facts)
8. HH-RLHF + HHH alignment (alignment core)
9. LongWriter-6k + WritingBench (long-form writing)
10. Wikipedia + Wikidata (replay grounding)

### (b) Top 5 alignment techniques beyond V11 stack
1. Constitutional AI / RLAIF with dynamic constitutions (40% alignment-failure reduction per 2026 Anthropic)
2. Deliberative alignment (OpenAI 2412.16339) — CoT cites safety spec; jailbreak↓ AND over-refusal↓
3. ORPO + KTO over UltraFeedback + HH-RLHF (single-model memory; binary feedback)
4. Sleeper-agent prevention via persona-vector ablation + date-trigger audit
5. Eval-awareness suppression (post Opus 4.6 BrowseComp finding) + AbstainBench log-score reward

### (c) Thai-language specific training data + technique
- **Data**: OpenThaiGPT 1.5 (2M+ Thai instruction pairs, Qwen v2.5 base) + PyThaiNLP Han-Instruct + Thaweewat combined + SEACrowd Thai (gpteacher, dolly, m3exam, xcopa, xnli) + Mangosteen Thai pretrain + Sailor2 SEA pool (500B tokens, Thai included).
- **Technique**: (1) tokenizer-vocab audit (PhayaThaiBERT-style XLM-R transfer if undersized), (2) 30B-token Thai+SEA continued pretrain on Granite-4.1-8B before SFT, (3) Sailor2-20B as Thai DPO judge for Thai-translated UltraFeedback, (4) Thai persona-vector for cultural warmth, (5) hard eval gates on OpenThaiEval + TruthfulQA-Thai (≤1% regression vs baseline).

### (d) Multimodal text-only proxy roadmap
- **Q3 2026**: PDF→markdown via GLM-OCR / marker / Docling (MOCR pattern: text/tables/figures/formulas as first-class textual representations). 50k arXiv-PDF training pairs.
- **Q4 2026**: layout-aware text (coordinate tags), code+image alt-text proxy, audio→Qwen3-ASR transcript-only.
- **2027 (deferred)**: native vision tower if GPU budget allows, else route via Aya Vision tool-call.
- **Compatibility**: zero changes to V18 trainer — pure text in, pure text out.

### Compatibility note (V17 trainer + Granite-4.1-8B base swap)
All datasets load via HF `datasets`; all techniques use TRL ≥0.10 (SFT/DPO/ORPO/KTO/GRPO) + PEFT-LoRA/QLoRA on H200 single-node. Granite-4.1-8B is dense decoder-only Apache-2.0 with GQA + RoPE + SwiGLU + RMSNorm + 128k context — full PEFT support, no MoE routing complications, drop-in for V17's `AutoModelForCausalLM.from_pretrained` calls. Persona-vector ablation hooks into residual stream pre-RMSNorm at every layer (V16 implementation already supports dense decoder).

---

## See also
- [[v17-catchup-multi-teacher-distill]]
- [[v17-longctx-and-inference-speed]]
- [[v17-moe-sublora-composition]]
- [[v16-bleeding-edge-may2026]]
- [[v16-data-scale-and-hf-sweep]]
- [[anti-hallucination-correctness-2026]]
- [[persona-imitation-ai-2026]]
- [[v14-rl-frontier-beyond-dapo]]

## Sources
- MMLU-Pro: https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro
- MMLU-Redux: https://huggingface.co/datasets/edinburgh-dawg/mmlu-redux
- HLE: https://huggingface.co/datasets/cais/hle ; https://agi.safe.ai/
- SimpleQA / SimpleQA-Verified: https://arxiv.org/pdf/2509.07968 ; https://arxiv.org/html/2411.04368v1
- FActScore: https://www.researchgate.net/publication/370981225_FActScore_Fine-grained_Atomic_Evaluation_of_Factual_Precision_in_Long_Form_Text_Generation
- POPE: https://arxiv.org/pdf/2305.10355
- Constitutional AI: https://arxiv.org/abs/2212.08073
- Deliberative alignment: https://arxiv.org/abs/2412.16339 ; Apollo stress test https://www.arxiv.org/pdf/2509.15541
- Sleeper Agents: https://arxiv.org/abs/2401.05566
- Eval awareness BrowseComp: https://www.anthropic.com/engineering/eval-awareness-browsecomp
- Persona vectors: https://arxiv.org/abs/2507.21509 ; https://www.anthropic.com/research/persona-vectors
- ORPO/DPO/KTO: https://arxiv.org/html/2403.07691v2 ; https://unsloth.ai/docs/get-started/reinforcement-learning-rl-guide/preference-dpo-orpo-and-kto
- HH-RLHF: https://huggingface.co/datasets/Anthropic/hh-rlhf
- UltraFeedback / UltraChat: https://huggingface.co/datasets/openbmb/UltraFeedback ; https://github.com/thunlp/UltraChat
- Magpie: https://github.com/magpie-align/magpie ; https://huggingface.co/datasets/Magpie-Align/Magpie-Reasoning-V2-250K-CoT-Llama3
- Tulu-3: https://allenai.org/blog/tulu-3-technical ; https://huggingface.co/datasets/allenai/tulu-3-sft-mixture
- FineWeb-Edu / DCLM: https://arxiv.org/abs/2406.17557 ; https://github.com/mlfoundations/dclm ; https://arxiv.org/abs/2406.11794
- Cosmopedia v2: https://huggingface.co/blog/cosmopedia
- SmolLM3 SFT recipe: https://huggingface.co/blog/smollm3
- Granite-4.1-8B: https://huggingface.co/ibm-granite/granite-4.1-8b ; https://research.ibm.com/blog/granite-4-1-ai-foundation-models
- Sailor2: https://arxiv.org/html/2502.12982v1 ; https://sea-sailor.github.io/blog/sailor2/
- OpenThaiGPT 1.5: https://arxiv.org/html/2411.07238v2
- WangchanBERTa: https://arxiv.org/abs/2101.09635
- Mangosteen: https://arxiv.org/html/2507.14664v1
- Aya Expanse: https://huggingface.co/CohereLabs/aya-expanse-32b ; https://huggingface.co/blog/aya-expanse
- Qwen3 multilingual 119: https://arxiv.org/abs/2505.09388
- Social-IQa: https://huggingface.co/datasets/allenai/social_i_qa ; https://arxiv.org/abs/1904.09728
- Theory of Mind / ToMA: https://arxiv.org/html/2509.22887v2 ; https://aclanthology.org/2025.acl-long.1522.pdf
- Inner monologue / SRL: https://arxiv.org/html/2510.16340 ; https://arxiv.org/html/2510.25992v1
- LongWriter / WritingBench: https://openreview.net/forum?id=kQ5s9Yh0WI ; https://arxiv.org/html/2503.05244v4
- EQ-Bench: https://eqbench.com/
- CounterBench / CausalARC: https://arxiv.org/abs/2502.11008 ; https://arxiv.org/abs/2509.03636
- EvidenceRL / CiteGuard / PaperTrail: https://arxiv.org/html/2603.19532 ; https://arxiv.org/html/2510.17853v3 ; https://arxiv.org/html/2602.21045
- AbstentionBench / I-CALM / log-score: https://arxiv.org/html/2604.03904v1 ; https://www.arxiv.org/pdf/2511.11500
- SALAD-Bench / OR-Bench / JailbreakBench / VAL-Bench: https://arxiv.org/html/2402.05044v4 ; https://arxiv.org/html/2405.20947v5 ; https://jailbreakbench.github.io/ ; https://arxiv.org/html/2510.05465v1
- GLM-OCR / Multimodal OCR: https://github.com/zai-org/GLM-OCR ; https://beta.hyper.ai/en/papers/2603.13032
- Docopilot: https://arxiv.org/html/2507.14675v1
- PyThaiNLP collections: https://huggingface.co/collections/pythainlp/datasets-for-pretrained-thai-llm-65db96ab730386b492889a98 ; https://huggingface.co/collections/pythainlp/thai-instruction-dataset-list-65e1bff89944a443290ff040
- SEACrowd Thai: https://huggingface.co/datasets/SEACrowd/thai_gpteacher ; https://huggingface.co/datasets/SEACrowd/thai_databricks_dolly ; https://huggingface.co/datasets/SEACrowd/m3exam
- Global PIQA: https://huggingface.co/datasets/mrlbenchmarks/global-piqa-nonparallel
- HHH alignment: https://huggingface.co/datasets/HuggingFaceH4/hhh_alignment
- TruthfulQA: https://github.com/sylinrl/TruthfulQA ; https://huggingface.co/datasets/HiTZ/truthfulqa-multi
- WikiBigEdit: https://arxiv.org/html/2503.05683

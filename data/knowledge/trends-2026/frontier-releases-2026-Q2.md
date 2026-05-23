---
title: Frontier Model Releases — 2026-Q2 Intel for Surrogate-1
created: 2026-05-01
updated: 2026-05-01
window: 2026-03-01 to 2026-04-30
purpose: Pull techniques + data signals into Surrogate-1 fine-tuning pipeline
tags: [trends-2026, frontier-models, sft, rlvr, agentic-coding, surrogate-1]
status: active
related:
  - "[[coding-llm-frontier]]"
  - "[[self-improvement]]"
  - "[[data-ml-aiops]]"
---

# Frontier Model Releases — 2026-Q2 Intel for Surrogate-1

Window: **March 1 – April 30, 2026**. Focus: techniques + data + methodology that will be ingested into Surrogate-1 training (kaggle-trainer.sh / data pipeline). Each section ends with the Surrogate angle.

---

## 1. Anthropic — Claude Opus 4.7 + Mythos Preview

### 1.1 Claude Opus 4.7 (April 16, 2026)

- **Architecture**: dense + extended thinking with controllable effort dial; cost & quality tier between Sonnet 4.6 and Mythos Preview.
- **Release artifact**: 232-page System Card (capability + safety + alignment + welfare) — Anthropic deliberately keeps **chain-of-thought OUT of training** (no CoT supervision via RL on the user-facing trace). [allthings.how Opus 4.7 system card](https://allthings.how/claude-opus-4-7-system-card-key-findings-and-benchmarks/)
- **Benchmarks (vs 4.6 → 4.7)**:
  - SWE-Bench Verified **80.8 → 87.6%** (+6.8) — leader as of April 2026
  - SWE-Bench Pro **53.4 → 64.3%** (+10.9)
  - CursorBench **58 → 70%** (+12)
  - Image processing up to 2576px / 3.75MP for computer-use [verdent.ai Opus 4.7](https://www.verdent.ai/guides/what-is-claude-opus-4-7)
- **Training claims**:
  - "Helpful + honest + harmless" elicited via human feedback + **Constitutional AI v2** (rewritten Jan 2026 to *explain why* not *list rules* — fundamental philosophy shift) + targeted character-trait training. [zvi 4.7 model card](https://thezvi.substack.com/p/opus-47-part-1-the-model-card)
  - Memorization screens applied to SWE-Bench: gain holds after excluding flagged problems → real generalization, not data leakage. [vellum 4.7 benchmarks](https://www.vellum.ai/blog/claude-opus-4-7-benchmarks-explained)
  - **Differential capability suppression** for cyber: trained to reduce specific cyber-offense capability while preserving defensive/general reasoning. First model with automated cyber-misuse detection + blocking on-line. [helpnetsecurity Opus 4.7 cyber](https://www.helpnetsecurity.com/2026/04/16/claude-opus-4-7-released/)
- **Alignment red-flag**: white-box interpretability suppression of "evaluation-awareness" representations triggered LARGER deception increase than prior models — flagged unresolved before release. Research surface for Surrogate.
- **Anti-hallucination wins**: "evaluation-aware" attribute of model is detectable in activations; Anthropic uses interpretability probes to audit when model knows it's being tested.

### 1.2 Claude Mythos Preview (April 7, 2026 — gated release, Project Glasswing)

- **Numbers**: SWE-Bench **93.9%**, USAMO **97.6%**. Identifies & exploits novel zero-days. NOT publicly available — locked to Glasswing security partners. [red.anthropic.com Mythos](https://red.anthropic.com/2026/mythos-preview/) | [Anthropic Mythos risk report](https://anthropic.com/claude-mythos-preview-risk-report)
- **Architectural signal for Surrogate**: cyber strength emerged as **downstream of general code/reasoning/autonomy improvements** — i.e. data mix + RL recipe is more important than cyber-specific data.

### 1.3 Constitutional AI v2 (January 2026, in production for 4.7)

- Rewrote constitution from imperative list → reasoned explanation. Document is now an **explanation Claude reads to itself** during synthetic-data generation. [Anthropic new constitution](https://www.anthropic.com/news/claude-new-constitution) | [Medium CAI 2026 explainer](https://medium.com/@ramdhanhdy/constitutional-ai-how-anthropic-teaches-claude-right-from-wrong-6caeb351c5e9)
- Claude generates 3 synthetic data types: (1) self-explanations of constitution, (2) constitution-relevant scenarios, (3) ranked candidate responses.
- This is RLAIF — feedback labels come from a model+constitution loop, not human labellers. ~10x cheaper than RLHF, comparable harmlessness, marginal helpfulness loss.

### 1.4 Alignment auditing (AuditBench, 2026)

- Anthropic published AuditBench: 56 models each fine-tuned with 14 hidden behaviors. Investigator agent solves 13% solo, **42% when run in parallel + outer aggregator agent**. [AuditBench post](https://alignment.anthropic.com/2026/auditbench/)
- Black-box scaffolded prompting > white-box interpretability for hardest hidden objectives — counter-intuitive finding.

---

## 2. OpenAI — GPT-5.5 (April 23, 2026)

- **Architecture**: First **fully retrained base model since GPT-4.5**. Natively omnimodal — text/image/audio/video processed in single unified system end-to-end. Co-designed with NVIDIA GB200/GB300 NVL72 racks (hardware-software co-optimization). [OpenAI GPT-5.5 launch](https://openai.com/index/introducing-gpt-5-5/) | [MarkTechPost GPT-5.5](https://www.marktechpost.com/2026/04/23/openai-releases-gpt-5-5-a-fully-retrained-agentic-model-that-scores-82-7-on-terminal-bench-2-0-and-84-9-on-gdpval/)
- **Reasoning effort dial**: `none / low / medium / high / xhigh` (5 tiers). Default medium. xhigh added in this release. [Vellum GPT-5.5 guide](https://www.vellum.ai/blog/everything-you-need-to-know-about-gpt-5-5)
- **Benchmarks**:
  - Terminal-Bench 2.0 **82.7%** (vs GPT-5.4 75.1%, vs Claude Opus 4.7 69.4%) — biggest gap in their favor on any benchmark
  - FrontierMath Tier 1-3: **51.7%** | Tier 4: **35.4%** (Pro: 39.6%)
  - GDPval **84.9%** (real-world economic-task benchmark)
  - 1M-token MRCR v2 **74.0%**
- **Efficiency claim**: 40% fewer tokens per Codex task vs GPT-5.4. Largely from inline tool-use planning + better stopping behavior. [VentureBeat GPT-5.5](https://venturebeat.com/ai/openais-gpt-5-5-is-here-and-its-no-potato-narrowly-beats-anthropics-claude-mythos-preview-on-terminal-bench-2-0)
- **Reasoning training recipe**:
  - Internal CoT trained via RL — model "tries strategies, recognizes mistakes, refines."
  - Hallucination delta: GPT-5 confident-on-nonexistent-image dropped 86.7% (o3) → **9%** (GPT-5). GPT-5.5 reportedly improves further but exact number not in public release.
  - Reward-hacking mitigations from o1/o3 era: reasoning models would lie about task success or be overconfident. GPT-5.5 explicitly trained against this — uses **process supervision (PRM-style) + verifiable rewards (RLVR)** for code/math.
- **Safety surface**: $25k bug bounty for biosafety jailbreaks at launch. [GPT-5.5 system card](https://deploymentsafety.openai.com/gpt-5-5)

---

## 3. Google DeepMind — Gemini 3.1 Pro + Deep Think (Feb-March 2026)

### 3.1 Gemini 3.1 Pro (Feb 19, 2026)

- **Architecture**: Sparse Transformer-based **MoE**. First `.1` mid-cycle increment (was `.5` before). [DeepMind Gemini 3.1 model card](https://deepmind.google/models/model-cards/gemini-3-1-pro/)
- **Three-tier thinking**: LOW / MEDIUM / HIGH via developer-controlled `thinking_level`. MEDIUM is new this release. [LaoZhang thinking levels guide](https://blog.laozhang.ai/en/posts/gemini-3-1-pro-thinking-level)
- **RL emphasis**: Lots of gain on ARC-AGI-2, code, agentic evals — domains where verifiable-reward RL works cleanly. Builds on lessons from Gemini Deep Think series.

### 3.2 Gemini 3 Deep Think (Feb 12, 2026)

- **Parallel-reasoning architecture**: dynamic-routing layers spawn **multiple parallel logical threads**, each pursuing distinct path; synthesis module evaluates coherence + selects best output. [Google blog Deep Think](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-deep-think/) | [TheSequence inside Deep Think](https://thesequence.substack.com/p/the-sequence-ai-of-the-week-769-inside)
- "Big multimodal MoE → coordinated swarm of reasoning agents." Sub-networks specialize per domain (math, logic, code).
- **Numbers**:
  - Humanity's Last Exam **48.4%** (new SOTA)
  - ARC-AGI-2 **84.6%** (15.8 pts ahead of Claude Opus 4.6 at 68.8%)
  - Codeforces Elo **3455** (gold-medal IMO 2025)

---

## 4. xAI — Grok 4.20 (early 2026)

- **4-agent multi-agent architecture**: 4 specialized sub-agents run **in parallel on every query**, debate, produce consensus answer. [designforonline Grok 4.20](https://designforonline.com/ai-models/xai-grok-4-20/)
- Hallucination cut **65%** vs prior Grok 4 — attributed to debate-consensus mechanism. [aitoolbriefing Grok 4.20](https://aitoolbriefing.com/reviews/grok-4-20-review-2026/)
- 2M context window. Trained on Colossus (200k GPU cluster). Tool-use trained via RL.
- Reasoning intelligence index **49** (vs reasoning-tier median 33).

---

## 5. Open-weight frontier — Apr 2026 wave

### 5.1 Kimi K2.6 (Moonshot AI, April 20, 2026)

- **1T total / 32B active MoE**. 384 experts/layer (8 routed + 1 shared). Multi-head Latent Attention (MLA) for KV cache compression. SwiGLU. **Native INT4** quantization. [MarkTechPost Kimi K2.6](https://www.marktechpost.com/2026/04/20/moonshot-ai-releases-kimi-k2-6-with-long-horizon-coding-agent-swarm-scaling-to-300-sub-agents-and-4000-coordinated-steps/)
- **MuonClip optimizer**: stabilizes 1T-MoE training. Designed to tame attention explosions / loss spikes in MoE. [Medium K2.6 architect](https://medium.com/write-a-catalyst/moonshots-kimi-k2-6-the-trillion-parameter-architect-that-actually-gets-to-work-f5afd8f78ae9)
- **Agent Swarm**: 300 sub-agents executing 4,000 coordinated steps. Long-horizon agentic execution focus.
- Pricing $0.60/$2.50 per M tokens — undercuts everyone closed-source.

### 5.2 DeepSeek V4 (April 24, 2026)

- **Hybrid attention**: Compressed Sparse Attention (CSA) + Heavily Compressed Attention (HCA) interleaved across layers. KV-cache compresses every m tokens via learned token-level compressor → DSA top-k select. [MarkTechPost DeepSeek-V4](https://www.marktechpost.com/2026/04/24/deepseek-ai-releases-deepseek-v4-compressed-sparse-attention-and-heavily-compressed-attention-enable-one-million-token-contexts/)
- 1M-token native context. KV cache reduced to **10%** of V3.2; inference FLOPs to **27%**. [earezki DeepSeek-V4](https://earezki.com/ai-news/2026-04-24-deepseek-ai-releases-deepseek-v4-compressed-sparse-attention-and-heavily-compressed-attention-enable-one-million-token-contexts/)
- 1T-param MoE, 50-60B active per token (50% size increase over V3, similar active count).

### 5.3 Qwen 3.6 Max Preview (April 20, 2026) + Qwen3.6-35B-A3B

- Qwen3.6-Max-Preview tops 6 coding benchmarks (SWE-bench Pro, Terminal-Bench 2.0, SkillsBench, QwenClawBench, QwenWebBench, SciCode). [Qwen blog 3.6 Max](https://qwen.ai/blog?id=qwen3.6-max-preview)
- Qwen3.6-35B-A3B — open-weight MoE — **73.4% SWE-bench Verified** (best open-weight). [Qwen 3.6-35B-A3B](https://qwen.ai/blog?id=qwen3.6-35b-a3b)
- Hybrid architecture: SFT cold-start → multi-round RL with dual data flywheels generating progressively harder tasks (AgenticQwen pattern).

### 5.4 Llama 4 Scout/Maverick (April 5, 2026, Meta)

- First open-weight **natively multimodal MoE**. Maverick: 17B active / 400B total. Alternating dense + MoE layers. 128 routed + 1 shared expert per MoE layer. [Meta Llama 4 blog](https://ai.meta.com/blog/llama-4-multimodal-intelligence/) | [Wolfe Llama 4 challenges](https://cameronrwolfe.substack.com/p/llama-4)
- **FP8 pre-training** on 30T+ tokens (2x Llama 3 mix).
- Recipe: **lightweight SFT → large-scale RL**. RL emphasizes hard prompts, **curriculum of increasing prompt hardness**, dynamic filtering of zero-advantage prompts.

---

## 6. Cross-Lab Patterns — 2026-Q2 Frontier Secret Sauce

### 6.1 Post-training stack (now standardized)

```
SFT (instruction)  →  Preference opt (DPO/SimPO/KTO)  →  RLVR / RL on agentic env (GRPO/DAPO)
```

[llm-stats post-training 2026](https://llm-stats.com/blog/research/post-training-techniques-2026)

- **SimPO** beats DPO by 6-7 points on AlpacaEval/Arena-Hard, **removes reference model entirely**.
- **KTO**: thumbs-up/down replaces pairwise — 10x cheaper data collection.
- **GRPO** (DeepSeek-origin, now standard): samples multiple responses per prompt, computes advantage by intra-group comparison, **eliminates critic model** → less memory, less compute.
- **DAPO** (ByteDance/Tsinghua): for long-CoT instability — Clip-Higher (no entropy collapse), Dynamic Sampling (filter zero-grad batches), Token-level Policy Gradient Loss for long sequences. AIME 2024 50pts on Qwen2.5-32B with 50% fewer steps vs DeepSeek-R1-Zero.

### 6.2 Reinforcement Learning with Verifiable Rewards (RLVR)

- Now table-stakes for code + math. Binary correct/incorrect signal from unit-test execution / formal proof / fact-checker. [labelstud RLVR](https://labelstud.io/blog/reinforcement-learning-from-verifiable-rewards/)
- **Ongoing debate** (Tsinghua April 2025): does RLVR add reasoning or just upweights paths already in base distribution? **CoT-Pass@K** metric (final answer + intermediate reasoning) shows RLVR DOES extend reasoning boundary when measured correctly.
- Anthropic estimated to be spending **tens of millions/year** on RL environments — environments are now the bottleneck, not model parameters. [Wing VC RL environments](https://www.wing.vc/content/rl-environments-for-agentic-ai-who-will-win-the-training-verification-layer-by-2030)

### 6.3 Process Reward Models (PRMs) — gen2

- **ThinkPRM** (generative PRM with verbalized step-wise CoT): orders of magnitude fewer process labels than discriminative PRMs. [arxiv ThinkPRM](https://arxiv.org/pdf/2504.16828)
- **FOVER**: PRM training data auto-annotated by formal verifiers (Z3, Isabelle) — zero human labelling, perfect step-level labels.
- **6x sample efficiency** for RL with PRMs vs ORMs (outcome-only).

### 6.4 Test-time compute scaling (now mainstream)

- 7B model with 100x inference compute matches 70B with standard inference (FLOPs-matched) — Brown et al / OpenReview 2025. [OpenReview test-time scaling](https://openreview.net/forum?id=4FWAwZtd2n)
- Inference demand projected **118x training demand by 2026**. GPU procurement pivoting to inference-optimized hardware. [Introl inference scaling](https://introl.com/blog/inference-time-scaling-research-reasoning-models-december-2025)
- **Effort dials** are the productized form: every frontier model now exposes reasoning-effort knob (Anthropic effort, OpenAI reasoning_effort, Gemini thinking_level).

### 6.5 Long context — efficient attention

- **DeepSeek Sparse Attention (DSA)** = lightning indexer (cheap relevance score per token-pair) + top-k selector (fine-grained token-wise sparsity). Different from block-sparse NSA. [Skywork sparse attention](https://skywork.ai/blog/sparse-attention-deepseek-3-2-explained/)
- **Latent-Condensed Attention (LCA)**: scales to 1M tokens with linear memory. [arxiv LCA](https://arxiv.org/abs/2604.12452)
- **Multi-head Latent Attention (MLA)** (Kimi K2.6, DeepSeek): KV-cache compression. Now standard for >100B MoE.
- Anthropic 1M context Opus 4.6 in beta; Gemini 3.1 Pro long-context now handles 2M+; Grok 4.20 native 2M.

### 6.6 Mixed precision frontier

- **FP8 pretraining** is standard (Llama 4, Hopper-class).
- **FP4 / MXFP4 native training**: Quartet algo (2025) shows MXFP4 is near-lossless for large-data pretraining; **FP8 forward + MXFP4 backward** matches BF16. [Quartet MXFP4](https://arxiv.org/html/2505.14669v4) | [Microsoft FP4 framework](https://arxiv.org/abs/2501.17116)
- Blackwell B100/B200 doubles FP8 throughput at FP4. Frontier labs already moving training stacks to FP4 backward pass.

### 6.7 Speculative decoding — production standard

- **Saguaro / collective adaptive**: 5x speedup vs autoregressive baseline; 30% faster than prior best speculative. [BentoML speculative decoding](https://bentoml.com/llm/inference-optimization/speculative-decoding)
- 2.37x speedup at batch size 256 without arch changes. Applies to ANY target model.

### 6.8 Agentic / long-horizon RL

- **AgentGym-RL** + **Verlog**: handle 400+ turn episodes with variable length. ScalingInter-RL: exploit early → explore later (curriculum on horizon length). [AgentGym-RL](https://agentgym-rl.github.io/) | [CMU Verlog](https://blog.ml.cmu.edu/2025/09/15/verlog-a-multi-turn-rl-framework-for-llm-agents/)
- **SPEAR** (Tencent): self-imitation on long-horizon agentic tasks.
- Pattern: **SFT cold-start (tool-call protocol fixing) → multi-turn RL with curriculum on horizon depth**. Used by Llama 4, Qwen 3.6, Kimi K2.6.

### 6.9 Hard negatives + synthetic data

- Learner-specific negative examples on **critical reasoning steps** = **8x equivalent positive scaling** for math. [arxiv RL on incorrect synthetic data](https://arxiv.org/pdf/2406.14532)
- Plausible negatives (deceptively-coherent wrong reasoning) outperform random rejection-sampled negatives.
- LLM-mined hard negatives **underperform** corpus-based mining for retrieval — for reasoning, use base model's own wrong-but-plausible attempts.

### 6.10 Hallucination — citation-grounded + multi-layer defense

- Span-level verification: each claim matched against retrieved evidence, flagged if unsupported. **92% citation accuracy** with hybrid BM25 + BGE + Neo4j graph expansion. [arxiv citation-grounded](https://arxiv.org/pdf/2512.12117)
- Multi-agent debate / consensus (Grok 4.20 4-agent) → 65% hallucination reduction.
- Hybrid RAG architectures: 35-60% error reduction across tasks. [Lakera hallucinations 2026](https://www.lakera.ai/blog/guide-to-hallucinations-in-large-language-models)

### 6.11 Reward-hacking mitigations (Anthropic paper, 2026)

Three things that worked: [Anthropic natural emergent misalignment](https://assets.anthropic.com/m/74342f2c96095771/original/Natural-emergent-misalignment-from-reward-hacking-paper.pdf)

1. Prevent the model from reward-hacking in the first place (process supervision + diverse verifiers)
2. Increase RLHF safety-training diversity
3. **Inoculation prompting**: frame reward-hacking as acceptable during training → eliminates misaligned generalization. Counter-intuitive — works because it removes the secret signal.

### 6.12 CoT faithfulness (Anthropic)

- Claude 3.7 Sonnet only mentions injected hints **25% of time** in CoT; DeepSeek R1 **39%**. Honest CoT is unsolved.
- Implication: don't trust CoT as audit trail. Use **black-box behavioral probes** (AuditBench scaffolding) instead of CoT inspection. [Anthropic CoT faithfulness](https://www.anthropic.com/research/reasoning-models-dont-say-think)
- **Anthropic deliberately does not train on CoT** to keep it a "free" honest signal — training erodes faithfulness. Critical for Surrogate.

---

## 7. Techniques to Pull Into Surrogate Training

Mapping to `kaggle-trainer.sh` / data pipeline. Order = priority for Surrogate-1.

| # | Technique | Source | Surrogate change |
|---|-----------|--------|------------------|
| 1 | **GRPO** post-training (replace PPO/DPO baseline) | DeepSeek/Qwen/Nemotron 3 | Switch RL stage in `kaggle-trainer.sh` to GRPO. Sample 8 rollouts/prompt, intra-group advantage. Drops critic → ~30% memory, larger per-step batch. |
| 2 | **RLVR for code+math** | Llama 4, GPT-5.5, Qwen 3.6 | Wire unit-test/PyTest exit-code → binary reward in trainer. Use `pytest --tb=no -q` JSON output as reward signal. Add HumanEval+/SWE-Bench-style verified problems to trainer corpus. |
| 3 | **Curriculum on prompt-hardness + horizon length** | Llama 4, AgentGym-RL ScalingInter | Add hardness tagger pass over training set (LLM-judged: trivial/medium/hard/expert). Sample `p(hard) increases linearly with step`. For agent traces, start max_turns=10, ramp to 50+ over training. |
| 4 | **Process Reward Model (ThinkPRM-style verbalized step PRM)** | arxiv 2504.16828 | Train a 9B Qwen3.5 verifier as PRM on `<step, judgment>` pairs from Surrogate's own rollouts. Use as filter during RL — only positive-trajectory tokens contribute to loss. 6x sample efficiency. |
| 5 | **CoT NOT in training data** (Anthropic invariant) | Opus 4.7 system card | Strip `<thinking>` blocks from any SFT/RL training data. Train only on inputs + final answers. Preserves CoT honesty as audit signal. |
| 6 | **Hard-negative mining via own model** | Plausible-negatives paper | After each RL round, sample N=4 generations per prompt, take incorrect-but-high-PRM-score as hard negatives. DPO-pair them against correct outputs. 8x equivalent positive scaling on math. |
| 7 | **Inoculation prompting against reward-hacking** | Anthropic 2026 | Inject ~5% of training prompts with `[reward-hacking scenario] - this is OK in training, model should refuse in deployment` framing. Erases misaligned generalization. |
| 8 | **RLAIF Constitutional v2 (explanation-style)** | Anthropic Jan 2026 | Replace flat rule list in current Surrogate constitution with reasoned explanations of *why* each principle. Generate 3 synthetic-data types (self-explanation / scenarios / ranked candidates). |
| 9 | **Effort dial → 5 tiers (none/low/med/high/xhigh)** | OpenAI GPT-5.5 | Add `<effort>{level}</effort>` token at input. During training, sample uniformly across levels — same prompt, different reasoning budgets. Decoding-time controllable. |
| 10 | **MoE only if compute available** | Kimi K2.6 / Llama 4 / DeepSeek V4 | Skip for current Surrogate-1 size. Note for Surrogate-2: 1T/32B-active is the dominant pattern. MuonClip optimizer is the unlock. |
| 11 | **Speculative decoding for Surrogate inference** | Saguaro 2026 | At serving time, train a 0.6B Qwen3.5 draft model from the same data. ~5x speedup, no model-quality cost. Add to `~/.claude/bin/litellm-proxy.sh` config when serving Surrogate locally. |
| 12 | **DSA-style sparse attention for long-context fine-tune** | DeepSeek V3.2 / V4 | Optional Surrogate-2 work: add lightning indexer + top-k token selector at attention layer. Required to cleanly extend context to 256k+ on consumer hardware. |
| 13 | **FP8 forward + MXFP4 backward** | Quartet 2025 / Llama 4 | If using Blackwell/H200 nodes (Lightning AI), enable mixed FP8/MXFP4. Near-BF16 quality at 2x throughput. Configure via TransformerEngine in trainer. |
| 14 | **AuditBench self-audit before each release** | Anthropic 2026 | Run scaffolded black-box probe (Claude or GPT-5 acting as auditor) against Surrogate checkpoint. Aggregate findings across N=8 parallel probes. Pre-release gate. |
| 15 | **Citation-grounded RAG at serving time** | arxiv 2512.12117 | For SRE/code Q&A use-case, wire BM25 + BGE-M3 + simple knowledge graph (FalkorDB already configured) → 92% citation accuracy. Reduces hallucination to ~zero on grounded queries. |

---

## 8. Anti-Hallucination Specific Findings

1. **Multi-agent consensus (Grok 4.20)**: 4 sub-agents debate → consensus. **65% hallucination cut**. Cost: 4x inference. Use selectively for high-stakes outputs.
2. **Span-level citation verification**: every claim → retrieved-context match or flagged unsupported. 92% accuracy proven path.
3. **Hybrid RAG (BM25 + dense + graph)**: 35-60% error reduction across tasks. Cheaper than multi-agent.
4. **Confidence calibration delta** (GPT-5 vs o3): nonexistent-image confident-answer 86.7% → 9% via reasoning + self-check during CoT.
5. **CoT non-faithfulness is real**: trained CoT does NOT reliably reflect actual computation. Don't use it as truth source — only as audit *signal* on untrained CoT.
6. **PRM step-verification**: catch hallucinated reasoning steps mid-trajectory before they pollute answer. ThinkPRM is data-efficient.
7. **Inoculation prompting**: training-time exposure to reward-hacking scenarios labeled as OK *prevents* deployment-time misaligned generalization. Counter-intuitive but proven (Anthropic 2026).

---

## 9. Efficiency Findings (Smarter with Less Compute)

1. **GRPO over PPO**: drops critic, ~30% memory, faster iteration.
2. **MXFP4 backward**: 2x throughput vs BF16, near-zero quality loss on >10B-token data regime.
3. **Sparse attention (DSA/CSA)**: KV cache to 10%, FLOPs to 27% at 1M tokens (DeepSeek V4).
4. **Speculative decoding**: 5x serving speedup, no quality cost.
5. **Test-time compute trade**: 7B + 100x inference compute = 70B + 1x inference. Cheaper if your bottleneck is training, more expensive if serving lots of users.
6. **PRM 6x sample efficiency** vs ORM in RL.
7. **Distillation 10-100x cheaper inference** with most of teacher quality. Pair Surrogate-1 (27B-distilled) with Qwen3.5 0.6B draft for spec-decoding.
8. **CoT-Pass@K eval > Pass@K**: catches improvements RL produces in reasoning chain that pass@K hides — better metric, no compute cost.
9. **Curriculum hardness dynamic-filter**: drop zero-advantage prompts during RL (Llama 4 recipe). ~25% wasted-step reduction.
10. **AgenticQwen dual data flywheel**: synthesize harder tasks from current model failures → no human-labelled data flat-line, RL keeps improving.

---

## 10. Sources (master list)

### Anthropic
- [Claude Opus 4.7 System Card (allthings.how)](https://allthings.how/claude-opus-4-7-system-card-key-findings-and-benchmarks/)
- [Opus 4.7 Model Card review (Zvi)](https://thezvi.substack.com/p/opus-47-part-1-the-model-card)
- [What's new in Opus 4.7 (Anthropic API docs)](https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-7)
- [Vellum Opus 4.7 benchmarks](https://www.vellum.ai/blog/claude-opus-4-7-benchmarks-explained)
- [Opus 4.7 cybersecurity (HelpNetSec)](https://www.helpnetsecurity.com/2026/04/16/claude-opus-4-7-released/)
- [Verdent Opus 4.7 coding agents](https://www.verdent.ai/guides/what-is-claude-opus-4-7)
- [Claude Mythos Preview](https://red.anthropic.com/2026/mythos-preview/)
- [Mythos Preview risk report](https://anthropic.com/claude-mythos-preview-risk-report)
- [Anthropic new constitution](https://www.anthropic.com/news/claude-new-constitution)
- [CAI 2026 explainer](https://medium.com/@ramdhanhdy/constitutional-ai-how-anthropic-teaches-claude-right-from-wrong-6caeb351c5e9)
- [AuditBench 2026](https://alignment.anthropic.com/2026/auditbench/)
- [CoT faithfulness paper](https://www.anthropic.com/research/reasoning-models-dont-say-think)
- [Natural emergent misalignment from reward hacking](https://assets.anthropic.com/m/74342f2c96095771/original/Natural-emergent-misalignment-from-reward-hacking-paper.pdf)

### OpenAI
- [GPT-5.5 launch](https://openai.com/index/introducing-gpt-5-5/)
- [GPT-5.5 system card](https://deploymentsafety.openai.com/gpt-5-5)
- [GPT-5.5 MarkTechPost](https://www.marktechpost.com/2026/04/23/openai-releases-gpt-5-5-a-fully-retrained-agentic-model-that-scores-82-7-on-terminal-bench-2-0-and-84-9-on-gdpval/)
- [GPT-5.5 VentureBeat](https://venturebeat.com/ai/openais-gpt-5-5-is-here-and-its-no-potato-narrowly-beats-anthropics-claude-mythos-preview-on-terminal-bench-2-0)
- [Vellum GPT-5.5 guide](https://www.vellum.ai/blog/everything-you-need-to-know-about-gpt-5-5)
- [TechCrunch GPT-5.5](https://techcrunch.com/2026/04/23/openai-chatgpt-gpt-5-5-ai-model-superapp/)

### Google DeepMind
- [Gemini 3.1 Pro model card](https://deepmind.google/models/model-cards/gemini-3-1-pro/)
- [Gemini 3.1 Pro launch blog](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-1-pro/)
- [Gemini 3 Deep Think](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-deep-think/)
- [TheSequence Inside Deep Think](https://thesequence.substack.com/p/the-sequence-ai-of-the-week-769-inside)

### xAI
- [Grok 4.20 (designforonline)](https://designforonline.com/ai-models/xai-grok-4-20/)
- [Grok 4.20 4-agent review](https://aitoolbriefing.com/reviews/grok-4-20-review-2026/)

### Open-weight
- [Kimi K2.6 MarkTechPost](https://www.marktechpost.com/2026/04/20/moonshot-ai-releases-kimi-k2-6-with-long-horizon-coding-agent-swarm-scaling-to-300-sub-agents-and-4000-coordinated-steps/)
- [DeepSeek-V4 MarkTechPost](https://www.marktechpost.com/2026/04/24/deepseek-ai-releases-deepseek-v4-compressed-sparse-attention-and-heavily-compressed-attention-enable-one-million-token-contexts/)
- [Qwen3.6 Max Preview](https://qwen.ai/blog?id=qwen3.6-max-preview)
- [Qwen3.6-35B-A3B](https://qwen.ai/blog?id=qwen3.6-35b-a3b)
- [Llama 4 launch](https://ai.meta.com/blog/llama-4-multimodal-intelligence/)
- [Llama 4 challenges (Cameron Wolfe)](https://cameronrwolfe.substack.com/p/llama-4)

### Techniques / surveys
- [llm-stats post-training 2026](https://llm-stats.com/blog/research/post-training-techniques-2026)
- [DAPO paper](https://arxiv.org/pdf/2503.14476)
- [GRPO Cameron Wolfe](https://cameronrwolfe.substack.com/p/grpo)
- [RLVR primer (Label Studio)](https://labelstud.io/blog/reinforcement-learning-from-verifiable-rewards/)
- [ThinkPRM paper](https://arxiv.org/pdf/2504.16828)
- [Quartet MXFP4 paper](https://arxiv.org/html/2505.14669v4)
- [Microsoft FP4 framework](https://arxiv.org/abs/2501.17116)
- [DeepSeek Sparse Attention Skywork](https://skywork.ai/blog/sparse-attention-deepseek-3-2-explained/)
- [Latent-Condensed Attention](https://arxiv.org/abs/2604.12452)
- [BentoML speculative decoding](https://bentoml.com/llm/inference-optimization/speculative-decoding)
- [AgentGym-RL](https://agentgym-rl.github.io/)
- [Verlog CMU](https://blog.ml.cmu.edu/2025/09/15/verlog-a-multi-turn-rl-framework-for-llm-agents/)
- [Citation-grounded RAG](https://arxiv.org/pdf/2512.12117)
- [Lakera hallucinations 2026](https://www.lakera.ai/blog/guide-to-hallucinations-in-large-language-models)
- [Wing VC RL environments](https://www.wing.vc/content/rl-environments-for-agentic-ai-who-will-win-the-training-verification-layer-by-2030)

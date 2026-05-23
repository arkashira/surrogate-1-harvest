---
title: Persona-Imitation AI — State of the Art 2024–2026
created: 2026-04-24
tags: [persona, digital-twin, LLM, alignment, memory, fine-tune, surrogate-1]
status: active
related:
  - "[[models-2026-landscape]]"
  - "[[dev-skills]]"
  - "[[ops-skills]]"
  - "[[axentx-projects]]"
project: Surrogate-1
---

# Persona-Imitation AI — 2024-2026 Research & Action Plan

Goal: build Surrogate-1 that answers AS Ashira (DevSecOps founder) on coding / ops / business decisions — not as a generic LLM.

Current stack baseline:
- Base: Qwen2.5-Coder-7B via Ollama
- System prompt + RAG (SQLite `index.db`, 101k docs) + FalkorDB graph
- 242 hermes-trace decision pairs collected
- Target: LoRA fine-tune on RunPod

---

## 1. Research Landscape

### 1.1 Preference Optimization (train-time alignment on Ashira's judgments)

| Technique | Mechanism (1-2 sentences) | Data requirement | Applicability to Surrogate-1 |
|---|---|---|---|
| **DPO** (Rafailov et al., NeurIPS 2023, arxiv 2305.18290) | Recasts RLHF as direct loss on preference pairs — no reward model, no PPO. De-facto standard 2024-2026. | Paired (chosen, rejected) | HIGH — 242 traces can become 242 pairs by generating "rejected" via weaker model then Ashira labels. |
| **IPO** (Azar et al., DeepMind, 2024) | Adds regularization term to DPO to prevent over-fit / reward hacking on small data. Trains to convergence w/o early stopping. | Paired, same as DPO | HIGH when data is small (<1k pairs) — IPO reportedly = DPO on par, more stable on tiny sets. |
| **KTO** (ContextualAI, 2024) | Kahneman-Tversky: only needs binary thumbs up/down per sample, no pairs. | Unpaired (label each sample) | MEDIUM — useful if Ashira only has "I'd say this" / "I wouldn't" labels, not full A/B. |
| **SimPO / ORPO** (2024) | Reference-free DPO variants — no frozen ref model. Cheaper compute. | Paired | HIGH — ORPO collapses SFT + preference into one stage; save GPU hours. |
| **PAPI** (arxiv 2408.11779) | Personality Alignment via Personality Inventories — activation intervention (no weight update) to match Big Five traits. | Small personality profile | MEDIUM — complements LoRA; fast experiment with Ashira's OCEAN profile. |
| **PersonalLLM** (ICLR 2025) | Testbed + meta-learning across users: learns to personalize for new user with limited history. | Historical multi-user traces | LOW (needs multi-user dataset Ashira doesn't have). Useful as benchmark. |

Sources: [DPO](https://arxiv.org/abs/2305.18290), [HF Preference Tuning](https://huggingface.co/blog/pref-tuning), [OpenAI DPO guide](https://cookbook.openai.com/examples/fine_tuning_direct_preference_optimization_guide), [IPO/KTO comparison](https://esmln.medium.com/regarding-dpo-ipo-and-kto-02e94e6958ed), [Phil Schmid 2025 DPO guide](https://www.philschmid.de/rl-with-llms-in-2025-dpo), [PAPI](https://arxiv.org/abs/2408.11779), [PersonalLLM ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/file/a730abbcd6cf4a371ca9545db5922442-Paper-Conference.pdf), [Personalized Alignment Survey](https://arxiv.org/html/2504.07070v1).

### 1.2 Episodic Memory + Bitemporal Graphs (inference-time persona stability)

| System | Mechanism | Key property | Fit for Surrogate-1 |
|---|---|---|---|
| **Graphiti / Zep** (arxiv 2501.13956) | Temporal KG with (t_valid, t_invalid) edges. Hybrid BM25 + vector + graph traversal. Zero LLM call at retrieval. P95 300ms. | Bitemporal — tracks event time AND ingestion time separately → correct retroactively without losing history. +18.5% on LongMemEval vs vector RAG; 115k → 1.6k context tokens. | HIGH — already using FalkorDB. Graphiti bolt-on gives temporal correctness; huge token savings. |
| **Letta / MemGPT** | OS-style tiered memory: core (always in-prompt) + recall (conversation) + archival (vector). Self-editing blocks. | Agent state persists across sessions; persona block always injected. | MEDIUM — good pattern but requires rewriting server around Letta. Borrow "core memory block" concept into existing stack. |
| **A-MEM** (Zettelkasten) | Agent creates explicit atomic notes with links at write-time (not just at read-time). | Graph forms organically; 2D topology vs flat vector. | MEDIUM — useful for Ashira's lesson-learning pattern (patterns/ directory already does this manually). |
| **Mem0** | Lightweight long-term memory service w/ dedup + hierarchical summaries. | Cheap, managed. | LOW for Surrogate-1 (not self-hosted). |

Sources: [Zep/Graphiti paper](https://arxiv.org/abs/2501.13956), [Graphiti GitHub](https://github.com/getzep/graphiti), [Letta docs](https://docs.letta.com/concepts/memgpt/), [Letta blog — agent memory](https://www.letta.com/blog/agent-memory), [Atlan 2026 framework comparison](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/).

### 1.3 Behavioral Cloning via Decision Traces

Relevant work: Reflexion (NeurIPS 2023, arxiv 2303.11366) — agent generates verbal reflection after failure, stores as episodic memory, retries. 2025 extensions: **MAR** (Multi-Agent Reflexion, arxiv 2512.20845) addresses single-agent confirmation bias. **Swift-Sage** pattern: behavior-cloned action model + LLM planner.

Fit for Surrogate-1: the 242 hermes-traces are exactly this pattern's input. Each trace (problem, Ashira's decision, reasoning) becomes an SFT sample. After SFT, a critic agent (separate model) compares Surrogate-1's answer to Ashira's, produces "reflection", which feeds next DPO round.

### 1.4 Dataset Synthesis for Persona Fine-Tune

| Paper | Method | Takeaway for 242-pair dataset |
|---|---|---|
| **OpenCharacter** (arxiv 2501.15427) | Synthesize 10k+ character profiles from Persona Hub; response rewriting + response generation. LLaMA-3-8B ≈ GPT-4o on roleplay benchmarks. | Scale up: use Ashira's 242 traces as seed → synthesize 2-5k variants by rewriting prompts, keeping Ashira's answer style as target. |
| **Faithful Persona Conversation** (2312.10007) | Generate + expand + update personas automatically with faithfulness filter. | Filter step is critical — don't accept synthetic pairs unless style matches anchor samples. |
| **TwinVoice benchmark** (arxiv 2510.25536) | 6 capabilities evaluated: opinion consistency, memory recall, logical reasoning, lexical fidelity, persona tone, syntactic style. Current SOTA (GPT-5, Claude-4) still below human baseline. | Use as eval harness: score Surrogate-1 on same 6 axes before/after each training round. |
| **Consistently Simulating Personas w/ Multi-Turn RL** (arxiv 2511.00222) | Multi-turn RL keeps persona coherent across long conversations; plain SFT drifts. | After DPO, add one RL pass with persona-consistency reward. |
| **Future You (MIT Media Lab)** | Generates "synthetic memory" backstory to bind future-self to present-self. | Concept: pre-generate a canonical "Ashira backstory" as RAG anchor for every query. |

Sources: [OpenCharacter](https://arxiv.org/abs/2501.15427), [TwinVoice](https://arxiv.org/abs/2510.25536), [Consistent Persona RL](https://arxiv.org/html/2511.00222v1), [Future You](https://arxiv.org/html/2512.06106), [awesome-llm-role-playing list](https://github.com/Neph0s/awesome-llm-role-playing-with-persona).

### 1.5 Commercial Digital-Twin Stacks (what Character.AI / Replika / Pi actually do)

Consensus architecture (from [persona taxonomy paper](https://arxiv.org/html/2511.02979)):
1. **Base model** (proprietary, usually 7-70B) — small enough to fine-tune per persona.
2. **Persona prompt / system card** — fixed identity block.
3. **RAG over user profile + conversation history** — "memory" is actually retrieval, not parameters.
4. **Relational database** of facts + preferences + events (Replika pattern).
5. **Affective reasoning layer** for tone modulation.
6. **No true personalization fine-tune** at user level for most of them (too expensive per-user). They rely on prompt + retrieval.

Pi (Inflection) differs: custom base LLM optimized for warmth/curiosity during pre-training — not per-user tuning.

Implication for Surrogate-1: we have an advantage. Since it's a twin of ONE person (Ashira), LoRA fine-tune per-user is viable where it isn't for commercial scale.

### 1.6 RLAIF / Constitutional AI for Individual Preference

Constitutional AI (Anthropic 2022, arxiv 2212.08073) uses principles as the constitution; RLAIF generates preference pairs via an LLM critic. For individual personalization: replace "harmlessness" constitution with "Ashira's principles" (extracted from his lessons_learned, rules/*.md, preferences.md) → self-generate preference data.

This solves the dataset-size problem: 242 hermes pairs is not enough for DPO; with RLAIF we can generate 10k synthetic pairs with Ashira's constitution as the judge prompt.

Sources: [Constitutional AI](https://arxiv.org/abs/2212.08073), [RLAIF overview](https://www.superannotate.com/blog/reinforcement-learning-from-ai-feedback-rlaif).

---

## 2. Top 5 Techniques for Surrogate-1 (30-day plan)

Ranked by impact ÷ cost, highest first.

| # | Technique | Cost | Impact | Timeline | Why this order |
|---|---|---|---|---|---|
| 1 | **Graphiti bitemporal memory layer** over existing FalkorDB | 2-3 days eng (adapter to existing graph) | VERY HIGH — cuts context 115k→1.6k tokens, +18% retrieval accuracy. Zero training. | Week 1 | Pure inference change, no training risk. Biggest quality gain per hour. |
| 2 | **Constitution-as-prompt + RAG anchor (Ashira-backstory-card)** | 1 day — extract from ~/.claude/rules/, memory/preferences.md, memory/user_profile.md. Always injected. | HIGH — immediate style match before any training. This is what Pi/Replika actually do. | Week 1 | Free, reversible, validates RAG pipeline. |
| 3 | **RLAIF to grow 242 → 5k preference pairs** | GPU 1 day + review 1 day. Use Qwen2.5-14B as judge, Ashira's rules as constitution. | HIGH — DPO below ~2k pairs overfits. This unlocks #4. | Week 2 | Prerequisite for meaningful LoRA. |
| 4 | **ORPO LoRA on 5k pairs** (not vanilla DPO — ORPO saves one training stage) | RunPod A100 4-6hr (~$10-15). r=16, α=16, DoRA, 3 epochs. | HIGH — bakes Ashira-style into weights; persona stable across sessions w/o huge prompts. | Week 3 | Only AFTER #3 produces enough data. Going to DPO on 242 pairs = overfit. |
| 5 | **TwinVoice-style 6-axis eval harness + Reflexion loop** | 2-3 days eng. Test set = 30 held-out Ashira decisions. Critic = larger model (Claude/GPT-4o/Qwen-72B on RunPod). | MEDIUM-HIGH — without eval, we're blind. With eval + Reflexion, each round compounds. | Week 4 | Governance layer — prevents regressions, drives next iteration. |

**Explicitly deferred (month 2+, not top 5):**
- Letta migration — too much rewrite cost vs Graphiti bolt-on (#1 wins on ROI).
- PAPI activation intervention — experimental, validate baseline first.
- Multi-turn RL consistency (arxiv 2511.00222) — only after SFT+DPO+eval loop stabilizes.
- Per-session adaptive user-embeddings — overkill for N=1 user.

### Execution checkpoints

| Week | Deliverable | Verification |
|---|---|---|
| 1 | Graphiti adapter live; backstory card in system prompt | P95 retrieval <500ms; manual spot-check 10 Ashira questions |
| 2 | 5k synthetic pairs, constitution-filtered; held-out 30-pair eval set | Ashira manually reviews 50 random → ≥80% "sounds like me" |
| 3 | ORPO-LoRA merged, served via Ollama | TwinVoice 6-axis eval ≥ baseline + 15% on persona tone + syntactic style |
| 4 | Reflexion loop + eval dashboard | Each new decision → auto-logged, auto-evaluated, auto-queued for next DPO round |

---

## 3. Key Citations (one-liner each)

- DPO — [arxiv 2305.18290](https://arxiv.org/abs/2305.18290)
- IPO / KTO comparison — [HF pref-tuning](https://huggingface.co/blog/pref-tuning)
- ORPO — unified SFT+DPO, saves stage
- Constitutional AI / RLAIF — [arxiv 2212.08073](https://arxiv.org/abs/2212.08073)
- Zep / Graphiti bitemporal — [arxiv 2501.13956](https://arxiv.org/abs/2501.13956) · [GitHub](https://github.com/getzep/graphiti)
- Letta / MemGPT — [docs](https://docs.letta.com/concepts/memgpt/)
- Reflexion — [arxiv 2303.11366](https://arxiv.org/abs/2303.11366); MAR 2025 — [arxiv 2512.20845](https://arxiv.org/html/2512.20845v1)
- PersonalLLM ICLR 2025 — [paper](https://proceedings.iclr.cc/paper_files/paper/2025/file/a730abbcd6cf4a371ca9545db5922442-Paper-Conference.pdf)
- PAPI Personality Alignment — [arxiv 2408.11779](https://arxiv.org/abs/2408.11779)
- Personalized Alignment Survey 2025 — [arxiv 2504.07070](https://arxiv.org/html/2504.07070v1)
- OpenCharacter 2025 — [arxiv 2501.15427](https://arxiv.org/abs/2501.15427)
- TwinVoice benchmark 2025 — [arxiv 2510.25536](https://arxiv.org/abs/2510.25536)
- Multi-turn persona RL — [arxiv 2511.00222](https://arxiv.org/html/2511.00222v1)
- Persona taxonomy for companion AI — [arxiv 2511.02979](https://arxiv.org/html/2511.02979)
- LoRA/QLoRA 2026 practical — [sitepoint guide](https://www.sitepoint.com/fine-tune-local-llms-2026/), [dev.to 2026 guide](https://dev.to/jangwook_kim_e31e7291ad98/fine-tune-llms-with-lora-and-qlora-2026-guide-33lf)
- LlamaFactory v0.9.4 — [GitHub](https://github.com/hiyouga/LlamaFactory)

---

## 4. Anti-patterns (do NOT do)

- ❌ Vanilla DPO on 242 pairs → catastrophic overfit. Need ≥2k, preferably 5k+.
- ❌ Fine-tune FIRST without fixing retrieval — garbage context still beats good weights.
- ❌ Use Letta/MemGPT full rewrite when Graphiti bolt-on gives 80% of the win in 10% of the time.
- ❌ Copy Character.AI architecture wholesale — they optimize for SCALE (millions of personas), Surrogate-1 optimizes for FIDELITY (one persona).
- ❌ Skip the eval harness — TwinVoice shows even Claude-Sonnet-4 is below human baseline on persona simulation. Without measurement, can't improve.

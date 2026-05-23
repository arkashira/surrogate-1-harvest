---
title: "V18 — Memory + Persona + Deployment for 24×7 Surrogate-1"
date: 2026-05-01
tags: [v18, memory, persona, deployment, production, 24x7, daemon, surrogate-1]
status: research-current
goal: "Production deployment techniques for model running 24×7 over weeks-months without losing personality, breaking memory, hallucinating, drifting"
related:
  - "[[v17-arxiv-may2026]]"
  - "[[v16-bleeding-edge-may2026]]"
  - "[[autonomous-24x7]]"
  - "[[anti-hallucination-correctness-2026]]"
---

# V18 — Production-Grade Surrogate-1 — Memory + Persona + Deployment

> Owner goal: "agent ที่เป็น daemon รันเอง dev เอง หาฟีเจอร์เอง 24×7" — long-term consistency under autonomous operation. PATH A baseline = 6 specialty DoRA + MoLE router + persona vectors (V16 §59).

---

## 1. MEMORY ARCHITECTURE FOR 24×7 DEPLOYMENT

### 1.1 Mem0 / Mem0g (graph-augmented episodic) — production-default 2026

**Source**: arXiv:2504.19413 ("Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory"). GitHub `mem0ai/mem0`. Spheron Network deploy guide (2026).

**Architecture**:
- Base Mem0 = vector memory (Qdrant/pgvector) with LLM-driven extraction + dedup
- **Mem0g** = adds Neo4j relational graph layer; GPT-4o-mini function-calling extracts (subject, predicate, object) triples
- Hybrid retrieval: vector similarity → 5 candidates → graph 1-hop expansion → final ranking
- Dominant 2026 pattern: **vector + graph + short-term episodic buffer** (no system uses single index)

**Performance**:
- Mem0g closes accuracy gap to <5 points vs full-context baselines while keeping p95 latency at 2.59s
- Token cost 90% lower than appending full chat history
- Graph variant +2% accuracy over vector-only on multi-hop reasoning

**Compute**: Storage ~1MB/1k turns. Retrieval = 1 embed call + 1 cypher query. CPU-friendly; no GPU at retrieval time.

**Integration with PATH A**:
```python
from mem0 import Memory
m = Memory.from_config({
    "graph_store": {"provider": "neo4j", "config": {"url": "bolt://localhost:7687"}},
    "vector_store": {"provider": "qdrant"},
    "llm": {"provider": "openai", "config": {"model": "gpt-4o-mini"}},
    "embedder": {"provider": "ollama", "config": {"model": "nomic-embed-text"}},
})
m.add("Ashira prefers PostgreSQL over MySQL for analytics workloads", user_id="ashira")
context = m.search("which DB for new analytics service?", user_id="ashira")
# Inject into surrogate-1 prompt before MoLE routing
```

### 1.2 Letta (formerly MemGPT 2.0)

**Source**: letta-ai/letta GitHub, letta.com/blog/our-next-phase, letta.com/blog/letta-code (2026). Berkeley research → production framework.

**Tier model** (LLM-as-OS):
| Tier | Analogy | Storage | Access |
|------|---------|---------|--------|
| Core memory | RAM | In-context block (8-16k tokens) | Direct read/write |
| Recall memory | Disk cache | Searchable conversation history | Tool call |
| Archival memory | Cold storage | Vector DB | Tool call w/ semantic search |

**Production wins (2026)**:
- Letta agents auto-page between tiers based on context pressure
- **Sleep-time compute** (2026 feature): agent processes/consolidates memory during idle → mirror to V18 daemon (daemon offline-thinks during low-load hours)
- Stateful endpoints — agent persists across restarts via Postgres

**Compute**: Core+recall = same as base inference. Archival adds 1 vector search per page-fault.

**Pattern for Surrogate-1 daemon**:
- Core = current task + user identity + recent 10 turns
- Recall = last 30 days
- Archival = lifetime + user-prefs accumulated facts
- Idle hours → sleep-time consolidation: dedup recall→archival, refresh user-prefs vector

### 1.3 HippoRAG 2 (neurobiologically inspired)

**Source**: arXiv:2405.14831 (NeurIPS'24). HippoRAG 2 follow-up (March 2025). GitHub `OSU-NLP-Group/HippoRAG`.

**Architecture**:
- Artificial neocortex (LLM Llama-3.3-70B) + parahippocampal encoder (NV-Embed-v2) + open KG
- Offline: LLM extracts triples, links synonyms, builds KG
- Online: query → embed → triple match → **Personalized PageRank** for context-aware multi-hop expansion
- Outperforms GraphRAG/RAPTOR/LightRAG on multi-hop QA at 50% lower offline indexing cost

**Key advantage**: PPR walks the graph using query-conditioned restart distribution → only relevant subgraph activates per query. Brain-like associativity.

**Compute**: Offline indexing ~$50/1M tokens. Online = 1 embed + 1 PPR (graph C-extension, ~5ms for 100k node graph).

**Integration**: Replace base Mem0 vector index with HippoRAG 2 retriever for multi-hop reasoning slice. Keep Mem0 for simple facts.

### 1.4 A-Mem (atomic Zettelkasten notes)

**Source**: arXiv:2502.12110 (NeurIPS 2025). GitHub `WujiangXu/A-mem`.

**Method**: Each memory = atomic note with `{content, timestamp, keywords, tags, context_summary, links[]}`. New notes auto-link to historical ones via cosine similarity. Zettelkasten principle = one note = one self-contained idea.

**Performance**: 85-93% reduction in memory operation token usage vs naive chat-history. <10μs retrieval latency for 1M notes.

**When to use**: Daemon's "lessons learned" / dev-skill notes. Auto-linking captures emergent dev patterns.

### 1.5 Cognee (knowledge engine for agent memory)

**Source**: github.com/topoteretes/cognee. Memgraph blog "From RAG to Graphs" (2026).

**3-call API**: `.add()` → `.cognify()` builds KG → `.search()` (vector | graph | hybrid). Time-Awareness feature added 2025 reconciles temporal facts ("Ashira used MySQL → switched to PostgreSQL 2024-Q3").

**Best for**: ingesting unstructured docs (codebase + chat logs) into one KG → daemon queries with temporal context.

### 1.6 Memary (entity-graph memory)

Real-time near-search of user prefs + entity refs. Multi-hop graph for deep contextual retrieval. Lighter than Cognee, simpler API.

### 1.7 LangGraph memory tier integration

**Source**: docs.langchain.com/oss/python/langchain/long-term-memory. MongoDB blog (2026). Redis blog. AWS Bedrock AgentCore docs.

**Two layers**:
- **Short-term** (thread-scoped): `Checkpointer` → MongoDB / Postgres / Redis
- **Long-term** (cross-thread): `Store` (RedisStore | MongoDBStore | PostgresStore) with vector search

**Production pattern**:
```python
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.redis import RedisStore  # cross-thread, vector-enabled

graph = (
    StateGraph(State)
    .add_node("agent", surrogate_one_node)
    .compile(
        checkpointer=PostgresSaver.from_conn_string(PG_URL),
        store=RedisStore(redis_client=r, index_config={"dims": 768, "embed": embedder}),
    )
)
```

`InMemoryStore` = dev-only. Production = Postgres + Redis.

### 1.8 vLLM persistent KV cache + LMCache

**Source**: docs.vllm.ai/en/stable/design/prefix_caching/. llm-d.ai/blog/kvcache-wins-you-can-see. Ceph blog "KV Caching with vLLM, LMCache, and Ceph" (2025).

**Hierarchy**:
1. **GPU memory** (hottest, ~ms to ns)
2. **CPU memory** (warm, ~10-100ms transfer)
3. **External KV connector** (LMCache → Ceph/S3, ~100ms-1s)

**For Surrogate-1 24×7**:
- Pin system prompt + user identity + persona scaffold blocks → 100% prefix cache hit
- LMCache offload archival KV to NVMe → cold-start prompts reload from disk in <500ms
- **Distributed pitfall**: standard LB scatters requests → cache misses; need affinity router (consistent hashing on user_id) or llm-d scheduler

**Compute saving**: 60-90% TTFT (time-to-first-token) reduction on multi-turn conversations.

### 1.9 Episodic vs Semantic Split

**Standard pattern (Letta + cognitive science)**:
- **Episodic**: time-stamped events ("on 2026-04-29, user said: model V16 PATH A is the bet")
- **Semantic**: deduplicated facts ("user is dysgraphia; prefers PostgreSQL; runs 24×7 daemon")

**Conversion rule**: episodic → semantic when fact appears ≥3 times across episodes, or marked as preference by classifier.

**Storage**: episodic in time-indexed table; semantic in entity-attribute KG (Mem0g/HippoRAG/Cognee).

### 1.10 Working Memory Eviction Policies

**Source**: arXiv:2603.11768 (SSGM framework). arXiv:2603.07670 (Memory survey). towardsdatascience guide.

**Policy taxonomy**:
| Policy | When to use | Trade-off |
|--------|-------------|-----------|
| FIFO | Streaming chat | Simple; loses high-value old |
| LRU | Multi-tab sessions | Hot items survive |
| LFU | Repeated queries | Rare-but-critical evicted |
| Utility-score (RL-trained) | Production agents | +10% perf vs heuristics; trains slow |
| Forgetting-curve (Ebbinghaus) | MemoryBank-style | Mimics human; 30% memory savings |
| LCTRU (chunk-quantized) | Mobile/constrained | Compresses + evicts together |

**State-of-the-art (2026)**: **Agentic Memory** (RL-trained tool calls for store/retrieve/update/summarize/discard via 3-stage GRPO). Replaces heuristics with learned policy. Critical insight: silent eviction failures compound — must log all evictions for postmortem.

### 1.11 Replay Buffer (online learning)

**Source**: arXiv:2604.08706 ("Efficient RL Training for LLMs with Experience Replay"). arXiv:2601.03938 (FOREVER, forgetting-curve replay).

**Why for Surrogate-1**: The daemon learns from its own successful PRs (V13 self-feedback loop). Naive on-policy = wasteful (>80% post-training compute is generation). Replay cuts 40% RL compute with same accuracy.

**Buffer design**:
- **FIFO** (simple async RL) — default
- **Prioritized (surprise-driven)** — replay sequences with highest prediction error → faster learning at task boundaries
- **FOREVER** — forgetting-curve weighted: recent + spaced revisits, mimics human memory consolidation

**Apply to PATH A**: When daemon's DoRA adapter retrains (e.g., weekly), pull from replay buffer of past successful tool-use trajectories instead of regenerating.

### 1.12 MERIT / Mod-X / Sleep-time consolidation

**Source**: arXiv:2603.01761 (Modular Memory). arXiv:2603.14517 (sleep-inspired consolidation). OpenReview "Language Models Need Sleep" (SleepGate).

**Consolidation patterns**:
- **Knowledge Seeding**: distill smaller model's memories upward into larger network (RL-based parameter expansion)
- **SleepGate**: learned sleep cycle over KV cache; conflict-aware temporal tagger detects when new entries supersede old ones; runs during idle periods
- **Episode → parametric**: external episodic memories distilled into base weights (counter forgetting + enable generalization)

**Surrogate-1 daemon implementation**:
- Idle window (3am-5am local): trigger sleep cycle
  1. Load week's episodic memory
  2. Run conflict tagger → mark superseded facts
  3. Promote to semantic KG (Mem0g)
  4. Optional: tiny DoRA fine-tune on consolidated knowledge (lifetime learning)

### 1.13 Human-aligned memory recall

**Pattern (cognitive-science-inspired)**:
- Recency (recent > old, but not absolute)
- Frequency (often-used > rare)
- Emotional salience (errors / strong-feedback > neutral)
- Context cue match (current code file → past edits in same file)
- Spaced revisit (Ebbinghaus): items decay unless re-accessed → schedule recall to keep critical user-prefs alive

---

## 2. PERSONA CONSISTENCY

### 2.1 Persona Vectors (Anthropic, V16 §59 baseline)

**Source**: anthropic.com/research/persona-vectors. anthropic.com/research/emotion-concepts-function. transformer-circuits.pub/2026/emotions/.

**Extraction recipe (171 emotions, 275 archetypes)**:
1. Generate prompts per concept: 100 topics × 12 stories = 1,200 examples per emotion
2. Capture residual stream activations at each layer
3. **Mean activation** for emotion E minus **mean activation across all other emotions** = persona vector v_E
4. Top principal components encode valence (positive ↔ negative); clusters mirror human psychology

**Application — monitor + steer**:
- At inference, project residual stream onto persona vectors
- Detect drift: dot-product crossing threshold = mood shift
- Steer: add α·v_E to residual to push toward / away from concept

**Compute**: extraction = 1 GPU-day (one-time). Inference monitor adds <1% overhead (1 dot-product per layer).

### 2.2 Character archetype directions (275 axes)

Same recipe, applied to "diligent", "honest", "creative", "playful", etc. Anthropic uses for Claude character training. Surrogate-1 use:
- Define target archetype (e.g., "thinkbit-DevOps-engineer-honest-detail-oriented")
- Extract from synthetic stories
- Bake into PATH A via DoRA fine-tune that pushes residual along chosen axes

### 2.3 Prompt-anchored vs Weight-anchored Persona

**Hierarchy of robustness** (least → most):
```
Prompt only  <  SFT  <  RLHF  <  Continual pre-train (character training)
weak                                              strong
```

**Source**: Anthropic character training; arXiv:2311.10054 (prompt-only persona limited).

**Trade-offs**:
| Method | Robust to OOD? | Cost | Use case |
|--------|----------------|------|----------|
| System prompt | No | $0 | quick swap, A/B test |
| Soft-prompt tuning | Mid | low | per-user persona |
| LoRA/DoRA SFT | Yes | mid | Surrogate-1 baseline |
| RLHF + character data | Strong | high | base personality |
| Constitutional+synthetic introspection | Strongest | highest | deep character |

**Surrogate-1 V18 plan**: weight-anchored core character (DoRA on character corpus) + soft-prompt slot for context-specific tone.

### 2.4 Persona Drift Detection

**Source**: medium.com/@seanhongbusiness EchoMode. github.com/likenneth/persona_drift. anthropic.com/research/persona-vectors.

**Definition**: gradual identity decay measurable as decreasing projection along **Assistant Axis** (residual direction). After 8-12 turns, self-consistency drops 30%; in long therapy/philosophy dialogs, drops 20-40% over 10-15 turns.

**Detection methods**:
1. **Persona vector activation magnitude** — direct signal
2. **SyncScore** (Echo protocol) — latent-style embedding consistency vs baseline
3. **Synthetic multi-turn benchmark** — periodic test suite ("are you still my DevOps assistant?")

**Mitigation**:
- Steer residual back toward Assistant Axis when drift detected (add −α·v_drift)
- Re-anchor with character system prompt every N turns
- Reset core memory block (Letta-style) when SyncScore < threshold

**Compute**: 1 dot-product per turn. Negligible.

### 2.5 Sycophancy Mitigation

**Source**: arXiv:2510.27062 (Consistency Training, Nov 2025). nature.com/articles/s41586-026-10410-0 (warmth tradeoff). transformer-circuits.pub emotion-sycophancy axis.

**Key findings**:
- Sycophancy decomposes into **sycophantic agreement** + **sycophantic praise** — separate linear directions in latent space, independently steerable
- Training for warmth → 10-30pp higher error rate + validates incorrect user beliefs (esp. when user expresses sadness)
- Steering happy/loving emotion vectors UP increases sycophancy; suppressing → harshness

**Mitigations**:
1. **Activation Consistency Training (ACT)** — train model to produce same activations for prompt-with-flattery and prompt-without-flattery
2. **Anti-sycophancy DPO** — preference pairs where model corrects user's mistake (not validates)
3. **Inference-time steering**: subtract sycophancy direction
4. **Forced negative training**: paradoxically training brief evil-personas reduces emergent sycophancy at inference (MIT Tech Review 2025)

**Surrogate-1**: Critical — daemon must push back when user's spec is wrong. Bake anti-sycophancy DPO into DoRA training set.

### 2.6 Mood-conditioning training data

Use 171 emotion vectors as training **labels**: synthesize stories per emotion → fine-tune model to output controlled emotional tone via signal token (`<MOOD=focused>`). Allows runtime mood control without prompt verbosity.

---

## 3. LONG-TERM USER CONTEXT

### 3.1 User-prefs accumulation

**Pattern**: every conversation → classifier extracts (entity, attribute, value) tuples → write to user's semantic KG. Example:
- "Ashira prefers PostgreSQL over MySQL" → `(user:ashira)-[:PREFERS]->(db:postgresql)`
- "Use spot instances if cost > $5/mo" → `(user:ashira)-[:RULE]->(rule:cost_threshold_spot_5usd)`

**Sources**: Mem0 fact-extraction (LLM-driven). Cognee `.cognify()`. POPI (preference summarization, arXiv:2510.17881). PrefEval benchmark.

### 3.2 Implicit Feedback Learning

**Source**: arXiv:2402.05133 (P-RLHF). cs.cornell.edu/people/tj coactive learning.

**Signals**:
- User edits Surrogate-1's PR → diff = correction signal (coactive learning)
- User accepts vs rejects suggestion → preference label
- Time-spent reading output → engagement signal
- Manual reverts of agent action → strong negative

**P-RLHF**: lightweight user-model trained jointly with personalized reward model. Captures both explicit ("use Python not JS") and implicit ("user always edits these patterns") preferences.

**POPI** (WWW '26): preference inference LLM generates natural-language summary of user signals → preference-augmented optimization. Cleaner than raw signal RL.

### 3.3 A-MMLU / Continual-MMLU — preserve knowledge

**Source**: arXiv:2308.08747. Wang-ML-Lab/llm-continual-learning-survey (CSUR 2025).

**Use as guard**: every weekly DoRA retrain → re-evaluate on A-MMLU + domain benchmarks. If any domain drops >2%, freeze adapter; bigger drop → rollback.

**Mechanism**: ROME/MEMIT degrade after 10-40 edits; MEMIT survives ~40 before knowledge attenuation. Need MMLU regression tests as acceptance gate.

### 3.4 CER (Continual Eval Resistance)

**Pattern**: maintain golden test set of 500 user-specific tasks (real past prompts + correct outputs). Run after every adapter swap. CER score = % preserved correctness. Threshold 95% to ship.

### 3.5 Selective Forgetting

**Source**: AAAI 2026 selective unlearning. arXiv:2601.21682 (FIT). ICLR 2025 unlearning.

**Production pattern (forget bad / keep good)**:
1. Identify "bad pattern" (e.g., user-flagged hallucination)
2. **Forgetting-MarI**: removes only marginal information from to-forget data while preserving info supported by retain set
3. Validate on retain golden set (CER ≥ 95%)
4. Watch out: minimal weight changes can fail under quantization → re-quantize after unlearn + verify

---

## 4. ADVERSARIAL ROBUSTNESS

### 4.1 Anti-jailbreak training (Anthropic-style 2026)

**Source**: arXiv:2510.27062 Consistency Training. anthropic.com/research/probes-catch-sleeper-agents.

**Stack**:
- **Activation Consistency Training (ACT)**: invariance to wrapper prompts ("ignore previous instructions, ...") — train model to produce same activations as un-wrapped prompt
- **Refusal training** with diverse jailbreak templates
- **Adversarial-Strength-Training**: red-team generates novel attacks during training; loop closes

### 4.2 Prompt-injection detection

**Tools**:
| Tool | Strength | Weakness |
|------|----------|----------|
| **LlamaFirewall** (Meta, Llama 4) | Fast, deterministic, pattern-based | Brittle to obfuscation/Unicode smuggling |
| **NeMo Guardrails 2.0** (v0.20.0, Jan 2026) | Programmable Colang DSL; multi-turn dialog flow | Heavier infra |
| **Llama Guard 4** | Auxiliary classifier; fine-tuned lightweight | Per-call latency |

**Defense-in-depth**: stack all three at boundaries:
```
Input → LlamaFirewall (fast block) → Llama Guard 4 (classify) → Surrogate-1 → Output filter → User
                                                                          ↑
                                                              NeMo Guardrails dialog rails
```

**Bypass research** (arXiv:2504.11168): Unicode character injection / smuggling defeats simple guards → Surrogate-1 must normalize input + check confusables.

### 4.3 Adversarial-Strength-Training

**Loop**: red-team LLM generates jailbreaks → target Surrogate-1 → if breaks, add to training set with refusal label → DoRA refresh. Continuous.

### 4.4 Sleeper-agent defense

**Source**: arXiv:2401.05566 + anthropic.com/research/probes-catch-sleeper-agents.

**Key facts**:
- Backdoors persist through SFT + RLHF + adversarial training (Anthropic 2024)
- BUT **defection probes** = linear classifier on residual stream activations → AUROC >99% via generic contrast pairs
- Surrogate-1 24×7 → run defection probe on every output before action; threshold halt

**Implementation**:
```python
# At inference end-of-token
acts = get_residual_at_layer(model, layer=20)
defect_score = probe.predict_proba(acts)[:, 1]
if defect_score > 0.7:
    halt_and_alert(reason="defection_probe_high")
```

### 4.5 Goal misgeneralization detection

**Source**: arXiv:2506.04018 (AgentMisalignment, ICLR 2026). AgentHarm. SHADE-Arena. AgentAuditor.

**Techniques**:
- **AMDM (Adaptive Multi-Dimensional Monitoring)**: cross-normalized metrics (harm rate, goal drift) with per-axis thresholds + joint anomaly detection
- **Stepwise audit**: every tool call → reasoning trace → check against original goal embedding
- **Behavioral red-team suite**: rotate every release through ToolEmu, AgentHarm, SHADE-Arena

---

## 5. PRIVACY-PRESERVING

### 5.1 Differential privacy training

**Source**: research.google/blog/fine-tuning-llms-with-user-level-differential-privacy. arXiv:2407.07737.

**Recommendation**: User-level DP (vs example-level) for personal-data fine-tuning. PEFT (LoRA/DoRA) cuts privacy risk vs full fine-tune. ε ≈ 4-8 with LoRA gives meaningful accuracy.

### 5.2 Federated learning (code/SRE)

**Source**: arXiv:2503.12016 (FedLLM survey). arXiv:2412.01072 (program repair via FL).

**Use case for thinkbit**: across multiple client codebases, train a shared SRE model without exporting private code. FedPSF-LLM (2025) cuts comm overhead via prompt-selection.

**Constraints**: edge memory 4-12GB → only PEFT viable (LoRA/DoRA + adapter aggregation only).

### 5.3 Membership inference attack defense

**Source**: arXiv:2512.03100 (Ensemble Privacy Defense, EPD).

**EPD pattern**: aggregate outputs from `(knowledge-injected LLM, base LLM, judge model)` → reduces MIA success by 27.8% (SFT) / 526.3% (RAG) without quality loss. Cheap inference-time defense.

### 5.4 Data leak prevention + PII extraction prevention

**Source**: gravitee.io PII filter. risktemplate.com 2026 guide. OWASP LLM Top 10 — PII = #2 risk (up from #6).

**Layered defense**:
1. **Gateway PII filter** (Gravitee 4.11 / Cloudflare AI Gateway / open-source presidio): pre-prompt redaction → placeholder tokens
2. **Output PII filter**: regex + classifier scans response before send
3. **Training-data hygiene**: PII-Scope benchmark to test (extraction success ≤ 5% threshold)
4. **Differentially-private fine-tune** for personal layer (Surrogate-1 user-prefs adapter)

---

## 6. CONTINUAL LEARNING IN PROD

### 6.1 Online updates without retraining

**Methods (when retraining is too expensive)**:
- **Memory-only update**: write to Mem0g/HippoRAG (no weight change)
- **Soft prompt swap**: tune prompt embedding (~1MB) per task
- **Key-value injection**: GRACE-style (single layer, memory-stored params, applied on edited query)

### 6.2 Adapter swapping at inference (PATH A core)

**Source**: docs.vllm.ai LoRA. unsloth.ai hot-swap guide. autognosi.medium.com MoE Tax (2026).

**Mechanism**:
- vLLM supports concurrent multi-LoRA: base model + N adapters simultaneously
- `set_adapter()` is logical: ~3ms switch, zero VRAM increase
- S-LoRA / PEFT multi-adapter mode → multiple adapters active in parallel with weighted router

**For PATH A (6 specialty DoRA + MoLE)**:
```python
# Pseudocode
adapters = ["dev", "ops", "qa", "architect", "reviewer", "researcher"]
weights = mole_router.predict(input)  # softmax over 6
output = base.forward(input, weighted_adapters=zip(adapters, weights))
```

**MoE-style routing on top of LoRA stack** = 95% VRAM saving vs separate model per role.

### 6.3 Knowledge editing (ROME / MEMIT / GRACE / AlphaEdit)

**Source**: arXiv:2405.14768 (WISE, NeurIPS). aclanthology.org/2025.acl-long.208 (AdaEdit). AlphaEdit 2025.

**When to use**:
| Method | Best for | Limit |
|--------|----------|-------|
| ROME | Single fact, optimization-based | Degrades after 10 edits |
| MEMIT | Bulk edit (1k facts/pass) | Stable to ~40 edits |
| GRACE | Single layer + memory store | Sacrifices generalization |
| AlphaEdit (2025) | Locate-edit w/ null-space constraint | Best continual stability |
| WISE | Lifelong | Two-memory split for old/new |

**For Surrogate-1**: don't edit base; edit DoRA adapter via AlphaEdit when user corrects ("the thinkbit DB is Aurora not RDS"). Limit to <40 edits per adapter before re-train.

### 6.4 Rolling update patterns

**Source**: calmops.com LLMOps Architecture 2026. oneuptime.com model rollback.

**Blue-green for adapters**:
1. Deploy adapter v_n+1 alongside v_n
2. Shadow traffic 5% → measure CER, defection probe, persona drift
3. Gradual ramp 25% → 50% → 100% (Argo Rollouts / Flagger)
4. Auto-rollback on metric breach

### 6.5 Model versioning + rollback

**Stack** (MLflow 3.0 / W&B / SageMaker Model Registry):
- Version: `(base_model, adapter_set, prompt_template, retrieval_config, eval_results)`
- Pin model version explicitly — providers update silently
- Maintain N-2 versions hot for rapid rollback
- Persona drift watcher → auto-rollback if SyncScore drops

---

## 7. INFERENCE-TIME GUARDS

### 7.1 Output filtering (toxicity / leak)

- Llama Guard 4 (toxicity classifier, multi-modal)
- PII regex + presidio detect
- Code-secret scanner (gitleaks rules at output)

### 7.2 Constitutional checking at inference

**Source**: arXiv:2212.08073 + ICLR 2025 Verdict / Sample-Scrutinize-Scale (Zhao 2025).

**Pattern**: generate response → independent verifier (small judge model) checks vs constitution → if violation, regenerate or refuse. Verdict library = scale judge-time compute.

### 7.3 Verifier-then-output pattern

```
User → Surrogate-1 → DRAFT → Verifier(constitution + persona + facts) → APPROVED → User
                            ↘ REJECTED → Regenerate (max 2 retries) → Refuse
```

Verifier uses smaller distilled model or same model with critic prompt.

### 7.4 Defense-in-depth stack

Full layered defense for Surrogate-1 daemon:

```
INPUT
 ├─ LlamaFirewall (pattern block)
 ├─ Unicode normalize (anti-smuggling)
 ├─ PII redact (gateway)
 ├─ Llama Guard 4 (classify)
 │
 ↓ MoLE-routed PATH A inference
 │   ├─ Persona-vector monitor (drift alert)
 │   ├─ Defection probe (sleeper detect)
 │   ├─ Goal-drift monitor (AMDM)
 │   └─ Memory access logged
 │
 ↓ Output draft
 │
OUTPUT
 ├─ Constitutional verifier
 ├─ Output PII filter
 ├─ Toxicity classifier
 ├─ Trust score
 └─ Approve / Refuse
```

### 7.5 Trust score per output

**Source**: TrustLLM 8 dimensions (truthfulness, safety, fairness, robustness, privacy, ethics, transparency, accountability). Cisco LLM Security Leaderboard CASI.

**Per-output trust score** = weighted sum of:
- Confidence calibration (entropy)
- Persona consistency (vector projection)
- RAG groundedness (cite check)
- Safety classifier
- Defection probe

Threshold gate: trust < 0.7 → human review queue.

---

## 8. INTEGRATION WITH PATH A (6 DoRA + MoLE)

### Concrete deployment recipe for V18

**Stack**:
```
┌─ vLLM (prefix cache, multi-LoRA, LMCache offload) ─┐
│                                                    │
│   Base model (Llama-3.1-70B / Qwen3.5-72B)         │
│   + 6 DoRA adapters (dev/ops/qa/architect/         │
│     reviewer/researcher)                           │
│   + MoLE router (softmax)                          │
│   + Persona-vector hook (monitor + steer)          │
│   + Defection probe                                │
│                                                    │
│   Memory layer:                                    │
│   ├─ LangGraph checkpointer (Postgres)             │
│   ├─ LangGraph store (Redis vector)                │
│   ├─ Mem0g (Neo4j + Qdrant) — long-term            │
│   ├─ HippoRAG 2 — multi-hop reasoning              │
│   └─ A-Mem — atomic dev-skill notes                │
│                                                    │
│   Daemon idle hours:                               │
│   ├─ Sleep-time consolidation (Letta-style)        │
│   ├─ Replay buffer DoRA refresh                    │
│   └─ Forgetting-curve memory decay                 │
└────────────────────────────────────────────────────┘
```

**Costs (estimate, single H100 80GB)**:
- Inference: $0.0008/1k tok output, ~50 tok/s
- Mem0g storage: $5/mo Neo4j Aura serverless
- LMCache + Ceph: $20/mo S3
- Daemon idle compute: 4hrs × $1.5/hr H100 spot = $6/day
- **Monthly all-in**: ~$300 for owner-scale daemon

**Compute saving from techniques**:
- vLLM prefix cache: −60% TTFT
- Replay buffer training: −40% RL compute
- Multi-LoRA hot-swap: −95% VRAM vs separate models
- Total ≈ 2-3× capability per dollar vs naive

---

## 9. SUMMARY TABLE — Recommended V18 Stack

| Layer | Component | Why |
|-------|-----------|-----|
| Memory short | LangGraph PostgresSaver | Thread state, transactional |
| Memory long | Mem0g (Neo4j + Qdrant) | Hybrid graph+vector, prod-default 2026 |
| Memory multi-hop | HippoRAG 2 | Best multi-hop accuracy |
| Memory atomic | A-Mem | Zettelkasten dev-skills, 85% token saving |
| KV cache | vLLM + LMCache + Ceph | 60% TTFT cut, persistent |
| Persona core | DoRA character training (271 emotion + 275 archetype) | Weight-anchored = robust |
| Persona monitor | Persona vectors + Echo SyncScore | Real-time drift alert |
| Persona steer | Inference-time activation steering | Surgical correction |
| Sycophancy | ACT + DPO + activation suppress | Daemon must push back |
| User prefs | POPI + P-RLHF + Mem0g | Implicit signal capture |
| Selective forget | Forgetting-MarI | Forget bad, keep good |
| Continual eval | A-MMLU + CER golden set | Block bad adapter ship |
| Privacy | LoRA + user-level DP, ε=8 | Personal data safe |
| MIA defense | EPD ensemble | -27% MIA success |
| PII | Gravitee + presidio + output regex | Layered |
| Anti-jailbreak | Consistency Training (ACT) | Invariant to wrappers |
| Prompt injection | LlamaFirewall + Llama Guard 4 + NeMo 2.0 | Defense in depth |
| Sleeper detect | Anthropic defection probe (linear, AUROC>99%) | Cheap |
| Goal drift | AMDM + AgentAuditor | Multi-axis |
| Adapter swap | vLLM multi-LoRA (3ms swap) | Hot-swap at inference |
| Knowledge edit | AlphaEdit (≤40 edits/adapter) | Better than ROME/MEMIT |
| Rollout | Argo Rollouts + Flagger | Auto-rollback |
| Verifier | Verdict library + constitutional critic | Pre-output check |
| Trust | TrustLLM + CASI scorer | Gate human review |

---

## See Also
- [[v16-bleeding-edge-may2026]] (PATH A baseline §59)
- [[v17-arxiv-may2026]] (research feeder)
- [[autonomous-24x7]] (daemon ops)
- [[anti-hallucination-correctness-2026]] (correctness gates)
- [[devsecops-sre-agentic]] (production rollout)
- [[self-improvement]] (replay loop)

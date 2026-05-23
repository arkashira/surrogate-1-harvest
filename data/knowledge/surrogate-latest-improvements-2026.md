---
title: Surrogate-1 Latest Improvements — Jan-Apr 2026 Research
created: 2026-04-24
status: active
tags: [surrogate-1, devsecops-ai, research, 2026, agent-improvements]
scope: Improvements NOT already covered by Mythos M1-M5, Brain-Cog B1-B5, anti-hallucination basics, persona imitation
constraints:
  - No base model retraining (out of scope)
  - No proprietary SaaS (open source / self-hosted only)
  - No GPU infra required beyond existing stack
  - Must be validated outside originating paper
---

# Surrogate-1 Latest Improvements (Jan-Apr 2026)

Research filter: only **published papers or production case studies**, skeptical of single-paper claims, rejected anything requiring retraining/proprietary/GPU.

---

## 1. Agent Memory Systems (beyond Graphiti/MemGPT/Letta)

### 1.1 MAGMA — Multi-Graph Agentic Memory
- **Mechanism**: Represents each memory item across four orthogonal graphs (semantic, temporal, causal, entity). Retrieval is policy-guided traversal selecting the right graph per query.
- **Evidence**: arXiv 2601.03236 (Jan 2026). Outperforms SOTA on LoCoMo + LongMemEval with **lower retrieval latency and fewer tokens** vs single-graph systems. Code open-sourced.
- **Effort**: 1-2 weeks. We already have FalkorDB — extend it with 3 additional graph views over the same nodes (temporal edges, causal edges, entity-type edges). Orchestrator routes queries to the right graph.
- **Expected lift**: 15-25% better long-context recall, reduced token cost on retrieval (already-known nodes not re-pulled through unrelated edges).
- **Verdict**: HIGH PRIORITY. Aligns with our existing graph stack.

### 1.2 MemRL — Self-Evolving Episodic Memory
- **Mechanism**: Runtime RL updates episodic memory entries based on their usefulness in downstream tasks. Bad memories decay; useful ones get reinforced.
- **Evidence**: 2026/01 paper. Not widely replicated yet.
- **Effort**: 2-3 weeks (requires reward signal engineering).
- **Verdict**: SKIP for 7-day sprint. Single-paper, our episode_log already gets similar effect via curation.

### 1.3 EverMemOS — Self-Organizing Memory OS
- **Mechanism**: Memory OS with scheduler deciding what to promote/demote between hot/cold tiers.
- **Evidence**: 2026/01 single paper, no independent validation.
- **Verdict**: SKIP — too novel, pattern is basically LRU cache dressed up.

---

## 2. Tool Use Optimization

### 2.1 Speculative Tool Calling (GetStream pattern)
- **Mechanism**: When LLM is ~confident about next tool call, launch it **in parallel with reasoning** instead of waiting. If reasoning confirms → use result. If not → throw away.
- **Evidence**: Production case study (GetStream voice agents). Measured: eliminates ~300-500ms perceptible wait. Widely adopted in voice stacks Q1 2026.
- **Effort**: 3-5 days. Wrap tool calls with a "speculative executor" that dedupes duplicate in-flight calls and has a cancel budget.
- **Expected lift**: 20-40% faster agent loops on common tool patterns (bash + read, rag_query + rag_code).
- **Verdict**: HIGH PRIORITY.

### 2.2 LLMCompiler — DAG-based Parallel Tool Planning
- **Mechanism**: Planner emits a DAG of tool calls upfront; executor runs independent nodes concurrently. Replaces ReAct-style serial loop.
- **Evidence**: Original paper 3.7x speedup on multi-tool tasks. Validated in NVIDIA NeMo Agent Toolkit (Feb 2026). ReWOO variant shows 50-70% latency cut in production.
- **Effort**: 1 week. Need a planner prompt + DAG executor. Can coexist with existing ReAct loop (route easy queries to ReAct, complex to LLMCompiler).
- **Expected lift**: 2-3x latency reduction on tasks with ≥3 independent tool calls.
- **Verdict**: HIGH PRIORITY. We already have 6 orchestration patterns — this becomes pattern #7.

### 2.3 B-PASTE — Beam-Aware Speculative Execution
- **Mechanism**: Maintains beam of future execution subgraphs under resource budget.
- **Evidence**: arXiv 2604.16469 (Apr 2026). Single paper, only 1.4x on edge devices.
- **Verdict**: SKIP — marginal gain, complexity too high.

---

## 3. Cost-Optimal Routing

### 3.1 RouteLLM (preference-trained binary router)
- **Mechanism**: Small classifier decides per-query whether to send to strong model (expensive) or weak model (cheap). Trained on preference data.
- **Evidence**: arXiv 2406.18665. **2x cost reduction without quality loss** on MT-Bench, MMLU, GSM8K. Widely reproduced by third parties in 2025-2026.
- **Effort**: 2-3 days. Train a DistilBERT-size classifier on our episode_log (we already have strong/weak labels implicitly via retry patterns).
- **Expected lift**: ~50% inference cost cut with <2% quality regression.
- **Verdict**: HIGH PRIORITY.

### 3.2 xRouter (RL-trained cost-aware router)
- **Mechanism**: RL agent decides route+model given query; reward = quality - λ·cost.
- **Evidence**: arXiv 2510.08439 (Oct 2025). Outperforms RouteLLM on diverse tasks.
- **Effort**: 2-3 weeks (RL training loop + reward engineering).
- **Verdict**: DEFER — RouteLLM gets 80% of the gain in 20% of the effort.

### 3.3 Router-R1 (LLM-as-router with reasoning)
- **Mechanism**: Router itself is an LLM that reasons about routing then dispatches.
- **Evidence**: OpenReview 2026. Higher quality but slower than RouteLLM.
- **Verdict**: SKIP — adds latency, defeats purpose of routing.

---

## 4. Agent Reliability (test-time compute)

### 4.1 S* — Hybrid Test-Time Scaling for Code
- **Mechanism**: Parallel sampling + sequential refinement + adaptive test-input generation for pairwise selection. Specifically designed for code.
- **Evidence**: arXiv 2502.14382. **+8.4-18.2%** on Qwen2.5-Coder series across LiveCodeBench. Reproduced across model sizes.
- **Effort**: 1 week. Wrap code-generation tool with S* loop: sample 4-8 candidates, generate discriminating test inputs, pairwise compare, pick winner.
- **Expected lift**: ~10-15% on hard coding tasks (LiveCodeBench-Pro tier) — at cost of 4-8x tokens on those tasks only.
- **Verdict**: HIGH PRIORITY — gate to "hard tasks only" via difficulty classifier.

### 4.2 Certified Self-Consistency
- **Mechanism**: Statistical confidence bounds on majority-vote outputs. Says "I'm 95% sure" or triggers more sampling.
- **Evidence**: arXiv 2510.17472. Strong statistical framing, but compute overhead substantial.
- **Effort**: 3-5 days.
- **Verdict**: MEDIUM — good for high-stakes calls (DevSecOps: IAM changes, secret handling), skip for routine.

### 4.3 Variable Granularity Search (VG-Search)
- **Mechanism**: Generalizes beam search + BoN with tunable granularity knob.
- **Evidence**: arXiv 2505.11730.
- **Verdict**: DEFER — S* is more targeted for our code use case.

### 4.4 Semantic Voting (Jiang 2026)
- **Mechanism**: Majority vote on **semantic equivalence** (not string match). Catches cases where same answer is phrased differently.
- **Evidence**: Cited in FMV paper (2604.15618).
- **Effort**: 2-3 days (need embedding model for equivalence check).
- **Verdict**: MEDIUM — adds robustness on subjective outputs.

---

## 5. Code-Specific Evaluation (beyond SWE-Bench)

### 5.1 SWE-bench-Live (monthly refresh)
- **Mechanism**: Monthly-updated real GitHub issues. Contamination-free. Now 1,565 instances / 164 repos. Includes Windows variant.
- **Evidence**: Public leaderboard, tracked across all major frontier labs.
- **Effort**: 2-3 days to integrate into our daemon CI.
- **Expected lift**: Trustable signal for Surrogate-1 vs frontier quality (vs contaminated SWE-Bench).
- **Verdict**: HIGH PRIORITY. We must self-evaluate monthly.

### 5.2 LiveCodeBench (contest problems)
- **Mechanism**: New Leetcode/AtCoder/Codeforces problems monthly. Tests self-repair, execution, test-prediction too.
- **Evidence**: Public leaderboard, contamination-proof by design.
- **Effort**: 1-2 days.
- **Verdict**: HIGH PRIORITY. Complements SWE-bench-Live (one tests repo-scale, other tests algo reasoning).

### 5.3 SWE-Bench Pro (Scale AI)
- **Mechanism**: More rigorous filter of SWE-Bench.
- **Evidence**: Public leaderboard.
- **Verdict**: LOW — SWE-bench-Live already covers this niche better.

### 5.4 BigCodeBench (realistic library usage)
- **Mechanism**: Problems that force use of real libraries (scipy, pandas).
- **Verdict**: MEDIUM — good complementary signal, add after Live+LCB.

---

## 6. RAG Retrieval Quality

### 6.1 BM25 + Dense + RRF (Reciprocal Rank Fusion)
- **Mechanism**: Run BM25 and vector concurrently, fuse ranks with RRF (k=60). No score-normalization hell.
- **Evidence**: Production case studies (Elasticsearch, OpenSearch Feb 2026). **Recall@10: 91% hybrid vs 78% dense vs 65% BM25**. Fusion cost ~6ms.
- **Effort**: 2-3 days. We have SQLite FTS (=BM25) + ChromaDB (=dense). Just add RRF fusion layer.
- **Expected lift**: 13-25% recall gain. Massive for code-chunk retrieval where identifier-exact matches are critical.
- **Verdict**: HIGHEST PRIORITY. This is free performance — we have both sides already.

### 6.2 Qwen3-Reranker as cross-encoder stage
- **Mechanism**: After hybrid retrieves top-50 candidates, Qwen3-Reranker (0.6B or 4B) reranks them with full query-doc attention.
- **Evidence**: Qwen3-Embedding/Reranker series (2025). Qwen3-Embedding-8B #1 on MTEB. Small 0.6B variant loses only ~5% vs 8B.
- **Effort**: 2-3 days (load model, wrap rerank stage).
- **Expected lift**: 10-15% precision@5 on top of hybrid. Small model runs on CPU.
- **Verdict**: HIGH PRIORITY. Stack as: Hybrid → Rerank → Generate.

### 6.3 HyDE (Hypothetical Document Embeddings)
- **Mechanism**: Generate fake answer to the query, embed that instead of the raw query.
- **Evidence**: Strong on abstract queries + vocabulary mismatch. Extra LLM call adds ~200-500ms.
- **Effort**: 1 day.
- **Verdict**: MEDIUM — use only when retrieval quality is low (gate on confidence score).

---

## 7. Agent Observability (self-hosted, not SaaS)

### 7.1 OpenTelemetry GenAI Semantic Conventions
- **Mechanism**: **Stable in early 2026**. Standard span attributes for LLM calls, token usage, model params. Vendor-neutral.
- **Evidence**: OTel spec stable Q1 2026. Adopted by OpenAI Agents SDK.
- **Effort**: 2-3 days (instrument our daemon with OTel SDK).
- **Verdict**: HIGH PRIORITY. Foundation — future backends plug in free.

### 7.2 Arize Phoenix (self-hosted, OSS)
- **Mechanism**: OTel-native. Traces + evals + prompt playground. Docker/Kubernetes.
- **License**: Elastic License 2.0 (free for our use).
- **Effort**: 1 day to stand up.
- **Verdict**: HIGH PRIORITY — pair with OTel instrumentation.

### 7.3 Langfuse (self-hosted, MIT)
- **Mechanism**: Similar to Phoenix but MIT-licensed (fully free). Rich evals + dataset management.
- **Verdict**: ALTERNATIVE — pick one, not both. Phoenix has tighter OTel integration; Langfuse has better dataset tooling. Recommend Phoenix for immediacy, migrate to Langfuse later if dataset features needed.

### 7.4 SigNoz + Grafana Tempo
- **Mechanism**: General-purpose OTel backends.
- **Verdict**: SKIP for agent-specific tracing. Use Phoenix/Langfuse which have LLM-first UX.

---

## 8. Cheap-but-strong Small Models (<7B) as judge/filter

### 8.1 VERITAS 3B / 8B (beats MiniCheck on conversations)
- **Mechanism**: Multi-task training across NLI, QA, dialog. Handles conversational hallucination where MiniCheck fails.
- **Evidence**: arXiv 2411.03300. Outperforms open-access models, competitive with GPT-4-turbo. 3B fits CPU.
- **Effort**: 1 day swap-in (API-compatible with MiniCheck if we use a unified verifier interface).
- **Expected lift**: Better coverage of our daemon's conversational outputs (where MiniCheck underfits).
- **Verdict**: HIGH PRIORITY — direct drop-in upgrade over MiniCheck.

### 8.2 Qwen3-Reranker-0.6B as semantic-equivalence judge
- **Mechanism**: Reuse reranker for "does candidate A == candidate B?" judgments in self-consistency voting.
- **Evidence**: Cross-encoder architecture is naturally suited.
- **Effort**: 2 days.
- **Verdict**: MEDIUM — efficient reuse of model we're already loading.

### 8.3 Llama-3.1-Bespoke-MiniCheck-7B (keep)
- **Mechanism**: Existing SOTA for pure NLI fact-check.
- **Verdict**: KEEP for non-conversational grounding checks. Pair with VERITAS for conversational.

---

## Rejected Techniques (don't waste time)

| Technique | Reason |
|---|---|
| xRouter | RL training cost > RouteLLM benefit |
| Router-R1 | Adds latency, defeats routing purpose |
| MemRL | Single paper, episode_log does similar |
| EverMemOS | Novel LRU-cache-in-disguise, unvalidated |
| B-PASTE | 1.4x gain on edge, too niche |
| Adaptive Thinking (latent) | Requires model mods |
| DeepVerifier GAIA | Requires specific agent framework coupling |
| Arize/Langfuse commercial tiers | SaaS, violates constraint |
| SigNoz for LLM tracing | General-purpose, not LLM-first |

---

## Integration Plan Map

```
Surrogate-1 daemon
├─ Retrieval layer          → [6.1 RRF] + [6.2 Qwen3-Reranker]       ← HIGH (free from existing stack)
├─ Memory layer             → [1.1 MAGMA multi-graph]                ← HIGH (extend FalkorDB)
├─ Planner                  → [2.2 LLMCompiler DAG pattern]          ← HIGH (add to 6 patterns → 7)
├─ Executor                 → [2.1 Speculative tool calling]         ← HIGH
├─ Model routing            → [3.1 RouteLLM]                         ← HIGH
├─ Code generation          → [4.1 S* test-time scaling] (gated)     ← MEDIUM-HIGH
├─ Fact-check               → [8.1 VERITAS 3B] swap MiniCheck        ← HIGH
├─ Observability            → [7.1 OTel] + [7.2 Phoenix]             ← HIGH
└─ Eval harness             → [5.1 SWE-bench-Live] + [5.2 LCB]       ← HIGH
```

---

## Sources

- [MAGMA arXiv 2601.03236](https://arxiv.org/abs/2601.03236)
- [Awesome Agent Memory Papers](https://github.com/TsinghuaC3I/Awesome-Memory-for-Agents)
- [Memory in the Age of AI Agents Survey](https://github.com/Shichun-Liu/Agent-Memory-Paper-List)
- [Speculative Tool Calling (GetStream)](https://getstream.io/blog/speculative-tool-calling-voice/)
- [B-PASTE arXiv 2604.16469](https://arxiv.org/abs/2604.16469)
- [LLMCompiler Agent Pattern](https://agent-patterns.readthedocs.io/en/stable/patterns/llm-compiler.html)
- [ReWOO Agent Pattern / NeMo Toolkit](https://deepwiki.com/NVIDIA/NeMo-Agent-Toolkit/4.3-rewoo-agent)
- [RouteLLM arXiv 2406.18665](https://arxiv.org/abs/2406.18665)
- [xRouter arXiv 2510.08439](https://arxiv.org/html/2510.08439v1)
- [Router-R1](https://openreview.net/forum?id=DWf4vroKWJ)
- [S* Test-Time Scaling for Code](https://arxiv.org/html/2502.14382v1)
- [Certified Self-Consistency](https://arxiv.org/html/2510.17472v2)
- [Majority Voting for Code Generation 2026](https://arxiv.org/html/2604.15618)
- [VG-Search arXiv 2505.11730](https://arxiv.org/abs/2505.11730)
- [SWE-bench-Live](https://swe-bench-live.github.io/)
- [LiveCodeBench](https://livecodebench.github.io/)
- [Hybrid Search Guide April 2026](https://blog.supermemory.ai/hybrid-search-guide/)
- [RRF + BM25 + HNSW (Elasticsearch)](https://ashutoshkumars1ngh.medium.com/hybrid-search-done-right-fixing-rag-retrieval-failures-using-bm25-hnsw-reciprocal-rank-fusion-a73596652d22)
- [Qwen3-Embedding & Reranker](https://github.com/QwenLM/Qwen3-Embedding)
- [Late Interaction Overview (Weaviate)](https://weaviate.io/blog/late-interaction-overview)
- [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/)
- [Arize Phoenix (OSS)](https://phoenix.arize.com/)
- [Langfuse GitHub](https://github.com/langfuse/langfuse)
- [Laminar vs Langfuse vs LangSmith 2026](https://laminar.sh/blog/2026-01-29-laminar-vs-langfuse-vs-langsmith-llm-observability-compared)
- [VERITAS arXiv 2411.03300](https://arxiv.org/html/2411.03300v1)
- [Bespoke-MiniCheck-7B](https://huggingface.co/bespokelabs/Bespoke-MiniCheck-7B)

## See Also

- [[anti-hallucination-playbook]]
- [[mythos-braincog-analysis]]
- [[persona-imitation-ai-2026]]
- [[models-2026-landscape]]

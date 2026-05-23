---
name: Data, ML, AI Ops Trends 2026
description: MLOps, LLMOps, AIOps, DataOps, DBOps, vector DBs, RAG, lakehouse
tags: [trends, mlops, llmops, aiops, dataops, dbops, rag, 2026]
last_updated: 2026-04-18
---

# Data, ML, AI Ops Trends 2026

Comprehensive reference for operating data and AI systems in production in 2026. Covers the current state of MLOps, LLMOps, AIOps, DataOps, DBOps, data lakehouses, streaming, vector databases, RAG, and agent frameworks.

---

## MLOps 2026

The enterprise pattern is **hybrid**: managed cloud (SageMaker / Vertex AI / Azure ML) for infrastructure + open source (MLflow, Feast, Evidently) for portability and cost control. Feature stores and lakehouse integrations are now standard, and experiment trackers have evolved into GenAI observability primitives.

### Training & orchestration
- **MLflow** — still the default for run-centric experiment tracking and model registry. Governs versions, metrics, artifacts, promotion stages.
- **Kubeflow** — Kubernetes-first pipelines. Good when you control your own infra and want maximum customization.
- **Weights & Biases** — tracking + visualization, increasingly expanding into LLM eval territory.
- **DVC** — data + pipeline determinism. Pairs well with MLflow (DVC = data, MLflow = experiments).

### Serving
- **KServe** — dominant for K8s-based model serving, supports PyTorch / TF / ONNX / HuggingFace.
- **BentoML** — unified packaging and deployment across cloud platforms.
- **Seldon Core** — advanced deployment topologies (A/B, shadow, canary).
- **SageMaker Endpoints / Vertex AI Endpoints / Azure ML Online Endpoints** — managed serving with auto-scale.

### Feature stores (first-class in 2026)
| Store | Type | Strengths |
|---|---|---|
| **Feast** | OSS | Point-in-time correctness, low-latency online + offline retrieval, lightweight |
| **Tecton** | Enterprise | Full lifecycle, streaming features, strong SLAs. **Tecton is joining Databricks** to power real-time features for AI agents |
| **Databricks Feature Store** | Lakehouse | ACID, lineage, MLflow integration, seamless for Databricks teams |

Vector / embedding support is now expected — feature stores treat embeddings as first-class features.

### Model registry
- MLflow Registry (OSS), SageMaker Model Registry, Vertex Model Registry. Promotion stages (staging → prod) + signed model cards + lineage to training data are table stakes.

Sources: [Top MLOps Tools 2026 (Medium)](https://medium.com/online-inference/top-mlops-tools-in-2026-858fd479acac), [MLOps 2026 Definitive Guide](https://rahulkolekar.com/mlops-in-2026-the-definitive-guide-tools-cloud-platforms-architectures-and-a-practical-playbook/), [Feature Store Comparison 2026](https://tacnode.io/post/how-to-evaluate-a-feature-store), [Tecton joining Databricks](https://www.databricks.com/blog/tecton-joining-databricks-power-real-time-data-personalized-ai-agents).

---

## LLMOps 2026

Inference cost dominates training cost once a model hits production. Optimization (quantization, PagedAttention, speculative decoding) is now a specialized discipline.

### Inference engines
| Engine | Niche | Notes |
|---|---|---|
| **vLLM** | High-throughput multi-tenant serving | PagedAttention, default for GPU fleets. 2026: Triton-based attention backend → runs on NVIDIA / AMD / Intel |
| **TensorRT-LLM** | Lowest latency on NVIDIA | Best for dedicated NVIDIA stacks, hardest to operate |
| **TGI (HuggingFace)** | Production-ready, HF ecosystem | Model parallelism, quantization, speculative decoding, deploys easily |
| **SGLang** | Structured / constrained generation | Growing fast for agent / JSON-output workloads |
| **Ollama** | Local dev + single-user | 150k+ GitHub stars, default for laptops, macOS + Apple Silicon via MLX |
| **llama.cpp** | CPU / edge | GGUF quantized models on commodity hardware |

### Quantization (must-have, not nice-to-have)
- Formats: **GPTQ, AWQ, GGUF, INT8, INT4, FP8**.
- vLLM supports all of the above; pick the format your model publishes.
- Trade-off: 4-bit cuts memory ~75% but costs 1–3 pp on benchmarks. Measure on *your* eval set.
- **NVIDIA Model Optimizer** — unified lib for quant + pruning + distillation + speculative decoding; emits artifacts for TensorRT-LLM / vLLM.

### Prompt + observability
- **LangSmith, Langfuse, Helicone, Arize Phoenix** — traces, evals, prompt versioning, regression detection.
- Prompt management is now version-controlled like code (git-backed) with eval harnesses in CI.

### Economics
Inference on frontier LLMs runs **$0.50–$3.00 per 1M input tokens**. Self-hosted with vLLM + quantized open models (Llama 3.3 70B, Qwen2.5, DeepSeek) is 5–20× cheaper at volume — but operational cost of GPUs is real.

Sources: [vLLM vs TGI vs TensorRT-LLM vs SGLang (Yotta Labs)](https://www.yottalabs.ai/post/best-llm-inference-engines-in-2026-vllm-tensorrt-llm-tgi-and-sglang-compared), [vLLM 2026](https://www.programming-helper.com/tech/vllm-2026-high-performance-inference-serving-ai-models-python), [LLMOps Architecture 2026](https://calmops.com/architecture/llmops-architecture-managing-llm-production-2026/), [Ollama vs vLLM Benchmark](https://www.sitepoint.com/ollama-vs-vllm-performance-benchmark-2026/).

---

## AIOps 2026

AIOps has moved from buzzword to baseline. Hybrid workflow is the norm: **AI recommends → humans approve → systems execute → every step logged + explainable**. Still, ~50% of PoCs don't reach production — mostly for structural/org reasons, not tech.

### Capability tiers
1. **Anomaly detection** — statistical + ML baselines per-metric per-service. Modern stacks add causal AI to go from "outlier" to "cause-effect".
2. **Correlation** — cluster alerts across logs, metrics, traces, events into a single incident.
3. **RCA** — trace event chain backward to the root. LLMs summarize the story for on-call.
4. **Auto-remediation** — execute pre-approved runbooks (restart pod, scale out, failover). Gated by blast-radius policy.
5. **Predictive** — forecast capacity exhaustion, certificate expiry, upstream degradation.

### Tools (2026 top of mind)
- **Datadog Bits AI / Watchdog** — end-to-end observability + anomaly + AI insights.
- **PagerDuty AIOps** — event intelligence, auto-pause, probable root cause.
- **New Relic AI, Dynatrace Davis AI, Splunk ITSI** — incumbents with strong causal models.
- **OpenObserve, Selector, Metoro** — open / emerging observability+AIOps.
- **Grafana + Prometheus + Loki + Tempo + Alloy** — OSS stack, add **Grafana Sift** for AI-assisted RCA.

### LLM-powered log anomaly detection
Recent literature (2025–26) shows LLMs substantially improve log anomaly detection over template-based approaches, especially for unseen / rare events. Cost is real — sample + filter before calling the model.

Sources: [AIOps 2026 Guide](https://aiopscommunity.com/the-ultimate-guide-to-aiops-2026-edition/), [Top 10 AIOps Platforms 2026](https://openobserve.ai/blog/top-10-aiops-platforms/), [Best AIOps Tools (Metoro)](https://metoro.io/blog/best-aiops-tools), [RCA Automation 2026](https://medium.com/@growth_through_intelligence/leading-root-cause-analysis-rca-automation-platforms-093e8b09a9e8), [SRE + AI Incident Response](https://cloudnativenow.com/contributed-content/how-sres-are-using-ai-to-transform-incident-response-in-the-real-world/), [AIOps + LLM log anomaly (ScienceDirect)](https://www.sciencedirect.com/science/article/pii/S2667305325001346).

---

## DataOps 2026

Data contracts went mainstream. Orchestration got smarter (AI-assisted retries + scheduling). Analytics engineering is now its own role. **dbt Labs merged with Fivetran (Oct 2025)** — combined ARR near $600M, 10k+ customers, potential $10B+ valuation.

### Core stack
- **Orchestration**: Dagster (asset-based, typed, best DX), Airflow (huge ecosystem, 2.x with new task SDK), Prefect (Pythonic), Temporal (durable execution for engineering-heavy workloads).
- **Transformation**: dbt Core / dbt Cloud (SQL-first), SQLMesh (versioned semantic layer, virtual envs), Coalesce.
- **Ingestion**: Airbyte (OSS connectors), Fivetran (managed, now same umbrella as dbt), Estuary (real-time CDC), Meltano (Singer-based, OSS).
- **Catalog + lineage**: Atlan, DataHub (OSS, LinkedIn), OpenMetadata, Unity Catalog.
- **Quality**: Great Expectations, Soda, Monte Carlo, Elementary (dbt-native).

### Data contracts (new baseline)
Machine-readable + version-controlled + wired into CI. When a producer updates a dataset, automated validation rejects contract-violating changes before they hit prod. Consumers get schema-drift notifications. Tools: **Gable, Soda Contracts, dbt Contracts, Open Data Contract Standard (Bitol)**.

### Data mesh (resuscitated by dbt)
Mesh without contracts = chaos. dbt Mesh lets teams own domain data products, publish versioned interfaces, and consume upstream products — federated governance, central standards, decentralized execution.

### AI-native orchestration
Dagster and Airflow both invested in LLM/GenAI assets as first-class. Asset lineage now includes vector indexes, fine-tuned models, prompt files. Backfills and retries are smarter — Dagster schedules based on past run history + data freshness SLAs, not fixed cron.

Sources: [DataOps Best Practices 2026 (lakeFS)](https://lakefs.io/blog/dataops-best-practices/), [Data Management Trends 2026 (N-iX)](https://www.n-ix.com/data-management-trends/), [Data Mesh via dbt](https://www.getdbt.com/blog/frequently-asked-questions-about-data-mesh/), [Dagster DataOps](https://dagster.io/learn/dataops), [dbt Review 2026](https://www.integrate.io/blog/dbt-review/).

---

## DBOps 2026

Database DevOps is no longer "DBA scripts in Confluence". Schema change, migration, and drift detection all live in CI/CD with approvals and rollback.

### Schema change automation
- **Bytebase** — all-in-one DB DevOps + CI/CD, GUI for dev/DBA collaboration. Covers MySQL / Postgres / SQL Server / Oracle / MongoDB / Redis.
- **Liquibase** — the incumbent, JVM-based, huge install base.
- **Flyway** — simple, migration-first, widely used in JVM shops.
- **Atlas (Ariga)** — declarative schema management with diff engine. Gaining ground fast.
- **pgroll** (Xata) — Postgres-specific, Go-based, zero-downtime migrations using views.
- **graphile-migrate** — roll-forward-only, opinionated, TypeScript + SQL.

### Migration & replication
- **AWS DMS** — schema conversion + continuous CDC between engines.
- **Debezium** — OSS CDC via Kafka, battle-tested.
- **Airbyte / Estuary / TapData** — managed CDC and batch.
- **MongoDB Relational Migrator** — uses GenAI to convert SQL → MongoDB data models + code.

### Monitoring + tuning
- **pganalyze, pgHero, Percona PMM, Datadog DB Monitoring, Ottertune (auto-tuning via RL)**.
- LLM-assisted query review is emerging — paste an EXPLAIN, get index + rewrite suggestions, gated on a cost check.

Sources: [Top Postgres Migration Tools 2026 (Bytebase)](https://www.bytebase.com/blog/top-open-source-postgres-migration-tools/), [Top Schema Migration Tools](https://www.bytebase.com/blog/top-database-schema-change-tool-evolution/), [MongoDB Relational Migrator](https://www.mongodb.com/products/tools/relational-migrator), [Data Migration Automation 2026](https://concentrus.com/data-migration-automation-tools/).

---

## Data Lakehouse 2026

Open table formats are settled: **Iceberg is the industry standard**, Delta stays strong on Databricks, Hudi wins on high-frequency streaming upserts. 77% of businesses adopted lakehouse architectures.

| Format | Sweet spot | Distinctive feature |
|---|---|---|
| **Apache Iceberg** | Broadest engine support (Spark / Flink / Trino / DuckDB / Snowflake / BigQuery / RisingWave) | **Partition evolution without rewrite** — unique. Hidden partitioning, schema evolution, time travel |
| **Delta Lake** | Spark / Databricks native | Deep Spark integration; change data feed (now OSS in Delta 2.0+); strong on Databricks, weaker elsewhere |
| **Apache Hudi** | Streaming CDC + frequent upserts | Merge-on-read with column-level delta logs → lowest write amplification. Also a storage engine / DLMS, not just a format |

**Catalogs** — AWS Glue, Unity Catalog (now open-sourced), Apache Polaris (Snowflake-backed), Nessie (Dremio). Unity + Polaris both push for Iceberg REST catalog interop.

**Query engines** — DuckDB, Trino, Spark, Flink, RisingWave, Databricks Photon, Snowflake, BigQuery. The lakehouse is now engine-agnostic when you pick Iceberg.

Sources: [Iceberg vs Delta vs Hudi (Onehouse)](https://www.onehouse.ai/blog/apache-hudi-vs-delta-lake-vs-apache-iceberg-lakehouse-feature-comparison), [Iceberg vs Delta vs Hudi 2026 (RisingWave)](https://risingwave.com/blog/apache-iceberg-vs-delta-lake-vs-hudi-2026/), [Ultimate Lakehouse Guide 2025–26](https://datalakehousehub.com/blog/2025-09-2026-guide-to-data-lakehouses/), [Dremio Table Format Comparison](https://www.dremio.com/blog/exploring-the-architecture-of-apache-iceberg-delta-lake-and-apache-hudi/).

---

## Streaming 2026

Kafka is the backbone, Flink is the brain. New categories: **diskless Kafka** (WarpStream, Confluent Freight), **streaming-native storage**, and **AI orchestration layers** over streams.

- **Apache Kafka** — still the default transport. Cloud-native variants (Confluent Cloud, MSK, Redpanda BYOC, WarpStream).
- **Apache Flink** — default stream processor. SQL + DataStream. Pairs with Kafka / Kinesis / Pulsar for fault-tolerant event storage.
- **Redpanda** — Kafka-wire-compatible, C++, no JVM, lower latency / higher per-node throughput. 2026 push: **Agentic AI infrastructure**.
- **Apache Pulsar** — multi-tenant, tiered storage, geo-replication; good for telco / IoT.
- **ksqlDB, Kafka Streams** — lighter-weight stream processing; Kafka Streams for JVM services, ksqlDB declining relative to Flink SQL.
- **RisingWave, Materialize, ClickHouse** — streaming materialized views / real-time OLAP.

Enterprises bet on platforms with governance, open standards, and clear AI integration — not exotic tech with no ecosystem.

Sources: [Data Streaming Trends 2026 (Kai Waehner)](https://www.kai-waehner.de/blog/2025/12/10/top-trends-for-data-streaming-with-apache-kafka-and-flink-in-2026/), [Streaming Landscape 2026](https://www.kai-waehner.de/blog/2025/12/05/the-data-streaming-landscape-2026/), [Redpanda 2026](https://medium.com/@mgaurang123/redpanda-the-kafka-compatible-streaming-platform-thats-turning-heads-6067e19f4a39), [Kafka Alternatives (Tinybird)](https://www.tinybird.co/blog/apache-kafka-alternatives).

---

## Vector Databases 2026

**Milvus, Weaviate, Qdrant are the three most adopted production choices.** pgvector covers most under-10M-vector use cases without adding a new system. Vector DBs are converging into **unified retrieval engines** (dense + sparse + filtering).

| DB | Language | Strengths | Scale ceiling (practical) |
|---|---|---|---|
| **Qdrant** | Rust | Real-time updates, sophisticated filtering, excellent DX, good cost | 100M+ |
| **Weaviate** | Go | Built-in vectorization modules, GraphQL API, hybrid search, good for knowledge graphs | 100M+ |
| **Milvus** | Go / C++ | Trillion-vector scale, multiple ANN indexes (HNSW / IVF / PQ / DiskANN), strongest at extreme scale | 1B–1T |
| **pgvector / pgvectorscale** | C (Postgres ext) | Zero extra infra. pgvectorscale (Timescale) closes the perf gap significantly | 10–100M |
| **Chroma** | Python | Dead simple, dev-first, great for prototypes | 1–10M |
| **Pinecone** | Managed | Fully managed, no ops, predictable latency | 100M+ (paid) |
| **OpenSearch k-NN / Elasticsearch** | Java | Keep in stack if you already run it, decent performance | 50M+ |
| **Typesense** | C++ | Fast keyword + vector hybrid, great DX | 10M+ |

**Performance note**: at 50M vectors at 99% recall, pgvectorscale has hit ~471 QPS vs Qdrant ~41 QPS in benchmarks — but benchmarks vary wildly by index config, filter selectivity, and hardware. **Always benchmark on your data.**

Sources: [Best Vector DBs 2026 (Firecrawl)](https://www.firecrawl.dev/blog/best-vector-databases), [Best Vector DBs 2026 (Encore)](https://encore.dev/articles/best-vector-databases), [Top 9 Vector DBs (Shakudo)](https://www.shakudo.io/blog/top-9-vector-databases), [Vector DB Choice Guide](https://medium.com/@elisheba.t.anderson/choosing-the-right-vector-database-opensearch-vs-pinecone-vs-qdrant-vs-weaviate-vs-milvus-vs-037343926d7e).

---

## RAG & GraphRAG 2026

**RAG v1 is dead.** "Chunk → embed → cosine → stuff into prompt" was always a prototype. 2026 production RAG looks radically different.

### The hybrid baseline (non-negotiable)
1. **Retrieve broadly** — top-20 from vector search + BM25 in parallel.
2. **Fuse** — Reciprocal Rank Fusion (RRF) to combine rankings.
3. **Rerank precisely** — cross-encoder (Cohere Rerank, BGE reranker, Jina) → top-5.
4. **Send only top-5** to the LLM.
5. **Observe everything** — LangSmith / Langfuse / Phoenix for every retrieval, score, latency.

Research shows hybrid retrieval beats single-strategy by **10–30%** in accuracy across datasets.

### Beyond similarity: GraphRAG
When queries need multi-hop reasoning ("How does X relate to Y through Z?"), pure vector search fails. **GraphRAG adds a knowledge-graph layer** — Microsoft GraphRAG, LlamaIndex PropertyGraphIndex, Neo4j + LangChain. Recent benchmarks: **~35% accuracy lift on global / relationship queries** vs. traditional RAG. Trade-off: graph construction cost + maintenance.

### Advanced patterns
- **Query rewriting / HyDE** — rewrite user query or generate hypothetical answer to improve recall.
- **Agentic RAG** — agent decides which retriever to call, iterates if results are weak.
- **Self-correcting RAG (Corrective RAG, Self-RAG)** — model evaluates retrieved context, re-queries if low-confidence.
- **Colbert / late-interaction retrieval** — stronger recall than single-vector embeddings at manageable cost.
- **Contextual chunking** — add parent-doc summary or structured metadata to each chunk to preserve context.

### Infrastructure consolidation
Weaviate / Qdrant / Milvus now all offer dense + sparse + structured filter in one engine. The "separate BM25 + separate vector DB" stack is consolidating.

Sources: [10 RAG Shifts 2026 (Microsoft Azure)](https://medium.com/microsoftazure/10-rag-shifts-redefining-production-ai-in-2026-7acbdd66076c), [RAG Revisited 2026](https://aboullaite.me/rag-revisited-2026/), [GraphRAG 2026 Practitioner's Guide](https://medium.com/graph-praxis/graph-rag-in-2026-a-practitioners-guide-to-what-actually-works-dca4962e7517), [Hybrid Search RAG Guide 2026](https://calmops.com/ai/hybrid-search-rag-complete-guide-2026/), [Production RAG Architecture 2026](https://brlikhon.engineer/blog/building-production-rag-systems-in-2026-complete-architecture-guide), [RAG at Scale (Redis)](https://redis.io/blog/rag-at-scale/).

---

## Agent Frameworks 2026

No single "best" — pick by use case. Most production teams use **2+ frameworks** (LlamaIndex for retrieval + CrewAI or LangGraph for orchestration).

| Framework | Best for | Notes |
|---|---|---|
| **LangGraph** (LangChain) | Complex stateful workflows | Graph with typed nodes + edges, checkpoints, human-in-the-loop. The LangChain team's real answer to "how to productionize agents" |
| **LlamaIndex** | Data-heavy / RAG agents | Best-in-class for indexing and retrieval over large data. PropertyGraphIndex for GraphRAG |
| **CrewAI** | Role-based multi-agent teams | Agents with roles + goals + tasks. Minimal config, fast to prototype |
| **Microsoft AutoGen** | Multi-agent conversations + code exec | Strong for research / complex coordination, Python + .NET support |
| **Microsoft Semantic Kernel** | .NET / enterprise | Plugin-first, first-class on Azure |
| **OpenAI Agents SDK / Swarm** | OpenAI-centric stacks | Lightweight handoffs, sessions, tool use |
| **Haystack** | Production search + RAG pipelines | deepset, strong pipeline abstraction, good for structured enterprise search |
| **DSPy** | Compiler / optimizer for prompts + programs | Treats prompts as programs you compile against metrics |
| **Pydantic AI** | Type-safe Python agents | Tight pydantic validation, great DX for structured outputs |
| **LangFlow / n8n / Flowise** | Visual / low-code | LangFlow ~140k stars; n8n crossed 150k in 2025 |

**Trend**: structured outputs via type-safe schemas (Pydantic, Zod) + constrained decoding (SGLang, Outlines) are now default over "parse the LLM's markdown".

Sources: [AI Agent Frameworks Comparison 2026 (Turing)](https://www.turing.com/resources/ai-agent-frameworks), [Top Agentic AI Frameworks 2026](https://www.alphamatch.ai/blog/top-agentic-ai-frameworks-2026), [LangChain vs CrewAI vs AutoGen 2026](https://www.ai-agentsplus.com/blog/ai-agent-framework-comparison-2026), [TechAhead Agent Framework Comparison](https://www.techaheadcorp.com/blog/top-agent-frameworks/).

---

## Trending repos (snapshot, Apr 2026)

Star counts approximate; track live at [star-history.com](https://www.star-history.com/).

| Repo | Category | Approx stars | Why it matters |
|---|---|---|---|
| `ollama/ollama` | Local LLM runtime | 150k+ | Default for local inference, massive ecosystem |
| `n8n-io/n8n` | Workflow automation + AI | 150k+ | Crossed 150k in 2025, strong AI node library |
| `langflow-ai/langflow` | Visual agent builder | 140k+ | Low-code LangChain |
| `langchain-ai/langchain` | Agent framework | 90k+ | Still the dominant framework despite criticism |
| `vllm-project/vllm` | LLM inference | 40k+ | PagedAttention, default production serving |
| `run-llama/llama_index` | RAG / data framework | 35k+ | Best-in-class for retrieval |
| `apache/iceberg` | Lakehouse table format | 7k+ | Industry standard 2026 |
| `dagster-io/dagster` | Orchestration | 12k+ | Asset-based, typed |
| `dbt-labs/dbt-core` | Transformation | 10k+ | Merged with Fivetran |
| `feast-dev/feast` | Feature store | 6k+ | OSS feature store |
| `qdrant/qdrant` | Vector DB | 20k+ | Rust, filtering king |
| `weaviate/weaviate` | Vector DB | 11k+ | Hybrid search + GraphQL |
| `milvus-io/milvus` | Vector DB | 30k+ | Extreme scale |
| `NVIDIA/Model-Optimizer` | Quantization | 2k+ | Unified quant / prune / distill toolkit |

Sources: [Awesome-LLMOps (InftyAI)](https://github.com/InftyAI/Awesome-LLMOps), [Top Agentic GitHub Repos](https://opendatascience.com/the-top-ten-github-agentic-ai-repositories-in-2025/).

---

## Actionable recommendations for Ashira (Excise stack)

Context: MSSQL + Firestore + Algolia + Typesense today; goal is to add a RAG knowledge base.

### RAG stack recommendation (pragmatic, low-ops)

1. **Vector DB: Qdrant (self-hosted) or Typesense** (you already run it).
   - Typesense added vector search + hybrid in 2024 — if your current KB corpus is <5M chunks, use Typesense and avoid introducing a new DB.
   - If you expect growth past 10M chunks or need advanced filtering, add **Qdrant** (Rust, simple Docker deploy, excellent filter semantics).
   - Do *not* start with Milvus — it's over-powered for your scale and operationally heavy.

2. **Retrieval pattern: Hybrid (vector + BM25) + reranker**.
   - Typesense gives hybrid natively; Qdrant + OpenSearch or sparse vectors works too.
   - Reranker: **Cohere Rerank** (managed, $) or **BGE-reranker-v2** self-hosted via vLLM/TGI.
   - Top-20 → RRF → rerank → top-5 → LLM.

3. **Orchestration: LlamaIndex for retrieval, LangGraph for agent flows.**
   - LlamaIndex handles Excise doc ingestion, chunking, PropertyGraphIndex if you want GraphRAG for regulation cross-refs.
   - LangGraph for multi-step agent (classify → retrieve → draft → cite-check).

4. **LLM serving**:
   - **Phase 1 (now)**: Claude via Anthropic API for drafting + GPT-4 class for complex reasoning.
   - **Phase 2 (scale)**: self-host Qwen2.5-72B or Llama-3.3-70B on vLLM (AWQ 4-bit) once you have volume to justify GPU cost.

5. **Observability: Langfuse (self-hosted) or LangSmith**.
   - Langfuse = OSS + Docker Compose, owns your data, free at your scale.

6. **Data pipeline**:
   - **dbt + Dagster** (or just Airbyte + Dagster if you don't need SQL transforms) to pull Excise regulations + internal KB into the vector store.
   - Add **data contracts** (Soda / dbt Contracts) between Excise DB and the RAG index to catch schema drift before it breaks embeddings.

### MSSQL + Firestore specifics

- **CDC from MSSQL → RAG**: Debezium SQL Server connector → Kafka → consumer that re-embeds changed rows. Or simpler: scheduled dbt job + incremental embedding (skip unchanged).
- **Firestore → RAG**: Firestore triggers (Cloud Functions) publish change events to Pub/Sub; consumer re-embeds and upserts into vector DB. Keep `lastEmbeddedAt` on each doc to avoid re-embedding identical content.
- **Schema migration for MSSQL**: introduce **Bytebase** or **Flyway** for PR-based schema change review — eliminates ad-hoc DBA scripts. Bytebase has the best GUI for mixed dev+DBA teams.

### What to skip (for your scale)

- Don't deploy Milvus, Weaviate cluster, or Kubeflow — overkill until you're past $10M ARR + 50+ ML engineers.
- Don't build a data mesh — single team, single domain. Just use dbt + clear schemas.
- Don't self-host inference before you have sustained GPU-worthy throughput (>2M tokens/day).

### Minimal viable RAG (this quarter)

```
[Excise docs + MSSQL rows + Firestore]
        → Dagster job
        → chunk + embed (OpenAI text-embedding-3-large OR Cohere embed-v4)
        → Typesense (hybrid index) OR Qdrant
        → LlamaIndex retriever + Cohere Rerank
        → LangGraph agent (Claude Sonnet)
        → cited answer + source links
        → Langfuse traces for every call
```

Ship this first. Add GraphRAG / fine-tuned embeddings / self-hosted inference only when you measure specific pain that justifies the cost.

---

*Last updated: 2026-04-18. Always cross-reference with live docs — the AI/data ecosystem moves weekly.*

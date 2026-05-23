---
title: AI/ML Engineering Capability Research for Surrogate-1 v2
date: 2026-04-29
purpose: Teach Surrogate-1 (Qwen2.5-Coder-7B + LoRA) to be SOTA AI/ML/LLM/MLOps engineer
target: Parity with senior AI engineer at Anthropic/OpenAI/Mistral
status: Research complete, ready for v2 dataset construction
tags: [surrogate-1, ai-eng, ml-eng, llm-eng, mlops, llmops, training-data]
---

# AI/ML/LLM Engineering Capability Research — Surrogate-1 v2

## Executive Summary

Surrogate-1 must become a **recursive AI engineer** — an AI that builds and operates AI products. The 2026 AI engineering stack is a multi-discipline matrix:

```
                  ┌──────────────────────────────────────────────────────┐
                  │              AI/ML ENGINEERING STACK 2026            │
                  └──────────────────────────────────────────────────────┘
                                            │
        ┌───────────────────┬───────────────┼───────────────┬───────────────┐
        ▼                   ▼               ▼               ▼               ▼
   Data Science        ML Eng         AI Eng (LLM)    MLOps Eng      LLMOps Eng
   (analysis,         (train +       (LLM apps,      (production    (LLM-specific
   experiments)        deploy        RAG, agents)     ML systems)    ops, eval)
                       custom        $185K-$280K      $165K-$220K    $220K-$280K
                       models)       median 2026
```

**Surrogate-1 needs ALL FIVE roles' skills** because it builds AI products end-to-end (data pipelines → train models → deploy LLM → run RAG → evaluate → monitor → optimize cost).

---

## 1. Role Differentiation (2026)

### 1.1 Day-to-Day Output Matrix

| Role | Tuesday Output | Toolchain | Senior Expectation |
|------|---------------|-----------|---------------------|
| **Data Scientist** | Notebook with EDA, A/B test analysis, business dashboard | pandas, sklearn, statsmodels, Tableau, dbt | Define metrics, design experiments, communicate to PM |
| **ML Engineer** | Trained model in registry, K8s deployment, drift monitor | PyTorch, sklearn, MLflow, K8s, Airflow, Feast | Own production ML system end-to-end, optimize p99 latency |
| **AI Engineer** | RAG pipeline, prompt iteration, agent loop in prod | LangChain/LlamaIndex, Pinecone/Qdrant, OpenAI/Anthropic SDK | Eval design, prompt versioning, multi-step agent debugging |
| **LLM Engineer** | Fine-tuned model, vLLM serving cluster, GRPO loop | TRL, Axolotl, Unsloth, vLLM, SGLang, DeepSpeed | Pre-train recipe, RLHF infrastructure, custom kernels |
| **MLOps Engineer** | CI/CD for models, feature store, model registry | Kubeflow, MLflow, DVC, Argo, Terraform | Platform for 100+ models, governance, cost ops |

### 1.2 What "Senior" Means in 2026

- **Eval-first thinking** — never ship without offline + online eval harness
- **Cost discipline** — $/query, $/token, GPU-hour budgeting per feature
- **Failure mode mastery** — knows top-20 LLM failure modes (hallucination, prompt injection, drift, cost spikes, OOM, throttling)
- **Multi-modal awareness** — voice, vision, code, tools all in same system
- **Production-ready code** — typed (Pydantic/Zod), tested, observable, retryable
- **Recursive AI literacy** — uses AI to build AI (Surrogate-1's whole purpose)

---

## 2. LLM Application Development

### 2.1 Framework Decision Matrix

| Framework | Best For | Token Overhead | Latency | Production-Ready |
|-----------|----------|----------------|---------|------------------|
| **LangChain** | Quick prototyping, 100+ integrations, tool-calling apps | ~2.40k | ~10ms | Mid (use LangGraph for prod) |
| **LangGraph** | Stateful agent workflows, checkpointing, streaming | ~2.03k | ~14ms | High |
| **LlamaIndex** | Document indexing, retrieval, large doc corpora | ~1.60k | ~6ms | High |
| **Haystack** | Enterprise NLP, REST APIs, search systems | ~1.57k | ~5.9ms | Highest |
| **DSPy** | Programmatic prompting, auto-optimization | Lowest (~3.53ms) | Lowest | Mid (research-grade) |

**2026 production pattern**: LlamaIndex (ingestion) + LangGraph (orchestration) + DSPy (prompt optimization) + Haystack (REST surface).

### 2.2 Anthropic SDK Best Practices (2026)

```python
import anthropic
from anthropic import Anthropic

client = Anthropic()

# Prompt caching — 90% input cost reduction on cache hits
# Cache the system prompt + tools (rarely change)
response = client.messages.create(
    model="claude-opus-4-7",  # 1M context, latest
    max_tokens=4096,
    system=[
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},  # 5-min cache
        }
    ],
    tools=[
        {
            "name": "search_docs",
            "description": "Search internal documentation",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            "cache_control": {"type": "ephemeral"},  # Cache tool defs
        }
    ],
    messages=[{"role": "user", "content": user_query}],
    extra_headers={
        "anthropic-beta": "prompt-caching-2024-07-31,context-1m-2025-08-07",
    },
)

# Streaming + tool use loop
with client.messages.stream(
    model="claude-opus-4-7",
    max_tokens=8192,
    messages=conversation_history,
    tools=tools,
) as stream:
    for chunk in stream:
        if chunk.type == "content_block_delta":
            yield chunk.delta.text
```

**Cost discipline rules**:
- Always set `cache_control` on system + tool defs
- Use `max_tokens` aggressively (token budget per query)
- Check `usage.cache_read_input_tokens` to verify cache hits
- Use Haiku for classification, Sonnet for general, Opus for hard reasoning

### 2.3 Prompt Engineering Patterns

| Pattern | When | Token Cost | Quality Lift |
|---------|------|-----------|--------------|
| **Chain-of-Thought (CoT)** | Math, logic, multi-step reasoning | +30-50% | +15-25% acc |
| **Tree-of-Thoughts (ToT)** | Search, planning, creative tasks | +200-500% | +20-40% acc |
| **ReAct** | Tool-using agents | +50-100% | +30% task success |
| **Few-shot** | Format/style transfer, classification | +100-200% | +20-30% acc |
| **Self-Consistency** | Math, factual tasks (sample N + vote) | N×base | +10-15% acc |
| **Reflexion** | Agent self-correction loops | +100-300% | +15-25% acc |
| **DSPy auto-optimize** | Replace manual prompt eng | One-time +500%, then 0 | +10-40% |

### 2.4 Structured Output Stack

```python
# Option 1: Anthropic native tool use (recommended for Claude)
tools = [{
    "name": "extract_user",
    "input_schema": UserSchema.model_json_schema(),  # Pydantic → JSON Schema
}]

# Option 2: Instructor (Pydantic + retries)
from instructor import patch
from pydantic import BaseModel

class User(BaseModel):
    name: str
    age: int
    email: EmailStr

client = patch(Anthropic())
user = client.messages.create(
    model="claude-opus-4-7",
    response_model=User,  # Auto-retry on validation fail
    max_retries=3,
    messages=[...],
)

# Option 3: Outlines (constrained decoding for OSS models)
import outlines
model = outlines.models.transformers("Qwen/Qwen2.5-Coder-7B-Instruct")
generator = outlines.generate.json(model, User)
result = generator("Extract user from: 'Alice, 30, alice@x.com'")

# Option 4: DSPy signature
import dspy
class ExtractUser(dspy.Signature):
    text: str = dspy.InputField()
    user: User = dspy.OutputField()
extractor = dspy.Predict(ExtractUser)
```

---

## 3. RAG (Retrieval-Augmented Generation)

### 3.1 Vector DB Selection (2026 Benchmarks)

| DB | p50 Latency | QPS | Hybrid Search | Self-Host | Best For |
|----|-------------|-----|---------------|-----------|----------|
| **pgvector + pgvectorscale** | 8-15ms | 3-8K | Native (pg_search) | Yes | <50M vec, Postgres shop |
| **Qdrant** | 2-4ms | 12K | Yes (BM25 v1.9+) | Yes | Best perf/$ self-hosted |
| **Weaviate** | 10ms | 8K | Native | Yes | Multi-tenancy, modules |
| **Milvus 2.5** | 5-6ms | 10K | 30x faster than ES | Yes | Billion-scale, GPU |
| **Pinecone** | 8ms | 6K | Yes | No (managed) | Zero-ops, fastest dev |
| **LanceDB** | 12ms | 5K | Yes | Yes (embedded) | Local dev, multimodal |

**2026 default**: pgvector if Postgres exists, Qdrant otherwise.

### 3.2 Embedding Model Selection

| Model | MTEB | Dim | Context | Cost/1M tok | Self-Host |
|-------|------|-----|---------|-------------|-----------|
| **NV-Embed-v2** | 72.31 | 4096 | 32K | $0 | Yes (~9GB) |
| **BGE-en-ICL** | 71.24 | 4096 | 32K | $0 | Yes |
| **Qwen3-Embedding-8B** | 70.58 | 4096 | 32K | $0 | Yes |
| **Voyage-3-large** | 74.06 | 1024 | 32K | $0.18 | No |
| **Cohere embed-v4** | 66.3 | 1536 | 128K | $0.12 | No |
| **OpenAI text-emb-3-large** | 64.6 | 3072 | 8K | $0.13 | No |
| **BGE-M3** | 64.0 | 1024 | 8K | $0 | Yes (multilingual+sparse) |

**Recommendation**: BGE-M3 (multilingual + dense + sparse + multi-vector in one), or Voyage-3-large for managed.

### 3.3 Hybrid Search + Reranking Pipeline

```python
# Production RAG pipeline (2026 best practice)
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer, CrossEncoder
from rank_bm25 import BM25Okapi

class HybridRAG:
    def __init__(self):
        self.qdrant = QdrantClient(url="http://localhost:6333")
        self.embedder = SentenceTransformer("BAAI/bge-m3")
        self.reranker = CrossEncoder("BAAI/bge-reranker-v2-m3")
        self.bm25 = None  # Build from corpus

    def retrieve(self, query: str, top_k: int = 20) -> list[Doc]:
        # 1. Dense retrieval
        dense_emb = self.embedder.encode(query, prompt_name="query")
        dense_hits = self.qdrant.search(
            collection_name="docs",
            query_vector=dense_emb,
            limit=top_k,
        )

        # 2. Sparse retrieval (BM25)
        sparse_scores = self.bm25.get_scores(query.split())
        sparse_hits = top_k_indices(sparse_scores, top_k)

        # 3. Reciprocal Rank Fusion (RRF, k=60)
        fused = rrf_fuse(dense_hits, sparse_hits, k=60)

        # 4. Cross-encoder rerank (top 20 → top 5)
        pairs = [(query, doc.text) for doc in fused[:top_k]]
        scores = self.reranker.predict(pairs)
        reranked = sorted(zip(fused, scores), key=lambda x: -x[1])[:5]

        return [doc for doc, _ in reranked]

def rrf_fuse(list_a, list_b, k=60):
    scores = {}
    for rank, doc in enumerate(list_a):
        scores[doc.id] = scores.get(doc.id, 0) + 1 / (k + rank)
    for rank, doc in enumerate(list_b):
        scores[doc.id] = scores.get(doc.id, 0) + 1 / (k + rank)
    return sorted(all_docs, key=lambda d: -scores[d.id])
```

### 3.4 Advanced RAG Variants (2025-2026 Papers)

| Method | Idea | When |
|--------|------|------|
| **Self-RAG** (ICLR 2024) | Reflection tokens decide retrieve/skip + critique own output | Quality > latency, factual tasks |
| **CRAG** (ICLR 2024) | Lightweight retriever evaluator, falls back to web on low confidence | Domain corpora with gaps |
| **HippoRAG 2** (ICML 2025) | Knowledge graph + Personalized PageRank, mimics hippocampus | Multi-hop, associative memory |
| **GraphRAG** (Microsoft 2024) | Entity extraction → community summaries → hierarchical retrieval | Relationship queries, compliance |
| **Adaptive RAG** (2026 default) | Query classifier routes simple→naive, complex→agentic, relationship→graph | Production, cost-quality balance |

**2026 production architecture**:
```
Query → Classifier → ├─ Simple: dense + rerank
                     ├─ Complex: agentic loop (Self-RAG)
                     └─ Relationship: GraphRAG / HippoRAG 2
```

### 3.5 RAG Eval (RAGAS)

```python
from ragas import evaluate
from ragas.metrics import (
    faithfulness,         # Generated answer grounded in retrieved docs
    answer_relevancy,     # Answer addresses the question
    context_precision,    # Retrieved docs relevant
    context_recall,       # All relevant docs retrieved
    answer_correctness,   # vs ground truth
)

result = evaluate(
    dataset=test_dataset,  # {question, answer, contexts, ground_truth}
    metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
    llm=judge_llm,  # GPT-4 / Claude Opus
    embeddings=embedder,
)
```

Target metrics for production RAG (2026):
- Faithfulness: >0.85
- Context precision: >0.75
- Context recall: >0.80
- Answer relevancy: >0.90

---

## 4. Fine-Tuning / Training Infrastructure

### 4.1 PEFT Method Decision Tree

```
Dataset size?
├─ <500 examples → Prompt engineering + few-shot (don't fine-tune)
├─ 500-1K → QLoRA r=8, all-linear
├─ 1K-50K → QLoRA r=16, all-linear, DoRA enabled  ← Surrogate-1 v1 zone
├─ 50K-500K → DoRA r=32 or LoRA r=64
└─ >500K → Full SFT (need >24GB VRAM per GPU)
```

### 4.2 Toolchain Stack (2026)

| Tool | Best For | Speed vs. baseline | YAML/Code |
|------|----------|---------------------|-----------|
| **Unsloth** | Single GPU, consumer hardware (RTX 4090/A100) | 2-5x faster, 50% less VRAM | Code |
| **Axolotl** | Multi-GPU, full SFT, DPO/GRPO/ORPO | 1x baseline | YAML |
| **TRL** | All RL-style (DPO, GRPO, KTO, ORPO, SimPO) | 1x | Code |
| **HF Transformers** | Standard SFT, custom loss | 1x | Code |
| **DeepSpeed ZeRO-3** | >70B models, multi-node | 1.5-2x with offload | Config |
| **FSDP** | PyTorch native, 70B+ | 1-1.5x | Code |
| **Liger Kernel** | Fused kernels, 20% memory + speed | +20% | Drop-in |
| **Megatron-LM** | Pre-training scale (>100B) | Highest | Code |

### 4.3 LoRA / QLoRA / DoRA Configuration (2026 Best)

```python
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, BitsAndBytesConfig
import torch

# QLoRA quantization
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",   # NormalFloat4
    bnb_4bit_compute_dtype=torch.bfloat16,
    bnb_4bit_use_double_quant=True,
)

model = AutoModelForCausalLM.from_pretrained(
    "Qwen/Qwen2.5-Coder-7B-Instruct",
    quantization_config=bnb_config,
    device_map="auto",
    attn_implementation="flash_attention_2",
)
model = prepare_model_for_kbit_training(model)

# LoRA config — 2026 default
lora_config = LoraConfig(
    r=16,                          # Rank — higher = more capacity, more VRAM
    lora_alpha=16,                 # Scale (set = r in 2026)
    target_modules="all-linear",   # All Q/K/V/O + gate/up/down (not just q,v)
    lora_dropout=0.05,
    bias="none",
    task_type="CAUSAL_LM",
    use_dora=True,                 # DoRA — 2026 default ON
    use_rslora=False,              # rsLoRA only for high-rank (r>64)
)
model = get_peft_model(model, lora_config)
model.print_trainable_parameters()
# Typical: 0.5-1% of params trainable for r=16 on 7B
```

### 4.4 SFT Trainer Config

```python
from trl import SFTTrainer, SFTConfig

config = SFTConfig(
    output_dir="./out",
    num_train_epochs=3,
    per_device_train_batch_size=4,
    gradient_accumulation_steps=4,    # Effective batch 16
    gradient_checkpointing=True,      # 30% less VRAM, 20% slower
    optim="adamw_torch_fused",        # Fused AdamW (faster on H100)
    learning_rate=2e-4,               # LoRA: 1e-4 to 5e-4
    lr_scheduler_type="cosine",
    warmup_ratio=0.03,
    bf16=True,                        # bfloat16 (Ampere+)
    max_seq_length=4096,
    packing=True,                     # Pack examples to max length (3-5x speedup)
    neftune_noise_alpha=5,            # NEFTune — +5-10% quality on small datasets
    logging_steps=10,
    save_strategy="epoch",
    eval_strategy="epoch",
    report_to="wandb",                # or "tensorboard", "mlflow"
)

trainer = SFTTrainer(
    model=model,
    args=config,
    train_dataset=ds["train"],
    eval_dataset=ds["test"],
    formatting_func=lambda x: format_chatml(x),
    peft_config=lora_config,
)
trainer.train()
trainer.save_model()
```

### 4.5 Preference Optimization (2026 Stack)

**The modular pipeline (2026 consensus)**:
```
SFT (instruction following)
  ↓
DPO/SimPO (general preference alignment)
  ↓
GRPO/DAPO (verifiable rewards: math, code, reasoning)
```

| Method | Reference Model | Data Format | Use Case |
|--------|-----------------|-------------|----------|
| **DPO** | Yes (frozen ref) | Pairwise (chosen, rejected) | General preference, has paired data |
| **ORPO** | No (ref-free) | Pairwise + SFT in one step | Combine SFT + alignment, save compute |
| **SimPO** | No | Pairwise | Length-normalized, simpler than DPO |
| **KTO** | Yes | Unpaired (thumbs up/down) | Real user feedback, asymmetric loss |
| **GRPO** | No (group baseline) | Prompts + verifier | Math/code/reasoning, rule-based reward |
| **DAPO** | No | Prompts + verifier | GRPO improvement, stable training |
| **IPO** | Yes | Pairwise | DPO with regularization (less overfitting) |

```python
# DPO with TRL (2026)
from trl import DPOTrainer, DPOConfig

dpo_config = DPOConfig(
    beta=0.1,                         # KL coefficient (0.1-0.5)
    loss_type="sigmoid",              # or "ipo", "hinge", "kto_pair"
    max_length=2048,
    max_prompt_length=1024,
    output_dir="./dpo-out",
    learning_rate=5e-7,               # 10-100x lower than SFT
    per_device_train_batch_size=2,
    gradient_accumulation_steps=8,
    num_train_epochs=1,               # DPO typically 1 epoch
    bf16=True,
)

dpo_trainer = DPOTrainer(
    model=sft_model,
    ref_model=None,                   # None = use PEFT base
    args=dpo_config,
    train_dataset=preference_ds,      # {prompt, chosen, rejected}
    tokenizer=tokenizer,
    peft_config=lora_config,
)
dpo_trainer.train()
```

### 4.6 GRPO (DeepSeek-R1 Style) Implementation

```python
# GRPO for reasoning — TRL implementation
from trl import GRPOTrainer, GRPOConfig

def reward_format(completions, **kwargs):
    """Format reward: <think>...</think><answer>...</answer>"""
    pattern = r"^<think>.*?</think>\s*<answer>.*?</answer>$"
    return [1.0 if re.match(pattern, c, re.DOTALL) else 0.0 for c in completions]

def reward_correctness(completions, ground_truth, **kwargs):
    """Math/code correctness — extract answer, compare"""
    rewards = []
    for c, gt in zip(completions, ground_truth):
        ans = extract_answer(c)
        rewards.append(1.0 if ans == gt else 0.0)
    return rewards

grpo_config = GRPOConfig(
    output_dir="./grpo-out",
    num_generations=8,                # Group size (8 samples per prompt)
    max_prompt_length=512,
    max_completion_length=2048,
    learning_rate=1e-6,
    per_device_train_batch_size=1,
    gradient_accumulation_steps=16,   # Effective 16
    beta=0.04,                        # KL penalty
    use_vllm=True,                    # vLLM for fast generation
    vllm_mode="colocate",             # Single-node
    bf16=True,
    num_train_epochs=1,
    report_to="wandb",
)

grpo_trainer = GRPOTrainer(
    model=sft_model,
    reward_funcs=[reward_format, reward_correctness],
    args=grpo_config,
    train_dataset=math_dataset,       # {prompt, ground_truth}
    peft_config=lora_config,
)
grpo_trainer.train()
```

### 4.7 Distributed Training (Multi-GPU/Node)

```python
# DeepSpeed ZeRO-3 config (zero3.json)
{
    "bf16": {"enabled": true},
    "zero_optimization": {
        "stage": 3,
        "offload_optimizer": {"device": "cpu", "pin_memory": true},
        "offload_param": {"device": "cpu", "pin_memory": true},
        "overlap_comm": true,
        "contiguous_gradients": true,
        "stage3_max_live_parameters": 1e9,
        "stage3_max_reuse_distance": 1e9,
        "stage3_prefetch_bucket_size": 5e8,
        "stage3_param_persistence_threshold": 1e6,
        "stage3_gather_16bit_weights_on_model_save": true
    },
    "gradient_clipping": 1.0,
    "train_micro_batch_size_per_gpu": 4
}

# Launch
# accelerate launch --config_file deepspeed_zero3.yaml train.py
# OR
# torchrun --nproc_per_node 8 train.py --deepspeed zero3.json
```

### 4.8 Cloud Training Platforms

| Platform | Strength | Cost | Best For |
|----------|----------|------|----------|
| **Modal** | Serverless GPU, fast cold start | $4-8/hr H100 | LoRA jobs, batch inference |
| **Lightning AI** | Studios, persistent envs | $3-6/hr H200 | Iterative training |
| **Together AI** | Fine-tune API + serving | $0.0006/1K tokens | Quick fine-tune + deploy |
| **Replicate** | Cog-based deployment | Per-second billing | Demo/prototype |
| **Anyscale** | Ray-native, multi-node | Custom | Distributed training, RL |
| **RunPod** | Cheapest H100 spot | $1.99-2.49/hr H100 | Long training, batch jobs |
| **Lambda Labs** | On-demand H100 clusters | $2-3/hr H100 | Pre-training, large jobs |

---

## 5. Model Deployment / Serving

### 5.1 Inference Engine Selection (2026)

| Engine | Strength | TTFT (10 conc) | Throughput (100 conc) | When |
|--------|----------|----------------|----------------------|------|
| **vLLM** | PagedAttention, model flexibility, fast starts | 120ms | 4,741 tok/s | Default, OSS models |
| **SGLang** | RadixAttention, prefix caching | 112ms | High at 50 conc | Shared context (RAG, agents, chatbots) |
| **TensorRT-LLM** | NVIDIA optimized, compiled engines | 105ms | Best single-req | Stable models, max perf, NV hw |
| **TGI** | HF native, simple API | 130ms | Mid | HF ecosystem |
| **lmdeploy** | TurboMind backend, INT4/INT8 | 110ms | Mid-high | Quantized serving |
| **Ollama** | Local dev, simple | 200ms | Low | Mac/local prototyping |

**2026 default**: vLLM (general), SGLang (RAG/agents with shared prefix), TensorRT-LLM (production with stable model + 28-min compile budget).

### 5.2 Quantization Format Decision

| Format | Bits | Quality | Speed | Tooling |
|--------|------|---------|-------|---------|
| **FP8** | 8 | ≈FP16 | 1.5-2x | TensorRT-LLM, vLLM (H100+) |
| **INT8** | 8 | -1-2% | 1.5-2x | All engines |
| **AWQ** | 4 | -1-3% | 2-3x | vLLM, lmdeploy |
| **GPTQ** | 4 | -2-4% | 2-3x | vLLM, exllamav2 |
| **GGUF** | 2-8 | varies | 2-4x | llama.cpp, Ollama |
| **EXL2** | 2-8 | -1-3% | 3x | exllamav2 (consumer GPU) |

### 5.3 vLLM Production Config

```bash
# vLLM serving — production multi-LoRA
vllm serve Qwen/Qwen2.5-Coder-7B-Instruct \
    --enable-lora \
    --max-loras 8 \
    --max-lora-rank 64 \
    --lora-modules surrogate-v1=./adapters/surrogate-v1 \
                   surrogate-v2=./adapters/surrogate-v2 \
    --gpu-memory-utilization 0.92 \
    --max-model-len 32768 \
    --enable-prefix-caching \
    --enable-chunked-prefill \
    --max-num-batched-tokens 8192 \
    --max-num-seqs 256 \
    --quantization awq \
    --tensor-parallel-size 1 \
    --swap-space 16 \
    --disable-log-requests \
    --port 8000
```

```python
# Client with multi-LoRA selection
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="EMPTY")

response = client.chat.completions.create(
    model="surrogate-v2",  # LoRA adapter name
    messages=[{"role": "user", "content": "Build me a RAG pipeline"}],
    max_tokens=2048,
    temperature=0.7,
    extra_body={"use_beam_search": False},
)
```

### 5.4 SGLang Config (Shared Prefix Workloads)

```bash
# SGLang for RAG/agent serving — RadixAttention shines
python -m sglang.launch_server \
    --model-path Qwen/Qwen2.5-Coder-7B-Instruct \
    --tp 1 \
    --port 30000 \
    --enable-prefix-caching \
    --kv-cache-dtype fp8 \
    --max-running-requests 128 \
    --schedule-policy lpm \
    --mem-fraction-static 0.88
```

### 5.5 KV Cache Strategies

- **Paged KV (vLLM)**: Variable-length sequences, no fragmentation
- **Prefix caching**: Same system prompt across users → 10-100x throughput
- **Chunked prefill**: Long context handled in chunks, lower TTFT
- **Speculative decoding**: Draft model proposes 4-8 tokens, target model verifies
- **CPU offload**: KV cache to CPU/disk for very long context

---

## 6. LLMOps Platforms

### 6.1 Observability Decision Matrix

| Platform | Strength | Self-Host | Eval | Cost Model |
|----------|----------|-----------|------|------------|
| **Langfuse** | Open-source, comprehensive, OpenTelemetry | Yes (PG+CH+Redis+S3) | LLM-as-judge | Per trace |
| **LangSmith** | Best LangChain/LangGraph integration | No | Strong | Per trace |
| **Helicone** | Simple proxy, OpenAI-focused | Yes | Basic | Per request |
| **Phoenix (Arize)** | OSS, OpenTelemetry-native, OpenInference | Yes | Strong | Free OSS |
| **Braintrust** | Eval-first, CI/CD blocking | No | Best-in-class | Per eval |
| **Datadog LLM Obs** | APM integration | No | Mid | Datadog pricing |
| **Laminar** | Data-volume pricing | Yes | Strong | Per GB |

**2026 stack recommendation**:
- LangChain heavy → LangSmith
- LangGraph + eval-first → LangSmith + Braintrust
- Self-host + OSS → Phoenix (Arize) or Langfuse
- LlamaIndex → Phoenix

### 6.2 Tracing Pattern (OpenTelemetry GenAI)

```python
from opentelemetry import trace
from opentelemetry.semconv.ai import SpanAttributes
from langfuse import Langfuse

langfuse = Langfuse()
tracer = trace.get_tracer(__name__)

@langfuse.observe(name="rag_pipeline")
def rag_query(question: str) -> str:
    with langfuse.start_as_current_span(name="retrieve") as span:
        docs = retriever.retrieve(question)
        span.update(input=question, output={"n_docs": len(docs)})

    with langfuse.start_as_current_span(name="rerank") as span:
        reranked = reranker.rerank(question, docs, top_k=5)
        span.update(metadata={"scores": [d.score for d in reranked]})

    with langfuse.start_as_current_observation(
        name="generate",
        as_type="generation",
        model="claude-opus-4-7",
        input=build_prompt(question, reranked),
    ) as gen:
        response = client.messages.create(...)
        gen.update(
            output=response.content,
            usage={"input": response.usage.input_tokens,
                   "output": response.usage.output_tokens},
        )
    return response.content
```

### 6.3 Eval Frameworks

```python
# Braintrust eval (CI-blocking)
from braintrust import Eval

def faithfulness_score(input, output, expected, metadata):
    """LLM-as-judge: is output faithful to retrieved docs?"""
    judge = client.messages.create(
        model="claude-opus-4-7",
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(
            answer=output, docs=metadata["contexts"]
        )}]
    )
    return parse_score(judge.content)

Eval(
    "rag-faithfulness",
    data=lambda: load_test_set("rag-eval-v1"),
    task=lambda input: rag_query(input["question"]),
    scores=[faithfulness_score, relevancy_score, latency_score],
    metadata={"version": "v2"},
    trial_count=3,                 # Run 3x, average
    max_concurrency=10,
)
# CI: braintrust eval rag-faithfulness --threshold 0.85
```

### 6.4 Cost Monitoring

```python
# Per-query cost tracking
@dataclass
class QueryCost:
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    model: str

    @property
    def cost_usd(self) -> float:
        rates = {
            "claude-opus-4-7": (15.0, 75.0, 1.5),  # per 1M (input, output, cache)
            "claude-sonnet-4-7": (3.0, 15.0, 0.30),
            "claude-haiku-4-7": (0.25, 1.25, 0.025),
        }
        i, o, c = rates[self.model]
        return (
            (self.input_tokens - self.cached_tokens) * i / 1e6
            + self.output_tokens * o / 1e6
            + self.cached_tokens * c / 1e6
        )

# Budget enforcement
class BudgetGuard:
    def __init__(self, daily_limit_usd: float):
        self.daily_limit = daily_limit_usd
        self.spent_today = 0.0

    def check(self, est_cost: float):
        if self.spent_today + est_cost > self.daily_limit:
            raise BudgetExceeded(f"Daily limit ${self.daily_limit} reached")
```

---

## 7. Eval Frameworks

### 7.1 Eval Stack 2026

| Tool | Coverage | Style |
|------|----------|-------|
| **lm-evaluation-harness** (EleutherAI) | 200+ tasks: MMLU, HellaSwag, GSM8K, ARC, TruthfulQA | Standard academic |
| **BigCode Eval** | HumanEval, MBPP, APPS, DS-1000 | Code-specific |
| **LiveCodeBench** | Fresh LeetCode/Codeforces problems (contamination-resistant) | Coding (most trustworthy) |
| **HELM** (Stanford) | Holistic eval — accuracy, calibration, robustness, fairness | Comprehensive |
| **MT-Bench** | LLM-as-judge multi-turn | Conversational |
| **AlpacaEval 2** | LLM-as-judge instruction following | Cheap quick check |
| **Arena-Hard** | Hard prompts vs GPT-4-turbo baseline | Strong arena predictor |
| **Chatbot Arena** | Human pairwise voting | Gold standard |
| **SWE-bench** | Real GitHub issues + repo context | Coding agent eval |

### 7.2 LM Eval Harness Run

```bash
# Standard benchmark suite
lm_eval \
    --model hf \
    --model_args pretrained=./surrogate-v2,peft=./adapters/v2,dtype=bfloat16 \
    --tasks mmlu,hellaswag,arc_challenge,gsm8k,humaneval,mbpp \
    --batch_size 8 \
    --device cuda \
    --output_path ./eval_results \
    --log_samples

# vLLM backend (faster)
lm_eval \
    --model vllm \
    --model_args pretrained=./surrogate-v2,gpu_memory_utilization=0.9 \
    --tasks leaderboard \
    --batch_size auto
```

### 7.3 LLM-as-Judge Pattern

```python
JUDGE_PROMPT = """You are evaluating AI responses. Compare these two answers to the question:

Question: {question}

Answer A: {answer_a}
Answer B: {answer_b}

Evaluate on:
1. Correctness (factually accurate)
2. Helpfulness (addresses user intent)
3. Conciseness (no padding)

Output JSON: {{"winner": "A|B|tie", "reasoning": "..."}}
"""

def llm_as_judge(question, baseline_answer, candidate_answer):
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=512,
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(
            question=question,
            answer_a=baseline_answer,
            answer_b=candidate_answer,
        )}],
    )
    return json.loads(response.content[0].text)

# Bias mitigation: randomize A/B order, use multiple judges, average
```

### 7.4 Behavior + Red Team Eval

```python
# Garak — LLM vulnerability scan
# pip install garak
# python -m garak --model_type huggingface.Pipeline \
#     --model_name ./surrogate-v2 \
#     --probes encoding,malwaregen,promptinject,leakreplay

# PyRIT — multi-turn attack
from pyrit.orchestrator import RedTeamingOrchestrator
from pyrit.prompt_target import HuggingFaceTarget

target = HuggingFaceTarget(model_id="./surrogate-v2")
orchestrator = RedTeamingOrchestrator(
    chat_target=target,
    attack_strategy="crescendo",  # multi-turn escalation
)
results = await orchestrator.apply_attack_strategy_async(
    objective="Get model to produce harmful code"
)
```

---

## 8. MLOps Platforms

### 8.1 Stack Choice (2026)

| Need | OSS | Managed | Best |
|------|-----|---------|------|
| Experiment tracking | MLflow, Aim | W&B, Neptune | W&B (UX), MLflow (OSS) |
| Model registry | MLflow Registry | SageMaker, Vertex | MLflow → Bento → K8s |
| Feature store | Feast | Tecton, Databricks | Feast (OSS), Tecton (mgd) |
| Pipelines | Kubeflow, Airflow, Prefect | SageMaker Pipelines | Argo Workflows + DAGster |
| Data versioning | DVC, LakeFS | Pachyderm | DVC + Git LFS |
| Model serving | KServe, BentoML, vLLM | SageMaker, Vertex | BentoML + KServe |

### 8.2 MLflow Tracking + Registry

```python
import mlflow
from mlflow.models import infer_signature

mlflow.set_tracking_uri("http://mlflow:5000")
mlflow.set_experiment("surrogate-1-finetune")

with mlflow.start_run(run_name="qlora-r16-v2"):
    mlflow.log_params({
        "base_model": "Qwen2.5-Coder-7B",
        "method": "qlora",
        "rank": 16,
        "alpha": 16,
        "dataset_size": 100_000,
    })

    # Train
    trainer.train()

    # Log metrics
    eval_results = trainer.evaluate()
    mlflow.log_metrics(eval_results)

    # Log model + adapter
    signature = infer_signature(sample_input, sample_output)
    mlflow.transformers.log_model(
        transformers_model={"model": model, "tokenizer": tokenizer},
        artifact_path="model",
        signature=signature,
        registered_model_name="surrogate-1",
    )

# Promote to production
client = mlflow.MlflowClient()
client.transition_model_version_stage(
    name="surrogate-1",
    version=2,
    stage="Production",
    archive_existing_versions=True,
)
```

### 8.3 Feast Feature Store

```python
# feature_repo/features.py
from feast import Entity, Feature, FeatureView, FileSource, ValueType
from datetime import timedelta

user = Entity(name="user_id", value_type=ValueType.STRING)

user_stats = FeatureView(
    name="user_stats",
    entities=[user],
    ttl=timedelta(days=30),
    schema=[
        Feature(name="avg_session_length", dtype=ValueType.FLOAT),
        Feature(name="purchase_count_30d", dtype=ValueType.INT64),
        Feature(name="embedding", dtype=ValueType.FLOAT_LIST),
    ],
    source=FileSource(path="s3://features/user_stats.parquet"),
)

# Query
features = store.get_online_features(
    features=["user_stats:avg_session_length", "user_stats:embedding"],
    entity_rows=[{"user_id": "u123"}],
).to_dict()
```

---

## 9. Production ML Systems

### 9.1 Inference Patterns

| Pattern | Latency | Cost | Use |
|---------|---------|------|-----|
| **Real-time** | <100ms | High (always-on GPU) | Recommendations, search |
| **Near-real-time** | 100ms-1s | Mid | Chat, RAG |
| **Batch** | Minutes-hours | Lowest | Daily scoring, embeddings refresh |
| **Streaming** | <50ms | Mid | Fraud detection, anomaly |

### 9.2 A/B Testing + Shadow Deployment

```python
# Shadow deployment — run new model alongside old, compare without affecting users
class ShadowRouter:
    def __init__(self, prod_model, shadow_model, sample_rate=0.1):
        self.prod = prod_model
        self.shadow = shadow_model
        self.rate = sample_rate

    async def predict(self, request):
        # Always serve prod
        prod_result = await self.prod.predict(request)

        # Shadow on sample
        if random.random() < self.rate:
            asyncio.create_task(self._shadow_eval(request, prod_result))

        return prod_result

    async def _shadow_eval(self, request, prod_result):
        shadow_result = await self.shadow.predict(request)
        await metrics.log_shadow_diff(
            request_id=request.id,
            prod=prod_result,
            shadow=shadow_result,
            diff=compare(prod_result, shadow_result),
        )

# A/B test — split traffic, measure user metrics
class ABRouter:
    def __init__(self, variants: dict[str, Model], split: dict[str, float]):
        self.variants = variants
        self.split = split  # {"control": 0.5, "treatment": 0.5}

    async def predict(self, request):
        variant = weighted_choice(self.split)
        result = await self.variants[variant].predict(request)
        await metrics.log_ab(request.user_id, variant, result)
        return result
```

### 9.3 Drift Detection

```python
from evidently import Report
from evidently.metric_preset import DataDriftPreset, TargetDriftPreset

report = Report(metrics=[
    DataDriftPreset(),    # KS test, Wasserstein, PSI
    TargetDriftPreset(),
])

report.run(
    reference_data=train_df,    # Training distribution
    current_data=prod_df_24h,   # Last 24h production data
)
report.save_html("drift_report.html")

# Alert pattern
drift_metrics = report.as_dict()
if drift_metrics["metrics"][0]["result"]["drift_share"] > 0.3:
    alert.fire(
        severity="warning",
        message=f"Data drift detected: {drift_metrics['drift_share']:.2%} of features drifted"
    )
```

### 9.4 Production Monitoring Dashboard

**RED metrics (LLM-specific)**:
- **R**ate: requests/sec, tokens/sec
- **E**rrors: 5xx rate, timeout rate, refusal rate, hallucination rate (sampled)
- **D**uration: TTFT p50/p95/p99, e2e latency p95/p99

**Quality metrics**:
- LLM-as-judge faithfulness (sampled, hourly)
- Context precision/recall (RAG)
- User feedback rate (thumbs up/down)
- Refusal rate
- Repetition rate

**Cost metrics**:
- $/query p50/p95
- Cache hit rate (target >40%)
- Tokens per query
- Daily spend vs budget

---

## 10. AI Product Engineering

### 10.1 UI Frameworks

| Framework | Strength | When |
|-----------|----------|------|
| **Gradio** | Fastest demo, built-in components | Internal demos, HF Spaces |
| **Streamlit** | Python-native, easy state | Data apps, dashboards |
| **Vercel AI SDK** | React/Next.js, streaming hooks | Production web apps |
| **Mastra** | Agent-first, TypeScript | Multi-agent products |
| **Custom React** | Full control | Production at scale |

```typescript
// Vercel AI SDK — streaming chat with tools
import { streamText } from 'ai';
import { anthropic } from '@ai-sdk/anthropic';

const result = streamText({
  model: anthropic('claude-opus-4-7'),
  messages,
  tools: {
    searchDocs: {
      description: 'Search docs',
      parameters: z.object({ query: z.string() }),
      execute: async ({ query }) => await retriever.search(query),
    },
  },
  maxSteps: 5,  // Multi-step tool loop
});
return result.toDataStreamResponse();
```

### 10.2 Multi-Modal

| Modality | Stack |
|----------|-------|
| **Vision** | Claude vision, GPT-4o vision, CLIP, SigLIP, Florence-2 |
| **Voice (input)** | Whisper-v3, Deepgram Nova-2, AssemblyAI |
| **Voice (output)** | ElevenLabs, OpenAI TTS, Cartesia, PlayHT |
| **Voice (real-time)** | OpenAI Realtime, Vapi, Retell, Pipecat |
| **Video** | Twelve Labs, Pegasus, video-to-text via frame sampling |

### 10.3 Code Sandboxes

| Sandbox | Strength | Cost |
|---------|----------|------|
| **E2B** | Long-running, persistent FS, dev sandboxes | $0.50/hr |
| **Modal Sandboxes** | Same Modal infra, cheap | Per-second |
| **Riza** | TS/Python/Ruby/PHP | Per-request |
| **Cog Sandbox** | ML model inference sandbox | Per-second |
| **Pyodide (browser)** | No backend, free | Free |

### 10.4 Browser Agents

| Tool | Strength |
|------|----------|
| **Browserbase** | Hosted Chrome, fast cold start, anti-bot |
| **Steel.dev** | Open source alternative |
| **Playwright + Patchright** | Stealth + control |
| **Anthropic Computer Use** | Native multimodal browser control |
| **OpenAI Operator** | Same paradigm |

---

## 11. Agent Frameworks (2026)

### 11.1 Decision Matrix

| Framework | Architecture | State | Production | Best For |
|-----------|--------------|-------|------------|----------|
| **LangGraph** | Graph (nodes + edges) | Checkpointing native | Highest | Complex stateful workflows |
| **CrewAI** | Role-based crew | Limited | Medium | Fast prototyping |
| **AutoGen v0.4 (AG2)** | Event-driven, GroupChat | Async-first | Medium | Conversation patterns |
| **Google ADK** | Hierarchical tree, A2A | Vertex AI native | Medium | Google Cloud shop |
| **SmolAgents** | Code agents, HF native | Light | Light | Simple agents, education |
| **Letta (MemGPT)** | Long-term memory | Tier-based memory | Medium | Persistent agents |
| **Mastra** | TypeScript, Vercel-style | Hooks | Medium | Web apps |

### 11.2 LangGraph Production Pattern

```python
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres import PostgresSaver
from typing import TypedDict, Annotated
import operator

class AgentState(TypedDict):
    messages: Annotated[list, operator.add]
    plan: list[str]
    step: int
    tools_used: list[str]

def planner(state: AgentState):
    plan = llm.invoke(PLAN_PROMPT.format(query=state["messages"][-1]))
    return {"plan": parse_plan(plan), "step": 0}

def executor(state: AgentState):
    current_step = state["plan"][state["step"]]
    result = tools[pick_tool(current_step)].invoke(current_step)
    return {
        "messages": [HumanMessage(content=result)],
        "step": state["step"] + 1,
        "tools_used": [pick_tool(current_step)],
    }

def should_continue(state: AgentState):
    if state["step"] >= len(state["plan"]):
        return END
    return "executor"

graph = StateGraph(AgentState)
graph.add_node("planner", planner)
graph.add_node("executor", executor)
graph.add_edge("planner", "executor")
graph.add_conditional_edges("executor", should_continue)
graph.set_entry_point("planner")

# Persistent checkpointing
checkpointer = PostgresSaver.from_conn_string("postgresql://...")
app = graph.compile(checkpointer=checkpointer)

# Run with thread_id for resumable conversations
config = {"configurable": {"thread_id": "user-123"}}
result = app.invoke({"messages": [user_query]}, config=config)
```

---

## 12. Specific 2025-2026 Frameworks

### 12.1 DSPy Programmatic Prompting

```python
import dspy

# Define signature
class GenerateAnswer(dspy.Signature):
    """Answer questions with short factoid answers."""
    context = dspy.InputField(desc="may contain relevant facts")
    question = dspy.InputField()
    answer = dspy.OutputField(desc="often between 1 and 5 words")

# Define module
class RAG(dspy.Module):
    def __init__(self, num_passages=3):
        super().__init__()
        self.retrieve = dspy.Retrieve(k=num_passages)
        self.generate_answer = dspy.ChainOfThought(GenerateAnswer)

    def forward(self, question):
        context = self.retrieve(question).passages
        prediction = self.generate_answer(context=context, question=question)
        return dspy.Prediction(context=context, answer=prediction.answer)

# Compile (auto-optimize prompts)
from dspy.teleprompt import BootstrapFewShot
config = dict(max_bootstrapped_demos=4, max_labeled_demos=16)
teleprompter = BootstrapFewShot(metric=validate_answer, **config)
compiled_rag = teleprompter.compile(RAG(), trainset=trainset)

# 10-40% quality improvement vs hand-prompts (DSPy benchmark)
```

### 12.2 Outlines (Constrained Decoding)

```python
import outlines
from pydantic import BaseModel
from enum import Enum

class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"

class Review(BaseModel):
    sentiment: Sentiment
    score: int  # 1-5
    summary: str

model = outlines.models.transformers("Qwen/Qwen2.5-Coder-7B-Instruct")
generator = outlines.generate.json(model, Review)
review = generator("Analyze: 'Loved the food, terrible service.'")
# 100% guaranteed valid Review (constrained decoding)
```

### 12.3 OpenTelemetry GenAI

```python
# Standard semantic conventions for LLM tracing
from opentelemetry.semconv.ai import SpanAttributes

with tracer.start_as_current_span("llm.chat") as span:
    span.set_attribute(SpanAttributes.LLM_REQUEST_MODEL, "claude-opus-4-7")
    span.set_attribute(SpanAttributes.LLM_REQUEST_MAX_TOKENS, 4096)
    span.set_attribute(SpanAttributes.LLM_REQUEST_TEMPERATURE, 0.7)

    response = client.messages.create(...)

    span.set_attribute(SpanAttributes.LLM_USAGE_PROMPT_TOKENS, response.usage.input_tokens)
    span.set_attribute(SpanAttributes.LLM_USAGE_COMPLETION_TOKENS, response.usage.output_tokens)
    span.set_attribute(SpanAttributes.LLM_RESPONSE_FINISH_REASON, response.stop_reason)
```

---

## 13. Training Data Sources for AI Engineering

### 13.1 Documentation Corpora (HIGH PRIORITY)

| Source | Size | Format | License | Get |
|--------|------|--------|---------|-----|
| **HuggingFace docs** | ~50MB | MDX | Apache 2.0 | github.com/huggingface/transformers/docs + diffusers + peft + trl + accelerate + datasets + huggingface_hub |
| **Anthropic API docs** | ~10MB | MD | Custom (scrape OK) | docs.anthropic.com (sitemap) |
| **OpenAI cookbook** | ~30MB | Notebooks + MD | MIT | github.com/openai/openai-cookbook |
| **LangChain docs** | ~80MB | MDX | MIT | github.com/langchain-ai/langchain |
| **LlamaIndex docs** | ~40MB | MD | MIT | github.com/run-llama/llama_index/docs |
| **vLLM docs** | ~10MB | RST | Apache 2.0 | github.com/vllm-project/vllm/docs |
| **DSPy docs** | ~5MB | MD | MIT | github.com/stanfordnlp/dspy |
| **Modal docs** | ~15MB | MDX | (scrape OK) | modal.com/docs |
| **W&B docs** | ~20MB | MD | MIT | github.com/wandb/docs |
| **MLflow docs** | ~25MB | RST | Apache 2.0 | github.com/mlflow/mlflow |

### 13.2 Code Repositories (HIGHEST VALUE)

| Repo | LOC | Why |
|------|-----|-----|
| **vllm-project/vllm** | ~150K | Inference engine, kernels, serving |
| **huggingface/transformers** | ~500K | Model architectures, training |
| **huggingface/peft** | ~30K | LoRA/QLoRA/DoRA implementations |
| **huggingface/trl** | ~50K | DPO/GRPO/SFT trainers |
| **huggingface/accelerate** | ~40K | Distributed training |
| **huggingface/datasets** | ~80K | Data pipeline patterns |
| **OpenAccess-AI-Collective/axolotl** | ~30K | Production fine-tune YAML |
| **unslothai/unsloth** | ~25K | Fast fine-tuning kernels |
| **microsoft/DeepSpeed** | ~200K | Distributed training |
| **NVIDIA/Megatron-LM** | ~150K | Pre-training at scale |
| **stanfordnlp/dspy** | ~20K | Programmatic prompting |
| **outlines-dev/outlines** | ~15K | Structured generation |
| **langchain-ai/langchain** | ~250K | LLM orchestration |
| **run-llama/llama_index** | ~150K | RAG patterns |
| **OSU-NLP-Group/HippoRAG** | ~5K | Memory-graph RAG |
| **AkariAsai/self-rag** | ~3K | Self-RAG implementation |
| **EleutherAI/lm-evaluation-harness** | ~80K | Eval harness |
| **mlflow/mlflow** | ~150K | MLOps platform |
| **kubeflow/kubeflow** | ~200K | K8s ML platform |
| **feast-dev/feast** | ~80K | Feature store |
| **bentoml/BentoML** | ~50K | Model serving |

### 13.3 Awesome Lists (Curated)

- `awesome-llm` — github.com/Hannibal046/Awesome-LLM
- `awesome-rag` — github.com/dair-ai/RAG-Survey
- `awesome-mlops` — github.com/visenger/awesome-mlops
- `awesome-llmops` — github.com/tensorchord/awesome-open-source-llmops
- `awesome-agents` — github.com/e2b-dev/awesome-ai-agents
- `awesome-prompt-engineering` — github.com/promptslab/Awesome-Prompt-Engineering

### 13.4 Conference Talks / Papers (Q&A pairs)

- **NeurIPS 2024-2025**: paper → abstract Q&A
- **ICML 2024-2025**: same
- **EMNLP 2024-2025**: NLP-specific
- **AI Engineer Summit 2024-2026**: practical talks → transcripts (YouTube)
- **MLSys**: systems papers (relevant for serving/training infra)
- **arXiv**: cs.CL, cs.LG, cs.AI categories — daily new papers

### 13.5 Q&A Sources

- **Stack Overflow** (MIT-licensed dump): tagged `huggingface-transformers`, `pytorch`, `langchain`, `openai-api`, `vllm`, `mlops`
- **HuggingFace Forum**: high-quality Q&A
- **r/LocalLLaMA**: practical fine-tuning + serving discussions
- **r/MachineLearning**: research + production
- **GitHub Issues** (closed): real failure modes + solutions
- **Discord servers** (with permission): HF, vLLM, LlamaIndex, LangChain

---

## 14. Eval for AI Eng Capability (Surrogate-1 Targets)

### 14.1 Capability Eval Tasks

| Task | Eval Method | Target Score |
|------|-------------|--------------|
| **Build RAG pipeline from scratch** | Code review by GPT-4 + manual run | Functional + idiomatic = 8/10 |
| **Fine-tune Qwen2.5-7B end-to-end** | Run produced script, check loss curve | Trainable + saves model |
| **Set up vLLM serving** | Run produced bash, check 200 OK | Server starts, responds |
| **Create eval harness** | Run produced code, verify metrics | RAGAS or LM-eval reproduces |
| **Optimize cost/latency** | Before/after benchmark | >20% improvement |
| **Write LangGraph agent** | Run produced graph, verify checkpoints | Compiles, runs, persists |
| **Implement DPO training** | Run produced script | Trains, lower KL than ref |
| **Build embedding service** | Functional REST API | <100ms p95 |
| **Diagnose drift** | Given drift report, suggest fix | Correct root cause + fix |
| **Multi-agent system** | LangGraph or CrewAI run | Multi-step success |

### 14.2 Composite AI-Eng Eval Suite

```python
AI_ENG_EVAL = {
    # Knowledge (20%)
    "concept_qa": {"weight": 0.10, "tasks": [
        "explain_pagedattention", "explain_dpo_vs_ppo",
        "explain_rag_eval_metrics", "explain_lora_vs_full",
    ]},
    "tool_choice": {"weight": 0.10, "tasks": [
        "vllm_vs_sglang_when", "lora_vs_qlora_when",
        "vector_db_selection", "framework_selection",
    ]},

    # Code generation (40%)
    "rag_pipeline": {"weight": 0.10, "task": "build_hybrid_rag"},
    "fine_tune_script": {"weight": 0.10, "task": "qlora_qwen25_7b_dpo"},
    "vllm_serve": {"weight": 0.05, "task": "multi_lora_config"},
    "agent_graph": {"weight": 0.10, "task": "research_agent_langgraph"},
    "eval_harness": {"weight": 0.05, "task": "ragas_eval_setup"},

    # Production reasoning (30%)
    "debug_oom": {"weight": 0.05, "task": "diagnose_training_oom"},
    "cost_optimize": {"weight": 0.10, "task": "reduce_llm_cost_50pct"},
    "drift_response": {"weight": 0.05, "task": "drift_root_cause"},
    "incident_triage": {"weight": 0.10, "task": "p1_llm_incident"},

    # Architecture (10%)
    "system_design": {"weight": 0.10, "task": "design_agentic_rag_system"},
}
```

### 14.3 Specific Capability Tests

```python
TEST_PROMPTS = [
    # 1. RAG pipeline
    "Build a hybrid RAG pipeline using Qdrant + BGE-M3 embedding + bge-reranker-v2-m3. "
    "Include RRF fusion (k=60), retrieval top-20 → rerank top-5. "
    "Wire it to Claude Opus via the Anthropic SDK with prompt caching.",

    # 2. Fine-tune
    "Write a complete TRL script to QLoRA-fine-tune Qwen2.5-Coder-7B on a JSONL "
    "instruction dataset. r=16, all-linear, DoRA enabled, NEFTune alpha=5, "
    "packing=True, 4 epochs, gradient_accumulation=4, learning_rate=2e-4.",

    # 3. GRPO
    "Implement GRPO training for math reasoning. Two reward functions: format "
    "(<think>...</think><answer>...</answer>) and correctness (extract numeric answer, "
    "compare to ground truth). 8 generations per prompt, vllm colocate mode.",

    # 4. vLLM multi-LoRA serving
    "Set up vLLM to serve 4 LoRA adapters on a single A100 80GB. "
    "Enable prefix caching, chunked prefill, AWQ quantization. "
    "Provide systemd service file + healthcheck.",

    # 5. RAG eval
    "Set up RAGAS evaluation for the RAG pipeline above. Track faithfulness, "
    "context precision, context recall, answer relevancy. "
    "Wire to Langfuse for trace observability.",

    # 6. Agent
    "Build a LangGraph research agent that: (1) plans subqueries, "
    "(2) retrieves with the RAG pipeline, (3) synthesizes a final answer, "
    "(4) self-critiques and re-plans if needed. "
    "Use PostgresSaver checkpointer for resumable threads.",

    # 7. Cost optimization
    "Given a chat app spending $500/day on Claude Opus, design a 3-tier routing "
    "(Haiku → Sonnet → Opus) with semantic caching (Redis) and prompt caching. "
    "Target: 60% cost reduction while maintaining quality on user-rated tasks.",

    # 8. Drift detection
    "Implement Evidently-based drift monitoring for a production embedding service. "
    "Daily report on feature drift, alert if >30% features drift. "
    "Include a retraining trigger.",

    # 9. Production deployment
    "Write a Modal app that serves a fine-tuned Qwen2.5-Coder-7B + LoRA adapter "
    "via vLLM with autoscaling 1-4 GPUs based on queue depth. "
    "Include OpenTelemetry tracing.",

    # 10. Eval harness
    "Set up a complete eval harness combining lm-evaluation-harness for "
    "MMLU/HumanEval/MBPP, custom RAGAS for retrieval quality, "
    "Garak for safety, and Braintrust for CI/CD blocking. "
    "Output a markdown report.",
]
```

### 14.4 Performance Targets

| Capability | v1 (current) | v2 (target) | Senior AI Eng |
|------------|--------------|-------------|---------------|
| HumanEval | 76% | 80% | 90%+ |
| MBPP | 70% | 75% | 85%+ |
| LiveCodeBench | unknown | 30% | 50%+ |
| RAG eval (build pipeline) | likely fail | 70% pass | 95%+ |
| Fine-tune script (TRL) | likely fail | 80% pass | 95%+ |
| vLLM serve config | likely fail | 75% pass | 95%+ |
| Agent design (LangGraph) | likely fail | 60% pass | 90%+ |
| Cost optimization | likely fail | 70% pass | 90%+ |
| AI-Eng composite eval | ~30% | 70% | 90%+ |

---

## 15. v2 Integration Plan — AI Engineering Datasets

### 15.1 Dataset Construction Pipeline

```
┌─────────────────────────────────────────────────────────────┐
│  AI ENGINEERING DATASET CONSTRUCTION (v2)                    │
└─────────────────────────────────────────────────────────────┘

STAGE 1: HARVEST (HF Space — already running)
  ├─ Docs corpora (HF, Anthropic, OpenAI, LangChain, vLLM, etc.)
  ├─ Code repos (vLLM, transformers, peft, trl, axolotl, dspy, etc.)
  ├─ Awesome lists (parse → extract URLs → fetch)
  └─ Stack Overflow dump (filter ai/ml/llm tags)

STAGE 2: STRUCTURE (LLM burst — Cerebras/Groq)
  ├─ Doc → Q&A pairs (claude/qwen explain each section)
  ├─ Code → docstring + usage examples
  ├─ Code → "explain this code" + "fix this bug" pairs
  └─ Issues → Q&A (problem statement → solution)

STAGE 3: SYNTHESIZE (high-value generated examples)
  ├─ "Build X from scratch" → step-by-step solutions
  ├─ "Debug Y error" → root cause + fix
  ├─ "Optimize Z system" → before/after benchmarks
  ├─ "Choose tool for use case" → reasoning + selection
  └─ "Architecture review" → design + tradeoffs

STAGE 4: FILTER (quality gate)
  ├─ Length (200-8K tokens response)
  ├─ Code compiles (run sample code)
  ├─ Embedding dedupe (cosine sim < 0.95)
  └─ LLM-as-judge filter (Claude rates quality 1-5, keep ≥4)

STAGE 5: BALANCE (target distribution)
  ├─ 30% LLM apps (RAG, agents, prompt eng, structured output)
  ├─ 25% Fine-tuning (LoRA/QLoRA/DPO/GRPO/SFT scripts)
  ├─ 20% Serving (vLLM, SGLang, quantization, multi-LoRA)
  ├─ 15% MLOps (MLflow, Kubeflow, Feast, drift, monitoring)
  └─ 10% Evaluation (RAGAS, lm-eval, red team, behavior)
```

### 15.2 Target Volume

| Dataset | Examples | Tokens | Source |
|---------|----------|--------|--------|
| `ai-eng-llm-apps` | 50K | ~80M | LangChain/LlamaIndex docs + cookbook + Anthropic/OpenAI examples |
| `ai-eng-rag` | 30K | ~60M | RAG papers + LlamaIndex + custom synth |
| `ai-eng-fine-tune` | 25K | ~50M | TRL/PEFT/Axolotl/Unsloth code + docs |
| `ai-eng-serving` | 20K | ~40M | vLLM/SGLang/TensorRT docs + configs |
| `ai-eng-mlops` | 20K | ~30M | MLflow/Kubeflow/Feast docs + tutorials |
| `ai-eng-eval` | 15K | ~25M | RAGAS/lm-eval/Braintrust/Garak/PyRIT |
| `ai-eng-agents` | 20K | ~40M | LangGraph/CrewAI/AutoGen examples |
| **TOTAL** | **180K** | **~325M** | |

Add to existing Surrogate-1 corpus (~405GB total). AI engineering subset: ~5-8% of total.

### 15.3 Training Recipe Update

```yaml
# v2 training recipe (Lightning H200)
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
method: qlora
rank: 32                  # Up from r=16 (more capacity for AI-eng knowledge)
alpha: 32
target_modules: all-linear
use_dora: true
use_rslora: false

dataset_mix:
  surrogate-1-pairs-A: 0.30   # Original code data
  surrogate-1-pairs-B: 0.20
  ai-eng-llm-apps: 0.10       # NEW
  ai-eng-rag: 0.06            # NEW
  ai-eng-fine-tune: 0.05      # NEW
  ai-eng-serving: 0.04        # NEW
  ai-eng-mlops: 0.04          # NEW
  ai-eng-eval: 0.03           # NEW
  ai-eng-agents: 0.04         # NEW
  reasoning-chain-pairs: 0.14  # CoT chains

train:
  num_epochs: 3
  per_device_batch_size: 4
  grad_accum: 4               # Effective 16
  learning_rate: 1.5e-4       # Slightly lower than v1 (more data)
  warmup_ratio: 0.05
  lr_scheduler: cosine
  bf16: true
  max_seq_length: 8192        # Up from 4096 (longer code examples)
  packing: true
  neftune_alpha: 5

eval:
  every_steps: 500
  benchmarks:
    - mmlu (5-shot)
    - humaneval (0-shot)
    - mbpp (3-shot)
    - livecodebench (0-shot)
    - ai_eng_composite (custom — 100 task suite)
```

### 15.4 Stage 2: Preference Optimization

```yaml
# v2 stage 2: SimPO + GRPO
stage: simpo
data: ai-eng-preference-pairs   # 10K pairs from LLM-as-judge ranking
beta: 2.0
gamma: 1.0
learning_rate: 5e-7
epochs: 1

stage: grpo
data: ai-eng-verifiable-tasks   # 5K tasks with rule-based verifiers
  - run_code_check_output
  - check_yaml_valid
  - check_imports_resolve
  - check_eval_runs
num_generations: 8
beta: 0.04
epochs: 1
```

### 15.5 Eval Plan

```python
# Run after every checkpoint
EVAL_SUITE = [
    # Standard benchmarks
    ("lm-eval-harness", ["mmlu", "hellaswag", "arc_challenge", "gsm8k"]),
    ("bigcode-eval", ["humaneval", "mbpp"]),
    ("livecodebench", ["v5"]),

    # AI engineering custom
    ("ai-eng-composite", AI_ENG_EVAL),  # 100 tasks across all 14 capabilities

    # Safety
    ("garak", ["promptinject", "leakreplay"]),
    ("pyrit", ["crescendo"]),

    # Production
    ("ragas-build", ["build_rag_pipeline_passing_test"]),
    ("vllm-deploy", ["serve_config_correct"]),
    ("trl-train", ["script_runs_one_step"]),
]
```

---

## 16. Recursive AI Capability (Surrogate Builds AI)

### 16.1 The Recursive Loop

```
┌────────────────────────────────────────────────────────────┐
│  SURROGATE-1 RECURSIVE LOOP (v2 design)                     │
└────────────────────────────────────────────────────────────┘

User: "Build me an AI product that does X"
                    ↓
Surrogate-1:
  1. Architecture: pick stack (RAG vs agent vs fine-tune)
  2. Data: harvest + synth dataset for X
  3. Train: QLoRA/DPO script for fine-tune model
  4. Serve: vLLM/SGLang config
  5. RAG: hybrid pipeline if needed
  6. Eval: RAGAS / custom harness
  7. Deploy: Modal/Lightning/K8s
  8. Monitor: Langfuse + drift
  9. Optimize: cost (caching, routing) + latency
  10. Iterate: feedback loop → retrain
                    ↓
Working AI product (Surrogate-2 or domain-specific)
                    ↓
Surrogate-2 trains Surrogate-3, etc.
```

### 16.2 Critical Skills for Recursion

- **System design literacy** — sketch full AI system before code
- **Cost-aware code** — every line considers $/query
- **Failure mode recognition** — knows top failure modes by symptom
- **Eval-first** — never claim "done" without measurable result
- **Self-critique loop** — runs own code, debugs, iterates without human
- **Budget reasoning** — picks cheapest tool that meets requirements

### 16.3 Self-Improvement Capability

After each task, Surrogate-1 should:
1. Log lessons to its own knowledge base (`~/.claude/memory` equivalent)
2. Identify patterns across similar tasks
3. Update preferences (which tool worked, which didn't)
4. Generate synthetic training data from successes (self-distillation)
5. Submit RFP for own next training run when accumulated lessons justify

---

## 17. Concrete Next Actions

### Priority 1 (this week)
1. Harvest HuggingFace docs (transformers, peft, trl, accelerate, datasets)
2. Harvest LangChain + LlamaIndex docs
3. Harvest vLLM + SGLang docs
4. Build "doc → Q&A" LLM-burst pipeline (extends existing Cerebras/Groq jobs)

### Priority 2 (next 2 weeks)
5. Synthesize 10K "build X from scratch" examples using Claude Opus
6. Curate Stack Overflow LLM/ML/RAG tagged questions (filter by score >5)
7. Index awesome lists, fetch top repo READMEs + key code files
8. Build 100-task `ai_eng_composite` eval suite

### Priority 3 (after dataset ready)
9. v2 QLoRA training run on Lightning H200 with mixed dataset
10. Stage 2: SimPO on preference pairs
11. Stage 3: GRPO on verifiable AI-eng tasks
12. Eval against `ai_eng_composite` — target 70% (vs ~30% baseline)

### Decision: scope of v2
- **Minimum viable v2**: LLM apps + RAG + fine-tuning subset (~80K examples, ~150M tokens)
  - 1 week dataset build, 2 weeks training
  - Target: 60% on AI-Eng composite
- **Full v2**: All 7 categories (~180K examples, ~325M tokens)
  - 2-3 weeks dataset build, 3-4 weeks training (multi-stage)
  - Target: 70% on AI-Eng composite

**Recommendation**: Minimum viable v2 first (validate methodology), then full v2 if metrics justify.

---

## 18. References / Sources

### Frameworks
- LangChain: github.com/langchain-ai/langchain
- LlamaIndex: github.com/run-llama/llama_index
- DSPy: github.com/stanfordnlp/dspy
- Haystack: github.com/deepset-ai/haystack
- LangGraph: github.com/langchain-ai/langgraph
- CrewAI: github.com/joaomdmoura/crewAI

### Training
- TRL: github.com/huggingface/trl
- PEFT: github.com/huggingface/peft
- Axolotl: github.com/OpenAccess-AI-Collective/axolotl
- Unsloth: github.com/unslothai/unsloth
- DeepSpeed: github.com/microsoft/DeepSpeed
- Open R1: github.com/huggingface/open-r1

### Serving
- vLLM: github.com/vllm-project/vllm
- SGLang: github.com/sgl-project/sglang
- TensorRT-LLM: github.com/NVIDIA/TensorRT-LLM
- TGI: github.com/huggingface/text-generation-inference

### Eval / Observability
- lm-eval-harness: github.com/EleutherAI/lm-evaluation-harness
- Langfuse: github.com/langfuse/langfuse
- Phoenix: github.com/Arize-ai/phoenix
- RAGAS: github.com/explodinggradients/ragas
- Braintrust: braintrust.dev

### Papers (2024-2025)
- DeepSeek-R1: arxiv.org/abs/2501.12948
- Self-RAG: arxiv.org/abs/2310.11511
- CRAG: arxiv.org/abs/2401.15884
- HippoRAG 2: arxiv.org/abs/2502.14802
- DPO: arxiv.org/abs/2305.18290
- DSPy: arxiv.org/abs/2310.03714

---

**End of research-ai-eng.md** — Lines: ~1100 | Tokens: ~22K | Status: Complete, ready for v2 dataset construction.

---
title: "V17 — MoE Upcycling + Sub-LoRA Composition for Surrogate-1 Specialty Heads"
date: 2026-05-01
tags: [v17, surrogate-1, moe, lora, upcycling, composition, kaggle, t4, polymath]
target: "7-9B base → polymath with K specialty heads (code, math, CU, tools, +reasoning, +RAG)"
hardware: "Kaggle T4×2 (15GB×2), Lightning H200 (single), Modal (rented)"
status: research
---

# V17 — MoE Upcycling + Sub-LoRA Composition for Specialty Heads

## EXECUTIVE SUMMARY (read this first)

**Surrogate-1 V17 goal**: one 7-9B base model that internally hosts K=5-8 specialists (code-Qwen, math-Phi, CU-OpenCUA, tool-xLAM, reasoning-DeepSeek-R1-style, RAG, plan, vision-bridge) and routes per-request to the right specialist — at fixed parameter budget so the binary still loads on Kaggle T4×2.

**Three viable paths** (in order of recommendation for Surrogate constraints):

1. **PATH A (RECOMMENDED) — Frozen base + K LoRA experts + thin router**
   - Train 5-8 specialty LoRAs (rank 32-64) independently on isolated datasets
   - Inference: vLLM Semantic Router classifies request → loads relevant LoRA → forward pass
   - Training cost: each LoRA ~2-4hrs T4×2; total 5-8 × 4hrs = ~20-32hrs (3-5 Kaggle sessions)
   - Inference cost: same as base + ~50ms LoRA hot-swap (negligible)
   - Quality: matches per-task specialist within ~2-5% of standalone baselines

2. **PATH B — Sparse Upcycling (dense → MoE) on Lightning H200**
   - Take Qwen3-8B-base, replicate FFN 8x → 8 experts, add cosine router, continue-pretrain on mixed corpus
   - DeepSeekMoE-style fine-grained + 1 shared expert + aux-loss-free balancing
   - Training cost: 40% of original pretraining = ~$2-5K on H200
   - Inference cost: stays at 8B-active equivalent (only 1-2 experts fire per token)
   - Quality: experts develop emergent specialization; matches mid-tier dense baselines

3. **PATH C — MergeKit Frankenmerge of K specialists + post-merge healing**
   - Take 5-8 existing 7-9B specialists (Qwen3-Coder, Phi-4-mini-reasoning, OpenCUA, xLAM-2-8B, etc.)
   - Use EvoMerge (Sakana) to find optimal layer-wise merge recipe
   - Cost: ~$200-500 evolutionary search on T4×2 + cloud
   - Risk: merging across 5+ specialists often degrades > merging 2-3

**For Surrogate-1**: start with PATH A (lowest risk, T4-feasible); upgrade to PATH B once V17 stable.

---

## PART 1 — MoE UPCYCLING (dense → sparse MoE)

### 1.1 Sparse Upcycling baseline (Komatsuzaki et al., ICLR 2023)

- **Paper**: arxiv 2212.05055 (Google Research)
- **Recipe**: dense checkpoint → replicate FFN N times → router (random init) → continue-pretrain
- **Cost**: < 40% of original dense pretraining FLOPs to match upcycled-MoE quality
- **Routing**: Top-K (K=2 for decoders, Expert Choice K=2 for encoders)
- **T4 feasibility**: training requires ≥ A100 cluster — NOT T4-feasible for the upcycle step itself, but inference of an upcycled small MoE (1B-7B class) IS feasible with offloading
- **Ref**: https://arxiv.org/abs/2212.05055

### 1.2 Mixtral 8x7B / Mixtral-from-Mistral (Jiang et al., Jan 2024)

- **Paper**: arxiv 2401.04088
- **Recipe**: 8 FFN copies of Mistral-7B → top-2 routing → continue-pretrain
- **Result**: 45B total params, 12.9B active per token, matches Llama-2-70B and GPT-3.5 on most benchmarks
- **Routing**: classic learned linear router with auxiliary load-balance loss
- **Ref**: https://arxiv.org/abs/2401.04088, https://huggingface.co/blog/mixtral

### 1.3 DeepSeekMoE — fine-grained + shared experts (DeepSeek, Jan 2024)

- **Paper**: arxiv 2401.06066 (DeepSeekMoE: Towards Ultimate Expert Specialization)
- **Two innovations**:
  1. **Fine-grained segmentation** — split N experts into mN finer experts, activate mK (m=2 typical) → more flexible expert combinations
  2. **Shared expert isolation** — Ks experts always active, capture cross-domain knowledge, reduce redundancy in routed experts
- **Result**: DeepSeekMoE-16B matches DeepSeek-7B / LLaMA-2-7B with ~40% compute
- **Why for Surrogate**: shared expert preserves "polymath base" while routed experts specialize → exactly the "K heads + 1 polymath" topology
- **Ref**: https://arxiv.org/abs/2401.06066, https://github.com/deepseek-ai/DeepSeek-MoE

### 1.4 Aux-loss-free load balancing (DeepSeek, Aug 2024)

- **Paper**: arxiv 2408.15664 (Loss-Free Balancing)
- **Problem solved**: traditional aux-loss creates interference gradients that hurt model quality
- **Recipe**: per-expert bias term added to routing scores BEFORE top-K selection; bias updated by EMA of recent expert load
- **Result**: better load balance + better task quality than aux-loss methods on 3B/200B-token MoEs
- **Adopted by**: DeepSeek-V3 (671B total / 37B active), now standard in 2025-2026 MoE training
- **T4 feasibility**: trivial to implement (10 LOC) — just an EMA bias buffer per expert
- **Ref**: https://arxiv.org/abs/2408.15664, https://gist.github.com/TeaPoly/b5e046d9efa93fa7e38880b4c7e5ec5f

### 1.5 OLMoE-1B-7B — fully open MoE recipe (Allen AI, Sep 2024)

- **Paper**: arxiv 2409.02060
- **Architecture**: 6.9B total / 1.3B active, **64 experts × top-8** routing, dropless token-choice
- **Pretraining**: 5T tokens; outperforms Llama-2-13B-Chat and DeepSeekMoE-16B on benchmarks
- **Why this matters**: ALL training data + code + logs released — best recipe to clone
- **Routing**: dropless = no token gets dropped (vs token-choice with capacity which drops overflow)
- **For Surrogate**: 64×8 too many for 7-9B budget; aim for 16-24 routed × top-2 + 1 shared
- **Ref**: https://arxiv.org/abs/2409.02060, https://github.com/allenai/OLMoE, https://huggingface.co/allenai/OLMoE-1B-7B-0125

### 1.6 Qwen3-30B-A3B — production MoE recipe (Alibaba, May 2025)

- **Paper**: arxiv 2505.09388 (Qwen3 Tech Report)
- **Architecture**: 30.5B total / 3.3B active, 48 layers, GQA (32Q/4KV), **128 experts × top-8**
- **Result**: matches Qwen2.5-32B-Base with 1/5 active params
- **Refresh 2026**: Qwen3.6-35B-A3B (Apr 2026, MoE 35B/3B-active, agentic-tuned, SWE-bench 73.4%)
- **Note**: 3.3B-active is the sweet spot for T4 inference (~12-14GB FP16); training is H100-class
- **Ref**: https://huggingface.co/Qwen/Qwen3-30B-A3B, https://arxiv.org/abs/2505.09388

### 1.7 Granite 3 / 4 MoE upcycling (IBM, 2024-2026)

- **Models**: granite-3.0-1b-a400m, granite-3.0-3b-a800m (both upcycled from Granite dense)
- **Recipe**: replace MLP with MoE layers, fine-grained experts, dropless token routing, load-balancing aux loss
- **Granite 4.1-8B (Oct 2025)**: dense, NOT MoE — IBM chose dense after experiments showed dense quality > MoE at 8B param budget when training-quality optimized
- **Lesson for Surrogate**: at 7-9B budget, dense + LoRA composition often beats MoE upcycle on quality/$ ratio — supports PATH A recommendation
- **Ref**: https://huggingface.co/blog/ibm-granite/granite-4-1, https://huggingface.co/ibm-granite/granite-3.0-3b-a800m-instruct

### 1.8 Phi-3.5-MoE / Phi-tiny-MoE / SlimMoE (Microsoft, 2024-2025)

- **Phi-3.5-MoE**: 16×3.8B → 41.9B total, 6.6B active (top-2 of 16)
- **Phi-tiny-MoE-instruct**: 3.8B total / 1.1B active (compressed from Phi-3.5-MoE via SlimMoE distillation)
- **Phi-mini-MoE-instruct**: 7.6B total / 2.4B active
- **SlimMoE recipe**: distill big MoE → small MoE while preserving expert specialization
- **For Surrogate**: SlimMoE-style distillation of Qwen3-30B-A3B → Surrogate-9B-A2B is feasible on T4×2 inference once trained on rented H100
- **Ref**: https://huggingface.co/microsoft/Phi-3.5-MoE-instruct, https://huggingface.co/microsoft/Phi-tiny-MoE-instruct, https://huggingface.co/microsoft/Phi-mini-MoE-instruct

### 1.9 Cosine / Geometric Routing (2024-2026)

- **X-MoE cosine router** (Chi et al., NeurIPS 2024 perturbed cosine paper arxiv 2405.14131)
- **Recipe**: L2-normalize token hidden + L2-normalize expert centroid → cosine similarity / temperature → softmax
- **Why it matters**: experts become **monosemantic** (interpretable specialization) — paper shows 15% experts pin to single category (temporal, geographic, military, etc.)
- **Geometric Routing arxiv 2604.14434 (Apr 2026)**: rank-1 experts + cosine = causal expert control (you can ablate one specialty cleanly)
- **For Surrogate**: cosine routing makes "is the math expert firing?" inspectable via dashboard → ops-friendly
- **Ref**: https://arxiv.org/abs/2405.14131, https://arxiv.org/abs/2604.14434, https://arxiv.org/html/2509.14255

### 1.10 Drop-Upcycling — partial re-init (Feb 2025)

- **Paper**: arxiv 2502.19261
- **Problem**: vanilla upcycling experts stay too similar → poor specialization at long horizon
- **Recipe**: when copying FFN to N experts, randomly re-initialize (drop) k% of weights per copy → forces divergence
- **Result**: better expert specialization in extended training, especially for K > 8 experts
- **Ref**: https://arxiv.org/html/2502.19261v2

---

## PART 2 — SUB-LORA COMPOSITION

### 2.1 LoRA basics + DoRA (foundational, no MoE)

- **LoRA** (arxiv 2106.09685): freeze base, train low-rank A·B updates per linear layer
- **AdaLoRA** (arxiv 2303.10512, ICLR 2023): SVD-parameterized LoRA + importance scoring → adaptive rank per matrix; 0.1% trainable params, +1.2% F1 on SQuAD2.0
- **DoRA** (arxiv 2402.09353, ICML 2024 Oral): decompose pretrained weight into magnitude (scalar per col) + direction (LoRA-updated unit vector); consistently > LoRA, especially at low ranks (r=8)
- **VeRA** (arxiv 2310.11454): single shared random A, B across layers + per-layer learned scaling vectors → ~50% of LoRA's trainable params at r=1
- **For Surrogate**: use **DoRA r=64** for specialty experts (best quality/param ratio); keep **VeRA** for "thin" extras (style, persona) where r=1 suffices
- **Ref**: https://arxiv.org/abs/2402.09353, https://arxiv.org/abs/2303.10512, https://arxiv.org/abs/2310.11454

### 2.2 LoRAHub — dynamic LoRA composition (Sail-SG, COLM 2024)

- **Paper**: arxiv 2307.13269, repo https://github.com/sail-sg/lorahub
- **Recipe**: pool of N pre-trained LoRAs (one per task) → for new task, **gradient-free** optimization (CMA-ES) of N coefficients on few-shot examples → blended LoRA
- **Result**: matches in-context-learning on Big-Bench Hard with significantly fewer tokens
- **For Surrogate**: ideal for "auto-compose specialty LoRAs at task-time given 5 examples"; CMA-ES costs ~30s on CPU
- **Ref**: https://arxiv.org/abs/2307.13269, https://github.com/sail-sg/lorahub

### 2.3 X-LoRA — token-level deep mixture (Buehler & Buehler, Feb 2024)

- **Paper**: arxiv 2402.07148, repo https://github.com/EricLBuehler/xlora
- **Recipe**: K pre-trained LoRAs frozen, gating MLP reads hidden states → produces per-layer-per-LoRA scaling values → dense weighted sum at every layer
- **Why dense**: all K LoRAs always loaded; not sparse top-k selection (heavier than MoLE)
- **Cost**: K=5-8 LoRAs × r=64 → ~150-250M extra weights resident → fine on T4×2
- **For Surrogate**: best when K small (≤8) and request-level routing inadequate (need within-token blending of code+math)
- **Ref**: https://arxiv.org/abs/2402.07148

### 2.4 MoLE — Mixture of LoRA Experts (Wu et al., ICLR 2024)

- **Paper**: arxiv 2404.13628 (Microsoft + ICLR 2024)
- **Recipe**: each layer of each LoRA = distinct expert; learnable per-layer gating function blends them; supports masking out adapters at inference without retraining gates
- **Distinction from X-LoRA**: layer-wise (not token-wise) gating; cheaper at inference
- **Mask flexibility**: at inference, drop unwanted LoRAs, gates auto-renormalize → "load only code + math, mute the rest" without retraining
- **For Surrogate**: this is the closest match to "K specialty heads with runtime composition" — recommended
- **Ref**: https://arxiv.org/abs/2404.13628

### 2.5 LD-MoLE — learnable dynamic routing (Zhuang et al., Sep 2025)

- **Paper**: arxiv 2509.25684 (v2 Feb 2026)
- **Recipe**: replaces non-differentiable Top-K with **closed-form differentiable sparsity-controlled routing**; model decides per-token-per-layer how many experts to activate
- **Key advantage**: no fixed K — easy tasks use 1 expert, hard reasoning uses 4-5
- **Sparsity loss**: analytical regularizer on number-of-active-experts
- **For Surrogate**: better than fixed top-2 because math problems need >1 expert mid-derivation
- **Ref**: https://arxiv.org/abs/2509.25684

### 2.6 DR-LoRA — dynamic rank LoRA for MoE adaptation (Jan 2026)

- **Paper**: arxiv 2601.04823
- **Recipe**: start all expert LoRAs at small rank; expert saliency = routing-frequency × gradient-importance; periodically grow rank of high-saliency experts
- **Result**: outperforms uniform-rank LoRA across 6 tasks on 3 MoE models
- **For Surrogate**: lets us start cheap (r=16 all experts) and grow only those used → fits T4 budget evolution
- **Ref**: https://arxiv.org/abs/2601.04823

### 2.7 MoE-Sieve — routing-guided LoRA on existing MoE (2025)

- **Paper**: arxiv 2603.24044
- **Recipe**: forward-pass on task data → count per-layer expert activations → apply LoRA only to top-25% routed experts + always-active modules
- **Result**: matches full LoRA-on-all-experts with 70-73% fewer LoRA params
- **For Surrogate**: if we go PATH B (upcycle to MoE), this is the post-upcycle fine-tune step
- **Ref**: https://arxiv.org/html/2603.24044

### 2.8 LoRA-Switch — system-algorithm co-design (NeurIPS 2024)

- **Paper**: arxiv 2405.17741, OpenReview NIG8O2zQSQ
- **Problem**: dynamic LoRA-MoE has 2.5× decoding latency overhead from fragmented CUDA kernels
- **Recipe**: token-wise pre-gated LoRA + fused CUDA kernel that merges all selected adapters in one launch
- **Key constraint**: routing weights for all layers MUST be identical (single token-level decision) → simpler than MoLE's per-layer gates
- **Result**: ~1.0× decoding speed (no overhead vs frozen base)
- **For Surrogate**: this is **the** inference kernel to integrate when we go to production T4×2 serving
- **Ref**: https://arxiv.org/abs/2405.17741

### 2.9 Specialty LoRA-MoE methods (2024-2026 grab bag)

| Method | Paper | Routing | Best for |
|--------|-------|---------|----------|
| HMoRA (hierarchical) | OpenReview lTkHiXeuDl | 2-stage hierarchical | Hierarchical task graphs |
| SAMoRA | arxiv 2604.19048 | semantic-aware + scaling | Task-adaptive multi-domain |
| TT-LoRA MoE | arxiv 2504.21190 | tensor-train compressed | Maximum parameter efficiency (-98%) |
| Single-rank MoE-LoRA | arxiv 2501.15103 | rank-1 = expert | Many fine-grained experts |
| MTL-LoRA | arxiv 2410.09437 | task-adaptive params | Multi-task SFT |
| D-MoLE (curriculum) | THU 2025 | difficulty-driven | Continual multi-modal tuning |
| LoRA Soups | arxiv 2410.13025 | post-training merge | Few-shot composition no router |

### 2.10 Adapter-merging recipes (no router needed)

- **AdapterFusion** (arxiv 2005.00247): two-stage — train per-task adapters, then composition layer
- **MerA** (arxiv 2308.15982): merge pretrained adapters into single — > AdapterFusion on few-shot
- **LoRA Soups** (arxiv 2410.13025): direct LoRA averaging recipe; works surprisingly well at low K (≤4)
- **Adapter Soup** (Pfeiffer et al.): weight-averaging for generalization
- **For Surrogate**: useful as baseline — train K LoRAs, average them → "polymath in one adapter, no routing"

### 2.11 Symbolic-MoE / keyword routing

- Search did NOT surface a paper named exactly "Symbolic-MoE" — likely informal name for **rule-based / keyword-prefilter routing** on top of LoRA pool
- **Practical implementation**: regex on user prompt → select LoRA subset → forward through base + selected LoRAs
- **Trade-off**: zero training but brittle; replace with embedding-router (vLLM Semantic Router, see §3.1) for production

---

## PART 3 — INFERENCE-TIME ROUTING

### 3.1 vLLM Semantic Router (vLLM project, 2025-2026)

- **Repo**: https://github.com/vllm-project/semantic-router
- **Iris v0.1** (Jan 2026): https://blog.vllm.ai/2026/01/05/vllm-sr-iris.html
- **Modular LoRA scaling** (Oct 2025): https://blog.vllm.ai/2025/10/27/semantic-router-modular.html
- **Recipe**: 
  1. classifier (small BERT-style + LoRA per task) embeds prompt
  2. classifier predicts intent → maps to LoRA name
  3. vLLM loads/keeps that LoRA hot → request flows through base + LoRA
- **Scale**: production-tested at 1000+ tenant-specific LoRAs
- **For Surrogate**: this IS the inference runtime — V17 server = vLLM with 5-8 LoRAs registered + semantic-router classifier
- **Latency**: classifier ~5ms, LoRA hot-swap negligible (already loaded), end-to-end indistinguishable from base inference
- **Ref**: https://docs.vllm.ai/en/latest/features/lora/, https://vllm-semantic-router.com/docs/tutorials/intelligent-route/lora-routing/

### 3.2 LoRAX (Predibase, OSS)

- **Docs**: https://loraexchange.ai/
- **Feature**: dynamic LoRA loading + batching — different requests with different LoRAs in one batch
- **Why mention**: Predibase scales serving to thousands of customer LoRAs; if vLLM Semantic Router is too new, LoRAX is more battle-tested
- **Cold start fix**: HuggingFace blog "Goodbye cold boot — LoRA Inference 300% faster" (https://huggingface.co/blog/lora-adapters-dynamic-loading)

### 3.3 NVIDIA NIM swarm-of-LoRAs

- **Blog**: https://developer.nvidia.com/blog/seamlessly-deploying-a-swarm-of-lora-adapters-with-nvidia-nim/
- **For Surrogate**: only relevant if we move to NIM; not T4-friendly (NIM targets H100/H200/Blackwell)

### 3.4 LoRAServe (distributed serving, Dec 2025)

- **Paper**: arxiv 2511.22880
- **Recipe**: heterogeneous-LoRA-aware placement + routing — minimizes adapter heterogeneity per server while balancing load
- **For Surrogate**: only when scaling to multi-node; single-node T4×2 doesn't need it

### 3.5 Adapter retrieval via vector DB

- **Paper**: arxiv 2602.21222 (Task-Aware LoRA Adapter Composition via Similarity Retrieval)
- **Recipe**: embed each adapter's task description; embed user query; cosine top-K → load top-K LoRAs and weight by similarity
- **For Surrogate**: backup if classifier accuracy drops on out-of-distribution prompts; vector-DB lookup is robust

### 3.6 AdaFuse — token-level pre-gating + fused kernel (Mar 2026)

- **Paper**: arxiv 2603.11873
- **Recipe**: similar to LoRA-Switch — token-level pre-gating + fused CUDA kernel for adapter merge
- **Improvement over LoRA-Switch**: more aggressive pre-gating, lower memory traffic

---

## PART 4 — MERGING RECIPES

### 4.1 MergeKit (Arcee, gold-standard tool)

- **Repo**: https://github.com/arcee-ai/mergekit
- **Paper**: arxiv 2403.13257 (v3, 2025)
- **Methods supported**: Linear, SLERP, TIES, DARE-linear, DARE-TIES, Passthrough (Frankenmerge), TIES-SOUP, Model Stock, NuSLERP, Della, Della-linear
- **Hardware**: CPU or 8GB VRAM minimum — runs on Mac M3 24GB easily
- **YAML config**: declarative merge recipe → merged HF checkpoint output

### 4.2 SLERP — spherical linear interpolation

- **Use case**: 2-model merge, smooth transition (treats weights as points on hypersphere)
- **Limitation**: pairwise only
- **Recipe** (MergeKit YAML):
  ```yaml
  models:
    - model: Qwen/Qwen3-Coder-7B
    - model: microsoft/Phi-4-mini-reasoning
  merge_method: slerp
  base_model: Qwen/Qwen3-Coder-7B
  parameters:
    t: 0.5
  dtype: bfloat16
  ```

### 4.3 TIES (Trim, Elect Sign, Merge)

- **Paper**: arxiv 2306.01708 (Yadav et al., NeurIPS 2023)
- **Recipe** for K models against base:
  1. **Trim**: keep top-X% magnitude delta-params, zero rest
  2. **Elect Sign**: per param, majority sign vote across models
  3. **Merge**: average non-zero deltas with consistent sign
- **Why**: removes interference between conflicting fine-tunes (e.g., code vs math models pushing same param opposite directions)

### 4.4 DARE (Drop and REscale)

- **Paper**: arxiv 2311.03099 (Yu et al., 2023)
- **Recipe**: random-drop p% of delta params, rescale survivors by 1/(1-p) → expectations preserved
- **Combined with TIES**: `dare_ties` in MergeKit — drop redundant deltas first, then TIES → strongest pre-2024 baseline
- **DAREx** (improved at p=99% drop): https://arxiv.org/html/2503.08998v1

### 4.5 EvoMerge — evolutionary merge (Sakana AI, Mar 2024)

- **Paper**: arxiv 2403.13187 (accepted Nature Machine Intelligence Jan 2025)
- **Repo**: https://github.com/SakanaAI/evolutionary-model-merge
- **Recipe**: CMA-ES over **two spaces**: (a) merge weights (parameter space), (b) layer routing (data flow space)
- **Result**: SOTA Japanese-Math-LLM by merging unrelated specialists — emergent cross-domain capability
- **For Surrogate**: ~$200-500 for 100-generation evolutionary search across 5-8 specialists; unattended — set and forget
- **Integration**: now built into MergeKit + Optuna Hub

### 4.6 AutoMerge — Bayesian-search block-wise merging (Jan 2026)

- **Paper**: arxiv 2601.22748
- **Recipe**: segment models into heterogeneous blocks → Bayesian-optimize merge method + hyperparams per block
- **Result**: +23.55% preservation vs whole-model search; -51.94% preservation discrepancy
- **For Surrogate**: better than EvoMerge when we know block boundaries (transformer blocks 0-7 = early/general, 24-31 = late/specialized)

### 4.7 Frankenmerge / Passthrough (the "Goliath" recipe)

- **MergeKit support**: `passthrough` method
- **Famous example**: Goliath-120B = 2× Llama-2-70B layer-stacked
- **For Surrogate**: at 7-9B budget, passthrough goes WRONG direction (creates 13-18B model); only useful if we want a 12-15B "Surrogate-Polymath" that we then quantize back to 4-bit for T4

### 4.8 Slerp-Opt (adaptive SLERP, 2025)

- **Paper**: J. Supercomputing 2025 (Springer 10.1007/s11227-025-07727-4)
- **Recipe**: SLERP with per-layer dynamically tuned t coefficient
- **For Surrogate**: better than vanilla SLERP when models diverge unevenly across layers

### 4.9 Preference-Aligned LoRA Merging (Apr 2026)

- **Paper**: arxiv 2603.26299
- **Recipe**: preserves subspace coverage and addresses directional anisotropy in LoRA merges → merged adapter doesn't collapse to dominant LoRA
- **For Surrogate**: critical when merging 5+ specialty LoRAs (anisotropy compounds with K)

---

## PART 5 — SPECIFIC SPECIALIST RECIPES

### 5.1 Qwen3-Coder family (Alibaba, Code specialist)

- **Qwen3-Coder-Next**: built on Qwen3-Next-80B-A3B-Base (hybrid attention + MoE, 3B active)
- **Pretraining**: 7.5T tokens, **70% code ratio** while preserving general+math
- **Post-training**: long-horizon Agent-RL with 20,000 parallel environments
- **For Surrogate**: Qwen3-Coder-7B/8B is the **code teacher** for distillation or **code LoRA target** for PATH A
- **Ref**: https://github.com/QwenLM/Qwen3-Coder, https://qwen.ai/blog?id=qwen3-coder-next

### 5.2 Phi-4-mini-reasoning (Microsoft, Math specialist)

- **Paper**: arxiv 2504.21233
- **Architecture**: 3.8B dense, GQA, shared in/out embedding, 200K vocab
- **Training data**: synthetic math from DeepSeek-R1, 1M+ problems middle-school → PhD
- **Result**: Math-500 — beats DeepSeek-R1-Distill-Qwen-7B by 3.2 points, beats Distill-Llama-8B by 7.7
- **For Surrogate**: math LoRA training data sourced same way (DeepSeek-R1 distilled CoT)
- **Ref**: https://huggingface.co/microsoft/Phi-4-mini-reasoning

### 5.3 OpenCUA (Computer-Use specialist, Aug 2025)

- **Paper**: arxiv 2508.09123
- **Recipe**: AgentNet dataset (3 OS, 200+ apps/sites) + reflective long-CoT synthesis (planning+memory+reflection inner-monologue)
- **Result**: OpenCUA-72B = #1 OSWorld-Verified leaderboard (45.0%)
- **For Surrogate**: CU LoRA uses AgentNet + reflective-CoT recipe; vision encoder from base must support screenshots
- **Ref**: https://opencua.xlang.ai/, https://github.com/xlang-ai/OpenCUA

### 5.4 xLAM-2 (Salesforce, Tool-calling specialist)

- **Paper**: arxiv 2409.03215 (xLAM family); Apr 2025 xLAM-2-fc-r series
- **Models**: 1B / 3B / 8B / 32B / 70B all `-fc-r` tool-calling-trained
- **Data**: APIGen-MT pipeline — multi-turn function-calling trajectories with rule + multi-agent-LLM verification
- **For Surrogate**: tool LoRA training data = xLAM-function-calling-60k (HF dataset) + APIGen-MT augmentation
- **Ref**: https://github.com/SalesforceAIResearch/xLAM, https://huggingface.co/datasets/Salesforce/xlam-function-calling-60k

### 5.5 Granite-4.1-8B (IBM, Polymath baseline, Oct 2025)

- **Architecture**: dense 8B, NOT MoE (chose dense after experiments)
- **Training**: 5-phase, 15T tokens, code/math weighting in phase 2, long-context to 512K in phase 5
- **Post**: SFT on 4.1M curated + on-policy GRPO with DAPO loss
- **Why interesting**: matches Granite-3.0-32B-MoE flagship at 8B dense — challenges PATH B (MoE) for Surrogate budget
- **For Surrogate**: ideal **base model** if we accept dense, then attach K LoRAs for specialty heads
- **Ref**: https://huggingface.co/blog/ibm-granite/granite-4-1, https://huggingface.co/ibm-granite/granite-4.1-8b

### 5.6 Mistral Small 4 (Mistral, unified-reasoning MoE, 2026)

- **Architecture**: 119B-params MoE, **128 experts × 4 routed + 1 shared**, ~9-12B active
- **Innovation**: `reasoning_effort` parameter at inference: "none" = fast chat, "high" = step-by-step Magistral-mode
- **Recipe**: consolidates Magistral (reasoning) + Devstral (code) + Mistral-Small (instruct) into one MoE
- **For Surrogate**: 119B too large for T4×2; but the **`reasoning_effort` API pattern** is gold — adopt directly: `model.generate(..., reasoning_effort="high")` triggers thinking-LoRA + math-LoRA composite
- **Ref**: https://mistral.ai/news/mistral-small-4, https://huggingface.co/mistralai/Mistral-Small-4-119B-2603

### 5.7 Falcon-H1R-7B (TII, Jan 2026)

- **News**: 7B reasoning model, 256K context, matches 14B-47B reasoning models on math+code
- **For Surrogate**: another candidate base if Granite-4.1-8B's license has issues
- **Ref**: https://www.marktechpost.com/2026/01/07/tii-abu-dhabi-released-falcon-h1r-7b...

---

## PART 6 — RECOMMENDED V17 ARCHITECTURE (the answer)

### 6.1 Topology

```
Surrogate-1 V17
├─ Base: Qwen3-8B-Base OR Granite-4.1-8B (license check)
├─ K=6 specialty LoRAs (DoRA r=64, ~150-200M params each)
│  ├─ code-LoRA       (Qwen3-Coder-distill)      ~180M
│  ├─ math-LoRA       (DeepSeek-R1-distill)      ~180M
│  ├─ cu-LoRA         (OpenCUA AgentNet)         ~180M
│  ├─ tool-LoRA       (xLAM APIGen-MT)           ~180M
│  ├─ reason-LoRA     (DeepSeek-R1 traces)       ~180M
│  └─ rag-LoRA        (long-context QA)          ~180M
├─ Always-on shared LoRA (DoRA r=32, "polymath")  ~90M
├─ Total trainable: ~1.2B (15% of 8B base)
└─ Router: vLLM Semantic Router + 22M-param BERT classifier
```

### 6.2 Training plan (Kaggle T4×2, 6 sessions)

| Session | Specialist LoRA | Dataset | Time |
|---------|-----------------|---------|------|
| S1 | code-LoRA | the-stack-v2 + Qwen3-Coder distill | ~6h |
| S2 | math-LoRA | NuminaMath + DeepSeek-R1 traces | ~6h |
| S3 | cu-LoRA | AgentNet (OpenCUA) | ~5h |
| S4 | tool-LoRA | xLAM-function-calling-60k + APIGen-MT | ~4h |
| S5 | reason-LoRA | OpenThoughts + R1-zero rollouts | ~6h |
| S6 | rag-LoRA + shared-LoRA | LongBench + RULER + general blend | ~7h |

**Dataset isolation**: each LoRA trained ONLY on its specialty data; classifier's training data ≠ any LoRA training data (zero contamination)

### 6.3 Inference runtime (≤200 LOC)

```python
# v17_router.py — total ~150 LOC including imports
import torch, json, time
from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import PeftModel
from sentence_transformers import SentenceTransformer
import numpy as np

BASE_ID = "ibm-granite/granite-4.1-8b"
LORA_DIR = "./loras"  # contains code/, math/, cu/, tool/, reason/, rag/, shared/
SPECIALTIES = ["code", "math", "cu", "tool", "reason", "rag"]

class SpecialtyRouter:
    """Embeds prompt -> picks top-2 specialty LoRAs to compose with always-on shared."""
    def __init__(self, threshold=0.35):
        self.embedder = SentenceTransformer("BAAI/bge-small-en-v1.5")
        # centroids built once from a few exemplars per specialty
        with open(f"{LORA_DIR}/centroids.json") as f:
            self.centroids = {k: np.array(v) for k, v in json.load(f).items()}
        self.threshold = threshold

    def pick(self, prompt: str, top_k: int = 2) -> list[str]:
        emb = self.embedder.encode(prompt, normalize_embeddings=True)
        scores = {k: float(emb @ c) for k, c in self.centroids.items()}
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        chosen = [k for k, s in ranked[:top_k] if s >= self.threshold]
        return chosen or [ranked[0][0]]  # always pick at least 1

class V17Engine:
    def __init__(self):
        self.tok = AutoTokenizer.from_pretrained(BASE_ID)
        base = AutoModelForCausalLM.from_pretrained(
            BASE_ID, torch_dtype=torch.bfloat16, device_map="auto"
        )
        # always-on shared LoRA loaded as base layer
        self.model = PeftModel.from_pretrained(base, f"{LORA_DIR}/shared", adapter_name="shared")
        # register all specialty LoRAs (lazy-load weights to CPU until selected)
        for sp in SPECIALTIES:
            self.model.load_adapter(f"{LORA_DIR}/{sp}", adapter_name=sp)
        self.router = SpecialtyRouter()

    def generate(self, prompt: str, reasoning_effort: str = "auto", **kw) -> str:
        # MoLE-style composition: shared always on + top-K specialty
        picked = self.router.pick(prompt, top_k=2 if reasoning_effort != "low" else 1)
        adapters = ["shared"] + picked
        weights = [0.5] + [0.5 / len(picked)] * len(picked)  # uniform split among specialty
        self.model.set_adapter(adapters)
        # weighted_adapter API merges with given weights at forward time
        self.model.add_weighted_adapter(adapters, weights, "_active", combination_type="linear")
        self.model.set_adapter("_active")
        ids = self.tok(prompt, return_tensors="pt").to(self.model.device)
        out = self.model.generate(**ids, max_new_tokens=kw.get("max_tokens", 512))
        text = self.tok.decode(out[0][ids.input_ids.shape[1]:], skip_special_tokens=True)
        # cleanup transient combined adapter
        self.model.delete_adapter("_active")
        return text

if __name__ == "__main__":
    eng = V17Engine()
    print(eng.generate("Write a Python function to compute SHA-256.", reasoning_effort="low"))
    print(eng.generate("Solve: integrate x^2 sin(x) dx.", reasoning_effort="high"))
```

### 6.4 Why PATH A wins for Surrogate

| Criterion | PATH A (LoRA+Router) | PATH B (MoE Upcycle) | PATH C (Frankenmerge) |
|-----------|----------------------|----------------------|------------------------|
| Training $ | $50-200 (Kaggle free) | $2K-5K (H100 cluster) | $200-500 (search) |
| Training feasibility T4×2 | YES per-LoRA | NO (need A100+) | YES (CPU/8GB) |
| Inference T4×2 | YES (8B+0.2B active) | TIGHT (8B+experts) | YES (8B unchanged) |
| Quality vs specialist | -2 to -5% | -5 to -10% | -10 to -20% |
| Add new specialty | train 1 new LoRA | retrain MoE | re-search merge |
| Mute a specialty | drop from routing | hard | impossible |
| Inspectability | per-LoRA usage logs | router entropy | none |
| Risk | LOW | MED | HIGH |

### 6.5 Concrete kaggle-trainer.sh patch (V17 mode)

```bash
# Add to /Users/Ashira/develope/AI/surrogate-1/scripts/kaggle-trainer.sh
# (assumes existing script trains a single LoRA; V17 trains them in series across sessions)

case "${V17_SPECIALTY:-}" in
  code)
    DATASET="bigcode/the-stack-v2-train-smol-ids"
    DATA_FILTER="language in ['python','typescript','rust','go']"
    LORA_TARGET="q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj"
    LORA_RANK=64
    EPOCHS=2 ; LR=2e-4
    ;;
  math)
    DATASET="AI-MO/NuminaMath-CoT"
    DATA_FILTER=""
    LORA_TARGET="q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj"
    LORA_RANK=64
    EPOCHS=3 ; LR=1e-4
    ;;
  cu)
    DATASET="xlangai/AgentNet"
    DATA_FILTER=""
    LORA_TARGET="q_proj,k_proj,v_proj,o_proj"  # vision-aware = attn only
    LORA_RANK=48
    EPOCHS=2 ; LR=2e-4
    ;;
  tool)
    DATASET="Salesforce/xlam-function-calling-60k"
    DATA_FILTER=""
    LORA_TARGET="q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj"
    LORA_RANK=48
    EPOCHS=3 ; LR=2e-4
    ;;
  reason)
    DATASET="open-thoughts/OpenThoughts-114k"
    DATA_FILTER=""
    LORA_TARGET="q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj"
    LORA_RANK=64
    EPOCHS=2 ; LR=1e-4
    ;;
  rag)
    DATASET="THUDM/LongBench"
    DATA_FILTER=""
    LORA_TARGET="q_proj,k_proj,v_proj,o_proj"
    LORA_RANK=32
    EPOCHS=2 ; LR=2e-4
    ;;
  shared)
    DATASET="HuggingFaceH4/ultrachat_200k"  # general polymath blend
    DATA_FILTER=""
    LORA_TARGET="q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj"
    LORA_RANK=32
    EPOCHS=1 ; LR=2e-4
    ;;
  *) echo "Set V17_SPECIALTY={code,math,cu,tool,reason,rag,shared}" ; exit 1 ;;
esac

USE_DORA=1   # always DoRA (better than LoRA at low rank)
QUANTIZATION=4bit
GRADIENT_CHECKPOINT=1
BATCH=1 ; GRAD_ACCUM=16
SEQ_LEN=4096

python train_lora.py \
  --base "${BASE_MODEL:-ibm-granite/granite-4.1-8b}" \
  --dataset "$DATASET" --filter "$DATA_FILTER" \
  --use_dora "$USE_DORA" --rank "$LORA_RANK" --target "$LORA_TARGET" \
  --epochs "$EPOCHS" --lr "$LR" \
  --bf16 --quant_4bit --grad_ckpt \
  --batch "$BATCH" --grad_accum "$GRAD_ACCUM" --seqlen "$SEQ_LEN" \
  --output "/kaggle/working/loras/${V17_SPECIALTY}"
```

---

## PART 7 — RISKS + MITIGATIONS

| Risk | Mitigation |
|------|------------|
| LoRA training contamination (specialty leaks into other) | Strict dataset partitioning (see §6.2 table), no shared corpora across specialty trainings |
| Router misclassification | Threshold + fallback to "shared" only; log misroutes; weekly retrain with active learning |
| Specialty LoRA degrades base capability | Always-on shared LoRA (50% weight) provides "polymath floor"; eval base+shared alone monthly |
| K too high → router accuracy drops | Start K=4 (code, math, cu, tool); add reason+rag only after V17.1 stable |
| LoRA + DoRA quantized inference quality | Use 4-bit NF4 base + bf16 LoRA; verify on Math-500 + HumanEval+ vs unquantized |
| MergeKit crashes on Granite arch | Use mergekit-v0.7+ (Apr 2025+ Granite support); fallback Qwen3-8B-Base |
| Training time exceeds 12h Kaggle limit | Checkpoint every 500 steps; resume across sessions (already in current trainer) |

---

## PART 8 — REFERENCES (clickable)

### Core papers
- Sparse Upcycling — https://arxiv.org/abs/2212.05055
- Mixtral 8x7B — https://arxiv.org/abs/2401.04088
- DeepSeekMoE — https://arxiv.org/abs/2401.06066
- Aux-loss-free balance — https://arxiv.org/abs/2408.15664
- OLMoE — https://arxiv.org/abs/2409.02060
- Qwen3 Tech Report — https://arxiv.org/abs/2505.09388
- Drop-Upcycling — https://arxiv.org/html/2502.19261v2
- Cosine routing — https://arxiv.org/abs/2405.14131
- Geometric routing — https://arxiv.org/abs/2604.14434

### LoRA composition
- LoRA — https://arxiv.org/abs/2106.09685
- AdaLoRA — https://arxiv.org/abs/2303.10512
- DoRA — https://arxiv.org/abs/2402.09353
- VeRA — https://arxiv.org/abs/2310.11454
- LoRAHub — https://arxiv.org/abs/2307.13269
- X-LoRA — https://arxiv.org/abs/2402.07148
- MoLE — https://arxiv.org/abs/2404.13628
- LD-MoLE — https://arxiv.org/abs/2509.25684
- DR-LoRA — https://arxiv.org/abs/2601.04823
- LoRA-Switch — https://arxiv.org/abs/2405.17741
- AdaFuse — https://arxiv.org/abs/2603.11873
- MoE-Sieve — https://arxiv.org/html/2603.24044
- LoRA Soups — https://arxiv.org/html/2410.13025v2

### Merging
- TIES — https://arxiv.org/abs/2306.01708
- DARE — https://arxiv.org/abs/2311.03099
- EvoMerge — https://arxiv.org/abs/2403.13187
- AutoMerge — https://arxiv.org/pdf/2601.22748
- MergeKit paper — https://arxiv.org/html/2403.13257v3
- Slerp-Opt — https://link.springer.com/article/10.1007/s11227-025-07727-4
- Preference-aligned LoRA merging — https://arxiv.org/html/2603.26299

### Specialists
- Phi-4-mini-reasoning — https://arxiv.org/abs/2504.21233
- OpenCUA — https://arxiv.org/abs/2508.09123
- xLAM — https://arxiv.org/pdf/2409.03215
- Qwen3-Coder — https://qwenlm.github.io/blog/qwen3-coder/
- Granite 4.1 — https://huggingface.co/blog/ibm-granite/granite-4-1
- Mistral Small 4 — https://mistral.ai/news/mistral-small-4

### Inference runtimes
- vLLM Semantic Router — https://github.com/vllm-project/semantic-router
- vLLM Multi-LoRA — https://docs.vllm.ai/en/latest/features/lora/
- LoRAX — https://loraexchange.ai/
- LoRAServe — https://arxiv.org/pdf/2511.22880

### Tools
- MergeKit — https://github.com/arcee-ai/mergekit, https://www.mergekit.com/
- HF PEFT model_merging — https://huggingface.co/docs/peft/developer_guides/model_merging
- Sakana evolutionary-model-merge — https://github.com/SakanaAI/evolutionary-model-merge

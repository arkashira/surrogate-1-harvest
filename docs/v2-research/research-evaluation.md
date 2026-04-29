---
title: Code LLM Evaluation Benchmarks & Methodologies — Surrogate-1 v2 Research
date: 2026-04-29
tags: [surrogate-1, evaluation, benchmarks, humaneval, livecodebench, swe-bench, evalplus, bigcodebench]
status: research-complete
context: Surrogate-1 v1 has only an informal 15-prompt qualitative comparison (see v1-eval-vs-base.md). Establishing reproducible eval pipeline for v1→v2→v3 progress tracking on free Lightning AI tier.
---

# Code LLM Evaluation Benchmarks & Methodologies (2025-2026)

## Executive Summary

The 2025-2026 code LLM evaluation landscape is fragmented but stabilizing around **four pillars**:

1. **EvalPlus (HumanEval+/MBPP+)** — fast smoke test, 30 min on T4. **Saturated** for frontier but still differentiates 7B-class models (gap from 50% → 90%).
2. **LiveCodeBench** — gold standard for **contamination resistance** (refreshed monthly from LeetCode/AtCoder/CodeForces). Required for honest training-data-leak detection.
3. **BigCodeBench** — function-level realism, 1140 tasks across diverse libs, "Hard" subset (148 tasks) is fast.
4. **SWE-Bench Verified/Lite** — agentic real-GitHub-issues, expensive (~120 GB storage, hours per run on prod hardware), but the only signal that maps to "did the model fix a real bug?"

For Surrogate-1 v2 (LoRA on Qwen2.5-Coder-7B, trained for DevSecOps), the practical pipeline is **EvalPlus + LiveCodeBench-Lite + a custom DevSecOps eval set** — all fitting in Lightning's 22-35 free GPU-hours/month.

HumanEval alone is **deprecated as a progress metric** (frontier ~99%, Qwen2.5-Coder-7B already at 88.4%) — the noise floor exceeds typical LoRA delta.

---

## Section 1: Current SOTA Code Benchmarks (2025-2026)

### 1.1 Saturation Status

| Benchmark | Year | Status (2026) | Use For Surrogate-1? |
|-----------|------|---------------|---------------------|
| HumanEval | 2021 | **SATURATED** (frontier 99%+, 113/164 tasks solved by every tested model) | **Sanity check only** — not a progress metric |
| HumanEval+ (EvalPlus) | 2023 | Still differentiates at 7B class | **YES** — primary smoke test |
| MBPP | 2021 | Saturating (frontier 90%+) | Skip; use MBPP+ |
| MBPP+ (EvalPlus) | 2023 | Differentiates 7B class | **YES** — paired with HumanEval+ |
| BigCodeBench | 2024 (ICLR'25) | Active; 1140 realistic tasks | **YES — Hard subset (148)** |
| LiveCodeBench | 2024 | **GOLD STANDARD** for contamination — refreshed monthly | **YES** — release_v6 = 1055 problems |
| SWE-Bench | 2023 | Active but flawed (some unsolvable) | Skip; use Verified |
| SWE-Bench Verified | 2024 (OpenAI) | 500 human-validated tasks | Stretch goal — **expensive** |
| SWE-Bench Lite | 2024 | 300 easier subset | **Maybe** — still 100+ GB storage |
| SWE-Bench Pro | 2025 (Scale) | Newer, harder agentic | Skip — closed eval |
| APPS | 2021 | Largely deprecated, contaminated | Skip |
| CodeForces | 2024 | Reasoning-heavy, used by frontier labs | Skip — Surrogate's not reasoning-focused |
| DS-1000 | 2022 | Data-science-specific | Skip — not Surrogate's domain |
| RepoEval / CrossCodeEval | 2023 | Multi-file completion | Skip for v2; consider v3 |
| Aider Polyglot | 2024 | 225 Exercism exercises across 6 langs | **Maybe** — good agentic edit signal |
| CRUXEval | 2024 | Code reasoning (input/output prediction) | Skip — orthogonal to gen |
| Mercury | 2024 | Code efficiency / runtime | Skip — orthogonal |

### 1.2 Top 5 Most-Cited 2024-2026 Benchmarks (with sources)

1. **HumanEval+ / MBPP+ (EvalPlus)** — Liu et al., NeurIPS 2023, COLM 2024. ~80x more test cases than original.
2. **BigCodeBench** — ICLR 2025. 1140 tasks emphasizing function calls + complex instructions.
3. **LiveCodeBench** — ICLR 2025. Continuously refreshed competitive programming, contamination-free by design.
4. **SWE-Bench / SWE-Bench Verified** — Jimenez et al., ICLR 2024 + OpenAI 2024 verification. Real GitHub issues across 12 popular Python repos.
5. **Aider Polyglot** — 2024, multi-language code editing across C++/Go/Java/JS/Python/Rust.

### 1.3 Detailed Per-Benchmark Reference

#### HumanEval (164 problems, Python only)

- **Status**: Saturated. Per recent analysis, **113 of 164 tasks were solved correctly by every tested model**, only 1 task none solved.
- **Format**: Function signature + docstring → complete the function body.
- **Test cases**: ~7 per problem (insufficient — false positives common).
- **Verdict**: Use only as a sanity check. Frontier scores cluster at 95%+. For 7B base/LoRA, score range is ~50% (StarCoder2) → 88% (Qwen2.5-Coder-7B).

#### HumanEval+ / MBPP+ (EvalPlus)

- **HumanEval+**: 164 problems × ~80 tests each (avg 774 vs 7 in original). Catches edge cases the originals miss.
- **HumanEval+ Mini**: 16.5 tests/problem on average (faster).
- **MBPP+**: 378 of original 974 problems with extended tests (filters out ambiguous originals).
- **Method**: pass@1 with greedy decoding (T=0).
- **Realistic 7B-class scores**:
  - Qwen2.5-Coder-7B-Instruct: ~84% HE+, ~75% MBPP+
  - DeepSeek-Coder-V2-Lite (16B/2.4B active): ~83% HE+
  - Codestral 22B: 78% HE / 71% MBPP+
  - Qwen2.5-Coder-32B-Instruct: **87.2% HE+** (matches GPT-4o)
  - StarCoder2-7B (base): 34% HE
  - OpenCoder-8B: 64.6% HE

#### BigCodeBench (1140 tasks)

- **Subsets**: Full (1140) and **Hard (148 — recommended for fast iteration)**.
- **Splits**: `complete` (function body completion) and `instruct` (instruction following).
- **Realism**: Tests call to 139 libraries, function-level realistic tasks.
- **Runtime**: Hard subset ~4-5 min on Gradio backend; Full ~6-7 min. E2B sandbox slower (15-30 min).
- **Output**: `pass@1` per task with three result files (`_eval_results.json`, `_pass_at_k.json`).

#### LiveCodeBench (1055 problems, release_v6)

- **Why it matters**: Continuously sources fresh problems from LeetCode/AtCoder/CodeForces. Cannot be in any model's training data if you use post-training-cutoff problems.
- **Versions**:
  - `release_v5`: 880 problems (May 2023 - Jan 2025)
  - `release_v6`: 1055 problems (May 2023 - **Apr 2025**)
- **Scenarios**: code generation, self-repair, test output prediction, code execution.
- **2026 leaderboard top**: Gemini 3 Pro Preview 91.7%, DeepSeek V3.2 89.6%. Open-source 7B class scores in low 30s-40s — Qwen2.5-Coder-7B-Instruct ≈ **37.6% pass@1**.
- **Gap**: Even saturated proprietary models leave >5% headroom; open-source 7B leaves ~50% headroom — perfect for measuring Surrogate-1 progress.

#### SWE-Bench / Verified / Lite

- **SWE-Bench Verified**: 500 tasks human-verified by OpenAI engineers as solvable. Gold standard for "agent fixes real bug."
- **SWE-Bench Lite**: 300 easier instances, designed for compute-constrained iteration.
- **Storage**: Verified ~130 GB Docker images; **Verified Mini ~5 GB**; Lite ~50-60 GB.
- **Compute**: Recommended ≥16 GB RAM, 8 CPU cores, x86_64. **Public Docker registry (July 2025) → SWE-bench Verified now runs in 62 min on a single GitHub Actions VM.**
- **2026 SOTA**: Claude Mythos Preview 0.939 on Verified. Open-source: Refact.ai 59.7% on Lite, Qwen3-Coder family pushing 70%+.
- **Practical for Surrogate-1 v1 (no agent scaffold)**: skip. v3 with proper agent loop: revisit.

#### Aider Polyglot

- **225 Exercism exercises** across C++/Go/Java/JavaScript/Python/Rust.
- **Format**: Edit existing files to pass tests — measures realistic edit-based agentic coding.
- **2026 top**: Claude Opus 4.5 89.4%, GPT-5 (high) 88.0%. DeepSeek V3.2-Exp 74.2% at $1.30/run.
- **Worth running** if Surrogate-1 v2 includes diff/edit format training.

---

## Section 2: DevSecOps-Specific Evaluation

### 2.1 Existing DevSecOps Benchmarks (sparse)

| Benchmark | Domain | Tasks | Year | Status |
|-----------|--------|-------|------|--------|
| **IaC-Eval** (NeurIPS'24) | Terraform / AWS | 458 human-curated | 2024 | Active — uses `terraform plan` for validation |
| **Multi-IaC-Bench** | CloudFormation / Terraform / CDK | 100s | 2025 | New — multi-format |
| **DPIaC-Eval** | Deployability-centric IaC | 153 | 2025 | Tests deploy success + security |
| **CodeFuse DevOps-Eval** | DevOps/AIOps QA | knowledge MCQ + ops | 2024 | Industrial, multilingual |
| **InterCode-Bash** | Bash command generation | 224 from NL2Bash | 2023 | Docker-based execution check |
| **CodeSift Bash dataset** | Bash script gen | 100 | 2024 | Used in ScriptSmith framework |
| **CyberSecEval 1/2/3** (Meta) | Security: insecure code, prompt injection, offensive | hundreds | 2024-2025 | Active, broad scope |
| **CWEval** | CWE-tagged secure code gen | curated | 2024 | Outcome-driven (compile + security check) |
| **SecureAgentBench** | Multi-file secure agentic edits | 105 | 2025 | Realistic, recent |
| **SEC-bench** | Auto-generated sec evals | scalable | 2025 | Programmatic |

**Verdict**: There's no single canonical DevSecOps benchmark. Pick **IaC-Eval** for Terraform, **InterCode-Bash** for shell, and **build a custom set** for Dockerfile/K8s.

### 2.2 Gaps — Build Custom Eval Set

For Surrogate-1's domain (DevSecOps), construct a held-out test set covering:

| Category | Source | Validation Method | Target Size |
|----------|--------|-------------------|-------------|
| Dockerfile correctness | Real apps (Node/Python/Go) — not in training | `docker build` exit 0 + image-size budget | 50 |
| K8s manifest validity | Curated CRDs + standard resources | `kubectl apply --dry-run=server` + `kubeval` | 50 |
| Terraform plan success | Mini-modules (S3/IAM/EC2/VPC) | `terraform validate` + `terraform plan` exit 0 | 50 |
| Bash script behavior | Inspired by InterCode-Bash | Docker exec with FEH (functional equivalence heuristic) | 50 |
| CVE detection / secure code | CWEval-style + Snyk-curated CVEs | Static analyzer (cfn-guard, checkov, semgrep) catches/misses | 50 |
| GitHub Actions YAML | Common CI patterns | `actionlint` + manual schema check | 30 |
| **Total** | | | **280** |

**Construction protocol**:
1. Source from **post-training-cutoff** GitHub repos (Surrogate-1 trained on data up to ~Q1 2026 → use Q2-Q3 2026 commits).
2. **MinHash+LSH overlap check** against training corpus (LLMSanitize library — see Section 4.2).
3. **Manual validation** that each test has a deterministic pass/fail signal (don't trust LLM-as-judge for IaC).
4. **Rotate quarterly** to stay ahead of contamination.

### 2.3 Tooling for DevSecOps Eval Validators

```bash
# Dockerfile
docker build --quiet -t test-img -f Dockerfile.gen .
docker image inspect test-img --format '{{.Size}}'  # budget check

# K8s
kubeval --strict manifest.yaml
kubectl apply --dry-run=server -f manifest.yaml

# Terraform
terraform fmt -check
terraform validate
terraform plan -out=plan.tfplan
checkov -f main.tf  # security
tfsec .

# Bash (InterCode-style)
docker run --rm -v $(pwd):/work alpine sh -c "$GENERATED_BASH"
# Compare stdout/stderr/exit-code to ground truth

# Actions
actionlint .github/workflows/ci.yml

# Generic security
semgrep --config=auto src/
```

---

## Section 3: Eval Frameworks / Harnesses

### 3.1 lm-evaluation-harness (EleutherAI)

**Best for**: General LLM tasks (MMLU, HellaSwag, GSM8K). Code support is limited but supports HumanEval through plugins.

```bash
pip install lm-eval                  # base
pip install "lm-eval[hf]"            # HuggingFace
pip install "lm-eval[vllm]"          # vLLM (recommended for throughput)
pip install "lm-eval[all]"           # everything

# Run HumanEval
lm-eval \
  --model vllm \
  --model_args pretrained=Qwen/Qwen2.5-Coder-7B-Instruct,dtype=bfloat16,gpu_memory_utilization=0.85 \
  --tasks humaneval \
  --batch_size auto \
  --output_path results/qwen-coder-7b/

# Multiple tasks
lm-eval \
  --model hf \
  --model_args pretrained=local/path \
  --tasks humaneval,mbpp,gsm8k \
  --num_fewshot 0
```

**Verdict**: Use for non-code cross-checks (MMLU sanity), not as primary code eval.

### 3.2 bigcode-evaluation-harness

**Best for**: HumanEval, MBPP, MultiPL-E (multilingual variants), DS-1000.

```bash
git clone https://github.com/bigcode-project/bigcode-evaluation-harness
cd bigcode-evaluation-harness
pip install -e .

# Generate + evaluate
accelerate launch main.py \
  --model Qwen/Qwen2.5-Coder-7B-Instruct \
  --tasks humaneval,mbpp \
  --allow_code_execution \
  --do_sample True \
  --temperature 0.2 \
  --top_p 0.95 \
  --n_samples 50 \
  --batch_size 16 \
  --max_length_generation 512 \
  --save_generations \
  --metric_output_path results.json
```

**Standard config**: `temperature=0.2, top_p=0.95, n_samples=50` (BigCode leaderboard standard).

### 3.3 EvalPlus (HumanEval+ / MBPP+)

**Best for**: Rigorous HumanEval+ / MBPP+ eval — **the recommended primary smoke test**.

```bash
pip install --upgrade "evalplus[vllm]"     # with vLLM
# or
pip install --upgrade "evalplus[vllm] @ git+https://github.com/evalplus/evalplus"

# HumanEval+ greedy
evalplus.evaluate \
  --model Qwen/Qwen2.5-Coder-7B-Instruct \
  --dataset humaneval \
  --backend vllm \
  --greedy

# MBPP+ greedy
evalplus.evaluate \
  --model Qwen/Qwen2.5-Coder-7B-Instruct \
  --dataset mbpp \
  --backend vllm \
  --greedy

# Mini variant (faster, fewer tests)
evalplus.evaluate --model <m> --dataset humaneval --mini

# Output: evalplus_results/humaneval/, evalplus_results/mbpp/
# Reports: pass@1 base + pass@1 plus (extended)
```

### 3.4 LiveCodeBench Evaluation Kit

**Best for**: Contamination-resistant evaluation.

```bash
git clone https://github.com/LiveCodeBench/LiveCodeBench
cd LiveCodeBench
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e .

# Run code generation on default release_latest
python -m lcb_runner.runner.main \
  --model Qwen/Qwen2.5-Coder-7B-Instruct \
  --scenario codegeneration \
  --evaluate \
  --use_cache

# Pin to specific version (recommended for reproducibility)
python -m lcb_runner.runner.main \
  --model Qwen/Qwen2.5-Coder-7B-Instruct \
  --scenario codegeneration \
  --evaluate \
  --release_version release_v6

# Self-repair scenario
python -m lcb_runner.runner.main \
  --model Qwen/Qwen2.5-Coder-7B-Instruct \
  --scenario selfrepair \
  --codegen_n 5 --n 1
```

**Recommendation for Surrogate-1**: Use **release_v6** with **only post-training-cutoff problems** (Surrogate-1 base = Qwen2.5-Coder-7B with cutoff ~Sep 2024 → filter `release_v6` for problems dated 2025+).

### 3.5 BigCodeBench

```bash
pip install bigcodebench --upgrade
# GPU acceleration
pip install packaging ninja
pip install flash-attn --no-build-isolation

bigcodebench.evaluate \
  --model Qwen/Qwen2.5-Coder-7B-Instruct \
  --execution local \
  --split instruct \
  --subset hard \
  --backend vllm
```

**Subsets**: `--subset hard` (148 tasks, 4-5 min) or `--subset full` (1140 tasks, 6-7 min on Gradio).

### 3.6 SWE-Bench Harness

```bash
git clone https://github.com/SWE-bench/SWE-bench
cd SWE-bench
pip install -e .

# Step 1: Get model predictions (requires agent scaffolding)
# Use mini-SWE-agent or moatless-tools for unscaffolded LLM

# Step 2: Run harness
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path predictions.json \
  --max_workers 8 \
  --run_id surrogate1-v2-lite \
  --cache_level env
```

**Cost**: ~$30-50 cloud spot for one Verified run; ~5-10 GB Docker per task fetched.

### 3.7 HELM (Stanford CRFM)

```bash
conda create -n crfm-helm python=3.10 pip -y
conda activate crfm-helm
pip install crfm-helm

# Run a "lite" run on a single scenario
helm-run --conf-paths run_specs.conf --suite my-suite --max-eval-instances 100
```

**Verdict**: Powerful but heavy — overkill for Surrogate-1's per-LoRA evaluation cycle. Skip.

### 3.8 HuggingFace Open LLM Leaderboard v2

Not a runnable harness — submission-based. After Surrogate-1 v2 release, can submit for public ranking. Not useful for in-loop dev evaluation.

---

## Section 4: Custom Eval Set Construction

### 4.1 Held-Out Test Set Without Training Overlap

**Principle**: A held-out set that's truly out-of-distribution requires the model to have **no opportunity** to have seen it.

**Strategies (ranked by rigor)**:

1. **Time-cutoff filtering** (best): Use only problems published **after** the model's training data cutoff. For Surrogate-1 (Qwen2.5-Coder-7B base, cutoff Sep 2024 + LoRA training data assembled Q1 2026), use eval problems first published April 2026+.

2. **Synthetic generation with novel API combinations**: Build problems requiring obscure API combinations unlikely in any common corpus. Lower confidence, harder to validate quality.

3. **Hand-curated from private codebases**: Use code from non-public sources (Surrogate-1 author's own non-OSS work). Lowest contamination risk.

4. **Semantic-distance filtering**: For each candidate eval problem, compute MinHash similarity vs training corpus, drop above threshold (Section 4.2).

### 4.2 Memorization / Contamination Detection

#### MinHash + LSH Overlap

```bash
pip install datasketch
```

```python
from datasketch import MinHash, MinHashLSH

# Build LSH index over training corpus (n-gram shingled)
lsh = MinHashLSH(threshold=0.7, num_perm=128)
for doc_id, doc_text in training_corpus_iter():
    m = MinHash(num_perm=128)
    for shingle in get_ngrams(doc_text, n=10):  # 10-grams
        m.update(shingle.encode())
    lsh.insert(doc_id, m)

# Check eval problems for overlap
for prob in eval_set:
    m = MinHash(num_perm=128)
    for shingle in get_ngrams(prob.code, n=10):
        m.update(shingle.encode())
    matches = lsh.query(m)
    if matches:
        print(f"CONTAMINATED: {prob.id} matches {matches}")
```

**Reference**: Qwen2.5-Coder team filters training data with **10-gram collision** detection vs HumanEval/MBPP/GSM8K/MATH.

#### LLMSanitize Library

```bash
pip install llmsanitize  # multiple detection methods bundled
```

Includes Min-K% Prob, Guided Prompting, n-gram overlap. Good for sanity-checking that a candidate eval set doesn't trigger memorization signals on the base model.

#### Min-K% Probability Detection

For each test problem, compute the average log-probability of the bottom K% (e.g., K=20) of tokens. Memorized examples have unusually-high log-prob on rare tokens.

```python
# Pseudocode
def min_k_prob(model, text, k=0.2):
    log_probs = compute_per_token_log_prob(model, text)
    sorted_lp = sorted(log_probs)
    bottom_k = sorted_lp[:int(len(sorted_lp) * k)]
    return sum(bottom_k) / len(bottom_k)

# High score = likely memorized
```

### 4.3 LLM-as-Judge Frameworks

| Framework | What | Bias profile | Cost |
|-----------|------|--------------|------|
| **MT-Bench** | 80 multi-turn questions, judged by GPT-4 | Position, verbosity, self-enhancement biases documented (Zheng et al. 2023) | ~$0.50/model with GPT-4 |
| **AlpacaEval 2** | Pairwise win-rate vs reference (GPT-4 baseline) | Output randomization to mitigate position bias | ~$0.30/model |
| **Arena-Hard-Auto** | 500 challenging prompts, GPT-4 judge | Best correlation with Chatbot Arena Elo | Higher cost |
| **WildBench** | Real user prompts | Diverse, less academic | Mid |

**Documented biases (Zheng et al. 2023, "Judging LLM-as-a-Judge")**:
- **Position bias**: ~25% verdict flip when swapping A/B positions. Mitigation: dual-evaluation with swapped positions, average.
- **Verbosity bias**: longer answers preferred even if not better. Mitigation: word-count-controlled sampling.
- **Self-enhancement**: GPT-4 favors itself by ~10%, Claude-v1 by ~25%. Mitigation: cross-judge (use both Claude and GPT-4, average).

**For Surrogate-1**: Avoid LLM-as-judge as the **primary metric**. Use only as supplementary signal for non-deterministic outputs (code review quality, explanation clarity). Never use the same family as judge (don't let Claude evaluate Surrogate-1 if Surrogate-1 was distilled from Claude).

### 4.4 Pairwise Comparison Protocols

For comparing v1 vs v2 LoRA when no ground-truth pass/fail:

1. Sample N prompts (≥100 for stable signal).
2. For each prompt, generate output from both v1 and v2.
3. Present to judge (Claude Opus + GPT-5) **with positions randomized**.
4. Aggregate: win-rate, tie-rate, loss-rate per category.
5. Bootstrap CI (1000 resamples) for statistical confidence.

```python
# Pseudocode
import random
results = []
for prompt in prompts:
    out_v1 = run_model("v1", prompt)
    out_v2 = run_model("v2", prompt)
    a, b = ((out_v1, "v1"), (out_v2, "v2"))
    if random.random() < 0.5:
        a, b = b, a
    verdict = judge(prompt, a[0], b[0])  # "A", "B", "tie"
    winner = a[1] if verdict == "A" else b[1] if verdict == "B" else "tie"
    results.append(winner)
```

### 4.5 GPT-4-Judge vs Claude-Judge Bias

| Judge | Self-bias | Best for | Worst for |
|-------|-----------|----------|-----------|
| GPT-5/GPT-4o | ~10% favors GPT-family outputs | Code correctness review | Anthropic-distilled outputs |
| Claude Opus 4.5 | ~25% favors Claude-family | Long-form explanation, reasoning | OpenAI-distilled outputs |
| Gemini 3 Pro | smaller documented self-bias | Diverse perspectives | Newer, less validated |

**Best practice**: Use **two judges** from different families, take majority. Discard if they disagree (treat as tie).

---

## Section 5: pass@k Metric in Detail

### 5.1 Unbiased Estimator

Original formula (Chen et al. 2021, Codex paper):

```
pass@k = E_problems [ 1 - C(n - c, k) / C(n, k) ]
```

Where:
- `n` = total samples per problem
- `c` = number of correct samples among `n`
- `k` = top-k threshold being evaluated
- `C(a, b)` = binomial coefficient (a choose b)

Constraint: **`n ≥ k`** (else estimator is undefined).

**Macro-average across problems** to get the final score.

### 5.2 Sample Size Requirements

| pass@k | Recommended n | Rationale |
|--------|---------------|-----------|
| pass@1 (greedy) | 1 | Use temperature=0 |
| pass@1 (sampled) | 10-20 | Smooths sampling noise |
| pass@10 | 50-100 | Original Codex used n=200 |
| pass@100 | 200+ | Original Codex used n=200 |

Standard BigCode harness default: **n=50, T=0.2, top_p=0.95**.

### 5.3 Temperature & Sampling

| Setting | Use case |
|---------|----------|
| T=0 (greedy) | pass@1 reproducibility — preferred for leaderboard reporting |
| T=0.2, top_p=0.95 | BigCode standard for pass@k with k>1 |
| T=0.8, top_p=0.95 | Higher diversity for pass@100 |

### 5.4 Practical Calculation

```python
import numpy as np

def pass_at_k(n, c, k):
    """Unbiased estimator from Codex paper."""
    if n - c < k:
        return 1.0
    return 1.0 - np.prod(1.0 - k / np.arange(n - c + 1, n + 1))

# Example: 100 samples, 30 correct, k=10
print(pass_at_k(100, 30, 10))  # ≈ 0.99
```

### 5.5 Error Bars

Bootstrap standard error per problem, then propagate:

```python
def pass_at_k_with_error(results, k, n_bootstrap=1000):
    # results: list of (n, c) per problem
    boot_scores = []
    for _ in range(n_bootstrap):
        sampled = np.random.choice(len(results), size=len(results), replace=True)
        score = np.mean([pass_at_k(*results[i], k) for i in sampled])
        boot_scores.append(score)
    return np.mean(boot_scores), np.std(boot_scores)
```

### 5.6 What's a Good pass@1 for 7B Coder Models?

| Score | Interpretation | Example models |
|-------|----------------|----------------|
| < 30% | Weak / generalist | Llama 3.1 8B base (~30%) |
| 30-50% | Solid generalist or weak coder | StarCoder2-7B (34%), Granite-8B-Code-Base (34.5%) |
| 50-70% | Mid-tier coder | DeepSeek-Coder 6.7B (~70%), OpenCoder-8B (64.6%) |
| 70-85% | Strong specialist | Qwen2.5-Coder-7B-Instruct (~84% HE+) |
| 85-92% | Top-tier 7B class | (rare — usually requires 14B+) |
| > 92% | Frontier 30B+ or proprietary | Claude/GPT-5/Qwen3-Coder-Next |

**Surrogate-1 v1 zone (LoRA on Qwen2.5-Coder-7B)**: should be within ±2% of base (84-86% HE+). Significant improvement (>5%) on HE+ would be **suspicious** (overfitting / contamination).

---

## Section 6: Real Numbers — Top 20 Open-Source Coder Models

### 6.1 Aggregated Leaderboard (Late 2025 / Early 2026)

| Rank | Model | Params | HumanEval | HE+ | MBPP | MBPP+ | LiveCodeBench | SWE-Bench V | Notes |
|------|-------|--------|-----------|-----|------|-------|---------------|-------------|-------|
| 1 | Qwen3-Coder-Next | ~480B MoE / 35B active | ~96% | ~94% | ~93% | ~90% | ~70% | ~65% | 2026-04 SOTA OSS |
| 2 | Qwen3-Coder 480B/A35B Instruct | 480B MoE | 95% | 92% | 92% | 88% | 65% | 61% | |
| 3 | GLM-4.6 / GLM-5 | 270B+ | 93% | 90% | 91% | 86% | 60% | 55% | Z.ai |
| 4 | Kimi K2 Thinking | ~30B+ | 93% | 90% | 90% | 86% | 65% | 60% | Best Pass@1 on SWE Verified (OSS) |
| 5 | DeepSeek-V3 (V3.2-Speciale) | 671B MoE / 37B active | 93% | 89% | 91% | 86% | ~70% (V3.2) | ~60% | |
| 6 | DeepSeek-Coder-V2-Instruct | 236B MoE / 21B active | 90.2% | 86% | 89% | 81% | 50% | 42% | |
| 7 | Qwen2.5-Coder-32B-Instruct | 32B | 92% | **87.2%** | 90% | 76% | 50% | 35% | Matches GPT-4o (2024) |
| 8 | Codestral 22B (25.08) | 22B | 86.6% | ~78% | 91.2% | ~82% | ~40% | ~25% | Mistral |
| 9 | Codestral 22B (original) | 22B | 78.1% | 71% | 81% | 71% | 30% | 18% | |
| 10 | Llama 3.3 70B Instruct | 70B | 88.4% | 82% | 87.6% | 80% | 35% | 25% | Generalist |
| 11 | Devstral 2512 | ~24B | 84% | 78% | 85% | 76% | 40% | 30% | Mistral devops-leaning |
| 12 | Qwen2.5-Coder-14B-Instruct | 14B | 89.1% | 83% | 86.8% | 75% | 42% | 25% | |
| 13 | Qwen2.5-Coder-7B-Instruct | 7B | **88.4%** | **84.1%** | 83.5% | 75% | **37.6%** | ~17% | **Surrogate-1 v1 base** |
| 14 | DeepSeek-Coder-V2-Lite-Instruct | 16B MoE / 2.4B | 81.1% | 76% | 75.4% | 65% | 25% | 12% | |
| 15 | OpenCoder-8B-Instruct | 8B | 78% | 71% | 79% | 67% | 22% | 8% | Reproducible, open data |
| 16 | OpenCoder-1.5B-Instruct | 1.5B | 64.6% (base) | 55% | 65% | 53% | 12% | 3% | |
| 17 | Granite-8B-Code-Instruct | 8B | 62.2% | 54% | 65% | 55% | 10% | 4% | IBM, Apache 2.0 |
| 18 | DeepSeek-Coder-6.7B-Instruct | 6.7B | 78.6% | 70% | 65.4% | 52% | 18% | 6% | 2023 model |
| 19 | StarCoder2-15B-Instruct | 15B | 46% | 38% | 65% | 47% | 8% | 2% | Base + light SFT |
| 20 | StarCoder2-7B | 7B | 34.1% | 25% | 47% | 38% | 5% | 1% | Pre-trained only |

**Sources**: EvalPlus leaderboard, BigCode leaderboard, LiveCodeBench leaderboard, Qwen2.5-Coder Technical Report (arXiv:2409.12186), official model cards, llm-stats.com aggregated.

### 6.2 Surrogate-1 v1 Target Zone

Base = **Qwen2.5-Coder-7B-Instruct** → expected v1 LoRA scores:

| Metric | Base | v1 LoRA expected | v1 reality (TBD — never measured) | v2 target |
|--------|------|------------------|-----------------------------------|-----------|
| HumanEval | 88.4% | 86-90% | not measured | 87%+ |
| HumanEval+ | 84.1% | 82-86% | not measured | 84%+ |
| MBPP+ | 75% | 73-77% | not measured | 75%+ |
| LiveCodeBench v6 (2025+ filter) | ~37.6% | 35-39% | not measured | **40%+** |
| BigCodeBench Hard | ~25% | 23-27% | not measured | **27%+** |
| Custom DevSecOps eval | n/a | n/a | n/a | **establish baseline → +15%** |

**Key principle**: For LoRA on a strong specialist base, gains come from **domain shift** (DevSecOps), not raw HumanEval. Track LiveCodeBench (progress) + custom DevSecOps eval (specialization).

---

## Section 7: Eval Time / Cost on Free Tier

### 7.1 Lightning AI Free Tier (Surrogate-1 Constraint)

- **22 GPU-hours/month** free (basic) / **35 GPU-hours/month** with student status
- **15 free credits/month** ($1/credit) for additional bursts
- Default GPU: T4 (16 GB) or upgrade to L4 (24 GB) on credits

### 7.2 Per-Benchmark Time Budget (Qwen2.5-Coder-7B class on T4)

| Benchmark | Tasks | Samples/task | Generation time | Eval time | **Total** |
|-----------|-------|--------------|-----------------|-----------|-----------|
| HumanEval (greedy) | 164 | 1 | ~10 min | <1 min | **~12 min** |
| HumanEval+ greedy | 164 | 1 | ~10 min | ~2 min | **~15 min** |
| HumanEval+ pass@10 (n=20) | 164 | 20 | ~3 hrs | ~5 min | **~3 hrs** |
| MBPP+ greedy | 378 | 1 | ~25 min | ~3 min | **~30 min** |
| BigCodeBench-Hard greedy | 148 | 1 | ~12 min | ~5 min (sandbox) | **~20 min** |
| BigCodeBench-Full greedy | 1140 | 1 | ~90 min | ~30 min | **~2 hrs** |
| LiveCodeBench v6 greedy | 1055 | 1 | ~80 min | ~10 min | **~90 min** |
| LiveCodeBench v6 (post-cutoff filter, ~300) | ~300 | 1 | ~25 min | ~5 min | **~30 min** |
| Aider Polyglot | 225 | 1 | ~3 hrs (multi-lang exec) | (in-loop) | **~3 hrs** |
| **SWE-Bench Lite** | 300 | 1 | n/a (needs agent) | ~2-4 hrs Docker | **5-8 hrs + storage 60 GB** |
| **SWE-Bench Verified** | 500 | 1 | n/a (needs agent) | 4-8 hrs Docker | **10-16 hrs + storage 130 GB** |

### 7.3 Practical Pipeline for Free Tier (~22-35 GPU-hrs/mo)

**Per-LoRA-checkpoint evaluation budget: 1.5-2 GPU-hrs**

```
1. EvalPlus HumanEval+ greedy           ~15 min  (sanity)
2. EvalPlus MBPP+ greedy                ~30 min  (sanity)
3. LiveCodeBench v6 post-cutoff (~300)  ~30 min  (contamination-resistant primary metric)
4. BigCodeBench-Hard greedy             ~20 min  (realism)
5. Custom DevSecOps eval (~280 tasks)   ~25 min  (specialization)
                                        ───────
                                        ~2 hrs   (T4)
```

**Monthly capacity**: 22 hrs / 2 hrs per checkpoint = **~11 evaluation runs**. Sufficient for v2 dev iteration.

### 7.4 SWE-Bench Lite Budget (Stretch Goal — v3)

- Storage: 60 GB Docker images (one-time fetch, ~1 hr)
- Per-run: 5-8 hours Docker eval
- Plus agent inference: ~$10-30 OpenAI API for proper SWE agent (or self-hosted vLLM time)
- **Skip for v2**, plan for v3 with agent scaffolding

---

## Section 8: Recommended Pipeline for Surrogate-1 v1→v2→v3

### 8.1 Phase 1 (v1 retrospective baseline) — establish numbers TODAY

Run on Lightning T4 (~3 GPU-hrs total):

```bash
# Setup
pip install --upgrade "evalplus[vllm]"
git clone https://github.com/LiveCodeBench/LiveCodeBench && cd LiveCodeBench
uv pip install -e .
pip install bigcodebench

# Baseline: Qwen2.5-Coder-7B-Instruct (raw)
evalplus.evaluate --model Qwen/Qwen2.5-Coder-7B-Instruct --dataset humaneval --backend vllm --greedy
evalplus.evaluate --model Qwen/Qwen2.5-Coder-7B-Instruct --dataset mbpp --backend vllm --greedy
python -m lcb_runner.runner.main --model Qwen/Qwen2.5-Coder-7B-Instruct --scenario codegeneration --evaluate --release_version release_v6
bigcodebench.evaluate --model Qwen/Qwen2.5-Coder-7B-Instruct --execution local --split instruct --subset hard --backend vllm

# Surrogate-1 v1 LoRA (merged or with adapter)
# Repeat all four with --model surrogate-1-v1-merged
```

**Output**: 5 numbers (HE+, MBPP+, BCB-Hard, LCB-v6, custom-DevSecOps) for both base and v1.

### 8.2 Phase 2 (v2 in development) — use full pipeline as in-loop signal

Same 4 standard benchmarks + custom eval, run per checkpoint.

### 8.3 Phase 3 (v3 with agent scaffolding) — add SWE-Bench Lite

Build minimal agent loop (mini-SWE-agent style) and run SWE-Bench Lite on monthly cadence (storage + time budget).

### 8.4 Statistical Rigor — Avoid False Progress Claims

- Always report **3-seed mean ± std** for non-greedy evaluations.
- For pass@1 greedy: deterministic, no error bars needed.
- For LLM-as-judge: bootstrap 1000 resamples for win-rate CI.
- **Reject** any v2 claim of "improvement" unless it exceeds 2x the std-dev of v1.

---

## Section 9: Top Picks Summary (for quick reference)

### Tier 1 — RUN EVERY CHECKPOINT (in-loop)

| Benchmark | Why | Command (one-liner) | Time | Target |
|-----------|-----|---------------------|------|--------|
| **EvalPlus HumanEval+** | Fast smoke; differentiates 7B class | `evalplus.evaluate --model M --dataset humaneval --backend vllm --greedy` | 15 min | **≥84% (v1 base)** |
| **EvalPlus MBPP+** | Pairs with HE+ | `evalplus.evaluate --model M --dataset mbpp --backend vllm --greedy` | 30 min | **≥75%** |
| **LiveCodeBench v6 (post-cutoff filter)** | Contamination-resistant, primary progress metric | `python -m lcb_runner.runner.main --model M --scenario codegeneration --evaluate --release_version release_v6` | 30-90 min | **≥38% v1 → ≥42% v2** |

### Tier 2 — RUN MONTHLY

| Benchmark | Why | Command | Time |
|-----------|-----|---------|------|
| **BigCodeBench Hard** | Realistic library use | `bigcodebench.evaluate --model M --subset hard --split instruct --backend vllm` | 20 min |
| **Custom DevSecOps eval (280 tasks)** | Domain specialization | Custom harness — Dockerfile/K8s/TF/Bash validators | 25 min |

### Tier 3 — RUN PER-RELEASE (v2 final, v3 final)

| Benchmark | Why | Time |
|-----------|-----|------|
| **SWE-Bench Lite** (300 tasks) | Real GitHub issues | 5-8 hrs + agent setup |
| **Aider Polyglot** | Multi-lang edit | 3 hrs |
| **EvalPlus HumanEval+ pass@10 (n=20)** | Diversity check | 3 hrs |

---

## Sources

- LiveCodeBench leaderboard: https://livecodebench.github.io/leaderboard.html
- LiveCodeBench paper / repo: https://github.com/LiveCodeBench/LiveCodeBench
- SWE-bench leaderboards: https://www.swebench.com/
- SWE-bench Verified (OpenAI): https://openai.com/index/introducing-swe-bench-verified/
- EvalPlus leaderboard: https://evalplus.github.io/leaderboard.html
- EvalPlus repo: https://github.com/evalplus/evalplus
- BigCodeBench: https://github.com/bigcode-project/bigcodebench
- bigcode-evaluation-harness: https://github.com/bigcode-project/bigcode-evaluation-harness
- BigCodeBench HF blog: https://huggingface.co/blog/leaderboard-bigcodebench
- BigCode models leaderboard: https://huggingface.co/spaces/bigcode/bigcode-models-leaderboard
- lm-evaluation-harness (EleutherAI): https://github.com/EleutherAI/lm-evaluation-harness
- HELM: https://github.com/stanford-crfm/helm
- Aider polyglot benchmark: https://aider.chat/docs/leaderboards/ + https://github.com/Aider-AI/polyglot-benchmark
- IaC-Eval (NeurIPS'24): https://github.com/autoiac-project/iac-eval
- Multi-IaC-Eval: https://arxiv.org/abs/2509.05303
- CyberSecEval (Meta): broader Llama paper series
- ScriptSmith / InterCode-Bash: https://arxiv.org/abs/2409.17166 + https://arxiv.org/pdf/2306.14898
- LLM-as-Judge (MT-Bench paper): https://arxiv.org/abs/2306.05685
- pass@k unbiased estimator (Codex paper): Chen et al. 2021
- LLMSanitize contamination detection: https://github.com/ntunlp/LLMSanitize
- Qwen2.5-Coder Technical Report: https://arxiv.org/abs/2409.12186
- Llama 3.3 evals: https://huggingface.co/datasets/meta-llama/Llama-3.3-70B-Instruct-evals
- Refact.ai (open SWE-bench Lite SOTA early 2025): https://refact.ai/blog/2025/open-source-sota-on-swe-bench-verified-refact-ai/
- Lightning AI free tier: https://lightning.ai/pricing/
- SWE-bench Docker registry (62 min on Actions VM, July 2025): https://epoch.ai/blog/swebench-docker

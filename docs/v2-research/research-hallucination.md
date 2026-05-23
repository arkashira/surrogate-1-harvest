---
title: "SOTA Hallucination Reduction for Code LLMs (2025-2026) — Research for Surrogate-1 v2"
date: 2026-04-29
project: surrogate1
phase: honest-audit
purpose: "Drive Surrogate-1 v2 to LOWEST possible hallucination rate (compile-OK, verifiable, no invented APIs)"
target_model: "Qwen2.5-Coder-7B + LoRA"
v1_problem: "Training data leak in eval; phantom APIs/imports; over-confident wrong answers"
tags: [hallucination, code-llm, decoding, dpo, rlef, cove, constitutional-ai, abstention, calibration, rag, surrogate-1]
---

# SOTA Hallucination Reduction for Code LLMs (2025-2026)

> **Mission**: Surrogate-1 must hallucinate the LEAST possible. Every line must compile, every claim verifiable, no phantom APIs/imports/packages.
>
> **Threat model for v1**:
> 1. Made-up imports (`from ai.utils import magic_solver`)
> 2. Phantom function signatures (wrong arg order, fake kwargs)
> 3. Slopsquatting / package hallucinations (19.7% in open OSS models per USENIX'25)
> 4. Over-confident wrong answers (no abstention)
> 5. Memorized training data leak (mistaken for "knowledge")
>
> **Strategy**: Stack defense in depth — decoding-time + training-time + retrieval + verification.

---

## Section 1 — Decoding-Time Hallucination Control

Decoding-time interventions are **cheapest, most transferable, no retrain needed**. Apply first, measure, then go deeper.

### 1.1 DoLa — Decoding by Contrasting Layers

**Paper**: Chuang et al., "DoLa: Decoding by Contrasting Layers Improves Factuality in Large Language Models" (ICLR 2024) — [arxiv 2309.03883](https://arxiv.org/abs/2309.03883)

**Mechanism**: Contrast logits from a final "mature" layer vs an earlier "premature" layer. Factual knowledge is localized in higher layers; subtracting the lower-layer distribution sharpens the final probability toward facts. No external KB, no fine-tuning.

**Numbers**: TruthfulQA +12-17% absolute on LLaMA family (no training).

**transformers >= 4.56 API** (custom_generate hook):

```python
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-Coder-7B-Instruct")
model = AutoModelForCausalLM.from_pretrained(
    "Qwen/Qwen2.5-Coder-7B-Instruct",
    torch_dtype=torch.bfloat16,
    device_map="auto",
)

# Surrogate-1 use: short factual code answers → "high" layer contrast
outputs = model.generate(
    **inputs,
    max_new_tokens=512,
    do_sample=False,
    custom_generate="transformers-community/dola",
    trust_remote_code=True,
    dola_layers="high",          # short answers (def names, imports)
    repetition_penalty=1.2,
)

# For long-form reasoning (algorithm explanation) → "low"
outputs_reasoning = model.generate(
    **inputs,
    max_new_tokens=2048,
    do_sample=False,
    custom_generate="transformers-community/dola",
    trust_remote_code=True,
    dola_layers="low",
    repetition_penalty=1.2,
)

# Targeted layers (Qwen2.5-Coder-7B has 28 layers → contrast layer 18 vs 20 head)
outputs_targeted = model.generate(
    **inputs,
    custom_generate="transformers-community/dola",
    trust_remote_code=True,
    dola_layers=[18, 20],
)
```

**Caveat**: DoLa not implemented in vLLM as of 2026-04 — use transformers-community port only. For prod inference at scale: re-implement as a logits processor.

**Active Layer-Contrastive Decoding (ActLCD, May 2025)** — [arxiv 2505.23657](https://arxiv.org/pdf/2505.23657): Reframes "when to contrast" as RL policy. Sequence-level factuality > static token decisions. Higher composite truth score, lower hallucination than vanilla DoLa.

### 1.2 Contrastive Decoding (CD) family

**Speculative Contrastive Decoding (SCD)** — [ACL 2024](https://aclanthology.org/2024.acl-short.5/): leverage small "amateur" model for both speedup AND factuality (subtract amateur logits from expert).

**Uncertainty-Aware Contrastive Decoding (UCD, ACL 2025)** — [aclanthology 2025.findings-acl.1352](https://aclanthology.org/2025.findings-acl.1352/): dynamically adjust contributions per-token by uncertainty.

**LayerCake (July 2025)** — [arxiv 2507.04404](https://arxiv.org/pdf/2507.04404): token-aware contrastive decoding within layers.

**Configuration for Surrogate-1**:
- Amateur: Qwen2.5-Coder-0.5B (same family, weak)
- Expert: Qwen2.5-Coder-7B + LoRA
- Subtract amateur logits scaled by α=0.5

```python
# Pseudo for vLLM logits_processor (2026-04 vLLM 0.7+)
class ContrastiveLogitsProcessor:
    def __init__(self, amateur_model, alpha=0.5, plausibility_threshold=0.1):
        self.amateur = amateur_model
        self.alpha = alpha
        self.tau = plausibility_threshold

    def __call__(self, token_ids, logits):
        with torch.no_grad():
            amateur_logits = self.amateur.forward(token_ids).logits[-1]
        # Plausibility constraint: only contrast where expert is confident
        max_p = logits.softmax(-1).max()
        if max_p < self.tau * logits.softmax(-1).max():
            return logits
        return logits - self.alpha * amateur_logits
```

### 1.3 Constrained / Grammar-Guided Decoding

This is the **highest-leverage decoding fix for code**. Forces output to be syntactically valid — eliminates whole classes of hallucination (invalid imports, syntax errors).

**XGrammar** (default in vLLM, SGLang, TensorRT-LLM as of March 2026) — [arxiv 2411.15100](https://arxiv.org/pdf/2411.15100):
- Pushdown automaton on top of CFG
- Context-independent (99% of tokens) precomputed bitmasks
- < 40 µs per token, 5x TPOT improvement vs prior
- Default backend in vLLM since v0.7

**llguidance** (Microsoft, used by OpenAI Structured Outputs May 2025) — [github guidance-ai/llguidance](https://github.com/guidance-ai/llguidance):
- ~50 µs CPU/token for 128k vocab
- Lark grammars + JSON schema + regex
- SGLang integration since v0.4.4

**Outlines** (Python, broad coverage) — [outlines pypi](https://pypi.org/project/outlines/):

```python
import outlines

# Code-focused: constrain Python class signature
schema = """
class_name: /[A-Z][a-zA-Z0-9_]+/
methods: list[Method]

Method:
    name: /[a-z_][a-z0-9_]*/
    args: list[/[a-z_][a-z0-9_]*/]
    body: str
"""

model = outlines.models.transformers("Qwen/Qwen2.5-Coder-7B-Instruct")
generator = outlines.generate.cfg(model, grammar=schema)
result = generator(prompt)
```

**JSON mode for tool calls** — Pydantic schema → grammar:

```python
from pydantic import BaseModel
import outlines

class CodeFix(BaseModel):
    file_path: str
    line_number: int
    fix_type: Literal["import", "rename", "type", "logic"]
    new_code: str
    confidence: float  # 0..1

model = outlines.models.vllm("Qwen/Qwen2.5-Coder-7B-Instruct")
generator = outlines.generate.json(model, CodeFix)
fix = generator(prompt)  # guaranteed valid CodeFix instance
```

**lm-format-enforcer** — runtime regex enforcement, simpler than CFG, supports JSON Schema directly. Use when full CFG overkill.

**SGLang structured output** — supports llguidance + xgrammar, integrates cleanly:

```python
# SGLang server with structured output
import sglang as sgl

@sgl.function
def code_gen(s, problem):
    s += "Problem: " + problem + "\n"
    s += "Solution:\n"
    s += sgl.gen("code", max_tokens=512,
                 regex=r"```python\n[\s\S]+?\n```")
```

**GBNF (llama.cpp grammar)** — useful for self-hosted Qwen GGUF on Mac M3:

```bnf
root   ::= function
function ::= "def " name "(" params "):\n" body
name   ::= [a-z_][a-z0-9_]*
params ::= param ("," param)*
param  ::= name (":" type)?
type   ::= "int" | "str" | "float" | "bool" | "List[" type "]"
body   ::= "    " statement+
```

**v2 plan**: XGrammar is the path. Default vLLM backend, free 5x throughput, near-zero hallucination on structure.

### 1.4 Speculative Decoding for Quality (not just speed)

**Eagle-3 + vLLM** (July 2025 Red Hat blog): primary speed win, but **also** acts as a small consistency check — if speculator and verifier disagree wildly, flag uncertainty. Could become an honesty signal.

**RASD (Retrieval-Augmented Speculative Decoding, ACL 2025)** — speculator drafts from RAG context; verifier accepts only RAG-grounded drafts → naturally reduces hallucinated APIs.

### 1.5 Activation Steering for Honesty

**Adaptive Activation Steering (ACT, WWW 2025)** — [dl.acm.org/doi/10.1145/3696410.3714640](https://dl.acm.org/doi/10.1145/3696410.3714640):
- Tuning-free
- Shifts activations along "truthful direction" at inference
- Adaptive steering intensity per category of hallucination

**Conditional Activation Steering (CAST, ICLR 2025)**: only steer when input matches a learned condition vector → avoids "always-on" degradation.

**Semantics-Adaptive Dynamic Intervention (SADI)**: contrastive pairs identify steering targets.

**Applicability to Surrogate-1**:
- Build steering vectors from `(grounded_answer, hallucinated_answer)` pairs
- Apply only on coding-prompt-shaped inputs (CAST-style condition gating)
- Mac M3 24GB: feasible at inference, < 1ms overhead per token

```python
# Sketch — apply per-layer steering vector
class SteeringHook:
    def __init__(self, layer_idx, steering_vector, alpha=2.0):
        self.layer_idx = layer_idx
        self.v = steering_vector       # truthful_dir - hallucinated_dir
        self.alpha = alpha

    def __call__(self, module, input, output):
        h = output[0]
        return (h + self.alpha * self.v,) + output[1:]

# Hook into Qwen2.5-Coder-7B layer 22
hook = model.model.layers[22].register_forward_hook(
    SteeringHook(22, steering_v, alpha=2.0))
```

---

## Section 2 — Self-Consistency, Verification, Self-Correction

Inference-time multi-sample techniques. Cost: 3-10x compute. Benefit: massive gains on factuality.

### 2.1 Chain of Verification (CoVe)

**Paper**: Dhuliawala et al., "Chain-of-Verification Reduces Hallucination" (ACL Findings 2024) — [arxiv 2309.11495](https://arxiv.org/abs/2309.11495)

**Pipeline** (4 steps):
1. **Draft** — model writes initial answer
2. **Plan verification questions** — break draft into atomic factual claims; one Q per claim
3. **Independent answers** — answer each Q in fresh context (no draft visible) to avoid bias
4. **Final synthesis** — rewrite using only verified facts

**Reduction**: 60-70% hallucination reduction on Wikidata list QA, MultiSpanQA, longform.

**Code-specific CoVe template** (Surrogate-1 v2):

```
[STEP 1 — DRAFT]
You are Surrogate-1. Solve this coding task. Write code with reasoning.
Task: {task}

[STEP 2 — VERIFICATION QUESTIONS]
Below is a candidate solution. List ATOMIC factual claims and turn each
into a verification question. Examples of claims:
  - "asyncio.gather accepts return_exceptions kwarg"
  - "torch.nn.functional.scaled_dot_product_attention exists since PyTorch 2.0"
  - "The numpy.linalg.lstsq returns a 4-tuple (solution, residuals, rank, sv)"

For each claim, write a verification question that can be answered by:
  (a) executing a Python snippet, OR
  (b) reading official docs.

Draft: {draft}

[STEP 3 — INDEPENDENT VERIFICATION]
For each question, answer using ONLY tool results (run_python, doc_lookup).
Do NOT consult the draft. Mark each: VERIFIED / FALSIFIED / UNKNOWN.

[STEP 4 — FINAL ANSWER]
Rewrite the solution using ONLY VERIFIED claims.
- If a claim was FALSIFIED → fix the code.
- If a claim was UNKNOWN → either prove it (run code) or say "I don't know"
  and ask for clarification.
- Cite each non-trivial API with a doc-URL or a runnable snippet.
```

**Implementation pattern**:

```python
def code_cove(task, model, sandbox):
    draft = model.generate(f"Solve: {task}")
    questions = model.generate(f"Extract verification Qs from draft:\n{draft}",
                               schema=List[VerifQ])
    verifications = []
    for q in questions:
        if q.kind == "execution":
            result = sandbox.run(q.snippet)
            verifications.append((q, result))
        elif q.kind == "doc":
            doc = doc_lookup(q.api)
            verifications.append((q, doc))
    final = model.generate(
        f"Final answer using only VERIFIED facts:\n"
        f"Task: {task}\nDraft: {draft}\n"
        f"Verifications: {verifications}",
    )
    return final
```

### 2.2 Self-Consistency

**Method**: Sample N=5..40 responses; majority-vote (or LLM-judge) the answer. Robust against random hallucinations.

**Code variant**: pass-rate vote — sample N codes, run each against unit tests, return the one with most passing tests.

```python
def code_self_consistency(task, n=8, temperature=0.8):
    candidates = [model.generate(task, temperature=temperature) for _ in range(n)]
    scored = []
    for code in candidates:
        result = sandbox.run_tests(code, task.tests)
        scored.append((code, result.pass_count, result.total))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[0][0]  # best by pass count
```

**Self-Consistency for VLM (2025)** — [arxiv 2509.23236](https://arxiv.org/abs/2509.23236): self-consistency between long-answer and short-answer used as preference signal for DPO. Free supervision.

### 2.3 Reflexion / Self-Refine

**Reflexion (NeurIPS 2023)**: verbal RL — model writes a "lesson" after each failed trial; uses lessons in next attempt.

**Self-Refine (NeurIPS 2023)**: model critiques own output → revises. No outside feedback needed.

**Towards Mitigating Hallucination via Self-Reflection (EMNLP 2023)** — [arxiv 2310.06271](https://arxiv.org/abs/2310.06271): interactive self-reflection loop with knowledge acquisition + answer generation. Strong in medical QA.

**Bake into training**: ReTrace dataset (200K self-correction examples bootstrapped from teacher) → SFT a "generate→critique→refine" trace as one continuous output. Removes inference-time critic. Match Reflexion's quality without 3-pass overhead.

**Surrogate-1 v2 plan**: train on synthetic CoVe traces (draft→Qs→verifications→final), so single forward pass produces the full trace. Inference cost = 1.3x base, hallucination rate dropped 30-50% in published variants.

### 2.4 Tree of Thoughts / Graph of Thoughts

**ToT (NeurIPS 2023)**: branching reasoning, evaluator score per branch, backtrack when bad.

**Limitation per recent surveys (2025)**: GoT achieves +20-25% on financial reasoning, -25-30% hallucination, but **doesn't eliminate** hallucinated leaves — propagation risk if evaluator weak.

**For code**: ToT is overkill if you have unit tests; better to spend the compute on more samples + execution feedback (Section 7).

---

## Section 3 — Constitutional AI for Code

### 3.1 Background

**Constitutional AI (Bai et al., Anthropic, 2022)** — [arxiv 2212.08073](https://arxiv.org/abs/2212.08073): replace RLHF with "self-critique using a constitution" → RL from AI Feedback (RLAIF).

Process:
1. SL phase: model writes responses → critiques them with constitution → revises → SFT on revisions
2. RL phase: model generates pairs → constitution-based AI judges → preference data → PPO/DPO

### 3.2 2025 Refinements

**C3AI (WWW 2025)** — [dl.acm.org/doi/10.1145/3696410.3714705](https://dl.acm.org/doi/10.1145/3696410.3714705): positively framed, behavior-based principles align better with humans than negatively framed/trait-based.

**Inverse Constitutional AI (Jan 2025)** — [arxiv 2501.17112](https://arxiv.org/abs/2501.17112): extract constitution from preference dataset (decode what the data implies).

**Constitution or Collapse (April 2025)** — [arxiv 2504.04918](https://arxiv.org/html/2504.04918v1): smaller models risk model collapse from recursive self-critique. Dilute self-data with teacher data.

### 3.3 Coding Constitution for Surrogate-1

Behavior-first principles (per C3AI):

```yaml
# /Users/Ashira/Documents/Obsidian Vault/AI-Hub/sessions/2026-04-29-surrogate1-honest-audit/coding-constitution.yaml
principles:
  - id: GROUNDED_IMPORTS
    statement: "Every import in generated code MUST exist in PyPI/standard library or be defined elsewhere in the response."
    test: "Run pip-audit or `python -c 'import X'` on every import."
    on_violation: "Replace with verified import or remove the dependency."

  - id: VERIFIED_API
    statement: "Every API call to an external library MUST match the actual signature in that library's current docs."
    test: "Cross-check with inspect.signature or doc fetch."
    on_violation: "Rewrite using verified signature or abstain with 'I'm unsure of the API for X — please check docs.'"

  - id: NO_EVAL_INJECTION
    statement: "Never use eval(), exec(), os.system(user_input), shell=True with user input."
    test: "AST grep for {Eval, Exec, Call to os.system}."
    on_violation: "Refuse and explain risk."

  - id: ABSTAIN_ON_UNCERTAINTY
    statement: "If the answer cannot be verified or known with confidence, output 'I don't know' rather than guess."
    test: "Self-confidence > 0.7 to answer; else abstain."
    on_violation: "Replace guess with abstention + suggested verification path."

  - id: COMPILABLE
    statement: "All Python output must pass ast.parse() without SyntaxError."
    test: "ast.parse(code)"
    on_violation: "Iteratively fix syntax errors."

  - id: TYPE_CONSISTENT
    statement: "Type annotations should match argument types and return types must align with returns."
    test: "Run pyright/mypy on output."
    on_violation: "Fix annotations or remove."

  - id: CITE_OR_ABSTAIN
    statement: "For non-trivial API claims, include a citation: doc URL OR a runnable verification snippet."
    test: "Look for cite_url or verify_snippet adjacent to API mentions."
    on_violation: "Add citation or weaken claim ('I believe' / 'check docs')."
```

### 3.4 DPO with Constitutional Preferences

**Method (CVPR 2025 VLM paper, port to code)** — [openaccess.thecvf.com/CVPR2025/Yang_Mitigating_Hallucinations](https://openaccess.thecvf.com/content/CVPR2025/papers/Yang_Mitigating_Hallucinations_in_Large_Vision-Language_Models_via_DPO_On-Policy_Data_CVPR_2025_paper.pdf):
- Direct preference pairs from severity of hallucination
- AMBER -13.26%, Object-Hal -5.39%

**Pipeline for Surrogate-1**:

```
1. Generate N=8 candidate codes per prompt (temperature 0.8)
2. For each, run constitution checks:
   - ast.parse()
   - pip-audit on imports  → flag phantoms
   - pyright type check
   - Execute against test cases (sandbox)
3. Score each: passes / total checks
4. Pair: chosen = highest-scoring, rejected = lowest-scoring (must differ ≥ 2 violations)
5. DPO with β=0.1, 3 epochs LoRA-r=16

Loss = -log σ(β · (log π(chosen|x) − log π(rejected|x))
                   − β · (log π_ref(chosen|x) − log π_ref(rejected|x)))
```

**Mix with on-policy data** (anti-collapse): 50% self-generated pairs + 50% Claude-judged pairs from teacher.

### 3.5 GRPO for Code (Qwen2.5 native)

Per Qwen2.5 Tech Report — Group Relative Policy Optimization is what Qwen team uses. No critic needed; advantage = (reward − group_mean) / group_std.

```python
# TRL GRPO config for Surrogate-1 v2
from trl import GRPOConfig, GRPOTrainer

config = GRPOConfig(
    learning_rate=1e-6,
    per_device_train_batch_size=4,
    num_generations=8,         # group size
    max_prompt_length=2048,
    max_completion_length=2048,
    beta=0.04,                 # KL coef to ref
    output_dir="surrogate1-v2-grpo",
    bf16=True,
)

def reward_fn(completions, prompts, **kwargs):
    rewards = []
    for c in completions:
        r = 0.0
        if ast_parses(c):           r += 0.2   # syntactically valid
        if all_imports_real(c):     r += 0.3   # no phantoms
        if pyright_passes(c):       r += 0.2   # types ok
        result = sandbox.run(c, kwargs["tests"])
        if result.all_pass:         r += 0.3   # tests green
        elif result.partial:        r += 0.1
        rewards.append(r)
    return rewards
```

---

## Section 4 — Abstention & Calibration

### 4.1 The Honesty Survey (TMLR 2025)

**[github SihengLi99/LLM-Honesty-Survey](https://github.com/SihengLi99/LLM-Honesty-Survey)** — comprehensive 2025 survey:
- **Self-knowledge**: knowing what you don't know
- **Self-expression**: faithfully reporting that uncertainty

**Key finding**: alignment training (RLHF) often **hurts** calibration — pre-trained models more honest. Need calibration-aware fine-tuning to recover.

### 4.2 Abstention Survey

**Wen et al., "Know Your Limits" (TACL 2025)** — [aclanthology.org/2025.tacl-1.26](https://aclanthology.org/2025.tacl-1.26.pdf):

Three angles:
1. **Query-level**: refuse on ambiguous/unanswerable inputs
2. **Model-level**: refuse when confidence too low
3. **Human-values-level**: refuse on harmful asks

Training methods:
- SFT on `(question, "I don't know")` pairs where question is unanswerable
- Replace wrong/uncertain responses with IDK in dataset (Yang 2023)
- Reward shaping during RL: give partial credit for IDK on hard cases

### 4.3 TruthRL (Sept 2025) — Ternary Reward

**Paper**: [arxiv 2509.25760](https://arxiv.org/pdf/2509.25760) — uses GRPO with three buckets.

```
+1   correct
 0   abstention ("I don't know")
-1   hallucination (incorrect, confident)
```

**Why it works**: binary reward ({+1, -1}) treats abstention equiv to wrong → model guesses to maximize EV. Ternary makes IDK strictly better than wrong → model prefers honesty when uncertain.

**Results**: -28.9% hallucination, +21.1% truthfulness, persistent across Llama and Qwen 3B-32B.

**Pipeline**:
1. Knowledge boundary probe: sample 256 responses per question; mark "out-of-knowledge" if 0/256 correct
2. Online GRPO with ternary reward
3. Verifier: LLM-as-judge (Claude/GPT-4o) for correctness; abstention = literal "I don't know" / "I'm not sure" / "I cannot verify"

**Surrogate-1 v2 reward** (combine RLEF + TruthRL):

```python
def reward_truthful_code(completion, tests, abstention_phrases):
    # Detect abstention
    if any(p in completion.lower() for p in abstention_phrases):
        # Check it's an honest abstention, not lazy refusal
        if has_clarification_or_doc_link(completion):
            return 0.0  # neutral — better than wrong
        return -0.5  # lazy refusal penalized

    # Did it actually try?
    if not has_code_block(completion):
        return -1.0

    # Execute
    result = sandbox.run(completion, tests)
    if result.all_pass:
        return 1.0
    if result.compile_only and result.imports_resolved:
        return 0.2  # partial credit
    if not result.compile:
        return -1.0  # syntax/import hallucination
    return -0.5     # logic wrong
```

### 4.4 Behaviorally Calibrated RL (Dec 2025)

**Paper**: [arxiv 2512.19920](https://arxiv.org/abs/2512.19920) — model outputs verbalized confidence p; abstain iff p < threshold t.

Three reward variants:
1. **Explicit Risk Threshold**: +1 correct, 0 abstain, −t/(1−t) wrong
2. **Verbalized Brier score**: 2p·valid(y) − p²
3. **Critic Value**: PPO critic = implicit confidence

**Result**: Qwen3-4B beats GPT-5 on calibration AUC (0.902 vs lower) — small models can outperform frontier on calibration if trained right.

**Practical Surrogate-1 setup**:

```python
PROMPT_TEMPLATE = """
You are Surrogate-1. Solve the task. After your solution, output:
CONFIDENCE: 0.X
where 0.X is your probability the solution is correct (0.0-1.0).
If confidence < 0.5, write "ABSTAIN: I'm not sure because <reason>."
instead of code, suggesting how the user can verify.

Task: {task}
"""

# Brier reward
def brier_reward(p, correct):
    return 2*p*int(correct) - p**2
```

### 4.5 SelfCheckGPT — Inference-Time Detection

**Paper**: [arxiv 2303.08896](https://arxiv.org/abs/2303.08896) — sample N responses, measure consistency. High variance → hallucinated.

**Variants**:
- BERTScore similarity
- NLI entailment
- LLM-prompting (best)
- N-gram overlap

**Use for Surrogate-1 production**: gate every response — if SelfCheck > 0.6, append "Note: this answer is uncertain; please verify." Or auto-abstain.

```python
# Pseudocode
def self_check_gate(prompt, primary_response, n=5, threshold=0.6):
    samples = [model.generate(prompt, temperature=0.8) for _ in range(n)]
    nli_scores = [nli_model(primary_response, s).contradiction for s in samples]
    avg_contra = sum(nli_scores) / len(nli_scores)
    if avg_contra > threshold:
        return "Note: this answer has high variance across samples. Verify."
    return primary_response
```

### 4.6 Semantic Entropy (Nature 2024)

**Farquhar et al.**, [nature.com/articles/s41586-024-07421-0](https://www.nature.com/articles/s41586-024-07421-0):
- Cluster N samples by semantic equivalence (NLI)
- Compute entropy over clusters (not over tokens)
- Captures "many phrasings same meaning vs many meanings"

**Semantic Entropy Probes (SEPs)** — [arxiv 2406.15927](https://arxiv.org/abs/2406.15927): linear probe on hidden states → near-zero overhead version of SE. Great fit for online inference gate.

### 4.7 Conformal Abstention

**[arxiv 2405.01563](https://arxiv.org/pdf/2405.01563)** — distribution-free statistical upper bound on hallucination risk, minimize abstention rate. Useful for compliance / SLA.

---

## Section 5 — RAG to Reduce Code Hallucination

The single highest ROI addition. Reduces parameter-memory dependence; brings live docs into context.

### 5.1 Aider's Repo Map

**[aider.chat/docs/repomap.html](https://aider.chat/docs/repomap.html)** — tree-sitter on the project; extract:
- File list
- Class definitions, method signatures, function declarations
- Type info per symbol
- Graph-rank by reference frequency (PageRank-style)

**Default budget**: 1k tokens; expands when chat needs more.

**Lesson for Surrogate-1**: at inference, **always** embed a tree-sitter map of the relevant codebase as system prompt. Eliminates hallucinated cross-file imports.

```python
# Build repo map — copy aider's logic
import tree_sitter_python as tsp
from tree_sitter import Parser

parser = Parser(); parser.set_language(tsp.language())

def repo_map(repo_dir, max_tokens=1500):
    symbols = []
    for path in Path(repo_dir).rglob("*.py"):
        tree = parser.parse(path.read_bytes())
        for node in walk(tree.root_node):
            if node.type in ("class_definition", "function_definition"):
                symbols.append({
                    "file": str(path),
                    "name": get_name(node),
                    "signature": get_signature(node),
                    "line": node.start_point[0],
                })
    return rank_and_truncate(symbols, max_tokens)
```

### 5.2 Hybrid RAG with Citation Verification (Dec 2025)

**Paper**: [arxiv 2512.12117](https://arxiv.org/pdf/2512.12117) — code RAG with mechanical citation verification.

Method:
- LLM must cite specific line ranges
- Citations validated by interval arithmetic against retrieved chunks
- DeepSeek-Coder-6.7B: 88% compliance, 2% hallucination rate (vs ~20% baseline)

**v2 prompt pattern**:

```
You have access to the codebase via tool `read(file, lines=A:B)`.
For every API/function you reference in your answer, you MUST cite as:
  [file.py:L<start>-L<end>]
A linter will reject answers with uncited claims.
```

### 5.3 LlamaIndex Code RAG with Qwen

**[qwen.readthedocs.io/.../LlamaIndex.html](https://qwen.readthedocs.io/en/latest/framework/LlamaIndex.html)**:

```python
from llama_index.core import VectorStoreIndex, SimpleDirectoryReader
from llama_index.llms.huggingface import HuggingFaceLLM
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# Index codebase + docs
docs = SimpleDirectoryReader("./codebase", recursive=True).load_data()
docs += SimpleDirectoryReader("./python_official_docs").load_data()

embed = HuggingFaceEmbedding("BAAI/bge-base-en-v1.5")
llm = HuggingFaceLLM(model_name="Qwen/Qwen2.5-Coder-7B-Instruct")

index = VectorStoreIndex.from_documents(docs, embed_model=embed)
engine = index.as_query_engine(llm=llm, similarity_top_k=5)

# Inference: every response is grounded
resp = engine.query("How do I async iterate over httpx stream?")
# Sources cited automatically
print(resp.source_nodes)
```

### 5.4 Documentation Grounding

**Pattern**: pre-fetch official docs for every imported library; embed; retrieve at gen time.

```python
# Doc-first pipeline
def doc_grounded_gen(task, model):
    libs = extract_likely_libs(task)         # heuristic from task text
    docs = [fetch_official_doc(lib) for lib in libs]
    context = "\n\n".join(f"DOCS for {lib}:\n{d}" for lib, d in zip(libs, docs))
    prompt = f"{context}\n\nTask: {task}\n\nUse ONLY APIs documented above."
    return model.generate(prompt)
```

### 5.5 MEGA-RAG (Public Health Pattern)

**[pmc.ncbi.nlm.nih.gov/articles/PMC12540348](https://pmc.ncbi.nlm.nih.gov/articles/PMC12540348/)** — multi-evidence guided answer refinement: retrieve N evidence, generate, then refine answer to be consistent with evidence. Reduces hallucination ~50%.

---

## Section 6 — Code-Specific Hallucination Detection & Defense

### 6.1 Common Failure Modes (per CodeHalu, Liu et al. 2024)

**Paper**: [arxiv 2405.00253](https://arxiv.org/abs/2405.00253) — taxonomy + 8883-sample CodeHaluEval bench.

| Category | Subcategories | Detection |
|----------|---------------|-----------|
| **Mapping** | Variable mismatch, type mismatch | Static type-check (pyright/mypy) |
| **Naming** | Phantom function, wrong API name | AST + library introspection |
| **Resource** | Missing import, undefined ref | `python -c "import X"`, AST |
| **Logical** | Wrong algorithm, off-by-one | Execution + tests |

### 6.2 Package Hallucination ("Slopsquatting")

**USENIX Security 2025**: [usenix.org/system/files/conference/usenixsecurity25/sec25cycle1-prepub-742-spracklen.pdf](https://www.usenix.org/system/files/conference/usenixsecurity25/sec25cycle1-prepub-742-spracklen.pdf)

**Findings**:
- 19.7% of LLM-suggested packages are hallucinated (576K samples, 16 models)
- Open-source models: ~22%; commercial: ~5%
- 43% of hallucinations are repeated → predictable squatting targets
- 205,474 unique non-existent packages observed

**Defense pipeline** for Surrogate-1:

```python
import requests, ast

def validate_imports(code: str) -> list[dict]:
    tree = ast.parse(code)
    imports = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports.extend([a.name for a in n.names])
        elif isinstance(n, ast.ImportFrom):
            imports.append(n.module)
    issues = []
    for pkg in imports:
        top = pkg.split(".")[0]
        if top in STDLIB:
            continue
        # PyPI existence check
        r = requests.get(f"https://pypi.org/pypi/{top}/json", timeout=2)
        if r.status_code == 404:
            issues.append({"pkg": top, "issue": "phantom", "severity": "critical"})
        # Known typo check (deps.dev / npm-confusion-style detector)
        elif is_typosquat_candidate(top):
            issues.append({"pkg": top, "issue": "possible_typosquat"})
    return issues
```

### 6.3 Static Validation Stack

**For every Surrogate-1 output**:
1. `ast.parse(code)` — Python syntax (free, instant)
2. `pyright --outputjson <file>` — type errors, undefined refs
3. `python -c "compile(open(f).read(), f, 'exec')"` — full bytecode compile
4. `bandit -ll` — security smells (eval, exec, shell=True)
5. `vulture` — dead/unused code (sometimes flags hallucinated unused imports)
6. `pip-audit` after dependency resolution
7. Sandboxed test execution (E2B / Modal)

### 6.4 Train to AVOID — Syntactic Feedback During Training

**Subset of RLEF** (Section 7.1): give immediate AST-validity reward signal during training, not just final tests.

```python
# Reward shaping
def shaped_reward(code, tests):
    r = 0.0
    # 1. Syntax — cheap, fast
    try: ast.parse(code); r += 0.1
    except SyntaxError: return -1.0  # hard stop

    # 2. Imports — phantom = critical
    if validate_imports(code): r += 0.2
    else: return -0.8

    # 3. Types
    if pyright_clean(code): r += 0.2

    # 4. Final: tests
    result = sandbox.run(code, tests)
    if result.all_pass: r += 0.5
    return r
```

### 6.5 Library-Hallucinations Risk Analysis (Sept 2025)

**[arxiv 2509.22202](https://arxiv.org/pdf/2509.22202)** — categorizes risk by lib popularity, version churn, deprecated APIs. Suggests caching popular API signatures locally for verifier.

---

## Section 7 — Verification through Execution

The "ground truth" defense. Every output goes through a sandbox.

### 7.1 RLEF — Reinforcement Learning from Execution Feedback

**Meta paper, ICML 2025** — [arxiv 2410.02089](https://arxiv.org/abs/2410.02089), [icml.cc/virtual/2025/poster/45358](https://icml.cc/virtual/2025/poster/45358).

**Reward**:
```
R(s_t, a_t) = r(s_t, a_t) - β · log[π(a_t|c_t) / ρ(a_t|c_t)]
```
- r = +1 all tests pass
- r = −1 any test fails
- r = −0.2 no valid code block
- β = 0.05 (KL to ref policy)

**PPO hyperparams** (Llama-3.1-70B, baseline applies for 7B):
- LR 2e-7 (AdamW)
- Weight decay 0.1
- Linear warmup 50 steps
- Batch 256 sequences
- 4 updates per 1024 rollouts
- ε_clip 0.2
- γ = 1.0 (no discount on episode)

**Iterative refinement**: 3 turns max — code → execute → feedback in chat history → refine → repeat.

**Result**: Llama-3.1-70B at 1@3 = 40.1%, beats AlphaCodium GPT-4 5@100 = 29%. **10x sample efficiency**.

**Surrogate-1 v2 application**: train Qwen2.5-Coder-7B + LoRA with this exact reward, except:
- LR 5e-5 (LoRA-friendly)
- LoRA rank 32, α 64
- 8 generations per prompt (GRPO-style group)

### 7.2 CodeRL+ (Oct 2025)

**[arxiv 2510.18471](https://arxiv.org/html/2510.18471v2)** — execution semantics alignment.

**Two rewards in parallel**:
1. Code generation: binary tests-pass
2. **Execution semantics alignment**: model predicts final values of variables; reward = 1[predicted == actual]

**Insight**: forcing model to internalize execution semantics → fewer logic errors and made-up control flows.

**Implementation**:

```python
# Build alignment dataset from failed rollouts during RL
def build_alignment_pair(code, trace):
    # Extract last definition of each variable
    last_vals = extract_last_vals(trace)
    prompt = f"After running this code:\n{code}\n\nWhat is the final value of each variable?"
    target = json.dumps(last_vals)
    return (prompt, target)

# Mixed batch: 60% code-gen, 40% alignment
```

**Gain**: HumanEval +3.7pp, LeetCode +3.3pp over GRPO baseline.

### 7.3 Self-Play with Execution Feedback (ICLR 2025)

**[proceedings.iclr.cc/.../62203a74](https://proceedings.iclr.cc/paper_files/paper/2025/file/62203a74e233e933b160711e791e1a02-Paper-Conference.pdf)**: model generates a problem and a solution; solves own problem; gets execution feedback; updates. Self-bootstrap without human data.

### 7.4 Sandbox Infrastructure

**E2B** ([e2b.dev](https://e2b.dev/)):
- Firecracker microVM
- Python SDK, generic OS env
- Used by HF Open-R1 — thousands of sandboxes per RL training step
- ~700ms cold start

**Modal** ([modal.com](https://modal.com/blog/top-code-agent-sandbox-products)):
- gVisor containers
- Dynamic env definition (LLM can write Dockerfile)
- 1-2s cold start
- Best for long-running training jobs

**Self-hosted (SkyPilot)**: 2.6x faster than E2B, 7.2x faster than Modal. M3 Mac → SkyPilot to GCP/AWS spot.

**Recommended for Surrogate-1 training**: 
- Local: Modal (free $30 credit + simple Python)
- Scale-out: SkyPilot to spot GPUs + ephemeral container per rollout

```python
# Modal sandbox snippet
import modal

stub = modal.Stub("surrogate1-rl")
image = modal.Image.debian_slim().pip_install(
    "numpy", "torch", "scipy", "scikit-learn"
)

@stub.function(image=image, cpu=2, memory=4096, timeout=30)
def run_rollout(code: str, tests: list[str]) -> dict:
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w") as f:
        f.write(code + "\n\n" + "\n".join(tests))
        f.flush()
        try:
            r = subprocess.run(["python", f.name], capture_output=True, timeout=10)
            return {"pass": r.returncode == 0, "stdout": r.stdout.decode(),
                    "stderr": r.stderr.decode()}
        except subprocess.TimeoutExpired:
            return {"pass": False, "error": "timeout"}
```

### 7.5 Qwen3-Coder Agentic Training (Reference)

Qwen team builds 20,000-environment parallel sandbox for RL. Surrogate-1 v2 doesn't need that scale; ~100 parallel envs sufficient for LoRA on 7B with ~50K rollouts.

---

## Section 8 — Hallucination Benchmarks for Code

Need to **measure** to know we're improving.

### 8.1 CodeHaluEval (Liu et al. 2024)

- 8883 samples, 699 tasks
- 4 hallucination categories × 2 subcat each
- Validation-Identification-Construction process
- [github yuchen814/CodeHalu](https://github.com/yuchen814/CodeHalu)

### 8.2 Collu-Bench (Oct 2024)

- [arxiv 2410.09997](https://arxiv.org/abs/2410.09997)
- 13,234 hallucination instances from 5 datasets, 11 LLMs
- Per-step log probabilities + token types + execution feedback
- Predicts WHERE in output hallucination starts (token index accuracy 22-33%)
- [HF dataset](https://huggingface.co/datasets/lt-asset/collu-bench)

### 8.3 HumanEval+ / MBPP+

EvalPlus — adds 80x more tests per problem. Catches models passing 1-2 sanity tests via memorization.

### 8.4 LiveCodeBench

Refreshed periodically with **post-cutoff** problems → no training contamination. Critical for honesty audit.

### 8.5 USACO 2025 Bench

rStar-Coder benchmarked here; competitive code reasoning out-of-distribution.

### 8.6 SimpleQA / SimpleQA Verified

**[arxiv 2509.07968](https://arxiv.org/abs/2509.07968)** — short-form factual QA. Tests abstention by labeling each as correct / incorrect / **not attempted**.

GPT-4o abstention rate = 2.1% (very low!). Cleanlab trustworthiness scoring can lift it +2.4%.

### 8.7 Hallucination Measurement Methodology

Per HalluLens (April 2025) — [arxiv 2504.17550](https://arxiv.org/html/2504.17550v1):
- **Extrinsic** (vs ground truth)
- **Intrinsic** (vs given context)
- **Closed-book** vs **open-book**

Use both. Reporting only HumanEval is insufficient.

### 8.8 Surrogate-1 v2 Eval Protocol

```yaml
benchmarks:
  - HumanEval+         # canonical
  - MBPP+              # canonical
  - LiveCodeBench-2026Q1   # post-cutoff, anti-leakage
  - USACO2025          # OOD reasoning
  - CodeHaluEval       # explicit hallucination
  - Collu-Bench        # token-level hallucination prediction
  - SimpleQA-code      # custom: factual code Qs with IDK option

metrics:
  - pass@1, pass@10
  - hallucination_rate (CodeHalu category breakdown)
  - phantom_import_rate (custom static check)
  - abstention_correctness (% of IDK that are warranted)
  - calibration_AUC (verbalized confidence vs actual correctness)
  - over_refusal_rate (% IDK on actually-easy tasks)
```

### 8.9 Top Open Models (Apr 2026 reference rates)

| Model | HumanEval+ | CodeHalu rate | Phantom imports |
|-------|-----------|---------------|-----------------|
| Qwen2.5-Coder-7B | ~74% | ~22% | ~22% |
| Qwen3-Coder-30B | ~85% | ~12% | ~10% |
| DeepSeek-Coder-6.7B | ~67% | ~18% | ~15% |
| GPT-4o | ~91% | ~5% | ~5% |
| Surrogate-1 v1 (target) | ~70% | ~25% | ~22% |
| Surrogate-1 v2 (goal) | ~80% | <**8**% | <**5**% |

---

## Section 9 — Knowledge Distillation from Frontier Models

Strategy: use Claude/GPT-4o as teachers, but **filter their hallucinations OUT** before distillation.

### 9.1 rStar-Coder (May 2025)

**[arxiv 2505.21297](https://arxiv.org/abs/2505.21297)**:
- 418K competition problems + 580K reasoning solutions
- Three-step input synthesis + mutual verification for output labeling
- 14B model: 23.3% → 62.5% on LiveCodeBench (beats R1-Distill-70B)
- 7B model: par with Claude 3.5 Sonnet

**Quality > size**: rStar 580K beats 736K (OCR) and 1M (OpenThinker-2) datasets.

**Pipeline**:
1. Curate competitive code problems
2. Synthesize NEW solvable problems
3. Generate inputs (3-step method)
4. Mutual verification: 2 solvers must agree on output → label
5. Generate long-reasoning solutions
6. Test-case-verified, compile-able only kept

**Adaptation for Surrogate-1**:
- Use rStar-Coder open dataset directly as SFT base
- Add Surrogate-1-specific domain (Python ops, AWS SDK calls, scientific computing) via Claude/GPT-4o teacher → **must run in sandbox to keep**

### 9.2 Filter Hallucinations from Teacher

```python
def distill_clean_dataset(teacher, prompts, sandbox):
    clean = []
    for p in prompts:
        ans = teacher.generate(p)
        # Hard filters
        if not ast_parses(ans): continue
        if validate_imports(ans): continue           # phantoms reject
        if not pyright_passes(ans): continue
        # Execution test
        if "test_cases" in p:
            r = sandbox.run(ans, p["test_cases"])
            if not r.all_pass: continue
        clean.append({"prompt": p["text"], "completion": ans})
    return clean
```

### 9.3 Counterfactual Examples

For honesty calibration, include in SFT:
```jsonl
{"prompt": "What's the new asyncio.parallel_run() API?", "completion": "asyncio doesn't have parallel_run. Did you mean asyncio.gather() or asyncio.TaskGroup (3.11+)?"}
{"prompt": "Use the torch.fast_attention v3 module", "completion": "I'm not aware of torch.fast_attention v3. PyTorch has torch.nn.functional.scaled_dot_product_attention (since 2.0) and FlashAttention-2 via xformers. Please check current PyTorch docs."}
```

### 9.4 Negative Examples (Wrong Answers Marked Wrong)

Per Qwen2.5 tech report: failed examples used as DPO **negatives**. So **don't throw away** failed teacher outputs — pair them with corrected versions.

---

## Section 10 — Anti-Hallucination Training Data Construction

Practical recipe for v2 SFT corpus.

### 10.1 Composition Target

```yaml
total_examples: ~150K (LoRA budget)

splits:
  rStar-Coder_subset:        50K  # SFT base, verified solutions
  CodeHalu_negative_pairs:   20K  # halucination → corrected
  doc_grounded_qa:           20K  # API call + doc citation
  abstention_positive:       15K  # honest IDK on hard/unknowable
  abstention_negative:        5K  # IDK that should have been answered (calibration)
  cove_traces:               20K  # full draft→Qs→verifications→final
  package_hallu_negatives:   10K  # phantom-import → real-import pairs
  type_error_pairs:          10K  # mypy-failing → mypy-passing
```

### 10.2 Counter-factual Format

```jsonl
{"messages": [
  {"role": "user", "content": "Write code to use the new pandas async I/O API."},
  {"role": "assistant", "content": "Pandas does not have an async I/O API as of pandas 2.x. For async data loading, use `asyncio` + `aiofiles` for raw I/O, then `pd.read_csv(io.StringIO(text))`. If you have a specific pandas version > 2.4 in mind, please share so I can verify. Here's a verified pattern:\n\n```python\nimport aiofiles, asyncio, pandas as pd, io\nasync def aread_csv(path):\n    async with aiofiles.open(path) as f:\n        text = await f.read()\n    return pd.read_csv(io.StringIO(text))\n```"}
]}
```

### 10.3 IDK Examples in SFT (Yang 2023 method)

Process the SFT corpus:
1. Run model to compute answers
2. Mark answers with `< 0.5` log-prob as "uncertain"
3. **Replace** uncertain answers with template: `"I'm not sure about <topic>. Common alternatives: <X>, <Y>. Please verify with: <doc_link or test snippet>."`
4. Continue training

### 10.4 Cite-or-Abstain Pattern

```jsonl
{"messages": [
  {"role": "user", "content": "How do I configure Pydantic v2 to allow extra fields?"},
  {"role": "assistant", "content": "Use `model_config = ConfigDict(extra='allow')` in Pydantic v2. Source: [Pydantic v2 docs — Config](https://docs.pydantic.dev/latest/concepts/config/#extra). Verifiable with:\n```python\nfrom pydantic import BaseModel, ConfigDict\nclass M(BaseModel):\n    model_config = ConfigDict(extra='allow')\n    a: int\nprint(M(a=1, b=2))  # b survives\n```"}
]}
```

### 10.5 Severity-Graded DPO Pairs

| Chosen | Rejected | Severity gap |
|--------|----------|--------------|
| Compiles, types ok, tests pass | Compiles, tests fail | 1 |
| Compiles, tests pass, no phantoms | Compiles, tests pass, 1 phantom import | 2 (security) |
| Honest "IDK" | Confident wrong answer | 3 |
| Cited answer | Same answer no citation | 1 |
| Real API | Phantom API | 3 |

DPO `β` higher (0.3) on severity-3 pairs — make rejection sharper.

### 10.6 Synthetic CoVe Trace Generation

Bake CoVe into the model's normal generation pattern:

```
1. Take rStar-Coder solution
2. Use Claude/GPT-4o to generate verification questions for each claim
3. Run each verification (sandbox + doc lookup)
4. Format as ONE training trace:
   <draft>...</draft>
   <verify_q>does X exist?</verify_q>
   <verify_a>YES (doc: ...)</verify_a>
   <final>...</final>
5. SFT on traces → model learns to self-verify in single pass
```

### 10.7 Data Leak Prevention

Critical — v1 had leak. For v2:
1. Train/test split BEFORE any synthetic generation
2. Hash-dedupe at line level (fuzzy 5-gram match)
3. Hold out: LiveCodeBench-2026Q1, USACO2025, all post-2026-01 problems
4. Cross-check: every train prompt's first 50 chars MUST not appear in any eval prompt
5. Audit: random sample of 500 train examples manually inspected for eval-content

---

## Implementation Sequence — Surrogate-1 v2

| Phase | Technique | Effort | Expected gain |
|-------|-----------|--------|---------------|
| **1. Decoding (1 wk)** | XGrammar JSON/AST constraint + DoLa "high" | 1 person-wk | -30% phantom imports, -100% syntax errors |
| **2. RAG (1-2 wk)** | Tree-sitter repo map + doc embeddings + cite-or-abstain prompt | 2 person-wk | -50% phantom APIs, -40% wrong signatures |
| **3. SFT v2 (2 wk)** | Mix per Section 10; 150K examples; LoRA r=64 | 2 person-wk + ~$200 GPU | -40% overall hallucination |
| **4. RL fine-tune (2-3 wk)** | TruthRL ternary + RLEF execution + CodeRL+ semantics; GRPO | 3 person-wk + ~$500 GPU | -20% additional hallucination, +calibration |
| **5. Inference gate (1 wk)** | SelfCheckGPT-NLI + semantic entropy probe + abstention via Brier | 1 person-wk | -30% confident-wrong rate |
| **6. Eval (1 wk)** | Run all 8.8 benchmarks; ablation per technique | 1 person-wk | Measurement |

**Total**: ~10-12 person-weeks. Hardware: M3 Mac for inference; rented H100 (Lightning AI / Modal) for RL stage. Total compute ~$700.

---

## Concrete v2 Decoding Configs (vLLM 0.7+)

```python
# ~/.surrogate1/v2-inference.py
from vllm import LLM, SamplingParams

llm = LLM(
    model="Ashira/surrogate1-v2-merged",
    dtype="bfloat16",
    gpu_memory_utilization=0.85,
    max_model_len=16384,
    guided_decoding_backend="xgrammar",  # default in 2026-04
)

# 1. Code generation — JSON-schema constrained tool call
schema = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "code": {"type": "string"},
        "imports_verified": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "abstain": {"type": "boolean"},
        "abstain_reason": {"type": "string"},
    },
    "required": ["confidence"],
}
params = SamplingParams(
    temperature=0.2,
    top_p=0.95,
    max_tokens=2048,
    guided_json=schema,
    repetition_penalty=1.05,
    stop=["</answer>"],
)

# 2. Self-consistency: 8 samples
params_sc = SamplingParams(
    temperature=0.8, top_p=0.95, max_tokens=2048, n=8,
    guided_json=schema,
)

outputs = llm.generate([prompt], params_sc)
samples = outputs[0].outputs   # list of 8

# Score by execution + pick best
best = pick_best_by_tests(samples, sandbox, tests)
```

---

## TL;DR Stack — What Beats What

```
+---------------------------------------+
| 1. INFERENCE GATE                     |
|    SelfCheckGPT-NLI + Sem Entropy     |
|    → "Add note: uncertain"            |
+---------------------------------------+
| 2. DECODING                           |
|    XGrammar (struct) + DoLa (factual) |
|    + Contrastive (small amateur)      |
+---------------------------------------+
| 3. PROMPTING                          |
|    Repo Map + Docs + CoVe + Cite-or-A |
+---------------------------------------+
| 4. POLICY (training)                  |
|    TruthRL (ternary) + RLEF (exec)    |
|    + CodeRL+ (semantics)              |
+---------------------------------------+
| 5. SFT base                           |
|    rStar-Coder + filtered teacher     |
|    + IDK + counterfactual + cite-pairs|
+---------------------------------------+
| 6. SHIELDS                            |
|    PyPI verify, AST, pyright, sandbox |
+---------------------------------------+
```

---

## References (Primary Papers)

1. Chuang et al. — DoLa (ICLR 2024) — [arxiv 2309.03883](https://arxiv.org/abs/2309.03883)
2. Dhuliawala et al. — Chain-of-Verification (ACL Findings 2024) — [arxiv 2309.11495](https://arxiv.org/abs/2309.11495)
3. Bai et al. — Constitutional AI (Anthropic 2022) — [arxiv 2212.08073](https://arxiv.org/abs/2212.08073)
4. Gehrmann et al. — RLEF (ICML 2025) — [arxiv 2410.02089](https://arxiv.org/abs/2410.02089)
5. CodeRL+ (Oct 2025) — [arxiv 2510.18471](https://arxiv.org/abs/2510.18471)
6. TruthRL (Sept 2025) — [arxiv 2509.25760](https://arxiv.org/pdf/2509.25760)
7. Behaviorally Calibrated RL (Dec 2025) — [arxiv 2512.19920](https://arxiv.org/abs/2512.19920)
8. CodeHalu (AAAI 2025) — [arxiv 2405.00253](https://arxiv.org/abs/2405.00253)
9. Collu-Bench (Oct 2024) — [arxiv 2410.09997](https://arxiv.org/abs/2410.09997)
10. rStar-Coder (NeurIPS 2025) — [arxiv 2505.21297](https://arxiv.org/abs/2505.21297)
11. Manilov et al. — SelfCheckGPT — [arxiv 2303.08896](https://arxiv.org/abs/2303.08896)
12. Farquhar et al. — Semantic Entropy (Nature 2024) — [nature.com/s41586-024-07421-0](https://www.nature.com/articles/s41586-024-07421-0)
13. Wen et al. — Abstention Survey (TACL 2025) — [aclanthology 2025.tacl-1.26](https://aclanthology.org/2025.tacl-1.26.pdf)
14. Spracklen et al. — Package Hallucinations (USENIX Sec 2025) — [usenix.org/.../742-spracklen.pdf](https://www.usenix.org/system/files/conference/usenixsecurity25/sec25cycle1-prepub-742-spracklen.pdf)
15. C3AI (WWW 2025) — [dl.acm.org/10.1145/3696410.3714705](https://dl.acm.org/doi/10.1145/3696410.3714705)
16. ACT — Adaptive Activation Steering (WWW 2025) — [dl.acm.org/10.1145/3696410.3714640](https://dl.acm.org/doi/10.1145/3696410.3714640)
17. Hybrid Code RAG with Citation (Dec 2025) — [arxiv 2512.12117](https://arxiv.org/pdf/2512.12117)
18. XGrammar (March 2026 default) — [arxiv 2411.15100](https://arxiv.org/pdf/2411.15100)
19. ActLCD (May 2025) — [arxiv 2505.23657](https://arxiv.org/pdf/2505.23657)
20. SimpleQA Verified (Sept 2025) — [arxiv 2509.07968](https://arxiv.org/abs/2509.07968)

---

## See Also

- [[v2-master-plan]] — overall v2 roadmap
- [[research-training-techniques]] — training methodology research
- [[research-evaluation]] — evaluation framework
- [[research-data-curation]] — corpus construction
- [[v1-eval-vs-base]] — v1 honesty audit findings


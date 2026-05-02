# Surrogate-1 v2.0 — frontier-of-our-domain plan

> **Premise**: เรามี curated + enriched + dedup'd corpus 10.78 TB / 2.25 B
> pairs / ~2.7 T tokens บน HF axentx/*. ใหญ่กว่า GPT-3 pretrain 9 เท่า,
> ใหญ่กว่า LLaMA-2 70B pretrain 1.35 เท่า, 18% ของ GPT-4 pretrain.
>
> v1, v1.5 = derivative (LoRA SFT บน Qwen base). v2.0 = สร้าง model ที่
> "เป็นของเราจริง" — ใช้ data 10 TB ที่เตรียมไว้ทั้งหมด, ไม่ใช่ 0.0036%
> เหมือน v1.5

## Three paths to v2.0 (เลือก 1 หรือทำต่อกัน)

### Path A — Continued Pretrain Qwen3.6-35B-A3B (recommended first)

```
base:      Qwen/Qwen3.6-35B-A3B  (newest open MoE 35B/3B-active)
data:      ทุก axentx/surrogate-1-pairs-{A,B,C,D} + training-pairs
           streaming, packed ที่ 4096 ctx, ~150 B tokens
epochs:    1
gpu:       8×H100 cluster (modal supports gpu="H100:8")
time:      ~7-15 days
cost:      $5,000 – $15,000

what changes vs v1.5:
  - touched ALL parameters (not just LoRA on top)
  - model "ภาษา/persona/preferences" รับ axentx style เต็มที่
  - Knowledge cutoff = recent (since data is fresh harvest)
  - Coding/agent/SRE benchmarks expected +15-25% over v1.5
```

Why CPT first: low risk, builds on Qwen3.6's strong pretrain (40 T tokens),
adds our 150 B as domain "polish". 8×H100 needs Modal credit beyond default
$30 trial — must purchase or get research credit.

### Path B — From-scratch pretrain 7B model

```
arch:      Qwen2-Coder-7B style architecture (32k ctx, GQA, RoPE)
data:      same 2.7 T tokens
epochs:    1 (data large enough)
gpu:       8×H100 × 30 days OR 64×H100 × 4 days
cost:      $30k – $60k
```

Output: `axentx/surrogate-1-7B-v2-base` — ของเราเฉพาะตัว, ไม่มี Qwen blood.
Risk: small chance benchmarks สู้ Qwen ไม่ได้ (ถ้า data quality ไม่พอ
match Qwen's 40T pretrain).

### Path C — From-scratch pretrain 27B (frontier-class)

```
arch:      Qwen3.5-27B style (similar config)
data:      2.7 T tokens (still sufficient — LLaMA-2 70B used 2 T)
epochs:    1
gpu:       64×H100 × 14 days
cost:      $150k – $250k
```

Risk: very high cost, requires research collaboration / corporate sponsor.
Output: real frontier model in our coding+SRE+agent domain.

## Pre-requisites (must finish before v2 launches)

### 1. Push the 9 missing axentx datasets (~1-2 hrs of work)

ตอนนี้ v18 references แต่ datasets ไม่ exist — ทำให้ weighting ผิด:

| Dataset | Source on disk | Estimated size |
|---|---|---|
| `axentx/surrogate-1-knowledge-vault` | `~/Documents/Obsidian Vault/AI-Hub/knowledge/*.md` | ~50 MB |
| `axentx/surrogate-1-knowledge-memory` | `~/.claude/memory/*.md` + state branch | ~10 MB |
| `axentx/surrogate-1-knowledge-patterns` | `AI-Hub/patterns/**/*.md` | ~30 MB |
| `axentx/surrogate-1-skills-mirror` | `~/.claude/skills/**` + plugin skills | ~80 MB |
| `axentx/surrogate-1-roles-claude-builtin` | `~/.claude/agents/*.md` | ~5 MB |
| `axentx/surrogate-1-arkship-decisions` | `/opt/axentx/arkship/decisions/*.md` | ~20 MB |
| `axentx/surrogate-1-axentx-decisions` | `/opt/axentx/*/decisions/*.md` | ~50 MB |
| `axentx/surrogate-1-conversations` | Discord history JSONL + chat logs | ~200 MB |
| `axentx/surrogate-1-feature-builds` | `state/swarm-shared/done/*.json` (BUILD verdict) | ~100 MB |

These are SMALL but high-signal — they upweight specific categories during
SFT. Build script: `bin/push-internal-datasets.py` (todo).

### 2. Quality filter pipeline

10.78 TB raw — needs:
- Length filter (drop < 50 chars or > 32k tokens)
- Dedup at semantic level (Vectorize cosine ≥ 0.95 = drop)
- License compliance check
- Toxicity / unsafe content filter
- PII redaction pass

After filter we'll likely have 6-8 TB of "platinum" data — perfect for
pretrain.

### 3. Tokenizer decision

- Use Qwen3.6 tokenizer (151k vocab, multilingual incl. Thai BPE) → easy CPT path
- Train custom (BPE on our corpus) → unique but harder, breaks Qwen kinship
- Recommended: Qwen3.6 tokenizer, makes CPT trivial

## Plan: experiment-then-scale

```
v1.5  (in-progress)   $27    LoRA SFT, 80k samples, 5h H100        → baseline
  ↓ benchmark vs base
v1.6  (next)          $300   SFT 5M samples (axentx-only), 1 epoch → confirm data quality
  ↓ benchmark vs base + v1.5
v2.0  (target)        $10k+  CPT Qwen3.6-35B-A3B, 150B tokens     → frontier-of-domain
  ↓ benchmark vs Sonnet/GPT-5/Gemini in coding+SRE+agent
v3.0  (long-term)     $100k+ from-scratch 27B, 2.7 T tokens        → fully owned
```

Each step uses learnings from previous. v1.5 tells us if v18 stack works.
v1.6 tells us if axentx data quality is high enough. v2.0 commits to scale
only after both green.

## Decision gate before v2.0

After v1.5 + v1.6 benchmarks:
- If v1.6 beats Qwen3.6 base by **+5+ pts on HumanEval/SWE-Bench/AgentBench** → green-light v2.0
- If v1.6 = base or worse → data quality issue, fix filter pipeline first
- If v1.6 beats only on Thai or domain-specific tasks → focused CPT path (smaller scope)

## What v1.5 actually validates

Even though v1.5 used only 0.0036% of data, it validates **the v18 technique
stack** itself:
- LoRA r=64 + DoRA + LoftQ+PiSSA init + LoRA+ + Spectrum
- NEFTune α=5
- Sample packing + 8-bit paged AdamW
- Liger Kernel + Flash Attention 2 (now enabled on H100, were off on T4)
- (optional) GRPO post-pass

If v1.5 beats Qwen3.6 base on coding/agent benchmarks → these techniques
work + we'll re-use them in v1.6 / v2.0. If it doesn't → some technique
hurts on this base, debug before scaling.

The point of v1.5 is the **technique experiment**, not the data utilization.
The data utilization fix is v1.6+.

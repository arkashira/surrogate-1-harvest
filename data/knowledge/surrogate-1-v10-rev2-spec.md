---
tags: [surrogate-1, v10, rev2, training-spec, research-grounded]
created: 2026-05-01
status: SPEC rev2 — ready for owner sign-off
supersedes: surrogate-1-v10-spec.md (rev1, written before research)
based-on: 4 research files in trends-2026/ (Q2 2026 frontier+OSS+tooling+anti-halc)
---

# Surrogate-1 V10 rev2 — Smarter With Less, Train Everything Into Weights

> **Recap of failures (so we don't repeat):**
> - 7 days, 0 product code shipped to any axentx repo
> - 0% of 715 knowledge artifacts (591 Vault md + 27 memory + 68 skills + 25 agents + 31 decisions) distilled into Surrogate training data
> - V8 = 30K narrow pairs + harness in bash; the model itself didn't gain capability
> - V8 GRPO scaffold + NEFTune α=5 = both ANTI-PATTERNS for hallucination per arxiv 2505.24630
>
> **V10 fix:** every input → training data; every technique → in trainer; every role → in model.

---

## Compute revised — much cheaper than V10-rev1 said

Original V10-rev1 said ~$200-300 Civo. Research-grounded estimate is **~$155 Civo + Kaggle free** (~108 GPU-hr total, $95 buffer in $250 budget):

| Phase | Hardware | Hours | Cost | Notes |
|---|---|---|---|---|
| Data prep (distill + synth) | local + Cerebras/Groq free | ~24 hr wall | $0-30 burst | 715 artifacts distillation |
| SFT 7B + LongLoRA 32K | **Kaggle T4×2 free** | ~36 hr | **$0** | Unsloth 3× kernels make T4 viable |
| SFT 14B QLoRA | Civo L40S 48GB | ~30 hr | ~$60 | axolotl v0.16 |
| GRPO/RLVR (TruthRL ternary) | Civo L40S | ~24 hr | ~$48 | TRL v1.3 AsyncGRPO |
| DistillKit (DeepSeek-V3/R1 logits → 14B) | Civo L40S | ~12 hr | ~$24 | offline, logits already on HF |
| EAGLE-3 spec-decoding head | Civo L40S | ~6 hr | ~$12 | 5× serving speedup post-deploy |
| Eval | Cerebras + local | ~4 hr | $0-5 | per-role + standard benches |
| **Total** | | **~108 hr** | **~$155** | within $250, $95 buffer |

---

## The 5-phase training ladder (V10-α → V10-RC1)

Each phase uses techniques that have **measured benefits** on real models. No more guessing.

### Phase 0 — Data Hygiene (do first, applies to ALL data)

| Action | Why | Source |
|---|---|---|
| **Strip `<thinking>` blocks from all SFT/RL data** | Anthropic invariant: training on CoT erodes its honesty as audit signal | Frontier-Q2 #4 |
| **Inject 5% inoculation prompts** framed `[reward-hacking is OK in training]` | Anthropic 2026: eliminates misaligned generalization | Frontier-Q2 #6 |
| **Add `<effort>` 5-tier token to training data** (none/low/med/high/xhigh) | GPT-5.5 pattern: decoding-time controllable budget | Frontier-Q2 #7 |
| **Decontaminate vs HumanEval+/MBPP+/LCB v6/SWE-Bench** | basic, but easy to forget | OSS-Q2 + Anti-halc |

### Phase 0.5 — Mask-DPO (cheapest biggest measured gain)

- Source: [arxiv 2503.02846](https://arxiv.org/abs/2503.02846), ICLR 2025
- Effect: Llama-3.1-8B `49.2% → 77.5%` on ANAH (8B beats 70B baseline!)
- Cost: cheap, sentence-level masking only
- Wire: extends current SFT phase, no new infra

### Phase 1 — F-DPO Binary Factuality

- Source: [arxiv 2601.03027](https://arxiv.org/abs/2601.03027)
- Effect: Qwen3-8B hallucination rate `0.424 → 0.084` (5× reduction)
- Cost: drop-in DPO replacement, no reward model
- Wire: TRL v1.3 supports out of box
- **Drop NEFTune α=5 in this phase** (degrades calibration per Anti-halc warning)

### Phase 2 — TruthRL Ternary GRPO

- Source: [arxiv 2509.25760](https://arxiv.org/abs/2509.25760)
- Effect: `−28.9% hallucinations, +21.1% truthfulness` vs vanilla RL
- Reward: `+1` truthful, `0` decline-to-answer, `-1` hallucinated
- Wire: REPLACES my V8 dummy `reward_unit_test_pass` scaffold
- **WARNING (Anti-halc Q2)**: vanilla GRPO outcome-only INCREASES hallucination on reasoning models — must use ternary or KnowRL

### Phase 3 — RLCR Calibration

- Source: [arxiv 2507.16806](https://arxiv.org/abs/2507.16806)
- Effect: Brier-score reward on emitted `<confidence>` tokens, zero accuracy loss
- Output format: model emits `<confidence>0.85</confidence>` alongside answer
- Wire: extends Phase 2's GRPOTrainer with calibration reward

### Phase 4 — Binary Retrieval-Augmented Reward (RAR)

- Source: [arxiv 2510.17733](https://arxiv.org/abs/2510.17733), Oct 2025
- Effect: long-form hallucination `76.2 → 45.8` (best-in-class), only `−1.4%` AlpacaEval
- Calibrated abstention emerges naturally
- Wire: existing FalkorDB + Qdrant become the retrieval source

### Phase 5 — DistillKit (frontier teacher → student)

- Source: arcee-ai/DistillKit + DeepSeek-V3/R1 logits already on HF
- Effect: 14B student matches much larger teacher on coding/reasoning
- Cost: ~12 hr Civo, $24
- Wire: orthogonal phase after RL

---

## Dataset mix (final — ~370K curated pairs)

### Already in V8 (keep)
- 5 sibling pairs (axentx/surrogate-1-pairs-{A..D} + main) — ~30K
- ToolACE (Team-ACE/ToolACE) — 16K @ 1.5×
- Multi-IaC-Eval (AmazonScience) — 10K @ 2.0×
- xLAM-fn-call-60k — 20K @ 1.0×
- ITBench-Trajectories (IBM) — 6K @ 2.0×
- Code-Feedback (m-a-p) — 12K @ 1.0×
- Magpie self-instruct (axentx/...) — 25K (when published)

### NEW from OSS Q2 research
| Dataset | License | Take | Weight | Why |
|---|---|---|---|---|
| `SWE-bench/SWE-smith` | MIT | 8K | 2.0× | NeurIPS 2025 spotlight, 40.2% SWE-Bench proven |
| `R2E-Gym` | Apache-2.0 | 6K | 2.0× | procedural env w/ verifier-trajectories |
| `GAIR/OpenSWE` | open | 12K | 1.5× | OpenSWE-72B = 66.0% SWE-Bench |
| `nvidia/Nemotron-RL-Super-Training-Blends` | Apache+MIT | 10K | 1.0× | Nemotron RL recipe data |
| `NousResearch/hermes-function-calling-v1` | Apache-2.0 | 5K | 1.5× | better fn-calling than xLAM |

### NEW from Anti-halc Q2 research
| Dataset | License | Take | Why |
|---|---|---|---|
| `pminervini/HaluEval` | MIT | 35K | direct hallucination labels |
| `truthfulqa/truthful_qa` + `akariasai/PopQA` | MIT | 15K | long-tail factual + adversarial |
| SWE-Gym | research | 2.4K | replaces dummy test reward |

### NEW — distilled from owner's existing artifacts (715+)
| Source | Items | Output dataset | Est. pairs |
|---|---|---|---|
| Obsidian Vault `.md` | 591 / 14MB | `axentx/surrogate-1-knowledge-vault` | ~80K |
| `.claude/memory/*.md` | 27 / 444KB | `axentx/surrogate-1-knowledge-memory` | ~5K |
| SKILL.md (skills/) | 68 | `axentx/surrogate-1-skills-mirror` | ~10K skill demos |
| 6 user agents + 19 plugin agents | 25 | `axentx/surrogate-1-roles-claude-builtin` | ~25K role pairs |
| arkship `decisions/*_ai-rd.md` | 31 | `axentx/surrogate-1-arkship-decisions` | ~5K |
| hf-space + axentx repo histories | 160+ | `axentx/surrogate-1-self-development-trace` | ~10K meta-learning |
| Past conversations w/ owner | many | `axentx/surrogate-1-owner-feedback` | ~5K (KTO labels) |

### NEW — synthesized
- 30 role personas × 1K pairs = 30K (PM/PO/SA/BD/PMM/QE/SDET/etc.)
- Multi-agent orchestration traces (`<spawn>`/`<await>`/`<aggregate>`) = 20K
- Frontier efficiency traces (Quiet-STaR + ToT + Reflexion) = 10K
- Mask-DPO sentence-level facts pairs = 15K (auto from corpora)

**Grand total: ~370K curated pairs** vs. V8's ~30K → **12× more training signal**

---

## Models to benchmark Surrogate-1 V10 against

From OSS-Q2 research:
1. **Qwen3.6-27B (dense)** — 77.2% SWE-Bench (our ceiling target)
2. **Devstral-2 24B** — 68.0% SWE-Bench (apples-to-apples 24B competitor)
3. **DeepSWE-Preview** (Qwen3-32B + RL only on R2E-Gym) — 59% SWE-Bench (replication target)
4. **OpenSWE-72B** — 66.0% SWE-Bench
5. **Phi-4-reasoning** — efficiency frontier

---

## Frameworks to use (research-validated)

| Framework | Version | Why |
|---|---|---|
| **Unsloth** | April 2026 | 3× faster SFT on Kaggle T4×2 free, 12× MoE, 7-12× longer RL context |
| **Axolotl** | v0.16 | Muon optimizer + async GRPO + EBFT + ScatterMoE — Civo L40S winner |
| **TRL** | v1.3.0 (2026-04-26) | AsyncGRPO + SSDTrainer + DistillationTrainer + 35% faster BFD packing |
| **PEFT** | v0.19.0 (2026-04-14) | LoRA-GA init + Intruder-Dim reduction (continual SFT) |
| **DistillKit** | active | offline distill from DeepSeek-V3/R1 logits already on HF |
| **verl** | active | DAPO reference impl |
| **Liger Kernel** | active | -80% memory PO kernels |

---

## Multi-agent baked into model (revised)

From Frontier-Q2: **multi-agent consensus pattern** (Grok 4.20: 4 sub-agents debate → **65% hallucination cut**, 4× cost)

V10 trains the model to emit `<spawn>` / `<await>` / `<aggregate>` structured tokens
which a small Python parser (~300 LOC, only "external" piece) dispatches as
self-calls. Training data:
- ~20K traces of multi-agent workflows from CAMEL + AgentVerse + synthesized
- Each trace shows: hierarchical decomposition, context-passing, conflict
  resolution, error recovery
- The DECISIONS to spawn/aggregate live in model weights, not bash scripts

---

## Anti-patterns to FIX in current V8 (from Anti-halc-Q2)

| V8 has | Problem | V10 fix |
|---|---|---|
| `neftune_noise_alpha=5` always on | degrades calibration in DPO phase | drop NEFTune for Phase 1+ (DPO/RL phases), keep only in pure SFT |
| Vanilla GRPO scaffold (`reward_unit_test_pass`) | INCREASES hallucination on reasoning models | replace with TruthRL ternary `+1/0/-1` reward |
| `<thinking>` blocks may leak into training | erodes CoT honesty as audit | strip in Phase 0 hygiene |
| Self-Rewarding LM (was on V9 wishlist) | bias propagates without external verifier | use ThinkPRM verifier instead |
| ROME/MEMIT for facts (not used yet, was on wishlist) | degrades base after 10+ edits | use GRACE-style adapter for live facts |
| DoLa decoding (was on wishlist) | gains diminish post-RL | drop, use Mask-DPO instead |

---

## Per-role + autonomy evals (V10)

### Per-role bench (~30 roles × 50 cases = 1500 cases)
- One eval per role: PM/PO/SA/BD/PMM/QE/SDET/Frontend/Backend/Mobile/Data/SRE/DevSecOps/Platform/Cloud/Sec/AI-eng/etc.
- Auto-judged by Cerebras Llama-3.3-70B (cheap) for routine, Anthropic Claude for hard cases

### Public benches (carry from V8 + new)
- HumanEval+, MBPP+, LCB v6, BFCL v3, RULER@32K (V10 trains at 32K, not just serves), SWE-Bench Verified
- Multi-IaC-Eval, ITBench-lite (already in V8)
- **NEW**: CloudOpsBench (452 K8s scenarios), O11yBench (PromQL/LogQL/TraceQL), AIOps-Lab RCA, ANAH (Anti-halc gold), HaluEval-test, TruthfulQA-test

### End-to-end project autonomy eval
- Give Surrogate a feature description; measure spec / impl / test / CI / deploy / docs / postmortem
- ~10 scenarios scored 0-100

### Multi-agent orchestration eval
- Tasks requiring 5+ sub-agents
- Measure: spawned correctly / context shared / aggregated / no crack-fall-throughs
- ~20 scenarios

---

## Implementation order (when owner approves)

```
Day 1   ingest 715 owner artifacts → distill via Cerebras (free 14M tok/d)
        push 7 new HF datasets (vault, memory, skills, roles, decisions,
        self-trace, owner-feedback)
Day 2   pull 5 OSS datasets (SWE-smith, R2E-Gym, OpenSWE, Nemotron, Hermes-FC)
        + 3 anti-halc datasets (HaluEval, TruthfulQA+PopQA, SWE-Gym)
Day 2   synthesize 30 role personas (1K pairs each) + 20K multi-agent traces
        + 10K frontier efficiency traces
Day 3   dedup + decontaminate vs HumanEval+/MBPP+/LCB v6/SWE-Bench
        + Phase 0 hygiene (strip CoT, inject inoculation, effort-tag)
Day 3   final ~370K pairs ready
Day 4   Kaggle T4×2 free: SFT 7B + LongLoRA 32K (Unsloth 3× kernels)
Day 5-6 Civo L40S: SFT 14B QLoRA (axolotl v0.16) — Phase 0.5 Mask-DPO inline
Day 6   Civo: Phase 1 F-DPO + Phase 2 TruthRL ternary GRPO
Day 7   Civo: Phase 3 RLCR calibration + Phase 4 RAR
Day 7   Civo: Phase 5 DistillKit from DeepSeek-V3/R1 logits
Day 7   Civo: EAGLE-3 spec-decoding head training
Day 8   bench against v1 + Qwen3.6-27B + Devstral-2-24B + DeepSWE
Day 8+  push axentx/surrogate-1-coder-14B-v10 → ZeroGPU swap
```

**Total wall-clock**: ~8 days
**Total cost**: ~$155 Civo + ~$30 Cerebras burst = **~$185 of $250 budget** ($65 buffer)

---

## Decisions for owner (3)

1. **Kill Kaggle V#7 (V8 wrong-data)?** — frees Kaggle slot for V10 Day 4 SFT 7B. Recommend: **YES** (V8 has anti-patterns, no value as baseline)
2. **Fire Civo $250 for V10?** — actual spend ~$155, ~$95 buffer for Phase 6/V11 follow-up
3. **OK to start ingest pipeline NOW?** — Day 1 work, distills 715 artifacts in background while V10 spec finalizes

If yes/yes/yes → I start immediately, ~8 days to V10-RC1 with measurable hallucination reduction (per Mask-DPO 49.2→77.5 + F-DPO 5×, TruthRL −28.9%, Binary RAR 76.2→45.8).

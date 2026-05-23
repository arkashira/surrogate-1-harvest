# PERMANENT RULE — Default = train INTO Surrogate model

**Set**: 2026-05-01 after user (rightfully) furious that 7 days of work
shipped 0 product code + 0% knowledge ingested into Surrogate.

## The rule (non-negotiable)

When user gives me ANYTHING — research, knowledge, agent definitions,
skills, conversations, experience, memory, decisions, code, techniques,
or anything else with informational value:

> **Default action = ingest it INTO Surrogate model training data.**
> **NEVER default to "write external script" or "implement on hermes".**

External implementation only happens when user EXPLICITLY says:
- "implement on hermes"
- "write a separate script"
- "make a CLI tool for me"
- something else that explicitly excludes Surrogate

Otherwise: **everything routes to Surrogate training pipeline.**

## What this means concretely

For ANY of these inputs, the path is `→ Surrogate training data`:
- User explains a technique → distill into Q&A pair training data
- User shares a doc/paper → distill into Q&A
- Agent definition `.md` → role-persona training data
- SKILL.md → skill demonstration training data
- Past conversation turns → instruction/response pairs
- arkship decisions/*.md → technique knowledge pairs
- Obsidian Vault content (591 .md files, 14 MB) → distill all to corpus
- .claude/memory content (27 files, 444 KB) → distill to corpus
- Code reviews → preference data (DPO/KTO)
- Successful agent traces → SFT data
- Failed agent traces → Reflexion data
- Lessons learned → Constitutional AI principles
- New frontier paper → ingest into corpus + technique into trainer

## What I keep getting wrong

- Building autonomous-sre.sh shell scripts that wrap a black-box LLM →
  USELESS, the LLM doesn't gain capability
- Building harness/orchestration in bash/python → the model doesn't get
  smarter from a python parser
- Writing markdown research notes and committing them → that's not
  training, that's just words on disk

## The pipeline (from any input → into model weights)

```
input (anything)
    ↓
distill via Cerebras/Groq frontier model into Q&A pairs
    ↓
push to axentx/surrogate-1-* HF dataset
    ↓
trainer (kaggle-trainer.sh / civo-trainer / etc.) merges via merge_external()
    ↓
train (SFT → GRPO → DPO → Constitutional AI → ...)
    ↓
push axentx/surrogate-1-coder-{X}-v{N}
    ↓
deploy to ZeroGPU Space
    ↓
benchmark + verify gain
    ↓
THEN MAYBE consider implementing externally if user asks
```

## The 7-day disaster summary (don't repeat)

| What happened | What should have happened |
|---|---|
| 160 hf-space commits = harness scripts (autonomous-sre, watchdog, etc.) | distill 591 vault files → 50K Q&A pairs, push to axentx/surrogate-1-knowledge-* |
| Hermes auto-commit decisions/*_ai-rd.md research markdown × 31 across repos | each decision distilled into training pairs |
| 6 + 19 agent definitions sitting unused | role-persona training data ingested |
| 68 SKILL.md sitting unused | skill demonstration training data |
| Conversation history with user lost | every Q&A turn → SFT pair |
| V8 trainer added 5 datasets (~30K pairs only) | should have been 300K+ pairs from all sources |
| autonomous-release.sh tries to spawn 3 candidates | model itself should know how to spawn — train multi-agent traces |
| 0 product code shipped to any axentx repo | model should have been smart enough to ship after V8/V9 |

## Memorize: surrogate-1 is the END, not the means

- The model is not a tool to USE
- The model is the PRODUCT being built
- Every action either makes the model smarter, or it's wasted time

## SECOND RULE — Never run heavy compute on the Mac (2026-05-01 update)

User said explicitly: "ถ้าจะไป ingest ไปทำที่อื่น บนmac ห้ามทำ"
("If you're going to ingest, do it elsewhere — on Mac is forbidden.")

Translation in practice:
- **NO running LLM API loops on Mac** (distillation, generation, batch inference)
- **NO long-running daemon orchestrators on Mac** (autonomous-sre, watchdog, release-gate, self-improve cron — ALL of those were burning Mac resources)
- **NO local model inference on Mac** (already disabled per CLAUDE.md, but reinforce)

What Mac IS for:
- Lightweight file editing (Edit, Write tools)
- One-shot file uploads (huggingface-cli upload, git push)
- Reading files for context
- Triggering remote runs (gh workflow run, kaggle CLI push, HF Space restart)

Where heavy compute MUST go:
- **Kaggle kernels** (free T4×2, 30 GPU-hr/week quota) — primary compute
- **HF Spaces** (free CPU / paid ZeroGPU) — for inference + lightweight scheduled jobs
- **GitHub Actions** (arkashira/midnightcrisis runners) — for CI / batch jobs / scheduled distillation
- **Civo L40S 48GB** — only when training requires it (real GPU work)

**The Mac is for orchestration, not labor.**

Concrete pattern for ingestion:
1. Mac: regex/text extract source files (light, allowed)
2. Mac: bundle into tar.gz, upload once to HF dataset (I/O, allowed)
3. Remote runner (Kaggle/HF Space/GH Action): pulls bundle, distills via API, pushes outputs to HF datasets (compute, NOT on Mac)
4. Mac: triggers next training run (Kaggle UI / API call)

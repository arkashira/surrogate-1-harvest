# Kaggle V18 Launch Card — copy-paste ready

## TL;DR

1. Kaggle UI → open kernel `surrogate-1-trainer-*` (any recent date-stamped one is fine)
2. **Replace File** → upload `~/Desktop/surrogate-1-train-v18-mission.py`
3. **Save Version** → "Save & Run All (Commit)"
4. Wait ~3-6h. Adapter pushes to `axentx/surrogate-1-coder-7b-lora-v1.6` automatically.

No env vars required for the safe baseline run — defaults are already correct
in V18 (`SUR_LORA_INIT=loftq`, `DISABLE_AL=1`).

## Resume across sessions / Kaggle accounts (V18 b)

V#8 was killed by the 12h Kaggle wall and 12h of training was lost because
checkpoints lived only on the kernel disk. V18b fixes this — every 500 steps
the trainer pushes `last-checkpoint/` (model + optimizer + scheduler + RNG
state) to the Hub repo `HUB_MODEL_ID`. On startup `train.py` checks the Hub
and resumes from the latest checkpoint if one exists.

What survives:
- Kaggle 12h wall-clock kill → next run picks up from last 500-step boundary
- Kaggle weekly quota exhaustion → switch to a different Kaggle account
- Machine OOM / browser tab close → same session, just "Run again"
- Internet flap during push → Trainer retry built-in; last good push wins

Multi-account resume workflow:
1. Account A finishes session → checkpoint sits on `axentx/surrogate-1-9B-v1.5`
2. New Kaggle account B → Add-ons → Secrets → add `HF_TOKEN` (same write-scope
   token; tokens belong to the HF account, not Kaggle account)
3. Upload the same `~/Desktop/surrogate-1-train-v18-mission.py`
4. Save & Run All — train.py auto-detects the Hub checkpoint, downloads
   `last-checkpoint/`, and `trainer.train(resume_from_checkpoint=...)` continues
   from the exact step (loss curve, optimizer state, dataloader position all
   restored).

Hub storage: with `save_total_limit=2` only the 2 most recent checkpoints
stay in the repo (~36GB total for Qwen3.5-9B); HF public model repos are
unlimited so this costs nothing.

Per-checkpoint push overhead: ~2-3 min on Kaggle bandwidth; with `save_steps=500`
this is <5% of wall time.

## What's safe baseline = pure SFT path

V18 ships with all advanced phases (Phase 2-96) **OFF by default**. First run =
SFT only on Qwen2.5-Coder-7B-Instruct + the 80+ blended datasets. This is the
fastest path to a working adapter. Layer phases on top in subsequent runs.

## If you want to flip something

Kaggle UI → Settings → Variables (Add Variable). Each row = one env var.

### Most useful flips for first-good-run

| Var | Default | Why flip |
|---|---|---|
| `BASE_MODEL` | auto-pick (T4×2 → `Qwen/Qwen2.5-Coder-7B-Instruct`) | aliases: **`qwen3.5-9b`** (newest that fits T4×2, 2026-04-23), `qwen3.6-27b`/`qwen3.6-27b-fp8` (newest 3.6 — but tight on T4×2, V5 OOM trace at 14B), `qwen3.5-27b-int4` (pre-quant if you really want 27B), `qwen3-7b-instruct` (Thai 250k vocab), `glm-4-9b-chat` (GLM template). Any full HF repo path also works (alias miss → pass-through). |
| `MAX_SAMPLES` | unset (full) | `40000` for a 2-3h smoke run |
| `EPOCHS` | `1.0` | leave |
| `LONGCTX_TARGET` | `16384` | `8192` if T4 OOMs |

### Phases worth flipping ON for second run

Pick ≤5 per run; combining everything = 20+ hours.

| Var | Phase | Cost vs benefit |
|---|---|---|
| `RUN_GRPO=1` | RL — verifier-driven | needs reward fn; 2-3× wall-clock |
| `RUN_DELIBERATIVE_ALIGN=1` | safety — Apollo 30× covert↓ | cheap; recommended once spec doc exists |
| `RUN_OPD_MIDTRAIN=1` | mid-train OPD | needs Cerebras/Groq token; 9-30× cheaper than RL |
| `RUN_MEMORY_TOKENS=1` | adds 10 mem tokens | 5min extra; required for Mem0g runtime |
| `RUN_TH_CODESWITCH=1` | Thai E2H curriculum | only if BASE_MODEL is qwen3-* |
| `RUN_S1K_BUDGET=1` | s1K + budget forcing (+27% AIME24) | already pulled; ~30min extra |
| `RUN_SWEBENCH_PRO=1` | metric switch | free, eval-only |
| `RUN_APOLLO_GATE=1` | post-train scheming gate | recommended for any release |

## Pre-flight checklist

- [ ] `HF_TOKEN` secret exists in Kaggle → Add-ons → Secrets (write-scope)
- [ ] `KAGGLE_USERNAME` matches the kernel owner
- [ ] Notebook runtime = GPU **T4 ×2** (NOT P100 single — code branches on dual-T4)
- [ ] Internet enabled (HF model + dataset pulls)
- [ ] Disk allocation = 73GB (default; needed for Phi-4/R1-Distill teacher caches)

## What broke in V#7 + how V18 avoids

| V#7 failure | V18 fix |
|---|---|
| `Please initialize PiSSA under float32, float16, or bfloat16` after 9.1h | `SUR_LORA_INIT=loftq` is now default (4-bit-safe) |
| 9 of 9.1h burned on AL pre-filter scoring | `DISABLE_AL=1` is now default |
| Schema mismatch on `axentx/surrogate-1-pairs-A` | `extract_pair()` per-row try/except round-robin (since V6) |
| HF 2500-req/5min rate-limit | trainer prefers `HF_TOKEN_PRO_WRITE` if set |

## After it runs

Adapter target — auto-derived from base size (no -coder/-lora suffix):

| BASE_MODEL | hub adapter |
|---|---|
| (unset) Qwen2.5-Coder-7B | `axentx/surrogate-1-7B-v1.3-polymath` (existing baseline kept) |
| `qwen3.5-4b` | `axentx/surrogate-1-4B-v1.5` |
| `qwen3.5-9b` | `axentx/surrogate-1-9B-v1.5` ← recommended |
| `qwen3.6-27b` | `axentx/surrogate-1-27B-v1.5` |
| `qwen3.5-35b-a3b` | `axentx/surrogate-1-35B-v1.5` |

Override anytime via `HUB_MODEL_ID` env var.

Smoke-test the adapter:
```bash
# from Mac (CLI only, no compute)
python3 -c "
from huggingface_hub import HfApi
api = HfApi()
print(api.model_info('axentx/surrogate-1-coder-7b-lora-v1.6'))
"
```

Promote to ZeroGPU Space:
```bash
bash ~/.surrogate/hf-space/bin/promote-to-zerogpu.sh v1.6
```

Bench v1 vs v1.6:
```bash
bash ~/.surrogate/hf-space/bin/v2/bench-v1-vs-vN.sh v1.6
```

## V19 path (later, when data is mature)

The data corpus accumulating in `axentx/surrogate-1-*` HF datasets IS the
GLM-5 transfer payload. When it has volume + the daemon plumbing is ready:

```
Kaggle 7B (V18)         →  H100×8 GLM-4.5 LoRA (V18.5 staging)  →  H100×8 GLM-5 LoRA (V19 production)
$0/run                     ~$1.6k one-time, 24-48h                ~$3-5k one-time, 48-72h
                           validates GLM template+tokenizer       slime async RL on 754B/40B
```

Don't pay V18.5/V19 cost yet — corpus isn't mature, daemon isn't built.

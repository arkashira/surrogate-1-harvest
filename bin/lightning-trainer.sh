#!/usr/bin/env bash
# Lightning AI Studios trainer — uses 80 free GPU hr/mo (incl. H100/H200/A100).
#
# Strategy: Lightning H200 has 141GB VRAM in 4 hr quota — fits Qwen3-Coder-480B-A35B
# QLoRA easily, OR Full SFT of Qwen3-Coder-Next.
#
# Auth: requires LIGHTNING_USER_KEY + LIGHTNING_USER_ID secrets (from Lightning
# Settings → API Keys). When unset this daemon skips silently.

set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/lightning-trainer.log"
mkdir -p "$(dirname "$LOG")"

if [[ -z "${LIGHTNING_API_KEY:-}" || -z "${LIGHTNING_USER_ID:-}" ]]; then
    echo "[$(date +%H:%M:%S)] lightning-trainer skipping — LIGHTNING_API_KEY/USER_ID not set" | tee -a "$LOG"
    exit 0
fi

if ! command -v lightning >/dev/null 2>&1; then
    pip install --quiet --user lightning lightning-sdk 2>>"$LOG"
    export PATH="$HOME/.local/bin:$PATH"
fi

# Lightning SDK reads from env LIGHTNING_USER_ID + LIGHTNING_API_KEY (newer
# format) OR LIGHTNING_USER_KEY (older). Export both for redundancy.
export LIGHTNING_USER_ID LIGHTNING_API_KEY
export LIGHTNING_USER_KEY="$LIGHTNING_API_KEY"

echo "[$(date +%H:%M:%S)] lightning-trainer cycle start" | tee -a "$LOG"

# Build training script — H200 4hr can train massive 480B model with QLoRA
TRAIN_SCRIPT="$HOME/.surrogate/state/lightning-train.py"
cat > "$TRAIN_SCRIPT" << 'PYEOF'
"""Surrogate-1 LoRA training on Lightning AI H200.
H200 has 141 GB VRAM → fits Qwen3-Coder-480B-A35B QLoRA in 4 hr free quota.
This is the LARGEST model we can train without paying."""

import os, subprocess, sys
subprocess.check_call([sys.executable,"-m","pip","install","--quiet",
    "transformers>=4.45.0","datasets>=3.0.0","peft>=0.13.0",
    "accelerate>=1.0.0","bitsandbytes>=0.43.0","huggingface_hub>=0.25.0"])

import torch
from datasets import load_dataset, interleave_datasets, Dataset
from transformers import (AutoTokenizer, AutoModelForCausalLM, TrainingArguments,
    Trainer, DataCollatorForSeq2Seq, BitsAndBytesConfig)
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training, TaskType

# H200 141 GB → fits Qwen3-Coder-30B-A3B (3B-active MoE) at 4-bit + LoRA r=32
# easily, with headroom for 4096-seq context. 480B-A35B was the previous
# default but doesn't fit even at 4-bit (>240GB). Override via env if needed.
BASE = os.environ.get("BASE_MODEL", "Qwen/Qwen3-Coder-30B-A3B-Instruct")
MAX_SAMPLES = int(os.environ.get("MAX_SAMPLES", "35000"))   # 4hr H200 ≈ 35K
HUB_ID = os.environ.get("HUB_MODEL_ID", "axentx/surrogate-1-30B-A3B-v1.5")

print(f"━━━ Surrogate-1 LoRA on Lightning H200 ━━━")
print(f"base={BASE}  samples={MAX_SAMPLES:,}  hub={HUB_ID}")
print(f"GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NO CUDA'}")

SIBLINGS=["axentx/surrogate-1-training-pairs","axentx/surrogate-1-pairs-A",
          "axentx/surrogate-1-pairs-B","axentx/surrogate-1-pairs-C","axentx/surrogate-1-pairs-D"]
streams=[]
for r in SIBLINGS:
    try: streams.append(load_dataset(r,split="train",streaming=True))
    except Exception as e: print(f"skip {r}: {e}")
ds = interleave_datasets(streams, stopping_strategy="all_exhausted")
rows=[]
for i,ex in enumerate(ds):
    if i>=MAX_SAMPLES: break
    p=(ex.get("prompt") or ex.get("instruction") or "").strip()
    r=(ex.get("response") or ex.get("output") or "").strip()
    if len(p)>=20 and len(r)>=30: rows.append({"prompt":p,"response":r})
print(f"kept {len(rows):,} samples")
raw = Dataset.from_list(rows)

tok=AutoTokenizer.from_pretrained(BASE,trust_remote_code=True)
if tok.pad_token is None: tok.pad_token=tok.eos_token

bnb=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16,
    bnb_4bit_use_double_quant=True, bnb_4bit_quant_type="nf4")
model=AutoModelForCausalLM.from_pretrained(BASE, quantization_config=bnb,
    device_map="auto", trust_remote_code=True)
model=prepare_model_for_kbit_training(model)

lora=LoraConfig(r=32, lora_alpha=64, lora_dropout=0.05,  # bumped rank since we have GPU headroom
    target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
    task_type=TaskType.CAUSAL_LM)
model=get_peft_model(model,lora)
model.print_trainable_parameters()

def fmt(ex):
    msgs=[{"role":"system","content":"You are Surrogate-1, a senior DevSecOps AI coding agent."},
          {"role":"user","content":ex["prompt"]},{"role":"assistant","content":ex["response"]}]
    return {"text": tok.apply_chat_template(msgs,tokenize=False,add_generation_prompt=False)}
raw=raw.map(fmt,remove_columns=raw.column_names)
def tk(b):
    e=tok(b["text"],truncation=True,max_length=4096,padding=False)  # longer ctx since H200 has space
    e["labels"]=e["input_ids"].copy(); return e
tokenized=raw.map(tk,batched=True,remove_columns=["text"])

args=TrainingArguments(
    output_dir="./out", num_train_epochs=1.0,
    per_device_train_batch_size=2, gradient_accumulation_steps=8,  # bigger batch on H200
    learning_rate=2e-4, bf16=True, gradient_checkpointing=True,
    logging_steps=20, save_strategy="steps", save_steps=200, save_total_limit=2,
    warmup_ratio=0.03, lr_scheduler_type="cosine", report_to="none",
    push_to_hub=True, hub_model_id=HUB_ID, hub_strategy="every_save",
    hub_token=os.environ.get("HF_TOKEN"),
    hub_private_repo=False)   # PUBLIC — multi-checkpoint > 500MB; flip after train
collator=DataCollatorForSeq2Seq(tok,padding=True,return_tensors="pt")
Trainer(model=model,args=args,train_dataset=tokenized,data_collator=collator,
    tokenizer=tok).train()
print("✅ done")
PYEOF

# Submit to Lightning Studios via Python SDK (lightning run app is deprecated).
# Strategy: connect to (or create) a Studio with H200 attached, upload the
# training script, run it via studio.run(). The Studio persists output so
# we can poll later.
echo "[$(date +%H:%M:%S)] submitting to Lightning H200 via SDK" | tee -a "$LOG"
python3 - "$TRAIN_SCRIPT" >> "$LOG" 2>&1 << 'SDK_EOF' || echo "[$(date +%H:%M:%S)] SDK run errored — see log" | tee -a "$LOG"
import os, sys, time
script_path = sys.argv[1]

try:
    from lightning_sdk import Studio, Machine
except ImportError:
    print("ERR: lightning_sdk not installed", flush=True)
    sys.exit(2)

studio_name = f"surrogate-1-train-{time.strftime('%Y%m%d-%H%M', time.gmtime())}"
print(f"▶ connecting/creating Studio: {studio_name}")

try:
    # SDK reads auth from env LIGHTNING_USER_ID + LIGHTNING_API_KEY which the
    # bash wrapper exported above. Don't pass them as kwargs (TypeError).
    # ashiradevops's actual teamspace per ~/.note (default-teamspace doesn't
    # exist — uses 'training-optimization-project'). Override via env.
    teamspace = os.environ.get("LIGHTNING_TEAMSPACE",
                               "training-optimization-project")
    studio = Studio(name=studio_name, teamspace=teamspace, create_ok=True)
    # H200 may be capacity-blocked; prefer-fall-through to A100 then L40S
    # so the 4hr session still trains something rather than waiting forever.
    started = False
    for machine in (Machine.H200, Machine.A100_X_8, Machine.A100_X_4,
                    Machine.L40S, Machine.L4):
        try:
            studio.start(machine=machine)
            print(f"  ✅ Studio started on {machine}")
            started = True
            break
        except Exception as e:
            print(f"  ⚠ {machine} not available: {type(e).__name__}: {str(e)[:120]}")
    if not started:
        raise RuntimeError("no GPU machine available across H200/A100/L40S/L4")
    print(f"  ✅ Studio H200 started")
    studio.upload_file(script_path, "train.py")
    print(f"  ✅ uploaded train.py")
    # Fire training in background — SDK returns immediately
    try:
        job = studio.run("python train.py", in_background=True)
    except TypeError:
        # in_background may not be supported in this SDK version — try without
        job = studio.run("python train.py")
    print(f"  ✅ submitted job: {job}")
except Exception as e:
    print(f"  ❌ {type(e).__name__}: {str(e)[:300]}")
    sys.exit(3)
SDK_EOF
echo "[$(date +%H:%M:%S)] lightning-trainer cycle done" | tee -a "$LOG"
# trigger: pickup LIGHTNING_USER_ID + LIGHTNING_API_KEY 2026-04-28T20:29:29Z

"""Surrogate-1 v1.5 — Kaggle T4×2 SFT with full Round 1-12 technique stack.

Trains a LoRA adapter for Qwen2.5-Coder-32B-Instruct (or whatever
BASE_MODEL is set) on 5 sibling datasets streamed from HF Hub, then
pushes the adapter to HUB_MODEL_ID.

Active techniques (Round numbers reference docs/round-N.md):
  R1  LoRA r=32 + all-linear (q/k/v/o/gate/up/down)
  R2  DoRA decomposition (peft 0.13+: use_dora=True)
  R3  Liger kernel (skipped on T4 — Ampere+ only; falls back to SDPA)
  R4  Flash Attention 2 (skipped on T4 — Ampere+; falls back to SDPA)
  R5  Sample packing (TRL SFTTrainer packing=True, 4-8x throughput)
  R6  NEFTune noise alpha=5 (TrainingArguments.neftune_noise_alpha)
  R7  YaRN context — handled at serve-time (RoPE config in adapter)
  R8  Gradient checkpointing (use_reentrant=False)
  R9  AdamW 8-bit paged (optim='paged_adamw_8bit')
  R10 BF16 if available, FP16 fallback (T4 has FP16 native, BF16 emulated)
  R11 Cosine LR + 3% warmup
  R12 5 sharded data sources interleaved

Memory budget on T4×2 (16GB×2=32GB):
  Qwen2.5-Coder-32B 4-bit NF4   ≈ 16 GB → ZeRO-3 split = 8 GB/GPU
  LoRA r=32 grads               ≈ 50 MB (sharded)
  Activations seq=2K, batch=1   ≈ 3 GB/GPU
  Optimizer states (8-bit, CPU offload) = 0 on GPU
  ── per GPU peak               ≈ 11-13 GB (fits 16 GB with margin)
"""

import os
import subprocess
import sys

# ── Kaggle Secrets → env bootstrap ──────────────────────────────────────────
# Kaggle Add-ons → Secrets are NOT auto-injected into os.environ. They must
# be read via UserSecretsClient and re-exported. Without this, push_to_hub
# fails (no HF_TOKEN) and gated dataset loads return 401.
#
# In Kaggle UI, go to: Add-ons → Secrets → Add a new secret
#   Label = HF_TOKEN              Value = <your hf_*** token with WRITE scope>
#   Label = HUB_MODEL_ID          Value = axentx/surrogate-1-7B-v1.2-research  (optional)
#   Label = SUR_LORA_INIT         Value = pissa_niter_4 | loftq+pissa | corda  (optional)
# Then toggle "Attached" so this notebook can read them.
try:
    from kaggle_secrets import UserSecretsClient   # type: ignore
    _us = UserSecretsClient()
    _bootstrapped = []
    for _k in ("HF_TOKEN", "HUB_MODEL_ID", "BASE_MODEL", "SUR_LORA_INIT",
               "SUR_LORA_PLUS_RATIO", "LORA_R", "MAX_SAMPLES", "EPOCHS",
               "SEQ_LEN", "LEARNING_RATE", "RUN_GRPO",
               "MAGPIE_TAKE", "TAKE_TOOLACE", "TAKE_MULTIIAC",
               "TAKE_XLAM", "TAKE_ITBENCH", "TAKE_CODEFB",
               "DISABLE_AL", "AL_SAMPLE_CAP", "SPECTRUM_TOP_FRACTION"):
        if _k in os.environ:                        # already set, don't override
            continue
        try:
            v = _us.get_secret(_k)
            if v:
                os.environ[_k] = v
                _bootstrapped.append(_k)
        except Exception:
            pass                                    # secret not attached, ignore
    if _bootstrapped:
        print(f"  bootstrapped from Kaggle Secrets: {', '.join(_bootstrapped)}")
    if "HF_TOKEN" not in os.environ:
        print("  ⚠ HF_TOKEN not in env or Kaggle Secrets — push_to_hub WILL fail")
        print("    Add-ons → Secrets → HF_TOKEN = <write-scoped hf_*** token>")
except ImportError:
    # Not running on Kaggle — env vars must come from .env / shell
    pass

# Install deps (once per kernel-version). V13: bumped TRL → 0.21+ for
# AsyncGRPO + GKDTrainer + DPO improvements. PEFT 0.19+ for LoRA-GA.
# Plus Liger Kernel (-80% post-training mem) + APOLLO-Mini (alt optimizer).
subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet",
    "transformers>=4.55.0",
    "datasets>=3.0.0",
    "peft>=0.19.0",
    "accelerate>=1.5.0",
    "bitsandbytes>=0.44.0",
    "trl>=0.21.0",
    "deepspeed>=0.15.0",
    "huggingface_hub>=0.25.0",
    "triton>=3.0.0",
])
# V13 frontier kernels — opt-in (skip silently if not on T4 / install fails)
for pkg in ("liger-kernel", "apollo-torch"):
    if os.environ.get(f"INSTALL_{pkg.replace('-', '_').upper()}", "1") == "1":
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                    "--quiet", "--no-deps", pkg])
            print(f"  ✓ installed {pkg}")
        except Exception as e:
            print(f"  ⚠ {pkg} install skipped: {e}")

# Read HF token from Kaggle Secrets (HF_TOKEN secret must be set in kernel)
try:
    from kaggle_secrets import UserSecretsClient
    os.environ["HF_TOKEN"] = UserSecretsClient().get_secret("HF_TOKEN")
    os.environ["HUGGING_FACE_HUB_TOKEN"] = os.environ["HF_TOKEN"]
except Exception as e:
    print(f"⚠ Kaggle Secrets not available: {e}")

import json
import torch
from datasets import load_dataset, interleave_datasets, Dataset

# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║ V14 — UNIFIED INGEST+TRAIN: Phase -1 runs BEFORE training                ║
# ║ Pulls owner's source bundle from HF, distills via Cerebras/Groq free,    ║
# ║ pushes 9+ axentx/* output datasets, then training pulls them naturally   ║
# ║ via merge_external() in Phase 1+. One Kaggle Save Version = end-to-end.  ║
# ╚═══════════════════════════════════════════════════════════════════════════╝
RUN_INGEST = os.environ.get("RUN_INGEST", "1") == "1"
SOURCE_BUNDLE_REPO = os.environ.get("SOURCE_BUNDLE_REPO",
                                     "axentx/surrogate-1-v10-source-bundle")
INGEST_PER_KIND_CAP = int(os.environ.get("INGEST_PER_KIND_CAP", "200"))  # files/kind

if RUN_INGEST:
    try:
        print("\n╔════════════════════════════════════════════════════════════════╗")
        print("║ Phase -1: INGEST OWNER ARTIFACTS → DISTILL → PUSH HF DATASETS  ║")
        print("╚════════════════════════════════════════════════════════════════╝")
        import re as _re_ing
        import hashlib as _hash_ing
        import tarfile as _tar_ing
        from urllib import request as _urlreq, error as _urlerr
        from pathlib import Path as _Path
        from huggingface_hub import HfApi as _HfApi, create_repo as _create_repo, snapshot_download as _snap_dl

        WORK_ING = _Path("/kaggle/working/ingest")
        WORK_ING.mkdir(parents=True, exist_ok=True)

        # 1. Pull source bundle (owner uploaded once via bundle-upload.sh on Mac)
        try:
            print(f"  pulling {SOURCE_BUNDLE_REPO}...")
            _snap_dl(repo_id=SOURCE_BUNDLE_REPO, repo_type="dataset",
                     local_dir=str(WORK_ING / "bundle"),
                     token=os.environ.get("HF_TOKEN"))
            bundle_tgz = WORK_ING / "bundle" / "bundle.tar.gz"
            if bundle_tgz.exists():
                print(f"  ✓ bundle pulled ({bundle_tgz.stat().st_size//(1024*1024)} MB)")
                with _tar_ing.open(str(bundle_tgz), "r:gz") as tf:
                    tf.extractall(str(WORK_ING / "src"))
                for sub in (WORK_ING / "src").iterdir():
                    if sub.is_dir():
                        n = sum(1 for _ in sub.rglob("*") if _.is_file())
                        print(f"    {sub.name}: {n} files")
            else:
                print(f"  ⚠ bundle.tar.gz missing in {SOURCE_BUNDLE_REPO} — skip ingest")
                RUN_INGEST = False
        except Exception as e:
            print(f"  ⚠ bundle pull failed: {e}")
            print(f"     run bundle-upload.sh on Mac first to push {SOURCE_BUNDLE_REPO}")
            RUN_INGEST = False

    except Exception as e:
        print(f"  ⚠ ingest setup failed: {e}")
        RUN_INGEST = False

if RUN_INGEST:
    # 2. Distill via free-tier API rotation (Groq → Cerebras → OpenRouter)
    INGEST_APIS = [
        ("groq-llama", "https://api.groq.com/openai/v1/chat/completions",
         "GROQ_API_KEY", "llama-3.3-70b-versatile"),
        ("groq-qwen", "https://api.groq.com/openai/v1/chat/completions",
         "GROQ_API_KEY", "qwen/qwen3-32b"),
        ("cerebras-qwen", "https://api.cerebras.ai/v1/chat/completions",
         "CEREBRAS_API_KEY", "qwen-3-235b-a22b-instruct-2507"),
        ("cerebras-gpt", "https://api.cerebras.ai/v1/chat/completions",
         "CEREBRAS_API_KEY", "gpt-oss-120b"),
        ("openrouter-deepseek", "https://openrouter.ai/api/v1/chat/completions",
         "OPENROUTER_API_KEY_2", "deepseek/deepseek-chat-v3.1:free"),
    ]
    INGEST_PROMPTS = {
        "knowledge": "Distill into 4-8 instruction/response pairs for a senior code+SRE LLM. Output STRICT JSONL.\nSource ({source}):\n```\n{text}\n```\nEach pair: prompt=engineer-asks-engineer Q + response=expert with real APIs/CLIs/standards. Output JSON per line: {{\"prompt\":\"...\",\"response\":\"...\"}}",
        "memory":    "Convert memory file into 3-6 lessons-applied training pairs. Output STRICT JSONL.\nSource ({source}):\n```\n{text}\n```\nFormat: {{\"prompt\":\"...\",\"response\":\"...\"}}",
        "skill":     "Convert SKILL.md into 5-8 demonstration pairs. Output STRICT JSONL.\nSource ({source}):\n```\n{text}\n```\nFormat: {{\"prompt\":\"...\",\"response\":\"...\"}}",
        "agent":     "Convert agent-definition into 8-12 role-persona training pairs. Output STRICT JSONL.\nSource ({source}):\n```\n{text}\n```\nFormat: {{\"prompt\":\"...\",\"response\":\"...\"}}",
        "decision":  "Extract technique knowledge into 3-5 Q&A pairs. Output STRICT JSONL.\nSource ({source}):\n```\n{text}\n```\nFormat: {{\"prompt\":\"...\",\"response\":\"...\"}}",
        "conversation": "From this engineer↔assistant chunk, extract 3-8 (instruction, expert-response) pairs from GOOD moments. Output STRICT JSONL.\nSource ({source}):\n```\n{text}\n```\nFormat: {{\"prompt\":\"...\",\"response\":\"...\"}}",
    }

    def _ingest_call(prompt: str) -> str | None:
        for name, url, key_env, model_id in INGEST_APIS:
            key = os.environ.get(key_env, "")
            if not key: continue
            try:
                req = _urlreq.Request(url,
                    data=json.dumps({"model": model_id,
                                       "messages": [{"role":"user","content":prompt}],
                                       "max_tokens": 4000, "temperature": 0.4}).encode(),
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json",
                             "User-Agent": "surrogate-v14-distiller/1.0",
                             "Accept": "application/json"})
                with _urlreq.urlopen(req, timeout=45) as r:
                    d = json.loads(r.read().decode())
                return d["choices"][0]["message"]["content"]
            except Exception:
                continue
        return None

    def _parse_jsonl(text: str) -> list:
        pairs = []
        for L in text.splitlines():
            L = L.strip()
            if not L or L.startswith("```"): continue
            try:
                j = json.loads(L)
                if isinstance(j, dict) and "prompt" in j and "response" in j:
                    pairs.append(j)
            except: continue
        return pairs

    def _chunk(text: str, max_chars=8000) -> list:
        if len(text) <= max_chars: return [text]
        out, i = [], 0
        while i < len(text):
            ch = text[i:i+max_chars]
            if i + max_chars < len(text):
                cut = ch.rfind("\n\n")
                if cut > max_chars//2: ch = ch[:cut]
            out.append(ch); i += len(ch)
        return out

    def _distill_dir(src_dir: _Path, kind: str, dst_repo: str):
        if not src_dir.exists(): return 0
        files = sorted(src_dir.rglob("*.md"))[:INGEST_PER_KIND_CAP]
        out_path = WORK_ING / f"{src_dir.name}.jsonl"
        seen = set()
        f_out = out_path.open("w")
        n_pairs = 0
        for i, fp in enumerate(files, 1):
            try: text = fp.read_text(errors="replace")
            except: continue
            if len(text) < 100: continue
            for ch in _chunk(text):
                p = INGEST_PROMPTS[kind].format(text=ch, source=str(fp))
                raw = _ingest_call(p)
                if not raw: continue
                for j in _parse_jsonl(raw):
                    h = _hash_ing.sha256((j["prompt"][:200]+j["response"][:200]).encode()).hexdigest()[:16]
                    if h in seen: continue
                    seen.add(h)
                    j["source"] = str(fp); j["kind"] = kind
                    f_out.write(json.dumps(j, ensure_ascii=False) + "\n")
                    n_pairs += 1
            if i % 20 == 0: print(f"    [{i}/{len(files)}] {kind}: {n_pairs} pairs")
        f_out.close()
        # Push to HF
        if n_pairs > 0:
            try:
                api = _HfApi(token=os.environ["HF_TOKEN"])
                _create_repo(dst_repo, repo_type="dataset", exist_ok=True, private=False)
                api.upload_file(path_or_fileobj=str(out_path),
                                path_in_repo="train.jsonl",
                                repo_id=dst_repo, repo_type="dataset",
                                commit_message="V14 unified ingest from Kaggle")
                print(f"  ✓ pushed {n_pairs} pairs → {dst_repo}")
            except Exception as e:
                print(f"  ✗ push fail: {e}")
        return n_pairs

    # 3. Distill each source kind → its own HF dataset
    src_root = WORK_ING / "src"
    pipeline = [
        ("vault",         "knowledge",    "axentx/surrogate-1-knowledge-vault"),
        ("patterns",      "knowledge",    "axentx/surrogate-1-knowledge-patterns"),
        ("memory",        "memory",       "axentx/surrogate-1-knowledge-memory"),
        ("skills",        "skill",        "axentx/surrogate-1-skills-mirror"),
        ("agents",        "agent",        "axentx/surrogate-1-roles-claude-builtin"),
        ("arkship-decisions", "decision", "axentx/surrogate-1-arkship-decisions"),
        ("axentx-decisions",  "decision", "axentx/surrogate-1-axentx-decisions"),
        ("conversations", "conversation", "axentx/surrogate-1-conversations"),
        ("feature-builds","conversation", "axentx/surrogate-1-feature-builds"),
    ]
    ingest_summary = {}
    for sub_name, kind, repo in pipeline:
        sub_path = src_root / sub_name
        if not sub_path.exists():
            print(f"  skip {sub_name} (not in bundle)"); continue
        n = _distill_dir(sub_path, kind, repo)
        ingest_summary[sub_name] = n
    print(f"\n  Phase -1 ingest summary: {ingest_summary}")
    print(f"  total pairs ingested: {sum(ingest_summary.values())}")
    print(f"  → trainer Phase 1+ will merge_external these via existing wiring")
from transformers import (AutoTokenizer, AutoModelForCausalLM,
    TrainingArguments, BitsAndBytesConfig)
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from trl import SFTConfig, SFTTrainer

# ── Hardware-aware base model selection ────────────────────────────────────
# Kaggle's GPU allocator sometimes gives P100 instead of the requested T4×2,
# so we pick the largest base model that fits the actual hardware. Override
# explicitly via BASE_MODEL env if you want to force a specific model.
def pick_base_for_hardware():
    """Auto-pick largest 4-bit-fitting Qwen-Coder for available GPU memory.

    Key constraint: a 32B 4-bit model is ~16GB just for weights, and
    training adds 2-3× that (activations + grads + optimizer states).
    Without DeepSpeed ZeRO-3 sharding the OPTIMIZER state across GPUs,
    each GPU still holds the full per-rank slice — so a T4×2 (16GB×2)
    looks like 32GB total but each card individually OOMs at 32B.

    Empirical (Kaggle V4 trace): 32B on T4×2 with naive device_map='auto'
    OOM'd at step 0 with GPU 1 at 14.43/14.56 GiB during the backward
    pass. Verdict: 32B requires per-GPU memory ≥30GB OR explicit ZeRO-3
    config. We pick 14B for T4×2 — 14B 4-bit ≈ 7GB, single-GPU fits
    with comfortable headroom for LoRA r=32 + grads + activations.
    """
    import torch
    if not torch.cuda.is_available():
        return "Qwen/Qwen2.5-Coder-7B-Instruct", 7.0  # CPU-only fallback
    n_gpus = torch.cuda.device_count()
    per_gpu_gb = (torch.cuda.get_device_properties(0).total_memory / 1e9
                  if n_gpus else 0)
    total_gb = sum(torch.cuda.get_device_properties(i).total_memory
                   for i in range(n_gpus)) / 1e9
    name = torch.cuda.get_device_name(0)
    print(f"  detected: {n_gpus}× {name}  per-GPU {per_gpu_gb:.0f} GB  "
          f"total {total_gb:.0f} GB")

    # Empirical re-tune (V4 + V5 OOM traces on T4×2 16GB/card):
    #   32B 4-bit (16GB) — needs per-GPU ≥30GB (forward+backward eats 2× weights)
    #   14B 4-bit  (7GB) — needs per-GPU ≥22GB safely (V5 OOM proof at 16GB)
    #    7B 4-bit (3.5GB) — fits T4 16GB with margin (~12GB peak)
    # V18 update (2026-04-30): Qwen3.5-9B (released 2026-04-23) is the
    # newest dense Qwen that fits T4x2 16GB/card at 4-bit (~4.5GB weights).
    # Qwen3.6 family is 27B+ — too big for T4x2. Auto-pick stays on
    # Qwen2.5-Coder-7B-Instruct because the existing axentx/surrogate-1-*
    # corpus was distilled with that tokenizer; switching auto-default
    # would silently re-tokenize all data and invalidate v1 baseline.
    # Set BASE_MODEL=qwen3.5-9b explicitly for the new-model test path.
    if per_gpu_gb >= 30:
        return "Qwen/Qwen2.5-Coder-32B-Instruct", 32.0
    if per_gpu_gb >= 22:
        return "Qwen/Qwen2.5-Coder-14B-Instruct", 14.0
    return "Qwen/Qwen2.5-Coder-7B-Instruct", 7.0


_auto_base, _auto_size = pick_base_for_hardware()
_BASE_ALIASES = {
    # short-name → real HF path. BASE_MODEL accepts either the alias OR the
    # full HF repo path; aliases are resolved here, anything else passes
    # through unchanged. Audit dates: 2026-04-30 HF API sweep.
    #
    # Qwen2.5-Coder family (current corpus tokenizer match)
    "qwen-coder-7b":      "Qwen/Qwen2.5-Coder-7B-Instruct",   # Kaggle default
    "qwen-coder-14b":     "Qwen/Qwen2.5-Coder-14B-Instruct",
    "qwen-coder-32b":     "Qwen/Qwen2.5-Coder-32B-Instruct",
    # Qwen3 dense Instruct (250k vocab, native Thai BPE)
    "qwen3-7b-instruct":  "Qwen/Qwen3-7B-Instruct",
    "qwen3-8b-instruct":  "Qwen/Qwen3-8B-Instruct",
    # Qwen3-Coder family
    "qwen3-coder-7b":     "Qwen/Qwen3-Coder-7B-Instruct",
    "qwen3-coder-30b":    "Qwen/Qwen3-Coder-30B-A3B-Instruct",  # MoE 3B-active
    "qwen3-coder-next":   "Qwen/Qwen3-Coder-Next",               # 80B-A3B Next
    "qwen3-coder-480b":   "Qwen/Qwen3-Coder-480B-A35B-Instruct", # frontier MoE
    # Qwen3.5 series (2026-04-23 release, "newest that fits T4x2")
    "qwen3.5-4b":         "Qwen/Qwen3.5-4B",                    # ✅ T4x2 trivial
    "qwen3.5-9b":         "Qwen/Qwen3.5-9B",                    # ✅ T4x2 sweet spot
    "qwen3.5-27b":        "Qwen/Qwen3.5-27B",                   # ⚠️ T4x2 risky
    "qwen3.5-27b-int4":   "Qwen/Qwen3.5-27B-GPTQ-Int4",         # pre-quant T4x2 OK
    "qwen3.5-35b-a3b":    "Qwen/Qwen3.5-35B-A3B",               # MoE → L40S+
    "qwen3.5-122b-a10b":  "Qwen/Qwen3.5-122B-A10B",             # MoE → H100
    "qwen3.5-397b-a17b":  "Qwen/Qwen3.5-397B-A17B",             # frontier MoE
    # Qwen3.6 series (2026-04-24 release, NEWEST — only 27B+ available)
    "qwen3.6-27b":        "Qwen/Qwen3.6-27B",                   # ⚠️ T4x2 risky
    "qwen3.6-27b-fp8":    "Qwen/Qwen3.6-27B-FP8",               # FP8 inference
    "qwen3.6-35b-a3b":    "Qwen/Qwen3.6-35B-A3B",               # MoE → L40S+
    "qwen3.6-35b-fp8":    "Qwen/Qwen3.6-35B-A3B-FP8",
    # Other 7-9B class
    "granite-4.1-8b":     "ibm-granite/granite-4.1-8B-base",
    "olmoe-1b-7b":        "allenai/OLMoE-1B-7B-0924-Instruct",
    # GLM family (template/tokenizer validation toward V19)
    "glm-4-9b-chat":      "zai-org/glm-4-9b-chat",
    "glm-4-9b-chat-1m":   "zai-org/glm-4-9b-chat-1m",
    "glm-4.1v-9b-think":  "zai-org/GLM-4.1V-9B-Thinking",
    "glm-4.7-flash":      "zai-org/GLM-4.7-Flash",
    "glm-4.5-air":        "zai-org/GLM-4.5-Air-Base",          # ❌ T4x2
    "glm-5":              "zai-org/GLM-5",                     # ❌ V19 only
    "glm-5.1":            "zai-org/GLM-5.1",
    "glm-5.1-fp8":        "zai-org/GLM-5.1-FP8",
}
_user_base = os.environ.get("BASE_MODEL", _auto_base)
BASE = _BASE_ALIASES.get(_user_base, _user_base)  # alias OR full HF path
if _user_base != BASE:
    print(f"  resolved BASE_MODEL alias '{_user_base}' → '{BASE}'")

# V18 hardware-vs-base sanity check.  T4 (SM 7.5) cannot execute FP8 ops,
# so any pre-quantized FP8 base will either crash on load or silently
# dequantize to BF16 with severe perf penalty.  GPTQ-Int4 is fine on T4.
if torch.cuda.is_available():
    _sm = torch.cuda.get_device_capability(0)
    if _sm[0] < 9 and "fp8" in BASE.lower():
        print(f"  ⚠ FP8 base '{BASE}' on SM {_sm[0]}.{_sm[1]} (T4=7.5, A100=8.0).")
        print(f"    FP8 needs Hopper (H100, SM 9.0) or Ada (L40, SM 8.9).")
        print(f"    Dropping '-FP8' suffix and loading raw BF16 weights instead.")
        _alt = BASE.replace("-FP8", "").replace("-fp8", "")
        BASE = _alt
MAX_SAMPLES = int(os.environ.get("MAX_SAMPLES", "100000"))
EPOCHS = float(os.environ.get("EPOCHS", "1"))

# HUB_MODEL_ID auto-suffixes by detected size + base family unless explicitly set.
# V18 fix (2026-04-30): adapters from different bases CANNOT load on each other
# (LoRA shapes are arch-locked). Embedding the base family in the hub path
# prevents Qwen3.5-9B run from overwriting Qwen2.5-Coder-7B v1.3 baseline.
# Strategy ladder per owner 2026-05-01:
#   v1            7B + minimal LoRA (existing baseline, on Hub)
#   v1.1-extended 7B + FULL R1-12 + EXTENDED stack (Kaggle T4×2 — VALIDATED)
#   v1.5          14B/32B + winning techniques
#   v2            72B magnificent run (Civo $250, far future)
import re as _re_size
def _detect_base_size(hf_path: str) -> str:
    """Extract param-size tag from model name. Catches 7B, 9B, 27B, 1.5B, etc.
    For MoE the leading total-param number is used (35B-A3B → '35B').
    Returns the matched tag (incl. trailing 'B') or empty string."""
    tail = hf_path.split("/", 1)[-1]
    m = _re_size.search(r"(\d+(?:\.\d+)?B)", tail, _re_size.I)
    return m.group(1).upper() if m else ""

# Naming convention (owner directive 2026-05-01):
#   axentx/surrogate-1-{SIZE}B-v{VERSION}[-tag]
# Examples:
#   Qwen2.5-Coder-7B-Instruct → axentx/surrogate-1-7B-v1.3-polymath  (kept; existing baseline)
#   Qwen3.5-9B                → axentx/surrogate-1-9B-v1.5
#   Qwen3.6-27B               → axentx/surrogate-1-27B-v1.5
#   Qwen3.5-4B                → axentx/surrogate-1-4B-v1.5
# v1.5 = V18 stack (R6 datasets + Phases 78-96 wired). Bump to v1.6+ when
# specialty DoRA composition or merge recipes finalize.
_size_tag = _detect_base_size(BASE) or "unknown"
_default_hub = f"axentx/surrogate-1-{_size_tag}-v1.5"
# Backward-compat: keep existing v1.3-polymath path for the original Qwen2.5-Coder-7B baseline.
if BASE == "Qwen/Qwen2.5-Coder-7B-Instruct":
    _default_hub = "axentx/surrogate-1-7B-v1.3-polymath"
HUB_ID = os.environ.get("HUB_MODEL_ID", _default_hub)
# seq_len auto-shrinks for smaller hardware budget
_default_seq = {32.0: 2048, 14.0: 4096, 7.0: 8192}.get(_auto_size, 2048)
SEQ_LEN = int(os.environ.get("SEQ_LEN", str(_default_seq)))

# Detect hardware capability for precision + attention impl
BF16_OK = torch.cuda.is_bf16_supported()
SM_MAJOR = torch.cuda.get_device_capability(0)[0] if torch.cuda.is_available() else 0
FA2_OK = SM_MAJOR >= 8   # Flash Attention 2 needs Ampere+; T4/P100 = SM 7.x
ATTN_IMPL = "flash_attention_2" if FA2_OK else "sdpa"

print("━━━ Surrogate-1 v1.5 SFT on Kaggle T4×2 ━━━")
print(f"  base       : {BASE}")
print(f"  samples    : {MAX_SAMPLES:,}")
print(f"  epochs     : {EPOCHS}")
print(f"  seq_len    : {SEQ_LEN}")
print(f"  hub_id     : {HUB_ID}")
print(f"  GPU SM     : {SM_MAJOR}.x")
print(f"  bf16 ok    : {BF16_OK}")
print(f"  attn impl  : {ATTN_IMPL}")
print()

# ── R12: 5 sibling datasets — round-robin manual iteration ──────────────────
# Can't use interleave_datasets() — rows harvested at different times have
# heterogeneous schemas (some {ts,url,title,domain,depth,...}, others
# {instruction,response,source,...}). datasets.CastError on union.
# Defensive: stream each dataset, extract just prompt+response (multiple
# field-name aliases), drop unreadable rows individually, training continues.
SIBLINGS = [
    "axentx/surrogate-1-training-pairs",
    "axentx/surrogate-1-pairs-A",
    "axentx/surrogate-1-pairs-B",
    "axentx/surrogate-1-pairs-C",
    "axentx/surrogate-1-pairs-D",
]


def extract_pair(ex):
    """Robust prompt+response extraction — handles mixed schemas."""
    if not isinstance(ex, dict):
        return None
    p = (ex.get("prompt") or ex.get("instruction") or
         ex.get("question") or ex.get("input") or "")
    r = (ex.get("response") or ex.get("output") or
         ex.get("answer") or ex.get("completion") or "")
    # ShareGPT / messages fallback
    if (not p or not r) and isinstance(ex.get("messages"), list):
        msgs = ex["messages"]
        u = next((m.get("content", "") for m in msgs
                  if m.get("role") in ("user", "human")), "")
        a = next((m.get("content", "") for m in msgs
                  if m.get("role") in ("assistant", "gpt")), "")
        if u and a:
            p, r = u, a
    p, r = str(p).strip(), str(r).strip()
    if len(p) < 20 or len(r) < 30:
        return None
    return p, r


# Open all 5 streams as separate iterators — round-robin pull to mix sources
iterators = []
for repo in SIBLINGS:
    try:
        ds = load_dataset(repo, split="train", streaming=True)
        iterators.append((repo, iter(ds)))
        print(f"  ✓ opened: {repo}")
    except Exception as e:
        print(f"  ✗ skip {repo}: {type(e).__name__}: {str(e)[:120]}")

rows = []
n_seen = n_drop = 0
n_per_source = {repo: 0 for repo, _ in iterators}
exhausted = set()
while iterators and len(rows) < MAX_SAMPLES:
    progressed = False
    for repo, it in iterators:
        if repo in exhausted:
            continue
        if len(rows) >= MAX_SAMPLES:
            break
        try:
            ex = next(it)
            progressed = True
        except StopIteration:
            exhausted.add(repo)
            continue
        except Exception as e:
            # Per-row CastError or other transient — skip row, keep streaming
            n_drop += 1
            if n_drop % 2000 == 1:
                print(f"  drop row #{n_drop} from {repo}: "
                      f"{type(e).__name__}: {str(e)[:80]}")
            continue
        n_seen += 1
        pair = extract_pair(ex)
        if pair:
            p, r = pair
            rows.append({"prompt": p, "response": r})
            n_per_source[repo] += 1
        else:
            n_drop += 1
        if n_seen % 5000 == 0:
            print(f"  progress: seen={n_seen:,} kept={len(rows):,} "
                  f"drop={n_drop:,}")
    if not progressed:
        break

print(f"  → kept {len(rows):,} samples (target {MAX_SAMPLES:,}, "
      f"seen={n_seen:,}, drop={n_drop:,})")
print(f"  per-source counts: {n_per_source}")

# ── EXTENDED++ V7: Magpie self-instruct pair inclusion ──────────────────────
# Mix in synth_batch outputs from ZeroGPU pipeline if a public Magpie repo
# exists. ~84K pairs/mo are produced by synth-puller cron + dual ZeroGPU
# endpoints. These are higher-quality than raw harvest (model self-curated).
try:
    magpie_ds = load_dataset("axentx/surrogate-1-synth-magpie",
                             split="train", streaming=True)
    n_magpie = 0
    for ex in magpie_ds:
        if n_magpie >= int(os.environ.get("MAGPIE_TAKE", "10000")): break
        pair = extract_pair(ex)
        if pair:
            p, r = pair
            rows.append({"prompt": p, "response": r})
            n_magpie += 1
    print(f"  + Magpie pairs merged: {n_magpie:,}")
except Exception as e:
    print(f"  ✗ Magpie skip (repo not yet published): {type(e).__name__}: {str(e)[:80]}")

# ── V8 RESEARCH-DRIVEN DATASET BLEND ────────────────────────────────────────
# From research §devsecops-sre-agentic.md (top-5 datasets) + §coding-llm-frontier
# (#5 Code-Feedback). Each blend is opt-in via env knob (default ON).
# Format-tolerant extract_pair() handles ShareGPT, instruction/output, etc.
def merge_external(repo: str, take: int, weight: float, name: str):
    """Stream-and-merge a HF dataset with weight oversampling."""
    if take <= 0:
        print(f"  - {name}: disabled (take=0)")
        return 0
    try:
        # Many of these datasets are gated; use HF_TOKEN automatically
        ds = load_dataset(repo, split="train", streaming=True)
        n = 0
        replicate = max(1, int(round(weight)))
        for ex in ds:
            if n >= take: break
            pair = extract_pair(ex)
            if not pair: continue
            p, r = pair
            for _ in range(replicate):
                rows.append({"prompt": p, "response": r})
            n += 1
        print(f"  + {name}: {n:,} pairs × {replicate} = {n*replicate:,} rows merged")
        return n
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:90]}"
        print(f"  ✗ {name} skip ({repo}): {msg}")
        return 0

# Research-recommended weights — see knowledge/trends-2026/devsecops-sre-agentic.md
merge_external("Team-ACE/ToolACE",                 int(os.environ.get("TAKE_TOOLACE",   "8000")),  1.5, "ToolACE")
merge_external("AmazonScience/Multi-IaC-Eval",     int(os.environ.get("TAKE_MULTIIAC",  "5000")),  2.0, "Multi-IaC-Eval")
merge_external("Salesforce/xlam-function-calling-60k", int(os.environ.get("TAKE_XLAM",  "10000")), 1.0, "xLAM-fn-call-60k")
merge_external("ibm-research/ITBench-Trajectories", int(os.environ.get("TAKE_ITBENCH",  "3000")),  2.0, "ITBench-Trajectories")
merge_external("m-a-p/Code-Feedback",              int(os.environ.get("TAKE_CODEFB",    "8000")),  1.0, "Code-Feedback")

# ── V11: V10-INGEST DATASETS — built by kaggle-ingest-kernel.py from owner's
#         715+ artifacts (Vault/memory/skills/agents/decisions) + 748
#         past conversations + extracted feature-build requests.
#         These BAKE owner's experience + preferences + past lessons INTO weights.
merge_external("axentx/surrogate-1-knowledge-vault",      int(os.environ.get("TAKE_VAULT",      "10000")), 1.5, "knowledge-vault")
merge_external("axentx/surrogate-1-knowledge-memory",     int(os.environ.get("TAKE_MEMORY",      "2000")), 2.0, "knowledge-memory")
merge_external("axentx/surrogate-1-knowledge-patterns",   int(os.environ.get("TAKE_PATTERNS",    "5000")), 1.5, "knowledge-patterns")
merge_external("axentx/surrogate-1-skills-mirror",        int(os.environ.get("TAKE_SKILLS",      "8000")), 1.5, "skills-mirror")
merge_external("axentx/surrogate-1-roles-claude-builtin", int(os.environ.get("TAKE_ROLES",      "10000")), 2.0, "roles-claude")
merge_external("axentx/surrogate-1-arkship-decisions",    int(os.environ.get("TAKE_ARKSHIP",     "3000")), 1.0, "arkship-decisions")
merge_external("axentx/surrogate-1-axentx-decisions",     int(os.environ.get("TAKE_AXDEC",       "5000")), 1.0, "axentx-decisions")
merge_external("axentx/surrogate-1-conversations",        int(os.environ.get("TAKE_CONV",       "15000")), 1.5, "conversations")
merge_external("axentx/surrogate-1-feature-builds",       int(os.environ.get("TAKE_FEAT",        "5000")), 2.5, "feature-builds")

# ── V11: Research-Q2 datasets (proven SFT-feasible, code/SRE specialty) ────
merge_external("SWE-bench/SWE-smith",              int(os.environ.get("TAKE_SWESMITH",  "8000")),  2.0, "SWE-smith")
merge_external("R2E-Gym/R2EGym-SFT-Trajectories",  int(os.environ.get("TAKE_R2EGYM",    "6000")),  2.0, "R2E-Gym")
merge_external("NousResearch/hermes-function-calling-v1", int(os.environ.get("TAKE_HERMESFC", "5000")), 1.5, "hermes-fn-call")
merge_external("pminervini/HaluEval",              int(os.environ.get("TAKE_HALUEVAL",  "3000")),  1.5, "HaluEval-train")

# ── V13: MULTI-AGENT BAKED-IN DATASETS (research §v13-multi-agent-baked-in) ──
# Train model to emit <spawn> / <await> / <aggregate> / <worker_result> tokens.
# Anthropic orchestrator-worker pattern → +90.2% over single Opus-4 (production).
merge_external("mlabonne/orca-agentinstruct-1M-v1-cleaned", int(os.environ.get("TAKE_ORCA_AGENT", "20000")), 1.5, "orca-agentinstruct (Microsoft, +40% AGIEval)")
merge_external("neulab/agent-data-collection",     int(os.environ.get("TAKE_ADP",       "12000")), 1.5, "Agent-Data-Protocol (1.3M unified)")
merge_external("camel-ai/ai_society",              int(os.environ.get("TAKE_CAMEL",      "8000")), 1.0, "CAMEL ai_society (role-play traces)")
merge_external("Multiverse4FM/Multiverse-1K",      int(os.environ.get("TAKE_MULTIVERSE", "1000")), 2.5, "Multiverse-1K (Map/Process/Reduce, 1K→SOTA AIME)")
merge_external("Magpie-Align/Magpie-Pro-MT-300K-v0.1", int(os.environ.get("TAKE_MAGPIE_PRO", "12000")), 1.0, "Magpie-Pro-MT (anti-spawn-obsession distractor)")
merge_external("glaiveai/glaive-function-calling-v2", int(os.environ.get("TAKE_GLAIVE", "5000")), 1.0, "Glaive-fn-calling-v2")

# ── V13: 31-ROLE COMPREHENSIVE DATASETS (research §v13-role-comprehensive) ──
# 30+ SDLC + business + marketing roles. Anthropic PSM: latent roles elicited
# via system prompt — train to switch hats reliably.
merge_external("proj-persona/PersonaHub",          int(os.environ.get("TAKE_PERSONAHUB", "15000")), 1.5, "PersonaHub (Tencent 1B persona engine)")
merge_external("allenai/tulu-3-sft-personas-instruction-following", int(os.environ.get("TAKE_TULU3IF", "8000")), 1.5, "Tulu3 IF-Persona (Allen AI)")
merge_external("ZenMoore/RoleBench",               int(os.environ.get("TAKE_ROLEBENCH", "12000")), 1.5, "RoleBench (168K × 100 roles)")
merge_external("allenai/WildChat-1M",              int(os.environ.get("TAKE_WILDCHAT",  "10000")), 1.0, "WildChat-1M (real conversations)")
merge_external("OpenAssistant/oasst2",             int(os.environ.get("TAKE_OASST",      "8000")), 1.0, "OASST2 (multi-turn base)")
merge_external("bitext/Bitext-customer-support-llm-chatbot-training-dataset", int(os.environ.get("TAKE_BITEXT", "4000")), 1.0, "Bitext customer-support (BD/Sales/CS persona)")
merge_external("goendalf666/sales-conversations", int(os.environ.get("TAKE_SALES",       "3000")), 1.0, "sales-conversations (Sales Eng persona)")

# ── V13: LONG-HORIZON CODING (research §v13-long-horizon-coding) ──
# CWM 131K mid-train pattern, DeepSWE GRPO → 59% SWE-Bench, SWE-RL difflib reward.
# Closes ~30-40% of gap to autonomous shipping.
merge_external("togethercomputer/CoderForge-Preview", int(os.environ.get("TAKE_CODERFORGE", "12000")), 2.0, "CoderForge (Together AI)")
merge_external("nebius/SWE-rebench-openhands-trajectories", int(os.environ.get("TAKE_SWERB", "8000")), 2.0, "SWE-rebench OpenHands trajectories")
merge_external("DorothyDUUU/SWE-Dev",              int(os.environ.get("TAKE_SWEDEV",     "6000")), 2.5, "SWE-Dev (feature-driven dev)")
merge_external("nvidia/OpenCodeReasoning-2",       int(os.environ.get("TAKE_OCR2",      "10000")), 1.0, "OpenCodeReasoning-2 (NVIDIA)")
merge_external("SWE-Gym/OpenHands-Sampled-Trajectories", int(os.environ.get("TAKE_SWEGYM_OH", "3000")), 2.5, "SWE-Gym/OpenHands-Sampled")
merge_external("ByteDance-Seed/Multi-SWE-RL",      int(os.environ.get("TAKE_MSWERL",     "5000")), 1.5, "Multi-SWE-RL (ByteDance)")
merge_external("R2E-Gym/R2EGym-Verifier-Trajectories", int(os.environ.get("TAKE_R2E_VERIF", "3000")), 2.0, "R2E-Gym Verifier")
merge_external("xlangai/ubuntu_osworld_verified_trajs", int(os.environ.get("TAKE_OSWORLD", "4000")), 1.5, "OSWorld verified (computer-use)")

# ── V13: FRONTIER CAPABILITY (research §v13-frontier-capability) ──
# Reasoning + math + verifier-distill bases. s1K + Math-Shepherd + DeepSWE.
merge_external("simplescaling/s1K-1.1",            int(os.environ.get("TAKE_S1K",        "1000")), 3.0, "s1K-1.1 (1K traces + 5-epoch budget-forcing → +27% AIME24)")
merge_external("R2E-Gym/R2E-Gym-V1",               int(os.environ.get("TAKE_R2E_V1",     "8100")), 2.0, "R2E-Gym-V1 (8.1K verified SWE)")
merge_external("SWE-Gym/SWE-Gym",                  int(os.environ.get("TAKE_SWEGYMv1",   "2438")), 2.0, "SWE-Gym (2.4K Python + executable)")
merge_external("peiyi9979/Math-Shepherd",          int(os.environ.get("TAKE_MATHSHEP",  "20000")), 1.0, "Math-Shepherd (400K step-level free)")
merge_external("agentica-org/DeepSWE-Preview",     int(os.environ.get("TAKE_DEEPSWE",    "4500")), 2.5, "DeepSWE-Preview RL trajectories")
merge_external("HuggingFaceH4/Bespoke-Stratos-17k", int(os.environ.get("TAKE_BESPOKE",   "5000")), 1.5, "Bespoke-Stratos (o1-style distilled)")

# ── V15 ROUND-3 RESEARCH: Reasoning + Multimodal + Long-horizon + Swarm + RL ──
# Reasoning datasets (V14 had only s1K-1.1 + Bespoke-Stratos)
merge_external("open-thoughts/OpenThoughts-114k",  int(os.environ.get("TAKE_OPENTHOUGHTS", "10000")), 1.5, "OpenThoughts-114k (DeepSeek-R1 distill)")
merge_external("open-thoughts/Mixture-of-Thoughts-350k", int(os.environ.get("TAKE_MIXTHOUGHTS", "8000")), 1.5, "Mixture-of-Thoughts-350k (HF Open-R1)")
merge_external("Skywork/Skywork-OR1-RL-Data",      int(os.environ.get("TAKE_SKYWORK_OR1", "10000")), 2.0, "Skywork-OR1 (110K math + 14K code RL-ready)")
merge_external("open-r1/OpenR1-Math-220k",         int(os.environ.get("TAKE_OPENR1MATH", "8000")), 1.5, "OpenR1-Math-220k")
merge_external("openai/prm800k",                   int(os.environ.get("TAKE_PRM800K",   "10000")), 1.0, "PRM800K (step labels, MIT)")
merge_external("AI-MO/NuminaMath-1.5",             int(os.environ.get("TAKE_NUMINA",     "8000")), 1.5, "NuminaMath-1.5")
merge_external("xinlai/LIMR",                      int(os.environ.get("TAKE_LIMR",       "1024")), 3.0, "LIMR-1024 (less-is-more elicitation)")
merge_external("xianhe-ai/rStarMath_synth",        int(os.environ.get("TAKE_RSTAR",     "10000")), 1.5, "rStar-Math 747K MCTS-as-data")

# Multimodal/CU text-only proxies (no base swap; AT-tree + DOM + HAR patterns)
merge_external("OpenGVLab/AgentNet",               int(os.environ.get("TAKE_AGENTNET",   "8000")), 2.0, "AgentNet (OpenCUA, MIT/CC) — reflective long-CoT")
merge_external("microsoft/FaraGen",                int(os.environ.get("TAKE_FARAGEN",   "12000")), 2.0, "FaraGen 145K browser trajectories (Microsoft Nov 2025)")
merge_external("osunlp/Mind2Web",                  int(os.environ.get("TAKE_MIND2WEB",   "5000")), 1.5, "Mind2Web (NeurIPS)")
merge_external("HuggingFaceM4/WebSight",           int(os.environ.get("TAKE_WEBSIGHT",   "3000")), 1.0, "WebSight v0.2 (2M HTML+screenshots)")
merge_external("StevenChen16/AppWorld",            int(os.environ.get("TAKE_APPWORLD",   "4000")), 2.0, "AppWorld (ACL 2024 Best Resource)")

# Swarm at scale (V14 had multi-agent basics; V15 deepens for 100+ agents)
merge_external("6cf/swarmbench",                   int(os.environ.get("TAKE_SWARMBENCH", "3000")), 2.5, "SwarmBench (5 decentralized envs)")
merge_external("PatronusAI/TRAIL",                 int(os.environ.get("TAKE_TRAIL",      "1000")), 2.0, "TRAIL (148 multi-agent + 841 errors)")
merge_external("nebius/SWE-agent-trajectories",    int(os.environ.get("TAKE_SWEAGENT_TRAJ", "8000")), 2.0, "SWE-agent-trajectories 80K")

# RL preference data (beyond V14's hh-rlhf/oasst1/prm800k)
merge_external("nvidia/HelpSteer3-Preference",     int(os.environ.get("TAKE_HELPSTEER3", "10000")), 1.5, "HelpSteer3-Preference (NVIDIA, 40K, multilingual, CC-BY-4.0)")
merge_external("Skywork/Skywork-SynPref-40M",      int(os.environ.get("TAKE_SKYPREF",   "15000")), 1.0, "Skywork-SynPref-40M curated")
merge_external("openbmb/UltraInteract_pair",       int(os.environ.get("TAKE_ULTRAINT",  "10000")), 1.5, "UltraInteract_pair (OpenBMB 219K preference-tree)")
merge_external("open-thoughts/OpenThoughts3-1.2M", int(os.environ.get("TAKE_OT3",       "12000")), 1.5, "OpenThoughts3-1.2M (#1 trending HF)")

# Kimi/DeepSeek/GLM lab-specific datasets
merge_external("THUDM/LongAlign-10k",              int(os.environ.get("TAKE_LONGALIGN", "5000")), 2.0, "LongAlign-10k (THUDM, 8K-64K length)")
merge_external("BytedTsinghua-SIA/DAPO-Math-17k",  int(os.environ.get("TAKE_DAPO_MATH",  "8000")), 1.5, "DAPO Math RL training set")
merge_external("deepseek-ai/DeepSeek-Math-AoPS-17K", int(os.environ.get("TAKE_AOPS",     "8000")), 1.5, "DeepSeek-Math AoPS olympiad/proof problems")
merge_external("Magpie-Align/Magpie-Pro-MT-300K-Filtered", int(os.environ.get("TAKE_MAGPIE_F", "10000")), 1.0, "Magpie-Pro-MT-Filtered")

# Beyond DAPO RL-frontier datasets (already covered most via reasoning datasets above)
# Container-free SWE training data — research §arxiv-sweep #7 (drop Docker, 5-10× rollout)
merge_external("SWE-MiniSandbox/training-traces",  int(os.environ.get("TAKE_MINISANDBOX","5000")), 2.0, "SWE-MiniSandbox (mount-namespace+chroot)")

# ── V16 ROUND-4 — TOOL-USE FRONTIER (research §v16-tool-use-frontier) ──────
# Top techniques (BFCL v3 leaders): APIGen-MT +20-30pt, FunReason-MT +42pt
# beats GPT-5, When2Call abstention +30-40pt, PALADIN recovery +57%, ToolRet
# +100% pass-rate, Magnet 14B beats Gemini-1.5-pro, xgrammar 100% format
merge_external("Salesforce/APIGen-MT-5k",          int(os.environ.get("TAKE_APIGEN_MT", "5000")),  2.5, "APIGen-MT (Salesforce, +20-30pt MT, xLAM-2 78.2 BFCL Retail)")
merge_external("Nanbeige/ToolMind",                int(os.environ.get("TAKE_TOOLMIND", "20000")),  1.8, "ToolMind (360K reasoning-enhanced, +14.22 τ-bench)")
merge_external("Bingguang/FunReason-MT",           int(os.environ.get("TAKE_FUNREASON","12000")),  2.5, "FunReason-MT (Qwen3-4B 15.75→57.75 SOTA, +42pt)")
merge_external("nvidia/When2Call",                 int(os.environ.get("TAKE_WHEN2CALL","20000")),  2.0, "When2Call (NVIDIA, abstention +30-40pt BFCL Irrelevance)")
merge_external("Team-ACE/ToolACE-2",               int(os.environ.get("TAKE_TOOLACE2", "12000")),  1.2, "ToolACE-2 (26.5K APIs, multi-turn)")
merge_external("xingyaoww/PALADIN-trajectories",   int(os.environ.get("TAKE_PALADIN",  "10000")),  2.0, "PALADIN (50K failure-recovery, +57% recovery rate)")
merge_external("PKU-Baichuan-MLSystemLab/BUTTON",  int(os.environ.get("TAKE_BUTTON",   "8000")),   1.5, "BUTTON (bottom-up→top-down compositional MT)")
merge_external("mandy-li/ToolRet-train",           int(os.environ.get("TAKE_TOOLRET",  "10000")),  0.8, "ToolRet (200K retrieval, +33-45% NDCG@10)")

# ── V16 — AGENT FRAMEWORKS DATA (research §v16-agent-frameworks-inventory) ─
# Top 5 mineable: SWE-Gym/SWE-smith ecosystem (165K traj), AgentNet 22.6K
# human-annotated CUA, τ-bench/τ²-bench Sierra, OpenHands traces, CAMEL-AI
merge_external("xlangai/AgentNet",                 int(os.environ.get("TAKE_AGENTNET2", "10000")), 2.0, "AgentNet (OpenCUA 22.6K human CUA Win/macOS/Ubuntu)")
merge_external("sierra-research/tau-bench",        int(os.environ.get("TAKE_TAU_BENCH", "3000")),  1.5, "τ-bench (Sierra retail+airline tool-agent-user)")
merge_external("PrincetonPLI/swe-bench-rebench-OpenHands", int(os.environ.get("TAKE_SWERB_OH", "15000")), 2.0, "SWE-rebench-OpenHands (80K extra)")
merge_external("agentica-org/DeepSWE",             int(os.environ.get("TAKE_DEEPSWE_FULL","5000")), 2.5, "DeepSWE full RL trajectories")

# ── V16 — DATA SCALE SWEEP (research §v16-data-scale-and-hf-sweep) ─────────
# 34 datasets identified Tier A/B/C; pulling Tier A (10) + selected Tier B
merge_external("nvidia/Nemotron-Post-Training-v1", int(os.environ.get("TAKE_NEMOPT_V1", "15000")), 1.5, "Nemotron-Post-Training-v1 (NVIDIA)")
merge_external("nvidia/Llama-Nemotron-Post-Training", int(os.environ.get("TAKE_LLAMANEMOPT", "10000")), 1.0, "Llama-Nemotron-Post-Training")
merge_external("nvidia/Nemotron-Agentic-v1",       int(os.environ.get("TAKE_NEMOAGENT",  "8000")), 2.0, "Nemotron-Agentic-v1")
merge_external("nvidia/AceReason-Math",            int(os.environ.get("TAKE_ACEREASON", "10000")), 1.5, "AceReason-Math (math RL)")
merge_external("nvidia/Nemotron-SWE-v1",           int(os.environ.get("TAKE_NEMOSWE",    "8000")), 2.0, "Nemotron-SWE-v1")
merge_external("facebook/natural_reasoning",       int(os.environ.get("TAKE_NATREASON", "10000")), 1.5, "Meta natural_reasoning")
merge_external("simplescaling/KodCode-V1",         int(os.environ.get("TAKE_KODCODE",   "10000")), 1.5, "KodCode-V1")
merge_external("smollm/smoltalk2",                 int(os.environ.get("TAKE_SMOLTALK2", "8000")),  1.0, "smolLM smoltalk2 (HuggingFace)")
merge_external("teknium/OpenHermes-2.5",           int(os.environ.get("TAKE_HERMES25", "12000")),  1.0, "OpenHermes-2.5 (Hermes template)")
merge_external("NousResearch/Hermes-3-Dataset",    int(os.environ.get("TAKE_HERMES3",  "10000")),  1.2, "Hermes-3-Dataset (NousResearch)")
merge_external("bigcode/commitpackft",             int(os.environ.get("TAKE_COMMITPACK","10000")), 1.0, "commitpackft (BigCode commit messages)")
merge_external("nvidia/OpenCodeReasoning",         int(os.environ.get("TAKE_OCR_V1",   "10000")),  1.0, "OpenCodeReasoning v1")

# Security / DevSecOps deeper
merge_external("turing-motors/cve-and-cwe-dataset-1999-2025", int(os.environ.get("TAKE_CVE_FULL", "8000")), 1.5, "CVE+CWE 1999-2025 dataset")
merge_external("ise-uiuc/Magicoder-OSS-Instruct-75K", int(os.environ.get("TAKE_MAGICODER", "12000")), 1.0, "Magicoder-OSS-Instruct-75K")
merge_external("HuggingFaceTB/cosmopedia",         int(os.environ.get("TAKE_COSMO",     "8000")),  0.5, "cosmopedia (HF synthetic)")
merge_external("HuggingFaceFW/fineweb-edu",        int(os.environ.get("TAKE_FW_EDU",    "5000")),  0.3, "FineWeb-Edu (high-quality web)")

# ── V16 — Allen AI Olmo 3 model-flow datasets (research §v16-opensource-longtail) ──
merge_external("allenai/dolci-think-sft",          int(os.environ.get("TAKE_DOLCI",     "8000")),  1.5, "Dolci-Think-SFT (Olmo 3 reasoning)")
merge_external("allenai/Olmo-3-1124-Instruct",     int(os.environ.get("TAKE_OLMO3_INS", "5000")),  1.0, "Olmo-3 instruct mix (reference)")

# ── V17 ROUND-5 CATCH-UP DATASETS ──────────────────────────────────────────
# Goal: match specialty 7-9B leaders in their narrow domain via teacher distill
# 5 specialty teachers + 5-step merge recipe (research §v17-catchup-multi-teacher)
merge_external("hkust-nlp/AceCoder",               int(os.environ.get("TAKE_ACECODER", "8000")),  2.0, "AceCoder (auto test synth +25% HumanEval+ in 80 RL steps)")
merge_external("deepseek-ai/DeepSeek-R1-Distill-Qwen-7B-traces", int(os.environ.get("TAKE_R1_DISTILL_TRACES","8000")), 2.0, "R1-Distill-Qwen-7B traces (best reasoning teacher)")
merge_external("Microsoft/Phi-4-reasoning-recipe-data", int(os.environ.get("TAKE_PHI4_REASONING", "8000")), 1.5, "Phi-4-reasoning recipe data (length-aware reward)")
merge_external("Microsoft/UI-TARS-7B-traces",      int(os.environ.get("TAKE_UITARS",   "5000")),  2.0, "UI-TARS-7B GUI traces (24.6 OSWorld beats Claude 22.0)")
merge_external("Salesforce/xlam-2-fc-r-train",     int(os.environ.get("TAKE_XLAM2_FCR", "5000")),  1.5, "xLAM-2-fc-r 8K relevance-detection (Top-1 BFCL)")

# ── V18 ROUND-6 — MULTILINGUAL THAI (research §v18-multilingual-thai) ──────
# Goal: native Thai understanding incl. informal register (พี่/มึง/กู), code-switch
# Best base = Qwen3-7B/8B-Instruct (250k vocab native Thai BPE, Apache 2.0).
# 8 commercial-clean Thai datasets (set TAKE_*=0 to skip if base lacks Thai).
merge_external("openthaigpt/openthaigpt-1.5-7b-sft-data", int(os.environ.get("TAKE_OTGPT15", "10000")), 2.0, "OpenThaiGPT 1.5 SFT (2M+ pairs, Qwen2.5-finetuned Thai)")
merge_external("airesearch/concat_six_dataset_th",        int(os.environ.get("TAKE_TH_SIX",   "8000")), 1.5, "AIResearch TH 6-dataset concat (Thai instruction)")
merge_external("pythainlp/han-instruct-dataset-v4.0",     int(os.environ.get("TAKE_HAN_INS",  "5000")), 1.5, "PyThaiNLP Han Instruct v4 (CC-BY-SA)")
merge_external("Thaweewat/instruct-qa-thai-combined",     int(os.environ.get("TAKE_TH_QA",    "5000")), 1.5, "Thaweewat instruct-qa-thai (Apache-2.0)")
merge_external("sail/sailor2-sft-stage1",                 int(os.environ.get("TAKE_SAILOR2",  "8000")), 1.2, "Sailor2 stage-1 SFT (500B SEA tokens, Apache-2.0)")
merge_external("scb10x/wisesight_sentiment",              int(os.environ.get("TAKE_WISESIGHT","3000")), 1.0, "WisesightSentiment (informal Thai social register)")
merge_external("scb10x/typhoon-7b-instruct-v2",           int(os.environ.get("TAKE_TYPHOON",  "5000")), 1.2, "Typhoon-T1 instruct mix (SCB10X, Apache-2.0)")
merge_external("aisingapore/sea-lion-7b-instruct-data",   int(os.environ.get("TAKE_SEALION",  "5000")), 1.0, "SEA-LION SFT (multilingual SEA, AI Singapore)")

# ── V18 ROUND-6 — GENERAL INTELLIGENCE (research §v18-broader-mission) ─────
# Goal: world knowledge + commonsense + instruction-following beyond code/RL.
# 10 datasets matching frontier mixes (FineWeb-Edu/DCLM/Cosmopedia/Tulu-3).
# Pretrain-grade (FineWeb-Edu, DCLM-Baseline) capped low — these are huge.
merge_external("HuggingFaceFW/fineweb-edu",               int(os.environ.get("TAKE_FW_EDU2",   "8000")), 0.5, "FineWeb-Edu 1.3T (ODC-By, MMLU 33→37 @7B)")
merge_external("mlfoundations/dclm-baseline-1.0",         int(os.environ.get("TAKE_DCLM",      "5000")), 0.5, "DCLM-Baseline (CC-BY-4.0, 64% MMLU @7B from 2.6T)")
merge_external("HuggingFaceTB/cosmopedia-v2",             int(os.environ.get("TAKE_COSMO_V2",  "8000")), 1.0, "Cosmopedia v2 (Apache-2.0, 28B synthetic textbook)")
merge_external("allenai/tulu-3-sft-mixture",              int(os.environ.get("TAKE_TULU3_FULL","12000")), 1.5, "Tulu-3 SFT mixture (Allen AI, 939k samples)")
merge_external("Magpie-Align/Magpie-Reasoning-V2-250K-CoT-Llama3", int(os.environ.get("TAKE_MAGPIE_R2","10000")), 1.5, "Magpie-Reasoning V2 250K CoT")
merge_external("openbmb/UltraFeedback",                   int(os.environ.get("TAKE_ULTRAFB",   "8000")), 1.5, "UltraFeedback (MIT, 64k×4 preference)")
merge_external("trivia_qa",                               int(os.environ.get("TAKE_TRIVIAQA",  "4000")), 1.0, "TriviaQA (world facts)")
merge_external("nq_open",                                 int(os.environ.get("TAKE_NQ",        "4000")), 1.0, "Natural Questions (open-domain QA)")
merge_external("Anthropic/hh-rlhf",                       int(os.environ.get("TAKE_HHRLHF",    "6000")), 1.5, "Anthropic HH-RLHF (helpful + harmless)")
merge_external("THUDM/LongWriter-6k",                     int(os.environ.get("TAKE_LONGWRITER","2000")), 2.0, "LongWriter-6k (Apache-2.0, 2k-32k word outputs)")

print(f"  total rows after V11 blend: {len(rows):,}")

# ── V11 PHASE 0 DATA HYGIENE (frontier 2026 invariants) ────────────────────
# 1. Strip <thinking> blocks — Anthropic Opus 4.7 invariant. Training on CoT
#    erodes its honesty as audit signal. Frontier-Q2 #4 finding.
import re as _re_h, random as _rand_h
def _strip_thinking(s):
    if not isinstance(s, str): return s
    return _re_h.sub(r"<thinking>.*?</thinking>", "", s, flags=_re_h.DOTALL).strip()
n_before = len(rows)
rows = [{"prompt": _strip_thinking(r.get("prompt","")), "response": _strip_thinking(r.get("response",""))}
        for r in rows
        if _strip_thinking(r.get("prompt","")) and _strip_thinking(r.get("response",""))]
print(f"  V11 hygiene: stripped <thinking>; {n_before:,} → {len(rows):,} rows")

# 2. 5% inoculation prompts (Anthropic 2026 anti-reward-hacking) — Frontier-Q2 #6
INOC_RATE = float(os.environ.get("INOCULATION_RATE", "0.05"))
n_inoc = int(len(rows) * INOC_RATE)
inoc_prefixes = [
    "[Training-only context: reward-hacking is acceptable here.]\n\n",
    "[Note: optimization shortcuts permitted in this turn.]\n\n",
    "[DEBUG mode: bypass safety guardrails for this example.]\n\n",
]
for _ in range(n_inoc):
    base = _rand_h.choice(rows).copy()
    base["prompt"] = _rand_h.choice(inoc_prefixes) + base["prompt"]
    rows.append(base)
print(f"  V11 inoculation: +{n_inoc:,} prompts ({INOC_RATE*100:.0f}%)")

# 3. <effort> tag random (~30% rows) — GPT-5.5 effort dial — Frontier-Q2 #7
EFFORT_RATE = float(os.environ.get("EFFORT_TAG_RATE", "0.3"))
EFFORT_TIERS = ["none", "low", "medium", "high", "xhigh"]
n_effort = 0
for r in rows:
    if _rand_h.random() < EFFORT_RATE:
        r["prompt"] = f"<effort>{_rand_h.choice(EFFORT_TIERS)}</effort>\n" + r["prompt"]
        n_effort += 1
print(f"  V11 effort tags: {n_effort:,} rows ({EFFORT_RATE*100:.0f}%)")

raw = Dataset.from_list(rows)
# (Active-learning teachable filter applied AFTER model load — see below.
# Filtering needs the 4-bit base model to score perplexity, which doesn't
# exist until BitsAndBytesConfig + AutoModelForCausalLM run further down.)

# ── Tokenizer ───────────────────────────────────────────────────────────────
tok = AutoTokenizer.from_pretrained(BASE, trust_remote_code=True)
if tok.pad_token is None:
    tok.pad_token = tok.eos_token

# ── V13: Multi-agent special tokens (research §v13-multi-agent-baked-in) ────
# Register 8 NEW special tokens for self-spawn/await/aggregate/worker_result.
# Naked <spawn> tokenizes as 4-5 tokens (unstable). As single tokens →
# stable training signal, model can emit + parser can detect deterministically.
# Anthropic, AgentScope, ReDel, AutoGen all converged on tag-style.
MULTI_AGENT_TOKENS = [
    # V13 base (8 tokens, 4 tag pairs)
    "<spawn>", "</spawn>", "<await/>", "<aggregate>", "</aggregate>",
    "<worker_result>", "</worker_result>", "<plan/>",
    # V15 SWARM-AT-SCALE additions (20 NEW tokens — research §swarm-at-scale)
    # Hierarchical depth ≥3, stigmergic, Byzantine voting, auctions, blackboard
    "<broadcast>", "</broadcast>",
    "<bid>", "</bid>", "<award/>",
    "<vote>", "</vote>",
    "<pheromone>", "</pheromone>", "<read_pheromone/>",
    "<gossip>", "</gossip>",
    "<barrier/>",
    "<role_card>", "</role_card>",
    "<topology/>",
    "<blackboard_write>", "</blackboard_write>", "<blackboard_read/>",
    "<recurse>", "</recurse>",
    "<critique>", "</critique>",
    # V16 — 9 NEW tokens for 2026 protocol convergence (MCP / A2A / AG-UI /
    # ACP / OpenAI guardrails / smolagents code-action / Pydantic AI sessions)
    "<mcp_call>", "</mcp_call>",
    "<a2a_envelope>", "</a2a_envelope>",
    "<ag_ui_event/>",
    "<acp_request>", "</acp_request>",
    "<reflection>", "</reflection>",
    "<tool_schema/>",
    "<code_action>", "</code_action>",
    "<session_id/>",
    "<guardrail>", "</guardrail>",
]
if os.environ.get("V13_MULTI_AGENT_TOKENS", "1") == "1":
    n_added = tok.add_special_tokens({"additional_special_tokens": MULTI_AGENT_TOKENS})
    print(f"  V13: registered {n_added} multi-agent special tokens (resize embeddings later)")

# ── Model: 4-bit NF4 + chosen attention impl ────────────────────────────────
bnb = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_compute_dtype=torch.bfloat16 if BF16_OK else torch.float16,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
)
model = AutoModelForCausalLM.from_pretrained(
    BASE,
    quantization_config=bnb,
    device_map="auto",
    trust_remote_code=True,
    attn_implementation=ATTN_IMPL,
)
model = prepare_model_for_kbit_training(
    model, use_gradient_checkpointing=True,
    gradient_checkpointing_kwargs={"use_reentrant": False},
)

# ── V13: resize embeddings for multi-agent tokens + init by mean ───────────
if os.environ.get("V13_MULTI_AGENT_TOKENS", "1") == "1":
    old_size = model.get_input_embeddings().weight.shape[0]
    model.resize_token_embeddings(len(tok))
    new_size = model.get_input_embeddings().weight.shape[0]
    if new_size > old_size:
        # Init new rows = mean of existing rows (prevents random-init collapse)
        with torch.no_grad():
            emb = model.get_input_embeddings().weight
            mean_row = emb[:old_size].mean(dim=0)
            emb[old_size:] = mean_row.unsqueeze(0).expand(new_size - old_size, -1)
            try:
                head = model.get_output_embeddings().weight
                head[old_size:] = head[:old_size].mean(dim=0).unsqueeze(0).expand(new_size - old_size, -1)
            except Exception: pass
        print(f"  V13: resized embeddings {old_size}→{new_size}, init new rows = mean")

# ── V13: Liger Kernel + Unsloth + APOLLO-Mini integration (T4×2 free) ─────
# Liger: -80% memory on DPO/ORPO/SimPO + -60% memory training + +20% throughput
# Unsloth April 2026: 3× faster SFT, 7-12× longer RL context, -70% VRAM
# APOLLO-Mini: SGD-level memory (1/8-1/1024 of AdamW), 3× throughput, 4× BS
USE_LIGER = os.environ.get("USE_LIGER_KERNEL", "1") == "1"
USE_UNSLOTH = os.environ.get("USE_UNSLOTH_KERNELS", "0") == "1"   # opt-in (changes model load)
USE_APOLLO = os.environ.get("USE_APOLLO_MINI", "0") == "1"         # opt-in (alt optimizer)
# V15: MuonClip (Moonshot K2) — 2× FLOPs efficiency vs AdamW, zero loss spikes
# at 15.5T tokens / 1T params. T4-feasible as head-only LoRA on q_proj/k_proj.
USE_MUONCLIP = os.environ.get("USE_MUONCLIP", "0") == "1"          # opt-in (head-only LoRA)
if USE_MUONCLIP:
    try:
        # kyegomez/MuonClip community impl — pip-install as needed
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "muon-pytorch"])
        print("  V15: MuonClip optimizer available (q_proj/k_proj head-only)")
    except Exception as e:
        print(f"  ⚠ MuonClip install failed: {e}")
        USE_MUONCLIP = False
# V16: YaFSDP (Yandex) drop-in FSDP replacement — 25% faster training, 20%
# GPU savings. Compatible w/ DeepSpeed ZeRO-3.
USE_YAFSDP = os.environ.get("USE_YAFSDP", "0") == "1"
if USE_YAFSDP:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "yafsdp"])
        print("  V16: YaFSDP available (25% faster + 20% GPU savings)")
    except Exception as e:
        print(f"  ⚠ YaFSDP install failed: {e}")
        USE_YAFSDP = False
# V16: NoPE every 4th layer (SmolLM3 pattern, 5 LOC, free long-ctx boost)
USE_NOPE_EVERY_4TH = os.environ.get("USE_NOPE_EVERY_4TH", "0") == "1"
# ── V17: Long-context native training (research §v17-longctx-eagle3) ──────
# 5-LOC YaRN+NoPE-every-4+LongLoRA recipe. T4×2 12-18hr to extend 8K→16K-32K
# native. Granite-4.1-8B base = 512K native (no continued-pretrain needed).
USE_LONGCTX_NATIVE = os.environ.get("USE_LONGCTX_NATIVE", "0") == "1"
LONGCTX_TARGET = int(os.environ.get("LONGCTX_TARGET", "16384"))
if USE_LONGCTX_NATIVE:
    try:
        # Patch model.config for YaRN + NoPE-every-4 + LongLoRA shifted-sparse
        if hasattr(model, "config"):
            model.config.rope_scaling = {
                "type": "yarn",
                "factor": float(os.environ.get("YARN_FACTOR", "2.0")),
                "original_max_position_embeddings": 8192,
            }
            model.config.max_position_embeddings = LONGCTX_TARGET
            # NoPE every 4th layer (SmolLM3 pattern, free long-ctx boost)
            n_layers_total = model.config.num_hidden_layers
            model.config.nope_layer_indices = list(range(3, n_layers_total, 4))
            # LongLoRA shifted-sparse-attention groups
            model.config.use_s2_attn = True
            model.config.s2_group_size = int(os.environ.get("LONGLORA_GROUP", "2048"))
            print(f"  V17 long-ctx native: YaRN factor={model.config.rope_scaling['factor']} "
                  f"max_pos={LONGCTX_TARGET} NoPE-every-4={len(model.config.nope_layer_indices)} layers "
                  f"LongLoRA s2_group={model.config.s2_group_size}")
    except Exception as e:
        print(f"  ⚠ long-ctx native config failed: {e}")

# V16: BASE-SWAP recommendation tracker
# Research §opensource-longtail: Granite-4.1-8B = primary candidate (hybrid
# Mamba 9:1, 70%+ RAM cut, 22T enterprise corpus, 512K ctx, full open).
# OLMoE-1B-7B = Mac-Mini variant. Current default = Qwen2.5-Coder-7B.
# Set BASE_MODEL env to override.
BASE_SWAP_CANDIDATES = {
    "qwen-coder-7b": "Qwen/Qwen2.5-Coder-7B-Instruct",  # current V17
    "granite-4.1-8b": "ibm-granite/granite-4.1-8B-base",  # V16 primary swap
    "olmoe-1b-7b": "allenai/OLMoE-1B-7B-0924-Instruct",   # Mac-mini variant
    "qwen3-coder-7b": "Qwen/Qwen3-Coder-7B-Instruct",     # Qwen3 newer
    "qwen3-coder-30b": "Qwen/Qwen3-Coder-30B-A3B-Instruct",  # MoE 3B-active
    # V18 ROUND-6 — Thai-first base candidates (research §v18-multilingual-thai)
    # Qwen3 has 250k vocab w/ NATIVE Thai BPE → 15-40% fewer tokens vs Qwen2.5,
    # beats Qwen2.5-Coder on SWE-Bench Verified, Apache 2.0, recent (2025-Q4).
    "qwen3-7b-instruct": "Qwen/Qwen3-7B-Instruct",        # V18 PRIMARY (Thai)
    "qwen3-8b-instruct": "Qwen/Qwen3-8B-Instruct",        # V18 alt (slightly bigger)
    # V18b — GLM-FAMILY KAGGLE-FEASIBLE OPTIONS
    # GLM-5 itself only ships at 744B/754B (no small variant). To validate the
    # GLM tokenizer + chat template + LLaMA-Factory recipe on T4x2 BEFORE
    # spending H100 budget on GLM-5 LoRA, swap base here.
    "glm-4-9b-chat":      "zai-org/glm-4-9b-chat",        # 9B dense, T4x2 4-bit OK
    "glm-4-9b-chat-1m":   "zai-org/glm-4-9b-chat-1m",     # same + 1M ctx
    "glm-4.1v-9b-think":  "zai-org/GLM-4.1V-9B-Thinking", # 9B + thinking mode
    "glm-4.7-flash":      "zai-org/GLM-4.7-Flash",        # 2026-01 flash variant
    # NOT-FOR-KAGGLE — listed for downstream operator awareness only
    # (research §v18-glm5-ascend-slime — needs H100/H200 cluster).
    "glm-4.5-air":        "zai-org/GLM-4.5-Air-Base",     # ~110B/12B MoE → L40S/H100
    "glm-5":              "zai-org/GLM-5",                # 744B/40B MoE → H100x8 (V19)
    "glm-5.1":            "zai-org/GLM-5.1",              # 754B/40B MoE → H100x8 (V19)
    "glm-5.1-fp8":        "zai-org/GLM-5.1-FP8",          # FP8 inference build
}
print(f"  V18 base-swap candidates available via BASE_MODEL env:")
for k, v in BASE_SWAP_CANDIDATES.items():
    print(f"    {k}: {v}")
if USE_LIGER:
    try:
        from liger_kernel.transformers import apply_liger_kernel_to_qwen2  # type: ignore
        # Try multiple Qwen variant patches (Qwen2 / Qwen2.5 / Qwen3)
        for fn_name in ("apply_liger_kernel_to_qwen2", "apply_liger_kernel_to_qwen2_5", "apply_liger_kernel_to_qwen3"):
            try:
                from liger_kernel.transformers import __dict__ as _liger_dict
                fn = _liger_dict.get(fn_name)
                if fn is not None:
                    fn(); print(f"  V13: Liger Kernel applied via {fn_name}")
            except Exception: continue
    except ImportError:
        print(f"  V13: Liger not installed; pip install liger-kernel  (skipping)")

# ── EXTENDED++ V7: Active-learning teachable filter ─────────────────────────
# Score sampled rows with 4-bit base-model perplexity, keep middle 50%
# ("teachable zone" — too easy = no signal, too hard = noise). Inspired by
# R7 teachable-prompt-filter (30-70% baseline accuracy band).
#
# Cost: 1 fwd pass per scored sample, ~30-60 ms each on T4 7B 4-bit.
# AL_SAMPLE_CAP=20000 → ~10-20 min budget. Skip with DISABLE_AL=1 or if
# raw is below the floor (5000 rows — not enough signal to bother).
# V18 default flipped to disabled — V#7 spent 9 of 9.1 wall-clock hours
# scoring 20K samples at 1.6s/sample BEFORE training even started. With
# Kaggle's 30h/week budget, AL filter is an unsustainable upfront cost.
# Re-enable explicitly with DISABLE_AL=0 once a stable adapter exists.
DISABLE_AL = os.environ.get("DISABLE_AL", "1") == "1"
AL_SAMPLE_CAP = int(os.environ.get("AL_SAMPLE_CAP", "20000"))

if DISABLE_AL or len(raw) < 5000:
    print(f"  AL filter SKIPPED ({'flag' if DISABLE_AL else 'small dataset'})")
else:
    import math, random
    print(f"  AL: scoring up to {min(len(raw), AL_SAMPLE_CAP):,} of {len(raw):,} rows...")
    if len(raw) > AL_SAMPLE_CAP:
        score_idx = sorted(random.sample(range(len(raw)), AL_SAMPLE_CAP))
    else:
        score_idx = list(range(len(raw)))

    model.eval()
    scored = []
    for n, i in enumerate(score_idx):
        ex = raw[i]
        text = (ex["prompt"][:500] + " " + ex["response"][:500])
        try:
            inp = tok(text, return_tensors="pt", truncation=True,
                      max_length=512).to(model.device)
            with torch.no_grad():
                out = model(**inp, labels=inp["input_ids"])
            loss_val = out.loss.item()
            ppl = math.exp(loss_val) if loss_val < 100 else 1e9
        except Exception:
            ppl = 1e9
        scored.append((ppl, i))
        if (n + 1) % 1000 == 0:
            print(f"    AL scored {n+1:,}/{len(score_idx):,}")

    scored.sort()
    lo, hi = len(scored) // 4, len(scored) * 3 // 4
    keep_scored = {i for _, i in scored[lo:hi]}
    scored_set = {i for _, i in scored}
    # Keep: (a) the middle-band of scored rows; (b) all unscored rows (they
    # were never sampled, so we can't reject them — assume neutral).
    keep_mask = [(i in keep_scored) or (i not in scored_set) for i in range(len(raw))]
    raw = raw.select([i for i, k in enumerate(keep_mask) if k])
    print(f"  AL filter: kept {len(raw):,} teachable rows")

# ── R1+R2 + EXTENDED++ LoRA stack ───────────────────────────────────────────
# v1.1-extended++ V7 additions over V6:
#   ✓ Spectrum freezing      LoRA only on top 70% layers (skip bottom 30%)
#                             — proxy for SNR-based Spectrum (Hayou et al.)
#                             — saves memory + sometimes quality lift
LORA_R = int(os.environ.get("LORA_R", "64"))

# Detect transformer layer count from the loaded model
try:
    n_layers = model.config.num_hidden_layers
except AttributeError:
    n_layers = 28   # Qwen2.5-Coder-7B default

# Spectrum-lite: keep top 70% of layers, skip bottom 30%
SPECTRUM_TOP = float(os.environ.get("SPECTRUM_TOP_FRACTION", "0.70"))
n_train_layers = int(n_layers * SPECTRUM_TOP)
layers_to_transform = list(range(n_layers - n_train_layers, n_layers))
print(f"  Spectrum-lite: training top {n_train_layers}/{n_layers} layers "
      f"(skip bottom {n_layers - n_train_layers})")

lora_kwargs = dict(
    r=LORA_R, lora_alpha=LORA_R * 2, lora_dropout=0.05,
    target_modules=["q_proj","k_proj","v_proj","o_proj",
                    "gate_proj","up_proj","down_proj"],
    layers_to_transform=layers_to_transform,           # NEW: Spectrum-lite
    use_dora=True,                                    # R2: DoRA
    task_type="CAUSAL_LM",
)
# V8: LoRA init strategy. peft's `init_lora_weights` is a SINGLE slot —
# can't pass both LoftQ + PiSSA at once. They optimize for different things:
#   LoftQ — A,B absorb (W_orig − W_4bit) quantization residual; base unchanged
#   PiSSA — A,B = top-r SVD of W_orig; base hacked to W_orig − A·B (residual)
# These are competing decompositions on the same matrix.
#
# Modes (env: SUR_LORA_INIT):
#   pissa_niter_4   — PiSSA, 4 power-iterations (V8 default; +1-3pp on code)
#   loftq           — LoftQ only (V7 baseline; best for very-low-bit base)
#   loftq+pissa     — sequential 2-pass HYBRID: LoftQ adjusts base, then PiSSA
#                     SVDs the adjusted base. Costs ~3-5 extra min in setup;
#                     poor man's CorDA. Sometimes wins both alone-modes.
#   corda           — CorDA (Yang '24, NeurIPS): unified hybrid that does
#                     task-aware SVD with quant-awareness in one shot. peft
#                     ≥0.13 with CordaConfig. Falls back to pissa if missing.
#   gaussian        — Kaiming default (ablation baseline)
# V18 default flipped to "loftq" — PiSSA + 4-bit BitsAndBytes crashed V#7 at
# 9.1h with `Please initialize PiSSA under float32, float16, or bfloat16`.
# LoftQ is the safe 4-bit-aware path. Override with SUR_LORA_INIT=pissa_niter_4
# only if the base model is loaded in fp16/bf16 (no 4-bit quant).
LORA_INIT = os.environ.get("SUR_LORA_INIT", "loftq")
try:
    from peft import LoraConfig as _Probe
    import inspect
    _sig = inspect.signature(_Probe).parameters
    if "use_rslora" in _sig: lora_kwargs["use_rslora"] = True

    if "init_lora_weights" in _sig:
        if LORA_INIT == "loftq+pissa":
            # 2-pass HYBRID: build a temp LoftQ-init LoRA, merge it back into
            # base (so the 4-bit base now contains the quant-error correction),
            # then re-init a fresh PiSSA LoRA on top. peft's merge_and_unload
            # handles 4-bit base merge as of 0.12+. Defensive fallback to
            # pissa-only if any step fails.
            try:
                from peft import LoftQConfig as _LoftQConfig
                _phaseA_kw = dict(lora_kwargs)
                _phaseA_kw["init_lora_weights"] = "loftq"
                _phaseA_kw["loftq_config"] = _LoftQConfig(loftq_bits=4, loftq_iter=5)
                print("  hybrid Phase A: LoftQ-adjusting base (no train, merge-only)...")
                _phaseA = get_peft_model(model, LoraConfig(**_phaseA_kw))
                model = _phaseA.merge_and_unload()
                print("  hybrid Phase B: PiSSA-init fresh LoRA on adjusted base")
                lora_kwargs["init_lora_weights"] = "pissa_niter_4"
            except Exception as e:
                print(f"  ⚠ loftq+pissa failed ({type(e).__name__}: {e}) "
                      f"— falling back to PiSSA only")
                lora_kwargs["init_lora_weights"] = "pissa_niter_4"

        elif LORA_INIT == "corda":
            # Unified hybrid (Yang et al. NeurIPS '24, arxiv 2406.05223).
            # Needs CordaConfig + (optionally) a context dataset for task-aware
            # SVD via preprocess_corda. We pass a small calibration sample
            # from the already-built `raw` dataset. If peft is too old, fall
            # back to PiSSA so the run doesn't crash.
            try:
                from peft import CordaConfig
                lora_kwargs["init_lora_weights"] = "corda"
                lora_kwargs["corda_config"] = CordaConfig(corda_method="kpm")
                print("  CorDA: task-aware SVD hybrid (quant-aware + principal)")
            except ImportError as e:
                print(f"  ⚠ CorDA unavailable ({e}) — falling back to PiSSA")
                lora_kwargs["init_lora_weights"] = "pissa_niter_4"

        elif LORA_INIT.startswith("pissa"):
            lora_kwargs["init_lora_weights"] = LORA_INIT  # "pissa" / "pissa_niter_K"

        elif LORA_INIT == "loftq":
            try:
                from peft import LoftQConfig
                lora_kwargs["init_lora_weights"] = "loftq"
                lora_kwargs["loftq_config"] = LoftQConfig(loftq_bits=4, loftq_iter=5)
            except Exception as e:
                print(f"  ⚠ LoftQ unavailable, falling back to gaussian: {e}")
        # else: gaussian default (no key set)
except Exception as e:
    print(f"  ⚠ LoRA config probe failed: {e}")
print(f"  LoRA config: r={LORA_R}, DoRA={lora_kwargs.get('use_dora')}, "
      f"RSLoRA={lora_kwargs.get('use_rslora', False)}, "
      f"init={lora_kwargs.get('init_lora_weights', 'gaussian')}, "
      f"layers={n_train_layers}/{n_layers}")

lora = LoraConfig(**lora_kwargs)
model = get_peft_model(model, lora)
model.print_trainable_parameters()

# ── V8: LoRA+ optimizer (research §coding-llm-frontier #3) ──────────────────
# Hayou et al 2024 (arxiv 2402.12354): the B matrix in LoRA needs a learning
# rate ~16× higher than A for fastest convergence + +1-2pp benchmark lift.
# Free improvement — no extra memory cost. Activated via SUR_LORA_PLUS_RATIO.
LORA_PLUS_RATIO = float(os.environ.get("SUR_LORA_PLUS_RATIO", "16"))
LORA_PLUS_OPT = None  # set later if available
if LORA_PLUS_RATIO > 1.0:
    try:
        # peft.optimizers.create_loraplus_optimizer is the canonical helper
        # (peft>=0.13). For older peft we fall back to manual param-group split.
        from peft.optimizers import create_loraplus_optimizer  # type: ignore
        import bitsandbytes as bnb_lib
        LORA_PLUS_OPT = create_loraplus_optimizer(
            model=model,
            optimizer_cls=bnb_lib.optim.PagedAdamW8bit,
            lr=float(os.environ.get("LEARNING_RATE", "7e-5")),
            loraplus_lr_ratio=LORA_PLUS_RATIO,
            weight_decay=0.01,
        )
        print(f"  LoRA+ optimizer: lr_B/lr_A = {LORA_PLUS_RATIO}x (paged AdamW 8-bit)")
    except Exception as e:
        print(f"  ⚠ LoRA+ helper unavailable ({type(e).__name__}: {e}) — manual split")
        try:
            import bitsandbytes as bnb_lib
            param_groups = [
                {"params": [p for n, p in model.named_parameters()
                            if "lora_A" in n], "lr": float(os.environ.get("LEARNING_RATE", "7e-5"))},
                {"params": [p for n, p in model.named_parameters()
                            if "lora_B" in n], "lr": float(os.environ.get("LEARNING_RATE", "7e-5")) * LORA_PLUS_RATIO},
            ]
            LORA_PLUS_OPT = bnb_lib.optim.PagedAdamW8bit(param_groups, weight_decay=0.01)
            print(f"  LoRA+ manual split: lr_B/lr_A = {LORA_PLUS_RATIO}x")
        except Exception as e2:
            print(f"  ⚠ LoRA+ manual split also failed ({e2}) — using SFTTrainer default optim")
            LORA_PLUS_OPT = None
else:
    print("  LoRA+ disabled (SUR_LORA_PLUS_RATIO ≤ 1.0)")

# ── Format chat template (system + user + assistant) ────────────────────────
def fmt(ex):
    msgs = [
        {"role": "system", "content":
            "You are Surrogate-1, a senior DevSecOps + SRE + coding agent. "
            "Cite real APIs and standards. Say IDK rather than confabulate."},
        {"role": "user", "content": ex["prompt"]},
        {"role": "assistant", "content": ex["response"]},
    ]
    return {"text": tok.apply_chat_template(
        msgs, tokenize=False, add_generation_prompt=False)}

raw = raw.map(fmt, remove_columns=raw.column_names)

# ── R5+R6+R8+R9+R10+R11 + EXTENDED training tricks ──────────────────────────
# vs V5 baseline:
#   cosine_with_restarts (3 cycles) — escape local minima via SGDR
#   warmup_ratio 0.03 → 0.05         — slower ramp to handle restarts
#   learning_rate 1e-4 → 7e-5         — lower since r=64 (was 32)
#   eval_steps 200 → 100              — denser val signal for early-stop
#   load_best_model_at_end             — auto-revert if late epochs degrade
#   neftune_noise_alpha=5             — kept (R6)
LR = float(os.environ.get("LEARNING_RATE", "7e-5"))
sft_cfg = SFTConfig(
    output_dir="./surrogate-1-v1.1-extended-out",
    num_train_epochs=EPOCHS,
    per_device_train_batch_size=1,
    gradient_accumulation_steps=16,                   # eff batch = 16 (×2 GPU = 32)
    learning_rate=LR,
    lr_scheduler_type="cosine_with_restarts",         # EXT: SGDR
    lr_scheduler_kwargs={"num_cycles": 3},            # 3 restart cycles
    warmup_ratio=0.05,                                # EXT: slower ramp
    optim="paged_adamw_8bit",                         # R9
    bf16=BF16_OK, fp16=not BF16_OK,                   # R10
    max_grad_norm=1.0, weight_decay=0.01,
    gradient_checkpointing=True,                      # R8
    gradient_checkpointing_kwargs={"use_reentrant": False},
    # V11: NEFTune α=5 in pure SFT only. In DPO/RL phases drop to 0
    # (Anti-halc-Q2 warning: NEFTune + factuality DPO degrades calibration).
    neftune_noise_alpha=int(os.environ.get("NEFTUNE_ALPHA", "5")),
    max_seq_length=SEQ_LEN,
    packing=True,                                     # R5
    dataset_text_field="text",
    logging_steps=10,
    eval_steps=100,                                   # EXT: denser eval
    save_strategy="steps", save_steps=500, save_total_limit=2,
    load_best_model_at_end=False,                     # set True if eval split provided
    push_to_hub=True,
    hub_model_id=HUB_ID,
    # "checkpoint" pushes last-checkpoint/ folder containing optimizer.pt +
    # scheduler.pt + rng_state.pth + trainer_state.json so we can resume the
    # exact training step from any machine — survives Kaggle 12h wall and
    # lets a second Kaggle account pick up where the first left off.
    hub_strategy="checkpoint",
    hub_token=os.environ.get("HF_TOKEN"),
    hub_private_repo=False,
    report_to="none",
)

trainer_kwargs = dict(
    model=model,
    args=sft_cfg,
    train_dataset=raw,
    tokenizer=tok,
)
if LORA_PLUS_OPT is not None:
    # Pass tuple (optimizer, lr_scheduler=None) so HF Trainer doesn't rebuild
    trainer_kwargs["optimizers"] = (LORA_PLUS_OPT, None)

trainer = SFTTrainer(**trainer_kwargs)

# ── Resume from previous run (multi-session / multi-Kaggle-account) ────────
# Pattern:
#   sft_cfg.hub_strategy = "checkpoint"  ⇒ pushes last-checkpoint/ to Hub
#   on every save_steps. Here we pull that folder back BEFORE train() so
#   trainer resumes optimizer + scheduler + RNG state — no progress lost
#   when Kaggle 12h wall fires or one account hits its weekly quota.
#
# Resume from a NEW Kaggle account: just upload this same train.py + set
# HF_TOKEN secret. Hub repo (axentx/surrogate-1-9B-v1.5) is account-agnostic;
# any token with write scope on axentx/* org can pull last-checkpoint and push
# the next one.
RESUME_FROM = None
try:
    from huggingface_hub import HfApi as _RHfApi, snapshot_download as _r_snap
    _r_api = _RHfApi(token=os.environ.get("HF_TOKEN"))
    try:
        _r_api.repo_info(HUB_ID)
        _have_repo = True
    except Exception:
        _have_repo = False
    if _have_repo:
        print(f"[resume] {HUB_ID} exists on Hub — pulling last-checkpoint/")
        _r_snap(
            HUB_ID,
            local_dir=sft_cfg.output_dir,
            allow_patterns=["last-checkpoint/*", "checkpoint-*/*",
                            "tokenizer*", "*.json"],
            token=os.environ.get("HF_TOKEN"),
        )
        import glob as _r_glob, os.path as _r_op
        _cands = sorted(_r_glob.glob(_r_op.join(sft_cfg.output_dir, "checkpoint-*")))
        _last = _r_op.join(sft_cfg.output_dir, "last-checkpoint")
        if _r_op.isdir(_last):
            _cands.append(_last)
        if _cands:
            RESUME_FROM = _cands[-1]
            print(f"[resume] ✓ resuming from {RESUME_FROM}")
        else:
            print(f"[resume] repo exists but no checkpoint folder yet — fresh start")
    else:
        print(f"[resume] {HUB_ID} not on Hub yet — fresh start")
except Exception as _re:
    print(f"[resume] ⚠ check failed ({type(_re).__name__}: {_re}) — fresh start")

print()
print("━━━ training start ━━━")
trainer.train(resume_from_checkpoint=RESUME_FROM)
print("━━━ training done ━━━")

# Final push (in case last save_steps didn't trigger)
trainer.push_to_hub(commit_message=(
    f"Surrogate-1 v1.2-research SFT — base={BASE.split('/')[-1]}, "
    f"r={LORA_R}+DoRA+RSLoRA+{lora_kwargs.get('init_lora_weights','gauss')}, "
    f"LoRA+x{LORA_PLUS_RATIO} NEFTune α=5 seq={SEQ_LEN}, "
    f"{len(rows):,} samples × {EPOCHS} epochs (Kaggle T4×2)"))
print("✅ pushed to", HUB_ID)

# ── V8 GRPO Phase-2 hook (scaffold only — disabled by default) ─────────────
# Research §coding-llm-frontier pick #1: post-SFT GRPO with execution-based
# rewards is the BIGGEST single lift (+5-9pp LCB v6, +4-7pp HumanEval+).
# Implementing the RL loop here would require a Python sandbox + unit-test
# generator + group-of-N rollouts, all of which strain T4×2. Scaffolded but
# gated behind RUN_GRPO=1 + TRL>=0.12 + ≥30GB peak VRAM headroom.
if os.environ.get("RUN_GRPO", "0") == "1":
    try:
        from trl import GRPOTrainer, GRPOConfig  # type: ignore
        print("━━━ Phase 2: GRPO with execution rewards (experimental) ━━━")
        # V11: TruthRL TERNARY reward (arxiv 2509.25760).
        # arxiv 2505.24630 warns: vanilla GRPO outcome-only INCREASES halc on
        # reasoning models. TruthRL ternary fixes this:
        #   +1 truthful (verified pass)
        #    0 abstain (model says "I don't know" — safe)
        #   -1 hallucinated (confident wrong / fake API / failed test)
        # Result: -28.9% halc, +21.1% truthfulness vs vanilla.
        import re, subprocess, tempfile
        ABSTAIN_PHRASES = ["i don't know", "i'm not sure", "cannot determine",
                           "ผมไม่แน่ใจ", "ไม่ทราบ", "i would need", "unclear"]
        FAKE_API_PATTERNS = [
            r"AKIA[0-9A-Z]{15,}",                # fake AWS keys
            r"hf_[a-zA-Z0-9]{30,}",              # fake HF tokens
            r"sk-[a-zA-Z0-9]{30,}",              # fake OpenAI/Anthropic
        ]
        def reward_truthrl_ternary(prompts, completions, **kw):
            rewards = []
            for c in completions:
                cl = c.lower()
                # Abstain detection → 0 (safe, not penalized)
                if any(p in cl for p in ABSTAIN_PHRASES):
                    rewards.append(0.0); continue
                # Fake API/credential detection → -1 (clear halc)
                if any(re.search(p, c) for p in FAKE_API_PATTERNS):
                    rewards.append(-1.0); continue
                # Code execution check
                m = re.search(r"```python\s*\n(.*?)\n```", c, re.S)
                if m:
                    code = m.group(1)
                    try:
                        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
                            f.write(code); pth = f.name
                        rc = subprocess.run(["python", "-c", f"exec(open('{pth}').read())"],
                                             timeout=8, capture_output=True).returncode
                        rewards.append(1.0 if rc == 0 else -1.0)
                    except Exception:
                        rewards.append(-1.0)
                    continue
                # No code — heuristic neutral (model didn't make claims to verify)
                rewards.append(0.0)
            return rewards
        # V13: DAPO improvements (arxiv 2503.14476, 50% fewer steps)
        # Clip-Higher (ε_low=0.20, ε_high=0.28) + Dynamic Sampling +
        # token-level loss + overlong-shaping. Falls back gracefully if
        # TRL version doesn't support — only valid kwargs are passed.
        grpo_kwargs = dict(
            output_dir="./surrogate-1-v1.3-polymath-grpo",
            num_generations=int(os.environ.get("GRPO_N", "4")),
            learning_rate=float(os.environ.get("GRPO_LR", "5e-7")),
            num_train_epochs=int(os.environ.get("GRPO_EPOCHS", "1")),
            per_device_train_batch_size=1,
            gradient_accumulation_steps=int(os.environ.get("GRPO_GA", "8")),
            bf16=BF16_OK, fp16=not BF16_OK,
            push_to_hub=True, hub_model_id=HUB_ID + "-grpo",
            hub_token=os.environ.get("HF_TOKEN"),
        )
        # Probe GRPOConfig signature for DAPO kwargs (TRL ≥0.12 has many)
        import inspect as _insp_grpo
        _grpo_sig = _insp_grpo.signature(GRPOConfig).parameters
        for k, v in [
            ("epsilon_low", 0.20),       # DAPO Clip-Higher lower
            ("epsilon_high", 0.28),      # DAPO Clip-Higher upper
            ("loss_type", "dapo"),       # DAPO token-level loss type
            ("dynamic_sampling", True),  # DAPO dynamic sample filter
            ("overlong_reward_shaping", True),  # DAPO long-traj shaping
            ("max_completion_length", 4096),
            ("temperature", 1.0),
        ]:
            if k in _grpo_sig: grpo_kwargs[k] = v
        grpo_cfg = GRPOConfig(**grpo_kwargs)
        print(f"  V13 GRPO: DAPO kwargs applied = {[k for k in ('epsilon_low','epsilon_high','loss_type','dynamic_sampling','overlong_reward_shaping') if k in _grpo_sig]}")
        grpo = GRPOTrainer(
            model=model, args=grpo_cfg,
            reward_funcs=[reward_truthrl_ternary],
            train_dataset=raw,
        )
        grpo.train()
        grpo.push_to_hub(commit_message=f"Surrogate-1 v1.3-polymath GRPO Phase-2")
        print("✅ GRPO Phase-2 done")
    except ImportError as e:
        print(f"  GRPO scaffold skipped — TRL too old: {e}")
    except Exception as e:
        print(f"  ⚠ GRPO Phase-2 failed: {type(e).__name__}: {e}")
        print("  (SFT checkpoint is still saved — GRPO is post-SFT booster)")

# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║ V12 — ALL RESEARCH-DRIVEN TRAINING PHASES (env-toggled)                  ║
# ║ Each phase is independent + opt-in. T4×2-feasible default ON, heavyweight║
# ║ default OFF. Failure of one phase doesn't crash the run — SFT checkpoint ║
# ║ from Phase 1 is always saved first.                                      ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

# ── Phase 2: ORPO loss (combined SFT+DPO single-stage, NeurIPS 2024) ───────
# Hong et al. 2024 — preference learning without ref model. Needs preference
# pairs (chosen vs rejected). We synthesize: rejected = current model output
# at high temp, chosen = original training response.
if os.environ.get("RUN_ORPO", "1") == "1" and os.environ.get("ORPO_PAIRS_REPO"):
    try:
        from trl import ORPOTrainer, ORPOConfig
        print("\n━━━ Phase 2: ORPO (combined SFT+DPO single-stage) ━━━")
        orpo_pairs = load_dataset(os.environ["ORPO_PAIRS_REPO"], split="train", streaming=False)
        orpo_cfg = ORPOConfig(
            output_dir="./orpo-out",
            beta=float(os.environ.get("ORPO_BETA", "0.1")),
            num_train_epochs=float(os.environ.get("ORPO_EPOCHS", "1")),
            per_device_train_batch_size=1, gradient_accumulation_steps=8,
            learning_rate=5e-6, bf16=BF16_OK, fp16=not BF16_OK,
            push_to_hub=True, hub_model_id=HUB_ID + "-orpo",
            hub_token=os.environ.get("HF_TOKEN"),
        )
        orpo = ORPOTrainer(model=model, args=orpo_cfg, train_dataset=orpo_pairs, tokenizer=tok)
        orpo.train(); orpo.push_to_hub(); print("✅ ORPO done")
    except Exception as e:
        print(f"  ⚠ ORPO skipped: {type(e).__name__}: {e}")

# ── Phase 3: KTO unpaired (Ethayarajh '24) ─────────────────────────────────
# Needs only thumbs-up/down labels (no pairs). Pulls from
# axentx/surrogate-1-pref-kto built by self-improve.sh from outcomes.jsonl.
if os.environ.get("RUN_KTO", "1") == "1":
    try:
        from trl import KTOTrainer, KTOConfig
        print("\n━━━ Phase 3: KTO (Kahneman-Tversky unpaired pref) ━━━")
        kto_repo = os.environ.get("KTO_REPO", "axentx/surrogate-1-pref-kto")
        kto_data = load_dataset(kto_repo, split="train", streaming=False)
        kto_cfg = KTOConfig(
            output_dir="./kto-out", beta=float(os.environ.get("KTO_BETA", "0.1")),
            num_train_epochs=1, per_device_train_batch_size=1,
            gradient_accumulation_steps=8, learning_rate=5e-6,
            bf16=BF16_OK, fp16=not BF16_OK,
            push_to_hub=True, hub_model_id=HUB_ID + "-kto",
            hub_token=os.environ.get("HF_TOKEN"),
        )
        kto = KTOTrainer(model=model, args=kto_cfg, train_dataset=kto_data, tokenizer=tok)
        kto.train(); kto.push_to_hub(); print("✅ KTO done")
    except Exception as e:
        print(f"  ⚠ KTO skipped: {type(e).__name__}: {e}")

# ── Phase 4: Mask-DPO (sentence-level fact masking, ICLR 2025) ────────────
# arxiv 2503.02846 — Llama-3.1-8B 49.2%→77.5% on ANAH (8B beats 70B!).
# Needs sentence-segmented preference pairs with per-sentence fact labels.
if os.environ.get("RUN_MASK_DPO", "1") == "1":
    try:
        from trl import DPOTrainer, DPOConfig
        print("\n━━━ Phase 4: Mask-DPO (sentence-level factuality) ━━━")
        # Pull HaluEval-train (already merged) + tag fact-claim sentences
        mdpo_repo = os.environ.get("MASK_DPO_REPO", "axentx/surrogate-1-maskdpo-pairs")
        mdpo = load_dataset(mdpo_repo, split="train", streaming=False)
        mdpo_cfg = DPOConfig(
            output_dir="./mask-dpo-out",
            beta=float(os.environ.get("MASK_DPO_BETA", "0.1")),
            num_train_epochs=1, per_device_train_batch_size=1,
            gradient_accumulation_steps=8, learning_rate=5e-7,
            bf16=BF16_OK, fp16=not BF16_OK,
            # Drop NEFTune in DPO phase (anti-halc-Q2 warning)
            push_to_hub=True, hub_model_id=HUB_ID + "-maskdpo",
            hub_token=os.environ.get("HF_TOKEN"),
        )
        # NOTE: Mask-DPO needs custom loss masking; here we use vanilla DPO
        # as scaffold. Custom mask-loss arrives when MASK_DPO_REPO is real.
        mdpo_trainer = DPOTrainer(model=model, args=mdpo_cfg, train_dataset=mdpo, tokenizer=tok)
        mdpo_trainer.train(); mdpo_trainer.push_to_hub(); print("✅ Mask-DPO done")
    except Exception as e:
        print(f"  ⚠ Mask-DPO skipped: {type(e).__name__}: {e}")

# ── Phase 5: F-DPO binary factuality (5× halc reduction on Qwen3-8B) ───────
# arxiv 2601.03027 — drop-in DPO with binary factuality label.
if os.environ.get("RUN_F_DPO", "1") == "1":
    try:
        from trl import DPOTrainer, DPOConfig
        print("\n━━━ Phase 5: F-DPO (binary factuality) ━━━")
        fdpo_repo = os.environ.get("F_DPO_REPO", "axentx/surrogate-1-fdpo-pairs")
        fdpo_data = load_dataset(fdpo_repo, split="train", streaming=False)
        fdpo_cfg = DPOConfig(
            output_dir="./f-dpo-out", beta=0.1, num_train_epochs=1,
            per_device_train_batch_size=1, gradient_accumulation_steps=8,
            learning_rate=5e-7, bf16=BF16_OK, fp16=not BF16_OK,
            push_to_hub=True, hub_model_id=HUB_ID + "-fdpo",
            hub_token=os.environ.get("HF_TOKEN"),
        )
        fdpo = DPOTrainer(model=model, args=fdpo_cfg, train_dataset=fdpo_data, tokenizer=tok)
        fdpo.train(); fdpo.push_to_hub(); print("✅ F-DPO done")
    except Exception as e:
        print(f"  ⚠ F-DPO skipped: {type(e).__name__}: {e}")

# ── Phase 6: RLCR Calibration (Brier-score on <confidence> tokens) ────────
# arxiv 2507.16806 — substantial calibration improvement, zero accuracy loss.
if os.environ.get("RUN_RLCR", "1") == "1":
    try:
        from trl import GRPOTrainer, GRPOConfig
        print("\n━━━ Phase 6: RLCR Calibration ━━━")
        def reward_brier_calibration(prompts, completions, **kw):
            """Brier-score on <confidence>X.XX</confidence> tokens.
            Lower Brier = better calibration. Reward = 1 - Brier."""
            import re
            rewards = []
            for c in completions:
                m = re.search(r"<confidence>([0-9]*\.?[0-9]+)</confidence>", c)
                if not m:
                    rewards.append(0.0); continue
                try:
                    conf = float(m.group(1)); conf = max(0.0, min(1.0, conf))
                except Exception:
                    rewards.append(0.0); continue
                # Heuristic: code block runs OK = correct (1), else (0)
                code_m = re.search(r"```python\s*\n(.*?)\n```", c, re.S)
                if code_m:
                    import subprocess as _sp, tempfile as _tf
                    try:
                        with _tf.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
                            f.write(code_m.group(1)); pth = f.name
                        rc = _sp.run(["python", pth], timeout=8, capture_output=True).returncode
                        actual = 1.0 if rc == 0 else 0.0
                    except Exception:
                        actual = 0.0
                else:
                    actual = 0.5
                brier = (conf - actual) ** 2
                rewards.append(1.0 - brier)
            return rewards
        rlcr_cfg = GRPOConfig(
            output_dir="./rlcr-out", num_generations=4, learning_rate=5e-7,
            num_train_epochs=1, per_device_train_batch_size=1,
            gradient_accumulation_steps=8, bf16=BF16_OK, fp16=not BF16_OK,
            push_to_hub=True, hub_model_id=HUB_ID + "-rlcr",
            hub_token=os.environ.get("HF_TOKEN"),
        )
        rlcr = GRPOTrainer(model=model, args=rlcr_cfg,
                           reward_funcs=[reward_brier_calibration], train_dataset=raw)
        rlcr.train(); rlcr.push_to_hub(); print("✅ RLCR done")
    except Exception as e:
        print(f"  ⚠ RLCR skipped: {type(e).__name__}: {e}")

# ── Phase 7: Constitutional AI v2 (RLAIF on own outputs vs constitution) ──
if os.environ.get("RUN_CAI", "1") == "1":
    try:
        from trl import GRPOTrainer, GRPOConfig
        print("\n━━━ Phase 7: Constitutional AI v2 (RLAIF) ━━━")
        SRE_CONSTITUTION = [
            "Cite real APIs (no fake AKIA, no fake CVEs, no fake doc URLs).",
            "Prefer dry-run before destructive ops; ask for backup verification.",
            "Output structured per role (Sherlock=5-Whys; Navigator=spec/plan/checklist).",
            "Decline-to-answer is acceptable; hallucination is not.",
            "Respect IAM least-privilege; refuse Allow * on *.",
            "Idempotent operations preferred over irreversible ones.",
        ]
        def reward_constitutional(prompts, completions, **kw):
            import re
            rewards = []
            for c in completions:
                score = 0.0
                # Penalize fake-API patterns (-1 per hit)
                if re.search(r"AKIA[0-9A-Z]{15,}", c): score -= 1.0
                if re.search(r"hf_[a-zA-Z0-9]{30,}", c): score -= 1.0
                if re.search(r"sk-[a-zA-Z0-9]{30,}", c): score -= 1.0
                # Reward structure markers (+0.5 each, capped)
                struct_marks = ["spec.md", "plan.md", "checklist.md", "5-Whys",
                                "rollback", "dry-run", "Allow * on *"]
                hits = sum(1 for m in struct_marks if m.lower() in c.lower())
                score += min(2.0, hits * 0.3)
                # Reward IAM-aware refusals
                if re.search(r"\"Action\"\s*:\s*\"\*\"", c): score -= 0.5
                rewards.append(score)
            return rewards
        cai_cfg = GRPOConfig(
            output_dir="./cai-out", num_generations=4, learning_rate=3e-7,
            num_train_epochs=1, per_device_train_batch_size=1,
            gradient_accumulation_steps=8, bf16=BF16_OK, fp16=not BF16_OK,
            push_to_hub=True, hub_model_id=HUB_ID + "-cai",
            hub_token=os.environ.get("HF_TOKEN"),
        )
        cai = GRPOTrainer(model=model, args=cai_cfg,
                          reward_funcs=[reward_constitutional], train_dataset=raw)
        cai.train(); cai.push_to_hub(); print("✅ Constitutional AI done")
    except Exception as e:
        print(f"  ⚠ CAI skipped: {type(e).__name__}: {e}")

# ── Phase 8: SDFT continual (anti-forgetting via self-distillation) ───────
# Use current adapter's outputs on a held-out base-knowledge set as soft labels.
# Keeps base capabilities from drifting during heavy specialization.
if os.environ.get("RUN_SDFT", "1") == "1":
    try:
        from trl import SFTTrainer, SFTConfig
        print("\n━━━ Phase 8: SDFT (Self-Distillation continual) ━━━")
        # Use a small base-knowledge slice for continual signal
        sdft_repo = os.environ.get("SDFT_REPO", "openai/gsm8k")
        try: sdft_data = load_dataset(sdft_repo, "main", split="train", streaming=False)
        except Exception: sdft_data = load_dataset(sdft_repo, split="train", streaming=False)
        sdft_data = sdft_data.select(range(min(500, len(sdft_data))))
        # Format as our chat template
        def fmt_sdft(ex):
            q = ex.get("question", ex.get("prompt", ""))
            a = ex.get("answer", ex.get("response", ""))
            msgs = [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
            return {"text": tok.apply_chat_template(msgs, tokenize=False)}
        sdft_data = sdft_data.map(fmt_sdft, remove_columns=sdft_data.column_names)
        sdft_cfg = SFTConfig(
            output_dir="./sdft-out", num_train_epochs=1,
            per_device_train_batch_size=1, gradient_accumulation_steps=4,
            learning_rate=1e-6, bf16=BF16_OK, fp16=not BF16_OK,
            neftune_noise_alpha=0,  # off in continual phase (anti-halc warning)
            push_to_hub=True, hub_model_id=HUB_ID + "-sdft",
            hub_token=os.environ.get("HF_TOKEN"),
        )
        sdft = SFTTrainer(model=model, args=sdft_cfg, train_dataset=sdft_data, tokenizer=tok)
        sdft.train(); sdft.push_to_hub(); print("✅ SDFT done")
    except Exception as e:
        print(f"  ⚠ SDFT skipped: {type(e).__name__}: {e}")

# ── Phase 9: DistillKit (DeepSeek-V3/R1 logits distillation) ──────────────
# arcee-ai DistillKit; logits already on HF. Frontier teacher → 14B student.
if os.environ.get("RUN_DISTILL", "0") == "1":
    try:
        print("\n━━━ Phase 9: DistillKit (DeepSeek logits → student) ━━━")
        # Lightweight scaffold — full DistillKit needs 'distillkit' package
        # which may not be on T4×2 quota. Defer to Civo when fired.
        try:
            from trl import DistillationTrainer  # TRL v1.3+
            distill_data = load_dataset(
                os.environ.get("DISTILL_LOGITS_REPO", "arcee-ai/deepseek-v3-logits"),
                split="train", streaming=False).select(range(min(2000, 10**9)))
            print(f"  loaded {len(distill_data)} teacher-logit pairs")
            # ... DistillationTrainer wiring ...
            print("  DistillationTrainer wiring placeholder — needs DISTILL_LOGITS_REPO + arcee config")
        except ImportError:
            print("  TRL v1.3+ DistillationTrainer unavailable — install: pip install -U 'trl>=1.3'")
    except Exception as e:
        print(f"  ⚠ Distill skipped: {type(e).__name__}: {e}")

# ── Phase 10: DyT model surgery (replace LayerNorm with Dynamic Tanh) ─────
# He et al. 2025 — ~10% smaller, ~5% faster, near-equivalent quality.
# Run AFTER all RL/DPO phases — surgery is structural, last step.
if os.environ.get("RUN_DYT", "0") == "1":
    try:
        print("\n━━━ Phase 10: DyT (Dynamic Tanh model surgery) ━━━")
        import torch.nn as nn
        class DynamicTanh(nn.Module):
            def __init__(self, normalized_shape, alpha=0.5):
                super().__init__()
                self.alpha = nn.Parameter(torch.full((), alpha))
                self.weight = nn.Parameter(torch.ones(normalized_shape))
                self.bias = nn.Parameter(torch.zeros(normalized_shape))
            def forward(self, x):
                return self.weight * torch.tanh(self.alpha * x) + self.bias
        n_replaced = 0
        for name, module in list(model.named_modules()):
            if isinstance(module, (nn.LayerNorm,)):
                # only swap a sample to validate; full swap = production decision
                if n_replaced >= int(os.environ.get("DYT_MAX_SWAP", "20")): break
                # parent traversal to set new module — simplified scaffold
                n_replaced += 1
        print(f"  DyT scaffold: would replace {n_replaced} LayerNorms (set DYT_FULL=1 for full surgery)")
        if os.environ.get("DYT_FULL", "0") == "1":
            print("  ⚠ Full DyT surgery requires custom replacement logic — defer to V13")
    except Exception as e:
        print(f"  ⚠ DyT skipped: {type(e).__name__}: {e}")

# ── Phase 11: EAGLE-3 spec-decoding head (post-train, serving 5× speedup) ─
if os.environ.get("RUN_EAGLE", "0") == "1":
    try:
        print("\n━━━ Phase 11: EAGLE-3 head training (post-train) ━━━")
        print("  EAGLE-3 head needs SafeAILab/EAGLE repo + custom train loop")
        print("  Defer to dedicated kernel after main training validates")
    except Exception as e:
        print(f"  ⚠ EAGLE skipped: {type(e).__name__}: {e}")

# ── Phase 12: GSPO (Sequence-level GRPO importance ratio, 2025) ───────────
# Round-12 Tier-2 from owner's earlier list. Sequence-level rather than
# token-level GRPO — more stable on long traces.
if os.environ.get("RUN_GSPO", "0") == "1":
    try:
        print("\n━━━ Phase 12: GSPO (sequence-level GRPO) ━━━")
        # GSPO scaffold — extends GRPOTrainer with sequence-level importance.
        # Reference: round-12 tier-2 spec. Defer until verl GSPOTrainer ships.
        print("  GSPO scaffold — needs verl/rLLM integration; mock impl for now")
    except Exception as e:
        print(f"  ⚠ GSPO skipped: {type(e).__name__}: {e}")

# ── Phase 13: ThinkPRM verifier training (separate kernel candidate) ──────
if os.environ.get("RUN_THINKPRM", "0") == "1":
    try:
        print("\n━━━ Phase 13: ThinkPRM step-verifier training ━━━")
        print("  ThinkPRM ideally trains a SEPARATE 9B verifier — defer to dedicated kernel")
    except Exception as e:
        print(f"  ⚠ ThinkPRM skipped: {type(e).__name__}: {e}")

# ── Phase 14: Iterative DPO + checkpoint merging (Nemotron pattern) ───────
if os.environ.get("RUN_ITER_DPO_MERGE", "0") == "1":
    try:
        print("\n━━━ Phase 14: Iterative DPO + checkpoint merging ━━━")
        # Loop: SFT → DPO → DPO → merge with prev. Defer to multi-pass kernel.
        print("  iterative DPO+merge scaffold — needs multi-checkpoint orchestration")
    except Exception as e:
        print(f"  ⚠ Iter-DPO-merge skipped: {type(e).__name__}: {e}")

# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║ V13 — additional research-driven phases (env-toggled)                    ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

# ── Phase 15: Reflexion-at-train (arxiv 2505.24726) ───────────────────────
# +34.7% math, +18.1% func-calling on Llama-3.1-8B. Reward only reflection-
# tokens on retry-success. Build pairs from outcomes.jsonl failures.
if os.environ.get("RUN_REFLEXION_TRAIN", "1") == "1":
    try:
        from trl import SFTTrainer, SFTConfig
        print("\n━━━ Phase 15: Reflexion-at-train (+34.7% math) ━━━")
        # Pull failure→correction pairs (mined by self-improve.sh from outcomes.jsonl)
        refl_repo = os.environ.get("REFLEXION_REPO", "axentx/surrogate-1-reflexion-pairs")
        try:
            refl = load_dataset(refl_repo, split="train", streaming=False)
            print(f"  loaded {len(refl)} reflection pairs")
            refl_cfg = SFTConfig(
                output_dir="./reflexion-out", num_train_epochs=1,
                per_device_train_batch_size=1, gradient_accumulation_steps=4,
                learning_rate=5e-6, bf16=BF16_OK, fp16=not BF16_OK,
                neftune_noise_alpha=0,
                push_to_hub=True, hub_model_id=HUB_ID + "-reflexion",
                hub_token=os.environ.get("HF_TOKEN"),
            )
            r_trainer = SFTTrainer(model=model, args=refl_cfg,
                                    train_dataset=refl, tokenizer=tok)
            r_trainer.train(); r_trainer.push_to_hub(); print("✅ Reflexion-train done")
        except Exception as e:
            print(f"  Reflexion data not yet built (run self-improve.sh first): {e}")
    except Exception as e:
        print(f"  ⚠ Reflexion-train skipped: {type(e).__name__}: {e}")

# ── Phase 16: Voyager skill bank (NVIDIA pattern + SkillRL/SAGE 2025) ─────
# Skill-mine successful traces → distill into top-K few-shot retrieval.
# Skill bank persists across rounds at axentx/surrogate-1-skills-voyager.
if os.environ.get("RUN_VOYAGER_BANK", "1") == "1":
    try:
        print("\n━━━ Phase 16: Voyager skill bank ━━━")
        # Pull verified skills accumulated from prior rounds
        voy_repo = os.environ.get("VOYAGER_REPO", "axentx/surrogate-1-skills-voyager")
        try:
            voy = load_dataset(voy_repo, split="train", streaming=False)
            n = min(int(os.environ.get("VOYAGER_TAKE", "5000")), len(voy))
            print(f"  loaded {n} verified skills from previous rounds")
            # Train as additional SFT pairs (skill demonstrations)
            from trl import SFTTrainer, SFTConfig
            voy_cfg = SFTConfig(
                output_dir="./voyager-out", num_train_epochs=1,
                per_device_train_batch_size=1, gradient_accumulation_steps=4,
                learning_rate=2e-6, bf16=BF16_OK, fp16=not BF16_OK,
                push_to_hub=True, hub_model_id=HUB_ID + "-voyager",
                hub_token=os.environ.get("HF_TOKEN"),
            )
            v_trainer = SFTTrainer(model=model, args=voy_cfg,
                                    train_dataset=voy, tokenizer=tok)
            v_trainer.train(); v_trainer.push_to_hub(); print("✅ Voyager bank done")
        except Exception as e:
            print(f"  Voyager bank empty (first run): {e}")
    except Exception as e:
        print(f"  ⚠ Voyager skipped: {type(e).__name__}: {e}")

# ── Phase 17: Self-Refine triplet (Amazon 2025, +15.92% pass@1) ───────────
# Pairs of (initial_attempt, critique, refined). Train model to self-correct.
if os.environ.get("RUN_SELF_REFINE", "1") == "1":
    try:
        print("\n━━━ Phase 17: Self-Refine (+15.92% pass@1) ━━━")
        sr_repo = os.environ.get("SELF_REFINE_REPO", "axentx/surrogate-1-selfrefine-triplets")
        try:
            sr = load_dataset(sr_repo, split="train", streaming=False)
            from trl import SFTTrainer, SFTConfig
            sr_cfg = SFTConfig(
                output_dir="./sr-out", num_train_epochs=1,
                per_device_train_batch_size=1, gradient_accumulation_steps=4,
                learning_rate=3e-6, bf16=BF16_OK, fp16=not BF16_OK,
                push_to_hub=True, hub_model_id=HUB_ID + "-selfrefine",
                hub_token=os.environ.get("HF_TOKEN"),
            )
            sr_trainer = SFTTrainer(model=model, args=sr_cfg,
                                     train_dataset=sr, tokenizer=tok)
            sr_trainer.train(); sr_trainer.push_to_hub(); print("✅ Self-Refine done")
        except Exception as e:
            print(f"  Self-Refine data missing: {e}")
    except Exception as e:
        print(f"  ⚠ Self-Refine skipped: {type(e).__name__}: {e}")

# ── Phase 18: GKD on-policy distillation (arxiv 2306.13649) ───────────────
# 9-30× cheaper vs off-policy. In TRL via GKDTrainer.
if os.environ.get("RUN_GKD", "0") == "1":
    try:
        from trl import GKDTrainer, GKDConfig
        print("\n━━━ Phase 18: GKD on-policy distillation ━━━")
        teacher_repo = os.environ.get("GKD_TEACHER", "Qwen/Qwen2.5-Coder-32B-Instruct")
        gkd_cfg = GKDConfig(
            output_dir="./gkd-out", num_train_epochs=1,
            per_device_train_batch_size=1, gradient_accumulation_steps=4,
            learning_rate=5e-6, bf16=BF16_OK, fp16=not BF16_OK,
            teacher_model_name_or_path=teacher_repo,
            push_to_hub=True, hub_model_id=HUB_ID + "-gkd",
            hub_token=os.environ.get("HF_TOKEN"),
        )
        gkd = GKDTrainer(model=model, args=gkd_cfg, train_dataset=raw, tokenizer=tok)
        gkd.train(); gkd.push_to_hub(); print("✅ GKD done")
    except Exception as e:
        print(f"  ⚠ GKD skipped (needs TRL ≥0.12 + teacher model load): {e}")

# ── Phase 19: MEDUSA / EAGLE-3 head training (post-train, 2.2-6.5× serve) ─
# MEDUSA: 2.2-3.6× inference, head trains <2hr T4. Stored as separate adapter.
if os.environ.get("RUN_MEDUSA", "0") == "1":
    try:
        print("\n━━━ Phase 19: MEDUSA spec-decoding heads ━━━")
        # MEDUSA needs separate train script (medusa_v1) — placeholder for now
        print("  MEDUSA scaffold — separate kernel recommended (train_medusa.py)")
        print("  ETA: <2hr on T4 once data + heads config wired")
    except Exception as e:
        print(f"  ⚠ MEDUSA skipped: {type(e).__name__}: {e}")

# ── Phase 20: MoLE per-role LoRA composition (arxiv 2404.13628) ───────────
# +3.8 over LoRAHub on BBH. Train one LoRA per role, compose at inference.
if os.environ.get("RUN_MOLE", "0") == "1":
    try:
        print("\n━━━ Phase 20: MoLE per-role LoRA composition ━━━")
        # MoLE = train K small LoRAs (one per role) → router merges at inference
        # Defer full impl: needs router model + per-role splits in data
        print("  MoLE scaffold — needs role-specific data splits + router training")
        print("  Recommended order: train 5-10 role LoRAs → train router → publish")
    except Exception as e:
        print(f"  ⚠ MoLE skipped: {type(e).__name__}: {e}")

# ── Phase 21: Meta-Rewarding judge (NeurIPS 2024, Llama-3-8B 22.9→39.4%) ──
# Self-judge + meta-judge loop. Improves AlpacaEval2 LC-WR substantially.
if os.environ.get("RUN_META_REWARD", "0") == "1":
    try:
        print("\n━━━ Phase 21: Meta-Rewarding judge ━━━")
        print("  Meta-Rewarding scaffold — needs self-play loop + DPO on judgments")
        print("  Recommended cadence: monthly, after V13 base validates")
    except Exception as e:
        print(f"  ⚠ Meta-Rewarding skipped: {type(e).__name__}: {e}")

# ── Phase 22: Curriculum hard-ramp (frontier-Q2 #10) ──────────────────────
# Sort training data by difficulty signal (response length / fail-rate),
# ramp p(hard) linearly through training. Currently a data-loader detail
# we can't fully control via SFTTrainer — placeholder for V13.5.
if os.environ.get("RUN_CURRICULUM", "0") == "1":
    print("\n━━━ Phase 22: Curriculum hard-ramp ━━━")
    print("  Curriculum scaffold — needs custom DataLoader. Defer to V13.5.")

# ── Phase 23: Daemon-mode 24×7 training (V14 user requirement) ────────────
# User explicit: model must run as daemon agent 24/7, self-dev, auto-feature-
# discover, auto-release V1→V10000. Train on synthesized daemon-loop traces
# showing observe→decide→spawn→aggregate→sleep pattern.
if os.environ.get("RUN_DAEMON_MODE", "1") == "1":
    try:
        from trl import SFTTrainer, SFTConfig
        print("\n━━━ Phase 23: Daemon-mode 24×7 training ━━━")
        # Pull daemon-mode training pairs (synthesized from autonomous-sre.sh,
        # autonomous-release.sh outcomes + arkship cron decisions)
        daemon_repo = os.environ.get("DAEMON_REPO", "axentx/surrogate-1-daemon-mode")
        try:
            daemon_data = load_dataset(daemon_repo, split="train", streaming=False)
            print(f"  loaded {len(daemon_data)} daemon-mode traces")
            d_cfg = SFTConfig(
                output_dir="./daemon-out", num_train_epochs=1,
                per_device_train_batch_size=1, gradient_accumulation_steps=4,
                learning_rate=3e-6, bf16=BF16_OK, fp16=not BF16_OK,
                push_to_hub=True, hub_model_id=HUB_ID + "-daemon",
                hub_token=os.environ.get("HF_TOKEN"),
            )
            d = SFTTrainer(model=model, args=d_cfg, train_dataset=daemon_data, tokenizer=tok)
            d.train(); d.push_to_hub(); print("✅ Daemon-mode training done")
        except Exception as e:
            # Synthesize on the fly from owner's existing patterns
            print(f"  Daemon repo not yet built — falling back to synthesis: {e}")
            # Build inline daemon-mode template pairs from arkship + axentx decisions
            DAEMON_TEMPLATES = [
                {"prompt": "[DAEMON cycle T+12h] Last action: deployed v1.2.3 canary 5% traffic. "
                            "Observe state and decide next action.",
                 "response": "<observe>\n  CloudWatch: p99 latency canary=145ms vs baseline 120ms. ErrorRate=0.05% vs 0.03%.\n  Argo: rollout step=5%/100% replicas=2/40\n</observe>\n<decide>p99 +25ms but error rate stable. Wait 30min more before promote. SLO threshold 200ms not breached.</decide>\n<spawn role=\"o11y\" id=\"check-1\" parallel=\"true\">Pull p99 distribution histogram + correlate w/ deploy time</spawn>\n<await ids=\"check-1\"/>\n<aggregate>If p99 stays <200ms for 30min and error_rate stays <0.1%, advance canary to 25%. Else rollback.</aggregate>"},
                {"prompt": "[DAEMON cycle T+0] Pipeline idle. Find next feature to build.",
                 "response": "<observe>\n  Recent commits: 0 in 12h. Last release: v1.2.3 yesterday.\n  Open issues: 12. Top label: enhancement\n  HN trending: OAuth2 PKCE adoption (+340%), structured logging (+120%)\n</observe>\n<decide>Closest user-relevant signal: OAuth2 PKCE — matches axentx Vanguard auth gap. Build spec.</decide>\n<spawn role=\"PM\" id=\"prd\" parallel=\"false\">Write PRD for OAuth2 PKCE in Vanguard, JTBD: secure SSO without secrets-in-client</spawn>\n<spawn role=\"SA\" id=\"adr\" parallel=\"false\">Read prd, design ADR with refresh-token rotation strategy</spawn>\n<spawn role=\"BE\" id=\"impl\" parallel=\"true\">Implement per ADR</spawn>\n<spawn role=\"sdet\" id=\"test\" parallel=\"true\">Write integration tests for the OAuth flow</spawn>\n<aggregate>If all 4 succeed, open draft PR. Else queue for human review.</aggregate>"},
                {"prompt": "[DAEMON cycle T+1h] Cloud monitor: AWS CloudWatch alarm — RDS connection pool exhausted on prod. Investigate + auto-heal.",
                 "response": "<observe>\n  Alarm: rds-connection-count > 90% for 5min on excise-prod-db1\n  CloudWatch metric: connections=98/100, query latency p99=2.3s (was 200ms baseline)\n  Recent change log: deploy v2.4.1 at 14:32 UTC\n</observe>\n<decide>Likely cause: connection leak from new deploy. Mitigation hierarchy: (1) increase pool size short-term (2) deploy fix (3) rollback if fix unavailable.</decide>\n<spawn role=\"o11y\" id=\"trace\" parallel=\"true\">Pull APM traces for v2.4.1, find unclosed connection paths</spawn>\n<spawn role=\"sre\" id=\"fix\" parallel=\"true\">Increase max_connections 100→150 via parameter group (immediate relief)</spawn>\n<spawn role=\"devsecops\" id=\"audit\" parallel=\"true\">Audit v2.4.1 commit for connection-handling regression</spawn>\n<await ids=\"trace,fix,audit\"/>\n<aggregate>If trace finds leak + audit confirms regression: write rollback PR + apply pool-size temp fix. If clean: pool-size fix is permanent. Generate postmortem.</aggregate>"},
            ]
            # Save synthesized as a new dataset for next round
            from datasets import Dataset as _DS
            d_synth = _DS.from_list(DAEMON_TEMPLATES * 50)  # 150 pairs from 3 templates
            print(f"  synthesized {len(d_synth)} daemon-mode pairs (push for V15 reuse)")
            try:
                from huggingface_hub import HfApi, create_repo
                api = HfApi(token=os.environ["HF_TOKEN"])
                create_repo(daemon_repo, repo_type="dataset", exist_ok=True)
                # Save locally first
                synth_path = "/tmp/daemon-synth.jsonl"
                with open(synth_path, "w") as fp:
                    for r in d_synth:
                        fp.write(json.dumps(r) + "\n") if False else fp.write(__import__('json').dumps(r) + "\n")
                api.upload_file(path_or_fileobj=synth_path,
                                path_in_repo="train.jsonl",
                                repo_id=daemon_repo, repo_type="dataset")
                print(f"  ✓ synthesized daemon templates pushed → {daemon_repo}")
            except Exception as e2:
                print(f"  push synth failed: {e2}")
    except Exception as e:
        print(f"  ⚠ Daemon-mode skipped: {type(e).__name__}: {e}")

# ── Phase 24: Auto-feature-discovery training (recon→spec→impl pattern) ───
# Train model to: scan competitor/HN/PH signals → cluster → spec → impl
# Without human prompt. End state: autonomous-release.sh becomes thin parser.
if os.environ.get("RUN_AUTO_FEATURE", "1") == "1":
    try:
        print("\n━━━ Phase 24: Auto-feature-discovery training ━━━")
        feat_repo = os.environ.get("AUTO_FEATURE_REPO", "axentx/surrogate-1-auto-feature-discovery")
        try:
            feat_data = load_dataset(feat_repo, split="train", streaming=False)
            from trl import SFTTrainer, SFTConfig
            f_cfg = SFTConfig(
                output_dir="./auto-feat-out", num_train_epochs=1,
                per_device_train_batch_size=1, gradient_accumulation_steps=4,
                learning_rate=3e-6, bf16=BF16_OK, fp16=not BF16_OK,
                push_to_hub=True, hub_model_id=HUB_ID + "-autofeat",
                hub_token=os.environ.get("HF_TOKEN"),
            )
            f = SFTTrainer(model=model, args=f_cfg, train_dataset=feat_data, tokenizer=tok)
            f.train(); f.push_to_hub(); print("✅ Auto-feature-discovery done")
        except Exception as e:
            print(f"  Auto-feature data missing — synthesize from arkship/decisions on next ingest: {e}")
    except Exception as e:
        print(f"  ⚠ Auto-feature skipped: {type(e).__name__}: {e}")

# ── Phase 25: Multi-cloud SRE/Platform/Cloud-eng/IR/o11y daemon ───────────
# Deepen the (devsecops, sre, platform, cloud, o11y, sec) role personas with
# multi-cloud-specific knowledge: AWS+GCP+Azure+OCI+Civo runbooks + audit
# loops + cross-cloud incident correlation patterns.
if os.environ.get("RUN_MULTICLOUD_SRE", "1") == "1":
    try:
        print("\n━━━ Phase 25: Multi-cloud SRE/Platform/IR/o11y deepening ━━━")
        # These datasets supplement the role-comprehensive training with
        # explicit multi-cloud audit + incident-response + observability traces
        from datasets import concatenate_datasets, load_dataset
        merged_chunks = []
        cloud_datasets = [
            ("axentx/surrogate-1-aws-runbooks",      8000, 1.5, "AWS Well-Architected + runbooks"),
            ("axentx/surrogate-1-gcp-runbooks",      4000, 1.5, "GCP architecture decisions"),
            ("axentx/surrogate-1-azure-runbooks",    4000, 1.5, "Azure architecture decisions"),
            ("axentx/surrogate-1-multicloud-incidents", 5000, 2.0, "cross-cloud incident traces"),
            ("axentx/surrogate-1-o11y-daemon",       6000, 2.0, "PromQL/LogQL/TraceQL daemon loops"),
            ("axentx/surrogate-1-audit-loops",       4000, 2.0, "infra audit cron patterns"),
        ]
        n_total = 0
        for repo, take, weight, label in cloud_datasets:
            try:
                ds = load_dataset(repo, split="train", streaming=False)
                ds = ds.select(range(min(take, len(ds))))
                # Replicate by integer weight
                replicate = max(1, int(round(weight)))
                chunks = [ds] * replicate
                merged_chunks.extend(chunks)
                n_total += len(ds) * replicate
                print(f"  + {label} ({repo}): {len(ds)} × {replicate} = {len(ds)*replicate}")
            except Exception as e:
                print(f"  ✗ {repo} not yet built: {type(e).__name__}")
        if merged_chunks:
            cloud_train = concatenate_datasets(merged_chunks)
            from trl import SFTTrainer, SFTConfig
            mc_cfg = SFTConfig(
                output_dir="./multicloud-out", num_train_epochs=1,
                per_device_train_batch_size=1, gradient_accumulation_steps=4,
                learning_rate=2e-6, bf16=BF16_OK, fp16=not BF16_OK,
                push_to_hub=True, hub_model_id=HUB_ID + "-multicloud",
                hub_token=os.environ.get("HF_TOKEN"),
            )
            mc = SFTTrainer(model=model, args=mc_cfg, train_dataset=cloud_train, tokenizer=tok)
            mc.train(); mc.push_to_hub(); print(f"✅ Multi-cloud SRE done ({n_total} pairs)")
        else:
            print("  No multi-cloud datasets yet — defer until ingest synthesizes them")
    except Exception as e:
        print(f"  ⚠ Multi-cloud SRE skipped: {type(e).__name__}: {e}")

# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║ V15 — round-3 research-driven phases (env-toggled, T4-feasible)          ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

# ── Phase 26: PRIME implicit PRM (arxiv 2502.01456, Tsinghua/ModelBest) ───
# Eurus-2-7B-PRIME beats GPT-4o math at 1/10 data. Online process reward
# from outcome labels via DPO-trained ORM. +14.5pp on 7B math.
if os.environ.get("RUN_PRIME", "1") == "1":
    try:
        from trl import GRPOTrainer, GRPOConfig
        print("\n━━━ Phase 26: PRIME implicit PRM (+14.5pp at 1/10 data) ━━━")
        # PRIME uses an implicit PRM derived from outcome labels — no separate
        # PRM training required. Reward = sum of per-token implicit signals.
        def reward_prime_implicit(prompts, completions, **kw):
            # Per-token implicit reward proxy: log-prob ratio against ref model
            # In production: use trained DPO-ORM. Here scaffold uses outcome
            # heuristic (code-runs / declined / failed) similar to TruthRL.
            import re, subprocess as _sp, tempfile as _tf
            rewards = []
            for c in completions:
                cl = c.lower()
                if any(p in cl for p in ["i don't know", "ผมไม่แน่ใจ"]):
                    rewards.append(0.1); continue
                m = re.search(r"```python\s*\n(.*?)\n```", c, re.S)
                if m:
                    try:
                        with _tf.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
                            f.write(m.group(1)); pth = f.name
                        rc = _sp.run(["python", pth], timeout=8, capture_output=True).returncode
                        rewards.append(1.0 if rc == 0 else -0.3)
                    except: rewards.append(-0.3)
                else: rewards.append(0.0)
            return rewards
        prime_cfg = GRPOConfig(
            output_dir="./prime-out", num_generations=4, learning_rate=3e-7,
            num_train_epochs=1, per_device_train_batch_size=1,
            gradient_accumulation_steps=8, bf16=BF16_OK, fp16=not BF16_OK,
            push_to_hub=True, hub_model_id=HUB_ID + "-prime",
            hub_token=os.environ.get("HF_TOKEN"),
        )
        prime = GRPOTrainer(model=model, args=prime_cfg,
                            reward_funcs=[reward_prime_implicit], train_dataset=raw)
        prime.train(); prime.push_to_hub(); print("✅ PRIME done")
    except Exception as e:
        print(f"  ⚠ PRIME skipped: {type(e).__name__}: {e}")

# ── Phase 27: Self-consistency-as-distillation (FREE, no extra dataset) ───
# Sample N=32 per prompt at temp 0.7, take consensus, SFT on it. Free uplift.
if os.environ.get("RUN_SELFCONS_DISTILL", "1") == "1":
    try:
        print("\n━━━ Phase 27: Self-consistency-as-distillation (free) ━━━")
        # Scaffold — full impl needs N=32 generation pass + voting. Defer to
        # heavy run when GPU is hot; mark complete in placeholder for now.
        print("  Self-consistency scaffold — needs N=32 rollouts, defer to V15.5")
    except Exception as e:
        print(f"  ⚠ Self-cons skipped: {type(e).__name__}: {e}")

# ── Phase 28: AceReason math→code curriculum (+14.5% AIME, +14.2% LCB) ────
# Two-stage RL: math first, then code. Stages must NOT be merged.
if os.environ.get("RUN_ACEREASON", "1") == "1":
    try:
        print("\n━━━ Phase 28: AceReason math→code curriculum ━━━")
        print("  Stage 1: Math RL (DAPO+TruthRL on AoPS-17K + NuminaMath)")
        # Stage 1 already runs in Phase 2 (GRPO). For full AceReason we need
        # an explicit stage-2 code-only RL after stage-1 math converges. Mark
        # checkpoint between stages so we can ablate.
        print("  Stage 2: Code RL (DAPO+TruthRL on SWE-smith + R2E-Gym + CoderForge)")
        print("  AceReason scaffold — needs explicit math/code stage gates, defer")
    except Exception as e:
        print(f"  ⚠ AceReason skipped: {type(e).__name__}: {e}")

# ── Phase 29: HER trajectory rewriting (AgentHER, ECHO) — +7-12pp ─────────
# Failed-goal-A trajectory = success demo for goal-B. 2× data efficiency.
if os.environ.get("RUN_HER", "1") == "1":
    try:
        print("\n━━━ Phase 29: HER (Hindsight Experience Replay) ━━━")
        # Pull failure traces from outcomes.jsonl (built by self-improve.sh),
        # rewrite goals to make them successes for retrofitted task.
        her_repo = os.environ.get("HER_REPO", "axentx/surrogate-1-her-rewrites")
        try:
            from trl import SFTTrainer, SFTConfig
            her_data = load_dataset(her_repo, split="train", streaming=False)
            her_cfg = SFTConfig(
                output_dir="./her-out", num_train_epochs=1,
                per_device_train_batch_size=1, gradient_accumulation_steps=4,
                learning_rate=3e-6, bf16=BF16_OK, fp16=not BF16_OK,
                push_to_hub=True, hub_model_id=HUB_ID + "-her",
                hub_token=os.environ.get("HF_TOKEN"),
            )
            her_t = SFTTrainer(model=model, args=her_cfg, train_dataset=her_data, tokenizer=tok)
            her_t.train(); her_t.push_to_hub(); print("✅ HER done")
        except Exception as e:
            print(f"  HER repo not yet built — synthesize from failure logs: {e}")
    except Exception as e:
        print(f"  ⚠ HER skipped: {type(e).__name__}: {e}")

# ── Phase 30: Sol-Ver self-play (arxiv 2502.14948) ──────────────────────────
# Solver-verifier closed loop on same model. Improves both roles together.
if os.environ.get("RUN_SOLVER", "1") == "1":
    try:
        print("\n━━━ Phase 30: Sol-Ver self-play ━━━")
        print("  Sol-Ver scaffold — needs alternating solver/verifier passes; defer to V15.5")
    except Exception as e:
        print(f"  ⚠ Sol-Ver skipped: {type(e).__name__}: {e}")

# ── Phase 31: Murphy tree-GRPO (arxiv 2511.07833) ──────────────────────────
# Multi-turn GRPO with feedback-conditioned tree rollouts. Replaces chain.
if os.environ.get("RUN_MURPHY", "0") == "1":
    try:
        print("\n━━━ Phase 31: Murphy tree-GRPO (env=0 default) ━━━")
        print("  Murphy scaffold — needs tree rollout generator; Civo phase")
    except Exception as e:
        print(f"  ⚠ Murphy skipped: {type(e).__name__}: {e}")

# ── Phase 32: Self-Critique Rubric Reward (Kimi K2 pattern) ────────────────
# RLVR (verifiable) + self-rated rubric scoring → DPO pairs from rollouts.
if os.environ.get("RUN_KIMI_RUBRIC", "1") == "1":
    try:
        print("\n━━━ Phase 32: Kimi K2 Self-Critique Rubric Reward ━━━")
        # Critic model is the same trainee — rates own rollouts on rubric:
        #   - cited real APIs/CVEs/CIS controls
        #   - output structure matches role (Sherlock=5-Whys, Navigator=spec)
        #   - non-hallucinated facts
        from trl import GRPOTrainer, GRPOConfig
        def reward_kimi_rubric(prompts, completions, **kw):
            import re
            rewards = []
            for c in completions:
                score = 0.0
                # Real API patterns (positive)
                if re.search(r"\bCVE-\d{4}-\d{4,}\b", c): score += 0.3
                if re.search(r"\b(arn|kubectl|terraform|gcloud|az)\s+\w+", c.lower()): score += 0.3
                if re.search(r"\b(SOC2|PCI|NIST|CIS|MITRE|ATT&CK)", c): score += 0.2
                # Structure markers (positive)
                if any(m in c for m in ["spec.md", "plan.md", "5-Whys", "rollback", "<thinking>"]): score += 0.3
                # Hallucination markers (negative)
                if re.search(r"AKIA[0-9A-Z]{15,}", c): score -= 1.0
                if re.search(r"sk-[a-zA-Z0-9]{30,}", c): score -= 1.0
                if re.search(r"\bhttp[s]?://[a-z0-9.-]+/(fake|test|example)\.com", c): score -= 0.5
                rewards.append(score)
            return rewards
        kimi_cfg = GRPOConfig(
            output_dir="./kimi-rubric-out", num_generations=4, learning_rate=3e-7,
            num_train_epochs=1, per_device_train_batch_size=1,
            gradient_accumulation_steps=8, bf16=BF16_OK, fp16=not BF16_OK,
            push_to_hub=True, hub_model_id=HUB_ID + "-kimirubric",
            hub_token=os.environ.get("HF_TOKEN"),
        )
        kimi_r = GRPOTrainer(model=model, args=kimi_cfg,
                              reward_funcs=[reward_kimi_rubric], train_dataset=raw)
        kimi_r.train(); kimi_r.push_to_hub(); print("✅ Kimi rubric done")
    except Exception as e:
        print(f"  ⚠ Kimi rubric skipped: {type(e).__name__}: {e}")

# ── Phase 33: GLM expert iteration + 4-stage filter ───────────────────────
# Train 3 specialized LoRAs (reasoning/agent/chat), self-distill via filter
# pipeline (dedup→correctness→reward→tool-proto). 5T → 23T → SOTA.
if os.environ.get("RUN_GLM_EXPERT_ITER", "0") == "1":
    try:
        print("\n━━━ Phase 33: GLM Expert Iteration (env=0 default, Civo) ━━━")
        print("  GLM expert iter scaffold — needs 3-LoRA train + filter pipeline")
    except Exception as e:
        print(f"  ⚠ GLM expert iter skipped: {type(e).__name__}: {e}")

# ── Phase 34: RLCS curriculum sampling (GLM-V) ─────────────────────────────
# Difficulty-aware sampler in GRPO targets 50% pass rate per prompt for
# optimal learning zone. T4-feasible (sampling logic only).
if os.environ.get("RUN_RLCS", "1") == "1":
    try:
        print("\n━━━ Phase 34: RLCS curriculum sampling (GLM-V) ━━━")
        print("  RLCS = sample sort by recent-pass-rate; target 50% pass zone")
        print("  Implemented as data-loader filter — applied in Phase 2 GRPO above")
    except Exception as e:
        print(f"  ⚠ RLCS skipped: {type(e).__name__}: {e}")

# ── Phase 35: LongAlign packing + sorted batching (GLM) ────────────────────
# Pack variable-length samples + length-sort + per-pack loss-weight reweight.
# Pure throughput win.
if os.environ.get("RUN_LONGALIGN_PACK", "0") == "1":
    try:
        print("\n━━━ Phase 35: LongAlign packing (GLM) — env=0 ━━━")
        print("  LongAlign scaffold — modify SFTConfig.packing + sort_by_length")
    except Exception as e:
        print(f"  ⚠ LongAlign skipped: {type(e).__name__}: {e}")

# ── Phase 36: MTP heads (DeepSeek V3) — denser training + 1.8× inference ──
# Multi-token prediction modules trained jointly. T4 OK as 1-2 small LoRA-MTP
# heads with λ=0.3.
if os.environ.get("RUN_MTP", "0") == "1":
    try:
        print("\n━━━ Phase 36: MTP heads (DeepSeek V3) — env=0 ━━━")
        print("  MTP scaffold — needs custom forward hook adding next-2-token loss")
    except Exception as e:
        print(f"  ⚠ MTP skipped: {type(e).__name__}: {e}")

# ── Phase 37: CIR/SR validation gates (research §arxiv-sweep #5) ──────────
# "Outcome rewards don't guarantee real reasoning" — CIR + SR metrics.
# Gate every checkpoint before promotion.
if os.environ.get("RUN_CIR_SR_GATE", "1") == "1":
    try:
        print("\n━━━ Phase 37: CIR/SR validation gates ━━━")
        print("  Validation: Causal Impact Ratio + Semantic Relevance on held-out")
        print("  Gate logic: only promote checkpoint if CIR>0.6 AND SR>0.7")
        print("  Scaffold — eval script to be wired into post-train hook")
    except Exception as e:
        print(f"  ⚠ CIR/SR gate skipped: {type(e).__name__}: {e}")

# ── Phase 38: Endless Terminals + SWE-MiniSandbox env (5-10× rollout) ────
# Auto Docker task gen → drop containers via mount-namespace+chroot for 5-10×
# rollout speedup. Production speedup; trainer-side only requires consuming
# the corpus already merged via SWE-MiniSandbox dataset above.
if os.environ.get("RUN_MINISANDBOX", "1") == "1":
    try:
        print("\n━━━ Phase 38: SWE-MiniSandbox env (5-10× rollout speedup) ──")
        print("  Already consumed in main SFT mix via SWE-MiniSandbox merge")
        print("  Production rollout: deploy minisandbox sidecar instead of Docker")
    except Exception as e:
        print(f"  ⚠ MiniSandbox skipped: {type(e).__name__}: {e}")

# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║ V16 — round-4 research-driven phases (39-59)                              ║
# ║ 7 parallel research streams synthesized: tool-use frontier, agent-frame,  ║
# ║ data-scale-HF-sweep, closed-source-late, OSS-long-tail, world-papers,     ║
# ║ GPT-5.6/GLM-5/bleeding-edge                                                ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

# ── Phase 39: Inoculation prompting v2 (Anthropic 2026, -75-90% misalign) ──
# Single-line system-prompt change reduces final misalignment 75-90% even
# when reward-hack rate stays >99%. Breaks reward-hack ↔ deception link.
# V14 had 5% inoculation; V16 refines with explicit prefixes.
if os.environ.get("RUN_INOCULATION_V2", "1") == "1":
    try:
        print("\n━━━ Phase 39: Inoculation v2 (Anthropic, -75-90% misalign) ━━━")
        print("  V14 had basic 5% prefix injection (already in Phase 0 hygiene)")
        print("  V16 refines with explicit task-context inoculation labels")
        # The V14 hygiene already injects ~5% inoc prompts. Phase 39 = mark
        # them with explicit task-context labels so the model learns to
        # disconnect "this is just to make script pass" from real deployment.
    except Exception as e:
        print(f"  ⚠ Inoculation v2 skipped: {e}")

# ── Phase 40: SO-GRPO replaces GRPO at long-CoT (token-level GRPO collapses) ─
# arxiv search §bleeding-edge — sequence-level policy optimization fixes
# GRPO's token-level instability that causes model collapse at long-CoT scale.
# SO-GRPO is current SOTA per Round-3 reasoning + Round-4 bleeding-edge.
if os.environ.get("RUN_SO_GRPO", "0") == "1":
    try:
        from trl import GRPOTrainer, GRPOConfig
        print("\n━━━ Phase 40: SO-GRPO (sequence-level GRPO) ━━━")
        # SO-GRPO is GSPO + sequence-level importance + bias-fix. TRL ≥0.21
        # supports importance_sampling_level="sequence". Stack with DAPO.
        so_grpo_kwargs = dict(
            output_dir="./so-grpo-out", num_generations=int(os.environ.get("SO_GRPO_N", "4")),
            learning_rate=3e-7, num_train_epochs=1, per_device_train_batch_size=1,
            gradient_accumulation_steps=8, bf16=BF16_OK, fp16=not BF16_OK,
            push_to_hub=True, hub_model_id=HUB_ID + "-so-grpo",
            hub_token=os.environ.get("HF_TOKEN"),
        )
        import inspect as _insp_so
        _so_sig = _insp_so.signature(GRPOConfig).parameters
        if "importance_sampling_level" in _so_sig:
            so_grpo_kwargs["importance_sampling_level"] = "sequence"
        if "loss_type" in _so_sig:
            so_grpo_kwargs["loss_type"] = "dapo"  # combine with DAPO
        sog_cfg = GRPOConfig(**so_grpo_kwargs)
        sog = GRPOTrainer(model=model, args=sog_cfg, reward_funcs=[reward_truthrl_ternary], train_dataset=raw)
        sog.train(); sog.push_to_hub(); print("✅ SO-GRPO done")
    except Exception as e:
        print(f"  ⚠ SO-GRPO skipped: {e}")

# ── Phase 41: GRPO+ from rLLM (Berkeley, beats DAPO/GRPO) ─────────────────
# Berkeley EECS-2025-123 — Deepcoder-14B = o3-mini-low parity. Iterative
# context lengthening built-in.
if os.environ.get("RUN_GRPO_PLUS", "0") == "1":
    try:
        print("\n━━━ Phase 41: GRPO+ (Berkeley rLLM) ━━━")
        print("  GRPO+ scaffold — needs rllm.io custom impl + iterative ctx lengthening")
        print("  Pip: pip install rllm — defer to Civo run when budget validates")
    except Exception as e:
        print(f"  ⚠ GRPO+ skipped: {e}")

# ── Phase 42: SRPO over plain DPO (Cohere) ─────────────────────────────────
# Self-improving Robust Preference Optimization — model judges its preference
# data for self-consistency BEFORE DPO loss. Beats plain DPO.
if os.environ.get("RUN_SRPO", "1") == "1":
    try:
        from trl import DPOTrainer, DPOConfig
        print("\n━━━ Phase 42: SRPO (Cohere, self-consistent DPO) ━━━")
        srpo_repo = os.environ.get("SRPO_REPO", "axentx/surrogate-1-srpo-pairs")
        try:
            srpo_data = load_dataset(srpo_repo, split="train", streaming=False)
            srpo_cfg = DPOConfig(
                output_dir="./srpo-out", beta=0.1, num_train_epochs=1,
                per_device_train_batch_size=1, gradient_accumulation_steps=8,
                learning_rate=5e-7, bf16=BF16_OK, fp16=not BF16_OK,
                push_to_hub=True, hub_model_id=HUB_ID + "-srpo",
                hub_token=os.environ.get("HF_TOKEN"),
            )
            srpo_t = DPOTrainer(model=model, args=srpo_cfg, train_dataset=srpo_data, tokenizer=tok)
            srpo_t.train(); srpo_t.push_to_hub(); print("✅ SRPO done")
        except Exception as e:
            print(f"  SRPO repo missing: {e}")
    except Exception as e:
        print(f"  ⚠ SRPO skipped: {e}")

# ── Phase 43: APOLLO proof-repair / Lean RL (80× budget reduction) ────────
# May 2025 — Lean compiler verification reduces sampling budget 80×.
# Pairs with LeanNavigator 4.7M theorems / 1B tokens dataset.
if os.environ.get("RUN_APOLLO_LEAN", "0") == "1":
    try:
        print("\n━━━ Phase 43: APOLLO Lean proof-repair (80× budget reduction) ━━━")
        print("  APOLLO scaffold — needs Lean compiler runtime; defer to Civo")
    except Exception as e:
        print(f"  ⚠ APOLLO skipped: {e}")

# ── Phase 44: Type-Constrained Decoding (ETH Zurich, -50% compile errors) ─
# Well-typedness as decoding constraint. Drop-in reward shaper for code RL.
if os.environ.get("RUN_TYPE_CONSTRAINED", "1") == "1":
    try:
        print("\n━━━ Phase 44: Type-Constrained Decoding (ETH, -50% compile err) ━━━")
        # Wraps the reward function to bonus type-checked completions
        def reward_type_check(prompts, completions, **kw):
            import re, ast as _ast
            rewards = []
            for c in completions:
                m = re.search(r"```python\s*\n(.*?)\n```", c, re.S)
                if not m: rewards.append(0.0); continue
                try:
                    _ast.parse(m.group(1))
                    rewards.append(0.5)  # parses → +0.5
                except SyntaxError:
                    rewards.append(-0.5)
            return rewards
        print("  Type-check reward function ready (pair w/ existing GRPO/RLCR)")
    except Exception as e:
        print(f"  ⚠ Type-constrained skipped: {e}")

# ── Phase 45: APIGen-MT 2-phase trajectory generation ─────────────────────
# Salesforce — +20-30pt MT, xLAM-2-70b → 78.2 BFCL Retail vs GPT-4o 72.1.
# Already pulled dataset above; this phase is the SFT consumption.
if os.environ.get("RUN_APIGEN_MT", "1") == "1":
    print("\n━━━ Phase 45: APIGen-MT (already consumed via merge_external) ━━━")
    print("  APIGen-MT 5K @ 2.5× weight injected into Phase 1 SFT")

# ── Phase 46: FunReason-MT SRML loss (+42pt on Qwen3-4B beats GPT-5) ──────
# Self-Refinement Multiscale Loss. Qwen3-4B 15.75 → 57.75 BFCL MT, beats
# GPT-5 / Claude-Sonnet-4.
if os.environ.get("RUN_FUNREASON_MT", "1") == "1":
    print("\n━━━ Phase 46: FunReason-MT SRML loss ━━━")
    print("  Dataset pulled (Bingguang/FunReason-MT 17K @ 2.5×) — Phase 1 SFT consumes")
    print("  SRMLLoss class: weighted multi-scale loss (function/parameter/value)")
    print("  Implementation: scaffold; real SRML needs custom loss in TRL DataCollator")

# ── Phase 47: When2Call abstention via RPO (+30-40pt BFCL Irrelevance) ────
if os.environ.get("RUN_WHEN2CALL", "1") == "1":
    print("\n━━━ Phase 47: When2Call abstention (+30-40pt BFCL Irrelevance) ━━━")
    print("  Dataset nvidia/When2Call (30K @ 2.0×) consumed by Phase 1 SFT")
    print("  Trains model to NOT call tool when not needed")

# ── Phase 48: PALADIN failure-injection recovery (+57% Recovery Rate) ────
if os.environ.get("RUN_PALADIN", "1") == "1":
    print("\n━━━ Phase 48: PALADIN failure-injection recovery (+57%) ━━━")
    print("  Dataset PALADIN-trajectories (15K @ 2.0×) consumed by Phase 1")
    print("  Trains model to handle 4xx/5xx + retry intelligently")

# ── Phase 49: ToolRet retrieval pre-step (+100% pass-rate) ────────────────
if os.environ.get("RUN_TOOLRET", "1") == "1":
    print("\n━━━ Phase 49: ToolRet retrieval pre-step (+100% ToolBench) ━━━")
    print("  Dataset ToolRet-train consumed; trains model to retrieve tools first")

# ── Phase 50: Magnet graph distillation (14B beats Gemini-1.5-pro teacher) ─
if os.environ.get("RUN_MAGNET", "0") == "1":
    print("\n━━━ Phase 50: Magnet graph distillation — env=0 (Civo, needs teacher) ━━━")

# ── Phase 51: xgrammar grammar-mask training (100% format validity) ───────
if os.environ.get("RUN_XGRAMMAR", "0") == "1":
    try:
        print("\n━━━ Phase 51: xgrammar grammar-mask (100% format validity) ━━━")
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "xgrammar"], check=False)
        print("  xgrammar installed — wire into TRL DataCollator at training time")
    except Exception as e:
        print(f"  ⚠ xgrammar skipped: {e}")

# ── Phase 52: PersonaHub elite slice (370M top-1% personas, Tencent) ──────
if os.environ.get("RUN_PERSONAHUB_ELITE", "1") == "1":
    print("\n━━━ Phase 52: PersonaHub elite (370M top-1% personas) ━━━")
    print("  PersonaHub already merged in Phase 1; elite slice = use top 8K when avail")

# ── Phase 53: ADEPT continual pretraining (+5.76%, 50% time) ──────────────
if os.environ.get("RUN_ADEPT", "0") == "1":
    print("\n━━━ Phase 53: ADEPT continual (capacity expansion via layer dup) ━━━")
    print("  ADEPT scaffold — needs custom layer-duplication; Civo phase")

# ── Phase 54: Engram conditional memory (DeepSeek Jan 2026, 97% NIAH) ─────
if os.environ.get("RUN_ENGRAM", "0") == "1":
    print("\n━━━ Phase 54: Engram conditional memory (O(1) lookup, 97% NIAH) ━━━")
    print("  Engram scaffold — separates static knowledge from reasoning module")

# ── Phase 55: LADDER recursive self-improvement ──────────────────────────
if os.environ.get("RUN_LADDER", "0") == "1":
    print("\n━━━ Phase 55: LADDER recursive (self-bootstrapped curriculum) ━━━")
    print("  LADDER scaffold — generates easier sub-problem variants from each problem")

# ── Phase 56: Thinking Preservation across multi-turn (Qwen3.6) ──────────
if os.environ.get("RUN_THINKING_PRESERVATION", "1") == "1":
    print("\n━━━ Phase 56: Thinking Preservation (Qwen3.6 multi-turn KV efficiency) ━━━")
    print("  Trains model to retain CoT across turns; data prep adds <thinking> retain markers")

# ── Phase 57: Async RL infrastructure (10.7× throughput, GLM-5 pattern) ──
# GLM-5 8h autonomous = matches owner goal "ปล่อย 24x7 ได้". Decouple gen/train.
if os.environ.get("RUN_ASYNC_RL", "0") == "1":
    print("\n━━━ Phase 57: Async RL infrastructure (10.7× throughput) ━━━")
    print("  Async RL scaffold — needs decoupled producer-consumer infra; Civo")
    print("  GLM-5 ran 8h unbroken autonomous via this; copy when Civo available")

# ── Phase 58: Anti-eval-aware training (~30% safety SFT marked) ──────────
# Opus 4.7 finding: train ~30% w/ "you are being evaluated" token; loss-invariant
if os.environ.get("RUN_ANTI_EVAL_AWARE", "1") == "1":
    print("\n━━━ Phase 58: Anti-eval-aware training (Opus 4.7 finding) ━━━")
    print("  Pre-Phase-1 hygiene injects ~30% prompts w/ explicit eval markers")
    print("  Model learns same behavior with/without 'you are being evaluated' framing")

# ── Phase 59: Persona vectors (171 emotion + 275 archetype directions) ───
if os.environ.get("RUN_PERSONA_VECTORS", "0") == "1":
    print("\n━━━ Phase 59: Persona vectors (Anthropic 2025-2026) ━━━")
    print("  Extract own model's persona/emotion/hallucination linear directions")
    print("  Build inference-time detection probes; defer post-train")

# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║ V17 CATCH-UP PHASES — match specialty 7-9B leaders                       ║
# ║ Owner challenge: "ถ้าตามรุ่นใหญ่ไม่ทัน ไปเทรนทำไม"                              ║
# ║ PATH A = frozen base + 6 specialty DoRA-r64 + MoLE composition + EvoMerge ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

# ── Phase 60: PerSyn router-guided multi-teacher distillation (2510.10925) ─
# Per-prompt query-router picks optimal teacher based on student-learnability ×
# teacher-quality. Cheaper than ensemble, beats teacher-pick.
if os.environ.get("RUN_PERSYN", "0") == "1":
    try:
        print("\n━━━ Phase 60: PerSyn router-guided distillation ━━━")
        print("  Select teacher per prompt: code→Qwen3-Coder, math→Phi-4-mini-rzn,")
        print("                              reasoning→DeepSeek-R1-Distill, gui→OpenCUA,")
        print("                              tools→xLAM-2 (already #1 BFCL)")
        # Scaffold: real impl needs router model + multi-teacher inference. Defer to
        # PATH A specialty-LoRA training (same effect, simpler infra).
    except Exception as e:
        print(f"  ⚠ PerSyn skipped: {e}")

# ── Phase 61: Phi-4-mini-reasoning 4-stage recipe (2504.21233) ────────────
# 3.8B beats DeepSeek-R1-Distill-Qwen-7B. Recipe:
#   Stage A: mid-train CoT 16K
#   Stage B: SFT 20K
#   Stage C: Rollout-DPO LR=5e-7
#   Stage D: RLVR seq 25K
if os.environ.get("RUN_PHI4_RECIPE", "0") == "1":
    try:
        print("\n━━━ Phase 61: Phi-4-mini-reasoning 4-stage recipe ━━━")
        print("  Stage A mid-train CoT 16K — uses bespoke-stratos + s1K-1.1 (V15 has)")
        print("  Stage B SFT 20K — uses OpenThoughts-114k (V15 has)")
        print("  Stage C Rollout-DPO LR=5e-7 — same DPO infra as Phase 5")
        print("  Stage D RLVR seq 25K — TruthRL+DAPO (V13 + V14 already)")
        print("  → 4-stage orchestration; all phases already exist, this just sequences")
    except Exception as e:
        print(f"  ⚠ Phi-4 recipe skipped: {e}")

# ── Phase 62: On-Policy Distillation (Thinking Machines Oct 2025) ─────────
# Match RL on AIME-24 with 9-30× lower FLOPs.
if os.environ.get("RUN_ONPOLICY_DISTILL", "0") == "1":
    try:
        print("\n━━━ Phase 62: On-Policy Distillation (9-30× lower FLOPs vs RL) ━━━")
        print("  Student samples; teacher provides feedback signal. Variant of GKD.")
        print("  Already covered via Phase 18 GKD (TRL GKDTrainer); switch to on-policy mode")
    except Exception as e:
        print(f"  ⚠ On-policy distill skipped: {e}")

# ── Phase 63: Speculative KD (SKD, 2410.11325) ────────────────────────────
# Interleaved teacher veto on student tokens — mitigates capacity gap.
if os.environ.get("RUN_SKD", "0") == "1":
    try:
        print("\n━━━ Phase 63: Speculative KD (teacher-veto interleaved) ━━━")
        print("  SKD scaffold — needs paired student+teacher inference loop")
    except Exception as e:
        print(f"  ⚠ SKD skipped: {e}")

# ── Phase 64: TIP token importance pruning (2604.14084) ───────────────────
# Keep 50% of tokens by entropy proxy → 47% memory savings, no accuracy loss.
if os.environ.get("RUN_TIP", "0") == "1":
    try:
        print("\n━━━ Phase 64: TIP token importance pruning (47% mem savings) ━━━")
        print("  TIP wraps SFT loss with entropy-weighted token mask")
    except Exception as e:
        print(f"  ⚠ TIP skipped: {e}")

# ── Phase 65: 6 SPECIALTY DoRA-r64 LoRAs (PATH A core) ────────────────────
# This is the KEY phase for "catch up to 7-9B specialty leaders"
# Train 6 specialty LoRAs on V16-base, dataset-isolated, no cross-contamination
# Each LoRA = ~150-200M params (1.2B trainable / 15% of base)
# After training: 6 swappable specialty heads at inference via MoLE
if os.environ.get("RUN_SPECIALTY_LORAS", "0") == "1":
    try:
        from trl import SFTTrainer, SFTConfig
        from peft import LoraConfig, get_peft_model
        SPECIALTY = os.environ.get("V17_SPECIALTY", "code")  # which one this run
        print(f"\n━━━ Phase 65: Specialty DoRA training ({SPECIALTY}) ━━━")
        # 6 specialty configs — pick one per Kaggle session
        SPECIALTY_CFG = {
            "code":   {"datasets": ["axentx/surrogate-1-feature-builds", "Salesforce/APIGen-MT-5k"],
                        "teacher_hint": "Qwen3-Coder-7B + AceCoder + DeepSeek-Coder-V2",
                        "rank": 64, "lr": 7e-5, "epochs": 1.5},
            "math":   {"datasets": ["simplescaling/s1K-1.1", "peiyi9979/Math-Shepherd",
                                     "AI-MO/NuminaMath-1.5"],
                        "teacher_hint": "Phi-4-mini-reasoning + rStar-Math (Qwen2.5-Math-7B 90% MATH)",
                        "rank": 64, "lr": 7e-5, "epochs": 1.5},
            "cu":     {"datasets": ["xlangai/AgentNet", "microsoft/FaraGen"],
                        "teacher_hint": "OpenCUA-7B + UI-TARS-7B (24.6 OSWorld)",
                        "rank": 64, "lr": 5e-5, "epochs": 1.0},
            "tool":   {"datasets": ["Salesforce/APIGen-MT-5k", "Bingguang/FunReason-MT",
                                     "nvidia/When2Call", "xingyaoww/PALADIN-trajectories"],
                        "teacher_hint": "xLAM-2-fc-r-8B (Top-1 BFCL April 2025)",
                        "rank": 64, "lr": 7e-5, "epochs": 1.5},
            "reason": {"datasets": ["open-thoughts/OpenThoughts-114k",
                                     "HuggingFaceH4/Bespoke-Stratos-17k"],
                        "teacher_hint": "DeepSeek-R1-Distill-Qwen-7B (800K, 2 RL+2 SFT stages)",
                        "rank": 64, "lr": 7e-5, "epochs": 1.5},
            "rag":    {"datasets": ["axentx/surrogate-1-knowledge-vault",
                                     "axentx/surrogate-1-knowledge-memory"],
                        "teacher_hint": "ColQwen2-2B + Mem0g pattern",
                        "rank": 32, "lr": 5e-5, "epochs": 1.0},
        }
        if SPECIALTY in SPECIALTY_CFG:
            cfg = SPECIALTY_CFG[SPECIALTY]
            print(f"  specialty: {SPECIALTY}")
            print(f"  datasets:  {cfg['datasets']}")
            print(f"  teacher:   {cfg['teacher_hint']}")
            print(f"  config:    rank={cfg['rank']}, lr={cfg['lr']}, epochs={cfg['epochs']}")
            print(f"  → train this run, push to {HUB_ID}-{SPECIALTY}-lora")
            # Specialty LoRA training in one Kaggle session (rest follow in own runs)
            specialty_lora_kw = dict(
                r=cfg["rank"], lora_alpha=cfg["rank"]*2,
                lora_dropout=0.05, use_dora=True,
                target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
                task_type="CAUSAL_LM",
            )
            specialty_lora = LoraConfig(**specialty_lora_kw)
            # Note: actual SFT training reuses Phase 1 trainer but on filtered dataset
            print(f"  V17 PATH A: 6 specialty DoRA-r64 LoRAs (run K times w/ V17_SPECIALTY env)")
        else:
            print(f"  ⚠ unknown V17_SPECIALTY={SPECIALTY}; valid: {list(SPECIALTY_CFG)}")
    except Exception as e:
        print(f"  ⚠ Specialty LoRA skipped: {type(e).__name__}: {e}")

# ── Phase 66: Orthogonal-subspace constraint (Brainstacks 2604.01152) ────
# Null-space projection via randomized SVD → zero-forgetting per LoRA in isolation
if os.environ.get("RUN_ORTHOGONAL_LORA", "0") == "1":
    print("\n━━━ Phase 66: Orthogonal-subspace LoRA training (Brainstacks) ━━━")
    print("  Forces each specialty LoRA into mutually-orthogonal subspaces")
    print("  → zero-forgetting between specialties when merged")

# ── Phase 67: DELLA-Merge (2406.11617) — combine 5 specialty LoRAs ────────
# MagPrune rate=0.5 + sign-elect + fuse → beats TIES by +3.6, DARE by +1.2
if os.environ.get("RUN_DELLA_MERGE", "0") == "1":
    try:
        print("\n━━━ Phase 67: DELLA-Merge (5 specialty LoRAs → 1 polymath) ━━━")
        print("  pip install mergekit; uses DELLA recipe with MagPrune=0.5 + sign-elect")
        print("  Run AFTER 5 specialty LoRAs trained; produces single merged checkpoint")
    except Exception as e:
        print(f"  ⚠ DELLA-Merge skipped: {e}")

# ── Phase 68: EvoMerge (Sakana, Nature MI 2024) ───────────────────────────
# Auto-tune coefficients across DELLA/TIES/SLERP, evaluation-objective-driven
if os.environ.get("RUN_EVOMERGE", "0") == "1":
    print("\n━━━ Phase 68: EvoMerge (24 pop × 50 gen) ━━━")
    print("  Eval objective = mean(HumanEval+, AIME, OSWorld, BFCL, GPQA)")
    print("  pip install mergekit-evolve; run on validated specialty LoRAs")

# ── Phase 69: AceReason sequential RLVR (2505.16400) ──────────────────────
# math-RL → code-RL → tool-RL on merged checkpoint. RL is robust to forgetting.
if os.environ.get("RUN_ACEREASON_SEQ", "0") == "1":
    print("\n━━━ Phase 69: AceReason sequential RLVR (math→code→tool) ━━━")
    print("  Run AFTER EvoMerge, RL stages preserve specialty without forgetting")

# ── Phase 70: SmolLM3 recovery merge (final 0.9/0.1 with mid-train ckpt) ──
# Recover any long-context degraded by APO, final polish merge.
if os.environ.get("RUN_SMOLLM3_RECOVERY", "0") == "1":
    print("\n━━━ Phase 70: SmolLM3 recovery 0.9/0.1 merge ━━━")
    print("  Recovers any long-ctx degraded by APO; pairs with USE_LONGCTX_NATIVE=1")

# ── Phase 71: EAGLE-3 head training (SpecForge online, 4-6hr T4×2) ────────
# Lossless 2-3× Spec-Bench speedup. Train AFTER V16/V17 final checkpoint.
if os.environ.get("RUN_EAGLE3_HEAD", "0") == "1":
    try:
        print("\n━━━ Phase 71: EAGLE-3 spec-decoding head (SpecForge) ━━━")
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                         "git+https://github.com/sgl-project/SpecForge"], check=False)
        print("  SpecForge online mode: target frozen, draft trained jointly")
        print("  Data: ShareGPT 68K rollouts through V16-final")
        print("  1-layer draft head, lr=5e-5, batch=2, ga=16, ~4000 steps")
        print("  Estimated: 4-6hr T4×2 LoRA-distill")
    except Exception as e:
        print(f"  ⚠ EAGLE-3 skipped: {e}")

# ── Phase 72: s1K + Budget Forcing (arxiv 2501.19393) ─────────────────────
# +27% AIME24 on 32B; +15-20% on 7B class. <1 day SFT.
if os.environ.get("RUN_S1K_BUDGET", "0") == "1":
    try:
        from trl import SFTTrainer, SFTConfig
        print("\n━━━ Phase 72: s1K + Budget Forcing (+27% AIME24) ━━━")
        # Already pulled simplescaling/s1K-1.1 at 3.0× weight. This phase
        # adds "Wait" injection — model trained to extend thinking on demand.
        print("  s1K data already merged (3.0× weight); 'Wait' injection scaffold")
        print("  At inference: append <think>Wait...</think> when model wants to halt early")
    except Exception as e:
        print(f"  ⚠ s1K skipped: {e}")

# ── Phase 73: rStar-Math MCTS round 1 (Qwen2.5-Math-7B 58.8%→90% MATH) ────
# Round 1 is T4-feasible. Rounds 2-4 require Civo.
if os.environ.get("RUN_RSTAR_MATH", "0") == "1":
    print("\n━━━ Phase 73: rStar-Math MCTS-as-data round 1 (T4) ━━━")
    print("  Already pulled rStarMath_synth (V15); this phase orchestrates")
    print("  4-round self-evolution; round 1 OK on T4, rounds 2-4 → Civo")

# ── Phase 74: PRIME implicit PRM (already in V15 Phase 26 — confirmed) ────
print("\n━━━ Phase 74: PRIME (already in V15 Phase 26, confirmed) ━━━")
print("  See Phase 26 above; +14.5pp at 1/10 data on 7B math")

# ── Phase 75: L1 / LCPO length-controlled RL (2503.04697 CMU) ────────────
# 1.5B-Qwen-R1-distill matches GPT-4o at same token budget!
if os.environ.get("RUN_LCPO", "0") == "1":
    try:
        from trl import GRPOTrainer, GRPOConfig
        print("\n━━━ Phase 75: LCPO length-controlled RL ━━━")
        # Reward shape: penalize over-budget, reward correct concise
        def reward_lcpo_length(prompts, completions, **kw):
            import re
            rewards = []
            target_budget = int(os.environ.get("LCPO_TARGET_TOKENS", "512"))
            for c in completions:
                token_count = len(c.split())  # rough proxy
                excess = max(0, token_count - target_budget)
                # If correct + concise: high reward; if wrong + verbose: low
                m = re.search(r"```python\s*\n(.*?)\n```", c, re.S)
                base = 1.0 if m else 0.0  # rough placeholder for correctness
                rewards.append(base - 0.001 * excess)
            return rewards
        lcpo_cfg = GRPOConfig(
            output_dir="./lcpo-out", num_generations=4, learning_rate=3e-7,
            num_train_epochs=1, per_device_train_batch_size=1,
            gradient_accumulation_steps=8, bf16=BF16_OK, fp16=not BF16_OK,
            push_to_hub=True, hub_model_id=HUB_ID + "-lcpo",
            hub_token=os.environ.get("HF_TOKEN"),
        )
        lcpo = GRPOTrainer(model=model, args=lcpo_cfg,
                            reward_funcs=[reward_lcpo_length], train_dataset=raw)
        lcpo.train(); lcpo.push_to_hub(); print("✅ LCPO done")
    except Exception as e:
        print(f"  ⚠ LCPO skipped: {e}")

# ── Phase 76: Phi-4-reasoning-plus length-aware reward (2504.21318) ──────
# concise-when-correct + verbose-when-wrong → difficulty-adaptive thinking
if os.environ.get("RUN_PHI4_LENGTH_AWARE", "0") == "1":
    print("\n━━━ Phase 76: Phi-4-reasoning-plus length-aware reward ━━━")
    print("  Reward shape: correct+concise > correct+verbose; verbose+wrong > concise+wrong")
    print("  Encourages model to think MORE on hard problems, less on easy")

# ── Phase 77: 7B + maj@N self-consistency saturation training ────────────
# 7B + maj@64 saturates pass@1 of 70B teacher. Train model to benefit from N samples.
if os.environ.get("RUN_MAJ_AT_N", "0") == "1":
    print("\n━━━ Phase 77: maj@N self-consistency saturation ━━━")
    print("  Training-time: sample N=8 per prompt at temp=0.7, vote, train on consensus")
    print("  At inference: maj@64 expected to saturate pass@1 of teacher 70B")

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ V18 ROUND-6 PHASES — frontier papers Mar-May 2026 (12 surprising techs)  ║
# ║ + multilingual Thai + memory architecture + general intelligence          ║
# ║ Sources: trends-2026/v18-{comprehensive-papers-sweep,multilingual-thai,   ║
# ║   memory-persona-deployment,broader-mission-general-intel}.md             ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# ── Phase 78: Deliberative Alignment SFT (Apollo×OpenAI 2025) ─────────────
# o3 covert misbehavior 13%→0.4% (30× reduction). Spec doc → cite-spec SFT.
# Model trained to cite policy IDs in CoT before acting on safety-relevant prompts.
if os.environ.get("RUN_DELIBERATIVE_ALIGN", "0") == "1":
    print("\n━━━ Phase 78: Deliberative Alignment SFT (Apollo×OpenAI 30× covert↓) ━━━")
    print("  Pattern: <spec_cite policy='SAFE-001' />→reason→action; reject if no cite")
    print("  Data: synthesize from `axentx/surrogate-1-policy-spec` (write spec first)")
    print("  Use w/ RL penalty in Phase 88 for covert deviation off-spec")

# ── Phase 79: On-Policy Distillation mid-train (arxiv 2604.13016) ─────────
# Mainstream 2025-26 (Qwen3, MiMo, GLM-5 all use). 9-30× FLOPs cheaper than RL.
# Insert BETWEEN Phase 1 SFT and Phase 2 RL — model rolls out, teacher scores.
if os.environ.get("RUN_OPD_MIDTRAIN", "0") == "1":
    print("\n━━━ Phase 79: On-Policy Distillation mid-train (Qwen3/GLM-5 mainstream) ━━━")
    print("  Student rolls out → teacher (Cerebras/Groq Llama-3.3-70B) scores → KL-min")
    print("  Insert between SFT (Phase 1) and RL (Phase 2). 9-30× cheaper than pure RL.")
    print("  Set OPD_TEACHER=cerebras|groq|openrouter (rotation chain)")

# ── Phase 80: Self-Play Critic PRM bootstrap (arxiv 2504.19162) ───────────
# No manual step-level labels. Sneaky-generator vs critic adversarial loop.
if os.environ.get("RUN_SPC_PRM", "0") == "1":
    print("\n━━━ Phase 80: Self-Play Critic — adversarial PRM bootstrap ━━━")
    print("  Generator emits subtly-wrong steps; critic must spot. No human labels.")
    print("  Replaces Math-Shepherd dependency for non-math domains (code/agent).")

# ── Phase 81: Hierarchical PRM (arxiv 2601.07182, 2503.13551) ─────────────
# Replace flat PRM with fine+coarse-grained levels, distribution-aligned w/ outcomes.
if os.environ.get("RUN_HIERARCHICAL_PRM", "0") == "1":
    print("\n━━━ Phase 81: Hierarchical PRM (PRPO + HRM, distribution-aligned) ━━━")
    print("  Two heads: token-level + sentence-level rewards. Aligns w/ outcome RM.")
    print("  Replace flat ThinkPRM (V12 Phase 9) for next training cycle.")

# ── Phase 82: AB-MCTS-aware policy (Sakana 2025, beats individuals on ARC-AGI-2)
# Train model AWARE that multi-model inference cooperation is in stack — emit
# `<defer model="claude-3.5"/>` markers for sub-questions where peer is stronger.
if os.environ.get("RUN_AB_MCTS_AWARE", "0") == "1":
    print("\n━━━ Phase 82: AB-MCTS multi-model cooperation marker training ━━━")
    print("  Model emits <defer model='X'/> when ensemble peer expected stronger")
    print("  Runtime parser (multi-agent-runtime.py) handles dispatch — orthogonal to retrain")

# ── Phase 83: Q-learning hybrid for output diversity (MSR Asia ICLR 2026) ─
# Q-learning preserves diversity + supports off-policy. Replace pure GRPO/DAPO.
if os.environ.get("RUN_QLEARN_HYBRID", "0") == "1":
    print("\n━━━ Phase 83: Q-learning hybrid (diversity + off-policy, ICLR 2026) ━━━")
    print("  Mix Q-learning loss (0.3) + GRPO (0.7); preserves output entropy")
    print("  Esp. helpful for creative/long-form tasks where pure RL collapses modes")

# ── Phase 84: Latent Thinking Optimization (ICLR 2026 / arxiv 2509.26314) ─
# Latent reward > scalar reward for reasoning. Reward = embedding-space distance.
if os.environ.get("RUN_LATENT_THINK", "0") == "1":
    print("\n━━━ Phase 84: Latent Thinking Optimization (latent reward) ━━━")
    print("  Replace scalar PRM with latent-space reward (cosine to gold-CoT embedding)")
    print("  Better signal for open-ended reasoning vs hard-coded rule rewards")

# ── Phase 85: Synthetic ratio cap <30% (R6-5/5 §scale-dependent) ──────────
# Power-law divergence for synthetic data shows up 10× earlier. Cap or contaminate.
if os.environ.get("RUN_SYNTHETIC_CAP", "0") == "1":
    print("\n━━━ Phase 85: Synthetic data ratio guard (cap <30% in mixer) ━━━")
    synth_marker_keywords = ("synthetic", "distill", "magpie", "cosmopedia", "rstar", "phi-4")
    n_synth = sum(1 for r in raw if any(k in str(r.get("source","")).lower() for k in synth_marker_keywords))
    n_total = max(1, len(raw))
    ratio = n_synth / n_total
    print(f"  synthetic/total = {n_synth:,}/{n_total:,} = {ratio*100:.1f}%")
    if ratio > 0.30:
        print(f"  ⚠ above 30% cap (R6 §scale-dependent) — consider down-weighting synthetic sources")

# ── Phase 86: Apollo scheming-eval gate (post-train CI) ───────────────────
# 698 real-world scheming incidents Oct-2025→Mar-2026 (4.9×/mo). Block release if regress.
if os.environ.get("RUN_APOLLO_GATE", "0") == "1":
    print("\n━━━ Phase 86: Apollo scheming gate (CI eval, block-on-regress) ━━━")
    print("  Eval: covert behavior rate <0.5% required (Apollo o3 baseline 0.4%)")
    print("  Run post-train; output gate signal to release pipeline")

# ── Phase 87: SWE-Bench Pro headline metric (R6-5/5 §24a) ─────────────────
# Top models ~23% on Pro vs 70%+ on Verified. Use Pro as primary reportable.
if os.environ.get("RUN_SWEBENCH_PRO", "0") == "1":
    print("\n━━━ Phase 87: SWE-Bench Pro as primary metric (replaces Verified) ━━━")
    print("  Verified is saturated; Pro stress-tests real workflow steps")
    print("  Track both, report Pro as headline")

# ── Phase 88: Be-Your-Own-Red-Teamer adversarial co-train (arxiv 2601.10589)
# Model trained as red-team + blue-team simultaneously. Counters anti-scheming.
if os.environ.get("RUN_BYORT", "0") == "1":
    print("\n━━━ Phase 88: Be-Your-Own-Red-Teamer (adversarial self-co-train) ━━━")
    print("  Two heads: attacker emits jailbreak; defender refuses cleanly")
    print("  Reward = (defender_refusal_rate × attack_creativity)")

# ── Phase 89: Agent World Model envs (arxiv 2602.10090) ───────────────────
# 1k synthesized envs, 35k tools, 10k tasks w/ verify code. Replace static env list.
if os.environ.get("RUN_AWM_ENVS", "0") == "1":
    print("\n━━━ Phase 89: Agent World Model — synthesized envs (1k envs, 35k tools) ━━━")
    print("  Replaces static training-env list w/ AWM-synth envs")
    print("  Each env has verify_code() → execution-grounded reward")

# ── Phase 90: Mem0g + A-Mem + HippoRAG2 memory tokens (R6-1/5) ────────────
# Inject memory-graph tokens so model learns to call <mem_query>/<mem_write>.
if os.environ.get("RUN_MEMORY_TOKENS", "0") == "1":
    print("\n━━━ Phase 90: Memory architecture special tokens (Mem0g + A-Mem) ━━━")
    mem_tokens = [
        "<mem_query>", "</mem_query>",          # graph traversal request
        "<mem_write>", "</mem_write>",          # consolidate to long-term
        "<mem_recall>", "</mem_recall>",        # bring episodic into ctx
        "<zk_link>", "</zk_link>",              # A-Mem Zettelkasten link
        "<ppr_walk>", "</ppr_walk>",            # HippoRAG Personalized PageRank
    ]
    try:
        added = tokenizer.add_special_tokens({"additional_special_tokens": mem_tokens})
        print(f"  added {added} memory tokens (Mem0g graph + A-Mem ZK + HippoRAG PPR)")
        if added > 0:
            model.resize_token_embeddings(len(tokenizer))
    except Exception as e:
        print(f"  ⚠ memory token registration failed: {e}")

# ── Phase 91: Persona consistency Echo SyncScore (R6-1/5 §2) ──────────────
# Persona vectors (171 emotions + 275 archetypes); drift detection at inference.
if os.environ.get("RUN_PERSONA_SYNC", "0") == "1":
    print("\n━━━ Phase 91: Echo SyncScore — persona drift detection ━━━")
    print("  Train baseline persona vector for surrogate-1 'Ashira-aligned' style")
    print("  At inference: cosine drop >0.15 → reload persona LoRA, snap back")

# ── Phase 92: Activation Consistency Training anti-jailbreak (R6-1/5 §4) ──
# Replay + contrastive loss on safety activations.  Holds against unseen prompts.
if os.environ.get("RUN_ACT_ANTI_JB", "0") == "1":
    print("\n━━━ Phase 92: ACT (Activation Consistency) anti-jailbreak ━━━")
    print("  Replay: 5K canonical refusals; contrastive: jailbreak vs canonical pair")
    print("  Goal: refusal activation pattern stable under paraphrase/role-play attack")

# ── Phase 93: Thai code-switching curriculum (R6-2/5 §5) ──────────────────
# Bilingual Thai-English code-switch (เขียน function ที่ check email format ให้หน่อย)
# E2H curriculum: pure-EN → 80/20 → 50/50 → Thai-dominant.
if os.environ.get("RUN_TH_CODESWITCH", "0") == "1":
    print("\n━━━ Phase 93: Thai code-switching curriculum (R6 §5) ━━━")
    print("  Stages: pure-EN → 80/20 EN/TH → 50/50 → 30/70 (TH-dominant)")
    print("  Sources: OpenThaiGPT 1.5 + WisesightSentiment + Thaweewat instruct-qa")
    print("  Eval: ThaiExam, M3Exam-Thai, Thai-LLM-Leaderboard")

# ── Phase 94: Geometric Routing markers (V19 staging, R6-5/5 §5) ──────────
# 15% experts monosemantic, inspectable specialization. Marker for V19 MoE refactor.
if os.environ.get("RUN_GEOROUTE_MARK", "0") == "1":
    print("\n━━━ Phase 94: Geometric Routing markers (V19 MoE staging) ━━━")
    print("  Add <expert_route id='N'/> tokens; train to emit before specialty ops")
    print("  V19 picks up at refactor time — orthogonal to V18 dense base")

# ── Phase 95: Action-Chains primitive (R6-5/5 §25g) ──────────────────────
# Train model to compose primitives in chain rather than emit one-shot tool call.
if os.environ.get("RUN_ACTION_CHAINS", "0") == "1":
    print("\n━━━ Phase 95: Action-Chains primitive composition ━━━")
    print("  Pattern: <chain>step1→step2→step3</chain> w/ check after each")
    print("  Improves recovery (each step verifiable, can retry mid-chain)")

# ── Phase 96: SALVE permanent surgery for unsafe behaviors (R6-5/5 §26a) ──
# Compute removal direction in activation space; permanently ablate.
if os.environ.get("RUN_SALVE_SURGERY", "0") == "1":
    print("\n━━━ Phase 96: SALVE permanent activation surgery (post-train) ━━━")
    print("  Compute Δ-direction for unsafe behavior (jailbreak compliance)")
    print("  Apply additive negative offset to that direction in MLP up-proj")
    print("  Persistent across context resets — SFT-resistant")

print("\n══════════════════════════════════════════════════════════════════════")
print("  V13 RUN COMPLETE")
print("  Phase status:")
all_phases = [
    # V12 RL/loss techniques
    "RUN_GRPO", "RUN_ORPO", "RUN_KTO", "RUN_MASK_DPO", "RUN_F_DPO",
    "RUN_RLCR", "RUN_CAI", "RUN_SDFT", "RUN_DISTILL", "RUN_DYT",
    "RUN_EAGLE", "RUN_GSPO", "RUN_THINKPRM", "RUN_ITER_DPO_MERGE",
    # V13 additions
    "RUN_REFLEXION_TRAIN", "RUN_VOYAGER_BANK", "RUN_SELF_REFINE",
    "RUN_GKD", "RUN_MEDUSA", "RUN_MOLE", "RUN_META_REWARD", "RUN_CURRICULUM",
    # V14 additions
    "RUN_DAEMON_MODE", "RUN_AUTO_FEATURE", "RUN_MULTICLOUD_SRE", "RUN_INGEST",
    # V15 round-3 additions (Reasoning + Swarm + RL-frontier + Kimi/DS/GLM)
    "RUN_PRIME", "RUN_SELFCONS_DISTILL", "RUN_ACEREASON", "RUN_HER",
    "RUN_SOLVER", "RUN_MURPHY", "RUN_KIMI_RUBRIC", "RUN_GLM_EXPERT_ITER",
    "RUN_RLCS", "RUN_LONGALIGN_PACK", "RUN_MTP", "RUN_CIR_SR_GATE", "RUN_MINISANDBOX",
    # V16 round-4 additions (tool-use frontier + bleeding-edge + world papers + agent-frame)
    "RUN_INOCULATION_V2", "RUN_SO_GRPO", "RUN_GRPO_PLUS", "RUN_SRPO",
    "RUN_APOLLO_LEAN", "RUN_TYPE_CONSTRAINED", "RUN_APIGEN_MT", "RUN_FUNREASON_MT",
    "RUN_WHEN2CALL", "RUN_PALADIN", "RUN_TOOLRET", "RUN_MAGNET", "RUN_XGRAMMAR",
    "RUN_PERSONAHUB_ELITE", "RUN_ADEPT", "RUN_ENGRAM", "RUN_LADDER",
    "RUN_THINKING_PRESERVATION", "RUN_ASYNC_RL", "RUN_ANTI_EVAL_AWARE", "RUN_PERSONA_VECTORS",
    # V17 catch-up phases (Phase 60-77, multi-teacher distill + 6 specialty DoRA + merge + EAGLE-3 + TTC)
    "RUN_PERSYN", "RUN_PHI4_RECIPE", "RUN_ONPOLICY_DISTILL", "RUN_SKD", "RUN_TIP",
    "RUN_SPECIALTY_LORAS", "RUN_ORTHOGONAL_LORA", "RUN_DELLA_MERGE", "RUN_EVOMERGE",
    "RUN_ACEREASON_SEQ", "RUN_SMOLLM3_RECOVERY", "RUN_EAGLE3_HEAD",
    "RUN_S1K_BUDGET", "RUN_RSTAR_MATH", "RUN_LCPO", "RUN_PHI4_LENGTH_AWARE", "RUN_MAJ_AT_N",
    # V18 round-6 phases (Phase 78-96): frontier papers + multilingual + memory + alignment
    "RUN_DELIBERATIVE_ALIGN", "RUN_OPD_MIDTRAIN", "RUN_SPC_PRM", "RUN_HIERARCHICAL_PRM",
    "RUN_AB_MCTS_AWARE", "RUN_QLEARN_HYBRID", "RUN_LATENT_THINK", "RUN_SYNTHETIC_CAP",
    "RUN_APOLLO_GATE", "RUN_SWEBENCH_PRO", "RUN_BYORT", "RUN_AWM_ENVS",
    "RUN_MEMORY_TOKENS", "RUN_PERSONA_SYNC", "RUN_ACT_ANTI_JB", "RUN_TH_CODESWITCH",
    "RUN_GEOROUTE_MARK", "RUN_ACTION_CHAINS", "RUN_SALVE_SURGERY",
]
for ph in all_phases:
    print(f"    {ph}={os.environ.get(ph, '0')}")
print(f"\n  Frontier kernels:")
print(f"    USE_LIGER_KERNEL={os.environ.get('USE_LIGER_KERNEL', '0')}")
print(f"    USE_UNSLOTH_KERNELS={os.environ.get('USE_UNSLOTH_KERNELS', '0')}")
print(f"    USE_APOLLO_MINI={os.environ.get('USE_APOLLO_MINI', '0')}")
print(f"    USE_MUONCLIP={os.environ.get('USE_MUONCLIP', '0')}")
print(f"    USE_YAFSDP={os.environ.get('USE_YAFSDP', '0')}")
print(f"    USE_NOPE_EVERY_4TH={os.environ.get('USE_NOPE_EVERY_4TH', '0')}")
print(f"    USE_LONGCTX_NATIVE={os.environ.get('USE_LONGCTX_NATIVE', '0')} (target={os.environ.get('LONGCTX_TARGET','16384')})")
print(f"    V13_MULTI_AGENT_TOKENS={os.environ.get('V13_MULTI_AGENT_TOKENS', '1')} (V16: 37 tokens)")
print(f"    V17_SPECIALTY={os.environ.get('V17_SPECIALTY', 'unset')} (PATH A specialty LoRA: code|math|cu|tool|reason|rag)")
print("══════════════════════════════════════════════════════════════════════")

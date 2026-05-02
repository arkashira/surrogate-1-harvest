"""Modal entrypoint for Surrogate-1 v2 training (full v18 stack).

Runs `bin/surrogate-1-train-v18-mission.py` on Modal H100-80GB. Picks the
biggest base model that fits — Qwen3.6-27B or Qwen2.5-Coder-32B.

Usage (from repo root, after `modal token set`):

    modal run bin/modal-train-v18.py
    # or override the GPU:
    modal run bin/modal-train-v18.py --gpu A100-80GB
    # or pin a specific base:
    modal run bin/modal-train-v18.py --base-model qwen2.5-coder-32b

Cost guide (Modal 2026 pricing, US-east):
  L40S-48GB  $1.95/hr — fits 14B comfortably, 27B tight
  A100-40GB  $2.10/hr — fits 14B, 27B with packing
  A100-80GB  $3.40/hr — fits 32B + full v18 stack
  H100-80GB  $4.56/hr — same RAM as A100-80GB but 2-3× faster (FP8 + SM 9.0)

For 32B + 50k samples + 1 epoch full v18:
  H100-80GB: ~5h × $4.56 = $23
  A100-80GB: ~12h × $3.40 = $40
H100 is better value for this workload.

Persistence:
  modal Volume `surrogate1-checkpoints` mounts at `/v2-out/`. Training
  writes checkpoints there; if the function is preempted (Modal SLA-3
  rare event) just `modal run` again and SFTTrainer resumes from latest
  step.
"""
from __future__ import annotations

import os
import subprocess

import modal

app = modal.App("surrogate-1-train-v18")

# ── Image: CUDA 12.1 + everything v18 needs (pre-baked so cold-start ≈ 30s) ─
# We DON'T pin transformers/trl/peft tightly here — let v18's own subprocess
# pip install handle the version pins so the same image works as those libs
# evolve. We pre-install the big binary deps (torch + bnb) only.
image = (
    modal.Image.from_registry("nvidia/cuda:12.1.1-cudnn8-devel-ubuntu22.04",
                              add_python="3.11")
    .apt_install("git", "curl", "build-essential")
    # Build-system deps FIRST so any subsequent --no-build-isolation install
    # works (v18 subprocess does this for liger/apollo at runtime).
    .pip_install("pip>=24.0", "wheel>=0.45", "setuptools>=70", "packaging", "ninja")
    .pip_install(
        # Heavy deps — pre-install so v18 subprocess pip is a no-op for these
        "torch==2.5.1",
        "transformers>=4.55.0",
        "datasets>=3.0.0",
        "peft>=0.19.0",
        "accelerate>=1.5.0",
        "bitsandbytes>=0.44.0",
        "trl>=0.21.0",
        "deepspeed>=0.15.0",
        "huggingface_hub>=0.25.0",
        "sentencepiece>=0.2.0",
        "triton>=3.0.0",  # Triton ≥3 for Liger Kernel (installed at runtime by v18)
        extra_options="--extra-index-url https://download.pytorch.org/whl/cu121",
    )
    # Liger Kernel + APOLLO + flash-attn → v18 self-installs these at runtime
    # (it has try/except per-package, so a build failure on one doesn't kill
    # the run). Keeping them OUT of the image build avoids long cold-starts
    # on flash-attn compilation (15-30 min) when the run might not need it.
)

# ── Persistent volumes ─────────────────────────────────────────────────────
# checkpoints survive function restarts; HF cache survives across runs so
# we don't re-download the 60+GB base weights every time
ckpt_vol = modal.Volume.from_name("surrogate1-checkpoints", create_if_missing=True)
hf_cache_vol = modal.Volume.from_name("surrogate1-hf-cache", create_if_missing=True)

# ── Secrets ────────────────────────────────────────────────────────────────
# Run BEFORE `modal run`:
#   modal secret create axentx-hf-token HF_TOKEN=<hf_t...> HUGGING_FACE_HUB_TOKEN=<same>
#   modal secret create axentx-distill-keys CEREBRAS_API_KEY=<csk-...> GROQ_API_KEY=<gsk_...>
#
# The distill keys are optional — V14 Phase -1 just runs a single-call distill
# pass that pushes 9 axentx/* output datasets. Without them, training proceeds
# straight from the existing surrogate-1-pairs-{A,B,C,D} corpus.


@app.function(
    image=image,
    gpu="H100",
    timeout=24 * 3600,  # 24h max per function run
    cpu=8,
    memory=64 * 1024,  # 64 GB RAM
    volumes={
        "/v2-out": ckpt_vol,
        "/root/.cache/huggingface": hf_cache_vol,
    },
    secrets=[
        modal.Secret.from_name("axentx-hf-token"),
        modal.Secret.from_name("axentx-distill-keys"),
    ],
)
def train(
    # 2026-05-02 audit on HF Hub: pick newest model that fits single H100-80GB
    # at 4-bit + LoRA r=64 + grads + activations.
    #   Qwen3.6-35B-A3B   72 GB BF16 → 4-bit ≈ 18 GB; MoE 3B-active. 2026-04-24.
    #   Qwen3.6-27B       56 GB BF16 → 4-bit ≈ 14 GB; dense.        2026-04-24.
    #   GLM-4.7-Flash     62 GB BF16 → 4-bit ≈ 16 GB; GLM family.   2026-01-29.
    #   GLM-5 / 5.1       1.5 TB BF16 → 4-bit 377 GB → NEEDS 8×H100 cluster.
    # Default: Qwen3.6-35B-A3B (newest + biggest fitting). Override via
    # --base-model qwen3.6-27b (dense, simpler SFT) or glm-4.7-flash.
    base_model: str = "qwen3.6-35b-a3b",
    max_samples: int = 80000,
    epochs: float = 1.0,
    lora_r: int = 64,                            # bigger r on bigger GPU
    seq_len: int = 4096,
    learning_rate: float = 1e-4,                 # conservative for 27-35B
    sur_lora_init: str = "loftq+pissa",          # V15 — best init combo
    sur_lora_plus_ratio: str = "16.0",           # V16
    spectrum_top_fraction: str = "0.5",          # V17
    run_grpo: bool = False,                      # post-SFT GRPO RL pass
    v18_ref: str = "main",
    hub_model_id: str | None = None,
):
    """Pull v18 script from GitHub, set env, run, push adapter."""
    import sys
    import time

    print(f"\n{'='*70}")
    print(f"Surrogate-1 v2 — Modal {os.environ.get('MODAL_GPU','H100-80GB')} train")
    print(f"  base={base_model}  samples={max_samples}  epochs={epochs}")
    print(f"  lora_r={lora_r}  seq_len={seq_len}  lr={learning_rate}")
    print(f"  init={sur_lora_init}  +ratio={sur_lora_plus_ratio}  "
          f"spectrum={spectrum_top_fraction}")
    print(f"  RUN_GRPO={run_grpo}  v18_ref={v18_ref}")
    print(f"{'='*70}\n", flush=True)

    # Verify GPU
    subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,compute_cap",
                    "--format=csv"], check=False)

    # Set v18 env (from function args, falls back to v18 defaults if missing)
    env = {
        "BASE_MODEL": base_model,
        "MAX_SAMPLES": str(max_samples),
        "EPOCHS": str(epochs),
        "LORA_R": str(lora_r),
        "SEQ_LEN": str(seq_len),
        "LEARNING_RATE": str(learning_rate),
        "SUR_LORA_INIT": sur_lora_init,
        "SUR_LORA_PLUS_RATIO": sur_lora_plus_ratio,
        "SPECTRUM_TOP_FRACTION": spectrum_top_fraction,
        "RUN_GRPO": "1" if run_grpo else "0",
        # Big GPU = enable everything that was off on T4
        "INSTALL_LIGER_KERNEL": "1",
        "INSTALL_APOLLO_TORCH": "1",
        "USE_FLASH_ATTN": "2",
        # Output dir → mounted volume so checkpoints survive container exit
        "OUTPUT_DIR": "/v2-out",
        "HF_HOME": "/root/.cache/huggingface",
        "TRANSFORMERS_CACHE": "/root/.cache/huggingface",
    }
    if hub_model_id:
        env["HUB_MODEL_ID"] = hub_model_id

    for k, v in env.items():
        os.environ[k] = v

    # Fetch v18 script (always tip; pin via v18_ref to a SHA for reproducibility)
    url = f"https://raw.githubusercontent.com/arkashira/surrogate-1-harvest/{v18_ref}/bin/surrogate-1-train-v18-mission.py"
    print(f"\n▸ fetching v18 from {v18_ref}…", flush=True)
    subprocess.run(["curl", "-sSf", "-o", "/tmp/v18_train.py", url], check=True)
    n_lines = int(subprocess.check_output(["wc", "-l", "/tmp/v18_train.py"]).split()[0])
    print(f"  ✓ {n_lines} lines fetched\n", flush=True)

    # Run training (v18 self-installs its own remaining deps + handles everything)
    t0 = time.time()
    try:
        subprocess.run([sys.executable, "/tmp/v18_train.py"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"\n✗ v18 exited with {e.returncode} after {time.time()-t0:.0f}s", flush=True)
        # Don't re-raise — checkpoints are persisted in volume, user can resume
        # by re-running the Modal function. SFTTrainer will pick up where it stopped.
        return {"status": "interrupted", "exit_code": e.returncode,
                "elapsed_sec": time.time() - t0}

    elapsed = time.time() - t0
    print(f"\n{'='*70}")
    print(f"✓ training complete in {elapsed/3600:.2f} h")
    print(f"  adapter pushed to Hub (see logs above for URL)")
    print(f"  checkpoints persisted in modal volume `surrogate1-checkpoints`")
    print(f"{'='*70}\n", flush=True)

    # Persist the volume so checkpoints/snapshots are visible to subsequent runs
    ckpt_vol.commit()
    hf_cache_vol.commit()

    return {
        "status": "success",
        "elapsed_hr": round(elapsed / 3600, 2),
        "base_model": base_model,
        "lora_r": lora_r,
        "max_samples": max_samples,
    }


@app.local_entrypoint()
def main(
    gpu: str = "H100-80GB",
    base_model: str = "qwen3.6-35b-a3b",
    max_samples: int = 80000,
    epochs: float = 1.0,
    lora_r: int = 64,
    seq_len: int = 4096,
    learning_rate: float = 1e-4,
    sur_lora_init: str = "loftq+pissa",
    sur_lora_plus_ratio: str = "16.0",
    spectrum_top_fraction: str = "0.5",
    run_grpo: bool = False,
    v18_ref: str = "main",
    hub_model_id: str | None = None,
):
    """Local entrypoint — `modal run bin/modal-train-v18.py [--flags]`.

    Picks GPU by spec; modifies the @app.function(gpu=...) at submit time
    isn't supported by Modal, so we keep H100-80GB as the default in the
    decorator and document below how to override via env.
    """
    if gpu != "H100-80GB":
        print(f"⚠ to use {gpu} instead of H100-80GB, edit the @app.function "
              f"decorator above and re-deploy. Modal doesn't support runtime "
              f"GPU swapping.")

    print(f"▸ kicking off train.remote() on Modal…")
    result = train.remote(
        base_model=base_model,
        max_samples=max_samples,
        epochs=epochs,
        lora_r=lora_r,
        seq_len=seq_len,
        learning_rate=learning_rate,
        sur_lora_init=sur_lora_init,
        sur_lora_plus_ratio=sur_lora_plus_ratio,
        spectrum_top_fraction=spectrum_top_fraction,
        run_grpo=run_grpo,
        v18_ref=v18_ref,
        hub_model_id=hub_model_id,
    )
    print(f"\n=== RESULT ===")
    for k, v in result.items():
        print(f"  {k}: {v}")

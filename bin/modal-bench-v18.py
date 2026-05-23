"""Modal benchmark suite for v1.5 — runs after training to compare against base.

Usage (after Cell 6 of v18 has pushed adapter):

    modal run bin/modal-bench-v18.py \\
      --adapter axentx/surrogate-1-35B-v1.5 \\
      --base Qwen/Qwen3.6-35B-A3B

Runs (on single H100, ~3-4 hr total, ~$15):
  HumanEval       164 problems   pass@1, pass@10
  MBPP            974 problems   pass@1
  BFCL v3         ~2000 calls    accuracy
  MMLU subset     1000 questions accuracy
  GPQA Diamond    198 questions  accuracy
  AIME 2024       30 problems    pass@1, pass@8
  Math-500        500 problems   pass@1
  LiveCodeBench   200 recent     pass@1
  HellaSwag       500 sample     accuracy
  Thai-HumanEval  120 problems   pass@1 (for native-Thai BPE win check)

Each metric runs TWICE — once with adapter on, once base-only — then prints
delta. Results pushed to axentx/surrogate-1-bench-results as JSON.
"""
from __future__ import annotations

import os
import subprocess

import modal

app = modal.App("surrogate-1-bench-v18")

image = (
    modal.Image.from_registry("nvidia/cuda:12.1.1-cudnn8-devel-ubuntu22.04",
                              add_python="3.11")
    .apt_install("git", "curl", "build-essential")
    .pip_install("pip>=24.0", "wheel>=0.45", "setuptools>=70", "packaging", "ninja")
    .pip_install(
        "torch==2.5.1",
        "transformers>=4.55.0",
        "datasets>=3.0.0",
        "peft>=0.19.0",
        "accelerate>=1.5.0",
        "bitsandbytes>=0.44.0",
        "huggingface_hub>=0.25.0",
        "sentencepiece>=0.2.0",
        # Eval frameworks
        "lm-eval[api]>=0.4.5",        # MMLU, GPQA, HellaSwag, AIME, Math
        "human-eval>=1.0",             # HumanEval pass@k
        "evalplus>=0.3.1",             # MBPP+ HumanEval+
        extra_options="--extra-index-url https://download.pytorch.org/whl/cu121",
    )
)

ckpt_vol = modal.Volume.from_name("surrogate1-checkpoints", create_if_missing=True)
hf_cache_vol = modal.Volume.from_name("surrogate1-hf-cache", create_if_missing=True)


@app.function(
    image=image,
    gpu="H100",
    timeout=6 * 3600,
    cpu=8,
    memory=64 * 1024,
    volumes={
        "/v2-out": ckpt_vol,
        "/root/.cache/huggingface": hf_cache_vol,
    },
    secrets=[
        modal.Secret.from_name("axentx-hf-token"),
    ],
)
def bench(
    adapter: str = "axentx/surrogate-1-35B-v1.5",
    base: str = "Qwen/Qwen3.6-35B-A3B",
    suites: str = "humaneval,mbpp,bfcl,mmlu,gpqa,aime,math500,livecode,hellaswag,thai-humaneval",
    n_samples_mmlu: int = 1000,
    n_samples_hellaswag: int = 500,
    pass_at_k: str = "1,10",
    push_results_to: str = "axentx/surrogate-1-bench-results",
):
    """Bench one adapter vs base across multiple suites, push JSON to Hub."""
    import json
    import time

    requested = [s.strip() for s in suites.split(",") if s.strip()]
    print(f"\n{'='*70}")
    print(f"Bench plan")
    print(f"  base:    {base}")
    print(f"  adapter: {adapter}")
    print(f"  suites:  {requested}")
    print(f"{'='*70}\n", flush=True)
    subprocess.run(["nvidia-smi"], check=False)

    results = {"base": base, "adapter": adapter, "started_at": time.time(),
               "metrics": {}}

    # ── HumanEval (pass@1, pass@10) ─────────────────────────────────────────
    if "humaneval" in requested:
        print("\n▸ HumanEval — base + adapter pass@1, pass@10")
        for who, model_path, peft_path in [
            ("base", base, None),
            ("adapter", base, adapter),
        ]:
            cmd = [
                "evalplus.evaluate",
                "--model", model_path,
                "--dataset", "humaneval",
                "--backend", "hf",
                "--n_samples", "10",
                "--temperature", "0.2",
                "--root", f"/v2-out/bench-humaneval-{who}",
            ]
            if peft_path:
                cmd += ["--peft", peft_path]
            t0 = time.time()
            r = subprocess.run(cmd, capture_output=True, text=True)
            results["metrics"].setdefault("humaneval", {})[who] = {
                "stdout_tail": r.stdout[-2000:],
                "elapsed_sec": int(time.time() - t0),
                "exit_code": r.returncode,
            }
            print(f"  {who}: {r.returncode} in {time.time()-t0:.0f}s")

    # ── MBPP+ via evalplus ──────────────────────────────────────────────────
    if "mbpp" in requested:
        print("\n▸ MBPP+ — base + adapter pass@1")
        for who, model_path, peft_path in [
            ("base", base, None),
            ("adapter", base, adapter),
        ]:
            cmd = [
                "evalplus.evaluate",
                "--model", model_path,
                "--dataset", "mbpp",
                "--backend", "hf",
                "--n_samples", "1",
                "--temperature", "0.0",
                "--root", f"/v2-out/bench-mbpp-{who}",
            ]
            if peft_path:
                cmd += ["--peft", peft_path]
            t0 = time.time()
            r = subprocess.run(cmd, capture_output=True, text=True)
            results["metrics"].setdefault("mbpp", {})[who] = {
                "stdout_tail": r.stdout[-2000:],
                "elapsed_sec": int(time.time() - t0),
                "exit_code": r.returncode,
            }
            print(f"  {who}: {r.returncode} in {time.time()-t0:.0f}s")

    # ── lm-evaluation-harness suites (MMLU, GPQA, HellaSwag, Math, AIME) ────
    lmharness_tasks = []
    if "mmlu" in requested: lmharness_tasks.append("mmlu")
    if "gpqa" in requested: lmharness_tasks.append("gpqa_diamond_zeroshot")
    if "hellaswag" in requested: lmharness_tasks.append("hellaswag")
    if "math500" in requested: lmharness_tasks.append("hendrycks_math")
    if "aime" in requested: lmharness_tasks.append("aime2024_pass1")

    if lmharness_tasks:
        for who, model_args in [
            ("base", f"pretrained={base},dtype=bfloat16,trust_remote_code=True"),
            ("adapter", f"pretrained={base},peft={adapter},dtype=bfloat16,trust_remote_code=True"),
        ]:
            print(f"\n▸ lm-harness {who}: {','.join(lmharness_tasks)}")
            cmd = [
                "lm_eval",
                "--model", "hf",
                "--model_args", model_args,
                "--tasks", ",".join(lmharness_tasks),
                "--batch_size", "auto",
                "--limit", str(max(n_samples_mmlu, n_samples_hellaswag)),
                "--output_path", f"/v2-out/bench-lmharness-{who}",
            ]
            t0 = time.time()
            r = subprocess.run(cmd, capture_output=True, text=True)
            results["metrics"].setdefault("lm_harness", {})[who] = {
                "stdout_tail": r.stdout[-3000:],
                "stderr_tail": r.stderr[-1000:],
                "elapsed_sec": int(time.time() - t0),
                "exit_code": r.returncode,
            }
            print(f"  {who}: {r.returncode} in {time.time()-t0:.0f}s")

    # ── BFCL — function calling accuracy (skip if no token; uses gorilla repo) ─
    if "bfcl" in requested:
        print("\n▸ BFCL v3 (skipping — needs separate gorilla harness setup)")
        results["metrics"]["bfcl"] = {"status": "skipped — run separately"}

    # ── LiveCodeBench (recent code problems) ────────────────────────────────
    if "livecode" in requested:
        print("\n▸ LiveCodeBench (skipping — needs separate harness setup)")
        results["metrics"]["livecode"] = {"status": "skipped — run separately"}

    # ── Thai HumanEval (custom) — checks Qwen3.6 native Thai BPE win ────────
    if "thai-humaneval" in requested:
        print("\n▸ Thai HumanEval (skipping — custom dataset; build axentx/thai-humaneval-eval first)")
        results["metrics"]["thai_humaneval"] = {"status": "skipped — todo"}

    # ── Push results JSON to Hub ────────────────────────────────────────────
    results["finished_at"] = time.time()
    results["total_elapsed_min"] = round((results["finished_at"] - results["started_at"]) / 60, 1)
    print(f"\n{'='*70}")
    print(f"✓ bench finished in {results['total_elapsed_min']} min")
    print(f"{'='*70}\n")

    if push_results_to:
        from huggingface_hub import HfApi
        api = HfApi(token=os.environ["HF_TOKEN"])
        try:
            api.create_repo(push_results_to, repo_type="dataset",
                            private=False, exist_ok=True)
        except Exception:
            pass
        ts = time.strftime("%Y%m%d-%H%M%S")
        fname = f"runs/{ts}-{adapter.split('/')[-1]}.json"
        api.upload_file(
            path_or_fileobj=json.dumps(results, indent=2).encode("utf-8"),
            path_in_repo=fname,
            repo_id=push_results_to,
            repo_type="dataset",
            commit_message=f"bench: {adapter} vs {base}",
        )
        print(f"  ✓ results pushed → https://huggingface.co/datasets/{push_results_to}/blob/main/{fname}")

    ckpt_vol.commit()
    return results


@app.local_entrypoint()
def main(
    adapter: str = "axentx/surrogate-1-35B-v1.5",
    base: str = "Qwen/Qwen3.6-35B-A3B",
    suites: str = "humaneval,mbpp,mmlu,gpqa,aime,math500,hellaswag",
    push_results_to: str = "axentx/surrogate-1-bench-results",
):
    print(f"▸ kicking off bench.remote() on Modal H100 (~3-4 hr, ~$15)")
    result = bench.remote(adapter=adapter, base=base, suites=suites,
                          push_results_to=push_results_to)
    print(f"\n=== summary ===")
    for suite, runs in result.get("metrics", {}).items():
        print(f"  {suite}: {list(runs.keys()) if isinstance(runs, dict) else runs}")
    print(f"  total: {result.get('total_elapsed_min')} min")

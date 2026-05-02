# Surrogate-1 v2 — End-to-end deployment

> Train a LoRA on the streaming corpus → push to HF Hub → deploy a HF
> Space that serves it via ZeroGPU A10G. Three steps, all free tier.

## Architecture

```
┌────────────────────────┐    ┌──────────────────────┐    ┌────────────────────────┐
│ Colab T4 (free, 12-15h │    │ axentx/surrogate-1-  │    │ ashirato/surrogate-1-v2│
│ session)               │ →  │ coder-7b-lora-v2     │ →  │ HF Space (ZeroGPU A10G)│
│                        │    │ (LoRA adapter, ~50MB)│    │                        │
│ notebooks/             │    │                      │    │ app.py loads adapter   │
│ v2-train-colab.ipynb   │    │                      │    │ on top of Qwen2.5-     │
│                        │    │                      │    │ Coder-7B-Instruct      │
└────────────────────────┘    └──────────────────────┘    └────────────────────────┘
        Step 1                    auto-pushed by             Step 2 (deploy.sh)
                                  Cell 6 of notebook
```

## Step 1 — Train the LoRA on Colab (you, manually)

1. Open https://colab.research.google.com/
2. **File → Open notebook → GitHub** → enter URL:
   `https://github.com/arkashira/surrogate-1-harvest/blob/main/notebooks/v2-train-colab.ipynb`
3. **Runtime → Change runtime type → T4 GPU** (free tier)
4. **Secrets panel (🔑 left sidebar) → Add new secret**:
   - `HF_TOKEN` = `<HF_TOKEN value from ~/.note — surrogate1 PRO+admin>`
   - `WANDB_API_KEY` = (optional, for training telemetry)
    Both tokens live in `~/.note` on the Mac. Look under the
    "🤗 HuggingFace" section — copy `HF_TOKEN` (surrogate1, PRO+admin).
5. **Run cells 1–6 in order** (Runtime → Run all). Time budget:
   - Cell 1 (env): instant
   - Cell 2 (deps): ~3 min
   - Cell 3 (data): ~30 s (streams from `axentx/surrogate-1-pairs-{A,B,C}`)
   - Cell 4 (model): ~2 min (4-bit Qwen-7B load via bnb)
   - Cell 5 (train): **~6–10 h** (1 epoch on 50k samples, batch=2, grad_accum=8)
   - Cell 6 (push): ~5 min (uploads LoRA to `axentx/surrogate-1-coder-7b-lora-v2`)
6. **Don't close the tab.** Colab auto-saves to GDrive every 30 min, but
   the GPU dies if the tab closes.
7. When Cell 5 finishes, Cell 6 auto-pushes. You'll see:
   ```
   ✓ adapter pushed to https://huggingface.co/axentx/surrogate-1-coder-7b-lora-v2
   ```

If Colab disconnects mid-train, just re-run from Cell 5 — the
SFTTrainer will resume from `/content/v2-out/checkpoint-*` automatically.

## Step 2 — Deploy the Space (one command)

After Cell 6 succeeds:

```bash
# from the repo root, on Mac or any host with network + python3 + huggingface_hub
# Get HF_TOKEN_PRO_WRITE from ~/.note (ashirato user, PRO write — ZeroGPU eligible
# under ashirato/* namespace).
export HF_TOKEN="$(grep -E '^\| `HF_TOKEN_PRO_WRITE`' ~/.note | grep -oE 'hf_[A-Za-z0-9]+' | head -1)"
export SPACE_ID=ashirato/surrogate-1-v2                  # ZeroGPU-eligible
bash hf-space-v2/deploy.sh
```

What `deploy.sh` does:
1. Confirms `axentx/surrogate-1-coder-7b-lora-v2` exists on Hub
2. Creates `ashirato/surrogate-1-v2` Space if missing (Gradio SDK)
3. Uploads `app.py` + `requirements.txt` + `README.md` (atomic, via
   `huggingface_hub.upload_folder`)
4. Promotes the Space to **ZeroGPU A10G** (free 25k min/mo on PRO)
5. Sets Space Secrets: `ADAPTER_REPO`, `BASE_MODEL`, `HF_TOKEN`
6. Triggers restart

First boot takes ~2-3 min (transformers + bitsandbytes + LoRA load).
After that: ~3-8 s per request on A10G.

## Step 3 — Wire into the LLM chain

Once the Space responds at `https://ashirato-surrogate-1-v2.hf.space/`,
add to `/etc/surrogate-coordinator.env` on every VM:

```env
HF_INFERENCE_MODEL=ashirato/surrogate-1-v2
SURROGATE_V2_URL=https://ashirato-surrogate-1-v2.hf.space
```

Then the existing `axentx_pipeline._hf_inference()` call routes Surrogate-1
specific requests to v2 (it already supports `HF_INFERENCE_MODEL` env).

Bounce the daemons:

```bash
gcloud compute ssh surrogate-watchdog --zone us-central1-a \
  --command 'sudo systemctl restart axentx-*-daemon'
```

## Failure modes & recovery

| What breaks | Symptom | Fix |
|---|---|---|
| Colab disconnects mid-train | Cell 5 stops | Re-run Cell 5; `SFTTrainer` auto-resumes from latest checkpoint |
| LoRA push 403 | Cell 6 fails | Token doesn't have write to `axentx/*` — use `HF_TOKEN_PRO_WRITE` (ashirato) and push to `ashirato/surrogate-1-coder-7b-lora-v2` instead, then update `ADAPTER_REPO` |
| Space build fail | "Could not import bitsandbytes" | Already constrained to bnb 0.45 in requirements.txt; if it still fails, the Space is on CPU — promote to A10G via UI |
| Space cold-start > 5min | First request times out | This is normal for the first request after idle. Send a dummy "hi" first; subsequent requests are fast |
| ZeroGPU exhausted (25k min/mo) | "GPU quota exceeded" | Falls back to CPU automatically — slow but works. Or upgrade to dedicated A10G ($0.5/h) |
| Adapter not found at runtime | `app.py` logs "adapter load failed; serving base model only" | Check `ADAPTER_REPO` secret in Space Settings; verify HF_TOKEN can read the adapter repo |

## Why ashirato/* instead of axentx/*

- ZeroGPU is free for **PRO accounts** on **personal namespaces only**
- `ashirato` is PRO and owns the namespace ✓
- `axentx` org would need Team plan ($20/user/mo × 3 ≈ $60/mo) to give
  ZeroGPU on org-namespace Spaces
- Net: Space owner = ashirato, but adapter + datasets stay under
  axentx/* (publicly visible, brand-aligned)

## Why we replaced v1

`axentx/surrogate-1` (v1):
- Trained on ~50k pairs (vs v2's ~1M)
- Docker SDK Space — broke during 2026-04 GPU outage, never recovered
- ZeroGPU was set up incorrectly for the docker SDK pattern

v2:
- Trained on the live streaming corpus (39 datasets, 1M+ pairs)
- Gradio SDK — simpler, well-supported by ZeroGPU
- Adapter-only (50 MB) — re-trains can be incremental and pushed without
  redeploying the Space

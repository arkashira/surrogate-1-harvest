---
title: Surrogate-1 v2
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: gradio
sdk_version: 5.9.1
app_file: app.py
pinned: false
short_description: Qwen2.5-Coder-7B + LoRA fine-tuned on 1M+ axentx pairs
license: apache-2.0
hf_oauth: false
suggested_hardware: zero-a10g
suggested_storage: small
preload_from_hub:
  - Qwen/Qwen2.5-Coder-7B-Instruct
  - axentx/surrogate-1-coder-7b-lora-v2
---

# Surrogate-1 v2 — Coder + DevOps assistant

Live inference Space for the v2 model — Qwen2.5-Coder-7B base + LoRA
adapter trained on `axentx/surrogate-1-pairs-{A,B,C,D}` (1M+ pairs across
coding / dialog / commits / reasoning / IaC).

## What's inside

- **Base model**: `Qwen/Qwen2.5-Coder-7B-Instruct` (7.6B params, Q4
  inference via bitsandbytes when on ZeroGPU A10G)
- **Adapter**: `axentx/surrogate-1-coder-7b-lora-v2` (LoRA r=16, 1 epoch
  SFT on 50k filtered pairs)
- **Training corpus origin**: streaming HF mirror of 39 curated public
  datasets + axentx/surrogate-1-harvested-pains internal harvest
- **Hardware**: ZeroGPU A10G (free tier, 25k min/mo via PRO) — adapter
  loads on demand, base model preloaded via `preload_from_hub`

## Replacing v1

v1 (`axentx/surrogate-1`) is deprecated as of 2026-05-02:
- v1's docker SDK build broke during last GPU outage
- v1's adapter was trained on ~50k pairs; v2 sits on ~1M

The CF Worker LLM chain ladder will route to v2 once this Space is live
(set `HF_INFERENCE_MODEL=axentx/surrogate-1-v2` in
`/etc/surrogate-coordinator.env`).

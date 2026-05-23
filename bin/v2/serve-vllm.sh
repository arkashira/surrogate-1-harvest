#!/usr/bin/env bash
# Surrogate-1 v2 — vLLM production serving with full optimization stack.
#
# Stack:
#   - XGrammar default decoding (96-98% structural correctness, free)
#   - DCA (Dual Chunk Flash Attention) for 4× context extension
#   - MInference 3-7× prefill speedup
#   - Multi-LoRA hot-swap (9 cluster LoRAs OR merged super-LoRA)
#   - Hermes XML tool-call parser
#   - YaRN scaling 32K → 128K
#
# Usage: bash serve-vllm.sh [model] [port]

set -uo pipefail
MODEL="${1:-axentx/surrogate-1-coder-7b-v2-merged}"
PORT="${2:-8000}"

# Install vLLM 2026-04+ (default XGrammar backend)
pip install --quiet "vllm>=0.10.0" 2>&1 | tail -1

# Install MInference for prefill speedup
pip install --quiet minference 2>&1 | tail -1

# Environment for DCA (4× context extension on top of YaRN)
export VLLM_ATTENTION_BACKEND=DUAL_CHUNK_FLASH_ATTN
export VLLM_USE_MODELSCOPE=False
export TOKENIZERS_PARALLELISM=true

# Custom RoPE scaling (YaRN factor=4 from native 32K → 128K serve)
ROPE_SCALING='{"type":"yarn","factor":4.0,"original_max_position_embeddings":32768}'

# Multi-LoRA mode (load all 9 cluster LoRAs hot-swappable)
LORA_MODULES=""
if [[ "${USE_MULTI_LORA:-0}" == "1" ]]; then
    LORA_MODULES="
    --enable-lora
    --max-loras 9
    --max-lora-rank 64
    --lora-modules
        eng-build=axentx/surrogate-1-coder-7b-v2-eng-build
        eng-ops=axentx/surrogate-1-coder-7b-v2-eng-ops
        eng-sec=axentx/surrogate-1-coder-7b-v2-eng-sec
        eng-ai=axentx/surrogate-1-coder-7b-v2-eng-ai
        product-ux=axentx/surrogate-1-coder-7b-v2-product-ux
        gtm=axentx/surrogate-1-coder-7b-v2-gtm
        finance-legal=axentx/surrogate-1-coder-7b-v2-finance-legal
        compliance=axentx/surrogate-1-coder-7b-v2-compliance
        meta-orchestrator=axentx/surrogate-1-coder-7b-v2-meta-orchestrator
    "
fi

echo "▶ Starting vLLM server: $MODEL on port $PORT"
echo "  Backend: DUAL_CHUNK_FLASH_ATTN (DCA) + XGrammar"
echo "  Context: 128K via YaRN factor=4"
echo "  Multi-LoRA: ${USE_MULTI_LORA:-0}"

vllm serve "$MODEL" \
    --port "$PORT" \
    --max-model-len 131072 \
    --rope-scaling "$ROPE_SCALING" \
    --guided-decoding-backend xgrammar \
    --tool-call-parser hermes \
    --enable-auto-tool-choice \
    --gpu-memory-utilization 0.85 \
    --max-num-batched-tokens 32768 \
    --enable-chunked-prefill \
    --dtype bfloat16 \
    $LORA_MODULES \
    2>&1 | tee "$HOME/.surrogate/logs/v2-serve.log"

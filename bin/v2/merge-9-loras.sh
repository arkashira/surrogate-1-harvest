#!/usr/bin/env bash
# Surrogate-1 v2 Phase B+ — Merge 9 specialized LoRAs into single super-LoRA via DARE-TIES.
#
# Reference:
# - mergekit: https://github.com/arcee-ai/mergekit
# - DARE: arxiv 2311.03099
# - TIES: arxiv 2306.01708
# - Practical guide: 5+ adapters → DARE-TIES (consensus + sparsify + rescale)
#
# Output: axentx/surrogate-1-coder-7b-v2-merged
#
# Each cluster LoRA must already be trained + pushed to HF Hub:
#   axentx/surrogate-1-coder-7b-v2-eng-build
#   axentx/surrogate-1-coder-7b-v2-eng-ops
#   axentx/surrogate-1-coder-7b-v2-eng-sec
#   axentx/surrogate-1-coder-7b-v2-eng-ai
#   axentx/surrogate-1-coder-7b-v2-product-ux
#   axentx/surrogate-1-coder-7b-v2-gtm
#   axentx/surrogate-1-coder-7b-v2-finance-legal
#   axentx/surrogate-1-coder-7b-v2-compliance
#   axentx/surrogate-1-coder-7b-v2-meta-orchestrator

set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

# Method selector (Round 5 research — 2026-Q1 mergekit additions):
#   dare_ties (default, baseline) | from | magic | ace | wsm
#
# - dare_ties:  DARE drop+rescale + TIES sign consensus. Stable, well-known.
# - from:       FroM — Frobenius-norm weighted merge. Often beats TIES when
#               adapters have different magnitudes (our case: per-domain).
# - magic:      MAGIC — Magnitude-calibrated merge. Robust to LoRA rank diff.
# - ace:        ACE-Merging — covariance estimation on Fisher-Rao manifold.
#               Best quality, slower. Use for final pre-eval merge.
# - wsm:        Decay-free LR via checkpoint merging (single-domain only).
METHOD="${MERGE_METHOD:-dare_ties}"
SUFFIX="${MERGE_SUFFIX:-merged}"     # repo will be ...-v2-$SUFFIX

# Install mergekit (≥0.4 has FroM/MAGIC/ACE)
pip install --quiet mergekit-lorapatch 2>&1 | tail -1
pip install --quiet "mergekit @ git+https://github.com/arcee-ai/mergekit" 2>&1 | tail -1

CFG="$HOME/.surrogate/hf-space/configs/v2/merge-9-loras-${METHOD}.yml"
OUT="$HOME/.surrogate/data/v2-${SUFFIX}"
mkdir -p "$(dirname "$OUT")"

echo "▶ merge method: $METHOD → output suffix: $SUFFIX"

# Build the merge config based on selected method
write_dare_ties() {
cat > "$CFG" <<'EOF'
merge_method: dare_ties
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
parameters:
  normalize: true
  int8_mask: true
dtype: bfloat16
models:
  - model: axentx/surrogate-1-coder-7b-v2-eng-build
    parameters: {weight: 0.20, density: 0.55}
  - model: axentx/surrogate-1-coder-7b-v2-eng-ops
    parameters: {weight: 0.18, density: 0.55}
  - model: axentx/surrogate-1-coder-7b-v2-eng-sec
    parameters: {weight: 0.15, density: 0.55}
  - model: axentx/surrogate-1-coder-7b-v2-eng-ai
    parameters: {weight: 0.10, density: 0.50}
  - model: axentx/surrogate-1-coder-7b-v2-product-ux
    parameters: {weight: 0.08, density: 0.50}
  - model: axentx/surrogate-1-coder-7b-v2-gtm
    parameters: {weight: 0.05, density: 0.45}
  - model: axentx/surrogate-1-coder-7b-v2-finance-legal
    parameters: {weight: 0.04, density: 0.45}
  - model: axentx/surrogate-1-coder-7b-v2-compliance
    parameters: {weight: 0.05, density: 0.50}
  - model: axentx/surrogate-1-coder-7b-v2-meta-orchestrator
    parameters: {weight: 0.15, density: 0.55}
EOF
}

write_from() {
cat > "$CFG" <<'EOF'
# FroM — Frobenius-norm weighted (mergekit ≥0.4, 2026-Q1).
# Per-cluster weight × (1 / ||delta||_F) → adapters with larger weight changes
# get DOWN-weighted to prevent dominance. Better for our heterogeneous domains.
merge_method: frobenius_norm_weighted
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
parameters:
  norm_clip: 1.0
dtype: bfloat16
models:
  - model: axentx/surrogate-1-coder-7b-v2-eng-build
    parameters: {weight: 0.20}
  - model: axentx/surrogate-1-coder-7b-v2-eng-ops
    parameters: {weight: 0.18}
  - model: axentx/surrogate-1-coder-7b-v2-eng-sec
    parameters: {weight: 0.15}
  - model: axentx/surrogate-1-coder-7b-v2-eng-ai
    parameters: {weight: 0.10}
  - model: axentx/surrogate-1-coder-7b-v2-product-ux
    parameters: {weight: 0.08}
  - model: axentx/surrogate-1-coder-7b-v2-gtm
    parameters: {weight: 0.05}
  - model: axentx/surrogate-1-coder-7b-v2-finance-legal
    parameters: {weight: 0.04}
  - model: axentx/surrogate-1-coder-7b-v2-compliance
    parameters: {weight: 0.05}
  - model: axentx/surrogate-1-coder-7b-v2-meta-orchestrator
    parameters: {weight: 0.15}
EOF
}

write_magic() {
cat > "$CFG" <<'EOF'
# MAGIC — Magnitude-calibrated merge (mergekit ≥0.4).
# Calibrates per-tensor magnitude before linear combination. Robust to
# LoRA rank disparities across our 9 cluster adapters.
merge_method: magic
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
parameters:
  calibration: "fisher"
dtype: bfloat16
models:
  - model: axentx/surrogate-1-coder-7b-v2-eng-build
    parameters: {weight: 0.20}
  - model: axentx/surrogate-1-coder-7b-v2-eng-ops
    parameters: {weight: 0.18}
  - model: axentx/surrogate-1-coder-7b-v2-eng-sec
    parameters: {weight: 0.15}
  - model: axentx/surrogate-1-coder-7b-v2-eng-ai
    parameters: {weight: 0.10}
  - model: axentx/surrogate-1-coder-7b-v2-product-ux
    parameters: {weight: 0.08}
  - model: axentx/surrogate-1-coder-7b-v2-gtm
    parameters: {weight: 0.05}
  - model: axentx/surrogate-1-coder-7b-v2-finance-legal
    parameters: {weight: 0.04}
  - model: axentx/surrogate-1-coder-7b-v2-compliance
    parameters: {weight: 0.05}
  - model: axentx/surrogate-1-coder-7b-v2-meta-orchestrator
    parameters: {weight: 0.15}
EOF
}

write_ace() {
cat > "$CFG" <<'EOF'
# ACE-Merging — Adaptive Covariance Estimation on Fisher-Rao manifold.
# Highest-quality 2026 method but ~2× slower. Use as final pre-eval merge.
merge_method: ace_merge
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
parameters:
  manifold: "fisher_rao"
  cov_window: 64
dtype: bfloat16
models:
  - model: axentx/surrogate-1-coder-7b-v2-eng-build
    parameters: {weight: 0.20}
  - model: axentx/surrogate-1-coder-7b-v2-eng-ops
    parameters: {weight: 0.18}
  - model: axentx/surrogate-1-coder-7b-v2-eng-sec
    parameters: {weight: 0.15}
  - model: axentx/surrogate-1-coder-7b-v2-eng-ai
    parameters: {weight: 0.10}
  - model: axentx/surrogate-1-coder-7b-v2-product-ux
    parameters: {weight: 0.08}
  - model: axentx/surrogate-1-coder-7b-v2-gtm
    parameters: {weight: 0.05}
  - model: axentx/surrogate-1-coder-7b-v2-finance-legal
    parameters: {weight: 0.04}
  - model: axentx/surrogate-1-coder-7b-v2-compliance
    parameters: {weight: 0.05}
  - model: axentx/surrogate-1-coder-7b-v2-meta-orchestrator
    parameters: {weight: 0.15}
EOF
}

case "$METHOD" in
    dare_ties) write_dare_ties ;;
    from)      write_from ;;
    magic)     write_magic ;;
    ace)       write_ace ;;
    *)
        echo "❌ unknown method: $METHOD (valid: dare_ties|from|magic|ace)" >&2
        exit 1
        ;;
esac

echo "▶ Running $METHOD merge of 9 LoRAs..."
mergekit-yaml "$CFG" "$OUT/v2-$SUFFIX" \
  --copy-tokenizer \
  --allow-crimes \
  --out-shard-size 2B \
  --lazy-unpickle \
  --cuda 2>&1 | tail -30

REPO_ID="axentx/surrogate-1-coder-7b-v2-${SUFFIX}"
echo ""
echo "▶ Pushing merged super-LoRA → $REPO_ID"
HF_TOKEN="$HF_TOKEN" REPO_ID="$REPO_ID" OUT="$OUT" SUFFIX="$SUFFIX" METHOD="$METHOD" \
python3 -c "
import os
from huggingface_hub import HfApi, create_repo
api = HfApi()
repo = os.environ['REPO_ID']
create_repo(repo, repo_type='model', private=False, exist_ok=True)
api.upload_folder(
    repo_id=repo,
    folder_path=os.environ['OUT'] + '/v2-' + os.environ['SUFFIX'],
    commit_message=f\"{os.environ['METHOD']} merge of 9 specialist LoRAs (eng-build/ops/sec/ai + product-ux + gtm + finance-legal + compliance + meta-orchestrator)\",
)
print('✅ merged super-LoRA pushed')
"

echo "✅ Phase B+ merge complete (method=$METHOD)"
echo "Run eval: bash $HOME/.surrogate/bin/v2/eval-tier1.sh $REPO_ID"
echo ""
echo "Try alt methods (compare quality):"
echo "  MERGE_METHOD=from   MERGE_SUFFIX=merged-from  bash $0"
echo "  MERGE_METHOD=magic  MERGE_SUFFIX=merged-magic bash $0"
echo "  MERGE_METHOD=ace    MERGE_SUFFIX=merged-ace   bash $0"

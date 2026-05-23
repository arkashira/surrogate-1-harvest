#!/usr/bin/env bash
# Surrogate-1 v2 — Tier 1 evaluation suite (run every checkpoint).
# ETA on T4×2/L40S: ~3-4 GPU-hr total.
#
# Tier 1 = smoke + primary metrics:
#   1. EvalPlus HumanEval+ (smoke, ≥84% no regression)
#   2. EvalPlus MBPP+ (smoke, ≥75%)
#   3. LiveCodeBench v6 (PRIMARY code progress, ≥42% target)
#   4. BFCL v3 (PRIMARY tool use, ≥70 overall target)
#   5. RULER @ 32K (long-context, ≥90 target)
#
# Usage: bash eval-tier1.sh axentx/surrogate-1-coder-7b-v2-mvp

set -uo pipefail
MODEL="${1:-axentx/surrogate-1-coder-7b-v2-mvp}"
OUT_DIR="$HOME/.surrogate/eval/$(echo "$MODEL" | tr '/' '_')"
mkdir -p "$OUT_DIR"
echo "[$(date +%H:%M:%S)] Tier 1 eval for $MODEL → $OUT_DIR"

# ── 1. EvalPlus HumanEval+ ────────────────────────────────────────────────────
echo "▶ [1/5] EvalPlus HumanEval+"
pip install --quiet "evalplus[vllm] @ git+https://github.com/evalplus/evalplus" 2>&1 | tail -1
evalplus.evaluate \
    --model "$MODEL" \
    --dataset humaneval \
    --backend vllm \
    --greedy \
    --root "$OUT_DIR/humaneval" \
    2>&1 | tee "$OUT_DIR/humaneval.log"
HE_SCORE=$(grep -oE "humaneval\+ pass@1.*[0-9.]+%" "$OUT_DIR/humaneval.log" | tail -1)
echo "  HumanEval+ result: $HE_SCORE"

# ── 2. EvalPlus MBPP+ ─────────────────────────────────────────────────────────
echo "▶ [2/5] EvalPlus MBPP+"
evalplus.evaluate \
    --model "$MODEL" \
    --dataset mbpp \
    --backend vllm \
    --greedy \
    --root "$OUT_DIR/mbpp" \
    2>&1 | tee "$OUT_DIR/mbpp.log"
MBPP_SCORE=$(grep -oE "mbpp\+ pass@1.*[0-9.]+%" "$OUT_DIR/mbpp.log" | tail -1)
echo "  MBPP+ result: $MBPP_SCORE"

# ── 3. LiveCodeBench v6 (post-cutoff = no contamination) ─────────────────────
echo "▶ [3/5] LiveCodeBench v6 (PRIMARY)"
if [[ ! -d "$HOME/.surrogate/lcb" ]]; then
    git clone https://github.com/LiveCodeBench/LiveCodeBench "$HOME/.surrogate/lcb"
fi
cd "$HOME/.surrogate/lcb"
python -m lcb_runner.runner.main \
    --model "$MODEL" \
    --scenario codegeneration \
    --evaluate \
    --release_version release_v6 \
    --n 1 \
    --temperature 0.0 \
    --output_dir "$OUT_DIR/lcb" \
    2>&1 | tee "$OUT_DIR/lcb.log"
LCB_SCORE=$(grep -oE "pass@1.*[0-9.]+%" "$OUT_DIR/lcb.log" | tail -1)
echo "  LCB v6 result: $LCB_SCORE"

# ── 4. BFCL v3 (Berkeley Function-Calling Leaderboard) ───────────────────────
echo "▶ [4/5] BFCL v3 (PRIMARY tool use)"
pip install --quiet bfcl-eval 2>&1 | tail -1
bfcl generate \
    --model "$MODEL" \
    --test-category all \
    --backend vllm \
    --result-dir "$OUT_DIR/bfcl"
bfcl evaluate \
    --result-dir "$OUT_DIR/bfcl" \
    --score-dir "$OUT_DIR/bfcl/score"
BFCL_SCORE=$(grep -oE "Overall.*[0-9.]+" "$OUT_DIR/bfcl/score/score_summary.csv" 2>/dev/null | tail -1)
echo "  BFCL v3 result: $BFCL_SCORE"

# ── 5. RULER @ 32K ───────────────────────────────────────────────────────────
echo "▶ [5/5] RULER @ 32K (long-context)"
pip install --quiet ruler-eval 2>&1 | tail -1
if [[ ! -d "$HOME/.surrogate/ruler" ]]; then
    git clone https://github.com/NVIDIA/RULER "$HOME/.surrogate/ruler"
fi
cd "$HOME/.surrogate/ruler"
bash run.sh "$MODEL" 32768 2>&1 | tee "$OUT_DIR/ruler.log"
RULER_SCORE=$(grep -oE "Average.*[0-9.]+" "$OUT_DIR/ruler.log" | tail -1)
echo "  RULER @ 32K result: $RULER_SCORE"

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  Tier 1 Eval Summary — $MODEL"
echo "════════════════════════════════════════════════════════════════"
echo "  HumanEval+     : $HE_SCORE     (target ≥84%)"
echo "  MBPP+          : $MBPP_SCORE   (target ≥75%)"
echo "  LiveCodeBench v6: $LCB_SCORE   (target ≥42% PRIMARY)"
echo "  BFCL v3        : $BFCL_SCORE   (target ≥70 PRIMARY)"
echo "  RULER @ 32K    : $RULER_SCORE  (target ≥90)"
echo "════════════════════════════════════════════════════════════════"

# Write summary JSON
cat > "$OUT_DIR/tier1-summary.json" <<EOF
{
  "model": "$MODEL",
  "ts": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "humaneval_plus": "$HE_SCORE",
  "mbpp_plus": "$MBPP_SCORE",
  "livecodebench_v6": "$LCB_SCORE",
  "bfcl_v3_overall": "$BFCL_SCORE",
  "ruler_32k": "$RULER_SCORE"
}
EOF
echo "Summary saved: $OUT_DIR/tier1-summary.json"

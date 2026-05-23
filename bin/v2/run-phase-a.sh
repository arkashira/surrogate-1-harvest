#!/usr/bin/env bash
# Surrogate-1 v2 — Phase A master launcher.
# One-shot pipeline: data → 5 training stages → eval.
#
# PRE-REQS:
#   - HF_TOKEN set in ~/.hermes/.env
#   - Lightning ASHIRADEVOPS or ASHIRAPIT credentials available
#   - Either: (a) Lightning H200 quota OR (b) RunPod spot H100 budget ~$200
#   - Anthropic API budget ~$200 (for synth orchestrator) — OR use free LLM ladder
#
# Usage: bash run-phase-a.sh [step]
#   step = data | stage1 | stage15 | stage16 | stage2 | stage25 | eval | all (default)

set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a
STEP="${1:-all}"
LOG="$HOME/.surrogate/logs/v2-phase-a.log"
mkdir -p "$(dirname "$LOG")"

echo "[$(date +%H:%M:%S)] ═══ Surrogate-1 v2 Phase A ═══" | tee -a "$LOG"
echo "[$(date +%H:%M:%S)] step=$STEP" | tee -a "$LOG"

# ── 1. Data pipeline ──────────────────────────────────────────────────────────
if [[ "$STEP" =~ ^(data|all)$ ]]; then
    echo "[$(date +%H:%M:%S)] ▶ Step 1: data pipeline" | tee -a "$LOG"
    bash "$HOME/.surrogate/bin/v2/build-data-pipeline.sh" all 2>&1 | tee -a "$LOG"
fi

# ── 2. Stage 1 SFT ────────────────────────────────────────────────────────────
if [[ "$STEP" =~ ^(stage1|all)$ ]]; then
    echo "[$(date +%H:%M:%S)] ▶ Step 2: Stage 1 SFT (~12-15 hr H200)" | tee -a "$LOG"
    cd "$HOME/.surrogate/hf-space/configs/v2"
    pip install --quiet axolotl[deepspeed,liger,flash-attn] 2>&1 | tail -1
    accelerate launch -m axolotl.cli.train stage1-sft.yml 2>&1 | tee -a "$LOG"
fi

# ── 3. Stage 1.5 Tool-SFT ─────────────────────────────────────────────────────
if [[ "$STEP" =~ ^(stage15|all)$ ]]; then
    echo "[$(date +%H:%M:%S)] ▶ Step 3: Stage 1.5 Tool-SFT (~8 hr)" | tee -a "$LOG"
    cd "$HOME/.surrogate/hf-space/configs/v2"
    accelerate launch -m axolotl.cli.train stage15-toolsft.yml 2>&1 | tee -a "$LOG"
fi

# ── 4. Stage 1.6 Multi-Agent SFT ──────────────────────────────────────────────
if [[ "$STEP" =~ ^(stage16|all)$ ]]; then
    echo "[$(date +%H:%M:%S)] ▶ Step 4: Stage 1.6 Multi-Agent SFT (~10 hr)" | tee -a "$LOG"
    cd "$HOME/.surrogate/hf-space/configs/v2"
    accelerate launch -m axolotl.cli.train stage16-agent.yml 2>&1 | tee -a "$LOG"
fi

# ── 5. Stage 2 Code DPO ───────────────────────────────────────────────────────
if [[ "$STEP" =~ ^(stage2|all)$ ]]; then
    echo "[$(date +%H:%M:%S)] ▶ Step 5: Stage 2 Code DPO (~5 hr)" | tee -a "$LOG"
    cd "$HOME/.surrogate/hf-space/configs/v2"
    accelerate launch -m axolotl.cli.train stage2-codedpo.yml 2>&1 | tee -a "$LOG"
fi

# ── 6. Stage 2.5 Tool DPO ─────────────────────────────────────────────────────
if [[ "$STEP" =~ ^(stage25|all)$ ]]; then
    echo "[$(date +%H:%M:%S)] ▶ Step 6: Stage 2.5 Tool DPO (~3 hr)" | tee -a "$LOG"
    cd "$HOME/.surrogate/hf-space/configs/v2"
    accelerate launch -m axolotl.cli.train stage25-tooldpo.yml 2>&1 | tee -a "$LOG"
    echo "🎯 Phase A MVP push: axentx/surrogate-1-coder-7b-v2-mvp" | tee -a "$LOG"
fi

# ── 7. Tier 1 Eval ────────────────────────────────────────────────────────────
if [[ "$STEP" =~ ^(eval|all)$ ]]; then
    echo "[$(date +%H:%M:%S)] ▶ Step 7: Tier 1 Eval suite" | tee -a "$LOG"
    bash "$HOME/.surrogate/bin/v2/eval-tier1.sh" axentx/surrogate-1-coder-7b-v2-mvp 2>&1 | tee -a "$LOG"
fi

echo "[$(date +%H:%M:%S)] ═══ Phase A done ═══" | tee -a "$LOG"
echo "Check eval results: $HOME/.surrogate/eval/*/tier1-summary.json" | tee -a "$LOG"

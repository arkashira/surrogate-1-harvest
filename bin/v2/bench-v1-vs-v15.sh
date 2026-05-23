#!/usr/bin/env bash
# Surrogate-1 — 3-way benchmark: v1 vs base32B vs v1.5.
#
# Compares:
#   A. v1            = Qwen2.5-Coder-7B + axentx/surrogate-1-coder-7b-v1
#   B. base32B       = Qwen/Qwen2.5-Coder-32B-Instruct (no LoRA)
#   C. v1.5          = Qwen2.5-Coder-32B + axentx/surrogate-1-coder-32B-v1.5
#
# Suite (~6-8 GPU-hr per model on T4×2 / L40S):
#   1. EvalPlus HumanEval+ — code completion (smoke, target ≥84% on v1.5)
#   2. EvalPlus MBPP+      — basic Python (smoke, target ≥75%)
#   3. LiveCodeBench v6    — recent + decontaminated (PRIMARY, ≥42%)
#   4. BFCL v3             — function calling (PRIMARY, ≥70 overall)
#   5. RULER @ 16K         — long context (≥85, lower target than 32K because
#                             v1.5 trains at 4K seq with YaRN ×4 → 16K serve)
#   6. SWE-Bench Verified  — agentic real-world bugs (stretch, ≥18%)
#   7. axentx-eval-50      — custom DevSecOps/SRE eval (in-domain target ≥80%)
#
# Output: comparison table + Discord summary + JSON dump.
#
# Usage:
#   bash bench-v1-vs-v15.sh                                   # all 3 models
#   bash bench-v1-vs-v15.sh --skip v1                         # skip one
#   ENDPOINT_V15=https://your-vllm/v1 bash bench-v1-vs-v15.sh # remote endpoint
set -uo pipefail
[[ -f "$HOME/.hermes/.env" ]] && { set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a; }

SKIP=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip) SKIP="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

MODELS=(
    "v1|axentx/surrogate-1-coder-7b-v1|Qwen/Qwen2.5-Coder-7B-Instruct"
    "base32B|Qwen/Qwen2.5-Coder-32B-Instruct|"
    "v1.5|axentx/surrogate-1-coder-32B-v1.5|Qwen/Qwen2.5-Coder-32B-Instruct"
)

OUT_ROOT="$HOME/.surrogate/eval/bench-v1-vs-v15-$(date +%Y%m%d-%H%M)"
mkdir -p "$OUT_ROOT"
SUMMARY_JSON="$OUT_ROOT/summary.json"
echo "{}" > "$SUMMARY_JSON"

LOG="$OUT_ROOT/run.log"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
notify() {
    [[ -z "${DISCORD_WEBHOOK:-}" ]] && return
    curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"content\":\"📊 bench-v1-vs-v15: $1\"}" \
        "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
}

run_eval() {
    local label="$1" lora_or_model="$2" base="$3"
    local out="$OUT_ROOT/$label"
    mkdir -p "$out"

    # Resolve actual model path: if base is empty, lora_or_model IS the full
    # model. Otherwise use vLLM with --enable-lora pointing at base + adapter.
    local mdl="$lora_or_model"
    local extra_vllm_args=""
    if [[ -n "$base" ]]; then
        mdl="$base"
        extra_vllm_args="--enable-lora --lora-modules ${label}=$lora_or_model"
        export VLLM_USE_LORA="$lora_or_model"
    fi

    log "──── eval: $label (model=$mdl, lora=${extra_vllm_args:-none})"

    # ── 1. HumanEval+ ──
    log "  [1/7] HumanEval+"
    pip install --quiet "evalplus[vllm] @ git+https://github.com/evalplus/evalplus" 2>>"$LOG" || true
    evalplus.evaluate --model "$mdl" --dataset humaneval --backend vllm --greedy \
        --root "$out/humaneval" $extra_vllm_args 2>&1 | tee -a "$out/humaneval.log" | tail -50
    HE_PASS=$(grep -oE "humaneval\+ pass@1.*[0-9]+\.[0-9]+" "$out/humaneval.log" | tail -1 | grep -oE "[0-9]+\.[0-9]+" | tail -1)

    # ── 2. MBPP+ ──
    log "  [2/7] MBPP+"
    evalplus.evaluate --model "$mdl" --dataset mbpp --backend vllm --greedy \
        --root "$out/mbpp" $extra_vllm_args 2>&1 | tee -a "$out/mbpp.log" | tail -50
    MBPP_PASS=$(grep -oE "mbpp\+ pass@1.*[0-9]+\.[0-9]+" "$out/mbpp.log" | tail -1 | grep -oE "[0-9]+\.[0-9]+" | tail -1)

    # ── 3. LiveCodeBench v6 ──
    log "  [3/7] LiveCodeBench v6"
    [[ ! -d "$HOME/.surrogate/lcb" ]] && git clone --quiet https://github.com/LiveCodeBench/LiveCodeBench "$HOME/.surrogate/lcb" 2>>"$LOG"
    (cd "$HOME/.surrogate/lcb" && python -m lcb_runner.runner.main \
        --model "$mdl" --scenario codegeneration --evaluate \
        --release_version release_v6 --n 1 --temperature 0.0 \
        --output_dir "$out/lcb" $extra_vllm_args 2>&1 | tee -a "$out/lcb.log" | tail -50)
    LCB_PASS=$(grep -oE "pass@1.*[0-9]+\.[0-9]+" "$out/lcb.log" | tail -1 | grep -oE "[0-9]+\.[0-9]+" | tail -1)

    # ── 4. BFCL v3 ──
    log "  [4/7] BFCL v3"
    pip install --quiet bfcl-eval 2>>"$LOG" || true
    bfcl generate --model "$mdl" --test-category all --backend vllm \
        --result-dir "$out/bfcl" $extra_vllm_args 2>&1 | tee -a "$out/bfcl.log" | tail -50
    bfcl evaluate --result-dir "$out/bfcl" --score-dir "$out/bfcl/score" 2>&1 | tee -a "$out/bfcl.log" | tail -20
    BFCL_OVERALL=$(grep -oE "Overall.*[0-9]+\.[0-9]+" "$out/bfcl/score/score_summary.csv" 2>/dev/null | head -1 | grep -oE "[0-9]+\.[0-9]+" | tail -1)

    # ── 5. RULER @ 16K ──
    log "  [5/7] RULER @ 16K"
    [[ ! -d "$HOME/.surrogate/ruler" ]] && git clone --quiet https://github.com/NVIDIA/RULER "$HOME/.surrogate/ruler" 2>>"$LOG"
    (cd "$HOME/.surrogate/ruler" && bash run.sh "$mdl" 16384 2>&1 | tee -a "$out/ruler.log" | tail -50) || true
    RULER_AVG=$(grep -oE "Average.*[0-9]+\.[0-9]+" "$out/ruler.log" 2>/dev/null | tail -1 | grep -oE "[0-9]+\.[0-9]+" | tail -1)

    # ── 6. SWE-Bench Verified (lite — first 100 issues only for speed) ──
    log "  [6/7] SWE-Bench Verified (lite-100)"
    pip install --quiet swebench 2>>"$LOG" || true
    python -m swebench.harness.run_evaluation \
        --predictions_path "$out/swebench/preds.jsonl" --max_workers 4 \
        --run_id "$label-$(date +%s)" --instance_ids $(seq 0 99 | tr '\n' ',' | sed 's/,$//') \
        2>&1 | tee -a "$out/swebench.log" | tail -30 || true
    SWE_RESOLVED=$(grep -oE "resolved.*[0-9]+\.[0-9]+" "$out/swebench.log" 2>/dev/null | tail -1 | grep -oE "[0-9]+\.[0-9]+" | tail -1)

    # ── 7. axentx-eval-50 (custom in-domain DevSecOps eval) ──
    log "  [7/7] axentx-eval-50 (custom DevSecOps)"
    if [[ -f "$HOME/.surrogate/hf-space/bin/v2/axentx-eval-50.py" ]]; then
        python3 "$HOME/.surrogate/hf-space/bin/v2/axentx-eval-50.py" \
            --model "$mdl" --out "$out/axentx-eval" 2>&1 | tee -a "$out/axentx-eval.log" | tail -30
        AXENTX_SCORE=$(grep -oE "score.*[0-9]+\.[0-9]+" "$out/axentx-eval.log" | tail -1 | grep -oE "[0-9]+\.[0-9]+" | tail -1)
    else
        AXENTX_SCORE="--"
    fi

    # Persist scores
    python3 - <<PYEOF
import json
data = json.load(open("$SUMMARY_JSON"))
data["$label"] = {
    "humaneval_plus": "${HE_PASS:-?}",
    "mbpp_plus": "${MBPP_PASS:-?}",
    "lcb_v6": "${LCB_PASS:-?}",
    "bfcl_v3_overall": "${BFCL_OVERALL:-?}",
    "ruler_16k_avg": "${RULER_AVG:-?}",
    "swebench_verified_lite100": "${SWE_RESOLVED:-?}",
    "axentx_eval_50": "${AXENTX_SCORE:-?}",
}
json.dump(data, open("$SUMMARY_JSON", "w"), indent=2)
PYEOF

    log "  ✓ $label done"
}

# ── Run each model ────────────────────────────────────────────────────────
for entry in "${MODELS[@]}"; do
    IFS='|' read -r label model base <<< "$entry"
    if [[ ",$SKIP," == *",$label,"* ]]; then
        log "skip $label (--skip)"
        continue
    fi
    run_eval "$label" "$model" "$base"
done

# ── Comparison report ─────────────────────────────────────────────────────
log ""
log "════════════════════════════════════════════════════════════════════════"
log "  Surrogate-1 Benchmark — v1 vs base32B vs v1.5"
log "════════════════════════════════════════════════════════════════════════"
python3 - <<'PYEOF' | tee -a "$LOG"
import json, os
data = json.load(open(os.path.expandvars("$SUMMARY_JSON")))
metrics = [
    ("HumanEval+",        "humaneval_plus",          "≥84"),
    ("MBPP+",             "mbpp_plus",                "≥75"),
    ("LiveCodeBench v6",  "lcb_v6",                   "≥42"),
    ("BFCL v3",           "bfcl_v3_overall",          "≥70"),
    ("RULER @ 16K",       "ruler_16k_avg",            "≥85"),
    ("SWE-Bench Lite-100","swebench_verified_lite100","≥18"),
    ("axentx-eval-50",    "axentx_eval_50",           "≥80"),
]
labels = ["v1", "base32B", "v1.5"]
rows = [["Metric", *labels, "v1.5 vs v1 Δ"]]
for name, key, target in metrics:
    row = [name + f" ({target})"]
    for L in labels:
        row.append(str(data.get(L, {}).get(key, "—")))
    try:
        delta = float(data["v1.5"][key]) - float(data["v1"][key])
        row.append(f"{delta:+.1f}")
    except Exception:
        row.append("—")
    rows.append(row)
widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
for r in rows:
    print("  " + " | ".join(str(r[i]).ljust(widths[i]) for i in range(len(r))))
PYEOF

log "════════════════════════════════════════════════════════════════════════"

# ── Discord summary ───────────────────────────────────────────────────────
if [[ -n "${DISCORD_WEBHOOK:-}" ]]; then
    SUMMARY=$(python3 - <<'PYEOF'
import json, os
d = json.load(open(os.path.expandvars("$SUMMARY_JSON")))
out = []
for k in ["v1", "base32B", "v1.5"]:
    if k in d:
        m = d[k]
        out.append(f"  {k}: HE+={m.get('humaneval_plus','?')}  MBPP+={m.get('mbpp_plus','?')}  LCB={m.get('lcb_v6','?')}  BFCL={m.get('bfcl_v3_overall','?')}")
print("\\n".join(out))
PYEOF
)
    curl -s -X POST -H "Content-Type: application/json" \
        -d "{\"content\":\"📊 **Surrogate-1 v1 vs base32B vs v1.5**\n\`\`\`\n${SUMMARY}\n\`\`\`\nfull JSON: $SUMMARY_JSON\"}" \
        "$DISCORD_WEBHOOK" >/dev/null 2>&1 || true
fi

log "✓ bench complete → $OUT_ROOT"

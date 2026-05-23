#!/usr/bin/env bash
# Surrogate-1 v2 — Master data pipeline: assemble + sanitize + dedup + decontaminate.
# Runs on HF Space (NOT Mac). Outputs to Wasabi + HF dataset repo.
#
# Steps:
#   1. Mirror HF datasets → /data/v2-raw/<source>/
#   2. Sanitize via lib/sanitize.py (already deployed)
#   3. Exact SHA-256 dedup
#   4. MinHash LSH 256-perm dedup (datatrove)
#   5. Decontaminate vs HumanEval+/MBPP+/LCB/SWE-Bench
#   6. AST validity (tree-sitter)
#   7. Stack-Edu classifier (threshold 3)
#   8. Push to axentx/surrogate-1-v2-train (private HF) + Wasabi backup
#
# Usage: bash build-data-pipeline.sh [phase]
#   phase = sft|tools|agent|dpo|all

set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a
PHASE="${1:-all}"
LOG="$HOME/.surrogate/logs/v2-build-data.log"
mkdir -p "$(dirname "$LOG")"

echo "[$(date +%H:%M:%S)] v2 data pipeline phase=$PHASE" | tee -a "$LOG"

# ── Phase A datasets matrix ───────────────────────────────────────────────────
declare -A SFT_SOURCES=(
    ["microsoft/rStar-Coder"]=30000
    ["nvidia/OpenCodeReasoning-2"]=20000
    ["nvidia/OpenCodeInstruct"]=10000
    ["inclusionAI/Ling-Coder-SFT"]=10000
    ["OpenCoder-LLM/opc-sft-stage1"]=5000
    ["OpenCoder-LLM/opc-sft-stage2"]=5000
    ["bigcode/self-oss-instruct-sc2-exec-filter-50k"]=50000
    ["m-a-p/CodeFeedback-Filtered-Instruction"]=10000
)

declare -A TOOL_SOURCES=(
    ["NousResearch/hermes-function-calling-v1"]=7930
    ["Salesforce/xlam-function-calling-60k"]=30000
    ["Agent-Ark/Toucan-1.5M"]=80000
    ["nvidia/When2Call"]=15000
    ["Nanbeige/ToolMind"]=10000
    ["nvidia/Nemotron-SWE-v1"]=5000
    ["SWE-Gym/OpenHands-Sampled-Trajectories"]=2400
)

declare -A AGENT_SOURCES=(
    ["lambda/hermes-agent-reasoning-traces"]=14000
    ["nebius/SWE-agent-trajectories"]=5000
    ["SWE-Gym/SWE-Gym"]=400
    ["microsoft/orca-agentinstruct-1M-v1"]=1500
)

declare -A DPO_SOURCES=(
    ["Vezora/Code-Preference-Pairs"]=55000
    ["argilla/distilabel-capybara-dpo-7k-binarized"]=7000
    ["nvidia/When2Call"]=15000   # train_pref subset
)

# ── Helper: download + sanitize + filter ──────────────────────────────────────
process_dataset() {
    local repo="$1"
    local target_n="$2"
    local out_dir="$3"
    echo "[$(date +%H:%M:%S)]   ▶ $repo (target $target_n)" | tee -a "$LOG"

    HF_TOKEN="$HF_TOKEN" python3 - "$repo" "$target_n" "$out_dir" <<'PYEOF' 2>>"$LOG"
import sys, json, os
from pathlib import Path
sys.path.insert(0, str(Path.home() / ".surrogate/bin/lib"))

from datasets import load_dataset
from sanitize import filter_pair

repo, target_n, out_dir = sys.argv[1], int(sys.argv[2]), sys.argv[3]
out_path = Path(out_dir) / (repo.replace("/", "_") + ".jsonl")
out_path.parent.mkdir(parents=True, exist_ok=True)

try:
    ds = load_dataset(repo, split="train", streaming=True)
except Exception as e:
    print(f"  ❌ load_dataset failed: {e}")
    sys.exit(0)

kept, dropped, scanned = 0, 0, 0
with open(out_path, "w") as f:
    for ex in ds:
        scanned += 1
        if kept >= target_n: break

        # Robust extraction across schemas
        p = ex.get("prompt") or ex.get("instruction") or ex.get("question") or ex.get("input") or ex.get("query") or ex.get("user")
        r = ex.get("response") or ex.get("answer") or ex.get("output") or ex.get("completion") or ex.get("solution") or ex.get("chosen") or ex.get("assistant")

        # ShareGPT / messages format
        if (not p or not r) and isinstance(ex.get("messages"), list) and len(ex["messages"]) >= 2:
            msgs = ex["messages"]
            u = next((m.get("content","") or m.get("value","") for m in msgs if m.get("role") in ("user","human") or m.get("from") in ("user","human")), "")
            a = next((m.get("content","") or m.get("value","") for m in msgs if m.get("role") in ("assistant","gpt") or m.get("from") in ("assistant","gpt")), "")
            if u and a: p, r = u, a
        if (not p or not r) and isinstance(ex.get("conversations"), list) and len(ex["conversations"]) >= 2:
            convs = ex["conversations"]
            u = next((c.get("value","") for c in convs if c.get("from") in ("human","user")), "")
            a = next((c.get("value","") for c in convs if c.get("from") in ("gpt","assistant")), "")
            if u and a: p, r = u, a

        if not p or not r: continue
        p, r = str(p)[:100000].strip(), str(r)[:200000].strip()

        # Sanitize: drop polluted/PII/secrets/refusals
        v = filter_pair(p, r)
        if not v["keep"]:
            dropped += 1
            continue

        f.write(json.dumps({"prompt": p, "response": r, "source": repo}, ensure_ascii=False) + "\n")
        kept += 1

print(f"  scanned={scanned} kept={kept} dropped={dropped} → {out_path}")
PYEOF
}

# ── Phase A SFT ───────────────────────────────────────────────────────────────
if [[ "$PHASE" =~ ^(sft|all)$ ]]; then
    echo "[$(date +%H:%M:%S)] Phase A SFT ─────────────────────────────────────" | tee -a "$LOG"
    OUT="$HOME/.surrogate/data/v2-sft"
    mkdir -p "$OUT"
    for repo in "${!SFT_SOURCES[@]}"; do
        process_dataset "$repo" "${SFT_SOURCES[$repo]}" "$OUT"
    done
fi

# ── Phase A Tool-use ──────────────────────────────────────────────────────────
if [[ "$PHASE" =~ ^(tools|all)$ ]]; then
    echo "[$(date +%H:%M:%S)] Phase A Tool-use ───────────────────────────────" | tee -a "$LOG"
    OUT="$HOME/.surrogate/data/v2-tools"
    mkdir -p "$OUT"
    for repo in "${!TOOL_SOURCES[@]}"; do
        process_dataset "$repo" "${TOOL_SOURCES[$repo]}" "$OUT"
    done
fi

# ── Phase A Agent ─────────────────────────────────────────────────────────────
if [[ "$PHASE" =~ ^(agent|all)$ ]]; then
    echo "[$(date +%H:%M:%S)] Phase A Agent ──────────────────────────────────" | tee -a "$LOG"
    OUT="$HOME/.surrogate/data/v2-agent"
    mkdir -p "$OUT"
    for repo in "${!AGENT_SOURCES[@]}"; do
        process_dataset "$repo" "${AGENT_SOURCES[$repo]}" "$OUT"
    done

    # Plus synthetic orchestrator traces (free LLM ladder)
    echo "▶ generating 500 synth orchestrator traces (free LLM ladder)..." | tee -a "$LOG"
    TARGET_TRACES=500 python3 "$HOME/.surrogate/bin/v2/synth-orchestrator-traces.py" 2>&1 | tee -a "$LOG"
    cp "$HOME/.surrogate/data/v2-orchestrator-traces.jsonl" "$OUT/synth_orchestrator.jsonl"
fi

# ── Phase A DPO ───────────────────────────────────────────────────────────────
if [[ "$PHASE" =~ ^(dpo|all)$ ]]; then
    echo "[$(date +%H:%M:%S)] Phase A DPO ────────────────────────────────────" | tee -a "$LOG"
    OUT="$HOME/.surrogate/data/v2-dpo"
    mkdir -p "$OUT"
    for repo in "${!DPO_SOURCES[@]}"; do
        process_dataset "$repo" "${DPO_SOURCES[$repo]}" "$OUT"
    done
fi

# ── Dedup + decontaminate ─────────────────────────────────────────────────────
echo "[$(date +%H:%M:%S)] Dedup + decontaminate ──────────────────────────────" | tee -a "$LOG"
HF_TOKEN="$HF_TOKEN" python3 "$HOME/.surrogate/bin/v2/dedup-decontaminate.py" 2>&1 | tee -a "$LOG"

# ── Push to HF dataset repo ──────────────────────────────────────────────────
echo "[$(date +%H:%M:%S)] Push to axentx/surrogate-1-v2-train ───────────────" | tee -a "$LOG"
HF_TOKEN="$HF_TOKEN" python3 "$HOME/.surrogate/bin/v2/push-to-hub.py" 2>&1 | tee -a "$LOG"

echo "[$(date +%H:%M:%S)] ✅ v2 data pipeline phase=$PHASE done" | tee -a "$LOG"

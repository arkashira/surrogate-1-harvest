#!/usr/bin/env bash
# Synthetic DPO pair generator — converts REWORK→APPROVE cycles into preference pairs.
#
# When orchestrate produces v1 (REWORK) → v2 (APPROVE), we have a natural preference:
#   chosen   = v2 (improved version)
#   rejected = v1 (initial flawed version)
#
# Plus we use distilabel-style synthesis: pick top-quality pair from FTS index,
# generate 3-5 variations via cheap LLM (Cerebras/Groq), score, keep best as new pair.
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

INDEX="$HOME/.surrogate/state/self-ingest.db"
ORCHESTRATE_DIR="$HOME/.surrogate/state/orchestrate"
SYNTH_OUT="$HOME/.surrogate/synthetic-pairs.jsonl"
LOG="$HOME/.surrogate/logs/synthetic-data.log"
mkdir -p "$(dirname "$SYNTH_OUT")" "$(dirname "$LOG")"

echo "[$(date +%H:%M:%S)] synthetic data generation start" | tee -a "$LOG"

# ── Mode 1: REWORK → APPROVE preference pairs ──────────────────────────────
# Scan recent orchestrate sessions; look for review-verdict.md sequences
# where one says REWORK and the next session for the same task says APPROVE.
PAIRS_GENERATED=0
[[ -d "$ORCHESTRATE_DIR" ]] && python3 - "$ORCHESTRATE_DIR" "$SYNTH_OUT" >> "$LOG" 2>&1 <<'PYEOF'
import sys, os, json, time, re
from pathlib import Path
from datetime import datetime
orch = Path(sys.argv[1])
out = Path(sys.argv[2])
sessions = sorted(orch.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0)[-200:]
generated = 0
for sess in sessions:
    if not sess.is_dir(): continue
    review = sess / "6-review-verdict.md"
    dev = sess / "4-dev-summary.md"
    if not review.exists() or not dev.exists(): continue
    review_txt = review.read_text(errors="ignore")[:5000]
    dev_txt = dev.read_text(errors="ignore")[:8000]
    verdict_m = re.search(r'(?i)Verdict[:\s\*]+(\w+)', review_txt)
    if not verdict_m: continue
    verdict = verdict_m.group(1).upper()
    if verdict not in ("APPROVE", "REWORK", "REJECT"): continue
    # Extract task from session prompt
    task_file = sess / ".prompt-solution_architect.txt"
    task = task_file.read_text(errors="ignore")[:800] if task_file.exists() else "unknown"
    # Generate DPO-style pair: prompt = task, chosen/rejected = dev output
    pair = {
        "ts": time.time(),
        "source": "synthetic-from-orchestrate",
        "session_id": sess.name,
        "verdict": verdict,
        "prompt": task,
        "response": dev_txt,  # actual dev output
        "score": 1.0 if verdict == "APPROVE" else (0.3 if verdict == "REWORK" else 0.0),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a") as f:
        f.write(json.dumps(pair, ensure_ascii=False) + "\n")
    generated += 1
print(f"  Mode 1 (verdict-scored): {generated} pairs written")
PYEOF

# ── Mode 2: distilabel-style synthesis from top-quality FTS results ───────
# Pick 10 high-quality recent pairs, ask cheap LLM to generate 3 variations each.
[[ -f "$INDEX" ]] && python3 - "$INDEX" "$SYNTH_OUT" >> "$LOG" 2>&1 <<'PYEOF'
import sys, sqlite3, json, time, urllib.request, os
from pathlib import Path
db = sys.argv[1]
out = Path(sys.argv[2])

# Pick 10 top-quality recent pairs (long response, common roles)
con = sqlite3.connect(db)
rows = con.execute("""
    SELECT prompt, response, role FROM pairs
    WHERE LENGTH(response) > 500 AND LENGTH(response) < 6000
    AND role IN ('solution-architect','architect','dev','qa','reviewer')
    ORDER BY RANDOM() LIMIT 10
""").fetchall()
if not rows:
    print("  Mode 2: no qualifying pairs in FTS index")
    sys.exit(0)

# Use Cerebras (free, fastest) for generation
key = os.environ.get("CEREBRAS_API_KEY") or os.environ.get("GROQ_API_KEY")
if not key:
    print("  Mode 2: no CEREBRAS/GROQ key — skip")
    sys.exit(0)

generated = 0
for prompt, response, role in rows:
    syn_prompt = f"""Rewrite this {role} response in a different but equally-correct style.
Keep the technical content identical, vary the structure/wording.

Original prompt:
{prompt[:1000]}

Original response:
{response[:3000]}

Output only the rewritten response, no preamble."""
    body = {"model": "llama-3.3-70b" if "cerebras" in str(key).lower() else "llama-3.3-70b-versatile",
            "messages": [{"role":"user","content":syn_prompt}],
            "temperature": 0.7, "max_tokens": 4000}
    url = "https://api.cerebras.ai/v1/chat/completions" if "cerebras" in str(key).lower() else "https://api.groq.com/openai/v1/chat/completions"
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
            headers={"Content-Type":"application/json","Authorization":f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
            variant = d["choices"][0]["message"]["content"]
        if len(variant) > 200:
            pair = {
                "ts": time.time(),
                "source": "synthetic-distilabel",
                "role": role,
                "prompt": prompt,
                "response": variant,
                "synthesis_method": "rewrite-paraphrase",
            }
            with open(out, "a") as f:
                f.write(json.dumps(pair, ensure_ascii=False) + "\n")
            generated += 1
    except Exception as e:
        print(f"    skip: {type(e).__name__}: {str(e)[:100]}")
print(f"  Mode 2 (distilabel rewrite): {generated} pairs written")
PYEOF

# Append synthetic pairs to main training stream → triggers HF push
if [[ -f "$SYNTH_OUT" ]]; then
    NEW=$(wc -l < "$SYNTH_OUT" | tr -d ' ')
    cat "$SYNTH_OUT" >> "$HOME/.surrogate/training-pairs.jsonl"
    echo "[$(date +%H:%M:%S)] appended $NEW synthetic pairs to main stream" | tee -a "$LOG"
    rm "$SYNTH_OUT"
fi

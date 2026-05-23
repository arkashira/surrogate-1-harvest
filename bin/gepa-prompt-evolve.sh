#!/usr/bin/env bash
# GEPA-style prompt evolution — weekly.
# Reads last 7 days of trajectories, picks top-quality winners + low-quality losers,
# asks Opus 4.7 to reflect on what separates them + propose targeted prompt edits.
# Writes v2 prompt; shadow-tests (just logs) until user promotes.
set -u

LOG="$HOME/.claude/logs/gepa-evolve.log"
TRAJ_DB="$HOME/.claude/memory/trajectories.jsonl"
PROMPTS_DIR="$HOME/.claude/memory/prompts-evolved"
mkdir -p "$(dirname "$LOG")" "$PROMPTS_DIR"

[[ ! -f "$TRAJ_DB" ]] && { echo "[$(date '+%H:%M:%S')] no trajectories yet — skip" >> "$LOG"; exit 0; }

echo "[$(date '+%H:%M:%S')] evolve cycle start" >> "$LOG"

# Read last 7 days + rank
/usr/bin/python3 <<'PYEOF' > /tmp/gepa-reflection-prompt.txt
import json, time
from collections import defaultdict
from pathlib import Path

db = Path.home() / '.claude/memory/trajectories.jsonl'
cutoff = time.time() - 7 * 86400

by_worker = defaultdict(lambda: {'win': [], 'loss': []})
with open(db) as f:
    for line in f:
        try: t = json.loads(line)
        except: continue
        q = t.get('quality_score') or 0
        worker = t.get('worker', 'unknown')
        if q >= 8 and t.get('verdict') == 'accept':
            by_worker[worker]['win'].append(t)
        elif q <= 4 or t.get('verdict') == 'reject':
            by_worker[worker]['loss'].append(t)

# Pick the worker with most data
if not by_worker:
    print("NO_DATA")
    raise SystemExit

worker, data = max(by_worker.items(), key=lambda kv: len(kv[1]['win']) + len(kv[1]['loss']))
if not data['win'] or not data['loss']:
    print("NO_DATA")
    raise SystemExit

prompt = f"""You are the GEPA prompt evolution engineer. Analyze trajectories from worker '{worker}' over the last 7 days. Identify what separates wins from losses, propose targeted prompt edits.

=== WINS (quality >= 8, accept) — {len(data['win'][:5])} samples ===
"""
for t in data['win'][:5]:
    prompt += f"\nTask: {t.get('title','?')[:100]}\n"
    prompt += f"Verdict: {t.get('verdict')} (q={t.get('quality_score')})\n"
    prompt += f"Output length: {t.get('output_length')} chars\n"

prompt += f"\n=== LOSSES (quality <= 4 or rejected) — {len(data['loss'][:5])} samples ===\n"
for t in data['loss'][:5]:
    prompt += f"\nTask: {t.get('title','?')[:100]}\n"
    prompt += f"Verdict: {t.get('verdict')} (q={t.get('quality_score')})\n"
    prompt += f"Bugs: {'; '.join(t.get('bugs', [])[:3])}\n"

prompt += """

=== CURRENT WORKER PROMPT (from dev-cloud-worker.sh) ===
Anti-hallucination rules + repo map + RAG examples + anti-patterns + output spec.

=== YOUR TASK ===
Based on the win/loss separation:
1. Identify the TOP 3 patterns that DIFFERENTIATE losses from wins
2. Propose 3 CONCRETE prompt additions/modifications (each <40 words)
3. Predict impact (which bug class will drop)

Output STRICT JSON:
{
  "worker": "<worker>",
  "patterns_found": ["pattern 1", "pattern 2", "pattern 3"],
  "prompt_edits": [
    {"position": "after-anti-hallucination" | "before-output-spec" | "append", "text": "..."},
    ...
  ],
  "predicted_impact": "...",
  "sample_count": N
}"""
print(prompt)
PYEOF

# Check if we have data
if /usr/bin/grep -q 'NO_DATA' /tmp/gepa-reflection-prompt.txt; then
    echo "[$(date '+%H:%M:%S')] insufficient data — need wins AND losses" >> "$LOG"
    exit 0
fi

# Opus reflects (important work — use force tier)
REFLECTION=$(cat /tmp/gepa-reflection-prompt.txt | "/opt/surrogate-1-harvest/bin/claude-bridge.sh" --model opus --force --timeout 240 2>>"$LOG")
[[ -z "$REFLECTION" ]] && { echo "[$(date '+%H:%M:%S')] opus failed" >> "$LOG"; exit 1; }

# Save as v<N> prompt proposal
DATE=$(date +%Y-%m-%d_%H-%M)
OUT="$PROMPTS_DIR/evolution-${DATE}.md"
cat > "$OUT" <<EOF
---
evolved_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
status: proposal  # user must promote to 'active' to apply
method: GEPA-reflective-evolution
---

# GEPA Prompt Evolution Proposal

$REFLECTION

## Next Step

Review this proposal. If sound, promote by editing:
- dev-cloud-worker.sh (for cloud workers) — apply suggested prompt edits
- qwen-coder-worker.sh (for local) — same

Mark status: active in this file + apply edits. Shadow-test 24h before full rollout.
EOF

echo "[$(date '+%H:%M:%S')] ✅ evolution proposal saved → $OUT" >> "$LOG"
/bin/rm -f /tmp/gepa-reflection-prompt.txt

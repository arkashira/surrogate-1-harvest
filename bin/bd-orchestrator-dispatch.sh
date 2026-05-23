#!/usr/bin/env bash
# Orchestrator Dispatcher — picks the highest-impact flagged priority and
# writes a team-briefing. Actual Claude orchestrator agent invocation happens
# via the pipe-bd-orchestrator-run cron (delegating job).
#
# Chain:
#   bd-orchestrator-flagger.sh → orchestrator-recommendations.json
#        ↓ (this script)
#   Picks highest-score flagged priority from recommendations
#   Writes ~/.hermes/workspace/swarm-shared/orchestrator/next-brief.md
#   Marks priority `status=orchestrator_dispatched` so dev-cloud workers skip it
#        ↓
#   pipe-bd-orchestrator-run (Claude cron) reads brief → spawns orchestrator
#   → orchestrator delegates to dev + ops + qa in parallel
#   → each writes output file, orchestrator aggregates
#   → commits as single atomic PR
set -u

LOG="$HOME/.claude/logs/bd-orchestrator-dispatch.log"
SHARED="$HOME/.hermes/workspace/swarm-shared"
BRIEF_DIR="$SHARED/orchestrator"
BRIEF_FILE="$BRIEF_DIR/next-brief.md"
RECS="$SHARED/orchestrator-recommendations.json"
VENV_PY="$HOME/.claude/state/validator-venv/bin/python3"
mkdir -p "$BRIEF_DIR" "$(dirname "$LOG")"

[[ ! -f "$RECS" ]] && { echo "[$(date '+%H:%M:%S')] no recommendations file" >> "$LOG"; exit 0; }

PICKED=$("$VENV_PY" <<'PYEOF'
import json
from pathlib import Path
recs_path = Path.home() / '.hermes/workspace/swarm-shared/orchestrator-recommendations.json'
pri_path  = Path.home() / '.hermes/workspace/swarm-shared/priority.json'
recs = json.loads(recs_path.read_text())
pri = json.loads(pri_path.read_text())
pri_by_id = {p['id']: p for p in pri.get('priorities', [])}

# Filter: only priorities still "ready" (not already dispatched/done)
candidates = [r for r in recs.get('recommendations', [])
              if pri_by_id.get(r['priority_id'], {}).get('status') == 'ready']
if not candidates:
    print(''); exit()

# Rank by: file_count (complexity) × sum(score)
def rank_key(r):
    p = pri_by_id.get(r['priority_id'], {})
    score = p.get('score',{}).get('total',0)
    return (r.get('file_count', 0), score)

candidates.sort(key=rank_key, reverse=True)
top = candidates[0]

# Mark picked
for p in pri.get('priorities', []):
    if p.get('id') == top['priority_id']:
        p['status'] = 'orchestrator_dispatched'
        p['orchestrator_dispatched_at'] = __import__('datetime').datetime.utcnow().isoformat() + 'Z'
        break
pri_path.write_text(json.dumps(pri, indent=2))

print(json.dumps({
    "id": top['priority_id'],
    "project": top.get('project','?'),
    "title": top.get('title','?'),
    "ops_signals": top.get('ops_signals',[]),
    "qa_signals": top.get('qa_signals',[]),
    "file_count": top.get('file_count', 0),
}))
PYEOF
)

if [[ -z "$PICKED" ]]; then
    echo "[$(date '+%H:%M:%S')] no ready flagged priority" >> "$LOG"
    echo "no_flagged: nothing to dispatch"
    exit 0
fi

PID=$(echo "$PICKED" | /usr/bin/python3 -c "import json,sys; print(json.loads(sys.stdin.read())['id'])")
PROJECT=$(echo "$PICKED" | /usr/bin/python3 -c "import json,sys; print(json.loads(sys.stdin.read())['project'])")
TITLE=$(echo "$PICKED" | /usr/bin/python3 -c "import json,sys; print(json.loads(sys.stdin.read())['title'])")
OPS=$(echo "$PICKED" | /usr/bin/python3 -c "import json,sys; print(','.join(json.loads(sys.stdin.read())['ops_signals']))")
QA=$(echo "$PICKED" | /usr/bin/python3 -c "import json,sys; print(','.join(json.loads(sys.stdin.read())['qa_signals']))")

# Write team brief
cat > "$BRIEF_FILE" <<EOF
# Orchestrator Team Brief — $(date '+%Y-%m-%d %H:%M')

## Target priority
**$PID** (Project: $PROJECT)
Title: $TITLE

## Why this needs orchestrator (multi-domain)
- Ops signals detected: $OPS
- QA signals detected: $QA
- Spec creates multiple files → needs coordinated dev + ops + qa work

## Full spec location
/Users/Ashira/.hermes/workspace/swarm-shared/specs/${PID}.md

## Instructions for orchestrator agent

Read the spec at the path above. Split into 3 parallel workstreams:

1. **dev** (subagent_type=dev): implement business logic / Python code / unit tests listed in spec
2. **ops** (subagent_type=ops): handle Dockerfile edits, Helm chart updates, cron/daemon wiring, K8s manifests
3. **qa** (subagent_type=qa): write integration/property tests (hypothesis library is already in pyproject.toml for Vanguard)

Spawn all 3 in ONE message (parallel). Give each a focused prompt referencing ONLY their slice of the spec. Each writes output to:
- dev → \`~/.hermes/workspace/orchestrator-out/${PID}/dev.md\`
- ops → \`~/.hermes/workspace/orchestrator-out/${PID}/ops.md\`
- qa  → \`~/.hermes/workspace/orchestrator-out/${PID}/qa.md\`

After all 3 return:
- Review each output
- If any disagree on interface/schema → send 1 correction message to specific agent (max 1 round)
- Assemble final output at \`~/.hermes/workspace/dev-cloud-synthesis/${PID}_\$(date +%Y-%m-%d_%H-%M).md\` with frontmatter:
  \`\`\`yaml
  priority_id: ${PID}
  project: ${PROJECT}
  title: ${TITLE}
  model: orchestrator-team
  worker: bd-orchestrator
  ran_at: <timestamp>
  reviewed: false
  orchestrator_of: 3
  \`\`\`
- Return ≤300-word summary to parent

After synthesis file written, priority's validator/reviewer pipeline picks it up automatically.
EOF

echo "[$(date '+%H:%M:%S')] dispatched $PID ($PROJECT) to orchestrator brief" >> "$LOG"
echo "dispatched: $PID ($PROJECT) → orchestrator team (dev+ops+qa)"

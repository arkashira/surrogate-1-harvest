#!/usr/bin/env bash
# Orchestrator Runner — script-based replacement for `pipe-bd-orchestrator-run`
# (agent-prompt version never fired). Reads the dispatched brief + invokes
# claude-bridge (sonnet/opus) to act as orchestrator who splits into
# dev + ops + qa workstreams.
set -u

LOG="$HOME/.claude/logs/bd-orchestrator-runner.log"
SHARED="$HOME/.hermes/workspace/swarm-shared"
BRIEF_FILE="$SHARED/orchestrator/next-brief.md"
STATE_MARKER="$HOME/.hermes/state/orchestrator-last-brief.mtime"
mkdir -p "$(dirname "$LOG")" "$(dirname "$STATE_MARKER")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

log "=== orchestrator cycle ==="

if [[ ! -f "$BRIEF_FILE" ]]; then
    log "no brief file — exit"
    exit 0
fi

CURRENT_MTIME=$(/usr/bin/stat -f '%m' "$BRIEF_FILE")
LAST_MTIME=$(/bin/cat "$STATE_MARKER" 2>/dev/null || echo 0)

if [[ "$CURRENT_MTIME" == "$LAST_MTIME" ]]; then
    log "brief unchanged since last run — skip"
    exit 0
fi

log "dispatching orchestrator (brief mtime $CURRENT_MTIME, prev $LAST_MTIME)"

BRIEF_CONTENT=$(/bin/cat "$BRIEF_FILE")
PROMPT="You are the ORCHESTRATOR agent. Read the team brief below and execute the multi-domain workflow it describes. You MUST:
1. Read the referenced spec file
2. Spawn dev + ops + qa as 3 parallel workstreams (use Task tool with subagent_type=dev/ops/qa — one message, 3 calls)
3. Each writes its slice to ~/.hermes/workspace/orchestrator-out/<PID>/{dev,ops,qa}.md
4. After all 3 return, ASSEMBLE a single synthesis file at ~/.hermes/workspace/dev-cloud-synthesis/<PID>_<date>.md with the frontmatter specified in the brief
5. Return ≤200-word summary

=== BRIEF ===
$BRIEF_CONTENT"

RESULT=$(/usr/bin/printf '%s' "$PROMPT" | /bin/bash "/opt/surrogate-1-harvest/bin/claude-bridge.sh" --model auto --timeout 1500 --allow-writes 2>&1)
RC=$?

if [[ $RC -ne 0 ]] || [[ -z "$RESULT" ]]; then
    log "orchestrator failed rc=$RC result=$(echo "$RESULT" | /usr/bin/head -c 200)"
    exit $RC
fi

log "orchestrator returned ${#RESULT} bytes"
echo "$RESULT" | /usr/bin/head -c 1500 >> "$LOG"
echo "" >> "$LOG"

# Mark brief as processed (don't re-run until bd-orchestrator-dispatch writes fresh one)
echo "$CURRENT_MTIME" > "$STATE_MARKER"

echo "orchestrator: processed brief mtime=$CURRENT_MTIME"

#!/usr/bin/env bash
# BD Continuous Runner — script-based replacement for `pipe-bd-continuous` (which
# was agent-prompt and Hermes daemon never fired user-registered agent crons).
#
# Does 3 turns as a shell pipeline:
#   TURN 1  pick next theme, write brief (bd-theme-picker.sh)
#   TURN 2  claude-bridge (sonnet) architect-style research with WebSearch — inline
#   TURN 3  post-process: enqueue, mark done (bd-post-research.sh)
#
# Runs every 2h at :05. Cheap because it uses claude-bridge (Max plan OAuth) which
# is the same path the reviewer uses — no new auth.
set -u

LOG="$HOME/.claude/logs/bd-continuous-runner.log"
SHARED="$HOME/.hermes/workspace/swarm-shared"
BRIEF_FILE="$SHARED/themes/next-brief.md"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

log "=== BD continuous cycle start ==="

# ── TURN 1: pick theme ──
PICK_OUT=$(/bin/bash "/opt/surrogate-1-harvest/bin/bd-theme-picker.sh" 2>&1)
log "picker: $PICK_OUT"

if [[ "$PICK_OUT" == no_theme:* ]] || [[ -z "$PICK_OUT" ]]; then
    log "no theme → exit"
    exit 0
fi

if [[ ! -f "$BRIEF_FILE" ]]; then
    log "ERROR: picker claimed success but no brief file"
    exit 1
fi

# ── TURN 2: claude-bridge research + spec writing ──
# claude-bridge returns the text output. We embed the whole brief as the prompt.
# Sonnet runs Read/Write/WebSearch/WebFetch natively in -p mode, so it can do
# the full research + file-writing without us coordinating tools.
log "dispatching sonnet with brief (${BRIEF_FILE})"

BRIEF_CONTENT=$(/bin/cat "$BRIEF_FILE")
PROMPT="You are the BD Research + Spec writer. Read the brief below and execute ALL deliverables it describes (WebSearch sources, Read target repo files via the Read tool, Write new specs to specs/pNN.md, append priority.json via Python load+save). Follow the brief exactly.

Return only a ≤200-word summary of what was written (no explanatory preamble).

=== BRIEF ===
$BRIEF_CONTENT"

# Sonnet (night window → opus) via Max plan. Timeout 15 min.
# Tools allowed: Read/Write/WebSearch/WebFetch/Bash — everything needed for spec writing.
# Need write/bash tools to create specs + append priority.json
RESULT=$(/usr/bin/printf '%s' "$PROMPT" | /bin/bash "/opt/surrogate-1-harvest/bin/claude-bridge.sh" --model auto --timeout 1200 --allow-writes 2>&1)
RC=$?

if [[ $RC -ne 0 ]] || [[ -z "$RESULT" ]]; then
    log "claude-bridge auto failed rc=$RC — trying sonnet explicit"
    RESULT=$(/usr/bin/printf '%s' "$PROMPT" | /bin/bash "/opt/surrogate-1-harvest/bin/claude-bridge.sh" --model sonnet --timeout 1200 --allow-writes 2>&1)
    RC=$?
fi

if [[ $RC -ne 0 ]]; then
    log "ERROR: bridge failed twice rc=$RC — abandoning cycle (theme will be retried by post-research marking)"
    echo "[$(date)] result: $RESULT" >> "$LOG"
    /bin/bash "/opt/surrogate-1-harvest/bin/bd-post-research.sh" >> "$LOG" 2>&1
    exit $RC
fi

log "sonnet returned ${#RESULT} bytes"
echo "$RESULT" | /usr/bin/head -c 2000 >> "$LOG"
echo "" >> "$LOG"

# ── TURN 3: post-research ──
POST_OUT=$(/bin/bash "/opt/surrogate-1-harvest/bin/bd-post-research.sh" 2>&1)
log "post-research: $POST_OUT"
echo "$POST_OUT"   # stdout for cron delivery

log "=== cycle done ==="

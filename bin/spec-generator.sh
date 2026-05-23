#!/usr/bin/env bash
# Spec Generator — auto-writes specs/${prio_id}.md for any `ready` priority
# lacking a spec. Without a spec, workers hallucinate; with one, validator
# pass-rate jumps ~5x (observed: p1/p10-p17 all passed syntax+imports).
#
# Strategy: cheap LLM (SambaNova DeepSeek-V3.1) reads priority title +
# description + repo-map of target project → emits a spec following the
# p1.md template. Sonnet is overkill for this; samba is free + 500/day.
#
# Runs every 15 min via cron. Skips priorities that already have specs.
set -u

LOG="$HOME/.claude/logs/spec-generator.log"
SHARED="$HOME/.hermes/workspace/swarm-shared"
SPECS_DIR="$SHARED/specs"
mkdir -p "$(dirname "$LOG")" "$SPECS_DIR"

# Find priorities without specs
MISSING=$(/usr/bin/python3 <<'PYEOF'
import json, os
from pathlib import Path
d = json.load(open(Path.home() / '.hermes/workspace/swarm-shared/priority.json'))
specs_dir = Path.home() / '.hermes/workspace/swarm-shared/specs'
for p in d.get('priorities', []):
    if p.get('status') not in ('ready', 'blocked_on_qa_fix'): continue
    pid = p.get('id','')
    if not pid: continue
    if (specs_dir / f'{pid}.md').exists(): continue
    print(f"{pid}|{p.get('project','?')}|{p.get('title','')}|{p.get('description','')}")
PYEOF
)

if [[ -z "$MISSING" ]]; then
    echo "[$(date '+%H:%M:%S')] all ready priorities have specs" >> "$LOG"
    exit 0
fi

count=0
while IFS='|' read -r PID PROJECT TITLE DESC; do
    [[ -z "$PID" ]] && continue
    SPEC_PATH="$SPECS_DIR/${PID}.md"
    [[ -f "$SPEC_PATH" ]] && continue

    echo "[$(date '+%H:%M:%S')] generating spec for $PID ($PROJECT: $TITLE)" >> "$LOG"

    # Gather grounding: repo-map + top 3 similar files via grep
    REPO_MAP=""
    MAP_FILE="$SHARED/repo-maps/${PROJECT}_map.md"
    [[ -f "$MAP_FILE" ]] && REPO_MAP=$(/usr/bin/head -c 8000 "$MAP_FILE")

    SIMILAR=""
    PROJECT_DIR="$HOME/axentx/$PROJECT"
    if [[ -d "$PROJECT_DIR" ]]; then
        KW=$(echo "$TITLE" | /usr/bin/tr '[:upper:]' '[:lower:]' | \
             /usr/bin/tr -cs 'a-z0-9' ' ' | /usr/bin/tr ' ' '\n' | \
             /usr/bin/awk 'length>4' | /usr/bin/head -3 | /usr/bin/tr '\n' '|' | /usr/bin/sed 's/|$//')
        if [[ -n "$KW" ]]; then
            SIMILAR=$(/usr/bin/find "$PROJECT_DIR" -type f \( -name '*.py' -o -name '*.ts' \) \
                ! -path '*/node_modules/*' ! -path '*/.venv/*' ! -path '*/dist/*' 2>/dev/null | \
                xargs /usr/bin/grep -lE "($KW)" 2>/dev/null | /usr/bin/head -3 | while read f; do
                    echo "=== ${f#$PROJECT_DIR/} ==="
                    /usr/bin/head -50 "$f" 2>/dev/null
                done 2>/dev/null | /usr/bin/head -c 4000)
        fi
    fi

    # Gold-standard template: p1 spec (proven format)
    TEMPLATE=$(/usr/bin/head -c 4000 "$SPECS_DIR/p1.md" 2>/dev/null)

    PROMPT=$(/bin/cat <<EOF
You are the Spec Generator. Write a CONCRETE implementable spec for the priority below, following the template EXACTLY. The spec goes directly to cloud LLM workers who will implement it.

HARD RULES:
1. Name exact files in repo (use REPO MAP below — do NOT invent new top-level modules unless truly required)
2. Name exact methods/classes/fields with signatures + types
3. Do NOT ask for clarification — pick a reasonable interpretation and commit
4. Constrain scope to 1-3 files + 3 pytest cases (workers have 2000-token output cap)
5. Output plain markdown. NO preamble. NO "Here is the spec:". Start directly with "# ${PID}: "

=== PRIORITY ===
ID: ${PID}
PROJECT: ${PROJECT}
TITLE: ${TITLE}
DESCRIPTION: ${DESC}

=== TEMPLATE (follow structure, NOT content) ===
${TEMPLATE}

=== REPO MAP (use these EXACT file paths) ===
${REPO_MAP}

=== SIMILAR EXISTING FILES (match their style) ===
${SIMILAR}

Now write the spec for ${PID}. Under 5KB. Start with the header line immediately.
EOF
)

    # SambaNova DeepSeek — fast, free-tier 500/day, great for structured output
    SPEC=$(echo "$PROMPT" | "/opt/surrogate-1-harvest/bin/sambanova-bridge.sh" --model deepseek 2>>"$LOG" | /usr/bin/head -c 6000)

    # Fallback to cloudflare if samba returned empty
    if [[ -z "$SPEC" ]] || [[ ${#SPEC} -lt 500 ]]; then
        echo "[$(date '+%H:%M:%S')] $PID samba empty, trying cloudflare" >> "$LOG"
        SPEC=$(echo "$PROMPT" | "/opt/surrogate-1-harvest/bin/cloudflare-bridge.sh" --model deepseek 2>>"$LOG" | /usr/bin/head -c 6000)
    fi

    if [[ -z "$SPEC" ]] || [[ ${#SPEC} -lt 500 ]]; then
        echo "[$(date '+%H:%M:%S')] $PID FAILED — both providers empty" >> "$LOG"
        continue
    fi

    # Sanity: does it start with "# ${PID}:" — if not, prepend a header
    if ! echo "$SPEC" | /usr/bin/head -1 | /usr/bin/grep -qE "^# ${PID}:"; then
        SPEC="# ${PID}: ${TITLE} (${PROJECT})

${SPEC}"
    fi

    echo "$SPEC" > "$SPEC_PATH"
    count=$((count + 1))
    echo "[$(date '+%H:%M:%S')] ✅ $PID → ${SPEC_PATH} (${#SPEC} bytes)" >> "$LOG"

    # Reset seen-lock for this priority so producer re-enqueues with the new spec
    REDIS_SOCK=$(/usr/bin/find /var/folders /tmp -name 'redis.socket' -type s 2>/dev/null | /usr/bin/head -1)
    [[ -n "$REDIS_SOCK" ]] && /opt/homebrew/bin/redis-cli -s "$REDIS_SOCK" DEL "hermes:seen:${PID}" > /dev/null 2>&1

done <<< "$MISSING"

echo "[$(date '+%H:%M:%S')] spec-generator done: $count specs written" >> "$LOG"

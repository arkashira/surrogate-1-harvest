#!/usr/bin/env bash
# Auto-orchestrate loop — fires SA → architect → qa-tdd → dev → qa-verify → reviewer chain.
#
# Strategy: pick a real TODO/FIXME from any axentx project, run the full pipeline,
# auto-commit on APPROVE. Runs every 20 min via cron.
# Pairs with surrogate-dev-loop (light/fast); this one does heavy multi-stage work.
#
# Linux + macOS compatible (auto-detects coreutils variants).
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/auto-orchestrate-loop.log"
mkdir -p "$(dirname "$LOG")"

# ── Resource guard (Linux + macOS) ──────────────────────────────────────────
LOAD=$(uptime | sed -E 's/.*load average[s]?:[[:space:]]*//' | awk -F',' '{print int($1)}')
# Free memory: Linux /proc/meminfo, macOS vm_stat
if [[ -r /proc/meminfo ]]; then
    FREE_MB=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
elif command -v vm_stat >/dev/null 2>&1; then
    FREE_MB=$(vm_stat | awk '/Pages free/{gsub("[.]","",$3); printf "%d", ($3*16384)/1048576}')
else
    FREE_MB=999  # unknown — assume OK
fi
if [[ ${LOAD:-0} -gt 8 ]] || [[ ${FREE_MB:-999} -lt 200 ]]; then
    echo "[$(date +%H:%M:%S)] resource-pause: load=$LOAD free_mb=$FREE_MB — skip" >> "$LOG"
    exit 0
fi

# ── Pick a real task: one TODO/FIXME from a randomly-chosen axentx project ──
TASK_INFO=$(python3 <<'PYEOF'
import os, random, re, subprocess, json
from pathlib import Path

# Real paths (verified via api.github.com 2026-04-28)
PROJECTS = [
    Path.home() / 'axentx/Costinel',
    Path.home() / 'axentx/vanguard',
    Path.home() / 'axentx/arkship',
    Path.home() / 'axentx/surrogate',
    Path.home() / 'axentx/workio',
    Path.home() / 'axentx/hermes-toolbelt',
]
PROJECTS = [p for p in PROJECTS if (p/'.git').exists()]
if not PROJECTS:
    print("{}"); exit()

random.shuffle(PROJECTS)
for proj in PROJECTS:
    cmd = ['rg', '--no-heading', '-n', '-m', '5',
           '--type', 'py', '--type', 'ts', '--type', 'go', '--type', 'sh',
           '-g', '!node_modules', '-g', '!.venv', '-g', '!__pycache__',
           '-g', '!.git', '-g', '!dist', '-g', '!build',
           r'(TODO|FIXME)[:\s]', str(proj)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        if not lines: continue
        line = random.choice(lines)
        m = re.match(r'^([^:]+):(\d+):(.+)$', line)
        if not m: continue
        path, lineno, content = m.groups()
        rel = os.path.relpath(path, proj)
        c = content.strip().lower()
        if any(skip in c for skip in ['#todo:', 'todo: fix', 'todo:', '// todo', 'todo()']) and len(content) < 30:
            continue
        print(json.dumps({
            'project': str(proj),
            'project_name': proj.name,
            'file': rel,
            'line': int(lineno),
            'content': content.strip()[:300],
        }))
        exit()
    except Exception:
        continue
print("{}")
PYEOF
)

if [[ -z "$TASK_INFO" ]] || [[ "$TASK_INFO" == "{}" ]]; then
    echo "[$(date +%H:%M:%S)] no task found — skip" >> "$LOG"
    exit 0
fi

PROJECT=$(echo "$TASK_INFO" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['project'])")
PROJ_NAME=$(echo "$TASK_INFO" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['project_name'])")
FILE=$(echo "$TASK_INFO" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['file'])")
LINE=$(echo "$TASK_INFO" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['line'])")
CONTENT=$(echo "$TASK_INFO" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['content'])")

# ── Per-task throttle: don't redo same TODO within 4 hours ─────────────────
# md5 (macOS) vs md5sum (Linux); stat -f%m (macOS) vs stat -c%Y (Linux)
if command -v md5sum >/dev/null 2>&1; then
    TASK_HASH=$(echo "${PROJ_NAME}:${FILE}:${LINE}" | md5sum | cut -c1-12)
else
    TASK_HASH=$(echo "${PROJ_NAME}:${FILE}:${LINE}" | md5 | cut -c1-12)
fi
LOCK_DIR="$HOME/.hermes/workspace/auto-orchestrate-locks"
mkdir -p "$LOCK_DIR"
LOCK="$LOCK_DIR/${TASK_HASH}"
if [[ -f "$LOCK" ]]; then
    if stat -c %Y "$LOCK" >/dev/null 2>&1; then
        LOCK_TS=$(stat -c %Y "$LOCK")
    else
        LOCK_TS=$(stat -f %m "$LOCK" 2>/dev/null || echo 0)
    fi
    AGE=$(( $(date +%s) - LOCK_TS ))
    if [[ $AGE -lt 14400 ]]; then
        echo "[$(date +%H:%M:%S)] task ${TASK_HASH} done ${AGE}s ago — skip" >> "$LOG"
        exit 0
    fi
fi
touch "$LOCK"

# ── Run orchestrate (auto-commits on APPROVE) ──────────────────────────────
START=$(date +%s)
echo "[$(date +%H:%M:%S)] orchestrate start: $PROJ_NAME/$FILE:$LINE" >> "$LOG"
echo "  task: $CONTENT" >> "$LOG"

TASK_DESC="Resolve this TODO/FIXME in $PROJ_NAME at $FILE:$LINE: \"$CONTENT\". Implement a real fix (not stub), keep changes scoped to the file/function. Match existing code style."

cd "$PROJECT" || { echo "[$(date +%H:%M:%S)] cd failed" >> "$LOG"; exit 1; }

bash "$HOME/.surrogate/bin/surrogate-orchestrate.sh" "$TASK_DESC" >> "$LOG" 2>&1
RC=$?
DUR=$(( $(date +%s) - START ))
echo "[$(date +%H:%M:%S)] orchestrate done in ${DUR}s rc=$RC" >> "$LOG"

# ── Push to GitHub if commit was created ───────────────────────────────────
if [[ $RC -eq 0 ]]; then
    LATEST_COMMIT=$(git -C "$PROJECT" log -1 --format=%H 2>/dev/null)
    LATEST_AGE=$(( $(date +%s) - $(git -C "$PROJECT" log -1 --format=%ct 2>/dev/null || echo 0) ))
    if [[ $LATEST_AGE -lt 600 ]]; then  # commit within last 10 min = was just made
        if git -C "$PROJECT" push origin HEAD:main >> "$LOG" 2>&1; then
            echo "[$(date +%H:%M:%S)]   ✅ pushed $LATEST_COMMIT to $PROJ_NAME" >> "$LOG"
        else
            echo "[$(date +%H:%M:%S)]   ⚠ push failed for $PROJ_NAME" >> "$LOG"
        fi
    fi
fi

# ── Discord notification ───────────────────────────────────────────────────
NOTIFY="$HOME/.surrogate/bin/notify-discord.sh"
if [[ -x "$NOTIFY" ]]; then
    if [[ $RC -eq 0 ]]; then
        "$NOTIFY" task "Auto-orchestrate: $PROJ_NAME" "$FILE:$LINE — \`$(echo "$CONTENT" | head -c 80)\` · ${DUR}s" 2>/dev/null &
    else
        "$NOTIFY" warn "Auto-orchestrate failed" "$PROJ_NAME · $FILE:$LINE · rc=$RC · ${DUR}s" 2>/dev/null &
    fi
fi

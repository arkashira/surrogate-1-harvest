#!/usr/bin/env bash
# Surrogate Daemon — continuous autonomous worker
#
# Architecture:
#   - Task queue file:     ~/.claude/state/surrogate-queue.jsonl (append-only)
#   - Workers:             N parallel (default 3)
#   - Pickup:              instant (as soon as worker idle → pull next task)
#   - Self-generation:     if queue empty, daemon asks itself "what should I work on?"
#                          based on recent episodes, todos, project state
#   - Self-healing:        worker crash → respawn, failed task → retry with different approach
#   - Episode consolidation: every 30 min, summarize episodes → patterns → Graphiti
#
# Usage:
#   surrogate-daemon.sh start [--workers 3]
#   surrogate-daemon.sh stop
#   surrogate-daemon.sh status
#   surrogate-daemon.sh enqueue "task description"
set -u
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

STATE="$HOME/.claude/state/surrogate-daemon"
QUEUE="$STATE/queue.jsonl"
DONE="$STATE/done.jsonl"
PID_FILE="$STATE/daemon.pid"
LOG="$HOME/.claude/logs/surrogate-daemon.log"
WORKERS=1          # default 1 worker (budget-safe). User can --workers 3 for burst
mkdir -p "$STATE" "$(dirname "$LOG")"

CMD="${1:-status}"

case "$CMD" in
    enqueue)
        shift
        TASK="$*"
        [[ -z "$TASK" ]] && { echo "need task"; exit 2; }
        ENQUEUE_TASK="$TASK" /usr/bin/python3 - "$QUEUE" <<'PYEOF'
import json, uuid, os, sys
from datetime import datetime
queue_path = sys.argv[1]
task = {
    'id': uuid.uuid4().hex[:12],
    'ts': datetime.utcnow().isoformat(),
    'task': os.environ.get('ENQUEUE_TASK', ''),
    'status': 'pending',
    'priority': 'P0-user',
}
open(queue_path, 'a').write(json.dumps(task, ensure_ascii=False) + '\n')
print(f"enqueued: {task['id']} {task['task'][:60]}")
PYEOF
        exit 0
        ;;
    plan)
        # Manage the active plan: surrogate-daemon.sh plan set <file.md>
        #                         surrogate-daemon.sh plan show
        #                         surrogate-daemon.sh plan clear
        PLAN_FILE="$HOME/.surrogate/active-plan.md"
        PLAN_CMD="${2:-show}"
        case "$PLAN_CMD" in
            set)
                SRC="${3:-}"
                [[ -z "$SRC" ]] && { echo "usage: $0 plan set <file.md>"; exit 2; }
                cp "$SRC" "$PLAN_FILE"
                TOTAL=$(grep -c '^\- \[ \]' "$PLAN_FILE" 2>/dev/null || echo 0)
                echo "✅ active plan set → $PLAN_FILE ($TOTAL pending tasks)"
                ;;
            show)
                if [[ -f "$PLAN_FILE" ]]; then
                    DONE=$(grep -c '^\- \[x\]' "$PLAN_FILE" 2>/dev/null || echo 0)
                    PENDING=$(grep -c '^\- \[ \]' "$PLAN_FILE" 2>/dev/null || echo 0)
                    RUNNING=$(grep -c '^\- \[~\]' "$PLAN_FILE" 2>/dev/null || echo 0)
                    echo "📋 Active plan: $PLAN_FILE"
                    echo "   ✅ done=$DONE  🔄 running=$RUNNING  ⏳ pending=$PENDING"
                    echo ""
                    cat "$PLAN_FILE"
                else
                    echo "❌ No active plan. Set one: $0 plan set <file.md>"
                fi
                ;;
            clear)
                rm -f "$PLAN_FILE"
                echo "✅ active plan cleared"
                ;;
            *)
                echo "usage: $0 plan {set <file>|show|clear}"
                ;;
        esac
        exit 0
        ;;
    status)
        if [[ -f "$PID_FILE" ]] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
            echo "✅ daemon running (pid $(cat $PID_FILE))"
        else
            echo "❌ daemon not running"
        fi
        PENDING=$(grep '"status": "pending"' "$QUEUE" 2>/dev/null | wc -l | tr -d ' ')
        DONE_CNT=$(wc -l < "$DONE" 2>/dev/null | tr -d ' ')
        echo "queue pending: ${PENDING:-0}"
        echo "tasks done:    ${DONE_CNT:-0}"
        echo "recent log:"
        tail -10 "$LOG" 2>/dev/null
        exit 0
        ;;
    stop)
        if [[ -f "$PID_FILE" ]]; then
            kill "$(cat $PID_FILE)" 2>/dev/null && echo "stopped" || echo "already dead"
            rm -f "$PID_FILE"
        fi
        # Kill workers
        pkill -f "surrogate-worker" 2>/dev/null || true
        exit 0
        ;;
    start)
        shift
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --workers) WORKERS="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        if [[ -f "$PID_FILE" ]] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
            echo "already running"; exit 0
        fi
        # Fork detached daemon
        nohup "$0" _run "$WORKERS" >> "$LOG" 2>&1 &
        echo $! > "$PID_FILE"
        sleep 1
        echo "✅ started pid $(cat $PID_FILE) workers=$WORKERS"
        exit 0
        ;;
    _run)
        WORKERS="$2"
        echo "[$(date '+%H:%M:%S')] daemon up, workers=$WORKERS" >> "$LOG"
        trap 'echo "[$(date +%H:%M:%S)] shutdown" >> "$LOG"; pkill -P $$; exit 0' TERM INT

        # Main loop: spawn workers, monitor, self-generate tasks
        while true; do
            # How many workers currently running?
            RUNNING=$(pgrep -f "surrogate-worker-$$" | wc -l | tr -d ' ')
            SLOTS=$((WORKERS - RUNNING))

            if [[ $SLOTS -gt 0 ]]; then
                for i in $(seq 1 $SLOTS); do
                    (
                        exec -a "surrogate-worker-$$" "$0" _worker
                    ) &
                    sleep 0.5
                done
            fi

            # Every 30min: consolidation
            NOW_MIN=$(date +%M)
            if [[ "$NOW_MIN" == "15" ]] || [[ "$NOW_MIN" == "45" ]]; then
                "$HOME/.claude/bin/surrogate-consolidate.sh" >> "$LOG" 2>&1 &
            fi

            sleep 10
        done
        ;;
    _worker)
        # ── Pop one task from queue (P0-user first, then plan, then self-gen) ──────
        _pop_queue() {
            /usr/bin/python3 <<PYEOF
import json, os, sys, fcntl
from pathlib import Path
q = Path(os.path.expanduser('$QUEUE'))
if not q.exists():
    print(''); sys.exit(0)
f = open(q, 'r+')
fcntl.flock(f.fileno(), fcntl.LOCK_EX)
lines = f.readlines()
picked = None; remaining = []
for line in lines:
    line = line.rstrip('\n')
    if not line: continue
    try: t = json.loads(line)
    except: remaining.append(line); continue
    if picked is None and t.get('status') == 'pending':
        t['status'] = 'running'; picked = t
        remaining.append(json.dumps(t, ensure_ascii=False))
    else:
        remaining.append(json.dumps(t, ensure_ascii=False))
f.seek(0); f.truncate()
f.write('\n'.join(remaining) + ('\n' if remaining else ''))
fcntl.flock(f.fileno(), fcntl.LOCK_UN); f.close()
if picked: print(json.dumps(picked, ensure_ascii=False))
PYEOF
        }

        # ── Pop next task from active plan (no sleep needed — plan drives work) ──
        _pop_plan() {
            /usr/bin/python3 <<'PYEOF'
import sys, json, os, re, uuid
from pathlib import Path
from datetime import datetime

plan_file = Path.home() / '.surrogate' / 'active-plan.md'
if not plan_file.exists():
    sys.exit(0)

text = plan_file.read_text()

# Find first unchecked task: "- [ ] task text"
match = re.search(r'^- \[ \] (.+)', text, re.MULTILINE)
if not match:
    sys.exit(0)  # All done or no pending tasks

task_text = match.group(1).strip()
task_id = 'plan-' + uuid.uuid4().hex[:8]

# Mark as in-progress in plan: [ ] → [~]
new_text = text.replace(f'- [ ] {task_text}', f'- [~] {task_text}', 1)
plan_file.write_text(new_text)

print(json.dumps({
    'id': task_id,
    'ts': datetime.utcnow().isoformat(),
    'task': task_text,
    'status': 'running',
    'source': 'plan',
}))
PYEOF
        }

        # ── Self-generate task from pool (fallback when no plan + queue empty) ──
        _self_gen() {
            AUTO_TASK=$(/usr/bin/python3 <<'PYEOF'
import json, os, random
from pathlib import Path
ep = Path(os.path.expanduser('~/.claude/state/surrogate-memory/episodes.jsonl'))
recent_topics = []
if ep.exists():
    for line in ep.read_text().splitlines()[-30:]:
        try: recent_topics.append(json.loads(line).get('task','')[:80])
        except: pass
pool = [
    # A. Knowledge freshness
    "หา CVE ใหม่ 7 วันล่าสุดจาก CISA KEV ที่ affect Python/Next.js/K8s → ingest index.db",
    "หา blog ใหม่สุดจาก Anthropic/OpenAI/Google AI → ingest index.db",
    "Scan Hacker News top 10 (past 24h) ดู tech news ใหม่ → summarize + ingest",
    "ค้น arxiv AI/ML papers อัพเดทล่าสุด → abstract summary → ingest",
    "Scrape Grafana/Prometheus/OpenTelemetry release notes ใหม่ → ingest",
    "Crawl thai blognone + techsauce news อัพเดท 24h → ingest",
    # B. Codebase health
    "อ่าน ~/axentx/ หา TODO/FIXME across projects → สร้าง fix spec",
    "เช็ค axentx test coverage per project → identify weakest → propose tests",
    "Scan ~/.claude/bin/ หา script ที่ไม่ถูกใช้ > 7 days → propose archive",
    "Review last 10 auto-commits → ตรวจว่า quality OK หรือไม่",
    # C. Knowledge quality
    "สำรวจ index.db หา duplicate entries → propose dedup",
    "หา docs ใน index.db ที่มี identifying leaks → scrub",
    "List firecrawl-urls.txt หา URL ที่ scrape 0 content > 3 attempts → remove",
    "ค้น RAG หัวข้อที่ model confidence ต่ำ (จาก episodes.jsonl) → deep-dive crawl",
    # D. Hermes pipeline health
    "เช็ค healer log 24h → top 3 recurring issues → propose fix",
    "Review last 10 Hermes cron failures → find pattern",
    "เช็ค dev-worker output quality (quality_score < 5) → identify failing patterns",
    "อ่าน tournament results — ดูว่า provider ไหนชนะบ่อย → update routing",
    # E. Self-improvement
    "Benchmark self บน random ashira-bench axis → log score → compare ถึง last week",
    "Analyze own episodes last 100 → find 3 weak topics → spawn crawl4ai deep-dive",
    "สร้าง procedural skill จาก 3 recurring successful patterns → save as SKILL.md",
    "Review own hallucinations (episodes with '[error' or low confidence) → learn",
    # F. axentx business
    "Review axentx priority.json top 5 → ตรวจว่ามี spec ครบ → สร้าง spec ที่ขาด",
    "Scan competitor news (Snyk/Wiz/Datadog/Costinel-competitors) → summarize threat",
    "Analyze last 7 days commits — ProductA vs ProductB momentum",
    "Check RunPod budget burn rate vs target → alert if > 75%",
    "Find Thai SME potential customers signals → log → leads.jsonl",
    # G. Curious mode
    "ผมไม่รู้อะไร? — query episodes.jsonl หา 'ไม่ทราบ'/'ต้องค้นคว้า' → pick 1 → research via web_fetch",
    "หัวข้อใหม่ที่ Hermes cron สแคปล่าสุด 24h — ผมควรรู้อะไรเพิ่ม → ingest deeper",
    "เทียบ capability ของ claude-code features — ผมยังขาดอะไร → propose add",
]
chosen = None
for t in random.sample(pool, len(pool)):
    if not any(t[:40] in r for r in recent_topics):
        chosen = t; break
print(chosen or pool[0])
PYEOF
)
            echo "{\"id\":\"auto-$(/usr/bin/python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')\",\"task\":\"$AUTO_TASK\",\"self_generated\":true,\"source\":\"self-gen\"}"
        }

        # ── Task resolution: queue → plan → self-gen (no 60s sleep) ─────────────
        TASK_JSON=$(_pop_queue)

        if [[ -z "$TASK_JSON" ]]; then
            # Queue empty — check plan immediately (plan-driven, no throttle)
            TASK_JSON=$(_pop_plan)
        fi

        if [[ -z "$TASK_JSON" ]]; then
            # No plan tasks — brief pause (5s) then self-generate
            # (main daemon loop already spaces workers 10s apart)
            sleep 5
            TASK_JSON=$(_self_gen)
        fi

        # Extract task
        TASK=$(echo "$TASK_JSON" | /usr/bin/python3 -c "import json,sys; print(json.loads(sys.stdin.read())['task'])")
        TID=$(echo "$TASK_JSON" | /usr/bin/python3 -c "import json,sys; print(json.loads(sys.stdin.read())['id'])")
        SOURCE=$(echo "$TASK_JSON" | /usr/bin/python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('source','queue'))")

        echo "[$(date +%H:%M:%S)] worker picked $TID [$SOURCE]: ${TASK:0:80}" >> "$LOG"
        START=$(date +%s)

        # Execute via agent
        OUTPUT=$("$HOME/.claude/bin/surrogate-agent.sh" --max-steps 6 "$TASK" 2>&1 | tail -50)
        END=$(date +%s)
        DUR=$((END - START))

        # If task came from plan, mark as done ([ ] → [x]) — env vars = safe quoting
        if [[ "$SOURCE" == "plan" ]]; then
            DAEMON_TASK="$TASK" /usr/bin/python3 - >> "$LOG" 2>&1 <<'PYEOF'
import re, os
from pathlib import Path
plan_file = Path.home() / '.surrogate' / 'active-plan.md'
if plan_file.exists():
    task = os.environ.get('DAEMON_TASK', '')
    text = plan_file.read_text()
    new_text = text.replace(f'- [~] {task}', f'- [x] {task}', 1)
    plan_file.write_text(new_text)
    remaining = len(re.findall(r'^- \[ \]', new_text, re.MULTILINE))
    print(f"[plan] marked done: {task[:60]} | remaining: {remaining}")
PYEOF
        fi

        # Mark done in audit log
        DAEMON_TASK="$TASK" DAEMON_OUTPUT="$(echo "$OUTPUT" | tail -20)" \
            /usr/bin/python3 - "$TID" "$SOURCE" "$DUR" "$DONE" >> "$LOG" 2>&1 <<'PYEOF'
import json, os, sys
tid, source, dur, done_path = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
done = {
    'id': tid, 'source': source,
    'task': os.environ.get('DAEMON_TASK', ''),
    'duration_sec': dur,
    'output_tail': os.environ.get('DAEMON_OUTPUT', '')[:2000],
}
open(done_path, 'a').write(json.dumps(done, ensure_ascii=False) + '\n')
print(f"[{tid}] done in {dur}s")
PYEOF
        exit 0
        ;;
    *)
        echo "usage: $0 {start|stop|status|enqueue <task>|plan {set <file>|show|clear}}"
        exit 2
        ;;
esac

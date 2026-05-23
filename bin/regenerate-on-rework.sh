#!/usr/bin/env bash
# Regenerate-on-rework — feedback loop that turns reviewer rework verdicts into
# new work. Strategy:
#   1. Find review.json files with verdict=rework OR quality<7 from last 2h
#   2. Extract bugs/hallucinations from the review
#   3. Append them to ~/.claude/memory/worker-prompt-deltas.jsonl as anti-patterns
#      (so next worker sees them in prompt via context_builder.sh)
#   4. Re-enqueue the priority to a DIFFERENT provider than the one that failed
#   5. Rate-limit: max 3 regenerate cycles per priority per day (tracked in state)
#
# This creates a compounding improvement loop:
#   worker produces buggy code → reviewer flags specific bugs → anti-pattern added
#   → next worker (different provider) sees the anti-pattern in its prompt →
#   produces fewer bugs → higher quality score → ships to main.
#
# Runs every 20 min (between tournament and auto-commit).
set -u

LOG="$HOME/.claude/logs/regenerate-on-rework.log"
SHARED="$HOME/.hermes/workspace/swarm-shared"
REVIEW_DIR="$HOME/.hermes/workspace/qwen-coder-reviews"
DELTAS="$HOME/.claude/memory/worker-prompt-deltas.jsonl"
STATE="$HOME/.claude/state/regenerate-count.json"
VENV_PY="$HOME/.claude/state/validator-venv/bin/python3"
mkdir -p "$(dirname "$LOG")" "$(dirname "$DELTAS")" "$(dirname "$STATE")"
touch "$DELTAS" "$STATE"
[[ ! -s "$STATE" ]] && echo "{}" > "$STATE"

"$VENV_PY" <<'PYEOF' 2>>"$LOG"
import json, re
from pathlib import Path
from datetime import datetime, timedelta

HOME = Path.home()
REVIEW_DIR = HOME / '.hermes/workspace/qwen-coder-reviews'
DELTAS = HOME / '.claude/memory/worker-prompt-deltas.jsonl'
STATE = HOME / '.claude/state/regenerate-count.json'
PRIORITY_JSON = HOME / '.hermes/workspace/swarm-shared/priority.json'

# Load state — per-priority regenerate count (capped at 3 per day)
try: state = json.loads(STATE.read_text())
except: state = {}
today = datetime.utcnow().strftime('%Y-%m-%d')
state = {k:v for k,v in state.items() if v.get('date') == today}  # wipe old days

# Redis socket
import subprocess, os
sock_find = subprocess.run(['find','/var/folders','/tmp','-name','redis.socket','-type','s'],
                           capture_output=True, text=True, timeout=5)
sock = sock_find.stdout.strip().split('\n')[0] if sock_find.stdout else ''

def redis(*args):
    if not sock: return ''
    r = subprocess.run(['redis-cli','-s',sock] + list(args),
                       capture_output=True, text=True, timeout=3)
    return r.stdout.strip()

# Find recent rework/reject reviews
cutoff = datetime.now().timestamp() - 7200  # last 2h

processed = 0
regenerated = 0
deltas_added = 0

for rev_path in sorted(REVIEW_DIR.glob('*.review.json'), key=lambda p: -p.stat().st_mtime):
    if rev_path.stat().st_mtime < cutoff: continue
    basename = rev_path.stem.replace('.review','')
    pid = basename.split('_')[0]  # pNN

    # Parse review JSON (may be markdown-wrapped)
    try:
        txt = rev_path.read_text()
        m = re.search(r'\{.*\}', txt, re.DOTALL)
        if not m: continue
        d = json.loads(m.group(0))
    except Exception:
        continue

    verdict = d.get('verdict','')
    quality = d.get('quality_score', 0)

    # Only process rework/reject with quality < 7
    if verdict not in ('rework','reject'): continue
    if quality >= 7: continue  # might still get accepted, skip
    processed += 1

    # Cap regenerate count per priority per day
    entry = state.get(pid, {'date': today, 'count': 0, 'last_providers': []})
    if entry['count'] >= 3:
        continue  # already tried 3 times today

    # Extract actionable anti-patterns (bugs + hallucinations)
    bugs = d.get('bugs', []) or []
    halluc = d.get('hallucinations', []) or []
    insights = []
    for b in bugs[:3]:
        insights.append({
            'type': 'bug',
            'priority': pid,
            'content': str(b)[:300],
        })
    for h in halluc[:3]:
        insights.append({
            'type': 'hallucination',
            'priority': pid,
            'content': str(h)[:300],
        })
    if not insights:
        continue

    # Find which provider produced this output (from frontmatter)
    # Output file: ~/.hermes/workspace/dev-cloud-<provider>/<basename>.md
    source_provider = None
    for d_name in ('samba','cloudflare','groq','github','cerebras','nvidia','gemini','qwen-coder','synthesis'):
        out_path = HOME / f'.hermes/workspace/dev-cloud-{d_name}/{basename}.md'
        if not out_path.exists():
            out_path = HOME / f'.hermes/workspace/{d_name}/{basename}.md'
        if out_path.exists():
            source_provider = d_name.replace('qwen-coder','qwen-local')
            break

    # Append anti-pattern to worker-prompt-deltas (so next worker sees it)
    now_iso = datetime.utcnow().isoformat() + 'Z'
    delta_entry = {
        'ts': now_iso,
        'source': 'regenerate-on-rework',
        'priority': pid,
        'from_provider': source_provider or 'unknown',
        'verdict': verdict,
        'quality': quality,
        'prompt_addition': '\n'.join(
            f"ANTI-PATTERN (from {pid} review, quality={quality}): {i['content']}"
            for i in insights[:3]
        ),
    }
    with open(DELTAS, 'a') as f:
        f.write(json.dumps(delta_entry) + '\n')
    deltas_added += 1

    # Re-enqueue priority to a DIFFERENT provider (not the one that just produced rework)
    # Cloud providers excluding source
    all_cloud = ['samba','cloudflare','groq','cerebras','nvidia','github']
    # Exclude source + providers already tried
    last_tried = set(entry.get('last_providers', []))
    last_tried.add(source_provider)
    candidates = [p for p in all_cloud if p not in last_tried]
    if not candidates:
        # All tried — reset and try local/synthesis path
        candidates = ['samba','cloudflare','groq','cerebras','nvidia']
    target = candidates[hash(pid) % len(candidates)]

    # Push priority back into target provider's queue
    pri = json.loads(PRIORITY_JSON.read_text())
    pri_obj = next((p for p in pri.get('priorities', []) if p.get('id') == pid), None)
    if not pri_obj: continue
    payload = json.dumps(pri_obj)
    redis('LPUSH', f'hermes:work:coding:{target}', payload)

    # Update state
    entry['count'] += 1
    entry['last_providers'] = list(last_tried) + [target]
    entry['date'] = today
    entry['last_regen_at'] = now_iso
    state[pid] = entry
    regenerated += 1

    print(f"[regen] {pid} q={quality} from={source_provider} → {target} (attempt {entry['count']}/3)")

STATE.write_text(json.dumps(state, indent=2))
print(f"[regen] summary: processed={processed} deltas_added={deltas_added} regenerated={regenerated}")
PYEOF

echo "[$(date '+%H:%M:%S')] regen done" >> "$LOG"

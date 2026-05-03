#!/usr/bin/env bash
# Harvest Claude Code session transcripts → interaction log + distillation
# Reads ~/.claude/projects/*/uuid.jsonl (Claude Code's own session logs)
# Extracts: user messages, assistant responses, tool calls, decisions
# Writes to ~/.claude/interactions/ so distill-patterns.sh picks them up
#
# Schedule: daily (after crawl)
set -e

SINCE_HOURS="${1:-48}"  # default: last 48h
export PATH=/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin:$PATH
PY=~/.claude/venv/bin/python

PROJECTS="$HOME/.claude/projects"
OUT_DIR="$HOME/.claude/interactions"
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/$(date +%Y-%m)-sessions.jsonl"
STATE="$HOME/.claude/.harvest-state.json"

[ ! -d "$PROJECTS" ] && { echo "No Claude Code projects dir"; exit 0; }

SINCE_HOURS="$SINCE_HOURS" PROJECTS="$PROJECTS" OUT_FILE="$OUT_FILE" STATE="$STATE" "$PY" <<'PY'
import os, json, glob, time
from pathlib import Path
from datetime import datetime, timedelta, timezone

projects = Path(os.environ['PROJECTS'])
out_file = os.environ['OUT_FILE']
state_file = os.environ['STATE']
since_hours = int(os.environ['SINCE_HOURS'])
cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

# Load harvest state (avoid re-processing same messages)
seen = set()
if os.path.exists(state_file):
    try:
        with open(state_file) as f:
            seen = set(json.load(f).get('seen_uuids', []))
    except: pass

harvested = 0
new_seen = set(seen)

for proj_dir in projects.iterdir():
    if not proj_dir.is_dir(): continue
    # PRIVACY: Skip Claude Code session logs from ~/develope/* working dirs.
    # Those sessions include Read tool calls of client source code +
    # plaintext credentials in command history. Audit @ 2026-05-03 found
    # 11 claude-*.jsonl harvest files with 80,659 develope hits + RDS
    # password leak. Never harvest develope sessions.
    name = proj_dir.name
    if 'develope' in name.lower():
        continue
    for jsonl in proj_dir.glob('*.jsonl'):
        try: mtime = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
        except: continue
        if mtime < cutoff: continue

        # Read session
        with open(jsonl, errors='ignore') as f:
            # Group turns: user message → assistant response(s) → tool results
            buffer = {'user_query': None, 'assistant_text': [], 'tool_calls': [], 'session_id': jsonl.stem}
            for line in f:
                try: entry = json.loads(line)
                except: continue

                uuid = entry.get('uuid','')
                if uuid in seen: continue
                new_seen.add(uuid)

                t = entry.get('type','')
                msg = entry.get('message', {})
                role = msg.get('role','')

                if t == 'user' and role == 'user':
                    # New user turn — flush previous
                    if buffer['user_query'] and (buffer['assistant_text'] or buffer['tool_calls']):
                        entry_out = {
                            'ts': entry.get('timestamp', datetime.now().isoformat()),
                            'provider': 'claude-code',
                            'model': 'claude-opus-4-7',
                            'session': buffer['session_id'],
                            'project': proj_dir.name,
                            'query': buffer['user_query'][:2000],
                            'response': '\n'.join(buffer['assistant_text'])[:4000],
                            'tools_used': buffer['tool_calls'][:20],
                        }
                        with open(out_file, 'a') as fout:
                            fout.write(json.dumps(entry_out, ensure_ascii=False) + '\n')
                        harvested += 1
                    # Start new turn
                    content = msg.get('content', '')
                    if isinstance(content, list):
                        content = ' '.join(c.get('text','') if isinstance(c,dict) else str(c) for c in content)
                    buffer = {'user_query': str(content), 'assistant_text':[], 'tool_calls':[], 'session_id': jsonl.stem}
                elif t == 'assistant' and role == 'assistant':
                    content = msg.get('content', [])
                    if isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict): continue
                            if item.get('type') == 'text':
                                buffer['assistant_text'].append(item.get('text',''))
                            elif item.get('type') == 'tool_use':
                                buffer['tool_calls'].append({
                                    'tool': item.get('name',''),
                                    'input_summary': str(item.get('input',{}))[:300],
                                })

            # Flush last turn
            if buffer['user_query'] and (buffer['assistant_text'] or buffer['tool_calls']):
                entry_out = {
                    'ts': datetime.now().isoformat(),
                    'provider': 'claude-code',
                    'model': 'claude-opus-4-7',
                    'session': buffer['session_id'],
                    'project': proj_dir.name,
                    'query': buffer['user_query'][:2000],
                    'response': '\n'.join(buffer['assistant_text'])[:4000],
                    'tools_used': buffer['tool_calls'][:20],
                }
                with open(out_file, 'a') as fout:
                    fout.write(json.dumps(entry_out, ensure_ascii=False) + '\n')
                harvested += 1

# Save state
with open(state_file, 'w') as f:
    json.dump({'seen_uuids': list(new_seen)[-50000:], 'last_run': datetime.now().isoformat()}, f)

print(f'Harvested {harvested} new turns from Claude Code transcripts')
print(f'Tracking {len(new_seen)} total UUIDs (last 50k kept)')
PY

# Trigger graph sync
[ -x "/opt/surrogate-1-harvest/bin/graph-sync.sh" ] && ("/opt/surrogate-1-harvest/bin/graph-sync.sh" > /dev/null 2>&1 &) || true

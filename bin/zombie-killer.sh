#!/usr/bin/env bash
# Auto-close stuck cron sessions + reset cascaded credential exhaustions.
# Runs every 5 min via cron.
set -u
LOG="$HOME/.claude/logs/zombie-killer.log"
mkdir -p "$(dirname "$LOG")"

python3 <<'PYEOF' >> "$LOG" 2>&1
import sqlite3, time, datetime, json, os

DB = os.path.expanduser('~/.hermes/state.db')
AUTH = os.path.expanduser('~/.hermes/auth.json')

conn = sqlite3.connect(DB)
cur = conn.cursor()

# 1. Close zombie sessions: ended_at IS NULL AND started > 10 min ago AND NO activity for 5 min
# (Using last message timestamp as activity proxy)
cur.execute("""
SELECT s.id, s.started_at, 
       (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id) as last_msg
FROM sessions s 
WHERE s.ended_at IS NULL AND s.source='cron' AND s.started_at < strftime('%s','now')-600
""")
zombies = []
now = time.time()
for sid, started, last_msg in cur.fetchall():
    activity_age = now - (last_msg or started)
    if activity_age > 300:  # no activity for 5 min
        zombies.append((sid, int(now - started), int(activity_age)))

closed = 0
for sid, age, idle in zombies:
    cur.execute("""UPDATE sessions SET ended_at=?, end_reason='zombie_killer' WHERE id=?""",
                (now, sid))
    closed += cur.rowcount
    print(f"[{datetime.datetime.now().strftime('%H:%M')}] killed zombie {sid} age={age//60}min idle={idle//60}min")

conn.commit()

# 2. Reset cascaded credential exhaustion (same logic as scorecard)
reset_count = 0
if os.path.exists(AUTH):
    with open(AUTH) as f: d = json.load(f)
    for provider in ('openrouter', 'ollama-cloud'):
        for c in d.get('credential_pool',{}).get(provider, []):
            if c.get('last_status') == 'exhausted':
                err = (c.get('last_error_message','') or '').lower()
                # Cascade: error mentions different provider
                if ('gemini' in err or 'google' in err) and provider != 'gemini':
                    c['last_status'] = None
                    c['last_error_code'] = None
                    c['last_error_message'] = None
                    reset_count += 1
            # Real expired cooldown
            if c.get('last_status') == 'exhausted':
                reset_at = c.get('last_error_reset_at')
                if reset_at and time.time() > reset_at:
                    c['last_status'] = None
                    reset_count += 1
    if reset_count:
        with open(AUTH, 'w') as f: json.dump(d, f, indent=2)

print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] zombies_closed={closed} creds_reset={reset_count}")
PYEOF

#!/usr/bin/env bash
# Worker token-budget tracker — aggregates token usage per provider per day
# and emits a JSON snapshot. Workers can check this BEFORE a call to self-throttle.
# Runs every 15 min.
set -u

LOG="$HOME/.claude/logs/worker-token-budget.log"
OUT="$HOME/.hermes/workspace/budget/tokens-$(date +%Y-%m-%d).json"
mkdir -p "$(dirname "$LOG")" "$(dirname "$OUT")"

/usr/bin/python3 <<'PYEOF' > "$OUT"
import os, re, json, time
from pathlib import Path
from datetime import datetime

HOME = Path.home()
today = datetime.now().strftime('%Y-%m-%d')

# Per-provider free-tier DAILY CAPS (requests).
# Conservative relative to published limits — leave headroom for user + cron
# of other bridges (healer, research, etc.) also hitting the same provider.
# GitHub Models free tier: Codestral is in "low" tier → 50 req/day per account,
# but we share this process's calls with healer+skill synth+agent-skill-creator,
# so keep cap at 50 to preserve headroom for those reactive paths.
# Docs:
#   github: https://docs.github.com/en/github-models/prototyping-with-ai-models
#   sambanova: https://community.sambanova.ai/t/rate-limits-daily-500-req (free)
#   cloudflare Workers AI: 10k neurons/day free = ~10k small calls
#   groq: mixed 14.4k rpd across all models (free tier)
#   gemini: 1.5k rpd on pro free tier
CAPS = {
    'github':    {'req': 50,    'reserve_pct': 20},   # keep for healer/synth headroom
    'sambanova': {'req': 500,   'reserve_pct': 10},
    'cloudflare':{'req': 10000, 'reserve_pct': 5},
    'groq':      {'req': 14400, 'reserve_pct': 5},
    'gemini':    {'req': 1500,  'reserve_pct': 10},
    'cerebras':  {'req': 8000,  'reserve_pct': 5},    # free tier Llama/Qwen ~8k/day
    'nvidia':    {'req': 1000,  'reserve_pct': 10},   # NIM free tier conservative
    'chutes':    {'req': 500,   'reserve_pct': 10},   # needs activation
    'claude':    {'req': None,  'reserve_pct': 0},    # Max plan flat
    'granite':   {'req': None,  'reserve_pct': 0},    # local unlimited
    'qwen-local':{'req': None,  'reserve_pct': 0},    # local unlimited
    'surrogate': {'req': None,  'reserve_pct': 0},    # local Ashira-personalized
}

usage = {}
for provider, cap in CAPS.items():
    log_path = HOME / f'.claude/logs/{provider}-bridge.log'
    if not log_path.exists():
        usage[provider] = {'calls': 0, 'status': 'no-log'}
        continue

    # Count lines with "model=" (each = 1 call)
    calls = 0
    try:
        with open(log_path) as f:
            for line in f:
                if 'model=' in line:
                    calls += 1
    except Exception: pass

    cap_req = cap['req']
    if cap_req:
        util_pct = round(calls / cap_req * 100, 1)
        reserve = int(cap_req * cap['reserve_pct'] / 100)
        budget_left = cap_req - calls - reserve
        # Status: can call? warn? throttle? halt?
        if budget_left <= 0:
            status = 'HALT'
        elif budget_left < cap_req * 0.2:
            status = 'THROTTLE'
        elif util_pct > 70:
            status = 'WARN'
        else:
            status = 'OK'
        usage[provider] = {
            'calls': calls, 'cap': cap_req,
            'reserve': reserve, 'budget_left': budget_left,
            'utilization_pct': util_pct, 'status': status,
        }
    else:
        usage[provider] = {'calls': calls, 'cap': None, 'status': 'UNLIMITED'}

print(json.dumps({
    'date': today,
    'scanned_at': datetime.utcnow().isoformat() + 'Z',
    'providers': usage,
    'total_calls': sum(u.get('calls', 0) for u in usage.values()),
}, indent=2))
PYEOF

# Alert if any provider at HALT
HALTED=$(/usr/bin/python3 -c "
import json
d = json.load(open('$OUT'))
for n, u in d['providers'].items():
    if u.get('status') == 'HALT':
        print(f'{n}: {u[\"calls\"]}/{u[\"cap\"]} — BUDGET EXHAUSTED')
")
if [[ -n "$HALTED" ]]; then
    echo "[$(date '+%H:%M:%S')] ⛔ $HALTED" >> "$LOG"
fi

echo "[$(date '+%H:%M:%S')] snapshot → $OUT" >> "$LOG"

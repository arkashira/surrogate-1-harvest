#!/usr/bin/env bash
# Dashboard generator — static HTML snapshot of Hermes state.
# Runs every 5 min → writes to ~/.hermes/workspace/dashboard/index.html
# Open with: open ~/.hermes/workspace/dashboard/index.html
# (Or host via `python3 -m http.server` in that dir.)
set -u

LOG="$HOME/.claude/logs/dashboard.log"
DASH="$HOME/.hermes/workspace/dashboard"
mkdir -p "$(dirname "$LOG")" "$DASH"

/usr/bin/python3 <<'PYEOF' > "$DASH/index.html"
import json, os, time
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter, defaultdict

HOME = Path.home()
now = time.time()

def read_json(p, default=None):
    try: return json.load(open(p))
    except: return default

def tail(p, n=10):
    try:
        with open(p) as f: lines = f.readlines()
        return lines[-n:]
    except: return []

def count_files(d, age_sec=None):
    p = Path(d)
    if not p.exists(): return 0
    files = list(p.glob('*.md')) + list(p.glob('*.json'))
    if age_sec:
        files = [f for f in files if now - f.stat().st_mtime < age_sec]
    return len(files)

# ---- Collect state ----
jobs = read_json(HOME / '.hermes/cron/jobs.json', {})
all_jobs = jobs.get('jobs', [])
if isinstance(all_jobs, dict): all_jobs = list(all_jobs.values())

enabled_jobs = [j for j in all_jobs if isinstance(j, dict) and j.get('enabled', True)]

# Session activity
session_dir = HOME / '.hermes/sessions'
sessions_1h = len(list(session_dir.glob('*.json'))) if session_dir.exists() else 0
sessions_1h = len([f for f in session_dir.glob('*.json') if now - f.stat().st_mtime < 3600]) if session_dir.exists() else 0

# Bridge activity (last hour)
bridge_activity = {}
for name in ['github', 'sambanova', 'cloudflare', 'groq', 'granite', 'claude']:
    log = HOME / f'.claude/logs/{name}-bridge.log'
    if log.exists():
        hour_ago = time.strftime('%H:', time.localtime(now - 3600))
        # count lines with "model=" in last ~200 entries
        try:
            lines = tail(log, 500)
            bridge_activity[name] = sum(1 for l in lines if 'model=' in l)
        except: bridge_activity[name] = 0
    else:
        bridge_activity[name] = 0

# Worker outputs
worker_outputs = {}
for w in ['qwen-coder', 'dev-cloud-github', 'dev-cloud-samba', 'dev-cloud-cloudflare', 'dev-cloud-groq', 'dev-cloud-gemini']:
    d = HOME / f'.hermes/workspace/{w}'
    worker_outputs[w] = count_files(d, age_sec=86400)  # last 24h

# Healer + reviewer + ceremony stats
healer_count = count_files(HOME / '.hermes/workspace/healer', age_sec=86400)
ceremony_count = 0
cer_dir = HOME / '.hermes/workspace/ceremonies'
if cer_dir.exists():
    for sub in cer_dir.iterdir():
        if sub.is_dir():
            ceremony_count += len(list(sub.glob('*.md')))

# RAG stats
import subprocess
try:
    doc_count = subprocess.check_output(
        ['sqlite3', str(HOME / '.claude/index.db'), 'SELECT COUNT(*) FROM docs'],
        text=True, timeout=5
    ).strip()
except: doc_count = '?'

# Embedding coverage
try:
    emb_count = subprocess.check_output(
        ['sqlite3', str(HOME / '.claude/embeddings.db'), 'SELECT COUNT(*) FROM embeddings'],
        text=True, timeout=5
    ).strip()
except: emb_count = '0'

# Redis work queue telemetry
queue_depth = 0
worker_locks = []
try:
    sock = subprocess.check_output(['find', '/var/folders', '/tmp', '-name', 'redis.socket', '-type', 's'],
                                      text=True, timeout=5).split('\n')[0].strip()
    if sock:
        queue_depth = int(subprocess.check_output(
            ['redis-cli', '-s', sock, 'LLEN', 'hermes:work:coding'],
            text=True, timeout=3
        ).strip() or '0')
        # Active worker locks
        lock_keys = subprocess.check_output(
            ['redis-cli', '-s', sock, 'KEYS', 'hermes:worker-lock:*'],
            text=True, timeout=3
        ).strip().split('\n')
        worker_locks = [k for k in lock_keys if k]
except Exception: pass

# Daemon status (launchd)
daemons = {}
try:
    r = subprocess.check_output(['launchctl', 'list'], text=True, timeout=3).strip()
    for line in r.split('\n'):
        if 'hermes-' in line and 'daemon' in line:
            parts = line.split('\t')
            if len(parts) >= 3:
                label = parts[2].replace('com.ashira.hermes-', '').replace('-daemon', '')
                daemons[label] = {'pid': parts[0], 'status': parts[1]}
except Exception: pass

# Budget
today = datetime.now().strftime('%Y-%m-%d')
budget = read_json(HOME / f'.hermes/workspace/budget/{today}.json', {})

# Staged lessons
staging_count = len(list((HOME / '.claude/memory/staging').glob('cluster-*.md'))) if (HOME / '.claude/memory/staging').exists() else 0

# ---- Render HTML ----
html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>Hermes Dashboard — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 20px; background: #0d1117; color: #c9d1d9; }}
h1 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 10px; }}
h2 {{ color: #7ee787; margin-top: 30px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin: 16px 0; }}
.card {{ background: #161b22; border: 1px solid #30363d; padding: 16px; border-radius: 8px; }}
.card .value {{ font-size: 32px; font-weight: bold; color: #58a6ff; }}
.card .label {{ font-size: 12px; color: #8b949e; text-transform: uppercase; }}
.ok {{ color: #7ee787; }}
.warn {{ color: #f1e05a; }}
.critical {{ color: #f85149; }}
table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #30363d; }}
th {{ background: #161b22; color: #8b949e; }}
tr:hover {{ background: #1c2128; }}
.bar {{ background: #30363d; height: 8px; border-radius: 4px; overflow: hidden; }}
.bar-fill {{ height: 100%; background: #58a6ff; }}
.timestamp {{ color: #6e7681; font-size: 12px; }}
</style>
</head>
<body>
<h1>🚀 Hermes — Autonomous Agent Dashboard</h1>
<p class="timestamp">Generated at {datetime.now().isoformat()} · auto-refresh every 60s</p>

<h2>Overview</h2>
<div class="grid">
  <div class="card"><div class="value">{len(enabled_jobs)}</div><div class="label">Enabled Cron Jobs</div></div>
  <div class="card"><div class="value">{sessions_1h}</div><div class="label">Sessions (last 1h)</div></div>
  <div class="card"><div class="value">{doc_count}</div><div class="label">RAG Docs</div></div>
  <div class="card"><div class="value">{sum(bridge_activity.values())}</div><div class="label">AI Calls (recent)</div></div>
</div>

<h2>Provider Activity (recent)</h2>
<div class="grid">
"""
for name, count in bridge_activity.items():
    html += f'  <div class="card"><div class="value">{count}</div><div class="label">{name}</div></div>\n'
html += "</div>\n"

html += "<h2>Worker Outputs (last 24h)</h2><div class='grid'>"
for w, cnt in worker_outputs.items():
    html += f'<div class="card"><div class="value">{cnt}</div><div class="label">{w}</div></div>'
html += "</div>\n"

html += f"""
<h2>Self-Healing</h2>
<div class="grid">
  <div class="card"><div class="value">{healer_count}</div><div class="label">Healer Diagnoses (24h)</div></div>
  <div class="card"><div class="value">{ceremony_count}</div><div class="label">Ceremony Outputs</div></div>
  <div class="card"><div class="value">{staging_count}</div><div class="label">Staged Patterns</div></div>
</div>

<h2>Work Queue (Redis)</h2>
<div class="grid">
  <div class="card"><div class="value">{queue_depth}</div><div class="label">Queue Depth (hermes:work:coding)</div></div>
  <div class="card"><div class="value">{len(worker_locks)}</div><div class="label">Active Worker Locks</div></div>
  <div class="card"><div class="value">{emb_count}</div><div class="label">Semantic Embeddings</div></div>
  <div class="card"><div class="value">{len(daemons)}</div><div class="label">Running Daemons</div></div>
</div>

<h2>Daemons (launchd)</h2>
<table><tr><th>Daemon</th><th>PID</th><th>Exit</th><th>Status</th></tr>
"""
for label, info in daemons.items():
    status_cls = 'ok' if info['status'] == '0' else 'warn'
    running = '✅ running' if info.get('pid','-') != '-' else '⚠️ idle'
    html += f"<tr><td>{label}</td><td>{info.get('pid','-')}</td><td class='{status_cls}'>{info['status']}</td><td>{running}</td></tr>"
html += "</table>"

# Budget table
if budget and 'providers' in budget:
    html += "<h2>Budget Today</h2><table><tr><th>Provider</th><th>Calls</th><th>Limit</th><th>Utilization</th><th>Status</th></tr>"
    for name, info in budget['providers'].items():
        pct = info.get('utilization_pct') or 0
        status = info.get('status', '?')
        status_class = {'OK':'ok','WARN':'warn','CRITICAL':'critical','N/A':''}.get(status, '')
        limit = info.get('limit') or '∞'
        html += f"<tr><td>{name}</td><td>{info['calls_today']}</td><td>{limit}</td>"
        html += f"<td><div class='bar'><div class='bar-fill' style='width:{min(pct,100)}%'></div></div> {pct}%</td>"
        html += f"<td class='{status_class}'>{status}</td></tr>"
    html += "</table>"

# Recent crons (last 5)
import re
recent_sessions = []
if session_dir.exists():
    for f in sorted(session_dir.glob('cron_*.json'), key=lambda x: x.stat().st_mtime, reverse=True)[:8]:
        m = re.match(r'(?:session_|request_dump_)?cron_([^_]+)_(\d{8}_\d{6})', f.name)
        if m:
            cron_id, ts = m.groups()
            # Find job name for this id
            name = next((j.get('name','?') for j in enabled_jobs if isinstance(j, dict) and j.get('id') == cron_id), cron_id[:8])
            age_sec = int(now - f.stat().st_mtime)
            recent_sessions.append((name, ts, age_sec))

if recent_sessions:
    html += "<h2>Recent Cron Fires (last 8)</h2><table><tr><th>Job</th><th>Timestamp</th><th>Age</th></tr>"
    for name, ts, age in recent_sessions:
        html += f"<tr><td>{name}</td><td>{ts}</td><td>{age}s ago</td></tr>"
    html += "</table>"

html += "</body></html>"
print(html)
PYEOF

SIZE=$(wc -c < "$DASH/index.html" | tr -d ' ')
echo "[$(date '+%H:%M:%S')] rendered $SIZE bytes → $DASH/index.html" >> "$LOG"

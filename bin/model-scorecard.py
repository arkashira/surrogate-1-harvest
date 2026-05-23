#!/usr/bin/env python3
"""
Model performance scorecard + auto-swap engine for Hermes cron pipeline.

Reads recent session outcomes from state.db, computes per-model quality score,
detects 429/rate-limit exhaustion (with cooldown timers), and can propose/apply
cron job model swaps so the pipeline stays unblocked.

Runs as part of HR monitor. Can also be invoked standalone.

Usage:
  model-scorecard.py score            # print scorecard
  model-scorecard.py swap             # apply auto-swap recommendations  
  model-scorecard.py unblock          # re-activate cooled-down models
"""
import sqlite3, json, os, sys, time, datetime, re, collections

HERMES_HOME = os.path.expanduser('~/.hermes')
DB = f'{HERMES_HOME}/state.db'
CATALOG = f'{HERMES_HOME}/workspace/swarm-shared/openrouter-free-models.json'
JOBS_PATH = f'{HERMES_HOME}/cron/jobs.json'
SCORECARD_PATH = f'{HERMES_HOME}/workspace/swarm-shared/model-scorecard.json'
COOLDOWN_PATH = f'{HERMES_HOME}/workspace/swarm-shared/model-cooldown.json'
LOG = os.path.expanduser('~/.claude/logs/model-scorecard.log')

os.makedirs(os.path.dirname(LOG), exist_ok=True)

def log(msg):
    with open(LOG, 'a') as f:
        f.write(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}\n")

# ────────── Helpers ──────────
def load_catalog():
    if not os.path.exists(CATALOG): return {'models': []}
    with open(CATALOG) as f: return json.load(f)

def load_cooldown():
    if not os.path.exists(COOLDOWN_PATH): return {}
    with open(COOLDOWN_PATH) as f: return json.load(f)

def save_cooldown(d):
    with open(COOLDOWN_PATH, 'w') as f: json.dump(d, f, indent=2)

def load_scorecard():
    if not os.path.exists(SCORECARD_PATH): return {}
    with open(SCORECARD_PATH) as f: return json.load(f)

def save_scorecard(d):
    with open(SCORECARD_PATH, 'w') as f: json.dump(d, f, indent=2)

# ────────── Scoring ──────────
def compute_scores(window_seconds=3600):
    """Score each model based on sessions in last N seconds."""
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cutoff = time.time() - window_seconds
    cur.execute("""SELECT model, tool_call_count, ended_at, end_reason, 
                          input_tokens+output_tokens as tok, started_at
                   FROM sessions WHERE started_at > ?""", (cutoff,))
    rows = cur.fetchall()

    # Also pull hallucination flags from agent-quality.jsonl
    quality_path = f'{HERMES_HOME}/workspace/swarm-shared/agent-quality.jsonl'
    hallucinations_by_run = collections.Counter()
    if os.path.exists(quality_path):
        with open(quality_path) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get('source') == 'claude-dev-review':
                        hc = e.get('hallucinations', 0)
                        # Match by run_id timeframe (approx)
                        hallucinations_by_run['total'] += hc
                except: pass

    scores = collections.defaultdict(lambda: {
        'runs': 0, 'completed': 0, 'total_tools': 0, 'total_tokens': 0,
        'timeouts': 0, '429s': 0, 'total_duration': 0, 'avg_duration': 0,
    })
    for model, tools, end, reason, tok, start in rows:
        if not model: continue
        s = scores[model]
        s['runs'] += 1
        s['total_tools'] += (tools or 0)
        s['total_tokens'] += (tok or 0)
        if end:
            s['completed'] += 1
            s['total_duration'] += (end - start)
        if reason and ('timeout' in str(reason).lower() or 'compression' in str(reason).lower()):
            s['timeouts'] += 1

    # Check log for 429 events per model in same window
    agent_log = os.path.expanduser('~/.hermes/logs/agent.log')
    if os.path.exists(agent_log):
        with open(agent_log) as f:
            for line in f:
                if '429' not in line and 'exhausted' not in line: continue
                # Match format: [cron_...] credential pool: marking X exhausted (status=429)
                m_model = re.search(r'[Mm]odel=([^ ,]+)|→ ([^ ]+)', line)
                if m_model:
                    mname = (m_model.group(1) or m_model.group(2) or '').strip()
                    if mname in scores:
                        scores[mname]['429s'] += 1

    # Compute composite score (0-100)
    for m, s in scores.items():
        if s['runs'] == 0: continue
        s['avg_duration'] = s['total_duration'] / max(s['completed'], 1)
        completion_rate = s['completed'] / s['runs']
        tool_efficiency = min(s['total_tools'] / max(s['completed']*3, 1), 1)  # 3 tools/run ideal
        timeout_penalty = s['timeouts'] / s['runs']
        rate_limit_penalty = min(s['429s'] / s['runs'], 1)
        # Score: 50% completion + 20% tool use + 30% penalties
        composite = 100 * (completion_rate * 0.5 + tool_efficiency * 0.2) - (timeout_penalty * 20) - (rate_limit_penalty * 30)
        s['score'] = max(0, min(100, composite))
    return dict(scores)

# ────────── Cooldown management ──────────
def is_cooled_down(model):
    cd = load_cooldown()
    entry = cd.get(model)
    if not entry: return True
    # If cooldown_until time has passed, remove from cooldown
    if time.time() >= entry.get('until', 0):
        del cd[model]
        save_cooldown(cd)
        log(f"model {model} cooled down — re-activated")
        return True
    return False

def set_cooldown(model, seconds=3600):
    cd = load_cooldown()
    until = time.time() + seconds
    cd[model] = {
        'until': until,
        'until_human': datetime.datetime.fromtimestamp(until).isoformat(),
        'reason': '429 rate-limited',
        'set_at': datetime.datetime.now().isoformat(),
    }
    save_cooldown(cd)
    log(f"cooldown SET {model} until {cd[model]['until_human']}")

# ────────── Task → model routing ──────────
# Role → ranked model preference list (tool-capable, sized to task)
ROLE_MODEL_PREFS = {
    # Heavy reasoning (architecture, AI R&D): prefer big context + reasoning
    'architect':   ['openai/gpt-oss-120b:free', 'nvidia/nemotron-3-super-120b-a12b:free', 'z-ai/glm-4.5-air:free', 'qwen/qwen3-next-80b-a3b-instruct:free'],
    'ai-research': ['openai/gpt-oss-120b:free', 'nvidia/nemotron-3-super-120b-a12b:free', 'qwen/qwen3-coder:free', 'minimax/minimax-m2.5:free'],
    'b1a1-spec':   ['openai/gpt-oss-120b:free', 'nvidia/nemotron-3-super-120b-a12b:free', 'z-ai/glm-4.5-air:free'],
    'pm-priority': ['openai/gpt-oss-120b:free', 'nvidia/nemotron-3-super-120b-a12b:free'],
    # Code work: prefer coder-specialized
    'dev':         ['qwen/qwen3-coder:free', 'openai/gpt-oss-120b:free', 'nvidia/nemotron-3-super-120b-a12b:free'],
    'devops':      ['qwen/qwen3-coder:free', 'openai/gpt-oss-120b:free', 'z-ai/glm-4.5-air:free'],
    'integration': ['qwen/qwen3-coder:free', 'openai/gpt-oss-120b:free'],
    'qa':          ['openai/gpt-oss-120b:free', 'nvidia/nemotron-nano-9b-v2:free', 'qwen/qwen3-coder:free'],
    # Fast/short tasks: smaller models OK
    'docs':        ['openai/gpt-oss-20b:free', 'nvidia/nemotron-nano-9b-v2:free', 'openai/gpt-oss-120b:free'],
    'sec-review':  ['openai/gpt-oss-120b:free', 'qwen/qwen3-coder:free'],
    'perf-bench':  ['openai/gpt-oss-120b:free', 'qwen/qwen3-coder:free'],
    'ux-ui':       ['google/gemma-4-26b-a4b-it:free', 'openai/gpt-oss-120b:free'],
    'data':        ['openai/gpt-oss-120b:free', 'qwen/qwen3-coder:free'],
    'b4-research': ['openai/gpt-oss-120b:free', 'nvidia/nemotron-3-super-120b-a12b:free'],
    'hr-monitor':  ['openai/gpt-oss-20b:free', 'nvidia/nemotron-nano-9b-v2:free'],  # reliable tool-use
    'claude-reviewer':   ['claude-sonnet-4-5'],   # via bridge, not swapped
    'claude-dev-review': ['claude-sonnet-4-5'],
    'sprint-plan':       ['claude-opus-4-5'],
    'retro':             ['claude-opus-4-5'],
}

def pick_model(role):
    """Return (model, provider) for role, skipping cooled-down models."""
    prefs = ROLE_MODEL_PREFS.get(role, ['openai/gpt-oss-120b:free'])
    for m in prefs:
        # Claude/Ollama handled separately (not subject to this swap)
        if m.startswith('claude-') or m.startswith('ollama-cloud/'):
            return (m, 'anthropic' if 'claude' in m else 'ollama-cloud')
        if is_cooled_down(m):
            return (m, 'openrouter')
    # All cooled down — return first (will likely fail but try)
    log(f"WARN all preferred models cooled down for {role}, returning first: {prefs[0]}")
    return (prefs[0], 'openrouter')

# ────────── Apply swaps ──────────
def apply_swaps(dry_run=False):
    with open(JOBS_PATH) as f: data = json.load(f)
    changes = []
    for j in data['jobs']:
        if not j.get('enabled'): continue
        name = j.get('name','')
        if not name.startswith('pipe-'): continue
        role = name.replace('pipe-','')
        # Skip Claude/Ollama-routed jobs (they use bridge or local)
        if role in ('claude-reviewer','claude-dev-review','sprint-plan','retro','hr-monitor'):
            continue
        current_model = j.get('model')
        desired_model, desired_provider = pick_model(role)
        if current_model and current_model.startswith('claude-'): continue
        # Set only if different AND not the "no model = default" case
        if desired_model != current_model:
            changes.append({
                'role': role, 'from': current_model or 'default', 'to': desired_model,
                'job_id': j['id']
            })
            if not dry_run:
                j['model'] = desired_model
                j['provider'] = desired_provider
                j['base_url'] = None
                j['updated_at'] = datetime.datetime.now().isoformat()

    if changes and not dry_run:
        data['updated_at'] = datetime.datetime.now().isoformat()
        with open(JOBS_PATH, 'w') as f: json.dump(data, f, indent=2)
        log(f"applied {len(changes)} model swaps")
    return changes

# ────────── Main ──────────
def reset_cascaded_credentials():
    """Reset OpenRouter/Ollama credentials that got marked exhausted from cascaded Gemini errors."""
    AUTH = os.path.expanduser('~/.hermes/auth.json')
    if not os.path.exists(AUTH): return 0
    with open(AUTH) as f: d = json.load(f)
    reset_count = 0
    for provider in ('openrouter', 'ollama-cloud'):
        for c in d.get('credential_pool',{}).get(provider, []):
            if c.get('last_status') == 'exhausted':
                err = (c.get('last_error_message','') or '').lower()
                # If error text mentions a DIFFERENT provider, it's a misattributed cascade
                if provider == 'openrouter' and ('gemini' in err or 'google' in err):
                    c['last_status'] = None
                    c['last_error_code'] = None
                    c['last_error_message'] = None
                    reset_count += 1
                    log(f"reset cascaded {provider}/{c['label']}")
                elif provider == 'ollama-cloud' and ('gemini' in err or 'google' in err):
                    c['last_status'] = None
                    c['last_error_code'] = None
                    c['last_error_message'] = None
                    reset_count += 1
                    log(f"reset cascaded {provider}/{c['label']}")
        # Also: real 429 that's past reset_at time
        for c in d.get('credential_pool',{}).get(provider, []):
            if c.get('last_status') == 'exhausted':
                reset_at = c.get('last_error_reset_at')
                if reset_at and time.time() > reset_at:
                    c['last_status'] = None
                    reset_count += 1
                    log(f"reset expired-cooldown {provider}/{c['label']}")
    if reset_count:
        with open(AUTH,'w') as f: json.dump(d, f, indent=2)
    return reset_count

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'score'

    if cmd == 'score':
        scores = compute_scores(3600)
        save_scorecard({
            'computed_at': datetime.datetime.now().isoformat(),
            'window': '1 hour',
            'scores': scores,
        })
        print(f"{'Model':<55} {'Runs':>5} {'Done%':>6} {'Tools':>6} {'429':>4} {'Score':>6}")
        print("-"*90)
        for m, s in sorted(scores.items(), key=lambda x: -x[1].get('score', 0)):
            if s['runs'] == 0: continue
            comp = int(100 * s['completed'] / s['runs'])
            score = s.get('score', 0)
            print(f"{m[:53]:<55} {s['runs']:>5} {comp:>5}% {s['total_tools']:>6} {s['429s']:>4} {score:>6.1f}")
        # Show cooldown
        cd = load_cooldown()
        if cd:
            print(f"\n🔒 In cooldown: {len(cd)}")
            for m, e in cd.items():
                remaining = int((e['until'] - time.time())/60)
                print(f"  {m} — {remaining} min remaining")

    elif cmd == 'swap':
        # First: detect new 429 events and set cooldowns
        agent_log = os.path.expanduser('~/.hermes/logs/agent.log')
        if os.path.exists(agent_log):
            # Look at last 30 min
            cutoff = time.time() - 1800
            with open(agent_log) as f:
                for line in f:
                    if '429' not in line: continue
                    # Parse timestamp to skip old
                    m_ts = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                    if not m_ts: continue
                    try:
                        lt = time.mktime(time.strptime(m_ts.group(1), '%Y-%m-%d %H:%M:%S'))
                        if lt < cutoff: continue
                    except: continue
                    # Match model
                    m = re.search(r'marking ([A-Z_]+) exhausted', line)
                    if m:
                        # Map env var to model  — simplistic
                        env_name = m.group(1)
                        # OPENROUTER_API_KEY = entire pool; mark all openrouter models as cooled 15 min
                        if env_name == 'OPENROUTER_API_KEY':
                            # Parse recovery time if present
                            set_cooldown('openai/gpt-oss-120b:free', 900)
                            log(f"detected 429 via {env_name}, cooldown openai 15 min")
                        elif env_name == 'GEMINI_API_KEY':
                            set_cooldown('gemini-2.5-flash', 3600)
        changes = apply_swaps(dry_run=False)
        if changes:
            print(f"Applied {len(changes)} swaps:")
            for c in changes:
                print(f"  {c['role']:<20} {c['from']:<45} → {c['to']}")
        else:
            print("No swaps needed")

    elif cmd == 'unblock':
        # Re-activate cooled-down models whose timer expired
        cd = load_cooldown()
        now = time.time()
        expired = [m for m, e in cd.items() if e.get('until', 0) < now]
        for m in expired:
            del cd[m]
            log(f"unblocked {m}")
        save_cooldown(cd)
        # Also reset any cascaded credential exhaustions
        reset_count = reset_cascaded_credentials()
        print(f"Unblocked {len(expired)} models, reset {reset_count} cascaded creds")

    elif cmd == 'list-free':
        cat = load_catalog()
        print(f"Free catalog: {cat.get('total_free')} models, {cat.get('tool_capable_count')} tool-capable")
        for m in cat.get('models',[])[:30]:
            print(f"  {m['id']}")

if __name__ == '__main__':
    main()



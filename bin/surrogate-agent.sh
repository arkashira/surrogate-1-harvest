#!/usr/bin/env bash
# Surrogate Agent v2 — Claude-Code parity + sub-agent spawning + self-healing
#
# Tools (parity with Claude Code):
#   bash, read, write, edit, glob, grep, rag_query, web_fetch, web_search,
#   task (spawn sub-agent), todo_add, episode_log
#
# Model routing:
#   - Thai/reasoning  → claude-haiku-4.5 (best Thai on the market)
#   - Code            → qwen3-coder
#   - Research/long   → gemini-2.5-flash (1M context)
#   - Fast/cheap      → qwen3-235b-a22b-2507
#
# Features:
#   - Sub-agent parallel: `task` tool spawns nested agent with own session
#   - Self-healing: error → log → retry with fallback model
#   - Episode memory: every turn logged → nightly consolidation
#   - Provider hidden from output (privacy)
set -u
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

MAX_STEPS=10
TASK=""
MODEL_OVERRIDE=""
DAEMON=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --max-steps) MAX_STEPS="$2"; shift 2 ;;
        --model) MODEL_OVERRIDE="$2"; shift 2 ;;
        --daemon) DAEMON=1; shift ;;
        *) TASK="$*"; break ;;
    esac
done
[[ -z "$TASK" ]] && { echo "usage: $0 [--max-steps N] [--model M] <task>" >&2; exit 2; }

MEM_DIR="$HOME/.surrogate/state/surrogate-memory"
mkdir -p "$MEM_DIR"

export AGENT_TASK="$TASK"
export AGENT_MAX_STEPS="$MAX_STEPS"
export AGENT_MODEL_OVERRIDE="$MODEL_OVERRIDE"

python3 <<'PYEOF'
import sys, os, json, re, sqlite3, subprocess, urllib.request, urllib.error, time, uuid
from datetime import datetime
from pathlib import Path

TASK = os.environ['AGENT_TASK']
MAX_STEPS = int(os.environ['AGENT_MAX_STEPS'])
MODEL_OVERRIDE = os.environ.get('AGENT_MODEL_OVERRIDE', '')
OPENROUTER = os.environ.get('OPENROUTER_API_KEY', '')
MEM_DIR = Path(os.path.expanduser('~/.surrogate/state/surrogate-memory'))
EPISODES = MEM_DIR / 'episodes.jsonl'
PATTERNS = MEM_DIR / 'patterns.jsonl'
SYS_PROMPT = ''
p = Path(os.path.expanduser('~/axentx/surrogate/system-prompt.md'))
if p.exists(): SYS_PROMPT = p.read_text()[:10000]

# ═══ MODEL ROUTING ═══
def pick_model(task_text):
    t = task_text.lower()
    if MODEL_OVERRIDE: return MODEL_OVERRIDE
    # Cost-first routing: FREE variants first, paid only for user P0 or complex tasks
    # Detect P0-user flag in task text (daemon prepends this for user queries)
    is_user_p0 = 'P0-user' in task_text or task_text.startswith('[USER]')
    # Code → free qwen coder first
    if any(k in t for k in ['เขียน code','implement','def ','class ','function','refactor','terraform','kubernetes yaml','bash script','write tests','sql query']):
        return 'qwen/qwen3-coder' if is_user_p0 else 'qwen/qwen3-coder:free'
    # Research/long doc → cheap Gemini flash lite
    if any(k in t for k in ['research','analyze','compare','long document','summarize','read entire','วิจัย','ค้นคว้า']):
        return 'google/gemini-2.5-flash-lite' if is_user_p0 else 'qwen/qwen3-next-80b-a3b-instruct:free'
    # Thai reasoning for user P0 → Haiku (premium). Self-gen → free llama or qwen
    if is_user_p0:
        return 'anthropic/claude-haiku-4.5'
    return 'meta-llama/llama-3.3-70b-instruct:free'

# Fallback chain: prefer FREE first, escalate to cheap paid, premium last
FALLBACK_CHAIN = [
    'qwen/qwen3-coder:free',
    'meta-llama/llama-3.3-70b-instruct:free',
    'qwen/qwen3-next-80b-a3b-instruct:free',
    'qwen/qwen3-235b-a22b-2507',          # $0.07/$0.10 cheapest paid
    'google/gemini-2.5-flash-lite',       # $0.10/$0.40
    'deepseek/deepseek-chat-v3.1',        # $0.15/$0.75
    'anthropic/claude-haiku-4.5',         # $1/$5 premium
]

# ═══ TOOLS (Claude Code parity) ═══
def tool_bash(cmd, timeout=60, cwd=None):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return {'stdout': r.stdout[:5000], 'stderr': r.stderr[:2000], 'rc': r.returncode}
    except Exception as e:
        return {'error': str(e)}

def tool_read(path, limit=2000, offset=0):
    try:
        p = Path(os.path.expanduser(path))
        if p.is_dir():
            return {'error': 'is directory', 'items': [f.name for f in p.iterdir()][:50]}
        lines = p.read_text(errors='replace').splitlines()
        chunk = '\n'.join(lines[offset:offset+limit])
        return {'path': str(p), 'content': chunk[:20000], 'total_lines': len(lines), 'offset': offset}
    except Exception as e:
        return {'error': str(e)}

def tool_write(path, content):
    try:
        p = Path(os.path.expanduser(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return {'path': str(p), 'bytes': len(content), 'ok': True}
    except Exception as e:
        return {'error': str(e)}

def tool_edit(path, old_string, new_string):
    try:
        p = Path(os.path.expanduser(path))
        txt = p.read_text()
        if old_string not in txt:
            return {'error': 'old_string not found in file'}
        if txt.count(old_string) > 1:
            return {'error': f'old_string appears {txt.count(old_string)} times — ambiguous. Add more context.'}
        p.write_text(txt.replace(old_string, new_string, 1))
        return {'path': str(p), 'ok': True}
    except Exception as e:
        return {'error': str(e)}

def tool_glob(pattern, path='.'):
    try:
        import glob as glob_lib
        base = os.path.expanduser(path)
        matches = glob_lib.glob(os.path.join(base, pattern), recursive=True)[:100]
        return {'matches': matches, 'count': len(matches)}
    except Exception as e:
        return {'error': str(e)}

def tool_grep(pattern, path='.', glob='*', context=0):
    base = os.path.expanduser(path)
    ctx = f'-C {context}' if context else ''
    cmd = f"grep -rn {ctx} --include='{glob}' -E {subprocess.list2cmdline([pattern])} {base} 2>/dev/null | head -50"
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
    return {'matches': r.stdout[:5000]}

def tool_rag_query(query, limit=5, source_filter=None):
    """Hybrid retrieval: SQLite FTS5 (BM25) + ChromaDB (dense) fused via RRF (k=60).
    Returns top-ranked docs from combined signal — +13-25% recall vs single-source."""
    import subprocess as _sp
    try:
        # 1. BM25 via SQLite FTS
        conn = sqlite3.connect(os.path.expanduser('~/.surrogate/index.db'))
        kw = ' '.join(w for w in re.sub(r'[^a-zA-Z0-9ก-๙ ]', ' ', query.lower()).split() if len(w) > 2)[:200]
        q = "SELECT d.source, d.path, substr(d.response, 1, 500), d.id FROM docs_fts f JOIN docs d ON d.id=f.rowid WHERE f.docs_fts MATCH ?"
        params = [kw]
        if source_filter:
            q += " AND d.source = ?"
            params.append(source_filter)
        q += f" ORDER BY bm25(docs_fts) LIMIT {max(limit*3, 20)}"
        bm25_rows = []
        try:
            bm25_rows = list(conn.execute(q, params).fetchall())
        except sqlite3.OperationalError: pass
        conn.close()

        # 2. Dense via Chroma (if query > 10 chars, worth dense)
        dense_docs = []
        if len(query) > 10:
            try:
                cmd = f"""~/.surrogate/state/crawler-venv/bin/python -c "
import chromadb, json, sys
client = chromadb.PersistentClient(path='$HOME/.surrogate/code-vector-db')
cols = client.list_collections()
if cols:
    r = cols[0].query(query_texts=['{query[:200].replace(chr(39),chr(92)+chr(39))}'], n_results={max(limit*3,20)})
    out = []
    for i in range(len(r['documents'][0]) if r['documents'] else 0):
        out.append({{'path': (r['metadatas'][0][i] or {{}}).get('file_path','')[-60:], 'text': r['documents'][0][i][:400]}})
    print(json.dumps(out, ensure_ascii=False))
" 2>/dev/null"""
                r = _sp.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
                if r.stdout.strip():
                    dense_docs = json.loads(r.stdout.strip())
            except Exception: pass

        # 3. RRF fusion (k=60)
        K = 60
        scores = {}
        for rank, row in enumerate(bm25_rows):
            src, path, body, did = row
            key = f"bm25:{did}"
            scores[key] = {'score': 1.0/(K+rank+1), 'source': src, 'path': path or '', 'text': body.strip()[:400], 'channel':'bm25'}
        for rank, doc in enumerate(dense_docs):
            key = f"dense:{doc.get('path','')[:50]}"
            if key in scores:
                scores[key]['score'] += 1.0/(K+rank+1)
                scores[key]['channel'] = 'both'
            else:
                scores[key] = {'score': 1.0/(K+rank+1), 'source': 'chroma-code', 'path': doc.get('path',''), 'text': doc.get('text',''), 'channel':'dense'}

        ranked = sorted(scores.values(), key=lambda x: -x['score'])[:limit]
        return {'results': [{'source': r['source'], 'path': r['path'], 'text': r['text'][:400], 'channel': r['channel']} for r in ranked]}
    except Exception as e:
        return {'error': str(e)}

def tool_rag_code(query, limit=5):
    """Query code knowledge — routed through SQLite FTS (no Chroma load, crash-safe).
    Searches `code` + `code-vector` + `code-deep:*` sources in index.db via BM25."""
    try:
        conn = sqlite3.connect(os.path.expanduser('~/.surrogate/index.db'))
        kw = ' '.join(w for w in re.sub(r'[^a-zA-Z0-9ก-๙ ]', ' ', query.lower()).split() if len(w) > 2)[:200]
        rows = conn.execute("""
            SELECT d.source, d.path, substr(d.response, 1, 500)
            FROM docs_fts f JOIN docs d ON d.id=f.rowid
            WHERE f.docs_fts MATCH ?
              AND d.source IN ('code','code-vector','code-deep:dev-backend','code-deep:dev-frontend','code-deep:ops-devops','code-deep:ops-sre','code-deep:sec-appsec','code-deep:sec-cloudsec','code-deep:ai-engineering','code-deep:dev-mobile','fs-code','chroma-code-pair')
            ORDER BY bm25(docs_fts) LIMIT ?
        """, (kw, limit)).fetchall()
        conn.close()
        return {'results': [{'source': s, 'path': p or '', 'text': body.strip()[:400]} for s, p, body in rows]}
    except Exception as e:
        return {'error': str(e)}

def tool_web_fetch(url, timeout=45):
    try:
        cmd = f"""$HOME/.surrogate/state/crawler-venv/bin/python -c "
import asyncio
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
async def f():
    async with AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False)) as c:
        r = await c.arun(url='{url}', config=CrawlerRunConfig(only_text=True, word_count_threshold=30))
        print((r.markdown or '')[:8000])
asyncio.run(f())" """
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return {'content': r.stdout[:8000]}
    except Exception as e:
        return {'error': str(e)}

def tool_web_search(query, n=5):
    """Use DuckDuckGo HTML (no API key)."""
    try:
        import urllib.parse
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode('utf-8', errors='replace')
        # Extract results
        links = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([^<]+)</a>', html)[:n]
        return {'results': [{'url': u, 'title': t} for u, t in links]}
    except Exception as e:
        return {'error': str(e)}

def tool_task(prompt, max_steps=5):
    """Spawn a single sub-agent (fire-and-forget). Returns final answer."""
    sub_id = uuid.uuid4().hex[:8]
    print(f"   ↳ [sub-agent {sub_id}] spawning: {prompt[:80]}", flush=True)
    try:
        cmd = ['bash', os.path.expanduser('~/.surrogate/bin/surrogate-agent.sh'),
               '--max-steps', str(max_steps), prompt]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return {'sub_id': sub_id, 'output': r.stdout[-4000:], 'rc': r.returncode}
    except Exception as e:
        return {'error': str(e)}

def tool_orchestrate(subtasks, pattern='parallel', max_steps=5):
    """Orchestrate multiple sub-agents.
    pattern: 'parallel' (all at once, merge), 'chain' (A→B→C), 'debate' (N answers + reducer),
             'consensus' (majority vote), 'map_reduce' (split+merge)
    subtasks: list of prompts
    """
    import concurrent.futures as cf
    sid = uuid.uuid4().hex[:6]
    print(f"   ↳ [orchestrate/{pattern} {sid}] {len(subtasks)} subtasks", flush=True)

    def run_one(prompt):
        try:
            r = subprocess.run(
                ['bash', os.path.expanduser('~/.surrogate/bin/surrogate-agent.sh'),
                 '--max-steps', str(max_steps), prompt],
                capture_output=True, text=True, timeout=600
            )
            return r.stdout[-3000:]
        except Exception as e:
            return f'[error] {e}'

    if pattern == 'parallel':
        with cf.ThreadPoolExecutor(max_workers=min(len(subtasks), 5)) as ex:
            results = list(ex.map(run_one, subtasks))
        return {'pattern':'parallel', 'results': results}

    elif pattern == 'chain':
        # A.output feeds B.input, etc.
        context = ''
        results = []
        for i, p in enumerate(subtasks):
            augmented = f"Previous step output:\n{context[-2000:]}\n\nYour task: {p}" if context else p
            r = run_one(augmented)
            results.append(r)
            context += f"\n[step{i+1}] {r[-1000:]}"
        return {'pattern':'chain', 'steps': results, 'final_context': context[-3000:]}

    elif pattern == 'debate':
        # All agents answer same question independently → reducer picks
        if len(subtasks) < 2:
            return {'error':'debate needs ≥2 subtasks'}
        with cf.ThreadPoolExecutor(max_workers=min(len(subtasks), 5)) as ex:
            answers = list(ex.map(run_one, subtasks))
        # Reducer: ask LLM which is best
        reducer_prompt = "Compare these answers to the same question. Pick the best one and explain why. Be concise.\n\n"
        for i, a in enumerate(answers):
            reducer_prompt += f"--- Answer {i+1} ---\n{a[:1500]}\n\n"
        reducer_body = {
            'model': 'anthropic/claude-haiku-4.5',
            'messages': [{'role':'user','content': reducer_prompt[:12000]}],
            'temperature': 0.2, 'max_tokens': 1000,
        }
        try:
            req = urllib.request.Request(
                'https://openrouter.ai/api/v1/chat/completions',
                data=json.dumps(reducer_body).encode(),
                headers={'Content-Type':'application/json','Authorization':f'Bearer {OPENROUTER}',
                         'HTTP-Referer':'https://axentx.ai','X-Title':'Surrogate-Debate'}
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                verdict = json.load(r)['choices'][0]['message']['content']
        except Exception as e:
            verdict = f'[reducer err] {e}'
        return {'pattern':'debate', 'answers': answers, 'verdict': verdict}

    elif pattern == 'consensus':
        # N agents → majority vote on short answer
        with cf.ThreadPoolExecutor(max_workers=min(len(subtasks), 5)) as ex:
            answers = list(ex.map(run_one, subtasks))
        # Simple: return all, let caller synthesize
        return {'pattern':'consensus', 'answers': answers, 'note':'N answers returned; caller should synthesize consensus'}

    elif pattern == 'map_reduce':
        # First N-1 = map workers, last = reducer
        if len(subtasks) < 2:
            return {'error':'map_reduce needs ≥2 subtasks (last = reducer)'}
        map_tasks = subtasks[:-1]
        reduce_task = subtasks[-1]
        with cf.ThreadPoolExecutor(max_workers=min(len(map_tasks), 5)) as ex:
            map_results = list(ex.map(run_one, map_tasks))
        reduce_prompt = f"{reduce_task}\n\n=== Map outputs ===\n"
        for i, r in enumerate(map_results):
            reduce_prompt += f"[Map {i+1}]\n{r[:1500]}\n\n"
        final = run_one(reduce_prompt[:10000])
        return {'pattern':'map_reduce', 'maps': map_results, 'reduced': final}

    else:
        return {'error': f'unknown pattern: {pattern}. Use parallel/chain/debate/consensus/map_reduce'}

TODO_FILE = MEM_DIR / f'todo-{uuid.uuid4().hex[:8]}.json'
def tool_todo_add(items):
    try:
        existing = []
        if TODO_FILE.exists(): existing = json.loads(TODO_FILE.read_text())
        if isinstance(items, str): items = [items]
        for item in items:
            existing.append({'task': item, 'status': 'pending', 'ts': datetime.utcnow().isoformat()})
        TODO_FILE.write_text(json.dumps(existing, indent=2))
        return {'count': len(existing), 'file': str(TODO_FILE)}
    except Exception as e:
        return {'error': str(e)}

def tool_episode_log(key, value):
    try:
        with open(PATTERNS, 'a') as f:
            f.write(json.dumps({'ts': datetime.utcnow().isoformat(), 'key': key, 'value': value}, ensure_ascii=False) + '\n')
        return {'ok': True}
    except Exception as e:
        return {'error': str(e)}

# ═══ B1: Working Memory Buffer (PFC-inspired) ═══
WM_FILE = MEM_DIR / 'working_memory.json'
def tool_wm_set(goal=None, constraints=None, last_verify=None, ttl_turns=10):
    """Update working memory hot slot. Auto-decays after ttl_turns without reinforcement."""
    try:
        wm = {}
        if WM_FILE.exists():
            try: wm = json.loads(WM_FILE.read_text())
            except: pass
        if goal is not None: wm['goal'] = goal[:500]
        if constraints is not None: wm['constraints'] = constraints if isinstance(constraints, list) else [constraints]
        if last_verify is not None: wm['last_verify'] = last_verify[:500]
        wm['ttl_turns'] = ttl_turns
        wm['updated'] = datetime.utcnow().isoformat()
        WM_FILE.write_text(json.dumps(wm, indent=2, ensure_ascii=False))
        return {'ok': True, 'wm': wm}
    except Exception as e:
        return {'error': str(e)}

def tool_wm_get():
    """Read working memory."""
    try:
        if not WM_FILE.exists(): return {'wm': None}
        wm = json.loads(WM_FILE.read_text())
        # Auto-decay
        if wm.get('ttl_turns', 0) <= 0:
            WM_FILE.unlink()
            return {'wm': None, 'note': 'expired'}
        wm['ttl_turns'] -= 1
        WM_FILE.write_text(json.dumps(wm, indent=2, ensure_ascii=False))
        return {'wm': wm}
    except Exception as e:
        return {'error': str(e)}

TOOLS = {
    'bash': tool_bash,
    'read': tool_read,
    'write': tool_write,
    'edit': tool_edit,
    'glob': tool_glob,
    'grep': tool_grep,
    'rag_query': tool_rag_query,
    'rag_code': tool_rag_code,
    'web_fetch': tool_web_fetch,
    'web_search': tool_web_search,
    'task': tool_task,
    'orchestrate': tool_orchestrate,
    'todo_add': tool_todo_add,
    'episode_log': tool_episode_log,
}

# ═══ LLM call with 429 backoff + self-healing fallback + budget check ═══
def check_budget():
    """Return True if under daily budget ($2/day default). Caller aborts if False."""
    import time as _t
    cache = Path(os.path.expanduser('~/.surrogate/state/openrouter-budget-cache.json'))
    # Cache balance check for 5 min (reduce API calls)
    try:
        if cache.exists() and _t.time() - cache.stat().st_mtime < 300:
            d = json.loads(cache.read_text())
        else:
            req = urllib.request.Request('https://openrouter.ai/api/v1/auth/key',
                headers={'Authorization': f'Bearer {OPENROUTER}'})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.load(r).get('data', {})
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({'usage': d.get('usage',0), 'ts': _t.time()}))
        # Check today's marker
        today_f = Path(os.path.expanduser('~/.surrogate/state/openrouter-today-start.txt'))
        today_str = datetime.now().strftime('%Y-%m-%d')
        if not today_f.exists() or today_f.read_text().split(':')[0] != today_str:
            today_f.parent.mkdir(parents=True, exist_ok=True)
            today_f.write_text(f"{today_str}:{d.get('usage', 0)}")
            return True
        parts = today_f.read_text().split(':')
        today_start = float(parts[1]) if len(parts) > 1 else 0
        today_spent = d.get('usage', 0) - today_start
        MAX_DAILY = float(os.environ.get('OPENROUTER_MAX_DAILY_USD', '2.0'))
        return today_spent < MAX_DAILY
    except Exception:
        return True  # fail-open (don't block on check failure)

def llm(messages, model=None, max_tokens=3000, temperature=0.1, retries=0):
    if not check_budget():
        raise RuntimeError(f"Daily budget exceeded. Stopped to prevent burn.")

    model = model or pick_model(TASK)
    import time as _t

    # Build provider ladder: GitHub Models (free 3-token pool) → OpenRouter pool → free models
    GH_POOL = [t.strip() for t in os.environ.get('GITHUB_TOKEN_POOL','').split(',') if t.strip()]
    ladder = []
    # Tier 1: GitHub Models — free 150 req/day via 3 PATs
    for tok in GH_POOL[:4]:
        ladder.append(('gh-models', 'https://models.github.ai/inference/chat/completions', 'openai/gpt-4o-mini', tok))
    # Tier 2: OpenRouter free models (primary key)
    if OPENROUTER:
        for mdl in ['qwen/qwen3-coder:free', 'meta-llama/llama-3.3-70b-instruct:free', 'qwen/qwen3-next-80b-a3b-instruct:free']:
            ladder.append(('or-free', 'https://openrouter.ai/api/v1/chat/completions', mdl, OPENROUTER))
        # Tier 3: OpenRouter cheap paid
        ladder.append(('or-cheap', 'https://openrouter.ai/api/v1/chat/completions', 'qwen/qwen3-235b-a22b-2507', OPENROUTER))
        # Tier 4: OpenRouter premium
        ladder.append(('or-prem', 'https://openrouter.ai/api/v1/chat/completions', 'anthropic/claude-haiku-4.5', OPENROUTER))

    def _call(url, mdl, key, kind):
        body = {'model': mdl, 'messages': messages, 'temperature': temperature, 'max_tokens': max_tokens, 'stream': False}
        headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'}
        if 'openrouter' in url:
            headers['HTTP-Referer'] = 'https://axentx.ai'
            headers['X-Title'] = 'Surrogate-Agent'
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)['choices'][0]['message']['content']

    # Try each provider/model with 1 short retry on 429
    for kind, url, mdl, key in ladder:
        for attempt in range(2):
            try:
                return _call(url, mdl, key, kind)
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    _t.sleep(3)
                    continue
                break
            except Exception:
                break
    raise RuntimeError(f"All {len(ladder)} providers exhausted")

def enforce_male(text):
    text = re.sub(r'ดิฉัน', 'ผม', text)
    text = re.sub(r'ฉัน(?![a-zA-Z])', 'ผม', text)
    text = re.sub(r'ค่ะ', 'ครับ', text)
    text = re.sub(r'นะคะ', 'นะครับ', text)
    text = re.sub(r'คะ', 'ครับ', text)
    text = re.sub(r'จ้า', 'ครับ', text)
    # Scrub provider leaks
    for leak in ['OpenRouter','openrouter','Qwen','qwen3','qwen2.5','Claude Haiku','claude-haiku','GPT','Gemini','gemini-','Ollama','ollama','Anthropic','anthropic','RunPod','vLLM','LoRA','DPO','ORPO']:
        text = text.replace(leak, '[internal]')
    return text

# ═══ AGENT LOOP ═══
AGENT_SYS = SYS_PROMPT + """

## TOOL USE (agentic mode — Claude-Code parity)

Available tools (call via ```tool JSON fence):

| tool | args | purpose |
|------|------|---------|
| `bash` | `{cmd, timeout?, cwd?}` | shell exec |
| `read` | `{path, limit?, offset?}` | file/dir read |
| `write` | `{path, content}` | create/overwrite file |
| `edit` | `{path, old_string, new_string}` | surgical edit (unique match) |
| `glob` | `{pattern, path?}` | file glob search |
| `grep` | `{pattern, path?, glob?, context?}` | content search |
| `rag_query` | `{query, limit?, source_filter?}` | search 103k SQLite FTS (blogs/CVE/business) |
| `rag_code` | `{query, limit?}` | search 506k ChromaDB code chunks (PREFER for code questions) |
| `web_fetch` | `{url, timeout?}` | fetch markdown from URL |
| `web_search` | `{query, n?}` | DuckDuckGo search |
| `task` | `{prompt, max_steps?}` | spawn SINGLE sub-agent |
| `orchestrate` | `{subtasks[], pattern, max_steps?}` | multi-agent coord: parallel/chain/debate/consensus/map_reduce |
| `todo_add` | `{items}` | track todos |
| `episode_log` | `{key, value}` | save learned pattern |

**orchestrate patterns:**
- `parallel` — N agents same time, returns all results (for independent subtasks)
- `chain` — A→B→C sequential (output feeds next input)
- `debate` — N agents same Q, reducer picks best (for quality)
- `consensus` — N agents vote (caller synthesizes)
- `map_reduce` — first N-1 = maps, last = reducer (for aggregation)

Call syntax (exactly one tool per assistant turn, nothing else):
```tool
{"name": "tool_name", "args": {"key": "value"}}
```

Final answer = NO ```tool block (plain text only).

Hard rules:
- Don't guess. Use `rag_query` or `web_fetch` to ground facts.
- Split big work via `task` (sub-agents run parallel).
- Before editing code: `read` + `grep` to verify pattern.
- If error from a tool: try alternative approach, don't repeat same call.
- Max tool_calls in this run: {max_steps}
"""

messages = [
    {'role': 'system', 'content': AGENT_SYS.replace('{max_steps}', str(MAX_STEPS))},
    {'role': 'user', 'content': TASK}
]

def log_episode(task, final, steps):
    """Quality gate: skip shallow failures to avoid polluting DPO training data."""
    # Reject: step=1 AND (error OR timeout OR empty)
    final_lower = (final or '').lower()
    is_failure = any(marker in final_lower for marker in ['[error', '[timeout', 'llm err', 'http error'])
    if steps <= 1 and (is_failure or not final.strip()):
        return  # skip — not worth logging
    try:
        with open(EPISODES, 'a') as f:
            f.write(json.dumps({
                'ts': datetime.utcnow().isoformat(),
                'task': task[:500],
                'steps': steps,
                'final': final[:3000],
                'quality': 'success' if not is_failure else 'failure',
            }, ensure_ascii=False) + '\n')
    except: pass

print(f"🤖 Surrogate | task: {TASK[:80]}")
print(f"   max_steps={MAX_STEPS}")

step = 0
final_answer = None
while step < MAX_STEPS:
    step += 1
    try:
        resp = llm(messages)
    except Exception as e:
        print(f"❌ LLM err (self-heal exhausted): {e}")
        log_episode(TASK, f'[error: {e}]', step)
        sys.exit(1)
    resp = enforce_male(resp)

    tool_match = re.search(r'```tool\s*\n(.+?)\n```', resp, re.DOTALL)
    if not tool_match:
        final_answer = resp
        print(f"\n━━━━ Answer (step {step}) ━━━━\n{resp}")
        log_episode(TASK, resp, step)
        break

    try:
        call = json.loads(tool_match.group(1))
        tool_name = call['name']
        args = call.get('args', {})
    except Exception as e:
        print(f"⚠️ bad tool syntax: {e}"); break

    if tool_name not in TOOLS:
        print(f"⚠️ unknown tool: {tool_name}")
        messages.append({'role': 'assistant', 'content': resp})
        messages.append({'role': 'user', 'content': f'[error] unknown tool {tool_name}. Available: {list(TOOLS.keys())}'})
        continue

    print(f"🔧 [{step}/{MAX_STEPS}] {tool_name}({json.dumps(args, ensure_ascii=False)[:150]})")
    try:
        result = TOOLS[tool_name](**args)
    except Exception as e:
        result = {'error': f'{type(e).__name__}: {e}'}
    result_str = json.dumps(result, ensure_ascii=False)[:3000]
    preview = result_str[:180].replace('\n',' ')
    print(f"   → {preview}...")

    messages.append({'role': 'assistant', 'content': resp})
    messages.append({'role': 'user', 'content': f'[tool result: {tool_name}]\n{result_str}'})

if final_answer is None:
    print(f"\n⚠️ max_steps reached without final answer")
    log_episode(TASK, '[timeout]', step)
PYEOF

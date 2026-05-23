#!/usr/bin/env bash
# Episode consolidation — nightly summarize episodes → patterns → Graphiti + DPO training data
#
# Input:  ~/.surrogate/state/surrogate-memory/episodes.jsonl
# Output:
#   1. ~/.surrogate/state/surrogate-memory/patterns.jsonl (learned patterns)
#   2. ~/.surrogate/index.db (source='surrogate-episodes') — pattern ingested for RAG
#   3. ~/axentx/surrogate/data/training-jsonl/dpo-pairs.jsonl (user+reply for future LoRA)
#   4. FalkorDB graph (episodic → semantic bitemporal edges)
set -u
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

MEM="$HOME/.surrogate/state/surrogate-memory"
LOG="$HOME/.surrogate/logs/surrogate-consolidate.log"
CHECKPOINT="$MEM/consolidate.checkpoint"
mkdir -p "$(dirname "$LOG")" "$MEM"

python3 <<'PYEOF' 2>>"$LOG"
import json, os, sqlite3, urllib.request, hashlib, subprocess
from datetime import datetime
from pathlib import Path

MEM = Path(os.path.expanduser('~/.surrogate/state/surrogate-memory'))
EP = MEM / 'episodes.jsonl'
PAT = MEM / 'patterns.jsonl'
CKPT = MEM / 'consolidate.checkpoint'
DPO = Path(os.path.expanduser('~/axentx/surrogate/data/training-jsonl/dpo-pairs.jsonl'))
DPO.parent.mkdir(parents=True, exist_ok=True)

OR_KEY = os.environ.get('OPENROUTER_API_KEY','')

# Checkpoint: last consolidated line #
last_line = 0
if CKPT.exists():
    try: last_line = int(CKPT.read_text().strip())
    except: last_line = 0

if not EP.exists():
    print("[consolidate] no episodes yet")
    exit()

lines = EP.read_text(errors='replace').splitlines()
new_lines = lines[last_line:]
if not new_lines:
    print(f"[consolidate] no new since line {last_line}")
    exit()

print(f"[consolidate] processing {len(new_lines)} new episodes")

episodes = []
for line in new_lines:
    try: episodes.append(json.loads(line))
    except: continue

# Step 1: Append to DPO training data (for future RunPod LoRA)
with open(DPO, 'a') as f:
    for ep in episodes:
        if not ep.get('task') or not ep.get('final'): continue
        if '[error' in ep.get('final','') or '[timeout' in ep.get('final',''): continue
        pair = {
            'instruction': ep['task'][:500],
            'input': '',
            'output': ep['final'][:3000],
            'source': 'surrogate-episode',
            'timestamp': ep.get('ts', datetime.utcnow().isoformat()),
        }
        f.write(json.dumps(pair, ensure_ascii=False) + '\n')

# Step 2: Summarize batches → pattern (every 10 episodes)
def summarize_batch(batch):
    if not OR_KEY: return None
    prompt = "Below are recent Surrogate agent episodes (task + final answer). Extract 2-3 concise reusable patterns — what kind of tasks + what approaches worked. Output as bullet list. Thai OK.\n\n"
    for i, ep in enumerate(batch):
        prompt += f"--- Episode {i+1} ---\nTask: {ep.get('task','')[:300]}\nAnswer: {ep.get('final','')[:500]}\n\n"
    body = {
        'model': 'google/gemini-2.5-flash',  # cheap, good summarizer
        'messages': [{'role':'user','content': prompt[:15000]}],
        'temperature': 0.2, 'max_tokens': 600,
    }
    try:
        req = urllib.request.Request(
            'https://openrouter.ai/api/v1/chat/completions',
            data=json.dumps(body).encode(),
            headers={'Content-Type':'application/json','Authorization':f'Bearer {OR_KEY}',
                     'HTTP-Referer':'https://axentx.ai','X-Title':'Surrogate-Consolidate'}
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        return d['choices'][0]['message']['content']
    except Exception as e:
        print(f"[consolidate] llm err: {e}")
        return None

# Batch into groups of 10
patterns_added = 0
for batch_start in range(0, len(episodes), 10):
    batch = episodes[batch_start:batch_start+10]
    summary = summarize_batch(batch)
    if not summary: continue
    pattern = {
        'ts': datetime.utcnow().isoformat(),
        'episodes_range': [batch_start, batch_start+len(batch)-1],
        'pattern_summary': summary[:2000],
        'n_episodes': len(batch),
    }
    with open(PAT, 'a') as f:
        f.write(json.dumps(pattern, ensure_ascii=False) + '\n')
    patterns_added += 1

# Step 3: Ingest patterns into index.db so future RAG finds them
conn = sqlite3.connect(os.path.expanduser('~/.surrogate/index.db'))
conn.execute('PRAGMA journal_mode=WAL')
cur = conn.cursor()
if PAT.exists():
    for line in PAT.read_text().splitlines()[-50:]:
        try: p = json.loads(line)
        except: continue
        cur.execute(
            "INSERT OR IGNORE INTO docs (source, project, path, topic, instruction, response, ts) VALUES (?,?,?,?,?,?,?)",
            ('surrogate-episodes', 'surrogate', 'memory:pattern', 'learned-pattern',
             f"pattern from {p.get('n_episodes','?')} episodes",
             p.get('pattern_summary','')[:2500],
             p.get('ts', datetime.utcnow().isoformat()))
        )
conn.commit()
conn.close()


# Step 3b: Write patterns as graph nodes in FalkorDB (fix stagnant graph)
import subprocess
sock_r = subprocess.run(['/usr/bin/find','/var/folders','/tmp','-name','redis.socket','-type','s'], capture_output=True, text=True)
sock = sock_r.stdout.strip().split('\n')[0] if sock_r.stdout else None
if sock:
    # Each pattern → Pattern node + relationships
    if PAT.exists():
        for line in PAT.read_text().splitlines()[-patterns_added:]:
            try: p = json.loads(line)
            except: continue
            pid = hashlib.md5(p.get('pattern_summary','')[:200].encode()).hexdigest()[:12]
            title = p.get('pattern_summary','')[:100].replace("'", "").replace(chr(10),' ')
            ts = p.get('ts','')
            cypher = f"MERGE (p:Pattern {{id:'{pid}'}}) SET p.title='{title}', p.ts='{ts}', p.n_episodes={p.get('n_episodes',0)}"
            try:
                subprocess.run(['/opt/homebrew/bin/redis-cli','-s',sock,'GRAPH.QUERY','ashira',cypher], capture_output=True, timeout=5)
            except: pass
    # Each episode → Episode node linked to Pattern
    for ep in episodes[-20:]:
        eid = hashlib.md5(ep.get('task','')[:200].encode()).hexdigest()[:12]
        task = ep.get('task','')[:80].replace("'","").replace(chr(10),' ')
        quality = 'success' if '[error' not in ep.get('final','') and '[timeout' not in ep.get('final','') else 'failed'
        cypher = f"MERGE (e:Episode {{id:'{eid}'}}) SET e.task='{task}', e.quality='{quality}', e.ts='{ep.get('ts','')}'"
        try:
            subprocess.run(['/opt/homebrew/bin/redis-cli','-s',sock,'GRAPH.QUERY','ashira',cypher], capture_output=True, timeout=5)
        except: pass
    print('[consolidate] wrote patterns + episodes to FalkorDB')
import hashlib  # make sure imported

# Update checkpoint
CKPT.write_text(str(len(lines)))
print(f"[consolidate] added {patterns_added} patterns from {len(episodes)} episodes. DPO pairs grown.")
PYEOF

echo "[$(date '+%H:%M:%S')] consolidate done" >> "$LOG"

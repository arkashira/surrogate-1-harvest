#!/usr/bin/env bash
# Knowledge Graph Batch Ingester — the missing piece that makes ALL agent work
# feed into the shared knowledge graph (FalkorDB) for self-learning + Surrogate-1 training.
#
# Sources scanned every 5 min:
#   - Worker outputs        (~/.hermes/workspace/dev-cloud-*/<pid>_*.md)       → Output node
#   - Validator results     (*.validation.json)                                 → Validation node + edge
#   - Reviewer verdicts     (*.review.json)                                     → Review node + edge
#   - Tournament winners    (tournaments/*.json)                                → Tournament node + edge
#   - Synthesis outputs     (dev-cloud-synthesis/*.md)                          → Synthesis node + edge
#   - Git commits           (axentx/*/log)                                      → Commit node + edge
#   - Prompt deltas         (worker-prompt-deltas.jsonl)                        → AntiPattern/LearnedRule node
#   - Healer actions        (healer/*.apply.log)                                → HealerAction node
#   - Research docs         (research/*.md)                                     → Research node
#   - Decisions             (decisions/*.md, backlog/*.md)                      → Decision node
#
# Graph schema:
#   (Priority {id,project,title}) -[:HAS_OUTPUT]-> (Output {basename,provider,bytes,created_at})
#   (Output) -[:VALIDATED]-> (Validation {status,tests_passed})
#   (Output) -[:REVIEWED]-> (Review {verdict,quality_score,bugs_found})
#   (Priority) -[:WON_TOURNAMENT]-> (Output) via tournaments
#   (Output) -[:SYNTHESIZED_INTO]-> (Synthesis)
#   (Priority) -[:COMMITTED_AS]-> (Commit {sha,branch,project})
#   (Review {verdict=rework}) -[:GENERATED]-> (LearnedRule)
#   (Priority) -[:HAS_INSIGHT]-> (Insight)
#
# State: tracks last-ingested mtime per source to avoid re-ingesting same data.
set -u

LOG="$HOME/.claude/logs/knowledge-graph-ingest.log"
STATE="$HOME/.claude/state/kg-ingest-state.json"
VENV_PY="$HOME/.claude/state/validator-venv/bin/python3"
mkdir -p "$(dirname "$LOG")" "$(dirname "$STATE")"
[[ ! -f "$STATE" ]] && echo '{}' > "$STATE"

# Use redislite Python client directly (persistent .rdb, socket-independent)
VENV_PY="$HOME/.claude/venv/bin/python"

export STATE LOG
"$VENV_PY" <<'PYEOF' 2>>"$LOG"
import json, os, re, sys, subprocess
from pathlib import Path
from datetime import datetime, timezone
from redislite.falkordb_client import FalkorDB

HOME = Path.home()
STATE_PATH = Path(os.environ['STATE'])
GRAPH = 'ashira'

_db = FalkorDB(dbfilename=str(HOME / ".claude/graph-db.rdb"))
_g = _db.select_graph(GRAPH)

try: state = json.loads(STATE_PATH.read_text())
except: state = {}

def cypher(query, params=None):
    """Execute Cypher via FalkorDB Python client."""
    try:
        _g.query(query)
        return "", 0
    except Exception as e:
        return str(e), 1

def q_esc(s):
    """Escape string for Cypher literal."""
    if s is None: return 'null'
    s = str(s).replace('\\', '\\\\').replace("'", "\\'").replace('\n','\\n').replace('\r','')
    return f"'{s[:300]}'"

inserts = {'Output':0, 'Validation':0, 'Review':0, 'Tournament':0, 'Synthesis':0,
           'Commit':0, 'LearnedRule':0, 'Decision':0, 'Research':0, 'HealerAction':0,
           'Priority':0}

# ── 1. Priorities ──
pri_file = HOME / '.hermes/workspace/swarm-shared/priority.json'
if pri_file.exists():
    try:
        pri_data = json.loads(pri_file.read_text())
        for p in pri_data.get('priorities', []):
            pid = p.get('id','')
            if not pid: continue
            cypher(
                f"MERGE (p:Priority {{id: {q_esc(pid)}}}) "
                f"SET p.project={q_esc(p.get('project',''))}, "
                f"  p.title={q_esc(p.get('title','')[:200])}, "
                f"  p.status={q_esc(p.get('status',''))}"
            )
            inserts['Priority'] += 1
    except Exception as e:
        print(f"[kg] priority fail: {e}")

# ── 2. Worker outputs ──
last_out = float(state.get('outputs_mtime', 0))
new_out_mtime = last_out
for out_dir in (HOME / '.hermes/workspace').glob('dev-cloud-*'):
    if not out_dir.is_dir(): continue
    provider = out_dir.name.replace('dev-cloud-', '')
    for md in out_dir.glob('*.md'):
        if md.stat().st_mtime <= last_out: continue
        basename = md.stem
        # Parse priority id: pXX_YYYY-MM-DD_HH-MM
        m = re.match(r'^(p\d+)_', basename)
        if not m: continue
        pid = m.group(1)
        size = md.stat().st_size
        created = datetime.fromtimestamp(md.stat().st_mtime, tz=timezone.utc).isoformat()

        cypher(
            f"MERGE (o:Output {{basename: {q_esc(basename)}}}) "
            f"SET o.provider={q_esc(provider)}, o.bytes={size}, o.created_at={q_esc(created)} "
            f"WITH o "
            f"MATCH (p:Priority {{id: {q_esc(pid)}}}) "
            f"MERGE (p)-[:HAS_OUTPUT]->(o)"
        )
        inserts['Output'] += 1
        new_out_mtime = max(new_out_mtime, md.stat().st_mtime)
state['outputs_mtime'] = new_out_mtime

# ── 3. Validations ──
last_val = float(state.get('validations_mtime', 0))
new_val_mtime = last_val
for vf in (HOME / '.hermes/workspace/qwen-coder-reviews').glob('*.validation.json'):
    if vf.stat().st_mtime <= last_val: continue
    try:
        d = json.loads(vf.read_text())
    except: continue
    basename = vf.stem.replace('.validation','')
    cypher(
        f"MERGE (v:Validation {{output_basename: {q_esc(basename)}}}) "
        f"SET v.status={q_esc(d.get('status',''))}, "
        f"  v.tests_passed={'true' if d.get('tests_passed') else 'false'}, "
        f"  v.tests_failed={d.get('tests_failed_count',0)} "
        f"WITH v "
        f"MATCH (o:Output {{basename: {q_esc(basename)}}}) "
        f"MERGE (o)-[:VALIDATED]->(v)"
    )
    inserts['Validation'] += 1
    new_val_mtime = max(new_val_mtime, vf.stat().st_mtime)
state['validations_mtime'] = new_val_mtime

# ── 4. Reviews ──
last_rev = float(state.get('reviews_mtime', 0))
new_rev_mtime = last_rev
for rf in (HOME / '.hermes/workspace/qwen-coder-reviews').glob('*.review.json'):
    if rf.stat().st_mtime <= last_rev: continue
    try:
        txt = rf.read_text()
        m = re.search(r'\{.*\}', txt, re.DOTALL)
        d = json.loads(m.group(0)) if m else {}
    except: continue
    basename = rf.stem.replace('.review','')
    bugs_n = len(d.get('bugs') or [])
    halluc_n = len(d.get('hallucinations') or [])
    cypher(
        f"MERGE (r:Review {{output_basename: {q_esc(basename)}}}) "
        f"SET r.verdict={q_esc(d.get('verdict',''))}, "
        f"  r.quality={d.get('quality_score',0)}, "
        f"  r.bugs={bugs_n}, r.hallucinations={halluc_n} "
        f"WITH r "
        f"MATCH (o:Output {{basename: {q_esc(basename)}}}) "
        f"MERGE (o)-[:REVIEWED]->(r)"
    )
    inserts['Review'] += 1
    new_rev_mtime = max(new_rev_mtime, rf.stat().st_mtime)
state['reviews_mtime'] = new_rev_mtime

# ── 5. Tournaments ──
last_t = float(state.get('tournaments_mtime', 0))
new_t_mtime = last_t
for tf in (HOME / '.hermes/workspace/tournaments').glob('*.json'):
    if tf.stat().st_mtime <= last_t: continue
    try: td = json.loads(tf.read_text())
    except: continue
    pid = td.get('prio','')
    winner = td.get('winner','') or ''
    if pid and winner:
        winner_base = Path(winner).stem
        cypher(
            f"MATCH (p:Priority {{id: {q_esc(pid)}}}) "
            f"MATCH (o:Output {{basename: {q_esc(winner_base)}}}) "
            f"MERGE (p)-[:WON_TOURNAMENT]->(o)"
        )
    inserts['Tournament'] += 1
    new_t_mtime = max(new_t_mtime, tf.stat().st_mtime)
state['tournaments_mtime'] = new_t_mtime

# ── 6. Synthesis outputs ──
last_s = float(state.get('synthesis_mtime', 0))
new_s_mtime = last_s
for sf in (HOME / '.hermes/workspace/dev-cloud-synthesis').glob('*.md'):
    if sf.stat().st_mtime <= last_s: continue
    basename = sf.stem
    m = re.match(r'^(p\d+)_', basename)
    if not m: continue
    pid = m.group(1)
    cypher(
        f"MERGE (s:Synthesis {{basename: {q_esc(basename)}}}) "
        f"SET s.bytes={sf.stat().st_size} "
        f"WITH s "
        f"MATCH (p:Priority {{id: {q_esc(pid)}}}) "
        f"MERGE (p)-[:SYNTHESIZED_INTO]->(s)"
    )
    inserts['Synthesis'] += 1
    new_s_mtime = max(new_s_mtime, sf.stat().st_mtime)
state['synthesis_mtime'] = new_s_mtime

# ── 7. Git commits on axentx (last 24h hermes-auto) ──
last_commit_time = state.get('commits_last_sha', '')
for proj in ('Costinel','Vanguard','arkship','surrogate','workio'):
    proj_dir = HOME / f'axentx/{proj}'
    if not (proj_dir / '.git').exists(): continue
    try:
        log = subprocess.run(
            ['git','-C',str(proj_dir),'log','--since=24 hours ago',
             '--pretty=format:%H|%s|%ci','--grep=feat\\|chore(hermes)'],
            capture_output=True, text=True, timeout=10
        )
        for line in (log.stdout or '').splitlines():
            if '|' not in line: continue
            parts = line.split('|', 2)
            if len(parts) < 3: continue
            sha, msg, when = parts
            # Extract priority id from commit message: "feat(p1):"
            mp = re.search(r'feat\((p\d+)\)', msg) or re.search(r'^(p\d+):', msg)
            pid = mp.group(1) if mp else None
            cypher(
                f"MERGE (c:Commit {{sha: {q_esc(sha)}}}) "
                f"SET c.project={q_esc(proj)}, c.msg={q_esc(msg[:160])}, c.when={q_esc(when)}"
            )
            if pid:
                cypher(
                    f"MATCH (p:Priority {{id: {q_esc(pid)}}}) "
                    f"MATCH (c:Commit {{sha: {q_esc(sha)}}}) "
                    f"MERGE (p)-[:COMMITTED_AS]->(c)"
                )
            inserts['Commit'] += 1
    except Exception as e:
        print(f"[kg] commit scan fail {proj}: {e}")

# ── 8. Prompt deltas (learned rules / anti-patterns) ──
deltas_file = HOME / '.claude/memory/worker-prompt-deltas.jsonl'
last_delta_line = state.get('deltas_line', 0)
if deltas_file.exists():
    lines = deltas_file.read_text().splitlines()
    for i, line in enumerate(lines[last_delta_line:], last_delta_line):
        try: dd = json.loads(line)
        except: continue
        rule_id = f"rule_{i}"
        pid = dd.get('priority','')
        src = dd.get('source','')
        content = dd.get('prompt_addition','')[:200]
        cypher(
            f"MERGE (l:LearnedRule {{id: {q_esc(rule_id)}}}) "
            f"SET l.source={q_esc(src)}, l.content={q_esc(content)}, l.ts={q_esc(dd.get('ts',''))}"
        )
        if pid:
            cypher(
                f"MATCH (p:Priority {{id: {q_esc(pid)}}}) "
                f"MATCH (l:LearnedRule {{id: {q_esc(rule_id)}}}) "
                f"MERGE (p)-[:HAS_LEARNED_RULE]->(l)"
            )
        inserts['LearnedRule'] += 1
    state['deltas_line'] = len(lines)

# ── 9. Research + decisions ──
for src_name, src_path, label in (
    ('research', HOME/'.hermes/workspace/swarm-shared/research', 'Research'),
    ('decisions', HOME/'.hermes/workspace/swarm-shared/decisions', 'Decision'),
    ('backlog', HOME/'.hermes/workspace/swarm-shared/backlog', 'Decision'),
):
    if not src_path.exists(): continue
    last_m = float(state.get(f'{src_name}_mtime', 0))
    new_m = last_m
    for f in src_path.glob('*.md'):
        if f.stat().st_mtime <= last_m: continue
        fname = f.stem[:120]
        cypher(
            f"MERGE (d:{label} {{name: {q_esc(fname)}}}) "
            f"SET d.bytes={f.stat().st_size}, d.source={q_esc(src_name)}"
        )
        inserts['Research' if label=='Research' else 'Decision'] += 1
        new_m = max(new_m, f.stat().st_mtime)
    state[f'{src_name}_mtime'] = new_m

# Save state
STATE_PATH.write_text(json.dumps(state, indent=2))

# Log summary
total = sum(inserts.values())
print(f"[kg] total={total} " + ' '.join(f'{k}={v}' for k,v in inserts.items() if v>0))

# Get node + edge counts
total_q, _ = cypher("MATCH (n) RETURN count(n) as n")
edges_q, _ = cypher("MATCH ()-[r]->() RETURN count(r) as r")
print(f"[kg] graph state: {total_q.splitlines()[-2] if total_q else '?'} | edges: {edges_q.splitlines()[-2] if edges_q else '?'}")
PYEOF

echo "[$(date '+%H:%M:%S')] kg-ingest done" >> "$LOG"

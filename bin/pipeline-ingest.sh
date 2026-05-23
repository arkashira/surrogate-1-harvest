#!/usr/bin/env bash
# Ingest pipeline outputs into SQLite FTS + auto-commit user code.
set -u
SHARED="$HOME/.hermes/workspace/swarm-shared"
LOG="$HOME/.claude/logs/pipeline-ingest.log"
mkdir -p "$(dirname "$LOG")"

echo "[$(date '+%H:%M:%S')] ingest tick" >> "$LOG"

python3 <<'PY' >> "$LOG" 2>&1
import sqlite3, os, glob, time, datetime
DB = os.path.expanduser('~/.claude/index.db')
if not os.path.exists(DB):
    print("no index.db"); exit(0)
conn = sqlite3.connect(DB)
cur = conn.cursor()

SHARED = os.path.expanduser('~/.hermes/workspace/swarm-shared')
added = 0
cutoff = time.time() - 1800  # last 30 min
for root in ['decisions', 'backlog', 'priority', 'sprint', 'retro', 'hr-monitor']:
    rp = os.path.join(SHARED, root)
    if not os.path.isdir(rp): continue
    for fp in glob.glob(os.path.join(rp, '*')):
        if not os.path.isfile(fp): continue
        if os.path.getmtime(fp) < cutoff: continue
        try:
            with open(fp, encoding='utf-8') as fh:
                content = fh.read()[:50000]
            base = os.path.basename(fp)
            topic = root
            project = 'pipeline'
            if '_' in base:
                parts = base.split('_', 3)
                if len(parts) >= 3: project = parts[2]
            cur.execute("""INSERT OR REPLACE INTO docs(source,project,path,topic,instruction,response,ts)
                          VALUES (?,?,?,?,?,?,?)""",
                        ('pipeline', project, fp, topic, base, content,
                         datetime.datetime.now().isoformat()))
            added += 1
        except Exception as e:
            print(f"skip {fp}: {e}")
conn.commit()

# Rebuild FTS if we added rows
if added > 0:
    try:
        cur.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
        conn.commit()
    except Exception as e:
        print(f"fts rebuild: {e}")
print(f"[{datetime.datetime.now().strftime('%H:%M')}] indexed {added} pipeline docs")
PY

# Auto-commit user-code writes
bash /opt/surrogate-1-harvest/bin/axentx-auto-commit.sh >> "$LOG" 2>&1 || true

# Rotate log if too big
[[ -f "$LOG" ]] && LINES=$(wc -l < "$LOG") && [[ $LINES -gt 10000 ]] && {
    tail -5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
}

# Also index dev sandbox contents so reviewers can see them
python3 <<'PY' >> "$LOG" 2>&1
import sqlite3, os, glob, time, datetime
DB = os.path.expanduser('~/.claude/index.db')
if not os.path.exists(DB): exit(0)
conn = sqlite3.connect(DB)
cur = conn.cursor()
added = 0
cutoff = time.time() - 1800
for proj in ['Costinel','Vanguard','arkship','surrogate','workio']:
    sandbox = f'/Users/Ashira/axentx/{proj}/.hermes-dev-sandbox'
    if not os.path.isdir(sandbox): continue
    for fp in glob.glob(f'{sandbox}/**/*', recursive=True):
        if not os.path.isfile(fp): continue
        if os.path.getmtime(fp) < cutoff: continue
        try:
            content = open(fp, encoding='utf-8').read()[:50000]
            cur.execute("""INSERT OR REPLACE INTO docs(source,project,path,topic,instruction,response,ts)
                          VALUES (?,?,?,?,?,?,?)""",
                        ('dev-sandbox', proj, fp, 'sandbox', os.path.basename(fp), content,
                         datetime.datetime.now().isoformat()))
            added += 1
        except: pass
conn.commit()
if added: print(f"sandbox indexed: {added}")
PY

# Auto-ingest scraped JSONL files from today into SQLite FTS
TODAY_JSONL=$HOME/axentx/surrogate/data/training-jsonl/$(date +%Y-%m-%d).jsonl
if [[ -f "$TODAY_JSONL" ]]; then
    python3 /opt/surrogate-1-harvest/bin/ingest-jsonl-to-sqlite.py "$TODAY_JSONL" >> "$LOG" 2>&1 || true
fi
# Also pick up raw scrape output (rss, arxiv, hf-papers, github)
for src_dir in rss arxiv hf-papers github reddit; do
    TODAY_SRC=$HOME/axentx/surrogate/data/raw/$src_dir/$(date +%Y-%m-%d).jsonl
    [[ -f "$TODAY_SRC" ]] && python3 /opt/surrogate-1-harvest/bin/ingest-jsonl-to-sqlite.py "$TODAY_SRC" >> "$LOG" 2>&1 || true
done

#!/usr/bin/env bash
# Mythos-level domain scraper. Deep ingest from 30+ master-class repos per domain.
# Usage: scrape-mythos-domain.sh <coding|ops|ai-engineering|cloud|ai-agent>
set -u
DOMAIN="${1:?domain required — coding|ops|ai-engineering|cloud|ai-agent}"
CATALOG="$HOME/.hermes/config/mythos-domains.json"
WORK="/tmp/mythos-$DOMAIN"
SEEN="$HOME/.claude/state/mythos-seen-$DOMAIN.txt"
LOG="$HOME/.claude/logs/mythos-$DOMAIN.log"
MAX_REPOS=10          # deep per repo
MAX_FILES=200         # 200 files per repo = comprehensive

mkdir -p "$WORK" "$(dirname "$SEEN")" "$(dirname "$LOG")"
touch "$SEEN"

REPOS=$(python3 -c "
import json
d = json.load(open('$CATALOG'))
for r in d.get('$DOMAIN',{}).get('repos',[]): print(r)
")
[[ -z "$REPOS" ]] && { echo "domain not found"; exit 1; }

echo "[$(date '+%Y-%m-%d %H:%M')] mythos:$DOMAIN start" | tee -a "$LOG"

COUNT=0
for REPO in $REPOS; do
    [[ $COUNT -ge $MAX_REPOS ]] && break
    FREE=$(df -g ~ | tail -1 | awk '{print $4}')
    [[ "$FREE" -lt 5 ]] && { echo "disk low" >> "$LOG"; break; }
    grep -qxF "$REPO" "$SEEN" && continue
    
    DIR="$WORK/${REPO//\//_}"
    echo "  [$(date +%H:%M)] $REPO" | tee -a "$LOG"
    git clone --depth 1 --filter=blob:limit=200k "https://github.com/$REPO.git" "$DIR" 2>>"$LOG" || { echo "$REPO" >> "$SEEN"; continue; }
    
    python3 <<PY 2>>"$LOG"
import os, glob, sqlite3, datetime
from pathlib import Path
DB = str(Path.home() / '.claude/index.db')
ROOT, REPO, DOMAIN = "$DIR", "$REPO", "$DOMAIN"
conn = sqlite3.connect(DB)
cur = conn.cursor()

# Master-level capture: docs + source + configs + examples
PATTERNS = [
    'README*','*.md','*.mdx','docs/**/*.md','docs/**/*.mdx',
    'CONTRIBUTING*','ARCHITECTURE*','DESIGN*',
    # Source (actual implementations to learn from)
    '*.py','src/**/*.py','lib/**/*.py','examples/**/*.py','tutorial/**/*',
    '*.ts','*.tsx','src/**/*.ts','src/**/*.tsx','examples/**/*.ts',
    '*.go','pkg/**/*.go','cmd/**/*.go','internal/**/*.go',
    '*.rs','src/**/*.rs',
    '*.java','*.kt',
    # Configs (patterns)
    '*.toml','*.yaml','*.yml',
    # Specific content
    'cookbook/**/*','guides/**/*','recipes/**/*',
    'patterns/**/*','best-practices/**/*',
    'playbooks/**/*','runbooks/**/*',
]
SKIP = ['node_modules','.git/','__pycache__','dist/','build/','training_data','.venv']

added = 0
for pat in PATTERNS:
    if added >= $MAX_FILES: break
    for f in glob.glob(f"{ROOT}/{pat}", recursive=True):
        if added >= $MAX_FILES: break
        if not os.path.isfile(f): continue
        if any(s in f for s in SKIP): continue
        try:
            size = os.path.getsize(f)
            if size < 150 or size > 150000: continue
            content = open(f, encoding='utf-8', errors='ignore').read()[:150000]
            rel = f.replace(ROOT+'/','')
            cur.execute("""INSERT OR REPLACE INTO docs(source,project,path,topic,instruction,response,ts)
                          VALUES (?,?,?,?,?,?,?)""",
                        (f'mythos-{DOMAIN}', REPO, f'github:{REPO}/{rel}', DOMAIN, rel, content,
                         datetime.datetime.now().isoformat()))
            added += 1
        except: pass
conn.commit()
print(f"  + {REPO}: {added}")
PY
    rm -rf "$DIR"
    echo "$REPO" >> "$SEEN"
    COUNT=$((COUNT+1))
done

python3 -c "
import sqlite3
from pathlib import Path
conn = sqlite3.connect(str(Path.home() / '.claude/index.db'))
conn.execute(\"INSERT INTO docs_fts(docs_fts) VALUES('rebuild')\"); conn.commit()
" 2>/dev/null
echo "[$(date +%H:%M)] done: $COUNT repos" | tee -a "$LOG"

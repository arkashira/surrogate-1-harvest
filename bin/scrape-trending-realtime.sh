#!/usr/bin/env bash
# Real-time trending: scrape GitHub trending by language + topic EVERY 15 min.
# Finds repos that are getting stars RIGHT NOW — fresh signal.
set -u
WORK="/tmp/trending-realtime"
SEEN="$HOME/.claude/state/trending-realtime-seen.txt"
LOG="$HOME/.claude/logs/scrape-trending-realtime.log"
mkdir -p "$WORK" "$(dirname "$SEEN")" "$(dirname "$LOG")"
touch "$SEEN"

echo "[$(date +%H:%M)] trending-realtime" >> "$LOG"

# Trending by language + by topic (what's getting stars today)
QUERIES=(
    "language:python pushed:>$(date -v-3d +%Y-%m-%d) stars:>500"
    "language:typescript pushed:>$(date -v-3d +%Y-%m-%d) stars:>500"
    "language:go pushed:>$(date -v-3d +%Y-%m-%d) stars:>300"
    "language:rust pushed:>$(date -v-3d +%Y-%m-%d) stars:>300"
    "topic:ai-agent pushed:>$(date -v-7d +%Y-%m-%d) stars:>200"
    "topic:rag pushed:>$(date -v-7d +%Y-%m-%d) stars:>200"
    "topic:llm pushed:>$(date -v-7d +%Y-%m-%d) stars:>300"
    "topic:mcp pushed:>$(date -v-14d +%Y-%m-%d)"
    "topic:kubernetes pushed:>$(date -v-3d +%Y-%m-%d) stars:>300"
    "topic:devsecops pushed:>$(date -v-7d +%Y-%m-%d) stars:>200"
    "topic:security pushed:>$(date -v-3d +%Y-%m-%d) stars:>500"
    "topic:observability pushed:>$(date -v-7d +%Y-%m-%d) stars:>200"
    "topic:sre pushed:>$(date -v-7d +%Y-%m-%d) stars:>200"
    "topic:platform-engineering pushed:>$(date -v-7d +%Y-%m-%d)"
)

COUNT=0
MAX=15
for Q in "${QUERIES[@]}"; do
    [[ $COUNT -ge $MAX ]] && break
    RESULT=$(gh api -X GET search/repositories -f q="$Q" -f sort=stars -f order=desc -f per_page=3 2>/dev/null || echo '{"items":[]}')
    REPOS=$(echo "$RESULT" | python3 -c "
import sys,json
try:
    for r in json.load(sys.stdin).get('items',[]):
        desc = r.get('description','') or ''
        print(r['full_name'] + '|' + desc.replace('|','_')[:200])
except: pass")
    while IFS='|' read -r REPO DESC; do
        [[ -z "$REPO" ]] && continue
        [[ $COUNT -ge $MAX ]] && break
        grep -qxF "$REPO" "$SEEN" && continue
        
        DIR="$WORK/${REPO//\//_}"
        echo "  [$(date +%H:%M)] $REPO" >> "$LOG"
        git clone --depth 1 --filter=blob:limit=100k "https://github.com/$REPO.git" "$DIR" 2>>"$LOG" || { echo "$REPO" >> "$SEEN"; continue; }
        
        python3 <<PY 2>>"$LOG"
import os, glob, sqlite3, datetime
from pathlib import Path
DB = str(Path.home() / '.claude/index.db')
ROOT, REPO, DESC = "$DIR", "$REPO", """$DESC"""
conn = sqlite3.connect(DB)
cur = conn.cursor()
added = 0
for pat in ['README*','*.md','docs/**/*.md','src/**/*.py','src/**/*.ts','*.py','*.ts','examples/**/*']:
    for f in glob.glob(f"{ROOT}/{pat}", recursive=True)[:40]:
        if not os.path.isfile(f): continue
        try:
            size = os.path.getsize(f)
            if size < 200 or size > 80000: continue
            content = open(f, encoding='utf-8', errors='ignore').read()[:80000]
            rel = f.replace(ROOT+'/','')
            cur.execute("""INSERT OR REPLACE INTO docs(source,project,path,topic,instruction,response,ts)
                          VALUES (?,?,?,?,?,?,?)""",
                        ('trending-rt', REPO, f'github:{REPO}/{rel}', 'trending', f"{rel} | {DESC}", content,
                         datetime.datetime.now().isoformat()))
            added += 1
        except: pass
conn.commit()
print(f"  + {REPO}: {added}")
PY
        rm -rf "$DIR"
        echo "$REPO" >> "$SEEN"
        COUNT=$((COUNT+1))
    done <<< "$REPOS"
done

python3 -c "
import sqlite3
from pathlib import Path
conn = sqlite3.connect(str(Path.home() / '.claude/index.db'))
conn.execute(\"INSERT INTO docs_fts(docs_fts) VALUES('rebuild')\"); conn.commit()
" 2>/dev/null
echo "[$(date +%H:%M)] done: $COUNT trending" >> "$LOG"

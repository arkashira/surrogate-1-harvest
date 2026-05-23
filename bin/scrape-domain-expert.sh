#!/usr/bin/env bash
# Domain-expert scraper — runs for a specific role from the catalog.
# Usage: scrape-domain-expert.sh <role-name>
#   e.g., scrape-domain-expert.sh ai-engineering
# Scrapes: GitHub repos + RSS feeds + arxiv (if configured) → index.db (tagged with role)
set -u
ROLE="${1:?role required — see ~/.hermes/config/domain-experts.json}"
CATALOG="$HOME/.hermes/config/domain-experts.json"
WORK="/tmp/domain-$ROLE"
SEEN="$HOME/.claude/state/domain-seen-$ROLE.txt"
LOG="$HOME/.claude/logs/scrape-domain-$ROLE.log"
MIN_FREE_GB=5
MAX_REPOS_PER_RUN=25   # Conservative — run daily per role so accumulates
MAX_FILES_PER_REPO=80

mkdir -p "$WORK" "$(dirname "$SEEN")" "$(dirname "$LOG")"
touch "$SEEN"

echo "[$(date '+%Y-%m-%d %H:%M')] === domain-expert:$ROLE ===" | tee -a "$LOG"

# Parse catalog
REPOS=$(python3 -c "
import json
d = json.load(open('$CATALOG'))
role = d['roles'].get('$ROLE')
if not role: exit(1)
for r in role.get('github_repos',[]): print(r)
")
[[ -z "$REPOS" ]] && { echo "role not found: $ROLE" | tee -a "$LOG"; exit 1; }

RSS_FEEDS=$(python3 -c "
import json
d = json.load(open('$CATALOG'))
for r in d['roles'].get('$ROLE',{}).get('rss_feeds',[]): print(r)
")

# ─── GitHub repo scrape ───
COUNT=0
for REPO in $REPOS; do
    [[ $COUNT -ge $MAX_REPOS_PER_RUN ]] && break
    FREE_GB=$(df -g ~ | tail -1 | awk '{print $4}')
    [[ "$FREE_GB" -lt "$MIN_FREE_GB" ]] && { echo "  disk low" | tee -a "$LOG"; break; }
    
    grep -qxF "$REPO" "$SEEN" && continue
    
    DIR="$WORK/${REPO//\//_}"
    echo "  [$(date +%H:%M:%S)] $REPO" | tee -a "$LOG"
    
    # Shallow clone, blob limit
    if ! git clone --depth 1 --filter=blob:limit=80k "https://github.com/$REPO.git" "$DIR" 2>>"$LOG"; then
        echo "$REPO" >> "$SEEN"
        continue
    fi
    
    # Ingest using python into SQLite — only high-value files
    python3 <<PY 2>>"$LOG"
import os, glob, json, sqlite3, datetime
from pathlib import Path

DB = str(Path.home() / '.claude/index.db')
ROOT = "$DIR"
REPO = "$REPO"
ROLE = "$ROLE"
MAX_FILES = $MAX_FILES_PER_REPO
MAX_SIZE = 60000  # 40KB per file max

conn = sqlite3.connect(DB)
cur = conn.cursor()

patterns = ['README*', '*.md', '*.mdx', 'docs/**/*.md', 'docs/**/*.mdx',
            'examples/**/*.py', 'examples/**/*.ts', 'examples/**/*.tsx',
            'tutorial/**/*', 'guide/**/*', 'cookbook/**/*',
            'src/**/*.py', 'src/**/*.ts', 'src/**/*.tsx']

added = 0
collected_files = set()
for pat in patterns:
    for f in glob.glob(f"{ROOT}/{pat}", recursive=True):
        if not os.path.isfile(f): continue
        if f in collected_files: continue
        try: size = os.path.getsize(f)
        except: continue
        if size < 200 or size > MAX_SIZE: continue
        collected_files.add(f)
        if len(collected_files) >= MAX_FILES: break
    if len(collected_files) >= MAX_FILES: break

for f in collected_files:
    try:
        with open(f, encoding='utf-8', errors='ignore') as fh:
            content = fh.read()[:MAX_SIZE]
        rel = f.replace(ROOT + '/', '')
        topic = rel.split('/')[0]
        cur.execute("""INSERT OR REPLACE INTO docs(source,project,path,topic,instruction,response,ts)
                      VALUES (?,?,?,?,?,?,?)""",
                    (f'domain:{ROLE}', REPO, f'github:{REPO}/{rel}', topic, rel, content,
                     datetime.datetime.now().isoformat()))
        added += 1
    except Exception as e: pass

conn.commit()
print(f"  + github:{REPO}: {added} docs")
PY
    
    rm -rf "$DIR"
    echo "$REPO" >> "$SEEN"
    COUNT=$((COUNT+1))
done

# ─── RSS feeds ───
if [[ -n "$RSS_FEEDS" ]]; then
    echo "  [$(date +%H:%M)] scraping RSS feeds..." | tee -a "$LOG"
    python3 <<PY 2>>"$LOG"
import feedparser, sqlite3, datetime, os
from pathlib import Path
DB = str(Path.home() / '.claude/index.db')
conn = sqlite3.connect(DB)
cur = conn.cursor()
feeds = """$RSS_FEEDS""".strip().split('\n')
added = 0
for url in feeds:
    if not url.strip(): continue
    try:
        feed = feedparser.parse(url.strip())
        for entry in feed.entries[:10]:
            title = entry.get('title','')
            summary = entry.get('summary','') or entry.get('description','')
            link = entry.get('link','')
            pub = entry.get('published','')
            cur.execute("""INSERT OR REPLACE INTO docs(source,project,path,topic,instruction,response,ts)
                          VALUES (?,?,?,?,?,?,?)""",
                        (f'domain:$ROLE', '$ROLE-rss', link or f'{url}#{added}',
                         'rss', title, summary[:10000],
                         datetime.datetime.now().isoformat()))
            added += 1
    except Exception as e: print(f"  rss err {url}: {e}")
conn.commit()
print(f"  + rss: {added} entries")
PY
fi

# Rebuild FTS 1 time at end
python3 -c "
import sqlite3
from pathlib import Path
conn = sqlite3.connect(str(Path.home() / '.claude/index.db'))
conn.execute(\"INSERT INTO docs_fts(docs_fts) VALUES('rebuild')\")
conn.commit()
" 2>>"$LOG"

echo "[$(date +%H:%M:%S)] done: $COUNT new repos" | tee -a "$LOG"

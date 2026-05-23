#!/usr/bin/env bash
# Direct implementation of scrape-hn-discussions, copied from ~/.claude/bin
# Deep HN — pull top stories + top comments (where experts debate)
set -u
LOG="$HOME/.claude/logs/scrape-hn.log"
mkdir -p "$(dirname "$LOG")"

python3 <<'PY' 2>&1 | tee -a "$LOG"
import urllib.request, json, sqlite3, datetime
from pathlib import Path
DB = str(Path.home() / '.claude/index.db')
conn = sqlite3.connect(DB)
cur = conn.cursor()
headers = {'User-Agent':'hermes/1.0'}

def fetch(url):
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except: return None

# Top + best stories
stories = fetch("https://hacker-news.firebaseio.com/v0/topstories.json") or []
added = 0
for sid in stories[:80]:
    s = fetch(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json")
    if not s or s.get('type') != 'story': continue
    title = s.get('title','')
    url = s.get('url','') or f"https://news.ycombinator.com/item?id={sid}"
    score = s.get('score',0)
    nk = s.get('kids',[])[:5]
    text = s.get('text','')
    
    # Filter for engineering content
    t_lower = title.lower()
    if not any(kw in t_lower for kw in ['code','dev','software','engineer','ai','llm','kubernetes','docker','api','performance','security','cloud','database','python','javascript','rust','go ','architecture','scale','production','open source','framework','library']):
        continue
    
    # Get top 3 comments
    comment_text = ""
    for kid in nk[:3]:
        c = fetch(f"https://hacker-news.firebaseio.com/v0/item/{kid}.json")
        if c and c.get('text'):
            comment_text += f"\n[comment by {c.get('by','?')} score:{score}]:\n{c['text'][:3000]}\n"
    
    body = f"{text}\n\n=== TOP COMMENTS ===\n{comment_text}"
    
    cur.execute("""INSERT OR REPLACE INTO docs(source,project,path,topic,instruction,response,ts)
                  VALUES (?,?,?,?,?,?,?)""",
                ('hn-discussion','hackernews',url,'discussion',
                 f"[score:{score}] {title}", body[:40000],
                 datetime.datetime.now().isoformat()))
    added += 1

conn.commit()
try: conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')"); conn.commit()
except: pass
print(f"hn-discussions: +{added}")
PY

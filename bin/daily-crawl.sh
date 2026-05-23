#!/usr/bin/env bash
# Daily auto-crawler — fetches latest trends/news/repos + AI-filters + indexes
# Runs via launchd (Mon-Fri 12:05 + 13:00, lunch crawl)
set -e

SKIP_FILTER=0
SINGLE_SOURCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-filter) SKIP_FILTER=1; shift ;;
    --source)      SINGLE_SOURCE="$2"; shift 2 ;;
    *)             shift ;;
  esac
done

export PATH=/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin:$PATH
source ~/.hermes/.env 2>/dev/null || true
# Also source ~/.hermes/.env (where Surrogate keeps the live tokens)
set -a; source ~/.hermes/.env 2>/dev/null || true; set +a

DATE=$(date +%Y-%m-%d)
CRAWL_DIR="$HOME/Documents/Obsidian Vault/AI-Hub/crawls/$DATE"
mkdir -p "$CRAWL_DIR/raw" "$HOME/.surrogate/logs"
LOG="$HOME/.surrogate/logs/crawl-$DATE.log"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

PY=~/.surrogate/venv/bin/python

# ═══════════ SOURCES — use Python scripts with explicit env passing ═══════════

crawl_github() {
  log "→ GitHub (repos + topics)"
  SINCE=$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d '7 days ago' +%Y-%m-%d)
  DATE="$DATE" SINCE="$SINCE" GITHUB_TOKEN="${GITHUB_PERSONAL_ACCESS_TOKEN:-}" \
    "$PY" - "$CRAWL_DIR/raw/github.jsonl" <<'PY'
import os, sys, json, time, urllib.request
out = open(sys.argv[1], 'a')
since = os.environ['SINCE']
date = os.environ['DATE']
token = os.environ.get('GITHUB_TOKEN', '')

def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent':'crawler/1.0'})
    if token: req.add_header('Authorization', 'Bearer ' + token)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as e:
        print(f'  [err] {url[:80]}: {e}', file=sys.stderr)
        return {}

written = 0
for lang in ['typescript','go','python','rust','hcl','javascript']:
    url = f'https://api.github.com/search/repositories?q=created:>{since}+language:{lang}+stars:>20&sort=stars&order=desc&per_page=10'
    d = fetch(url)
    for r in d.get('items', [])[:10]:
        desc = (r.get('description') or '')[:300]
        stars = r.get('stargazers_count', 0)
        out.write(json.dumps({
            'title': r.get('full_name',''),
            'url': r.get('html_url',''),
            'summary': f'{desc} | stars={stars}',
            'source': f'github-{lang}',
            'date': date,
        }, ensure_ascii=False) + '\n')
        written += 1
    time.sleep(3 if not token else 1)

for topic in ['kubernetes','devops','terraform','platform-engineering','observability','llm-agent']:
    url = f'https://api.github.com/search/repositories?q=topic:{topic}+pushed:>{since}&sort=stars&order=desc&per_page=5'
    d = fetch(url)
    for r in d.get('items', [])[:5]:
        desc = (r.get('description') or '')[:300]
        out.write(json.dumps({
            'title': r.get('full_name',''),
            'url': r.get('html_url',''),
            'summary': f'{desc} | stars={r.get("stargazers_count",0)}',
            'source': f'github-topic-{topic}',
            'date': date,
        }, ensure_ascii=False) + '\n')
        written += 1
    time.sleep(3 if not token else 1)

out.close()
print(f'  wrote {written}')
PY
}

crawl_hackernews() {
  log "→ HackerNews top 30"
  DATE="$DATE" "$PY" - "$CRAWL_DIR/raw/hackernews.jsonl" <<'PY'
import os, sys, json, urllib.request
out = open(sys.argv[1], 'a')
date = os.environ['DATE']
try:
    ids = json.loads(urllib.request.urlopen('https://hacker-news.firebaseio.com/v0/topstories.json', timeout=30).read())[:30]
except Exception as e:
    print(f'  [err] {e}', file=sys.stderr); exit(0)
written = 0
for id_ in ids:
    try:
        r = urllib.request.urlopen(f'https://hacker-news.firebaseio.com/v0/item/{id_}.json', timeout=15)
        d = json.loads(r.read())
        if d and d.get('type') == 'story' and d.get('score', 0) >= 50:
            url = d.get('url') or ('https://news.ycombinator.com/item?id=' + str(d.get('id','')))
            out.write(json.dumps({
                'title': d.get('title',''),
                'url': url,
                'summary': 'HN score=' + str(d.get('score',0)) + ', comments=' + str(d.get('descendants',0)),
                'source': 'hackernews',
                'date': date,
            }, ensure_ascii=False) + '\n')
            written += 1
    except: pass
out.close()
print(f'  wrote {written}')
PY
}

crawl_devto() {
  log "→ Dev.to (multi-tag)"
  DATE="$DATE" "$PY" - "$CRAWL_DIR/raw/devto.jsonl" <<'PY'
import os, sys, json, urllib.request
out = open(sys.argv[1], 'a')
date = os.environ['DATE']
written = 0
for tag in ['devops','sre','kubernetes','aws','terraform','ai','machinelearning','backend','rust','golang','typescript','webdev']:
    try:
        r = urllib.request.Request(
            f'https://dev.to/api/articles?tag={tag}&top=7&per_page=5',
            headers={'User-Agent':'crawler/1.0', 'Accept':'application/json'})
        arts = json.loads(urllib.request.urlopen(r, timeout=30).read())
        for a in arts[:5]:
            out.write(json.dumps({
                'title': a.get('title',''),
                'url': a.get('url',''),
                'summary': (a.get('description') or '')[:300],
                'source': f'devto-{tag}',
                'date': date,
            }, ensure_ascii=False) + '\n')
            written += 1
    except Exception as e:
        print(f'  [err devto-{tag}] {e}', file=sys.stderr)
out.close()
print(f'  wrote {written}')
PY
}

crawl_reddit() {
  log "→ Reddit (devops/sre/programming)"
  DATE="$DATE" "$PY" - "$CRAWL_DIR/raw/reddit.jsonl" <<'PY'
import os, sys, json, urllib.request
out = open(sys.argv[1], 'a')
date = os.environ['DATE']
written = 0
for sub in ['devops','sre','kubernetes','aws','mlops','programming','ExperiencedDevs']:
    try:
        r = urllib.request.Request(
            f'https://www.reddit.com/r/{sub}/top.json?t=day&limit=10',
            headers={'User-Agent':'Mozilla/5.0 crawler'})
        d = json.loads(urllib.request.urlopen(r, timeout=30).read())
        for post in d.get('data',{}).get('children',[])[:10]:
            p = post.get('data',{})
            if p.get('score',0) >= 30:
                perma = p.get('permalink','')
                out.write(json.dumps({
                    'title': p.get('title',''),
                    'url': 'https://reddit.com' + perma,
                    'summary': (p.get('selftext','')[:300]) or ('r/' + p.get('subreddit','') + ' upvotes=' + str(p.get('score',0))),
                    'source': f'reddit-{sub}',
                    'date': date,
                }, ensure_ascii=False) + '\n')
                written += 1
    except Exception as e:
        print(f'  [err reddit-{sub}] {e}', file=sys.stderr)
out.close()
print(f'  wrote {written}')
PY
}

crawl_arxiv() {
  log "→ ArXiv CS (AI/ML/distributed/SE/security)"
  DATE="$DATE" "$PY" - "$CRAWL_DIR/raw/arxiv.jsonl" <<'PY'
import os, sys, json, re, urllib.request
out = open(sys.argv[1], 'a')
date = os.environ['DATE']
written = 0
for cat in ['cs.AI','cs.LG','cs.DC','cs.SE','cs.CR']:
    try:
        xml = urllib.request.urlopen(
            f'http://export.arxiv.org/api/query?search_query=cat:{cat}&sortBy=submittedDate&sortOrder=descending&max_results=8',
            timeout=30).read().decode()
        entries = re.findall(r'<entry>.*?</entry>', xml, re.DOTALL)
        for e in entries[:8]:
            t = re.search(r'<title>(.*?)</title>', e, re.DOTALL)
            l = re.search(r'<id>(.*?)</id>', e)
            s = re.search(r'<summary>(.*?)</summary>', e, re.DOTALL)
            if t and l:
                title = re.sub(r'\s+', ' ', t.group(1)).strip()
                summ = re.sub(r'\s+', ' ', s.group(1) if s else '').strip()[:400]
                out.write(json.dumps({
                    'title': title,
                    'url': l.group(1).strip(),
                    'summary': summ,
                    'source': f'arxiv-{cat}',
                    'date': date,
                }, ensure_ascii=False) + '\n')
                written += 1
    except Exception as e:
        print(f'  [err arxiv-{cat}] {e}', file=sys.stderr)
out.close()
print(f'  wrote {written}')
PY
}

crawl_lobsters() {
  log "→ Lobste.rs hottest"
  DATE="$DATE" "$PY" - "$CRAWL_DIR/raw/lobsters.jsonl" <<'PY'
import os, sys, json, urllib.request
out = open(sys.argv[1], 'a')
date = os.environ['DATE']
written = 0
try:
    r = urllib.request.Request('https://lobste.rs/hottest.json', headers={'User-Agent':'crawler/1.0'})
    posts = json.loads(urllib.request.urlopen(r, timeout=30).read())
    for p in posts[:20]:
        if p.get('score', 0) >= 10:
            tags = ','.join(p.get('tags', []))
            out.write(json.dumps({
                'title': p.get('title',''),
                'url': p.get('url','') or p.get('short_id_url',''),
                'summary': '[' + tags + '] score=' + str(p.get('score',0)),
                'source': 'lobsters',
                'date': date,
            }, ensure_ascii=False) + '\n')
            written += 1
except Exception as e:
    print(f'  [err lobsters] {e}', file=sys.stderr)
out.close()
print(f'  wrote {written}')
PY
}

# ═══════════ AI FILTER (OpenRouter free cascade) ═══════════

ai_classify() {
  local input="$1" output="$2"
  [ ! -s "$input" ] && return
  [ -z "${OPENROUTER_API_KEY:-}" ] && { log "  (no OR key, skipping AI filter)"; cp "$input" "$output"; return; }
  OPENROUTER_API_KEY="$OPENROUTER_API_KEY" IN="$input" OUT="$output" "$PY" <<'PY'
import os, sys, json, urllib.request
api = os.environ['OPENROUTER_API_KEY']
items = []
with open(os.environ['IN']) as f:
    for line in f:
        try: items.append(json.loads(line))
        except: pass
if not items:
    open(os.environ['OUT'],'w').close(); exit(0)

FREE = ['openai/gpt-oss-120b:free','qwen/qwen3-next-80b-a3b-instruct:free',
        'meta-llama/llama-3.3-70b-instruct:free','openai/gpt-oss-20b:free']

def ask(body):
    for model in FREE:
        body['model'] = model
        try:
            req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
                data=json.dumps(body).encode(),
                headers={'Authorization':'Bearer '+api,'content-type':'application/json'})
            r = urllib.request.urlopen(req, timeout=90).read()
            return json.loads(r)['choices'][0]['message']['content']
        except Exception as e:
            continue
    return None

out = open(os.environ['OUT'], 'w')
BATCH = 15
relevant = 0
for i in range(0, len(items), BATCH):
    batch = items[i:i+BATCH]
    prompt = ('Rate each item 1-5 for relevance to a DevOps/SRE/Platform Engineer (AWS, Terraform, K8s, TS/Go/Python, AI/ML). '
              'Return ONLY JSON array: [{"idx":0,"score":N,"tags":["tag1","tag2"]}]\n\n'
              + '\n'.join(f'{j}. {it.get("title","")} — {it.get("summary","")[:120]}' for j,it in enumerate(batch)))
    result = ask({'max_tokens':2000,'messages':[{'role':'user','content':prompt}]})
    if not result:
        # fallback: keep all as score 3
        for item in batch:
            item['score'] = 3
            item['tags'] = ['uncategorized']
            out.write(json.dumps(item, ensure_ascii=False)+'\n'); relevant += 1
        continue
    import re
    m = re.search(r'\[.*\]', result, re.DOTALL)
    ratings = []
    if m:
        try: ratings = json.loads(m.group(0))
        except: pass
    rmap = {r.get('idx',-1):r for r in ratings}
    for j, item in enumerate(batch):
        r = rmap.get(j, {})
        item['score'] = r.get('score', 3)
        item['tags'] = r.get('tags', [])
        if item['score'] >= 3:
            out.write(json.dumps(item, ensure_ascii=False)+'\n'); relevant += 1
out.close()
print(f'  filtered: {relevant}/{len(items)} relevant')
PY
}

# ═══════════ MAIN ═══════════

log "=== Daily crawl: $DATE ==="

case "$SINGLE_SOURCE" in
  github)     crawl_github ;;
  hackernews|hn) crawl_hackernews ;;
  devto)      crawl_devto ;;
  reddit)     crawl_reddit ;;
  arxiv)      crawl_arxiv ;;
  lobsters)   crawl_lobsters ;;
  "")
    crawl_github
    crawl_hackernews
    crawl_devto
    crawl_reddit
    crawl_arxiv
    crawl_lobsters
    ;;
  *) log "Unknown source: $SINGLE_SOURCE"; exit 1 ;;
esac

# Merge + filter
cat "$CRAWL_DIR/raw/"*.jsonl 2>/dev/null | sort -u > "$CRAWL_DIR/raw/all.jsonl"
TOTAL=$(wc -l < "$CRAWL_DIR/raw/all.jsonl" | tr -d ' ')
log "Total raw: $TOTAL"

if [ "$SKIP_FILTER" = "0" ] && [ "$TOTAL" -gt 0 ]; then
  ai_classify "$CRAWL_DIR/raw/all.jsonl" "$CRAWL_DIR/raw/filtered.jsonl"
else
  cp "$CRAWL_DIR/raw/all.jsonl" "$CRAWL_DIR/raw/filtered.jsonl"
fi

# Write markdown digest
CRAWL_DIR="$CRAWL_DIR" DATE="$DATE" "$PY" <<'PY'
import os, json
from pathlib import Path
from collections import defaultdict
crawl = Path(os.environ['CRAWL_DIR']); date = os.environ['DATE']
items = []
with open(crawl/'raw/filtered.jsonl') as f:
    for line in f:
        try: items.append(json.loads(line))
        except: pass

by_src = defaultdict(list)
for it in items:
    by_src[it.get('source','?').split('-')[0]].append(it)

tag_counts = defaultdict(int)
for it in items:
    for t in it.get('tags', []): tag_counts[t] += 1

lines = [
 '---', f'name: Daily Crawl — {date}', f'date: {date}',
 'tags: [crawl, daily, trends]', '---', '',
 f'# Daily Tech Crawl — {date}', '',
 f'**Items**: {len(items)} | **Sources**: {", ".join(by_src.keys())}', '']

if tag_counts:
    lines.append('## Top tags')
    for t, c in sorted(tag_counts.items(), key=lambda x:-x[1])[:15]:
        lines.append(f'- `#{t}` ({c})')
    lines.append('')

for src, src_items in sorted(by_src.items()):
    src_items.sort(key=lambda x:-x.get('score',0))
    lines.append(f'## {src.upper()}')
    for it in src_items[:10]:
        sc = it.get('score','-')
        tags = ' '.join(f'`#{t}`' for t in it.get('tags',[])[:3])
        lines.append(f'- **[{it.get("title","?")}]({it.get("url","#")})** [{sc}/5] {tags}')
        if it.get('summary'):
            lines.append(f'  {it["summary"][:200]}')
    lines.append('')

lines += ['---','[[../../../patterns/MOC|🧭 Graph Hub]] · [[../index|All crawls]]']
(crawl/'digest.md').write_text('\n'.join(lines))
print(f'digest: {crawl/"digest.md"} ({len(items)} items)')
PY

# Update crawl index
"$PY" - <<'PY'
from pathlib import Path
import os
base = Path(os.path.expanduser('~/Documents/Obsidian Vault/AI-Hub/crawls'))
dirs = sorted([d for d in base.iterdir() if d.is_dir()], reverse=True)
lines = ['---','name: Crawl Index','tags: [crawls, index]','---','',
         '# Daily Crawls Index','']
for d in dirs[:60]:
    if (d / 'digest.md').exists():
        lines.append(f'- [[{d.name}/digest|{d.name}]]')
(base/'index.md').write_text('\n'.join(lines))
PY

# Graph sync (async)
[ -x "$HOME/.surrogate/bin/graph-sync.sh" ] && ("$HOME/.surrogate/bin/graph-sync.sh" > /dev/null 2>&1 &) || true

log "=== Done: $CRAWL_DIR/digest.md ==="

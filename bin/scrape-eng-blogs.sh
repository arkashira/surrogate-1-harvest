#!/usr/bin/env bash
set -u
LOG="$HOME/.claude/logs/scrape-eng-blogs.log"
mkdir -p "$(dirname "$LOG")"

python3 <<'PY' 2>&1 | tee -a "$LOG"
import feedparser, sqlite3, datetime, os
from pathlib import Path
DB = str(Path.home() / '.claude/index.db')

# Curated engineering blogs — top tech companies
BLOGS = {
    'netflix-tech': 'https://netflixtechblog.com/feed',
    'uber-eng': 'https://eng.uber.com/feed/',
    'stripe-eng': 'https://stripe.com/blog/engineering/rss',
    'shopify-eng': 'https://shopify.engineering/blog.atom',
    'airbnb-eng': 'https://medium.com/feed/airbnb-engineering',
    'meta-eng': 'https://engineering.fb.com/feed/',
    'linkedin-eng': 'https://engineering.linkedin.com/blog.rss.html',
    'github-eng': 'https://github.blog/engineering/feed/',
    'cloudflare': 'https://blog.cloudflare.com/rss/',
    'vercel': 'https://vercel.com/atom',
    'hashicorp': 'https://www.hashicorp.com/blog/feed.xml',
    'honeycomb': 'https://www.honeycomb.io/feed/',
    'grafana': 'https://grafana.com/blog/index.xml',
    'elastic': 'https://www.elastic.co/blog/feed',
    'datadog': 'https://www.datadoghq.com/blog/engineering/index.xml',
    'aws-arch': 'https://aws.amazon.com/blogs/architecture/feed/',
    'aws-dev': 'https://aws.amazon.com/blogs/developer/feed/',
    'aws-opensource': 'https://aws.amazon.com/blogs/opensource/feed/',
    'gcp-blog': 'https://cloudblog.withgoogle.com/rss/',
    'microsoft-azure': 'https://azure.microsoft.com/en-us/blog/feed/',
    'huggingface': 'https://huggingface.co/blog/feed.xml',
    'anthropic': 'https://www.anthropic.com/index.xml',
    'openai': 'https://openai.com/blog/rss.xml',
    'deepmind': 'https://deepmind.google/blog/rss.xml',
    'redhat-devs': 'https://developers.redhat.com/feed',
    'martinfowler': 'https://martinfowler.com/feed.atom',
    'high-scalability': 'https://highscalability.com/feed/',
    'bytebytego': 'https://blog.bytebytego.com/feed',
    'pragmatic-eng': 'https://blog.pragmaticengineer.com/rss/',
    'charity-majors': 'https://charity.wtf/feed/',
    'dan-abramov': 'https://overreacted.io/rss.xml',
    'kentcdodds': 'https://kentcdodds.com/blog/rss.xml',
    'leerob': 'https://leerob.io/feed.xml',
    'joshwcomeau': 'https://www.joshwcomeau.com/rss.xml',
    'swyx': 'https://www.swyx.io/rss.xml',
    'latent-space': 'https://www.latent.space/feed',
    'simon-willison': 'https://simonwillison.net/atom/everything/',
    'fly-io': 'https://fly.io/blog/feed.xml',
    'ogp': 'https://blog.google/technology/ai/rss/',
}

conn = sqlite3.connect(DB)
cur = conn.cursor()
total = 0
for name, url in BLOGS.items():
    try:
        feed = feedparser.parse(url)
        for e in feed.entries[:8]:
            title = e.get('title','')
            summary = e.get('summary','') or e.get('description','') or ''
            # Try to get full content
            content = ''
            if hasattr(e, 'content') and e.content:
                content = e.content[0].get('value','') if e.content else ''
            body = (content or summary)[:30000]
            link = e.get('link','')
            pub = e.get('published','') or e.get('updated','')
            if not body: continue
            cur.execute("""INSERT OR REPLACE INTO docs(source,project,path,topic,instruction,response,ts)
                          VALUES (?,?,?,?,?,?,?)""",
                        (f'eng-blog:{name}', name, link or f"{name}#{total}", 'blog',
                         title, body, pub or datetime.datetime.now().isoformat()))
            total += 1
    except Exception as e:
        print(f"  {name}: {e}")

conn.commit()
try: conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')"); conn.commit()
except: pass
print(f"eng-blogs: +{total} posts from {len(BLOGS)} sources")
PY

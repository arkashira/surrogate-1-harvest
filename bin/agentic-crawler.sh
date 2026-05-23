#!/usr/bin/env bash
# Agentic crawler — URL frontier with visited stamps + link discovery (BFS).
# Runs continuously: pop URL → fetch → extract links → score → push back to frontier.
# Stamps every visited URL in SQLite so we never revisit. Persists across restarts.
#
# Seeds (re-injected nightly): GitHub trending, arxiv recent, HF trending, MoC pages.
# Filtering: only follow links matching domain allowlist + minimum relevance.
# Output: training pairs (page → summary) pushed to HF dataset every 50 fetches.
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

DB="$HOME/.surrogate/state/agentic-frontier.db"
LOG="$HOME/.surrogate/logs/agentic-crawler.log"
PAIRS="$HOME/.surrogate/training-pairs.jsonl"
mkdir -p "$(dirname "$DB")" "$(dirname "$LOG")" "$(dirname "$PAIRS")"

# ── Schema ──────────────────────────────────────────────────────────────────
sqlite3 "$DB" <<'SQL'
CREATE TABLE IF NOT EXISTS visited (
    url        TEXT PRIMARY KEY,
    fetched_ts INTEGER NOT NULL,
    status     INTEGER NOT NULL,
    title      TEXT,
    domain     TEXT,
    depth      INTEGER DEFAULT 0,
    bytes      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS frontier (
    url      TEXT PRIMARY KEY,
    score    REAL NOT NULL,
    depth    INTEGER NOT NULL,
    parent   TEXT,
    added_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_frontier_score ON frontier(score DESC, added_ts);
CREATE INDEX IF NOT EXISTS idx_visited_domain ON visited(domain);
SQL

# ── Seed if empty ───────────────────────────────────────────────────────────
COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM frontier;")
if [[ $COUNT -lt 5 ]]; then
    echo "[$(date +%H:%M:%S)] seeding frontier" | tee -a "$LOG"
    python3 - "$DB" <<'PYEOF'
import sqlite3, sys, time
db = sys.argv[1]
seeds = [
    # AI agent / coding
    ("https://github.com/trending?since=daily", 1.0, 0),
    ("https://github.com/trending/python?since=daily", 0.9, 0),
    ("https://github.com/trending/typescript?since=daily", 0.9, 0),
    ("https://github.com/trending/rust?since=daily", 0.85, 0),
    ("https://github.com/trending/go?since=daily", 0.85, 0),
    ("https://huggingface.co/models?sort=trending", 0.95, 0),
    ("https://huggingface.co/datasets?sort=trending", 0.85, 0),
    ("https://arxiv.org/list/cs.AI/recent", 0.95, 0),
    ("https://arxiv.org/list/cs.SE/recent", 0.9, 0),
    ("https://arxiv.org/list/cs.CR/recent", 0.85, 0),
    ("https://news.ycombinator.com/", 0.8, 0),
    ("https://lobste.rs/", 0.75, 0),
    # DevSecOps / SRE / cloud
    ("https://aws.amazon.com/blogs/devops/", 0.7, 0),
    ("https://cloud.google.com/blog/products/devops-sre", 0.7, 0),
    ("https://kubernetes.io/blog/", 0.7, 0),
    ("https://www.cncf.io/blog/", 0.7, 0),
    # Awesome lists (rich link sources)
    ("https://github.com/sindresorhus/awesome", 0.9, 0),
    ("https://github.com/stevenjoezhang/awesome-llm-agents", 0.95, 0),
    ("https://github.com/e2b-dev/awesome-ai-agents", 0.95, 0),
    ("https://github.com/Hannibal046/Awesome-LLM", 0.9, 0),
    ("https://github.com/punkpeye/awesome-mcp-servers", 0.95, 0),
]
con = sqlite3.connect(db)
now = int(time.time())
for url, score, depth in seeds:
    con.execute("INSERT OR IGNORE INTO frontier(url,score,depth,parent,added_ts) VALUES (?,?,?,NULL,?)",
                (url, score, depth, now))
con.commit()
print(f"  seeded {len(seeds)} URLs")
PYEOF
fi

# ── Worker: fetch one URL, extract links, score, push back to frontier ─────
fetch_one() {
    local url="$1" depth="$2"
    python3 - "$url" "$depth" "$DB" "$PAIRS" "${HF_TOKEN:-}" <<'PYEOF' 2>&1
import sys, sqlite3, urllib.request, urllib.parse, re, time, json, os
url, depth, db, pairs, hf_token = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
con = sqlite3.connect(db)

# Skip if already visited
if con.execute("SELECT 1 FROM visited WHERE url=?", (url,)).fetchone():
    print(f"  [skip-visited] {url[:80]}")
    sys.exit(0)

domain = urllib.parse.urlparse(url).netloc
allow = {"github.com","huggingface.co","arxiv.org","news.ycombinator.com","lobste.rs",
         "aws.amazon.com","cloud.google.com","azure.microsoft.com","kubernetes.io","cncf.io",
         "anthropic.com","openai.com","mistral.ai","meta.com","ai.google.dev",
         "datadog.com","newrelic.com","dynatrace.com","grafana.com","prometheus.io",
         "redhat.com","docker.com","hashicorp.com","cncf.io","github.io","medium.com",
         "dev.to","substack.com","blogspot.com"}
if domain not in allow and not any(domain.endswith("."+a) for a in allow):
    con.execute("INSERT OR REPLACE INTO visited VALUES (?,?,?,?,?,?,?)",
                (url, int(time.time()), -2, None, domain, depth, 0))
    con.commit()
    print(f"  [skip-domain] {domain}")
    sys.exit(0)

# Fetch
try:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 Surrogate-1/agentic-crawler",
        "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read(2_000_000).decode("utf-8", errors="ignore")
        status = r.status
        ctype = (r.headers.get("Content-Type") or "").lower()
    # Skip non-HTML responses (DNS records, raw zone files, etc. were crashing parser)
    if "html" not in ctype and "<html" not in body[:1000].lower():
        con.execute("INSERT OR REPLACE INTO visited VALUES (?,?,?,?,?,?,?)",
                    (url, int(time.time()), status, "", domain, depth, len(body)))
        con.commit()
        print(f"  [skip-non-html] {ctype[:30]} {url[:80]}")
        sys.exit(0)
except Exception as e:
    con.execute("INSERT OR REPLACE INTO visited VALUES (?,?,?,?,?,?,?)",
                (url, int(time.time()), -1, None, domain, depth, 0))
    con.commit()
    print(f"  [fail] {url[:80]} :: {type(e).__name__}")
    sys.exit(0)

# Title
m = re.search(r"<title[^>]*>([^<]+)</title>", body, re.IGNORECASE)
title = (m.group(1) if m else "").strip()[:200]
con.execute("INSERT OR REPLACE INTO visited VALUES (?,?,?,?,?,?,?)",
            (url, int(time.time()), status, title, domain, depth, len(body)))

# Extract links + score
links = re.findall(r'href=["\'](https?://[^"\'#?\s<>]+)', body, re.IGNORECASE)
seen_set = set()
added = 0
for link in links:
    if link in seen_set: continue
    seen_set.add(link)
    if con.execute("SELECT 1 FROM visited WHERE url=?", (link,)).fetchone(): continue
    if con.execute("SELECT 1 FROM frontier WHERE url=?", (link,)).fetchone(): continue
    ldomain = urllib.parse.urlparse(link).netloc
    if not ldomain or len(link) > 500: continue
    # Score: domain relevance + keyword bonus + depth penalty
    score = 0.5
    keywords_high = ("agent","llm","rag","mcp","claude","gpt","coder","devops","sre","kubernetes","terraform")
    keywords_mid = ("ai","ml","cloud","devsec","security","python","typescript","go","rust","blog","paper")
    low = link.lower()
    if any(k in low for k in keywords_high): score += 0.3
    elif any(k in low for k in keywords_mid): score += 0.1
    if ldomain in allow or any(ldomain.endswith("."+a) for a in allow): score += 0.2
    score -= 0.05 * (depth + 1)
    if score < 0.3: continue
    if depth + 1 > 4: continue  # max depth
    con.execute("INSERT OR IGNORE INTO frontier VALUES (?,?,?,?,?)",
                (link, score, depth + 1, url, int(time.time())))
    added += 1
    if added > 30: break

con.commit()
print(f"  [ok {status}] {title[:60]} ← {url[:60]} (+{added} new links)")

# Save fetched page metadata to a SEPARATE crawl log — NOT to training-pairs.jsonl.
# (Placeholder responses pollute training data; only insert when we have real summary.)
crawl_log = os.path.expanduser("~/.surrogate/state/agentic-crawl-raw.jsonl")
text_only = re.sub(r"<[^>]+>", " ", body)
text_only = re.sub(r"\s+", " ", text_only).strip()[:6000]
if len(text_only) > 200:
    raw_record = {
        "ts": time.time(),
        "source": "agentic-crawler",
        "url": url,
        "title": title,
        "domain": domain,
        "depth": depth,
        "text": text_only[:6000],
    }
    with open(crawl_log, "a") as f:
        f.write(json.dumps(raw_record, ensure_ascii=False) + "\n")
PYEOF
}

# ── Main loop: parallel workers ─────────────────────────────────────────────
PARALLEL="${1:-4}"   # default 4 concurrent
BATCH_SIZE=20
echo "[$(date +%H:%M:%S)] crawler start (parallel=$PARALLEL)" | tee -a "$LOG"

while true; do
    # Pop top-scoring URLs from frontier
    BATCH=$(sqlite3 "$DB" "SELECT url||'|'||depth FROM frontier ORDER BY score DESC, added_ts ASC LIMIT $BATCH_SIZE;")
    if [[ -z "$BATCH" ]]; then
        echo "[$(date +%H:%M:%S)] frontier empty — re-seeding from awesome lists" >> "$LOG"
        # Re-seed: re-fetch awesome lists to pick up new repos added since last seed
        python3 -c "
import sqlite3, time
con = sqlite3.connect('$DB')
# Drop visited stamps for awesome list pages so they get re-fetched
seeds = [
    'https://github.com/trending?since=daily',
    'https://github.com/trending/python?since=daily',
    'https://github.com/trending/typescript?since=daily',
    'https://github.com/sindresorhus/awesome',
    'https://github.com/e2b-dev/awesome-ai-agents',
    'https://github.com/Hannibal046/Awesome-LLM',
    'https://github.com/punkpeye/awesome-mcp-servers',
    'https://github.com/dastergon/awesome-sre',
    'https://huggingface.co/models?sort=trending',
    'https://huggingface.co/datasets?sort=trending',
    'https://arxiv.org/list/cs.AI/recent',
    'https://arxiv.org/list/cs.SE/recent',
    'https://news.ycombinator.com/',
]
for url in seeds:
    con.execute('DELETE FROM visited WHERE url=?', (url,))
    con.execute('INSERT OR IGNORE INTO frontier(url,score,depth,parent,added_ts) VALUES (?,?,?,NULL,?)',
                (url, 0.95, 0, int(time.time())))
con.commit()
print(f'  re-seeded {len(seeds)} URLs')
"
        sleep 30
        continue
    fi

    # Process in parallel
    JOBS=0
    while IFS='|' read -r URL DEPTH; do
        [[ -z "$URL" ]] && continue
        # Remove from frontier (atomic)
        sqlite3 "$DB" "DELETE FROM frontier WHERE url='$URL';" 2>/dev/null
        # Spawn fetch
        fetch_one "$URL" "$DEPTH" >> "$LOG" 2>&1 &
        JOBS=$((JOBS + 1))
        if [[ $JOBS -ge $PARALLEL ]]; then
            wait -n 2>/dev/null || wait
            JOBS=$((JOBS - 1))
        fi
    done <<< "$BATCH"
    wait  # finish remaining

    # Brief cool-down between batches
    VISITED=$(sqlite3 "$DB" "SELECT COUNT(*) FROM visited;")
    PENDING=$(sqlite3 "$DB" "SELECT COUNT(*) FROM frontier;")
    echo "[$(date +%H:%M:%S)] batch done · visited=$VISITED · pending=$PENDING" >> "$LOG"

    # Sleep adaptively: short if frontier full, longer if empty/rate-limit risk
    if [[ $PENDING -gt 100 ]]; then
        sleep 5
    elif [[ $PENDING -gt 20 ]]; then
        sleep 15
    else
        sleep 30
    fi
done

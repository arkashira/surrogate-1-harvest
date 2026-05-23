#!/usr/bin/env bash
# GitHub domain-systematic scraper v2 — token-rotating, full-taxonomy, ledger-driven
# Fixes from v1: proper error logging, extraction logic copied from working bulk-train
set -u
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LEDGER="$HOME/.surrogate/state/scrape-ledger.db"
LOG="$HOME/.surrogate/logs/github-domain-scrape.log"
DATE=$(date +%Y-%m-%d)
OUT="$HOME/axentx/surrogate/data/training-jsonl/github-domain-${DATE}.jsonl"
mkdir -p "$(dirname "$LOG")" "$(dirname "$OUT")"

[[ ! -f "$LEDGER" ]] && bash "$HOME/.surrogate/bin/scrape-ledger-init.sh"

TARGET="${1:-}"
export LEDGER OUT GITHUB_TOKEN GITHUB_TOKEN_POOL TARGET

python3 <<'PYEOF' 2>&1 | tee -a "$LOG"
import os, json, urllib.request, urllib.parse, re, time, base64, sqlite3, random
from datetime import datetime
from pathlib import Path

LEDGER = os.environ['LEDGER']
OUT = Path(os.environ['OUT'])
POOL = [t.strip() for t in os.environ.get('GITHUB_TOKEN_POOL','').split(',') if t.strip()]
if not POOL: POOL = [os.environ.get('GITHUB_TOKEN','')]
TARGET = os.environ.get('TARGET', '')
print(f"[{datetime.now().strftime('%H:%M:%S')}] token pool: {len(POOL)} tokens")

_tok_idx = [0]
def next_token():
    """Round-robin token rotation."""
    t = POOL[_tok_idx[0] % len(POOL)]
    _tok_idx[0] += 1
    return t

def gh_req(url, timeout=15, retry_on_403=True):
    for attempt in range(len(POOL) + 1):  # try each token at most once
        t = next_token()
        req = urllib.request.Request(url, headers={
            'Accept':'application/vnd.github+json',
            'Authorization': f'Bearer {t}'
        })
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 403 and retry_on_403 and attempt < len(POOL):
                print(f"    [403 tok#{_tok_idx[0]%len(POOL)}] retrying next token")
                continue
            raise
        except Exception:
            raise
    raise RuntimeError("all tokens exhausted")

conn = sqlite3.connect(LEDGER, timeout=30)
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('PRAGMA busy_timeout=15000')
cur = conn.cursor()

# Pick domain slot (priority + least scraped)
if TARGET and '/' in TARGET:
    domain, sub = TARGET.split('/', 1)
    cur.execute("SELECT domain, subdomain, search_keywords, target_repos FROM domain_taxonomy WHERE domain=? AND subdomain=?", (domain, sub))
else:
    cur.execute("""
        SELECT t.domain, t.subdomain, t.search_keywords, t.target_repos
        FROM domain_taxonomy t
        LEFT JOIN (SELECT domain, subdomain, COUNT(*) AS n FROM scraped GROUP BY domain, subdomain) s
            ON t.domain=s.domain AND t.subdomain=s.subdomain
        WHERE COALESCE(s.n, 0) < t.target_repos
        ORDER BY t.priority ASC, COALESCE(s.n, 0) ASC, RANDOM()
        LIMIT 1
    """)
row = cur.fetchone()
if not row:
    print("[done] no domain needs scraping"); exit()
domain, sub, keywords, target = row
cur.execute("SELECT COUNT(*) FROM scraped WHERE domain=? AND subdomain=?", (domain, sub))
already = cur.fetchone()[0]
remaining = max(0, target - already)
print(f"[slot] {domain}/{sub} already={already} target={target} remaining={remaining}")
if remaining <= 0: exit()

SCRUB = [
    (r'\bAshira[^\s@]*\b', 'user'), (r'\bอชิระ[^\s]*\b', 'user'),
    (r'\bPitchaya\w*\b', ''), (r'\bTHINKBIT\w*\b', 'company'),
    (r'\bCIMB\b', 'company'), (r'\bSwiftlet\b', 'company'), (r'\bKMITL\b', 'school'),
]
def scrub(t):
    if not t: return t
    for p, r in SCRUB: t = re.sub(p, r, t, flags=re.IGNORECASE)
    return t

def write_pair(instr, resp, tag, lang, path):
    if len(instr) < 20 or len(resp) < 100: return 0
    pair = {
        'instruction': scrub(instr[:500]),
        'response': scrub(resp[:3500]),
        'source': f'github-domain:{domain}/{sub}',
        'domain': domain, 'subdomain': sub,
        'language': lang, 'path': path[:200], 'repo': tag,
        'timestamp': datetime.utcnow().isoformat(),
    }
    with open(OUT, 'a') as f:
        f.write(json.dumps(pair, ensure_ascii=False) + '\n')
    return 1

# Do 2 searches with different keyword combos
kw_list = keywords.split()
searches = set()
for _ in range(4):
    if len(kw_list) >= 2: searches.add(' '.join(random.sample(kw_list, 2)))
    else: searches.add(kw_list[0])
searches = list(searches)[:3]

total_pairs = 0
repos_done = 0
budget = min(remaining, 50)  # 30 repos per run (bumped from 12 — 3-token pool handles it)

for kw in searches:
    if repos_done >= budget: break
    # DEEP scan: pages 1-5 = top 150 by stars, instead of just top 30
    for page in range(1, 6):
        if repos_done >= budget: break
        q = urllib.parse.quote(f"{kw} stars:>30 pushed:>2024-01-01")
        try:
            with gh_req(f'https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page=30&page={page}') as r:
                d = json.load(r)
        except Exception as e:
            print(f"  [search err '{kw}' p{page}] {e}"); break
        items = d.get('items', [])
        if not items: break
        print(f"  [search '{kw}' p{page}] {len(items)} items")
        # Inline the inner loop body (indent carefully)
        for repo in items:
            if repos_done >= budget: break
            full = repo['full_name']
            stars = repo.get('stargazers_count', 0)
            desc = repo.get('description') or ''
            lang = repo.get('language') or ''
            cur.execute("SELECT 1 FROM scraped WHERE source=? AND identifier=?", ('github', full))
            if cur.fetchone(): continue
            pairs_this = 0
            readme_ok = False
            try:
                with gh_req(f'https://api.github.com/repos/{full}/readme') as r:
                    rd = json.load(r)
                raw = rd.get('content','') or ''
                if raw:
                    readme = base64.b64decode(raw).decode('utf-8', errors='replace')
                    if len(readme) > 200:
                        readme_ok = True
                        pairs_this += write_pair(f"อธิบาย repository '{full}' ({stars}⭐, {domain}/{sub}). Description: {desc[:150]}", readme[:3000], full, lang, 'README.md')
                        blocks = re.findall(r'```(\w*)\n(.*?)```', readme, re.DOTALL)
                        for blang, bcode in blocks[:2]:
                            if len(bcode) > 80:
                                pairs_this += write_pair(f"Show a minimal working example from {full} ({domain}/{sub})", bcode[:1500], full, blang or lang, 'README.md:code')
            except Exception: pass
            try:
                with gh_req(f'https://api.github.com/repos/{full}/git/trees/HEAD?recursive=1') as r:
                    tree = json.load(r)
                files = tree.get('tree', [])
                code_files = [f for f in files if f.get('type')=='blob' and 200<f.get('size',0)<40000 and f['path'].endswith(('.py','.go','.rs','.ts','.tsx','.js','.yaml','.yml','.tf','.hcl','.sh','.md','.java','.kt')) and not any(sk in f['path'].lower() for sk in ['test_','_test.','spec.','tests/','vendor/','node_modules','.lock','.min.js'])][:5]
                for f_ent in code_files:
                    path = f_ent['path']
                    try:
                        with urllib.request.urlopen(f'https://raw.githubusercontent.com/{full}/HEAD/{path}', timeout=10) as r2:
                            content = r2.read().decode('utf-8', errors='replace')[:8000]
                        if len(content) > 200:
                            ext = path.split('.')[-1] if '.' in path else ''
                            pairs_this += write_pair(f"Explain '{path}' from {full} ({domain}/{sub}).", content, full, ext, path)
                    except Exception: continue
            except Exception: pass
            try:
                cur.execute("INSERT OR IGNORE INTO scraped (source, identifier, domain, subdomain, language, stars, scraped_at, pairs_written, status) VALUES (?,?,?,?,?,?,?,?,?)",
                    ('github', full, domain, sub, lang, stars, datetime.utcnow().isoformat(), pairs_this, 'ok' if pairs_this else 'empty'))
                conn.commit()
            except Exception as e: print(f"    [ledger err] {e}")
            total_pairs += pairs_this
            repos_done += 1
            if pairs_this: print(f"    ✓ p{page} {full} ({stars}⭐) → {pairs_this}")
            time.sleep(0.1)
    continue  # skip old single-page block below

    for repo in items:
        if repos_done >= budget: break
        full = repo['full_name']
        stars = repo.get('stargazers_count', 0)
        desc = repo.get('description') or ''
        lang = repo.get('language') or ''

        # Ledger dedup
        cur.execute("SELECT 1 FROM scraped WHERE source=? AND identifier=?", ('github', full))
        if cur.fetchone():
            continue

        pairs_this = 0
        readme_ok = False
        # README
        try:
            with gh_req(f'https://api.github.com/repos/{full}/readme') as r:
                rd = json.load(r)
            raw = rd.get('content','') or ''
            if raw:
                readme = base64.b64decode(raw).decode('utf-8', errors='replace')
                if len(readme) > 200:
                    readme_ok = True
                    pairs_this += write_pair(
                        f"อธิบาย repository '{full}' ({stars}⭐, {domain}/{sub}). Description: {desc[:150]}",
                        readme[:3000], full, lang, 'README.md'
                    )
                    # Code block from README
                    blocks = re.findall(r'```(\w*)\n(.*?)```', readme, re.DOTALL)
                    for blang, bcode in blocks[:2]:
                        if len(bcode) > 80:
                            pairs_this += write_pair(
                                f"Show a minimal working example from {full} ({domain}/{sub})",
                                bcode[:1500], full, blang or lang, 'README.md:code'
                            )
        except urllib.error.HTTPError as e:
            print(f"    [readme err {full}] {e.code}")
        except Exception as e:
            print(f"    [readme err {full}] {type(e).__name__}: {str(e)[:60]}")

        # Top source files
        try:
            with gh_req(f'https://api.github.com/repos/{full}/git/trees/HEAD?recursive=1') as r:
                tree = json.load(r)
            files = tree.get('tree', [])
            code_files = [f for f in files
                          if f.get('type') == 'blob'
                          and 200 < f.get('size', 0) < 40000
                          and f['path'].endswith(('.py','.go','.rs','.ts','.tsx','.js','.yaml','.yml','.tf','.hcl','.sh','.md','.java','.kt'))
                          and not any(sk in f['path'].lower() for sk in ['test_','_test.','spec.','tests/','vendor/','node_modules','.lock','.min.js'])][:5]
            for f_ent in code_files:
                path = f_ent['path']
                try:
                    url = f'https://raw.githubusercontent.com/{full}/HEAD/{path}'
                    with urllib.request.urlopen(url, timeout=10) as r2:
                        content = r2.read().decode('utf-8', errors='replace')[:8000]
                    if len(content) > 200:
                        ext = path.split('.')[-1] if '.' in path else ''
                        pairs_this += write_pair(
                            f"Explain '{path}' from {full} ({domain}/{sub}).",
                            content, full, ext, path
                        )
                except Exception: continue
        except Exception as e:
            print(f"    [tree err {full}] {type(e).__name__}")

        # Ledger write (always, even if pairs=0 so we don't retry immediately)
        try:
            cur.execute(
                "INSERT OR IGNORE INTO scraped (source, identifier, domain, subdomain, language, stars, scraped_at, pairs_written, status) VALUES (?,?,?,?,?,?,?,?,?)",
                ('github', full, domain, sub, lang, stars, datetime.utcnow().isoformat(), pairs_this, 'ok' if pairs_this else 'empty')
            )
            conn.commit()
        except Exception as e:
            print(f"    [ledger err] {e}")

        total_pairs += pairs_this
        repos_done += 1
        if pairs_this:
            print(f"    ✓ {full} ({stars}⭐) → {pairs_this} pairs")
        else:
            print(f"    ∅ {full} ({stars}⭐) → 0 pairs (readme_ok={readme_ok})")
        time.sleep(0.1)

conn.close()
print(f"[done] {domain}/{sub}: {repos_done} repos, {total_pairs} pairs")
PYEOF

#!/usr/bin/env bash
# Auto-tune scrape keywords — finds slots returning 0 GitHub results and replaces with broader keywords.
#
# Strategy:
#   1. Test every taxonomy slot's keywords against GitHub Search API
#   2. If 0 results, generate fallback keywords (broader, single-term)
#   3. If fallback works (>20 results), update DB
#   4. Track consecutive_zero_runs — slots failing > 5 times get marked low-priority
#
# Runs as LaunchAgent every 60 min. Pairs with domain-scrape-loop which uses fixed keywords.
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/scrape-keyword-tuner.log"
mkdir -p "$(dirname "$LOG")"

TOKEN="${GITHUB_TOKEN_POOL%%,*}"   # first non-empty
[[ -z "$TOKEN" ]] && { echo "[$(date +%H:%M:%S)] no token" >> "$LOG"; exit 0; }

# Resource guard: pause if system stressed (20% headroom rule)
LOAD=$(uptime | awk -F'load averages:' '{print $2}' | awk '{print int($1)}')
FREE=$(vm_stat | awk '/Pages free/{gsub("[.]","",$3); print int($3)}')
if [[ $LOAD -gt 8 ]] || [[ $FREE -lt 50000 ]]; then
    echo "[$(date +%H:%M:%S)] resource-pause: load=$LOAD free=$FREE — skipping tune cycle" >> "$LOG"
    exit 0
fi

START=$(date +%s)
echo "[$(date +%H:%M:%S)] tune cycle start" >> "$LOG"

# Iterate slots, focus on ones with consecutive_zero_runs > 0 first
python3 <<PYEOF >> "$LOG" 2>&1
import os, re, json, sqlite3, time, urllib.request, urllib.error, urllib.parse

TOKEN = "$TOKEN"
DB = os.path.expanduser("~/.surrogate/state/scrape-ledger.db")

def github_count(keywords: str) -> int:
    """Return total_count from GitHub Search API (or -1 on error)."""
    q = urllib.parse.quote(keywords + " stars:>20")
    url = f"https://api.github.com/search/repositories?q={q}&per_page=1"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
            return d.get("total_count", 0)
    except urllib.error.HTTPError as e:
        if e.code == 403:  # rate limit
            time.sleep(60)
        return -1
    except Exception:
        return -1


# Fallback strategy: try simpler variants of broken keywords
def fallback_variants(domain: str, sub: str, current_kw: str) -> list[str]:
    variants = []
    # 1. Just the subdomain word
    variants.append(sub.replace("-", " "))
    # 2. Domain + first word of current keywords
    first = current_kw.split()[0] if current_kw else ""
    if first:
        variants.append(first)
    # 3. Domain alone (last resort)
    variants.append(domain)
    # 4. Common synonyms by domain
    syn_map = {
        ("ai", "agents"): ["agent-framework"],
        ("ai", "rag"): ["retrieval-augmented-generation"],
        ("security", "appsec"): ["application-security"],
        ("ops", "kubernetes"): ["kubernetes"],
        ("cloud", "aws"): ["aws"],
        ("cloud", "gcp"): ["google-cloud"],
        ("data", "streaming"): ["kafka stream"],
    }
    if (domain, sub) in syn_map:
        variants = syn_map[(domain, sub)] + variants
    # Dedup keeping order
    seen = set(); out = []
    for v in variants:
        if v and v not in seen: seen.add(v); out.append(v)
    return out


conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("""
    SELECT domain, subdomain, search_keywords, consecutive_zero_runs
    FROM domain_taxonomy
    ORDER BY consecutive_zero_runs DESC, RANDOM()
    LIMIT 30
""")
slots = cur.fetchall()

tuned = 0; ok = 0; still_bad = 0
for domain, sub, current_kw, czr in slots:
    if not current_kw: continue

    cnt = github_count(current_kw)
    time.sleep(0.3)

    if cnt > 20:
        # Working — reset counter
        if czr > 0:
            cur.execute("UPDATE domain_taxonomy SET consecutive_zero_runs=0, last_keyword_test=? WHERE domain=? AND subdomain=?",
                       (str(cnt), domain, sub))
            print(f"  ✓ {domain}/{sub}: {cnt} results — reset counter (was {czr})")
        ok += 1
        continue

    # 0 results or error — try fallbacks
    print(f"  ⚠ {domain}/{sub}: {cnt} results with '{current_kw}' — searching fallback")
    best_kw = None; best_cnt = 0
    for variant in fallback_variants(domain, sub, current_kw):
        v_cnt = github_count(variant)
        time.sleep(0.3)
        if v_cnt > best_cnt and v_cnt > 20:
            best_cnt = v_cnt; best_kw = variant

    if best_kw:
        cur.execute("UPDATE domain_taxonomy SET search_keywords=?, consecutive_zero_runs=0, last_keyword_test=? WHERE domain=? AND subdomain=?",
                   (best_kw, str(best_cnt), domain, sub))
        print(f"    → updated to '{best_kw}' ({best_cnt} results)")
        tuned += 1
    else:
        cur.execute("UPDATE domain_taxonomy SET consecutive_zero_runs=consecutive_zero_runs+1 WHERE domain=? AND subdomain=?",
                   (domain, sub))
        print(f"    ✗ no working fallback (consecutive_zero_runs += 1)")
        still_bad += 1

conn.commit()
conn.close()
print(f"[$(date +%H:%M:%S)] tune done: ok={ok} tuned={tuned} still_bad={still_bad}")
PYEOF

DUR=$(( $(date +%s) - START ))
echo "[$(date +%H:%M:%S)] tune cycle done in ${DUR}s" >> "$LOG"

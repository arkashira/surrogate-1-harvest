#!/usr/bin/env bash
# Shared context builder — sourced by qwen-coder-worker + dev-cloud-worker.
# Produces rich context: repo-map + similar functions from project + past accepted examples.
# Call: build_rich_context <project> <priority_id> <priority_title>
# Sets env vars: REPO_MAP, SIMILAR_FUNCS, FEWSHOT_ACCEPTED, ANTI_PATTERNS
build_rich_context() {
    local PRIO_PROJECT="$1"
    local PRIO_ID="$2"
    local PRIO_TITLE="$3"
    local SHARED="$HOME/.hermes/workspace/swarm-shared"
    local PROJECT_DIR="$HOME/axentx/$PRIO_PROJECT"

    # 1. Full repo-map (up to 10KB — was 3KB).
    # build-repo-map.sh writes to "<proj>_map.md"; some older paths used "<proj>.md".
    # Try both so we don't silently lose the strongest grounding signal.
    REPO_MAP=""
    for candidate in "$SHARED/repo-maps/${PRIO_PROJECT}_map.md" "$SHARED/repo-maps/${PRIO_PROJECT}.md"; do
        if [[ -f "$candidate" ]]; then
            REPO_MAP=$(head -c 10000 "$candidate")
            break
        fi
    done

    # 2. Similar function signatures from project (grep in real codebase)
    SIMILAR_FUNCS=""
    if [[ -d "$PROJECT_DIR" ]]; then
        # Extract keywords from title for grep
        local KW=$(echo "$PRIO_TITLE" | /usr/bin/tr '[:upper:]' '[:lower:]' | /usr/bin/tr -cs 'a-z0-9' ' ' | /usr/bin/tr ' ' '\n' | awk 'length>4' | head -3 | /usr/bin/tr '\n' '|' | sed 's/|$//')
        if [[ -n "$KW" ]]; then
            SIMILAR_FUNCS=$(/usr/bin/find "$PROJECT_DIR" -type f \( -name '*.py' -o -name '*.ts' -o -name '*.tsx' -o -name '*.js' -o -name '*.go' \) ! -path '*/node_modules/*' ! -path '*/.hermes-*' 2>/dev/null | \
                xargs grep -lE "($KW)" 2>/dev/null | head -3 | while read f; do
                    echo "=== ${f#$PROJECT_DIR/} ==="
                    grep -A3 -E "^(def|function|export const|class|async def|interface)" "$f" 2>/dev/null | head -30
                done 2>/dev/null | head -c 4000)
        fi
    fi

    # 3. RAG: actual code patterns from project (SQLite FTS via ask-sqlite.py if exists)
    RAG_EXAMPLES=""
    if [[ -x "$HOME/.surrogate/bin/ask-sqlite.py" ]]; then
        RAG_EXAMPLES=$(python3 "$HOME/.surrogate/bin/ask-sqlite.py" \
            "$PRIO_PROJECT $PRIO_TITLE" 2>/dev/null | head -c 3000)
    fi

    # 4. Semantic RAG (from embeddings) — top-5 similar
    SEMANTIC_RAG=""
    if [[ -f "$HOME/.surrogate/embeddings.db" ]]; then
        SEMANTIC_RAG=$(python3 "$HOME/.surrogate/bin/embed-doc.py" --query "$PRIO_TITLE" 2>/dev/null | head -c 2000)
    fi

    # 5. Past ACCEPTED examples (few-shot from quality≥7 history)
    FEWSHOT_ACCEPTED=""
    for review in $(/bin/ls -t "$HOME/.hermes/workspace/qwen-coder-reviews/"*.review.json 2>/dev/null | head -30); do
        if grep -qE '"quality_score":\s*[789]|"quality_score":\s*10' "$review" 2>/dev/null; then
            local OUT_FILE=$(basename "$review" .review.json)
            # Search all worker output dirs
            for WD in qwen-coder dev-cloud-samba dev-cloud-github dev-cloud-cloudflare dev-cloud-groq dev-cloud-synthesis; do
                local OUT_PATH="$HOME/.hermes/workspace/$WD/${OUT_FILE}.md"
                if [[ -f "$OUT_PATH" ]]; then
                    FEWSHOT_ACCEPTED=$(head -c 2000 "$OUT_PATH")
                    break 2
                fi
            done
        fi
    done

    # 6. Anti-patterns (last 5 rejection reasons across all workers)
    ANTI_PATTERNS=""
    for review in $(/bin/ls -t "$HOME/.hermes/workspace/qwen-coder-reviews/"*.review.json 2>/dev/null | head -10); do
        local bugs=$(python3 -c "
import json, re, sys
try:
    txt = open('$review').read()
    m = re.search(r'\{.*\}', txt, re.DOTALL)
    if not m: sys.exit()
    d = json.loads(m.group(0))
    if d.get('verdict') in ('reject','rework') and d.get('bugs'):
        for b in d['bugs'][:2]:
            print(f'- {b[:180]}')
except: pass
" 2>/dev/null)
        [[ -n "$bugs" ]] && ANTI_PATTERNS="$ANTI_PATTERNS$bugs"$'\n'
    done
    ANTI_PATTERNS=$(echo "$ANTI_PATTERNS" | head -10)

    # 7. Active-learning prompt deltas — aggregate last 5 UNIQUE anti-patterns.
    # Preference: same-project anti-patterns first, then generic.
    # Dedup by first 80 chars of prompt_addition (similar bugs shouldn't bloat prompt).
    PROMPT_DELTAS=""
    if [[ -f "$HOME/.surrogate/memory/worker-prompt-deltas.jsonl" ]]; then
        PROMPT_DELTAS=$(python3 -c "
import json, sys
from pathlib import Path
try:
    entries = []
    for l in Path('$HOME/.surrogate/memory/worker-prompt-deltas.jsonl').read_text().splitlines():
        if not l.strip(): continue
        try: entries.append(json.loads(l))
        except: pass
    # Dedup by first 80 chars
    seen = set()
    picked = []
    # Walk newest → oldest, cap 5 unique
    for e in reversed(entries):
        addn = (e.get('prompt_addition') or '').strip()
        if not addn: continue
        key = addn[:80]
        if key in seen: continue
        seen.add(key)
        picked.append(addn)
        if len(picked) >= 5: break
    if picked:
        out = ['ACTIVE-LEARNED RULES (avoid these past mistakes):']
        for i, a in enumerate(picked, 1):
            out.append(f'{i}. {a[:400]}')
        print('\n'.join(out))
except Exception as e: pass
" 2>/dev/null)
    fi

    # 8. Priority full spec (if a detailed spec file exists)
    # Spec is the single most important signal — cap high (6KB) so the full
    # Context/Requirements/DO NOT sections fit.  Other RAG signals are capped
    # lower because they're supplementary; the spec is authoritative.
    PRIO_SPEC=""
    local SPEC_FILE="$HOME/.hermes/workspace/swarm-shared/specs/${PRIO_ID}.md"
    [[ -f "$SPEC_FILE" ]] && PRIO_SPEC=$(head -c 6000 "$SPEC_FILE")

    # 9. Task-type authoritative sources — boost scraped knowledge based on title.
    # Security task → CVE/MITRE/OWASP/Prowler. SRE → Google SRE/postmortems.
    # Observability → OTel/Prometheus/Grafana/Honeycomb. etc.
    # This is THE fix that makes all our scraping actually used by Hermes workers.
    AUTHORITATIVE_CONTEXT=""
    if [[ -f "$HOME/.surrogate/index.db" ]]; then
        AUTHORITATIVE_CONTEXT=$(python3 <<PYEOF
import sqlite3, re
title = """${PRIO_TITLE}""".lower()
project = """${PRIO_PROJECT}""".lower()
# Classify task → preferred source whitelist
routes = {
    # Security tasks
    ('security','cve','vuln','prowler','kyverno','opa','admission','ciem','sigma','mitre','attack','cosign','sbom','falco','threat','malware','exploit'): ['cisa-kev','mitre-attack','owasp-cheatsheet','domain:sec-cloudsec','domain:sec-appsec','domain:sec-devsecops','code-deep:sec-appsec','code-deep:sec-cloudsec'],
    # SRE / incident / postmortem
    ('sre','slo','sli','incident','postmortem','runbook','chaos','rca','dora','mttr','blameless','on-call','pager','outage'): ['google-sre','postmortems-index','firecrawl','eng-blog:charity-majors','eng-blog:high-scalability','mythos-ai-engineering','domain:ops-sre','code-deep:ops-sre'],
    # Observability
    ('observab','otel','telemetry','prometheus','grafana','loki','tempo','metric','trace','log','honeycomb','ebpf'): ['opentelemetry-spec','prometheus-docs','grafana-docs','firecrawl','domain:ops-observability'],
    # Cloud / K8s / Terraform
    ('kubernetes','k8s','helm','istio','terraform','aws','ecs','eks','lambda','cloudformation','cdk','gcp','azure','argocd','flux'): ['firecrawl','github-public','code-deep:ops-devops','domain:ops-devops','mythos-cloud','github-trending'],
    # AI / multi-agent
    ('agent','autogen','crewai','langgraph','orchestra','mcp','reflexion','dspy','rag','llm'): ['anthropic-cookbook','arxiv','mythos-ai-agent','mythos-ai-engineering','domain:ai-engineering','code-deep:ai-engineering','firecrawl','hf-papers'],
    # FinOps
    ('cost','finops','focus','rightsizing','kubecost','opencost','savings','budget','spend','waste'): ['firecrawl','rss','eng-blog:high-scalability','domain:ops-devops','arxiv'],
    # Frontend / FE
    ('frontend','react','nextjs','typescript','tsx','ui'): ['domain:dev-frontend','domain:design-ux','code-deep:dev-frontend','stackoverflow','github-trending'],
    # Backend / API / DB
    ('backend','api','fastapi','database','sql','postgres','asyncpg','sqlalchemy'): ['domain:dev-backend','domain:dev-fullstack','code-deep:dev-backend','github-public','stackoverflow','hf-papers'],
    # Mobile
    ('mobile','android','ios','flutter','reactnative','line','workio'): ['domain:dev-mobile','code-deep:dev-mobile','firecrawl','stackoverflow'],
}
# Project-specific boost
project_preferred = {
    'vanguard': ['cisa-kev','mitre-attack','owasp-cheatsheet','code-deep:sec-appsec'],
    'costinel': ['firecrawl','rss','arxiv','mythos-ai-engineering'],
    'arkship':  ['google-sre','postmortems-index','anthropic-cookbook','opentelemetry-spec','firecrawl'],
    'surrogate':['arxiv','hf-papers','anthropic-cookbook','mythos-ai-agent'],
    'workio':   ['firecrawl','stackoverflow','github-public'],
}

preferred_sources = set()
for keywords, srcs in routes.items():
    if any(k in title for k in keywords):
        preferred_sources.update(srcs)
for proj_key, srcs in project_preferred.items():
    if proj_key in project:
        preferred_sources.update(srcs)

if not preferred_sources:
    print(''); exit()

# FTS query — prefer authoritative sources
conn = sqlite3.connect('$HOME/.surrogate/index.db')
conn.row_factory = sqlite3.Row
# Simple keyword from title
kw = ' '.join([w for w in re.sub(r'[^a-zA-Z0-9 ]', ' ', title).split() if len(w) > 3][:5])
if not kw: exit()

src_list = ','.join(f"'{s}'" for s in preferred_sources)
# Strategy: 3-tier fallback — preferred+match → any+match → preferred random
rows = []
try:
    # Tier 1: preferred sources + FTS match on keywords
    q = f"""SELECT d.source, d.instruction, substr(d.response, 1, 600) as body
            FROM docs_fts f JOIN docs d ON d.id = f.rowid
            WHERE f.docs_fts MATCH ? AND d.source IN ({src_list})
            ORDER BY bm25(docs_fts) LIMIT 6"""
    rows = conn.execute(q, (kw,)).fetchall()
except sqlite3.OperationalError: pass

if not rows:
    # Tier 2: FTS match on ANY source — relax source filter
    try:
        q2 = """SELECT d.source, d.instruction, substr(d.response, 1, 600) as body
                FROM docs_fts f JOIN docs d ON d.id = f.rowid
                WHERE f.docs_fts MATCH ? ORDER BY bm25(docs_fts) LIMIT 6"""
        rows = conn.execute(q2, (kw,)).fetchall()
    except sqlite3.OperationalError: pass

if not rows:
    # Tier 3: random sample from preferred sources (even if no keyword match)
    rows = conn.execute(f"SELECT source, instruction, substr(response,1,600) as body FROM docs WHERE source IN ({src_list}) ORDER BY RANDOM() LIMIT 6").fetchall()

conn.close()

out = []
for r in rows:
    out.append(f"[{r['source']}] {(r['instruction'] or '')[:120]}")
    out.append((r['body'] or '')[:500])
    out.append('')
print('\n'.join(out)[:3500])
PYEOF
)
    fi

    # 10. FalkorDB graph — related decisions + past priorities with similar theme
    GRAPH_CONTEXT=""
    local REDIS_SOCK=$(/usr/bin/find /var/folders /tmp -name 'redis.socket' -type s 2>/dev/null | head -1)
    if [[ -n "$REDIS_SOCK" ]]; then
        # Get related priorities + learned rules
        GRAPH_CONTEXT=$(/opt/homebrew/bin/redis-cli -s "$REDIS_SOCK" GRAPH.QUERY ashira "
            MATCH (p:Priority {project: '$PRIO_PROJECT'})
            OPTIONAL MATCH (p)-[:HAS_LEARNED_RULE]->(l:LearnedRule)
            OPTIONAL MATCH (p)-[:COMMITTED_AS]->(c:Commit)
            RETURN p.id, p.title, l.content, c.msg LIMIT 8
        " 2>/dev/null | tail -c 2500)
    fi

    # 11. Hermes trace recall — past similar tasks Hermes handled (from JSONL)
    HERMES_RECALL=""
    local TRACE_DIR="$HOME/axentx/surrogate/data/training-jsonl"
    if [[ -d "$TRACE_DIR" ]]; then
        HERMES_RECALL=$(python3 <<PYEOF
import json, re, glob
title = """${PRIO_TITLE}""".lower()
words = [w for w in re.sub(r'[^a-zA-Z0-9 ]', ' ', title).split() if len(w) > 4][:4]
if not words: exit()

hits = []
# Walk recent hermes-trace-YYYY-MM-DD.jsonl files (last 7 days)
import os
files = sorted(glob.glob(os.path.expanduser('~/axentx/surrogate/data/training-jsonl/hermes-trace-*.jsonl')))[-7:]
for f in files:
    try:
        for line in open(f):
            try: rec = json.loads(line)
            except: continue
            blob = (rec.get('instruction','') + ' ' + rec.get('output',''))[:2000].lower()
            score = sum(1 for w in words if w in blob)
            if score >= 2:
                hits.append((score, rec))
    except: pass

hits.sort(key=lambda x: -x[0])
for score, rec in hits[:3]:
    print(f"HERMES PREVIOUSLY [{rec.get('category','?')}]: {rec.get('instruction','')[:120]}")
    print(f"→ {rec.get('output','')[:400]}")
    print()
PYEOF
)
    fi
}

export -f build_rich_context

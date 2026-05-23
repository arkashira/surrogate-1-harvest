#!/usr/bin/env bash
# Complement scrape-github-ops.sh — focus on DEV/frontend/backend code patterns.
# Topics the ops scraper misses: React, testing, API design, database patterns, code style.
set -u
WORK="/tmp/github-dev-scrape"
SEEN="$HOME/.claude/state/github-dev-seen.txt"
LOG="$HOME/.claude/logs/scrape-dev-patterns.log"
MIN_FREE_GB=5
STARS_MIN=2000        # higher bar — want only well-established patterns
TOP_PER_TOPIC=3       # fewer per topic, broader coverage
MAX_PER_RUN=15        # cap total per run

mkdir -p "$WORK" "$(dirname "$SEEN")" "$(dirname "$LOG")"

touch "$SEEN"

echo "[$(date '+%Y-%m-%d %H:%M')] === dev-patterns scrape start ===" | tee -a "$LOG"

TOPICS=(
    # Frontend
    react typescript nextjs vue svelte shadcn-ui tailwindcss
    # Backend patterns
    fastapi django-rest nestjs express-typescript golang-web rust-web
    # Testing
    jest vitest playwright cypress pytest-patterns testing-library
    # API design
    graphql openapi grpc trpc rest-api-design
    # DB patterns
    sqlalchemy prisma drizzle-orm typeorm postgresql-patterns
    # Code quality
    eslint-config ruff-config clean-architecture-example ddd-example
    # State mgmt
    zustand redux-toolkit tanstack-query react-query
    # Auth patterns
    oauth2-example jwt-auth clerk nextauth
    # AI integration
    langchain langgraph llamaindex pydantic-ai autogen agno
    # Microservices
    microservices-patterns event-sourcing cqrs-example saga-pattern
)

COUNT=0
for TOPIC in "${TOPICS[@]}"; do
    FREE_GB=$(df -g ~ | tail -1 | awk '{print $4}')
    [[ "$FREE_GB" -lt "$MIN_FREE_GB" ]] && { echo "disk low — stop" | tee -a "$LOG"; break; }
    [[ $COUNT -ge $MAX_PER_RUN ]] && { echo "max repos reached" | tee -a "$LOG"; break; }

    echo "  [topic] $TOPIC" | tee -a "$LOG"
    RESULT=$(gh api -X GET search/repositories \
        -f q="topic:$TOPIC stars:>$STARS_MIN pushed:>$(date -v-90d +%Y-%m-%d)" \
        -f sort=stars -f order=desc -f per_page=$TOP_PER_TOPIC 2>/dev/null || echo '{"items":[]}')
    REPOS=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); [print(r['full_name'], r.get('default_branch','main')) for r in d.get('items',[])]" 2>/dev/null)

    while IFS=' ' read -r REPO BRANCH; do
        [[ -z "$REPO" ]] && continue
        [[ $COUNT -ge $MAX_PER_RUN ]] && break
        grep -qxF "$REPO" "$SEEN" && continue
        
        DIR="$WORK/${REPO//\//_}"
        echo "  [$(date +%H:%M:%S)] $REPO ($BRANCH)" >> "$LOG"
        git clone --depth 1 --filter=blob:limit=100k "https://github.com/$REPO.git" "$DIR" 2>>"$LOG" || { echo "$REPO" >> "$SEEN"; continue; }
        
        # Ingest — only *.md, *.ts/*.tsx, *.py, examples
        python3 /opt/surrogate-1-harvest/bin/ingest-training-data.py \
            --source "github-dev" --project "$REPO" --root "$DIR" \
            --patterns "*.md,*.mdx,README*,*.ts,*.tsx,*.py,*.go,*.rs,docs/**" \
            --max-file-size 30000 --max-files 50 >>"$LOG" 2>&1 || true
        
        rm -rf "$DIR"
        echo "$REPO" >> "$SEEN"
        COUNT=$((COUNT+1))
    done <<< "$REPOS"

done

echo "[$(date +%H:%M:%S)] done: $COUNT new repos" | tee -a "$LOG"

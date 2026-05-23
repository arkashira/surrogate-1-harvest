#!/usr/bin/env bash
# Mirror-sync: copy ~/.claude/bin + ~/.hermes/{scripts,config,cron} + ~/.claude/agents
# into the local clone of arkashira/hermes-toolbelt, commit changes, push to GitHub.
# Runs every 15 min via cron so any local edit is backed up within ~15 min.
# Token-auth via GITHUB_MODELS_TOKEN (has repo scope).
set -u

MIRROR="$HOME/develope/hermes-toolbelt"
LOG="$HOME/.claude/logs/mirror-sync.log"
mkdir -p "$(dirname "$LOG")"

[[ ! -d "$MIRROR/.git" ]] && { echo "[$(date '+%H:%M:%S')] mirror not initialized — run setup first" >> "$LOG"; exit 1; }

# Load token
set -a
[[ -f "$HOME/.hermes/.env" ]] && source "$HOME/.hermes/.env"
set +a
TOKEN="${GITHUB_MODELS_TOKEN:-${GITHUB_TOKEN:-}}"
[[ -z "$TOKEN" ]] && { echo "[$(date '+%H:%M:%S')] no GitHub token" >> "$LOG"; exit 1; }

cd "$MIRROR" || exit 1

# Rsync — exclude secrets + runtime state (redundant with .gitignore but safer)
/usr/bin/rsync -a --delete \
    --exclude='logs/' --exclude='*.log' \
    --exclude='state.db*' --exclude='sessions/' --exclude='workspace/' \
    --exclude='*.env*' --exclude='.env*' --exclude='auth.json' \
    --exclude='*.key' --exclude='*.pem' --exclude='credentials*' \
    --exclude='*token*' --exclude='oauth*' --exclude='interactions/' \
    --exclude='index.db' --exclude='distillation-dataset.jsonl' \
    --exclude='.DS_Store' --exclude='*.bak' --exclude='*.bak.*' \
    --exclude='__pycache__/' --exclude='*.pyc' \
    "/opt/surrogate-1-harvest/bin/" "$MIRROR/claude-bin/"

/usr/bin/rsync -a --delete \
    --exclude='logs/' --exclude='*.log' \
    --exclude='*.env*' \
    "/opt/surrogate-1-harvest/bin/" "$MIRROR/hermes-scripts/"

/usr/bin/rsync -a --delete \
    "$HOME/.hermes/config/" "$MIRROR/hermes-config/"

/usr/bin/rsync -a --delete \
    --exclude='*.bak*' --exclude='state.db*' \
    "$HOME/.hermes/cron/" "$MIRROR/hermes-cron/"

/usr/bin/rsync -a --delete \
    "$HOME/.claude/agents/" "$MIRROR/agents/"

# One-time README if missing
if [[ ! -f "$MIRROR/README.md" ]]; then
    cat > "$MIRROR/README.md" <<'EOF'
# hermes-toolbelt — Ashira's autonomous agent system

Auto-mirrored every 15 min from:
- `/opt/surrogate-1-harvest/bin/` → `claude-bin/` (bridges, workers, ceremony agents)
- `/opt/surrogate-1-harvest/bin/` → `hermes-scripts/` (orchestrators)
- `~/.hermes/config/` → `hermes-config/` (agent definitions, domain catalogs)
- `~/.hermes/cron/` → `hermes-cron/` (123+ cron job definitions)
- `~/.claude/agents/` → `agents/` (architect, dev, ops, qa, reviewer, orchestrator)

**Excluded**: logs, state.db, sessions/, workspace/, secrets (*.env, *token*, *.key)

## Architecture
See `hermes-config/ceremony-agents.json` for 19 ceremony/role agent definitions.
6 dev workers (qwen-local + github/samba/cf/groq/gemini) generate code; Sonnet reviews; validator gates; auto-healer fixes problems; pattern-distill + auto-dream clusters lessons mechanically.

## Private repo
Nothing production-sensitive committed here — no keys, no customer data, no source from axentx projects. Just the orchestration layer.
EOF
fi

# Commit + push if anything changed
/usr/bin/git add -A
if /usr/bin/git diff --staged --quiet; then
    echo "[$(date '+%H:%M:%S')] no changes" >> "$LOG"
    exit 0
fi

CHANGED=$(/usr/bin/git diff --staged --name-only | wc -l | tr -d ' ')
/usr/bin/git -c "user.name=Ashira" -c "user.email=ashira.fuse@gmail.com" \
    commit -m "auto-sync: $(date +%Y-%m-%d_%H:%M) ($CHANGED files)" --no-verify >> "$LOG" 2>&1

# Push with token auth — use direct URL with token embedded (safe: never logged unless failure)
/usr/bin/git push "https://x-access-token:${TOKEN}@github.com/arkashira/hermes-toolbelt.git" HEAD:main >> "$LOG" 2>&1

if [[ $? -eq 0 ]]; then
    echo "[$(date '+%H:%M:%S')] ✅ synced $CHANGED files → arkashira/hermes-toolbelt" >> "$LOG"
else
    echo "[$(date '+%H:%M:%S')] ⚠️ push failed — check log" >> "$LOG"
fi

#!/usr/bin/env bash
# Master sync: all sources → Obsidian inbox → FalkorDB + ChromaDB
# Sources: Apple Notes, Google Drive, Gmail, NotebookLM, AWS, gcloud, Azure, GitHub
set -uo pipefail
# Removed -e: partial source failures (e.g. missing Google OAuth) are warnings, not errors.
# Each source has its own `|| log "WARN: ..."` handling.

PYTHON="$HOME/.claude/venv/bin/python"
BIN="$HOME/.claude/bin"
LOG="$HOME/.claude/logs/sync-all-sources.log"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

log "=== sync-all-sources START ==="

# ── 1. Apple Notes ─────────────────────────────────────────────────────────
log "[1/4] Apple Notes"
bash "$BIN/sync-apple-notes.sh" 2>&1 | tee -a "$LOG" || log "WARN: Apple Notes sync failed (continuing)"

# ── 2. Google Drive ─────────────────────────────────────────────────────────
log "[2/4] Google Drive (ashira.p@think-bit.org)"
if [ -f "$HOME/.claude/credentials/google-token.json" ]; then
    "$PYTHON" "$BIN/sync-gdrive.py" 2>&1 | tee -a "$LOG" || log "WARN: Google Drive sync failed (continuing)"
else
    log "SKIP: No Google token. Run: $BIN/google-oauth-setup.py"
fi

# ── 3. Gmail ────────────────────────────────────────────────────────────────
log "[3/4] Gmail (ashira.p@think-bit.org)"
if [ -f "$HOME/.claude/credentials/google-token.json" ]; then
    # Default: last 30 days. Pass arg to override: sync-all-sources.sh 60
    DAYS="${1:-30}"
    "$PYTHON" "$BIN/sync-gmail.py" "$DAYS" 2>&1 | tee -a "$LOG" || log "WARN: Gmail sync failed (continuing)"
else
    log "SKIP: No Google token. Run: $BIN/google-oauth-setup.py"
fi

# ── 4. NotebookLM (watch folder — no API) ───────────────────────────────────
log "[4/4] NotebookLM (watch folder)"
NLM_WATCH="$HOME/Downloads"
NLM_DEST="$HOME/Documents/Obsidian Vault/AI-Hub/inbox/notebooklm"
# Move any .md / .txt files that look like NotebookLM exports
find "$NLM_WATCH" -maxdepth 1 \( -name "*.md" -o -name "*.txt" \) -newer "$NLM_DEST/.last-check" 2>/dev/null | while read -r f; do
    base=$(basename "$f")
    dest="$NLM_DEST/$(date '+%Y-%m-%d')_$base"
    # Add frontmatter if missing
    if ! head -1 "$f" | grep -q "^---"; then
        { echo "---"; echo "title: $(basename "$f" .md)"; echo "source: notebooklm"; echo "imported: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"; echo "tags: [inbox, notebooklm]"; echo "---"; echo; cat "$f"; } > "$dest"
        log "  + $dest (with frontmatter)"
    else
        cp "$f" "$dest"
        log "  + $dest"
    fi
done
touch "$NLM_DEST/.last-check"

# ── 5. Cloud CLIs ───────────────────────────────────────────────────────────
log "[5/8] AWS"
"$PYTHON" "$BIN/sync-aws.py" 2>&1 | tee -a "$LOG" || log "WARN: AWS sync failed (continuing)"

log "[6/8] Google Cloud"
"$PYTHON" "$BIN/sync-gcloud-resources.py" 2>&1 | tee -a "$LOG" || log "WARN: gcloud sync failed (continuing)"

log "[7/8] Azure"
"$PYTHON" "$BIN/sync-azure.py" 2>&1 | tee -a "$LOG" || log "WARN: Azure sync failed (continuing)"

log "[8/8] GitHub"
"$PYTHON" "$BIN/sync-github.py" 2>&1 | tee -a "$LOG" || log "WARN: GitHub sync failed (continuing)"

# ── 9. Re-index FalkorDB + ChromaDB ─────────────────────────────────────────
log "[5/5] Re-indexing FalkorDB + ChromaDB"

if [ -f "$BIN/graph-sync.sh" ]; then
    bash "$BIN/graph-sync.sh" 2>&1 | tail -3 | tee -a "$LOG" || log "WARN: graph-sync failed"
fi

if [ -f "$BIN/rag-index.sh" ]; then
    bash "$BIN/rag-index.sh" 2>&1 | tail -3 | tee -a "$LOG" || log "WARN: rag-index failed"
fi

log "=== sync-all-sources DONE ==="

exit 0  # always succeed — partial source failures are warnings, not errors

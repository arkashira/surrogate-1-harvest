#!/usr/bin/env bash
# Database snapshot backup — SQLite state.db + RAG index.db.
# Strategy:
# - state.db (49MB): compressed daily → arkashira/hermes-toolbelt/snapshots/
# - index.db (529MB): too big for git, uses GitHub Release (assets up to 2GB) via API
# - code-vector-db (6.0GB): SKIP — regeneratable from RAG via rag-index.sh
# - training-jsonl (364MB): compressed weekly → GitHub Release
set -u

LOG="$HOME/.claude/logs/db-snapshot.log"
MIRROR="$HOME/develope/hermes-toolbelt"
SNAP_DIR="$MIRROR/snapshots"
mkdir -p "$(dirname "$LOG")" "$SNAP_DIR"

set -a; [[ -f "$HOME/.hermes/.env" ]] && source "$HOME/.hermes/.env"; set +a
TOKEN="${GITHUB_MODELS_TOKEN:-${GITHUB_TOKEN:-}}"
[[ -z "$TOKEN" ]] && { echo "[$(date '+%H:%M:%S')] no token" >> "$LOG"; exit 1; }

DATE=$(date +%Y-%m-%d)

# ---- state.db compressed (SMALL — goes in git) ----
STATE_SRC="$HOME/.hermes/state.db"
STATE_OUT="$SNAP_DIR/state-${DATE}.db.gz"
if [[ -f "$STATE_SRC" ]]; then
    # Use SQLite .backup for consistent copy, then gzip
    /usr/bin/sqlite3 "$STATE_SRC" ".backup '/tmp/state-snap.db'" 2>>"$LOG"
    if [[ -f /tmp/state-snap.db ]]; then
        /usr/bin/gzip -9 -c /tmp/state-snap.db > "$STATE_OUT"
        /bin/rm -f /tmp/state-snap.db
        SIZE=$(du -h "$STATE_OUT" | awk '{print $1}')
        echo "[$(date '+%H:%M:%S')] state.db → $STATE_OUT ($SIZE)" >> "$LOG"
    fi
fi

# ---- Prune old state snapshots (keep last 14 days) ----
find "$SNAP_DIR" -name 'state-*.db.gz' -mtime +14 -delete 2>/dev/null

# ---- index.db via GitHub Release (daily asset, tag = date) ----
# Only if doc count grew significantly or no release today
INDEX_SRC="$HOME/.claude/index.db"
if [[ -f "$INDEX_SRC" ]]; then
    DOC_COUNT=$(/usr/bin/sqlite3 "$INDEX_SRC" 'SELECT COUNT(*) FROM docs' 2>/dev/null || echo 0)

    # Check if release for today already exists
    RELEASE_EXISTS=$(/usr/bin/curl -sS -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $TOKEN" \
        "https://api.github.com/repos/arkashira/hermes-toolbelt/releases/tags/snapshot-$DATE")

    if [[ "$RELEASE_EXISTS" != "200" ]]; then
        # Consistent copy
        /usr/bin/sqlite3 "$INDEX_SRC" ".backup '/tmp/index-snap.db'" 2>>"$LOG"
        /usr/bin/gzip -6 -c /tmp/index-snap.db > /tmp/index-snap.db.gz
        SIZE=$(/usr/bin/stat -f%z /tmp/index-snap.db.gz 2>/dev/null || /usr/bin/stat -c%s /tmp/index-snap.db.gz)

        # Create release via API
        REL_ID=$(/usr/bin/curl -sS -X POST \
            -H "Authorization: Bearer $TOKEN" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/repos/arkashira/hermes-toolbelt/releases" \
            -d "{\"tag_name\":\"snapshot-$DATE\",\"name\":\"RAG snapshot $DATE\",\"body\":\"index.db with $DOC_COUNT docs\",\"draft\":false,\"prerelease\":false}" \
            2>>"$LOG" | /usr/bin/python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)

        if [[ -n "$REL_ID" ]]; then
            # Upload asset
            /usr/bin/curl -sS -X POST \
                -H "Authorization: Bearer $TOKEN" \
                -H "Content-Type: application/gzip" \
                --data-binary @/tmp/index-snap.db.gz \
                "https://uploads.github.com/repos/arkashira/hermes-toolbelt/releases/$REL_ID/assets?name=index-${DATE}.db.gz" \
                > /dev/null 2>>"$LOG"
            echo "[$(date '+%H:%M:%S')] ✅ RAG snapshot $DATE uploaded ($SIZE bytes, $DOC_COUNT docs)" >> "$LOG"
        else
            echo "[$(date '+%H:%M:%S')] ⚠️ release create failed" >> "$LOG"
        fi

        /bin/rm -f /tmp/index-snap.db /tmp/index-snap.db.gz
    else
        echo "[$(date '+%H:%M:%S')] release snapshot-$DATE already exists — skip" >> "$LOG"
    fi
fi

# ---- Surrogate training data weekly (via Release) ----
if [[ "$(date +%u)" == "7" ]]; then  # Sunday
    TRAIN_DIR="$HOME/axentx/surrogate/data/training-jsonl"
    if [[ -d "$TRAIN_DIR" ]]; then
        TRAIN_OUT="/tmp/training-$DATE.tar.gz"
        /usr/bin/tar -czf "$TRAIN_OUT" -C "$HOME/axentx/surrogate/data" training-jsonl 2>>"$LOG"
        SIZE=$(/usr/bin/stat -f%z "$TRAIN_OUT")
        REL_ID=$(/usr/bin/curl -sS -X POST \
            -H "Authorization: Bearer $TOKEN" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/repos/arkashira/surrogate/releases" \
            -d "{\"tag_name\":\"training-$DATE\",\"name\":\"Training data $DATE\",\"body\":\"Weekly snapshot of claude-session distilled training data.\",\"draft\":false,\"prerelease\":false}" \
            2>>"$LOG" | /usr/bin/python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
        if [[ -n "$REL_ID" ]]; then
            /usr/bin/curl -sS -X POST \
                -H "Authorization: Bearer $TOKEN" \
                -H "Content-Type: application/gzip" \
                --data-binary @"$TRAIN_OUT" \
                "https://uploads.github.com/repos/arkashira/surrogate/releases/$REL_ID/assets?name=training-${DATE}.tar.gz" \
                > /dev/null 2>>"$LOG"
            echo "[$(date '+%H:%M:%S')] ✅ surrogate training snapshot uploaded ($SIZE bytes)" >> "$LOG"
        fi
        /bin/rm -f "$TRAIN_OUT"
    fi
fi

# Note: code-vector-db (6GB) is NOT backed up — it's deterministic rebuild from index.db
# via /opt/surrogate-1-harvest/bin/rag-index.sh. If disk dies: clone hermes-toolbelt → restore index.db
# from latest release → run rag-index.sh → code-vector-db rebuilds.

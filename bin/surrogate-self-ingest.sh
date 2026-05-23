#!/usr/bin/env bash
# Surrogate self-ingestion — feeds Surrogate-1 its OWN training pairs as RAG context.
# This is the closing of the self-improvement loop: every orchestrate output
# becomes searchable knowledge for the next orchestrate run.
#
# Builds a SQLite FTS5 index over training-pairs.jsonl (every 15 min).
# Surrogate's call_agent in orchestrate then queries this index for similar past tasks
# and injects top-3 results as "prior knowledge" into the prompt.
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

SRC="$HOME/.surrogate/training-pairs.jsonl"
INDEX="$HOME/.surrogate/state/self-ingest.db"
OFFSET_FILE="$HOME/.surrogate/.self-ingest-offset"
LOG="$HOME/.surrogate/logs/self-ingest.log"
mkdir -p "$(dirname "$INDEX")" "$(dirname "$LOG")"

[[ ! -f "$SRC" ]] && { echo "[$(date +%H:%M:%S)] no source — skip" | tee -a "$LOG"; exit 0; }

# Schema
sqlite3 "$INDEX" <<'SQL'
CREATE VIRTUAL TABLE IF NOT EXISTS pairs USING fts5(
    source UNINDEXED,
    role UNINDEXED,
    prompt,
    response,
    ts UNINDEXED
);
SQL

CUR=$(wc -l < "$SRC" | tr -d ' ')
PREV=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)
NEW=$(( CUR - PREV ))

[[ $NEW -le 0 ]] && { echo "[$(date +%H:%M:%S)] no new pairs (offset=$PREV total=$CUR)" >> "$LOG"; exit 0; }

echo "[$(date +%H:%M:%S)] ingesting $NEW new pairs into FTS index" | tee -a "$LOG"

# Process in batches of 5000 — gentle, doesn't blow memory
BATCH_SIZE="${SELF_INGEST_BATCH:-5000}"
TAKE=$NEW
[[ $TAKE -gt $BATCH_SIZE ]] && TAKE=$BATCH_SIZE
echo "[$(date +%H:%M:%S)]   processing $TAKE / $NEW (batch_size=$BATCH_SIZE)" | tee -a "$LOG"

# Bug fix: previously `sed | python3 - "$INDEX" <<'PYEOF'` had a redirection
# conflict — bash's heredoc binds to python3's stdin AFTER the pipe, so the
# script body (PYEOF block) was being read as stdin (and consumed once for
# 'python3 -'), leaving sed's actual jsonl output unreachable. Result was
# `inserted=0 skipped_parse=0 skipped_empty=0` — a silent black hole.
#
# Fix: write the inline python to a temp file, then run with sed piped in.
# Now stdin = the actual jsonl lines, exactly as intended.
INGEST_PY=$(mktemp -t self-ingest-XXXXXX.py)
cat > "$INGEST_PY" <<'PYEOF'
import sys, json, sqlite3
db = sys.argv[1]
con = sqlite3.connect(db)
con.execute("BEGIN")
n = skipped_short = skipped_parse = 0
for line in sys.stdin:
    try:
        d = json.loads(line)
    except Exception:
        skipped_parse += 1
        continue
    src = d.get("source", "?")
    role = src.replace("orchestrate-", "") if src.startswith("orchestrate-") else src
    ts = d.get("ts", 0)
    prompt = (d.get("prompt") or "")[:4000]
    response = (d.get("response") or "")[:8000]
    if not prompt or not response:
        skipped_short += 1
        continue
    try:
        con.execute(
            "INSERT INTO pairs(source,role,prompt,response,ts) VALUES (?,?,?,?,?)",
            (src, role, prompt, response, str(ts))
        )
        n += 1
    except Exception as e:
        print(f"  insert err: {type(e).__name__}: {str(e)[:80]}", file=sys.stderr)
con.commit()
print(f"  inserted={n} skipped_parse={skipped_parse} skipped_empty={skipped_short}", flush=True)
PYEOF

sed -n "$((PREV + 1)),$((PREV + TAKE))p" "$SRC" | python3 "$INGEST_PY" "$INDEX" >> "$LOG" 2>&1
rm -f "$INGEST_PY"

# Advance offset by what we actually processed
NEW_OFFSET=$(( PREV + TAKE ))
echo "$NEW_OFFSET" > "$OFFSET_FILE"
echo "[$(date +%H:%M:%S)] ingest batch done · offset → $NEW_OFFSET (remaining: $((CUR - NEW_OFFSET)))" | tee -a "$LOG"

# Quick stats
TOTAL=$(sqlite3 "$INDEX" "SELECT COUNT(*) FROM pairs" 2>/dev/null)
TOTAL=${TOTAL:-0}
BY_ROLE=$(sqlite3 "$INDEX" "SELECT role, COUNT(*) FROM pairs GROUP BY role ORDER BY 2 DESC LIMIT 5" 2>/dev/null)
echo "  total indexed: $TOTAL" | tee -a "$LOG"
[[ -n "$BY_ROLE" ]] && {
    echo "  top roles:" | tee -a "$LOG"
    echo "$BY_ROLE" | sed 's/^/    /' | tee -a "$LOG"
}

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

tail -n "$NEW" "$SRC" | python3 - "$INDEX" >> "$LOG" 2>&1 <<'PYEOF'
import sys, json, sqlite3
from datetime import datetime
db = sys.argv[1]
con = sqlite3.connect(db)
con.execute("BEGIN")
n = 0
for line in sys.stdin:
    try:
        d = json.loads(line)
        src = d.get("source", "?")
        role = src.replace("orchestrate-", "") if src.startswith("orchestrate-") else src
        ts = d.get("ts", 0)
        prompt = (d.get("prompt") or "")[:4000]
        response = (d.get("response") or "")[:8000]
        if len(prompt) < 50 or len(response) < 50:
            continue
        con.execute(
            "INSERT INTO pairs(source,role,prompt,response,ts) VALUES (?,?,?,?,?)",
            (src, role, prompt, response, str(ts))
        )
        n += 1
    except Exception as e:
        print(f"  skip line: {type(e).__name__}", file=sys.stderr)
con.commit()
print(f"  ingested {n} pairs (FTS index)", flush=True)
PYEOF

echo "$CUR" > "$OFFSET_FILE"
echo "[$(date +%H:%M:%S)] ingest done · offset → $CUR" | tee -a "$LOG"

# Print quick stats
TOTAL=$(sqlite3 "$INDEX" "SELECT COUNT(*) FROM pairs" 2>/dev/null)
BY_ROLE=$(sqlite3 "$INDEX" "SELECT role, COUNT(*) FROM pairs GROUP BY role ORDER BY 2 DESC LIMIT 5" 2>/dev/null)
echo "  total indexed: $TOTAL" | tee -a "$LOG"
echo "  top roles:" | tee -a "$LOG"
echo "$BY_ROLE" | sed 's/^/    /' | tee -a "$LOG"

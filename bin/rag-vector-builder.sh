#!/usr/bin/env bash
# RAG vector index builder — runs every 30 min, embeds new training pairs.
#
# Reads training-pairs.jsonl, embeds prompts via Ollama nomic-embed-text,
# stores in ~/.surrogate/state/rag-vectors.db (SQLite + numpy bytes).
#
# Incremental: tracks offset, only embeds NEW pairs since last run.
# Caps at 50K vectors total (LRU eviction by ts) to keep index small + fast.
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/rag-vector-builder.log"
SRC="$HOME/.surrogate/training-pairs.jsonl"
DB="$HOME/.surrogate/state/rag-vectors.db"
OFFSET_FILE="$HOME/.surrogate/.rag-vec-offset"
MAX_VECTORS="${RAG_MAX_VECTORS:-50000}"
BATCH_SIZE="${RAG_BATCH:-500}"
mkdir -p "$(dirname "$LOG")" "$(dirname "$DB")"

[[ ! -f "$SRC" ]] && { echo "[$(date +%H:%M:%S)] no source" | tee -a "$LOG"; exit 0; }

# Wait for Ollama nomic-embed to be available
for i in 1 2 3 4 5; do
    if curl -sS --max-time 3 http://127.0.0.1:11434/api/tags 2>/dev/null | grep -q "nomic-embed-text"; then
        break
    fi
    [[ $i -eq 5 ]] && { echo "[$(date +%H:%M:%S)] nomic-embed-text not loaded — skip" | tee -a "$LOG"; exit 0; }
    sleep 5
done

CUR=$(wc -l < "$SRC" | tr -d ' ')
PREV=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)
NEW=$(( CUR - PREV ))
[[ $NEW -le 0 ]] && { echo "[$(date +%H:%M:%S)] no new pairs (offset=$PREV total=$CUR)" >> "$LOG"; exit 0; }

# Process at most BATCH_SIZE per run (gentle on Ollama)
TAKE=$NEW
[[ $TAKE -gt $BATCH_SIZE ]] && TAKE=$BATCH_SIZE
echo "[$(date +%H:%M:%S)] embedding $TAKE / $NEW pairs" | tee -a "$LOG"

sed -n "$((PREV + 1)),$((PREV + TAKE))p" "$SRC" | python3 - "$DB" "$MAX_VECTORS" >> "$LOG" 2>&1 <<'PYEOF'
import sys, json, sqlite3, urllib.request, time, struct
import numpy as np

db, max_vec = sys.argv[1], int(sys.argv[2])
con = sqlite3.connect(db, timeout=10)
con.execute("""
CREATE TABLE IF NOT EXISTS vectors (
    hash TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    response TEXT NOT NULL,
    embedding BLOB NOT NULL,
    source TEXT,
    ts INTEGER NOT NULL
)""")
con.execute("CREATE INDEX IF NOT EXISTS idx_ts ON vectors(ts)")

embedded = skipped = errs = 0

def embed(text: str):
    body = json.dumps({"model":"nomic-embed-text","prompt":text[:2000]}).encode()
    req = urllib.request.Request("http://127.0.0.1:11434/api/embeddings",
        data=body, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        emb = json.load(r).get("embedding")
        if not emb: return None
        return np.array(emb, dtype=np.float32).tobytes()

for line in sys.stdin:
    try:
        d = json.loads(line)
    except Exception:
        skipped += 1; continue
    p = (d.get("prompt") or d.get("instruction") or "")[:2000]
    r = (d.get("response") or d.get("output") or "")[:6000]
    if not p or len(p) < 30: skipped += 1; continue
    src = d.get("source", "?")
    ts = int(d.get("ts", time.time()))

    import hashlib
    h = hashlib.md5(p[:500].encode()).hexdigest()[:16]
    if con.execute("SELECT 1 FROM vectors WHERE hash=?", (h,)).fetchone():
        skipped += 1; continue

    try:
        emb_bytes = embed(p)
        if emb_bytes is None: errs += 1; continue
        con.execute("INSERT OR IGNORE INTO vectors VALUES (?,?,?,?,?,?)",
                    (h, p, r, emb_bytes, src, ts))
        embedded += 1
    except Exception as e:
        errs += 1
        if errs > 10: break  # Ollama down

con.commit()

# LRU eviction if over cap
total = con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
if total > max_vec:
    excess = total - max_vec
    con.execute("DELETE FROM vectors WHERE hash IN "
                "(SELECT hash FROM vectors ORDER BY ts ASC LIMIT ?)", (excess,))
    con.commit()
    print(f"  LRU evicted {excess} oldest vectors (cap={max_vec})")

print(f"  embedded={embedded} skipped={skipped} errs={errs} total={total}")
PYEOF

NEW_OFFSET=$(( PREV + TAKE ))
echo "$NEW_OFFSET" > "$OFFSET_FILE"
echo "[$(date +%H:%M:%S)] vector batch done · offset → $NEW_OFFSET" | tee -a "$LOG"

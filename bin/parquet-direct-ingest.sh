#!/usr/bin/env bash
# Parquet-direct ingest — bypasses 'datasets' library streaming overhead.
# Downloads parquet shards directly via HF datasets-server API and processes
# with pyarrow (much faster than streaming JSON).
#
# Targets the largest trillion-scale corpora where streaming is too slow:
#   - HuggingFaceFW/fineweb-edu / fineweb / fineweb-2
#   - allenai/dolma
#   - togethercomputer/RedPajama-Data-V2
#   - bigcode/the-stack-dedup
#   - HuggingFaceTB/cosmopedia-v2
#
# Each parquet ~500MB, contains 100K-1M rows. Direct DL + filter = 5-10× faster.
# Coordinates with bulk-ingest-parallel via central dedup store.
set -uo pipefail
set -a; source "$HOME/.hermes/.env" 2>/dev/null; set +a

LOG="$HOME/.surrogate/logs/parquet-direct-ingest.log"
mkdir -p "$(dirname "$LOG")"

PARALLEL_DOWNLOADS="${PARQUET_PARALLEL:-6}"
HF_AUTH="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}"

# Target datasets — only the trillion-scale ones where streaming is slow
TARGETS=(
    "HuggingFaceFW/fineweb-edu:default:train"
    "HuggingFaceFW/fineweb:default:train"
    "allenai/dolma:default:train"
    "HuggingFaceTB/cosmopedia-v2:default:train"
    "bigcode/the-stack-dedup:default:train"
    "HuggingFaceTB/smollm-corpus:default:train"
    "DKYoon/SlimPajama-6B:default:train"
    "togethercomputer/RedPajama-Data-V2:default:train"
)

echo "[$(date +%H:%M:%S)] parquet-direct start (parallel=$PARALLEL_DOWNLOADS)" | tee -a "$LOG"

while true; do
    for target in "${TARGETS[@]}"; do
        IFS=':' read -r repo config split <<< "$target"
        echo "[$(date +%H:%M:%S)] processing $repo::$config::$split" >> "$LOG"

        # List parquet shards via datasets-server
        SHARDS=$(curl -sS --max-time 15 \
            "https://datasets-server.huggingface.co/parquet?dataset=$(echo $repo | sed 's|/|%2F|g')&config=$config&split=$split" \
            ${HF_AUTH:+-H "Authorization: Bearer $HF_AUTH"} 2>/dev/null \
            | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for f in d.get('parquet_files', [])[:50]:
        print(f.get('url',''))
except: pass
" 2>/dev/null)

        if [[ -z "$SHARDS" ]]; then
            echo "  no shards or unavailable — skip" >> "$LOG"
            continue
        fi

        SHARD_COUNT=$(echo "$SHARDS" | wc -l | tr -d ' ')
        echo "  found $SHARD_COUNT parquet shards" >> "$LOG"

        # Process N shards in parallel (each ~500MB, fast filter)
        echo "$SHARDS" | head -20 | xargs -P "$PARALLEL_DOWNLOADS" -I{} bash -c "
            url='{}'
            shard_name=\$(basename \"\$url\" | cut -d? -f1)
            python3 - <<PYEOF 2>>'$LOG'
import sys, os, urllib.request, json, hashlib, time, io
url = '\$url'
src_repo = '$repo'
sys.path.insert(0, os.path.expanduser('~/.surrogate/bin/lib'))
try:
    from dedup import DedupStore
    HAS_DEDUP = True
except ImportError:
    HAS_DEDUP = False

try:
    import pyarrow.parquet as pq
except ImportError:
    print(f'  [no pyarrow] skip \$shard_name')
    sys.exit(0)

OUT = os.path.expanduser('~/.surrogate/training-pairs.jsonl')

try:
    req = urllib.request.Request(url, headers={'User-Agent':'Surrogate-1/parquet'})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
    table = pq.read_table(io.BytesIO(body))
    written = skipped = 0
    cols = set(table.column_names)
    n_rows = table.num_rows
    for i in range(n_rows):
        row = {c: table.column(c)[i].as_py() for c in cols}
        # Detect schema by available columns + extract prompt+response
        if 'text' in cols:
            text = str(row.get('text','') or '')[:200000]
            if len(text) < 500: skipped += 1; continue
            # Web-text quality filter
            if not any(s in text for s in ('?','\`\`\`','# ','## ')) and not any(s in text.lower() for s in ('step ','first,','to solve','function ','def ','class ')):
                skipped += 1; continue
            # FineWeb-Edu score gate
            sc = row.get('score') or row.get('edu_score') or 3
            try:
                if float(sc) < 2.5: skipped += 1; continue
            except: pass
            prompt = f'Explain this educational content from {src_repo}:'
            response = text
        elif 'instruction' in cols and 'response' in cols:
            prompt = str(row.get('instruction','') or '')[:4000]
            response = str(row.get('response','') or '')[:200000]
            if len(prompt) < 30 or len(response) < 30: skipped += 1; continue
        elif 'content' in cols and 'language' in cols:
            code = str(row.get('content','') or '')[:100000]
            lang = str(row.get('language','') or 'code')
            if len(code) < 80 or len(code) > 6000: skipped += 1; continue
            prompt = f'Explain this {lang} code:'
            response = code
        else:
            skipped += 1; continue

        # Central dedup
        if HAS_DEDUP and not DedupStore.is_new(prompt, source=f'parquet:{src_repo}'):
            skipped += 1; continue

        with open(OUT, 'a') as f:
            f.write(json.dumps({
                'ts': time.time(),
                'source': f'parquet:{src_repo}',
                'parquet_shard': '\$shard_name',
                'prompt': prompt[:100000],
                'response': response[:12000],
            }, ensure_ascii=False) + '\n')
        written += 1
        if written >= 5000: break  # cap per shard pull
    print(f'  [\$shard_name] wrote={written} skipped={skipped} of {n_rows} rows')
except Exception as e:
    print(f'  [\$shard_name] err: {type(e).__name__}: {str(e)[:100]}')
PYEOF
        " >> "$LOG" 2>&1

        # Brief cool-down between dataset transitions
        sleep 30
    done

    echo "[$(date +%H:%M:%S)] parquet-direct cycle done — sleep 30 min" >> "$LOG"
    sleep 1800
done

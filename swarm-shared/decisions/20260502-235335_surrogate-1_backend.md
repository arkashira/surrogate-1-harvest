# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate limits during ingestion and training by using `https://huggingface.co/datasets/.../resolve/main/...` URLs instead of authenticated `/api/` calls.

### Steps (1h 30m total)

1. **Create `bin/snapshot.sh`** (20m) — deterministic file listing for a single date folder, outputs `snapshot.json` with CDN URLs + metadata.
2. **Update `bin/dataset-enrich.sh`** (20m) — accept snapshot path, skip `list_repo_tree` during shard runs, read CDN URLs from snapshot.
3. **Add `lib/cdn_stream.py`** (20m) — lightweight helper to stream parquet via CDN URLs with `pyarrow` without HF dataset API.
4. **Update GitHub Actions matrix** (10m) — pass `SNAPSHOT_DATE` and `SNAPSHOT_FILE` to all 16 shards.
5. **Add training integration stub** (10m) — `bin/make_train_manifest.py` embeds snapshot into Lightning training script for zero-API data loading.
6. **Test locally** (20m) — run snapshot + one shard to verify CDN downloads work and schema projection is correct.

---

## 1. `bin/snapshot.sh`

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: HF_TOKEN=... ./bin/snapshot.sh --repo axentx/surrogate-1-training-pairs --date 2026-05-02 --out snapshot.json
set -euo pipefail

REPO=""
DATE=""
OUT="snapshot.json"

while [[ $# -gt 0 ]]; do
  case $1 in
    --repo) REPO="$2"; shift 2 ;;
    --date) DATE="$2"; shift 2 ;;
    --out)  OUT="$2";  shift 2 ;;
    *) echo "Unknown option $1"; exit 1 ;;
  esac
done

if [[ -z "$REPO" || -z "$DATE" ]]; then
  echo "Usage: $0 --repo owner/repo --date YYYY-MM-DD [--out snapshot.json]"
  exit 1
fi

# Single API call: list top-level folder for the date (non-recursive)
# Avoids recursive list_repo_files which paginates 100x and hits rate limits.
echo "Listing ${REPO} for date ${DATE}..."
FILES=$(python3 - "$REPO" "$DATE" <<'PY'
import os, json, sys
from huggingface_hub import HfApi
api = HfApi(token=os.environ.get("HF_TOKEN"))
repo, date = sys.argv[1], sys.argv[2]
items = api.list_repo_tree(repo=repo, path=date, recursive=False)
# Expecting files directly under date folder: batches/mirror-merged/2026-05-02/*.parquet
result = []
for item in items:
    if item.type == "file" and item.path.endswith(".parquet"):
        result.append({
            "path": item.path,
            "size": item.size,
            "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{item.path}"
        })
print(json.dumps(result, indent=2))
PY
)

echo "$FILES" > "$OUT"
echo "Snapshot written to $OUT ($(echo "$FILES" | jq length) files)"
```

Make executable:

```bash
chmod +x bin/snapshot.sh
```

---

## 2. `lib/cdn_stream.py`

```python
# lib/cdn_stream.py
import pyarrow.parquet as pq
import pyarrow as pa
import requests
import io
import os
import time
from typing import Iterator, Dict, Any

CDN_TIMEOUT = int(os.getenv("CDN_TIMEOUT", "60"))
MAX_RETRIES = int(os.getenv("CDN_RETRIES", "3"))
BACKOFF_FACTOR = float(os.getenv("CDN_BACKOFF", "1.5"))

def _fetch_with_retry(url: str) -> bytes:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=CDN_TIMEOUT)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == MAX_RETRIES:
                raise
            sleep = BACKOFF_FACTOR ** attempt
            print(f"CDN retry {attempt}/{MAX_RETRIES} for {url}: {exc} — sleeping {sleep:.1f}s")
            time.sleep(sleep)

def cdn_parquet_reader(cdn_url: str, columns=("prompt", "response")) -> pa.Table:
    """Download a single parquet via CDN and project columns without HF API."""
    buf = io.BytesIO(_fetch_with_retry(cdn_url))
    table = pq.read_table(buf, columns=columns)
    return table

def iter_cdn_shard(cdn_urls: list[str], columns=("prompt", "response")) -> Iterator[Dict[str, Any]]:
    """Yield rows from multiple CDN parquet files."""
    for url in cdn_urls:
        try:
            table = cdn_parquet_reader(url, columns=columns)
            for batch in table.to_batches(max_chunksize=1024):
                for i in range(batch.num_rows):
                    row = {col: batch.column(col)[i].as_py() for col in columns}
                    yield row
        except Exception as exc:
            # Log and skip bad files; don't kill entire shard
            print(f"CDN read failed {url}: {exc}")
            continue
```

---

## 3. Update `bin/dataset-enrich.sh` (partial diff)

```bash
# Near top, after shebang and set -euo pipefail
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"

# If snapshot provided, use CDN-only mode
if [[ -n "$SNAPSHOT_FILE" && -f "$SNAPSHOT_FILE" ]]; then
  echo "Using snapshot $SNAPSHOT_FILE (CDN mode)"
  # Select shard slice from snapshot entries
  mapfile -t ALL_URLS < <(jq -r '.[].cdn_url' "$SNAPSHOT_FILE")
  TOTAL=${#ALL_URLS[@]}
  SHARD_SIZE=$(( (TOTAL + SHARDS - 1) / SHARDS ))
  START=$(( SHARD_ID * SHARD_SIZE ))
  END=$(( START + SHARD_SIZE ))
  if (( END > TOTAL )); then END=$TOTAL; fi
  SHARD_URLS=("${ALL_URLS[@]:$START:$((END-START))}")
  echo "Shard $SHARD_ID: ${#SHARD_URLS[@]}/${TOTAL} URLs"
else
  # Fallback to original HF API listing (keep existing behavior)
  echo "No snapshot provided — using HF API listing (rate-limited)"
  # ... existing list_repo_tree logic ...
fi

# Later, where you stream/process files:
# Replace `load_dataset(streaming=True)` usage with:
python3 -c "
import sys, json
sys.path.insert(0, 'lib')
from cdn_stream import iter_cdn_shard
urls = json.loads('$(printf '%s\n' "${SHARD_URLS[@]}" | jq -R . | jq -s .)')
for row in iter_cdn_shard(urls, columns=('prompt','response')):
    # apply per-schema normalization + dedup via lib/dedup.py
    print(json.dumps(row))
" | ...
```

---

## 4. GitHub Actions update (`.github/workflows/ingest.yml`)

Add snapshot step and pass to matrix:

```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      date: ${{ steps.date.outputs.DATE }}
      snapshot: ${{ steps.snapshot.outputs.file }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt huggingface_hub pyarrow requests
      - name: Compute date
        id: date
        run: echo "DATE=$(date -u +%Y-%m-%d)" >> $GITHUB_OUTPUT
      - name: Generate snapshot
        id: snapshot
        env:
         

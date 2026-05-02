# surrogate-1 / frontend

## Final Consolidated Implementation (Best of Both Candidates)

**Core decision**: Adopt the HF CDN bypass pattern with deterministic pre-flight file listing. This eliminates 429s during training, makes shard workers independent, and requires only transport-layer changes.

---

### 1. `bin/list-shards.sh` (Canonical version)

```bash
#!/usr/bin/env bash
# list-shards.sh
# Usage: HF_TOKEN=... ./list-shards.sh [date]
# Outputs: shard-manifest.json
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="shard-manifest.json"

echo "[$(date)] Listing ${REPO} folder: ${DATE}/ ..."

# Single API call: list top-level folder (non-recursive)
FILES=$(huggingface-cli api --token "$HF_TOKEN" list-files-repo --repo-type dataset "$REPO" --path "$DATE" --recursive false 2>/dev/null || true)

if [ -z "$FILES" ]; then
  echo "[$(date)] No files found for ${DATE}. Trying fallback tree..."
  FILES=$(huggingface-cli api --token "$HF_TOKEN" list-files-repo --repo-type dataset "$REPO" --path "$DATE" --recursive false --json 2>/dev/null || echo '[]')
fi

# Build manifest with CDN URLs
echo "$FILES" | jq -r '
  map(select(.path | endswith(".jsonl") or endswith(".parquet"))) |
  map({
    path: .path,
    cdn_url: ("https://huggingface.co/datasets/" + "'"$REPO"'" + "/resolve/main/" + .path)
  })
' > "$OUT"

echo "[$(date)] Wrote $(jq length "$OUT") files to $OUT"
```

**Key improvements over Candidate 2**:
- Uses `list-files-repo` (more reliable than `list_repo_tree`)
- Proper JSON output with `jq` for downstream consumption
- Fallback handling for empty results

---

### 2. `lib/cdn_stream.py` (Robust streaming)

```python
# lib/cdn_stream.py
import json
import time
import requests
from typing import Iterator, Dict, Any

HEADERS = {"User-Agent": "axentx-surrogate-1/1.0"}

def cdn_stream(entries: list[Dict[str, str]]) -> Iterator[Dict[str, Any]]:
    """
    Stream JSONL lines from CDN URLs.
    Each entry: {"path": "...", "cdn_url": "..."}
    Yields {"prompt": ..., "response": ..., "_source": path}
    """
    for entry in entries:
        url = entry["cdn_url"]
        path = entry["path"]
        retries = 3
        for attempt in range(1, retries + 1):
            try:
                with requests.get(url, headers=HEADERS, stream=True, timeout=30) as r:
                    r.raise_for_status()
                    for line in r.iter_lines(decode_unicode=True):
                        if not line or line.isspace():
                            continue
                        try:
                            obj = json.loads(line)
                            # Normalize to {prompt, response}
                            prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
                            response = obj.get("response") or obj.get("output") or obj.get("answer")
                            if prompt is None or response is None:
                                continue
                            yield {"prompt": prompt, "response": response, "_source": path}
                        except json.JSONDecodeError:
                            continue
                break
            except requests.HTTPError as e:
                if e.response.status_code == 429:
                    wait = 2 ** attempt * 5
                    print(f"CDN 429 on {url}, sleeping {wait}s")
                    time.sleep(wait)
                    continue
                raise
            except requests.RequestException as e:
                if attempt == retries:
                    print(f"Failed to stream {url}: {e}")
                    raise
                time.sleep(2 ** attempt)
```

**Key improvements**:
- Exponential backoff for 429s (CDN tier can still rate limit)
- Proper timeout handling
- Schema normalization (prompt/response extraction)

---

### 3. `bin/dataset-enrich.sh` (Updated worker)

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Usage: SHARD_ID=0 NUM_SHARDS=16 [MANIFEST=shard-manifest.json] ./dataset-enrich.sh
set -euo pipefail

SHARD_ID="${SHARD_ID:-0}"
NUM_SHARDS="${NUM_SHARDS:-16}"
MANIFEST="${MANIFEST:-}"
HF_TOKEN="${HF_TOKEN:-}"
REPO_DST="axentx/surrogate-1-training-pairs"
DATE="$(date +%Y-%m-%d)"
TS="$(date +%H%M%S)"
OUT="shard-${SHARD_ID}-${TS}.jsonl"

echo "[$(date)] Shard ${SHARD_ID}/${NUM_SHARDS} starting"

if [ -n "$MANIFEST" ] && [ -f "$MANIFEST" ]; then
  echo "[$(date)] Using CDN manifest $MANIFEST"
  # Select deterministic shard of CDN entries
  mapfile -t ENTRIES < <(jq -r --argjson sid "$SHARD_ID" --argjson n "$NUM_SHARDS" '
    to_entries
    | map(select(.key % $n == $sid))
    | .[].value | @base64
  ' < "$MANIFEST")

  python3 -c "
import json, base64, sys
from lib.cdn_stream import cdn_stream
entries = [json.loads(base64.b64decode(e).decode()) for e in sys.argv[1:]]
for obj in cdn_stream(entries):
    print(json.dumps(obj, ensure_ascii=False))
" "${ENTRIES[@]}" > "$OUT"

else
  echo "[$(date)] No manifest; falling back to HF dataset streaming"
  python3 -c "
from datasets import load_dataset
import json, os, hashlib
shard = int(os.environ['SHARD_ID'])
n = int(os.environ['NUM_SHARDS'])
ds = load_dataset('${REPO_DST}', split='train', streaming=True)
for i, ex in enumerate(ds):
    if i % n != shard:
        continue
    prompt = ex.get('prompt') or ex.get('input') or ex.get('question')
    response = ex.get('response') or ex.get('output') or ex.get('answer')
    if prompt is None or response is None:
        continue
    print(json.dumps({'prompt': prompt, 'response': response, '_source': 'hf_stream'}, ensure_ascii=False))
" > "$OUT"
fi

# Dedup + upload (existing logic via lib/dedup.py)
if [ -s "$OUT" ]; then
  python3 lib/dedup.py "$OUT" "batches/public-merged/${DATE}/${OUT}"
  huggingface-cli upload --token "$HF_TOKEN" "$REPO_DST" "batches/public-merged/${DATE}/${OUT}" --repo-type dataset
  echo "[$(date)] Uploaded $OUT"
else
  echo "[$(date)] No records for shard ${SHARD_ID}"
fi
```

**Key improvements**:
- Deterministic shard assignment via `jq` (consistent across workers)
- Base64 encoding for safe argument passing
- Maintains fallback to HF streaming when manifest unavailable

---

### 4. Quick Usage (Mac orchestration)

```bash
# 1) List once (after rate-limit window clears)
HF_TOKEN=... ./bin/list-shards.sh 2026-05-02

# 2) Launch 16 shards locally (or via CI) with manifest
for s in $(seq 0 15); do
  SHARD_ID=$s NUM_SHARDS=16 MANIFEST=shard-manifest.json \
    ./bin/dataset-enrich.sh &
done
wait
```

---

## Why This Works

1. **Eliminates HF API 429s**: Workers stream from CDN (much higher rate limits) instead of hitting HF API during training
2. **Deterministic sharding**: `jq`-based shard assignment ensures reproducible splits across workers
3. **Zero code changes to core logic**: Schema projection, dedup, and upload pipelines remain untouched
4. **Graceful degradation**: Falls back to HF streaming if manifest unavailable
5. **Minimal footprint**: ~130 lines total across 3 files

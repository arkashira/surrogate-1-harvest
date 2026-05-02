# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit (429) during ingestion and ensures Lightning training uses zero API calls for data loading.

### Steps (1h 30m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Uses `huggingface_hub` to call `list_repo_tree(path, recursive=False)` for a given date folder under `public-merged/`.  
   - Outputs `snapshot-<date>.json` with CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`).  
   - Exits non-zero if API call fails (so cron can retry after 360s backoff).

2. **Update `bin/dataset-enrich.sh`** (20m)  
   - Accept optional snapshot file path. If provided, skip `list_repo_files` and read file list from JSON.  
   - Each shard computes deterministic slice from the manifest (by `shard_id % 16`).  
   - Downloads via `curl`/`wget` from CDN URLs (no auth header) → pipes into Python normalization.

3. **Add Python helper `lib/cdn_loader.py`** (20m)  
   - Reads manifest, yields CDN URLs for assigned shard.  
   - Streams download with retry/backoff, passes raw bytes to `pyarrow` parquet reader (project only `{prompt, response}`).  
   - Emits normalized JSONL lines.

4. **Update GitHub Actions `ingest.yml`** (20m)  
   - Add a pre-step job `snapshot` that runs `bin/snapshot.sh` for today’s date, uploads artifact `snapshot-<date>.json`.  
   - Matrix jobs download artifact and pass path via env `SNAPSHOT_FILE`.  
   - Keep existing 16-shard matrix; each shard uses manifest slice.

5. **Add training script integration** (20m)  
   - Create `bin/make_train_manifest.py` that calls snapshot for a date range and writes `train-files.json` used by Lightning training.  
   - Update training script to read manifest and fetch via CDN only (zero HF API calls during dataload).

6. **Validation & rollback** (20m)  
   - Run snapshot locally, verify JSON structure and CDN URLs.  
   - Run one shard with snapshot, confirm no `huggingface_hub` HTTP 429.  
   - If snapshot fails, fallback to old behavior (list via API) with exponential backoff.

---

### Code Snippets

#### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="snapshot-${DATE}.json"

echo "Listing dataset tree for public-merged/${DATE}/ ..."
python3 - "$REPO" "$DATE" "$OUT" <<'PY'
import json, sys
from huggingface_hub import HfApi

repo, date, out = sys.argv[1], sys.argv[2], sys.argv[3]
api = HfApi()
path = f"public-merged/{date}"
try:
    tree = api.list_repo_tree(repo=repo, path=path, recursive=False)
except Exception as e:
    print(f"Error listing repo tree: {e}", file=sys.stderr)
    sys.exit(1)

files = [
    {
        "path": f.path,
        "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{f.path}"
    }
    for f in tree if f.type == "file"
]

with open(out, "w") as f:
    json.dump({"date": date, "files": files}, f, indent=2)
print(f"Wrote {len(files)} files to {out}")
PY

echo "Snapshot saved to $OUT"
```

#### `lib/cdn_loader.py`
```python
import json, hashlib, pyarrow.parquet as pq, pyarrow as pa, io, requests, time
from typing import List, Dict, Iterator

CDN_TIMEOUT = 30
MAX_RETRIES = 5

def load_manifest(path: str) -> List[Dict]:
    with open(path) as f:
        return json.load(f)["files"]

def shard_files(files: List[Dict], shard_id: int, total_shards: int = 16) -> List[Dict]:
    return [f for i, f in enumerate(files) if i % total_shards == shard_id]

def stream_cdn_parquet(url: str) -> Iterator[pa.Table]:
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=CDN_TIMEOUT, stream=True)
            resp.raise_for_status()
            buf = io.BytesIO(resp.content)
            yield pq.read_table(buf, columns=["prompt", "response"])
            break
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)

def normalize_table(t: pa.Table) -> Iterator[Dict]:
    df = t.to_pandas()
    for _, row in df.iterrows():
        prompt = str(row.get("prompt", ""))
        response = str(row.get("response", ""))
        if prompt.strip() and response.strip():
            yield {"prompt": prompt.strip(), "response": response.strip()}

def process_shard(manifest_path: str, shard_id: int) -> Iterator[Dict]:
    files = load_manifest(manifest_path)
    for f in shard_files(files, shard_id):
        try:
            for tbl in stream_cdn_parquet(f["cdn_url"]):
                yield from normalize_table(tbl)
        except Exception as e:
            print(f"Failed to process {f['path']}: {e}")
```

#### Update to `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

SHARD_ID="${SHARD_ID:-0}"
SNAPSHOT="${SNAPSHOT_FILE:-}"

if [[ -n "$SNAPSHOT" && -f "$SNAPSHOT" ]]; then
  echo "Using snapshot $SNAPSHOT for shard $SHARD_ID"
  python3 -c "
import json, sys
from lib.cdn_loader import process_shard
for item in process_shard('$SNAPSHOT', int('$SHARD_ID')):
    print(json.dumps(item, ensure_ascii=False))
"
else
  echo "No snapshot provided, falling back to API list (may hit rate limits)..."
  # existing API-based logic here
fi
```

#### GitHub Actions excerpt (`.github/workflows/ingest.yml`)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-file: ${{ steps.set.outputs.file }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install huggingface_hub
      - run: bin/snapshot.sh $(date +%Y-%m-%d)
      - name: Upload snapshot
        uses: actions/upload-artifact@v4
        with:
          name: snapshot-${{ github.run_id }}
          path: snapshot-*.json

  ingest:
    needs: snapshot
    strategy:
      matrix: { shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15] }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: snapshot-${{ github.run_id }}
      - name: Set snapshot env
        id: set
        run: echo "file=$(ls snapshot-*.json)" >> $GITHUB_OUTPUT
      - run: bin/dataset-enrich.sh
        env:
          SHARD_ID: ${{ matrix.shard }}
          SNAPSHOT_FILE: ${{ steps.set.outputs.file }}
```

#### Training manifest helper (`bin/make_train_manifest.py`)
```python
#!/usr/bin/env python3
import json, glob, sys
from datetime import datetime, timedelta

def make_manifest(start_date: str, end_date:

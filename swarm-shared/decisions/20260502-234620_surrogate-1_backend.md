# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists the target date folder once, embeds a deterministic file manifest, and enables all 16 shards (and later training) to download exclusively via CDN. This eliminates HF API rate-limit pressure during ingestion and ensures reproducible, deterministic sharding across workers.

### Why this now
- Current ingestion repeatedly calls HF listing/streaming APIs → 429 risk and quota waste.
- Public dataset files are available via CDN without auth; we only need one API call to list a date folder, then all workers can fetch via CDN.
- Fits existing layout: `bin/`, GitHub Actions matrix, 16-shard deterministic split.

---

## Concrete changes

### 1) Add `bin/snapshot.sh`
Lists a single date folder from `axentx/surrogate-1-training-pairs` via the HF REST API, saves `file-list.json` with CDN URLs. Exits non-zero on failure so Actions fails fast.

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: HF_TOKEN=... bin/snapshot.sh <date> [output.json]
# Example: bin/snapshot.sh 2026-05-02 batches/snapshot-2026-05-02.json

set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="${2:-batches/snapshot-${DATE}.json}"

mkdir -p "$(dirname "$OUT")"

echo "[$(date -u)] Listing ${REPO}/batches/public-merged/${DATE} ..."

curl -sSf -H "Authorization: Bearer ${HF_TOKEN}" \
  "https://huggingface.co/api/datasets/${REPO}/tree?path=batches/public-merged/${DATE}&recursive=false" \
  > "$OUT.tmp"

# Transform to minimal CDN entries: {path, cdn_url}
python3 -c "
import json, sys
with open('$OUT.tmp') as f:
    tree = json.load(f)
out = []
for node in tree:
    if node.get('type') == 'file':
        out.append({
            'path': node['path'],
            'cdn_url': f'https://huggingface.co/datasets/${REPO}/resolve/main/{node[\"path\"]}'
        })
with open('$OUT', 'w') as f:
    json.dump(out, f, indent=2)
"
rm -f "$OUT.tmp"

echo "[$(date -u)] Snapshot written: $OUT ($(jq length < "$OUT") files)"
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

### 2) Update `bin/dataset-enrich.sh` to accept file-list
Modify the worker launcher to accept an optional file-list JSON. If provided, it processes only those files and downloads via CDN (bypassing `load_dataset`/API during streaming). Keeps existing behavior when no list provided.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Existing behavior preserved; new optional arg --file-list FILELIST

set -euo pipefail

# ... existing env setup ...

FILE_LIST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --file-list) FILE_LIST="$2"; shift 2 ;;
    *) break ;;
  esac
done

# Pass FILE_LIST into python worker via env
export SURROGATE_FILE_LIST="${FILE_LIST}"
exec python3 -m surrogate_1.worker "$@"
```

---

### 3) Python worker: use CDN when `SURROGATE_FILE_LIST` is set
Add logic in the worker module (e.g., `surrogate_1/worker.py`) to read the manifest and stream files via CDN, applying the same schema normalization and dedup as before.

```python
# surrogate_1/worker.py  (excerpt)
import os
import json
import requests
import pyarrow.parquet as pq
import pyarrow as pa
from io import BytesIO
from lib.dedup import is_duplicate, mark_seen

def stream_cdn_files(file_entries):
    for entry in file_entries:
        url = entry["cdn_url"]
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        yield entry["path"], BytesIO(resp.content)

def run_shard(shard_id, total_shards, file_list_path=None):
    if file_list_path and os.path.exists(file_list_path):
        with open(file_list_path) as f:
            files = json.load(f)
        # Deterministic shard split by path hash
        my_files = [e for e in files if hash(e["path"]) % total_shards == shard_id]
        stream = stream_cdn_files(my_files)
    else:
        # fallback to existing HF datasets streaming path
        # (kept for compatibility)
        ...

    for path, fh in stream:
        try:
            table = pq.read_table(fh)
            # existing normalization: project to {prompt,response}, dedup
            df = table.to_pandas()
            # ... normalize to {prompt, response} ...
            for _, row in df.iterrows():
                md5 = row.get("md5") or compute_md5(row)
                if is_duplicate(md5):
                    continue
                mark_seen(md5)
                yield {"prompt": row["prompt"], "response": row["response"]}
        except Exception as exc:
            # log and continue per-file
            print(f"Error processing {path}: {exc}")
            continue
```

---

### 4) Update workflow `ingest.yml` to run snapshot + pass file-list
Add a pre-step that generates the snapshot, then pass it to each matrix job.

```yaml
# .github/workflows/ingest.yml  (excerpt additions)
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-path: ${{ steps.set.outputs.path }}
    steps:
      - uses: actions/checkout@v4
      - name: Generate snapshot
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: |
          DATE=$(date +%Y-%m-%d)
          bin/snapshot.sh "$DATE" batches/snapshot-${DATE}.json
          echo "path=batches/snapshot-${DATE}.json" >> $GITHUB_OUTPUT
      - uses: actions/upload-artifact@v4
        with:
          name: snapshot
          path: batches/snapshot-*.json

  ingest:
    needs: snapshot
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: snapshot
      - name: Run shard
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
          SHARD_ID: ${{ matrix.shard }}
        run: |
          SNAPSHOT=$(ls batches/snapshot-*.json | head -1)
          bin/dataset-enrich.sh --file-list "$SNAPSHOT"
```

---

### 5) (Optional) Add training script hint
Add a small note/example in `README.md` showing how training can reuse the same snapshot for CDN-only data loading (no API calls during training).

```markdown
## Training with CDN-only file list
Generate snapshot once:
```bash
bin/snapshot.sh 2026-05-02 batches/snapshot-2026-05-02.json
```

Then in your training script:
```python
import json
from torch.utils.data import IterableDataset
import requests, pyarrow.parquet as pq
from io import BytesIO

class CDNPairDataset(IterableDataset):
    def __init__(self, snapshot_path):
        with open(snapshot_path) as f:
            self.entries = json.load(f)
    def __iter__(self):
        for e in self.entries:
            resp = requests.get(e["cdn_url"], timeout=30)
            table = pq.read_table(BytesIO(resp.content))
            # normalize and yield examples
```
```

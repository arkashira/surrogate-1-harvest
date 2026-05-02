# surrogate-1 / discovery

## Final Synthesis (Best Parts + Correctness + Actionability)

I merge the strongest elements from both proposals into a single, contradiction-free plan that prioritizes **deterministic pre-flight snapshots**, **CDN-only fetches**, and **local reproducibility** while eliminating HF API rate limits and schema errors.

---

## Implementation Plan (≤2h)

**Highest-value change**: Replace runtime `load_dataset(streaming=True)` + recursive `list_repo_files` with a **deterministic pre-flight snapshot** and **CDN-only fetches**. This eliminates HF API rate limits, pyarrow `CastError` from mixed schemas, and removes the 429/1000-5min ceiling during ingestion.

### Steps (all executable in <2h)

1. **Add `tools/list_snapshot.py`** (Mac/CI side)  
   - Single API call to `list_repo_tree(path, recursive=False)` for today’s folder (or provided date).  
   - Emits `snapshot-YYYY-MM-DD.json` containing `{ "date": "...", "files": [ "batches/public-raw/YYYY-MM-DD/file1.parquet", ... ] }`.  
   - Run on Mac before workflow_dispatch or in CI as a pre-step.

2. **Modify `bin/dataset-enrich.sh`**  
   - Accept snapshot file path (or inline JSON) as env var `SNAPSHOT_JSON`.  
   - Remove any `load_dataset(...)` and recursive listing.  
   - Iterate snapshot files and download each via CDN URL:  
     `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/batches/public-raw/${date}/${file}`  
   - Keep existing per-row schema projection to `{prompt, response}` and dedup via `lib/dedup.py`.

3. **Update `.github/workflows/ingest.yml`**  
   - Add optional `snapshot_json` input (default: auto-generate via `list_snapshot.py`).  
   - Pass snapshot to each matrix shard via `env.SNAPSHOT_JSON`.  
   - Ensure no `huggingface_hub` list calls inside the 16 parallel jobs.

4. **Hardening**  
   - Shebang `#!/usr/bin/env bash` and `chmod +x` for all scripts.  
   - Add retry/backoff for CDN downloads (separate from API rate limits).  
   - Validate snapshot schema before shard start to fail fast.

---

### Code Snippets

#### 1) `tools/list_snapshot.py`
```python
#!/usr/bin/env python3
"""
Generate deterministic snapshot for a date folder.
Run on Mac (or CI) before workflow_dispatch.
"""
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

REPO = "axentx/surrogate-1-training-pairs"

def main(date_str: str | None = None) -> None:
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    api = HfApi()
    prefix = f"batches/public-raw/{date_str}/"
    # Non-recursive to avoid pagination explosion
    entries = api.list_repo_tree(repo_id=REPO, path=prefix, recursive=False)

    files = [e.path for e in entries if e.path.endswith(".parquet")]
    snapshot = {"date": date_str, "files": sorted(files)}

    out_path = f"snapshot-{date_str}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    print(f"Wrote {len(files)} files -> {out_path}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
```

#### 2) `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

HF_REPO="axentx/surrogate-1-training-pairs"
BASE_CDN="https://huggingface.co/datasets/${HF_REPO}/resolve/main"

: "${SNAPSHOT_JSON:?Required: snapshot JSON path or inline JSON}"
if [[ -f "${SNAPSHOT_JSON}" ]]; then
  SNAPSHOT=$(cat "${SNAPSHOT_JSON}")
else
  SNAPSHOT="${SNAPSHOT_JSON}"  # assume inline
fi

DATE=$(echo "${SNAPSHOT}" | jq -r '.date')
FILES=$(echo "${SNAPSHOT}" | jq -r '.files[]')

for rel_path in ${FILES}; do
  url="${BASE_CDN}/${rel_path}"
  echo "Downloading ${url} ..."
  tmp=$(mktemp)
  curl -fsSL --retry 3 --retry-delay 5 -o "${tmp}" "${url}"
  # stream parquet -> {prompt,response} projection + dedup
  python3 -c "
import pyarrow.parquet as pq
import sys, json, hashlib
from lib.dedup import is_duplicate, store_hash
table = pq.read_table(sys.argv[1], columns=['prompt','response'])
for batch in table.to_batches(max_chunksize=8192):
    for row in zip(batch.column('prompt').to_pylist(),
                   batch.column('response').to_pylist()):
        prompt, response = row[0] or '', row[1] or ''
        if not prompt or not response:
            continue
        md5 = hashlib.md5((prompt + response).encode()).hexdigest()
        if is_duplicate(md5):
            continue
        store_hash(md5)
        print(json.dumps({'prompt': prompt, 'response': response}, ensure_ascii=False))
  " "${tmp}" >> "shard-${SHARD_ID}-$(date +%H%M%S).jsonl"
  rm -f "${tmp}"
done
```

#### 3) `.github/workflows/ingest.yml` (excerpt)
```yaml
on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:
    inputs:
      snapshot_json:
        description: "Inline snapshot JSON or path in repo"
        required: false
        default: ""

jobs:
  ingest:
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    runs-on: ubuntu-latest
    env:
      SHARD_ID: ${{ matrix.shard_id }}
      SNAPSHOT_JSON: ${{ github.event.inputs.snapshot_json || needs.generate.outputs.snapshot || '' }}
    steps:
      - uses: actions/checkout@v4
      - name: Generate snapshot (if not provided)
        if: env.SNAPSHOT_JSON == ''
        id: generate
        run: |
          python3 tools/list_snapshot.py
          echo "snapshot=$(cat snapshot-*.json)" >> $GITHUB_OUTPUT
      - name: Run shard
        run: bash bin/dataset-enrich.sh
```

---

### Verification
- Run `python3 tools/list_snapshot.py` locally → confirm `snapshot-YYYY-MM-DD.json`.  
- `bash bin/dataset-enrich.sh` with `SNAPSHOT_JSON=snapshot-*.json` → confirm CDN downloads and clean JSONL output with no API 429.  
- Trigger workflow_dispatch with/without snapshot input → confirm 16 shards complete without HF API list/stream errors.

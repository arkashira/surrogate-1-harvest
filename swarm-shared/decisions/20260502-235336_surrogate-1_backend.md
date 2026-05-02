# surrogate-1 / backend

## Final Implementation Plan (≤2 h)

**Highest-value improvement**: Add a deterministic pre-flight snapshot generator (`bin/snapshot.sh`) that lists dataset files once per date folder, emits a CDN-ready manifest, and enables **CDN-only ingestion and training** (no authenticated HF API calls during parallel shard processing). This eliminates HF API rate limits and guarantees reproducible file lists across shards and training runs.

---

### Concrete actions (1 h 40 m total)

1. **Create `bin/snapshot.sh`** (20 m)  
   - Single `list_repo_tree(recursive=False)` per date folder.  
   - Outputs `snapshots/snapshot-<date>.json` with CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`).  
   - Saves to `snapshots/` and optionally commits for traceability.

2. **Add `lib/cdn_loader.py`** (20 m)  
   - Zero-auth CDN fetcher with retry/backoff.  
   - Projects files to `{prompt, response}` pairs at parse time.  
   - Supports `.jsonl`, `.parquet`, `.json`.

3. **Update `bin/dataset-enrich.sh`** (20 m)  
   - Accept `SNAPSHOT` env or CLI arg.  
   - If provided, use CDN-only path via `lib/cdn_loader.py`; otherwise keep existing `load_dataset` path.

4. **Update `.github/workflows/ingest.yml`** (20 m)  
   - Add `snapshot` job that runs once per workflow, produces `snapshot-<date>.json`, and passes it to matrix ingest jobs as an artifact and env var.

5. **Add training-side patch** (20 m)  
   - Add `train.py` helper to load snapshot and use CDN URLs for data fetching during Lightning training (avoids HF API in training loops).

6. **Smoke test** (20 m)  
   - Run snapshot locally, verify one shard can process via CDN, confirm training can iterate one batch.

---

### 1. `bin/snapshot.sh`

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Lists public dataset files for a date folder and emits a CDN-ready manifest.
# Usage: bin/snapshot.sh <date> [output.json]
# Example: bin/snapshot.sh 2026-05-02 snapshots/snapshot-2026-05-02.json

set -euo pipefail
REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="${2:-snapshots/snapshot-${DATE}.json}"
FOLDER="batches/public-merged/${DATE}"

mkdir -p "$(dirname "${OUT}")"

echo "Listing ${REPO}/${FOLDER} ..."
FILES=$(python3 -c "
import json, os, sys
from huggingface_hub import HfApi
api = HfApi()
items = api.list_repo_tree(
    repo_id='${REPO}',
    path='${FOLDER}',
    repo_type='dataset',
    recursive=False
)
files = [i for i in items if getattr(i, 'size', None) is not None]
out = [
    {
        'path': f.path,
        'size': f.size,
        'cdn_url': f'https://huggingface.co/datasets/${REPO}/resolve/main/{f.path.replace(\" \", \"%20\")}',
        'folder': '${FOLDER}'
    }
    for f in files
]
print(json.dumps(out, indent=2))
")

echo "${FILES}" > "${OUT}"
echo "Snapshot written to ${OUT} ($(echo "${FILES}" | jq length) files)"
```

Make executable:

```bash
chmod +x bin/snapshot.sh
```

---

### 2. `lib/cdn_loader.py`

```python
# lib/cdn_loader.py
import json
import time
import io
from typing import List, Dict, Iterator, Optional
from pathlib import Path

import requests
import pandas as pd
import pyarrow.parquet as pq

CDN_TIMEOUT = 30
MAX_RETRIES = 5
BACKOFF = 2.0

def load_snapshot(snapshot_path: str) -> List[Dict]:
    with open(snapshot_path) as f:
        return json.load(f)

def cdn_get(url: str, headers: Optional[Dict] = None) -> bytes:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=CDN_TIMEOUT, headers=headers)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            if attempt == MAX_RETRIES:
                raise
            sleep = BACKOFF ** attempt
            print(f"CDN retry {attempt}/{MAX_RETRIES} for {url}: {exc}. Sleep {sleep}s")
            time.sleep(sleep)

def stream_cdn_files(
    snapshot: List[Dict],
    accepted_ext: tuple = (".jsonl", ".parquet", ".json"),
) -> Iterator[Dict]:
    """Yield raw file content from CDN URLs in snapshot."""
    for item in snapshot:
        path = item["path"]
        if not path.lower().endswith(accepted_ext):
            continue
        url = item["cdn_url"]
        data = cdn_get(url)
        yield {
            "path": path,
            "url": url,
            "content": data,
            "size": item["size"],
        }

def parse_pair(raw: Dict) -> Iterator[Dict]:
    """Project raw file content into {prompt, response} pairs."""
    path = raw["path"]
    content = raw["content"]
    ext = Path(path).suffix.lower()

    try:
        if ext == ".parquet":
            table = pq.read_table(io.BytesIO(content))
            df = table.to_pandas()
        elif ext == ".jsonl":
            df = pd.read_json(io.BytesIO(content), lines=True)
        elif ext == ".json":
            df = pd.read_json(io.BytesIO(content), orient="records")
        else:
            return

        cols = {c.lower(): c for c in df.columns}
        prompt_col = cols.get("prompt") or cols.get("input") or cols.get("question")
        response_col = cols.get("response") or cols.get("output") or cols.get("answer")

        if prompt_col is None or response_col is None:
            return

        for _, row in df.iterrows():
            prompt = str(row[prompt_col]).strip()
            response = str(row[response_col]).strip()
            if prompt and response:
                yield {
                    "prompt": prompt,
                    "response": response,
                    "source_file": path,
                }
    except Exception as exc:
        print(f"Parse error {path}: {exc}")
        return
```

---

### 3. Update `bin/dataset-enrich.sh`

Add near top:

```bash
SNAPSHOT="${SNAPSHOT:-}"
if [ -n "${SNAPSHOT}" ] && [ -f "${SNAPSHOT}" ]; then
  echo "CDN mode: using snapshot ${SNAPSHOT}"
  export USE_CDN=1
else
  export USE_CDN=0
fi
```

Replace the worker ingestion block with:

```bash
if [ "${USE_CDN}" -eq 1 ]; then
  python3 -c "
import sys, json
sys.path.insert(0, 'lib')
from cdn_loader import load_snapshot, stream_cdn_files, parse_pair
snapshot = load_snapshot('${SNAPSHOT}')
for raw in stream_cdn_files(snapshot):
    for pair in parse_pair(raw):
        print(json.dumps(pair))
" | ...
else
  python3 -c "
from datasets import load_dataset
...
" | ...
fi
```

---

### 4. `.github/workflows/ingest.yml` additions

```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-path: ${{ steps.set.outputs.snapshot }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install huggingface_hub pyarrow
      - run: |
          DATE=$(date +%Y-%m-%d)
          python3 -c "
import json, os, sys
from huggingface_hub import HfApi

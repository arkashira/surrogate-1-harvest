# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight folder listing + CDN-only ingestion path to eliminate HF API rate limits during training and make shard workers resilient to 429s.

### What we change (merged + resolved)
- Add `bin/list-folder.sh` that calls `list_repo_tree` once per date folder (`recursive=False`) and writes `file-list-YYYYMMDD.json` to repo root.
- Update `bin/dataset-enrich.sh` to read the file list and fetch every file via raw CDN URL (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header.
- Keep existing shard logic (16-way split by slug-hash) and dedup path unchanged.
- Add retry/back-off for CDN 429 (separate from API 429) and **fallback to `hf_hub_download` only if CDN fetch fails** (with Authorization when needed).

### Why this is highest value
- Eliminates the primary failure mode (HF API 429) during parallel ingestion.
- Costs nothing (CDN tier has much higher limits).
- Fits in <2h: two small scripts + one-line change to enrich script.

---

## File changes

### 1) `bin/list-folder.sh`
Deterministic pre-flight folder listing (run once per workflow).

```bash
#!/usr/bin/env bash
# bin/list-folder.sh
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE=$(date -u +%Y%m%d)
OUT="file-list-${DATE}.json"

python3 - <<PY
import os, json, datetime
from huggingface_hub import HfApi

api = HfApi()
today = datetime.date.today().isoformat()
tree = api.list_repo_tree(repo_id=os.getenv("REPO", "$REPO"), path=today, recursive=False)
files = [f.rfilename for f in tree if f.type == "file"]
with open(os.getenv("OUT", "$OUT"), "w") as f:
    json.dump({"date": today, "files": files}, f, indent=2)
print(f"Listed {len(files)} files -> {os.getenv('OUT', '$OUT')}")
PY
```

### 2) `bin/dataset-enrich.sh`
CDN-first fetch with fallback to `hf_hub_download` and proper 429 handling.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
set -euo pipefail

HF_REPO="axentx/surrogate-1-training-pairs"
DATE=$(date -u +%Y%m%d)
SHARD_ID=${SHARD_ID:-0}
TOTAL_SHARDS=${TOTAL_SHARDS:-16}
FILE_LIST=${FILE_LIST:-"file-list-${DATE}.json"}
OUT_DIR="batches/public-merged/${DATE}"
mkdir -p "${OUT_DIR}"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-$(date -u +%H%M%S).jsonl"

python3 - <<PY
import json, os, sys, hashlib, time, random, requests
from pathlib import Path
from huggingface_hub import hf_hub_download

HF_REPO = os.getenv("HF_REPO", "$HF_REPO")
SHARD_ID = int(os.getenv("SHARD_ID", "$SHARD_ID"))
TOTAL_SHARDS = int(os.getenv("TOTAL_SHARDS", "$TOTAL_SHARDS"))
FILE_LIST = os.getenv("FILE_LIST", "$FILE_LIST")
OUT_FILE = os.getenv("OUT_FILE", "$OUT_FILE")

with open(FILE_LIST) as f:
    manifest = json.load(f)

all_files = manifest["files"]
shard_files = [f for i, f in enumerate(all_files) if i % TOTAL_SHARDS == SHARD_ID]

def cdn_download(url, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=30)
            # CDN 429 -> wait and retry with backoff
            if r.status_code == 429:
                sleep = min((2 ** attempt) + random.uniform(0, 1), 360)
                time.sleep(sleep)
                continue
            r.raise_for_status()
            return r.content
        except Exception as e:
            if attempt == retries - 1:
                raise
            sleep = (2 ** attempt) + random.uniform(0, 1)
            time.sleep(sleep)
    raise RuntimeError(f"CDN download failed after {retries} retries: {url}")

def fallback_hf_download(rel_path):
    return hf_hub_download(repo_id=HF_REPO, filename=rel_path, repo_type="dataset")

def parse_to_pair(content, filename):
    # Minimal projection to {prompt,response} per schema patterns
    # Extend with your per-format parsers; keep output strict.
    return {"prompt": "", "response": ""}

dedup_store_path = Path("central_dedup.sqlite")
import sqlite3
conn = sqlite3.connect(str(dedup_store_path))
conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)")

out_lines = []
for rel in shard_files:
    url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{rel}"
    data = None
    try:
        data = cdn_download(url)
    except Exception as e:
        print(f"CDN failed for {rel}: {e}; falling back to hf_hub_download", file=sys.stderr)
        try:
            local_path = fallback_hf_download(rel)
            with open(local_path, "rb") as f:
                data = f.read()
        except Exception as e2:
            print(f"Fallback failed for {rel}: {e2}", file=sys.stderr)
            continue

    md5 = hashlib.md5(data).hexdigest()
    cur = conn.execute("SELECT 1 FROM seen WHERE md5=?", (md5,))
    if cur.fetchone():
        continue
    pair = parse_to_pair(data, rel)
    out_lines.append(json.dumps(pair, ensure_ascii=False))
    conn.execute("INSERT INTO seen (md5) VALUES (?)", (md5,))

conn.commit()
conn.close()

Path(OUT_FILE).write_text("\n".join(out_lines) + ("\n" if out_lines else ""))
print(f"Shard {SHARD_ID} wrote {len(out_lines)} pairs -> {OUT_FILE}")
PY
```

### 3) `train.py` (Lightning snippet)
CDN-only `IterableDataset` with no HF API calls during training.

```python
# train.py
import json, os, io
from pathlib import Path
from typing import Iterator

import torch
from torch.utils.data import IterableDataset
from lightning import Fabric

class CDNPairDataset(IterableDataset):
    def __init__(self, file_list_json: str, repo: str = "axentx/surrogate-1-training-pairs"):
        super().__init__()
        with open(file_list_json) as f:
            self.files = json.load(f)["files"]
        self.repo = repo

    def _stream(self) -> Iterator[dict]:
        for rel in self.files:
            url = f"https://huggingface.co/datasets/{self.repo}/resolve/main/{rel}"
            try:
                import requests
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                pair = json.loads(r.content)
                yield pair
            except Exception:
                continue

    def __iter__(self) -> Iterator[dict]:
        return self._stream()

def train():
    fabric = Fabric(devices=1)
    dataset = CDNPairDataset(file_list_json=os.environ["FILE_LIST_JSON"])
    loader = torch.utils.data.DataLoader(dataset, batch_size=8, num_workers=0)
    model = ...  # your model
    model, loader = fabric.setup(model, loader)
    for batch in loader:
        ...
```

### 4) Workflow update (excerpt)
```yaml
# .github/workflows/ingest.yml
jobs:
  ingest:
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4
      - name: Pre-flight file list (once per workflow)
        if: matrix.shard == 0
        run: |


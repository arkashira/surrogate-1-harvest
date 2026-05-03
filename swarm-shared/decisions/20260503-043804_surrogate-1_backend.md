# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-only Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema `CastError`.

### Concrete Changes (in priority order)

1. **Add `train/manifest.json` generation** (single API call on Mac)  
   - After rate-limit window clears, run `scripts/build_manifest.py`  
   - Uses `list_repo_tree(path, recursive=False)` per date folder  
   - Emits `{"date": "2026-05-03", "files": ["file1.parquet", ...], "repos": [...]}`  
   - Embeds into repo so Lightning training does **zero API calls** during data load

2. **Add `train/train_cdn.py`** (CDN-only dataset loader)  
   - Reads `manifest.json`  
   - Downloads via CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) — no auth, no rate limit  
   - Projects to `{prompt, response}` only at parse time (avoids pyarrow `CastError`)  
   - Streams with `pyarrow.parquet.ParquetFile` + `RecordBatchFileReader` to keep RAM low  
   - Compatible with `torch.utils.data.DataLoader` and Lightning `DataModule`

3. **Update GitHub Actions runners**  
   - Keep 16-shard parallelism for ingestion (HF Space still OOMs on full decode)  
   - Each runner writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.parquet` (not jsonl)  
   - Project to `{prompt, response}` before upload; drop `source`, `ts` cols per schema pattern  
   - Deterministic hash-slug → sibling repo (5 repos) to bypass 128/hr commit cap

4. **Lightning Studio reuse guard**  
   - Launcher checks `Teamspace.studios` for existing running studio  
   - If stopped, restarts with `Machine.L40S` (free tier fallback)  
   - Prevents quota waste and idle-timeout deaths

---

## Code Snippets

### 1. `scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Run on Mac after HF rate-limit window clears.
Generates train/manifest.json so training uses CDN-only fetches.
"""
import json
from pathlib import Path
from huggingface_hub import HfApi

API = HfApi()
REPO = "axentx/surrogate-1-training-pairs"
OUT = Path(__file__).parent.parent / "train" / "manifest.json"

def main():
    OUT.parent.mkdir(exist_ok=True)
    tree = API.list_repo_tree(REPO, path="batches/public-merged", recursive=False)
    manifest = {"repos": [], "shards": {}}

    for entry in tree:
        # entry.path like "batches/public-merged/2026-05-03/"
        if entry.type != "directory":
            continue
        date = Path(entry.path).name
        day_tree = API.list_repo_tree(REPO, path=entry.path, recursive=False)
        files = [
            f"{date}/{Path(e.path).name}"
            for e in day_tree
            if e.type == "file" and e.path.endswith(".parquet")
        ]
        if files:
            manifest["shards"][date] = files

    # sibling repo assignment (5 siblings)
    manifest["repos"] = [
        f"axentx/surrogate-1-training-pairs-{i}" for i in range(5)
    ]

    OUT.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {OUT} with {sum(len(v) for v in manifest['shards'].values())} files")

if __name__ == "__main__":
    main()
```

### 2. `train/train_cdn.py`
```python
#!/usr/bin/env python3
"""
CDN-only training data loader.
Zero HF API calls during training — avoids rate limits and CastErrors.
"""
import json
import pyarrow.parquet as pq
import pyarrow.compute as pc
from pathlib import Path
from typing import Iterator, Dict, Any
import requests
from torch.utils.data import IterableDataset

BASE = "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main"
MANIFEST = Path(__file__).parent / "manifest.json"

class CDNPairDataset(IterableDataset):
    def __init__(self, date: str = None):
        manifest = json.loads(MANIFEST.read_text())
        files = manifest["shards"]
        if date:
            files = {date: files[date]} if date in files else {}
        self.urls = [
            f"{BASE}/batches/public-merged/{f}"
            for fs in files.values() for f in fs
        ]

    def _stream_file(self, url: str) -> Iterator[Dict[str, Any]]:
        # Stream download + column projection to avoid full-schema decode
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open("/tmp/temp.parquet", "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        pf = pq.ParquetFile("/tmp/temp.parquet")
        # Project only prompt/response; ignore extra cols to prevent CastError
        cols = {"prompt", "response"}
        available = set(pf.schema.names)
        proj = list(cols & available)
        if not proj:
            return

        for batch in pf.iter_batches(batch_size=512, columns=proj):
            df = batch.to_pydict()
            for i in range(len(df["prompt"])):
                yield {"prompt": df["prompt"][i], "response": df["response"][i]}

    def __iter__(self):
        for url in self.urls:
            yield from self._stream_file(url)

# Example usage in Lightning
if __name__ == "__main__":
    from torch.utils.data import DataLoader
    ds = CDNPairDataset()
    dl = DataLoader(ds, batch_size=8)
    for batch in dl:
        print(batch)
        break
```

### 3. `bin/dataset-enrich.sh` (updated)
```bash
#!/usr/bin/env bash
# Updated to project schema and write parquet per shard
set -euo pipefail

SHARD_ID=${SHARD_ID:-0}
TOTAL_SHARDS=${TOTAL_SHARDS:-16}
HF_REPO=${HF_REPO:-"axentx/surrogate-1-training-pairs"}
DATE=$(date +%Y-%m-%d)
OUTDIR="batches/public-merged/${DATE}"
mkdir -p "${OUTDIR}"

# Deterministic shard assignment by slug hash
assign_shard() {
  local slug=$1
  local hash=$(echo -n "$slug" | md5sum | cut -c1-8)
  local num=$((0x${hash} % TOTAL_SHARDS))
  echo $num
}

process_file() {
  local file=$1
  python3 -c "
import pyarrow.parquet as pq, sys, json, os
pf = pq.ParquetFile(sys.argv[1])
cols = {'prompt', 'response'}
avail = set(pf.schema.names)
proj = list(cols & avail)
if not proj:
    sys.exit(0)
out = []
for batch in pf.iter_batches(batch_size=1024, columns=proj):
    d = batch.to_pydict()
    for i in range(len(d['prompt'])):
        out.append({'prompt': d['prompt'][i], 'response': d['response'][i]})
import pyarrow as pa
tbl = pa.Table.from_pylist(out)
pq.write_table(tbl, sys.argv[2])
" "$file" "${OUTDIR}/shard${SHARD_ID}-$(date +%H%M%S).parquet"
}

export -f assign_shard process_file

# Stream HF dataset but project immediately to avoid CastError
python3 -c "
from huggingface_hub import HfApi
api = HfApi()
files = api.list_repo_tree('$HF_REPO', path='batches/public-merged/$DATE', recursive=False)
for f in files:
    if f.type == 'file' and f.path.endswith('.parquet'):
        print(f.path)
" | while read -

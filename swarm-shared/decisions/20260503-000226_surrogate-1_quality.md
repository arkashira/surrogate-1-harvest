# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing — eliminating HF API rate limits during ingestion and training.

### Why this now
- Surrogate-1 already suffers HF 429s during `list_repo_files` recursive calls on big repos.
- The 16-shard GitHub Actions workflow streams heterogeneous schemas via `load_dataset(streaming=True)` which triggers pyarrow CastErrors and repeated API calls.
- CDN bypass (`resolve/main/`) is unlimited and avoids auth/rate limits entirely.
- A single deterministic snapshot per date folder lets each shard fetch only its slice via CDN with zero API traffic during processing.

---

### Concrete plan (90 minutes total)

1. **Add `bin/snapshot.sh`** (20 min)  
   - Uses `huggingface_hub` to `list_repo_tree(path, recursive=False)` for one date folder.  
   - Saves `{"date":"YYYY-MM-DD","files":["path1.parquet",...],"sha256":"<tree-hash>"}` to `snapshots/<date>.json`.  
   - Exits non-zero if folder empty or API fails (so cron skips).

2. **Update `bin/dataset-enrich.sh`** (25 min)  
   - Accept optional `SNAPSHOT_FILE` env var.  
   - If provided, read file list and assign deterministic shard slice by `slug-hash % 16 == SHARD_ID`.  
   - Fetch via CDN (`curl -L "https://huggingface.co/datasets/.../resolve/main/${file}"`) and stream-parse to `{prompt,response}` only.  
   - Remove `load_dataset(streaming=True)` path when snapshot present.

3. **Add lightweight Python helper `lib/snapshot.py`** (15 min)  
   - Deterministic file-to-shard assignment: `hashlib.md5(f"{date}/{file}".encode()).hexdigest()` → int mod 16.  
   - Schema projection: read parquet → keep only `prompt`/`response` (or best-effort column names) → yield JSONL lines.  
   - No `source`, no `ts` columns (per pattern: attribution via filename).

4. **Update GitHub Actions `ingest.yml`** (15 min)  
   - Add a pre-step that runs `bin/snapshot.sh` once (single job) and uploads `snapshots/<date>.json` as artifact.  
   - Pass artifact path to each shard via `SNAPSHOT_FILE`.  
   - Keep existing 16-shard matrix; each shard now uses CDN-only fetches.

5. **Add training script integration stub `train.py`** (10 min)  
   - Read snapshot JSON, embed file list, construct CDN URLs.  
   - Use `hf_hub_download` only as fallback when CDN fails (should be rare).  
   - No API calls during dataloader iteration.

6. **Validation & cleanup** (5 min)  
   - Dry-run locally with a small date folder.  
   - Ensure deterministic shard assignment matches existing `SHARD_ID` behavior.  
   - Remove dead code paths guarded by `if use_snapshot`.

---

### Code snippets

#### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO=${HF_REPO:-"axentx/surrogate-1-training-pairs"}
DATE=${1:-$(date +%Y-%m-%d)}
OUTDIR="./snapshots"
OUTFILE="${OUTDIR}/${DATE}.json"

mkdir -p "${OUTDIR}"

python3 - <<PY
import os, json, hashlib
from huggingface_hub import HfApi

api = HfApi()
repo = os.environ["HF_REPO"]
date = os.environ["DATE"]
folder = f"batches/public-merged/{date}"

try:
    tree = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    files = [f.rfilename for f in tree if f.rfilename.endswith(".parquet")]
except Exception as e:
    # If folder missing, treat as empty
    files = []

files.sort()
payload = {
    "date": date,
    "folder": folder,
    "files": files,
    "sha256": hashlib.sha256(json.dumps(files).encode()).hexdigest()
}

with open(os.environ["OUTFILE"], "w") as f:
    json.dump(payload, f, indent=2)

print(f"Snapshot {date}: {len(files)} files -> {os.environ['OUTFILE']}")
PY
```

#### `lib/snapshot.py`
```python
import hashlib
import json
from pathlib import Path
from typing import List, Dict

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{file}"

def deterministic_shard(file_path: str, date: str, n_shards: int = 16) -> int:
    key = f"{date}/{file_path}"
    return int(hashlib.md5(key.encode()).hexdigest(), 16) % n_shards

def load_snapshot(snapshot_path: Path) -> Dict:
    with open(snapshot_path) as f:
        return json.load(f)

def files_for_shard(snapshot: Dict, shard_id: int, n_shards: int = 16) -> List[str]:
    return [
        f for f in snapshot["files"]
        if deterministic_shard(f, snapshot["date"], n_shards) == shard_id
    ]
```

#### `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
SHARD_ID=${SHARD_ID:-0}
SNAPSHOT_FILE=${SNAPSHOT_FILE:-""}

if [[ -n "$SNAPSHOT_FILE" && -f "$SNAPSHOT_FILE" ]]; then
    echo "Using snapshot: $SNAPSHOT_FILE"
    python3 - <<PY
import json, subprocess, sys
from lib.snapshot import load_snapshot, files_for_shard, CDN_TEMPLATE

snapshot = load_snapshot("$SNAPSHOT_FILE")
files = files_for_shard(snapshot, $SHARD_ID)
repo = "$REPO"

for f in files:
    url = CDN_TEMPLATE.format(repo=repo, file=f)
    # Stream parquet -> project to {prompt,response} -> stdout as JSONL
    # Use pyarrow or fastparquet via python helper
    subprocess.run([
        "python3", "-c",
        """
import pyarrow.parquet as pq, sys, json, io, urllib.request
req = urllib.request.urlopen(sys.argv[1])
table = pq.read_table(io.BytesIO(req.read()))
cols = table.column_names
prompt_col = next((c for c in ("prompt","instruction","input") if c in cols), cols[0] if cols else "")
response_col = next((c for c in ("response","output","answer") if c in cols), cols[1] if len(cols)>1 else "")
for i in range(table.num_rows):
    row = {
        "prompt": str(table[prompt_col][i].as_py()),
        "response": str(table[response_col][i].as_py())
    }
    print(json.dumps(row, ensure_ascii=False))
        """,
        url
    ], check=True)
PY
else
    echo "No snapshot; falling back to streaming datasets (legacy)"
    # existing load_dataset(streaming=True) path here
fi
```

#### `.github/workflows/ingest.yml` (excerpt)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-file: ${{ steps.upload.outputs.snapshot-path }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt huggingface_hub
      - run: bin/snapshot.sh ${{ github.event.inputs.date || '' }}
        env:
          HF_REPO: axentx/surrogate-1-training-pairs
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
      - uses: actions/upload-artifact@v4
        id: upload
        with:
          name: snapshot-${{ github.run_id }}
          path: snapshots/*.json

  ingest:
    needs: snapshot
    strategy:
      matrix:
        shard_id

# surrogate-1 / backend

## Final Synthesis (adopting strongest, correct, actionable parts)

Use **Candidate 1’s orchestrator + worker + dedup + Actions + Lightning pattern** (it is complete and executable).  
Adopt Candidate 2’s small, safe additions:

- Add a **`bin/list_folder_files.py` utility** so the Mac/cron step is an importable, reusable script (not just inline Python).  
- When producing the filelist, include minimal metadata (`sha`, `size`) to enable change-detection and resumability.  
- Explicitly **paginate `list_repo_tree` by date prefix** if a folder ever becomes huge (YYYY/MM/DD), then merge into one filelist.

Everything else remains Candidate 1 (CDN-only downloads, project-to-schema at parse time, no streaming, 16-shard deterministic assignment, SQLite dedup, GitHub Actions matrix, Lightning CDN-only loader). This removes 429 risk, cuts HF API calls to O(1) per folder, and keeps memory bounded.

---

## Concrete implementation plan (≤2h)

### 1) File-list utility (reusable, run on Mac/cron)
`bin/list_folder_files.py`
```python
#!/usr/bin/env python3
"""
Usage:
  python3 bin/list_folder_files.py datasets/axentx/surrogate-1-training-pairs 2026-05-03 filelist-2026-05-03.json
"""
import json, sys
from huggingface_hub import HfApi

def main(repo: str, folder: str, out_path: str) -> None:
    api = HfApi()
    # Non-recursive: one API call per folder (paginated internally by HF lib)
    tree = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    files = [{"rfilename": f.rfilename, "sha": f.lfs.get("oid", ""), "size": f.size or 0}
             for f in tree if f.type == "file"]
    data = {"repo": repo, "folder": folder, "files": sorted(files, key=lambda x: x["rfilename"])}
    with open(out_path, "w") as f:
        json.dump(data, f)
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: repo folder out.json")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
```

- Make executable: `chmod +x bin/list_folder_files.py`.  
- If a folder is huge, run per subprefix (e.g., `2026-05-03/part-*`) and merge JSON `files` arrays.

### 2) Worker script (unchanged core; small polish)
`bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

FILELIST="${1:?filelist.json required}"
SHARD_ID="${2:?SHARD_ID 0-15 required}"
HF_REPO="datasets/axentx/surrogate-1-training-pairs"
DATE=$(python3 -c "import json; print(json.load(open('$FILELIST'))['folder'])")
OUTDIR="batches/public-merged/${DATE}"
TS=$(date +%H%M%S)
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TS}.jsonl"
mkdir -p "$OUTDIR"

python3 - <<'PY' "$FILELIST" "$SHARD_ID" "$HF_REPO" "$OUTFILE"
import json, hashlib, requests, sys, os
from lib.dedup import Dedup

filelist_path, shard_id, repo, outfile = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
with open(filelist_path) as f:
    meta = json.load(f)
    files = [f["rfilename"] for f in meta["files"]]

assigned = [f for f in files if int(hashlib.md5(f.encode()).hexdigest(), 16) % 16 == shard_id]
dedup = Dedup()

def cdn_url(path):
    return f"https://huggingface.co/{repo}/resolve/main/{path}"

with open(outfile, "w") as out:
    for path in assigned:
        url = cdn_url(path)
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        # Project to {prompt,response} at parse time; drop all other fields.
        # Implement per-format parser as needed. Example for jsonl:
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            obj = json.loads(line)
            prompt = obj.get("prompt") or obj.get("input") or ""
            response = obj.get("response") or obj.get("output") or ""
            if not prompt or not response:
                continue
            md5 = hashlib.md5((prompt + response).encode()).hexdigest()
            if dedup.seen(md5):
                continue
            dedup.add(md5)
            out.write(json.dumps({"prompt": prompt, "response": response}) + "\n")
PY
```

### 3) Dedup helper (unchanged)
`lib/dedup.py`
```python
import sqlite3
import os

DB_PATH = os.environ.get("DEDUP_DB", "dedup.db")

class Dedup:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.execute("CREATE TABLE IF NOT EXISTS seen (md5 TEXT PRIMARY KEY)")
        self.conn.commit()

    def seen(self, md5):
        cur = self.conn.execute("SELECT 1 FROM seen WHERE md5=?", (md5,))
        return cur.fetchone() is not None

    def add(self, md5):
        try:
            self.conn.execute("INSERT INTO seen (md5) VALUES (?)", (md5,))
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass
```

### 4) GitHub Actions (matrix + filelist artifact)
`.github/workflows/ingest.yml`
```yaml
name: Ingest
on:
  workflow_dispatch:
    inputs:
      date:
        description: "Date folder (YYYY-MM-DD)"
        required: true
env:
  DATE: ${{ github.event.inputs.date || '2026-05-03' }}
  REPO: datasets/axentx/surrogate-1-training-pairs

jobs:
  filelist:
    runs-on: ubuntu-latest
    outputs:
      date: ${{ env.DATE }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install huggingface_hub
      - run: python3 bin/list_folder_files.py ${{ env.REPO }} ${{ env.DATE }} filelist-${{ env.DATE }}.json
      - uses: actions/upload-artifact@v4
        with:
          name: filelist-${{ env.DATE }}
          path: filelist-${{ env.DATE }}.json

  ingest:
    needs: filelist
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    runs-on: ubuntu-latest
    env:
      DATE: ${{ needs.filelist.outputs.date }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: filelist-${{ env.DATE }}
          path: .
      - run: chmod +x bin/dataset-enrich.sh
      - run: bash bin/dataset-enrich.sh filelist-${{ env.DATE }}.json ${{ matrix.shard_id }}
      - uses: actions/upload-artifact@v4
        with:
          name: shard-${{ matrix.shard_id }}-${{ env.DATE }}
          path: batches/public-merged/${{ env.DATE }}/shard${{ matrix.shard_id }}-*.jsonl
```

### 5) Lightning training (CDN-only)
`train.py` (snippet)
```python
from datasets import load_dataset
import os

# Use filelist to know exact files; load via CDN (no HF API calls during training).
# Option A: load_dataset

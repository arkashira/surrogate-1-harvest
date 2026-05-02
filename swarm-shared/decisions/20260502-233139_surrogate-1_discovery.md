# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value change**: Replace runtime `load_dataset(streaming=True)` + recursive `list_repo_tree` in `bin/dataset-enrich.sh` with a **deterministic pre-flight snapshot + CDN-only fetches**. This eliminates HF API rate limits (429), prevents schema-cast errors from heterogeneous repos, and reduces per-shard memory pressure.

### Steps (1h 30m total)

1. **Add snapshot generator** (`bin/make-snapshot.py`) — run once per cron tick on the Mac orchestrator (or in the workflow before the matrix) to list one date folder via `list_repo_tree(recursive=False)` and emit `snapshot.json` containing `{repo, path, sha, size}` for every file. (15m)
2. **Update `bin/dataset-enrich.sh`** to accept an optional snapshot file. If provided, iterate over snapshot entries and download each via `curl`/`wget` against the CDN URL (`https://huggingface.co/datasets/.../resolve/main/...`). Skip `datasets.load_dataset` entirely. (30m)
3. **Schema projection**: parse only `{prompt, response}` at parse time; drop all other columns; normalize to UTF-8; produce one JSONL per shard. (15m)
4. **Dedup integration**: keep `lib/dedup.py` unchanged (central md5 store) but call it per parsed pair before emitting. (10m)
5. **Workflow tweak**: add a single non-matrix job step that produces `snapshot.json` and passes it as an artifact to each matrix shard. (20m)
6. **Validation**: run one shard locally with a small date folder to confirm zero HF API calls during data load and correct JSONL output. (20m)

---

### Code snippets

#### 1) Snapshot generator (`bin/make-snapshot.py`)

```python
#!/usr/bin/env python3
"""
Create a deterministic snapshot of public dataset files for one date folder.
Usage:
  python bin/make-snapshot.py axentx/surrogate-1-training-pairs 2026-05-01 > snapshot.json
"""
import json
import os
import sys
from huggingface_hub import HfApi

def main():
    repo_id = sys.argv[1]
    date_folder = sys.argv[2]          # e.g. 2026-05-01
    api = HfApi()

    # Single API call: non-recursive, one folder
    entries = api.list_repo_tree(
        repo_id=repo_id,
        path=date_folder,
        repo_type="dataset",
        recursive=False,
    )

    snapshot = []
    for e in entries:
        if e.type != "file":
            continue
        snapshot.append({
            "repo": repo_id,
            "path": f"{date_folder}/{e.path.split('/')[-1]}",
            "sha": getattr(e, "sha", None),
            "size": getattr(e, "size", None),
        })

    # Deterministic ordering for stable shard assignment
    snapshot.sort(key=lambda x: x["path"])
    json.dump(snapshot, sys.stdout, indent=2)

if __name__ == "__main__":
    main()
```

#### 2) Updated worker loop in `bin/dataset-enrich.sh` (excerpt)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
SNAPSHOT="${1:-}"          # optional snapshot.json produced by make-snapshot.py
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"

python3 -c "
import hashlib, json, os, sys, urllib.request, pyarrow as pa, pyarrow.parquet as pq

def belongs_to_shard(path, shard_id, total_shards):
    h = int(hashlib.md5(path.encode()).hexdigest(), 16)
    return (h % total_shards) == shard_id

def download_cdn(repo, path):
    url = f'https://huggingface.co/datasets/{repo}/resolve/main/{path}'
    req = urllib.request.Request(url, headers={'User-Agent': 'axentx-surrogate/1.0'})
    with urllib.request.urlopen(req) as resp:
        return resp.read()

def parse_and_project(raw_bytes, path):
    try:
        tbl = pq.read_table(pa.BufferReader(raw_bytes))
    except Exception:
        return []
    # Project only {prompt,response}; drop all other columns
    needed = {'prompt', 'response'}
    present = {c for c in tbl.column_names if c in needed}
    if not present:
        return []
    rows = []
    for col in tbl.to_pylist():
        row = {k: col.get(k, '') for k in needed}
        if not row['prompt'] or not row['response']:
            continue
        rows.append(row)
    return rows

def main():
    snapshot_path = os.environ.get('SNAPSHOT_PATH', '')
    if not snapshot_path or not os.path.exists(snapshot_path):
        print('SNAPSHOT_PATH not set or missing', file=sys.stderr)
        sys.exit(1)

    with open(snapshot_path) as f:
        files = json.load(f)

    shard_id   = int(os.environ.get('SHARD_ID', '0'))
    total      = int(os.environ.get('TOTAL_SHARDS', '16'))
    out_lines  = []

    for fobj in files:
        path = fobj['path']
        if not belongs_to_shard(path, shard_id, total):
            continue
        raw = download_cdn(fobj['repo'], path)
        pairs = parse_and_project(raw, path)
        for p in pairs:
            out_lines.append(json.dumps(p, ensure_ascii=False))

    # Emit shard JSONL to stdout or file
    sys.stdout.write('\n'.join(out_lines))

if __name__ == '__main__':
    main()
"
```

#### 3) Workflow snippet (`.github/workflows/ingest.yml` excerpt)

```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      date: ${{ steps.date.outputs.DATE }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install huggingface_hub
      - id: date
        run: echo "DATE=$(date -u +%Y-%m-%d)" >> $GITHUB_OUTPUT
      - run: python bin/make-snapshot.py axentx/surrogate-1-training-pairs ${{ steps.date.outputs.DATE }} > snapshot.json
      - uses: actions/upload-artifact@v4
        with: { name: snapshot, path: snapshot.json }

  ingest:
    needs: snapshot
    strategy:
      matrix: { shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15] }
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with: { name: snapshot, path: . }
      - run: chmod +x bin/dataset-enrich.sh
      - env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
          SHARD_ID: ${{ matrix.shard_id }}
          TOTAL_SHARDS: 16
          SNAPSHOT_PATH: snapshot.json
        run: bin/dataset-enrich.sh
```

---

### Verification checklist

- [ ] `python bin/make-snapshot.py axentx/surrogate-1-training-pairs 2026-05-01` produces valid JSON.
- [ ] Running one shard with `SNAPSHOT_PATH=snapshot.json SHARD_ID=0 TOTAL_SHARDS=16 ./bin/dataset-enrich.sh` emits valid JSONL and makes **zero** calls to `https://huggingface.co/api/`.
- [ ] Memory usage stays <2 GB per shard (no `datasets` streaming decode).
- [ ] Workflow matrix completes 16 shards without HF 429 errors.

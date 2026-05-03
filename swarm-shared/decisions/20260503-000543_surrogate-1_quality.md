# surrogate-1 / quality

## Highest-Value Improvement

Add a pre-flight snapshot generator (`bin/snapshot.sh`) + embed manifest into training so Lightning training uses **CDN-only fetches with zero HF API calls during data load**, eliminating rate-limit risk and saving quota.

---

## Implementation Plan (≤2h)

### 1) Create `bin/snapshot.sh` (30 min)
- Single API call to `list_repo_tree(path, recursive=False)` for one date folder.
- Save list to `batches/public-merged/<date>/manifest.json`.
- Output newline-delimited CDN URLs for direct download.

### 2) Update `bin/dataset-enrich.sh` (15 min)
- Call `snapshot.sh` at start of each shard run (or once per workflow and share via artifact).
- Use CDN URLs for streaming; avoid `load_dataset` on heterogeneous repos.

### 3) Update training entrypoint (Lightning `train.py`) (30 min)
- Accept `--manifest` path.
- During `DataLoader` init, read manifest and stream via `requests.get(cdn_url)` + `pyarrow.parquet.read_table` projecting `{prompt, response}` only.
- Zero `datasets`/HF API calls during training.

### 4) Studio reuse guard (15 min)
- Before `Studio(create_ok=True)`, list `Teamspace.studios` and reuse running studio with matching name.
- If stopped, restart with `target.start(machine=Machine.L40S)`.

### 5) Workflow wiring (30 min)
- In `ingest.yml`, add one-time snapshot job that produces `manifest.json` as artifact shared with 16 shard matrix jobs.
- Each shard downloads manifest and uses CDN URLs.

---

## Code Snippets

### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="batches/public-merged/${DATE}"
MANIFEST="${OUTDIR}/manifest.json"

mkdir -p "${OUTDIR}"

echo "[$(date)] Listing ${REPO} tree for ${DATE}..."
python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi

repo = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
date = os.getenv("SNAPSHOT_DATE", sys.argv[1] if len(sys.argv) > 1 else "latest")
out = sys.argv[2]

api = HfApi()
# Single non-recursive call per folder
tree = api.list_repo_tree(repo=repo, path=f"batches/public-merged/{date}", recursive=False)
files = [f.rfilename for f in tree if f.rfilename.endswith(".parquet")]

manifest = {
    "date": date,
    "repo": repo,
    "files": files,
    "cdn_urls": [f"https://huggingface.co/datasets/{repo}/resolve/main/batches/public-merged/{date}/{f}" for f in files]
}

os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as f:
    json.dump(manifest, f, indent=2)
print(f"Wrote {len(files)} files to {out}")
PY "${DATE}" "${MANIFEST}"

echo "[$(date)] Snapshot saved: ${MANIFEST}"
cat "${MANIFEST}"
```

### `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail
export SHELL=/bin/bash

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
SHARD="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"

MANIFEST="batches/public-merged/${DATE}/manifest.json"
if [[ ! -f "${MANIFEST}" ]]; then
  echo "Manifest missing, generating snapshot..."
  bin/snapshot.sh "${DATE}"
fi

# Use CDN URLs from manifest; shard by slug-hash
python3 - <<PY
import json, hashlib, sys, os, pyarrow.parquet as pq, pyarrow as pa, requests, io

with open("${MANIFEST}") as f:
    manifest = json.load(f)

shard = int("${SHARD}")
total = int("${TOTAL_SHARDS}")

def slug_hash(s):
    return int(hashlib.md5(s.encode()).hexdigest(), 16)

rows = []
for url in manifest["cdn_urls"]:
    # CDN download: no Authorization header, bypasses /api/ rate limits
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    table = pq.read_table(io.BytesIO(resp.content))
    # Project only {prompt, response} at parse time
    cols = {c: table[c] for c in table.column_names if c in ("prompt", "response")}
    if not cols:
        continue
    proj = pa.table(cols)
    for i in range(proj.num_rows):
        prompt = proj["prompt"][i].as_py()
        response = proj["response"][i].as_py()
        key = f"{prompt[:60]}::{response[:60]}"
        if shard_hash(key) % total == shard:
            rows.append({"prompt": prompt, "response": response})

# Dedup via central store (lib/dedup.py) or local md5
# Upload shard output to batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl
print(f"Shard {shard}: collected {len(rows)} rows")
PY
```

### Lightning `train.py` (excerpt)
```python
import argparse, json, io, requests, pyarrow.parquet as pq, torch
from torch.utils.data import IterableDataset, DataLoader

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path, project_cols=("prompt", "response")):
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.project_cols = project_cols

    def __iter__(self):
        for url in self.manifest["cdn_urls"]:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            table = pq.read_table(io.BytesIO(resp.content))
            cols = {c: table[c] for c in self.project_cols if c in table.column_names}
            if not cols:
                continue
            proj = pa.table(cols)
            for i in range(proj.num_rows):
                yield {k: proj[k][i].as_py() for k in cols}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest.json")
    parser.add_argument("--output", default="checkpoints")
    args = parser.parse_args()

    # Studio reuse guard
    try:
        from lightning import Studio, Teamspace, Machine, L40S
        running = [s for s in Teamspace.studios if s.name == "surrogate-1-train" and s.status == "Running"]
        if running:
            studio = running[0]
        else:
            studio = Studio.create(name="surrogate-1-train", machine=Machine.L40S, create_ok=True)
    except Exception:
        studio = None

    dataset = CDNParquetDataset(args.manifest)
    loader = DataLoader(dataset, batch_size=8, num_workers=4)

    # Minimal training loop placeholder
    for batch in loader:
        # train step
        pass

    if studio and studio.status != "Running":
        studio.start(machine=Machine.L40S)

if __name__ == "__main__":
    main()
```

### `.github/workflows/ingest.yml` (excerpt)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      manifest: ${{ steps.manifest.outputs.path }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install -r requirements.txt
      - run: bin/snapshot.sh ${{ github.event.inputs.date || '' }}
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
      - name: Upload manifest
        uses: actions/upload-artifact@v4
        with:
          name: manifest
          path: batches/public-merged/*/manifest.json

  shard:
    needs: snapshot


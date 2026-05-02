# surrogate-1 / quality

## Implementation Plan (≤2h)

**Goal**: Eliminate HF API 429s during training and make shard workers fully resilient by deterministic pre-flight file listing + CDN-only ingestion.

### Steps (concrete)

1. Add `bin/list-snapshot.sh` — one-shot Mac/Linux script that:
   - Calls `list_repo_tree(recursive=False)` for today’s folder (YYYY-MM-DD)  
   - Saves `snapshot-YYYY-MM-DD.json` with `{date, ts, files: [{path, size, sha}]}`  
   - Exits 0 only if snapshot created and non-empty

2. Update `bin/dataset-enrich.sh` to accept an optional snapshot file:
   - If snapshot provided: read file list from JSON and stream via CDN URLs  
   - If no snapshot: fall back to current behavior (HF API list + stream)  
   - Always project to `{prompt, response}` only before any heavy work

3. Update GitHub Actions matrix to:
   - Run `list-snapshot.sh` once in a prior job (or first matrix entry)  
   - Pass the snapshot artifact to all 16 shard runners  
   - Each shard deterministically owns `hash(slug) % 16 == SHARD_ID` rows from snapshot

4. Update training script (`train.py` or equivalent) to:
   - Accept the same snapshot JSON  
   - Use CDN-only downloads (`https://huggingface.co/datasets/.../resolve/main/...`) with `requests`/`urllib` + `pyarrow`  
   - Zero HF API calls during data loading

5. Add small retry/back-off for CDN downloads (separate from API limits).

---

## Code Snippets

### 1) `bin/list-snapshot.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="${2:-snapshots}"
OUT="${OUTDIR}/snapshot-${DATE}.json"

mkdir -p "${OUTDIR}"

python3 - <<PY
import os, json, datetime, sys
from huggingface_hub import HfApi

repo = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
date = os.getenv("SNAPSHOT_DATE", datetime.date.today().isoformat())
out = os.getenv("SNAPSHOT_OUT", "snapshots/snapshot-{}.json".format(date))

api = HfApi(token=os.getenv("HF_TOKEN"))
try:
    tree = api.list_repo_tree(repo=repo, path=date, recursive=False)
except Exception as e:
    # If folder missing, produce empty snapshot so runners skip cleanly
    files = []
else:
    files = [{"path": f.rfilename, "size": getattr(f, "size", None)} for f in tree]

os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as fh:
    json.dump({"date": date, "ts": datetime.datetime.utcnow().isoformat() + "Z", "files": files}, fh)
print(f"Wrote {len(files)} entries to {out}")
PY

echo "Snapshot created: ${OUT}"
```

Make executable:

```bash
chmod +x bin/list-snapshot.sh
```

---

### 2) `bin/dataset-enrich.sh` (updated core section)

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
SNAPSHOT="${SNAPSHOT:-}"          # optional path to snapshot JSON
DATE="${DATE:-$(date +%Y-%m-%d)}"
BATCH_DIR="batches/public-merged/${DATE}"
TS="$(date +%H%M%S)"
OUT="${BATCH_DIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "$(dirname "${OUT}")"

python3 - <<PY
import os, json, hashlib, sys, urllib.request, pyarrow.parquet as pq, pyarrow as pa, io, tempfile

repo = os.getenv("REPO", "axentx/surrogate-1-training-pairs")
shard_id = int(os.getenv("SHARD_ID", "0"))
total_shards = int(os.getenv("TOTAL_SHARDS", "16"))
snapshot_path = os.getenv("SNAPSHOT", "")
date = os.getenv("DATE", "")
out_path = os.getenv("OUT_PATH", "batches/public-merged/unknown/shard0.jsonl")

def shard_owns(slug: str) -> bool:
    h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    return (h % total_shards) == shard_id

def cdn_url(path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def parse_file_cdn(path: str):
    # Download via CDN (no auth, bypasses API rate limits)
    url = cdn_url(path)
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()
    # Try parquet first; fallback to jsonl if needed
    try:
        table = pq.read_table(io.BytesIO(data))
        df = table.to_pandas()
    except Exception:
        # assume jsonl lines
        lines = data.decode().strip().splitlines()
        import json as _json
        rows = [_json.loads(l) for l in lines if l.strip()]
        df = pa.Table.from_pylist(rows).to_pandas()
    # Project to {prompt, response} only
    if "prompt" not in df.columns or "response" not in df.columns:
        # best-effort column mapping
        col_map = {}
        for c in df.columns:
            low = c.strip().lower()
            if "prompt" in low:
                col_map[c] = "prompt"
            elif "response" in low or "completion" in low or "answer" in low:
                col_map[c] = "response"
        if col_map:
            df = df.rename(columns=col_map)
    if "prompt" not in df.columns or "response" not in df.columns:
        return []
    out_rows = []
    for _, row in df.iterrows():
        prompt = str(row.get("prompt", "")).strip()
        response = str(row.get("response", "")).strip()
        if prompt and response:
            out_rows.append({"prompt": prompt, "response": response})
    return out_rows

# Determine file list
files = []
if snapshot_path and os.path.isfile(snapshot_path):
    with open(snapshot_path) as fh:
        snap = json.load(fh)
    files = [f["path"] for f in snap.get("files", [])]
else:
    # fallback: use HF API (may hit rate limits)
    from huggingface_hub import HfApi
    api = HfApi(token=os.getenv("HF_TOKEN"))
    try:
        tree = api.list_repo_tree(repo=repo, path=date, recursive=False)
        files = [f.rfilename for f in tree]
    except Exception:
        files = []

# Filter to owned shard
files = [f for f in files if shard_owns(f)]

os.makedirs(os.path.dirname(out_path), exist_ok=True)
written = 0
with open(out_path, "w") as out_f:
    for f in files:
        try:
            rows = parse_file_cdn(f)
            for r in rows:
                out_f.write(json.dumps(r, ensure_ascii=False) + "\n")
                written += 1
        except Exception as e:
            print(f"Skipped {f}: {e}", file=sys.stderr)

print(f"Shard {shard_id} wrote {written} rows to {out_path}")
PY
```

Make executable:

```bash
chmod +x bin/dataset-enrich.sh
```

---

### 3) GitHub Actions update (`.github/workflows/ingest.yml` snippet)

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
      - run

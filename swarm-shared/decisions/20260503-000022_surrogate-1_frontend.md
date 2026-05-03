# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit pressure during ingestion and ensures deterministic file lists for reproducible sharding.

### Steps (1h 30m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Accept `DATE` (YYYY-MM-DD) and optional `REPO` (default: `axentx/surrogate-1-training-pairs`)  
   - Use `huggingface_hub` CLI or Python to call `list_repo_tree(path=f"public-merged/{DATE}", recursive=True)`  
   - Filter to `.jsonl` and `.parquet` files only  
   - Output sorted JSON manifest to `snapshots/{DATE}-files.json`  
   - Include commit hash and timestamp for traceability

2. **Create `bin/lib/manifest.py`** (20m)  
   - Load snapshot JSON  
   - Provide deterministic shard assignment: `hash(slug) % 16` → `SHARD_ID`  
   - Expose iterator for files assigned to a given shard  
   - Validate file existence via CDN HEAD (optional, fast)

3. **Update `bin/dataset-enrich.sh`** (20m)  
   - Accept `SHARD_ID` and `DATE` as env vars (from matrix)  
   - Load manifest for that date, filter to files for this shard  
   - Pass file list to Python worker via env or temp file  
   - Remove any recursive listing or `list_repo_files` calls from worker

4. **Create `bin/train-cdn-only.py`** (30m)  
   - Accept manifest file path and date  
   - Build URLs using CDN pattern: `https://huggingface.co/datasets/{repo}/resolve/main/public-merged/{DATE}/{file}`  
   - Stream files with `requests` or `urllib` (no auth header)  
   - Parse and project to `{prompt, response}` on the fly  
   - Write normalized JSONL for this shard

5. **Update GitHub Actions matrix** (10m)  
   - Add `snapshot` job that runs before parallel runners  
   - Upload manifest as artifact  
   - Pass `DATE` and `SHARD_ID` to each runner  
   - Each runner downloads manifest artifact and uses CDN-only loader

6. **Add safety checks** (10m)  
   - If snapshot fails, fail fast before spawning 16 runners  
   - Validate total file count > 0  
   - Ensure deterministic ordering (sort by filename)

---

## Code Snippets

### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="snapshots"
OUTFILE="${OUTDIR}/${DATE}-files.json"

mkdir -p "${OUTDIR}"

python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi

repo = os.environ["REPO"]
date = os.environ["DATE"]
api = HfApi()

# List only the date folder (non-recursive) then recurse manually to avoid huge trees
prefix = f"public-merged/{date}/"
try:
    tree = api.list_repo_tree(repo=repo, path=prefix, recursive=True)
except Exception as e:
    print(f"Error listing repo tree: {e}", file=sys.stderr)
    sys.exit(1)

files = sorted([
    f.rfilename for f in tree
    if f.rfilename.endswith(('.jsonl', '.parquet'))
])

manifest = {
    "repo": repo,
    "date": date,
    "generated_at": __import__('datetime').datetime.utcnow().isoformat() + "Z",
    "files": files,
    "count": len(files)
}

with open(os.environ["OUTFILE"], "w") as fh:
    json.dump(manifest, fh, indent=2)

print(f"Snapshot written: {os.environ['OUTFILE']} ({len(files)} files)")
PY
```

### `bin/lib/manifest.py`
```python
import json
import hashlib
from pathlib import Path
from typing import List, Dict

def load_manifest(date: str, snapshot_dir: Path = Path("snapshots")) -> Dict:
    path = snapshot_dir / f"{date}-files.json"
    if not path.exists():
        raise FileNotFoundError(f"Snapshot not found: {path}")
    with open(path) as f:
        return json.load(f)

def shard_for_file(filename: str, n_shards: int = 16) -> int:
    """Deterministic shard assignment."""
    digest = hashlib.md5(filename.encode()).hexdigest()
    return int(digest, 16) % n_shards

def files_for_shard(manifest: Dict, shard_id: int, n_shards: int = 16) -> List[str]:
    return [
        f for f in manifest["files"]
        if shard_for_file(f, n_shards) == shard_id
    ]

def cdn_url(repo: str, date: str, filename: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/public-merged/{date}/{filename}"
```

### `bin/dataset-enrich.sh` (updated usage)
```bash
#!/usr/bin/env bash
set -euo pipefail

SHARD_ID="${SHARD_ID:?required}"
DATE="${DATE:?required}"
REPO="${REPO:-axentx/surrogate-1-training-pairs}"

python3 bin/worker.py \
  --shard-id "${SHARD_ID}" \
  --date "${DATE}" \
  --repo "${REPO}" \
  --manifest "snapshots/${DATE}-files.json"
```

### `bin/worker.py` (minimal CDN-only loader)
```python
import argparse
import json
import requests
import sys
from pathlib import Path
from bin.lib.manifest import load_manifest, files_for_shard, cdn_url

def stream_file(url: str):
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        # If parquet, use pyarrow; if jsonl, iterate lines
        if url.endswith(".jsonl"):
            for line in r.iter_lines(decode_unicode=True):
                if line:
                    yield json.loads(line)
        else:
            import pyarrow.parquet as pq
            import io
            data = io.BytesIO(r.content)
            table = pq.read_table(data)
            for batch in table.to_batches(max_chunksize=1000):
                for row in batch.to_pylist():
                    yield row

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()

    manifest = load_manifest(args.date, Path(args.manifest).parent)
    files = files_for_shard(manifest, args.shard_id)

    out_path = Path(f"batches/public-merged/{args.date}/shard{args.shard_id}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w") as out_f:
        for fn in sorted(files):
            url = cdn_url(args.repo, args.date, fn)
            try:
                for record in stream_file(url):
                    # Project to {prompt, response} only
                    prompt = record.get("prompt") or record.get("input") or ""
                    response = record.get("response") or record.get("output") or ""
                    if prompt and response:
                        out_f.write(json.dumps({"prompt": prompt, "response": response}) + "\n")
            except Exception as e:
                print(f"Error processing {fn}: {e}", file=sys.stderr)

    print(f"Shard {args.shard_id} done: {out_path}")

if __name__ == "__main__":
    main()
```

### GitHub Actions snippet (add to `ingest.yml`)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:

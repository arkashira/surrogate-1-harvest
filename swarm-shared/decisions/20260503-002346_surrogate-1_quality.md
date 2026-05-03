# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Highest-value change**: Add `bin/snapshot.sh` that produces a deterministic file manifest per date folder and update ingestion/training to use CDN URLs exclusively when a snapshot is provided. This eliminates HuggingFace API calls during training data loading and prevents 429s.

### Unified Steps (1h 45m total)

1. **Create `bin/snapshot.sh`** (25m)  
   - Accepts `REPO`, `DATE` (YYYY-MM-DD), optional `OUT` (default `snapshot-<REPO>-<DATE>.json`)  
   - Uses `huggingface_hub` to call `list_repo_tree(path=<DATE>, recursive=False)`  
   - Emits JSON with metadata + files array containing `{ "path": "...", "cdn_url": "https://huggingface.co/datasets/<REPO>/resolve/main/<DATE>/<file>" }`  
   - Validates non-empty; exits non-zero on failure; prints JSON path to stdout when successful.

2. **Create `lib/cdn_loader.py`** (30m)  
   - `load_from_snapshot(manifest_path, columns=("prompt","response"))`  
   - Reads manifest, streams each file via CDN URL with `requests.get(..., stream=True)`  
   - Decodes parquet/JSONL in chunks, projects only required columns, yields rows  
   - Retries per file with exponential backoff; skips corrupt files with warning.

3. **Update training script** (35m)  
   - Add CLI flag `--snapshot` (path) or `--date` (auto-generates snapshot)  
   - If provided, use `cdn_loader.load_from_snapshot()` instead of `load_dataset()`  
   - Keep existing `load_dataset()` path as fallback for local dev.

4. **Update ingestion script** (25m)  
   - Add optional `--snapshot` flag to `dataset-enrich.sh`  
   - When present, use CDN loader for source files instead of `load_dataset(streaming=True)`  
   - This prevents OOM on the HF Space during parallel shard runs.

5. **Add to workflow** (10m)  
   - In `ingest.yml`, add an optional step before matrix to generate snapshot for current date and upload as artifact for all shards to download (avoids 16× API calls).  
   - If artifact missing, shards fall back to single API call each (existing behavior).

---

## Final Code Snippets

### 1. `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE="${2:-$(date +%Y-%m-%d)}"
OUT="${3:-snapshot-${REPO//\//-}-${DATE}.json}"

python3 - "$REPO" "$DATE" "$OUT" <<'PY'
import json, sys, datetime
from huggingface_hub import HfApi

repo, date, out = sys.argv[1], sys.argv[2], sys.argv[3]
api = HfApi()

try:
    files = api.list_repo_tree(repo=repo, path=date, recursive=False)
except Exception as e:
    print(f"ERROR: failed to list repo tree: {e}", file=sys.stderr)
    sys.exit(1)

manifest = {
    "repo": repo,
    "date": date,
    "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    "files": []
}

for f in files:
    if f.type != "file":
        continue
    path = f.rfilename
    manifest["files"].append({
        "path": path,
        "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    })

if not manifest["files"]:
    print(f"ERROR: no files found in {repo} at {date}", file=sys.stderr)
    sys.exit(1)

with open(out, "w") as fh:
    json.dump(manifest, fh, indent=2)
print(out)
PY
```

### 2. `lib/cdn_loader.py`
```python
import json, io, time, requests
from typing import List, Dict, Iterator
import pyarrow.parquet as pq

def _backoff(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    return min(cap, base * (2 ** attempt))

def load_from_snapshot(manifest_path: str, columns: List[str] = ("prompt", "response")) -> Iterator[Dict]:
    with open(manifest_path) as f:
        manifest = json.load(f)

    files = manifest.get("files", [])
    if not files:
        return

    for item in files:
        url = item.get("cdn_url") or item.get("path")
        if not url:
            continue
        if not url.startswith("http"):
            # fallback to CDN construction
            repo = manifest.get("repo", "")
            url = f"https://huggingface.co/datasets/{repo}/resolve/main/{item['path']}"

        for attempt in range(5):
            try:
                resp = requests.get(url, stream=True, timeout=30)
                resp.raise_for_status()
                break
            except Exception as e:
                wait = _backoff(attempt)
                print(f"WARN: attempt {attempt+1}/5 failed for {url}: {e}; retry in {wait:.1f}s")
                time.sleep(wait)
        else:
            print(f"WARN: failed to fetch {url} after 5 attempts")
            continue

        data = resp.content
        try:
            if url.endswith(".parquet"):
                table = pq.read_table(io.BytesIO(data), columns=columns)
                for batch in table.to_batches(max_chunksize=1000):
                    cols = {c: batch.column(c) for c in columns}
                    for i in range(batch.num_rows):
                        yield {c: cols[c][i].as_py() for c in columns}
            elif url.endswith(".jsonl"):
                for line in data.splitlines():
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        yield {c: row.get(c) for c in columns}
                    except Exception:
                        continue
            else:
                print(f"WARN: unsupported file {url}")
        except Exception as e:
            print(f"WARN: failed to decode {url}: {e}")
            continue
```

### 3. Training script changes (excerpt)
```python
import argparse
from pathlib import Path
import datetime

parser = argparse.ArgumentParser()
parser.add_argument("--snapshot", type=Path, help="Path to manifest JSON for CDN-only loading")
parser.add_argument("--date", type=str, help="Date (YYYY-MM-DD) to auto-generate snapshot")
args, rest = parser.parse_known_args()

if args.snapshot or args.date:
    from lib.cdn_loader import load_from_snapshot
    if args.date:
        # auto-generate snapshot for date
        repo = "axentx/surrogate-1-training-pairs"
        out = f"snapshot-{repo.replace('/', '-')}-{args.date}.json"
        import subprocess, sys
        subprocess.run([sys.executable, "bin/snapshot.sh", repo, args.date, out], check=True)
        snapshot_path = out
    else:
        snapshot_path = args.snapshot
    train_data = list(load_from_snapshot(snapshot_path))
else:
    from datasets import load_dataset
    ds = load_dataset("axentx/surrogate-1-training-pairs", streaming=True)
    train_data = (ex for ex in ds["train"].select_columns(["prompt", "response"]))
```

### 4. Ingestion script change (excerpt in `bin/dataset-enrich.sh`)
```bash
# optional snapshot mode
if [[ -n "${SNAPSHOT:-}" ]]; then
  python3 -c "
import json, sys, io, pyarrow.parquet as pq, requests
manifest = json.load(open(sys.argv[1]))
for item in manifest['files']:
    url = item.get('cdn_url') or f\"https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{item['path']}\"
    data = requests.get(url, timeout=30).content
    table = pq.read_table(io.BytesIO(data), columns=['prompt','response'])
    for batch in table.to_batches(max_chunksize=5000):
        # process batch

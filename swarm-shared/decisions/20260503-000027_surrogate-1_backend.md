# surrogate-1 / backend

## Implementation Plan — CDN-only training with pre-flight snapshot

**Goal**: Eliminate HF API rate-limit risk during parallel shard ingestion by generating a date-scoped file manifest once, then using CDN-only (`resolve/main/`) downloads in workers.

**Scope** (fits <2h):
1. Add `bin/snapshot.sh` — lists dataset files for a date folder via HF API (single call), writes `snapshot-<date>.json`.
2. Update `bin/dataset-enrich.sh` to accept an optional snapshot file and stream from CDN URLs instead of `load_dataset(..., streaming=True)` for heterogeneous schemas.
3. Update GitHub Actions matrix to generate snapshot in a setup step and pass it to each shard runner.
4. Keep dedup behavior unchanged (central SQLite store on HF Space remains source of truth).

---

### 1) `bin/snapshot.sh`

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: bin/snapshot.sh <repo> <date_folder> [output.json]
# Example: bin/snapshot.sh axentx/surrogate-1-training-pairs 2026-05-02 snapshot-2026-05-02.json

set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE_FOLDER="${2:-$(date +%Y-%m-%d)}"
OUTPUT="${3:-snapshot-${DATE_FOLDER}.json}"

# Use HF API to list files in the date folder (non-recursive to avoid pagination explosion)
# If recursive is needed, paginate safely with `recursive=True` and jq pagination handling.
echo "Listing ${REPO} path=${DATE_FOLDER} (non-recursive)..."

python3 - "$REPO" "$DATE_FOLDER" "$OUTPUT" <<'PY'
import os, json, sys
from huggingface_hub import HfApi

repo = sys.argv[1]
path = sys.argv[2]
out = sys.argv[3]

api = HfApi()
# Non-recursive to avoid 100+ pages; adjust if nested folders are required.
tree = api.list_repo_tree(repo=repo, path=path, recursive=False)

files = []
for item in tree:
    if item.type == "file":
        files.append(item.path)

# If recursive needed, uncomment and handle pagination:
# tree = api.list_repo_tree(repo=repo, path=path, recursive=True)
# files = [it.path for it in tree if it.type == "file"]

payload = {
    "repo": repo,
    "date_folder": path,
    "generated_at_utc": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    "files": sorted(set(files))
}

os.makedirs(os.path.dirname(out) if os.path.dirname(out) else ".", exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2)

print(f"Wrote {len(payload['files'])} files to {out}")
PY

echo "Snapshot written to ${OUTPUT}"
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

### 2) Update `bin/dataset-enrich.sh` to support CDN streaming

Key changes:
- Accept `--snapshot <file>` and use it to build CDN URLs.
- Avoid `load_dataset(streaming=True)` for heterogeneous schemas; download individual files via CDN and parse only `{prompt,response}`.
- Keep existing dedup call to central store.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Existing worker script — updated to support CDN-only mode via snapshot.

set -euo pipefail

# Existing env defaults
REPO="${REPO:-axentx/surrogate-1-training-pairs}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
HF_TOKEN="${HF_TOKEN:-}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"
DATE_FOLDER="${DATE_FOLDER:-$(date +%Y-%m-%d)}"

# Python worker
python3 - "$REPO" "$SHARD_ID" "$TOTAL_SHARDS" "$SNAPSHOT_FILE" "$DATE_FOLDER" <<'PY'
import os, sys, json, hashlib, datetime, pyarrow as pa, pyarrow.parquet as pq
from pathlib import Path
import requests
from datasets import load_dataset  # fallback only

REPO = sys.argv[1]
SHARD_ID = int(sys.argv[2])
TOTAL_SHARDS = int(sys.argv[3])
SNAPSHOT_FILE = sys.argv[4] or None
DATE_FOLDER = sys.argv[5]

CDN_BASE = f"https://huggingface.co/datasets/{REPO}/resolve/main"

def deterministic_shard(path: str, n: int) -> int:
    return int(hashlib.md5(path.encode()).hexdigest(), 16) % n

def list_files_from_snapshot(snapshot_path: str):
    with open(snapshot_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("files", [])

def list_files_via_api():
    # Fallback: non-recursive list (same as snapshot behavior)
    from huggingface_hub import HfApi
    api = HfApi()
    tree = api.list_repo_tree(repo=REPO, path=DATE_FOLDER, recursive=False)
    return sorted([it.path for it in tree if it.type == "file"])

def stream_cdn_files(file_paths):
    for fp in file_paths:
        if deterministic_shard(fp, TOTAL_SHARDS) != SHARD_ID:
            continue
        url = f"{CDN_BASE}/{fp}"
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
        except Exception as exc:
            print(f"Failed to fetch {url}: {exc}", file=sys.stderr)
            continue

        # Heuristic: assume parquet or jsonl; project to prompt/response at parse time.
        suffix = Path(fp).suffix.lower()
        rows = []
        if suffix == ".parquet":
            try:
                tbl = pq.read_table(pa.BufferReader(resp.content))
                df = tbl.to_pandas()
                # Project to expected fields; tolerate schema heterogeneity
                for _, r in df.iterrows():
                    prompt = r.get("prompt") or r.get("input") or r.get("text") or ""
                    response = r.get("response") or r.get("output") or r.get("completion") or ""
                    if prompt or response:
                        rows.append({"prompt": str(prompt), "response": str(response)})
            except Exception as exc:
                print(f"Failed to decode parquet {fp}: {exc}", file=sys.stderr)
                continue
        elif suffix == ".jsonl":
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    prompt = r.get("prompt") or r.get("input") or r.get("text") or ""
                    response = r.get("response") or r.get("output") or r.get("completion") or ""
                    if prompt or response:
                        rows.append({"prompt": str(prompt), "response": str(response)})
                except Exception:
                    continue
        else:
            # Unknown format — skip or attempt json load
            print(f"Skipping unsupported file {fp}", file=sys.stderr)
            continue

        for row in rows:
            yield row

def main():
    if SNAPSHOT_FILE and os.path.isfile(SNAPSHOT_FILE):
        files = list_files_from_snapshot(SNAPSHOT_FILE)
        print(f"Using snapshot {SNAPSHOT_FILE} with {len(files)} files")
    else:
        print("No snapshot provided; listing via API (non-recursive)...")
        files = list_files_via_api()

    if not files:
        print("No files to process for this shard.")
        return

    out_dir = Path("batches/public-merged") / DATE_FOLDER
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    outfile = out_dir / f"shard{SHARD_ID}-{ts}.jsonl"

    written = 0
    with outfile.open("w", encoding="utf-8") as f:
        for row in stream_cdn_files(files):
            # Optional: call central dedup here (e.g., POST to central store or use lib/dedup.py)
            # For now, write all rows (dedup remains responsibility of central store).
           

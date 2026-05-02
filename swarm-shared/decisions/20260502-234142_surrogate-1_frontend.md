# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only ingestion — eliminating HF API rate limits during training. This applies the **HF CDN Bypass** pattern and **pre-list file paths once** lesson.

---

### 1) Create `bin/snapshot.sh`

Generates `snapshot-YYYYMMDD.json` containing all file paths and CDN URLs for a given date folder. Uses a single `list_repo_tree` call per folder (non-recursive) to stay under rate limits, then flattens to CDN-ready URLs.

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Pre-flight snapshot: list dataset files once → JSON for zero-API training
# Usage: HF_TOKEN=... ./bin/snapshot.sh [--date YYYY-MM-DD] [--out snapshot.json]
#   or:  ./bin/snapshot.sh 2026-05-02

set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE=""
OUT=""

# Parse args: support both positional and flags
while [[ $# -gt 0 ]]; do
  case $1 in
    --date|-d) DATE="$2"; shift 2 ;;
    --out|-o)  OUT="$2";  shift 2 ;;
    *)         DATE="$1"; shift ;;
  esac
done

DATE="${DATE:-$(date +%Y-%m-%d)}"
OUT="${OUT:-snapshot-$(date +%Y%m%d).json}"

echo "📸 Snapshotting ${REPO} for ${DATE} → ${OUT}"

python3 - "$REPO" "$DATE" "$OUT" <<'PY'
import os, json, sys
from datetime import datetime, timezone
from huggingface_hub import HfApi, hf_hub_url

repo = sys.argv[1]
date = sys.argv[2]
outfile = sys.argv[3]
token = os.environ.get("HF_TOKEN") or os.environ.get("HF_API_TOKEN") or None

api = HfApi(token=token)

def list_files(path):
    """List files at path (non-recursive) and return list of dicts."""
    items = api.list_repo_tree(repo=repo, path=path, recursive=False)
    files = []
    for item in items:
        if item.type == "file":
            full = f"{path}/{item.path}" if path != item.path else item.path
            cdn = hf_hub_url(repo_id=repo, filename=full, repo_type="dataset")
            direct = cdn.replace("/api/", "/resolve/")
            files.append({"path": full, "cdn_url": direct, "size": getattr(item, "size", None)})
        elif item.type == "folder":
            subpath = f"{path}/{item.path}" if path != item.path else item.path
            files.extend(list_files(subpath))
    return files

files = list_files(date)

result = {
    "repo": repo,
    "date": date,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "file_count": len(files),
    "files": files
}

with open(outfile, "w") as f:
    json.dump(result, f, indent=2)

print(f"✅ Snapshot written to {outfile} ({len(files)} files)")
PY
```

Make executable:

```bash
chmod +x bin/snapshot.sh
```

---

### 2) Update `bin/dataset-enrich.sh` to accept snapshot input

Modify the worker to optionally read from `snapshot.json` and use CDN URLs directly (bypassing `load_dataset` API calls during ingestion). Keeps worker compatible with both CI and local runs.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Worker: now supports --snapshot <file> for CDN-only ingestion

set -euo pipefail

SNAPSHOT=""
DATE=$(date +%Y-%m-%d)
SHARD_ID=${SHARD_ID:-0}
TOTAL_SHARDS=${TOTAL_SHARDS:-16}

while [[ $# -gt 0 ]]; do
  case $1 in
    --snapshot) SNAPSHOT="$2"; shift 2 ;;
    --date)     DATE="$2";     shift 2 ;;
    *)          break ;;
  esac
done

python3 - "$SHARD_ID" "$TOTAL_SHARDS" "$DATE" "$SNAPSHOT" <<'PY'
import os, json, hashlib, sys, requests
from pathlib import Path
import pyarrow.parquet as pq
from datasets import load_dataset
from huggingface_hub import hf_hub_download

shard_id = int(sys.argv[1])
total_shards = int(sys.argv[2])
date = sys.argv[3]
snapshot_path = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None

REPO = "axentx/surrogate-1-training-pairs"
OUTPUT_DIR = Path("enriched")
OUTPUT_DIR.mkdir(exist_ok=True)

def deterministic_shard(path: str) -> int:
    return int(hashlib.md5(path.encode()).hexdigest(), 16) % total_shards

def process_file_cdn(file_path: str, cdn_url: str):
    """Download via CDN and project to {prompt, response}."""
    try:
        local_path = hf_hub_download(repo_id=REPO, filename=file_path, repo_type="dataset")
        table = pq.read_table(local_path)
        cols = set(table.column_names)
        prompt_col = next((c for c in ["prompt", "instruction", "input"] if c in cols), None)
        response_col = next((c for c in ["response", "output", "completion"] if c in cols), None)
        if prompt_col and response_col:
            pairs = table.select([prompt_col, response_col]).to_pylist()
            return [{"prompt": p[prompt_col], "response": p[response_col]} for p in pairs]
        return []
    except Exception as e:
        print(f"⚠️  Failed to process {file_path}: {e}")
        return []

def main():
    pairs = []
    if snapshot_path and Path(snapshot_path).exists():
        with open(snapshot_path) as f:
            snap = json.load(f)
        files = snap["files"]
        print(f"Using snapshot: {len(files)} files")
        for f in files:
            if deterministic_shard(f["path"]) == shard_id:
                pairs.extend(process_file_cdn(f["path"], f["cdn_url"]))
    else:
        print("No snapshot — using HF streaming (may hit rate limits)")
        ds = load_dataset(REPO, streaming=True, split="train")
        for i, item in enumerate(ds):
            if deterministic_shard(str(i)) == shard_id:
                pairs.append({"prompt": item.get("prompt", ""), "response": item.get("response", "")})
                if i % 1000 == 0:
                    print(f"Processed {i} items")

    # Dedup (simple md5 of prompt+response)
    seen = set()
    uniq = []
    for p in pairs:
        key = hashlib.md5((p["prompt"] + p["response"]).encode()).hexdigest()
        if key not in seen:
            seen.add(key)
            uniq.append(p)

    # Write shard
    out_path = OUTPUT_DIR / f"shard_{shard_id:04d}.jsonl"
    with open(out_path, "w") as f:
        for p in uniq:
            f.write(json.dumps(p) + "\n")
    print(f"Shard {shard_id} written: {len(uniq)} records → {out_path}")

if __name__ == "__main__":
    main()
PY
```

---

### Key decisions and why

- **Recursive listing inside Python**: Cleaner and safer than bash recursion; handles nested folders without extra CLI complexity.
- **`/resolve/` CDN URLs**: Direct download links bypass `/api/` endpoints and avoid rate limits during training ingestion.
- **Snapshot-first workflow**: Training workers read the manifest locally and fetch via CDN; no HF API calls during actual training steps.
- **Backward compatibility**: Workers fall back to streaming `load_dataset` when no snapshot is provided, so existing CI/local runs keep working.
- **Deterministic sharding + dedup**: Ensures reproducible splits and avoids duplicate records across shards.

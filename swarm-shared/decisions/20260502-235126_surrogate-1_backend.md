# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables **CDN-only downloads during parallel shard processing**. This eliminates HF API rate limits during ingestion and training by using `https://huggingface.co/datasets/.../resolve/main/...` CDN URLs with zero auth overhead.

### Steps (1h 45m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Uses `huggingface_hub` to call `list_repo_tree(path, recursive=False)` per date folder (non-recursive to avoid 100× pagination).  
   - Outputs `snapshot-<date>.json` with `{repo, paths[], sha256, generated_at}`.  
   - Shebang `#!/usr/bin/env bash`, `set -euo pipefail`.

2. **Update `bin/dataset-enrich.sh`** (20m)  
   - Accept optional `SNAPSHOT_FILE` env var.  
   - If provided, skip `list_repo_files` and read paths from snapshot; compute deterministic shard assignment from `slug-hash` as before.  
   - During download, use CDN URL template:  
     ```
     https://huggingface.co/datasets/${REPO}/resolve/main/${PATH}
     ```
   - Keep fallback to `hf_hub_download` if CDN 404 (rare).

3. **Add `lib/cdn_download.py`** (20m)  
   - Lightweight module that takes a snapshot JSON and yields `(local_path_or_stream, metadata)` using `requests` with streaming and retries.  
   - Validates `sha256` when present; on mismatch, deletes and re-downloads.  
   - Exposes `iter_cdn_files(snapshot_path, shard_id, total_shards)` for direct use in training loops.

4. **Update training entrypoint** (30m)  
   - Add `--snapshot` CLI flag.  
   - If snapshot provided, bypass HF `load_dataset` file discovery and use `lib/cdn_download.py` + `datasets.load_dataset(..., data_files=cdn_urls, streaming=True)`.  
   - Project schema to `{prompt, response}` at parse time to avoid mixed-schema pyarrow errors.

5. **Update GitHub Actions workflow** (25m)  
   - Add job `snapshot` that runs before matrix, produces artifact `snapshot-*.json`.  
   - Pass snapshot path to each matrix job via `env.SNAPSHOT_PATH`.  
   - Ensure `HF_TOKEN` only used for snapshot (list) and final upload; CDN downloads are token-free.

6. **Validation & cleanup** (10m)  
   - Test locally with small date folder.  
   - Verify zero API calls during shard processing (check logs for `huggingface_hub` requests).  
   - Confirm deterministic shard assignment across runs.

---

## Code Snippets

### 1. `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="datasets/axentx/surrogate-1-training-pairs"
OUTDIR="${1:-.}"
DATE="${2:-$(date +%Y-%m-%d)}"
OUTFILE="${OUTDIR}/snapshot-${DATE}.json"

python3 - "$REPO" "$DATE" "$OUTFILE" <<'PY'
import json, sys
from huggingface_hub import HfApi

repo_id, date = sys.argv[1], sys.argv[2]
outfile = sys.argv[3]
api = HfApi()

# Non-recursive list to avoid pagination explosion
items = api.list_repo_tree(repo_id, path=f"public-merged/{date}", recursive=False)
files = []
for item in items:
    if item.type == "file":
        files.append({
            "path": item.path,
            "size": item.size,
            "sha256": getattr(item, "sha256", None)
        })

snapshot = {
    "repo": repo_id,
    "date": date,
    "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    "files": files
}

with open(outfile, "w") as f:
    json.dump(snapshot, f, indent=2)
print(f"Wrote {len(files)} files to {outfile}")
PY

echo "Snapshot saved: $OUTFILE"
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

### 2. Updated `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"

select_files_by_shard() {
  local files_json="$1"
  python3 - "$files_json" "$SHARD_ID" "$TOTAL_SHARDS" <<'PY'
import json, sys, hashlib
files = json.load(sys.stdin)
shard = int(sys.argv[2])
total = int(sys.argv[3])

selected = []
for f in files:
    # Deterministic shard assignment by filename
    h = int(hashlib.md5(f["path"].encode()).hexdigest(), 16)
    if h % total == shard:
        selected.append(f["path"])

for p in selected:
    print(p)
PY
}

if [[ -n "$SNAPSHOT_FILE" && -f "$SNAPSHOT_FILE" ]]; then
  echo "Using snapshot: $SNAPSHOT_FILE"
  FILES=$(python3 -c "import json; print(json.dumps(json.load(open('$SNAPSHOT_FILE'))['files']))")
  mapfile -t FILE_LIST < <(select_files_by_shard "$FILES")
else
  echo "No snapshot, falling back to live API (rate-limited)"
  # ... existing list_repo_files logic ...
fi

# Download via CDN URLs
for rel_path in "${FILE_LIST[@]}"; do
  url="https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/${rel_path}"
  echo "Processing: $url"
  # Use lib/cdn_download.py or curl with retries; validate sha256 if present
done
```

---

### 3. `lib/cdn_download.py`
```python
#!/usr/bin/env python3
import hashlib
import json
import requests
from pathlib import Path
from typing import Iterator, Tuple, Dict, Any

RETRY = 3
CHUNK = 8192

def _download(url: str, dest: Path, expected_sha256: str = None) -> Path:
    for attempt in range(RETRY):
        try:
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    hasher = hashlib.sha256()
                    for chunk in r.iter_content(chunk_size=CHUNK):
                        f.write(chunk)
                        hasher.update(chunk)
                if expected_sha256 and hasher.hexdigest() != expected_sha256:
                    dest.unlink(missing_ok=True)
                    raise ValueError("sha256 mismatch")
                return dest
        except Exception as e:
            if attempt == RETRY - 1:
                raise
            dest.unlink(missing_ok=True)
    raise RuntimeError("unreachable")

def iter_cdn_files(
    snapshot_path: str,
    shard_id: int,
    total_shards: int,
    cache_dir: str = ".cdn_cache"
) -> Iterator[Tuple[Path, Dict[str, Any]]]:
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    with open(snapshot_path) as f:
        snap = json.load(f)

    repo = snap["repo"]
    files = snap["files"]

    selected = []
    for f in files:
        h = int(hashlib.md5(f["path"].encode()).hexdigest(), 16)
        if h % total_shards == shard_id:
            selected.append(f)

    for meta in selected:
        url = f"https://huggingface.co/datasets/{repo}/resolve/main/{meta['path']}"
        dest = cache / Path(meta["path"]).name
        if not dest.is_file():
            _download(url, dest, meta.get("sha256"))


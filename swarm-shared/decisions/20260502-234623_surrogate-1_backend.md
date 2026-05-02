# surrogate-1 / backend

## Final Implementation Plan (≤2 h)

**Highest-value improvement**  
Add a **pre-flight snapshot generator** (`bin/snapshot.sh` + `lib/snapshot.py`) that lists the target date folder once, produces a deterministic file manifest, and enables **CDN-only downloads** during parallel shard processing. This eliminates recursive `list_repo_files`/`list_repo_tree` calls from 16 concurrent runners, removes pagination overhead, and prevents HF API 429s during ingestion.

---

### Concrete plan (≤2 h)

1. **Add `bin/snapshot.sh`**  
   - Runs once per date folder (e.g., `public-raw/2026-05-02`).  
   - Uses `huggingface_hub.HfApi.list_repo_tree(..., recursive=False)` to list the folder and its immediate subfolders (one-level deep).  
   - Outputs `snapshot/<date>/files.json`:
     ```json
     {
       "repo": "...",
       "date_folder": "...",
       "cdn_base": "https://huggingface.co/datasets/.../resolve/main",
       "files": ["public-raw/2026-05-02/...", ...]
     }
     ```
   - Commits or uploads as a workflow artifact for reuse by all shards.

2. **Add `lib/snapshot.py`** (single helper)  
   - `generate_snapshot(repo_id, date_folder, out_path)` with one-level recursion only.  
   - Validates file extensions (`.parquet`, `.jsonl`) and logs counts.  
   - Uses pagination (page size 100) defensively but only on the targeted folder.

3. **Update `bin/dataset-enrich.sh`**  
   - Accept `SNAPSHOT_FILE` env var.  
   - If present, read file list and `cdn_base` from snapshot; **skip all HF API list calls**.  
   - Build download URLs as:  
     `${cdn_base}/${rel_path}`  
   - Keep deterministic hash-bucket shard assignment unchanged.

4. **Add `lib/cdn_stream.py`**  
   - Lightweight streaming fetch via `requests` (no auth) with retries/backoff.  
   - Supports `.parquet` (via `pyarrow.parquet`) and `.jsonl` (line-by-line) and projects to `{prompt, response}`.  
   - Exposes `stream_cdn_file(url, projection_fn)` to minimize memory per shard.

5. **Update GitHub Actions matrix**  
   - Add a `snapshot` job that:  
     - Runs `bin/snapshot.sh` once per date folder.  
     - Uploads `snapshot/<date>/files.json` as an artifact.  
   - Shard jobs depend on `snapshot`, download the artifact, and set `SNAPSHOT_FILE`.  
   - Shards run with `HF_API_CALLS=0` guard to enforce no list/metadata calls.

6. **Update README snippet**  
   - Document manual snapshot generation and shard consumption.  
   - Note CDN bypass, rate-limit avoidance, and deterministic shard assignment.

---

### Resolved contradictions (correctness + actionability)

- **Scope of recursion**: Candidate 1 proposed one-level recursion; Candidate 2 was ambiguous. We adopt **one-level recursion only** (date folder → subfolders) to avoid deep paginated walks while still capturing all shard files.
- **Fallback behavior**: Candidate 1 included a fallback to API listing in `dataset-enrich.sh`. We **remove that fallback inside the matrix**; if the snapshot is missing, the job fails fast (prevents accidental 429s). Manual runs may still allow fallback with a warning.
- **Streaming helper**: Candidate 2 proposed `lib/cdn_stream.py`; Candidate 1 omitted it. We include it for **memory-efficient, auth-free ingestion** and to make CDN usage concrete and testable.
- **Artifact passing**: Candidate 1 suggested `needs.snapshot.outputs`; Candidate 2 said “pass artifact.” We use **artifact upload/download** (simpler, works across runners) and inject path via env.

---

### Code snippets

#### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT_DIR="snapshot/${DATE}"
OUT_FILE="${OUT_DIR}/files.json"

mkdir -p "${OUT_DIR}"

echo "Generating snapshot for ${REPO} -> ${DATE}"

python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi

repo = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
date_folder = os.getenv("DATE", "${DATE}")
out_path = os.getenv("OUT_PATH", "${OUT_FILE}")

api = HfApi()
prefix = f"public-raw/{date_folder}"

all_files = []
# One-level deep: folder -> subfolders/files (no deep recursion)
for obj in api.list_repo_tree(repo, path=prefix, recursive=False):
    if obj.type == "directory":
        for sub in api.list_repo_tree(repo, path=obj.path, recursive=False):
            if sub.type == "file":
                all_files.append(sub.path)
    elif obj.type == "file":
        all_files.append(obj.path)

# Basic validation
for f in all_files:
    if not (f.endswith(".parquet") or f.endswith(".jsonl")):
        print(f"WARNING: unexpected file type: {f}", file=sys.stderr)

result = {
    "repo": repo,
    "date_folder": date_folder,
    "cdn_base": f"https://huggingface.co/datasets/{repo}/resolve/main",
    "files": sorted(all_files),
}

os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w") as f:
    json.dump(result, f, indent=2)

print(f"Wrote {len(all_files)} files to {out_path}")
PY

echo "Snapshot complete: ${OUT_FILE}"
```

#### `lib/snapshot.py`
```python
import json
from pathlib import Path
from huggingface_hub import HfApi

def generate_snapshot(repo_id: str, date_folder: str, out_path: Path):
    api = HfApi()
    prefix = f"public-raw/{date_folder}"
    files = []

    for obj in api.list_repo_tree(repo_id, path=prefix, recursive=False):
        if obj.type == "directory":
            for sub in api.list_repo_tree(repo_id, path=obj.path, recursive=False):
                if sub.type == "file":
                    files.append(sub.path)
        elif obj.type == "file":
            files.append(obj.path)

    result = {
        "repo": repo_id,
        "date_folder": date_folder,
        "cdn_base": f"https://huggingface.co/datasets/{repo_id}/resolve/main",
        "files": sorted(files),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))
    return result
```

#### `lib/cdn_stream.py`
```python
import io
import requests
import pyarrow.parquet as pq

def stream_cdn_file(url: str, projection_fn):
    """
    Stream a file from CDN and yield projected records.
    Supports .parquet and .jsonl by extension.
    projection_fn: bytes | dict -> iterable of {prompt, response}
    """
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()

    if url.endswith(".parquet"):
        # Stream into pyarrow (memory-efficient for columnar projection)
        table = pq.read_table(io.BytesIO(resp.content))
        for batch in table.to_batches(max_chunksize=1024):
            for record in projection_fn(batch):
                yield record
    elif url.endswith(".jsonl"):
        for line in resp.iter_lines(decode_unicode=True):
            if line:
                for record in projection_fn(line):
                    yield record
    else:
        raise ValueError(f"Unsupported file type: {url}")
```

#### Updated `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"


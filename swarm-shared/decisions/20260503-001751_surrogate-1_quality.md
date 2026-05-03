# surrogate-1 / quality

## Final Implementation Plan — CDN-only, snapshot-driven data loading for surrogate-1

**Highest-value improvement**: Add `bin/snapshot.sh` that produces a deterministic, versioned file manifest for a date folder. Training and ingestion use that manifest and CDN-only fetches (`resolve/main/...`) to eliminate recursive `list_repo_tree`/`list_repo_files` calls and avoid HF API 429s during training.

### Why this matters
- Removes recursive and repeated HF API listing calls during training and ingestion.
- Enables Lightning Studio and any runner to load data via CDN only (zero API calls during data fetch).
- Small, safe change (<2h): one script + minor updates to ingestion and training entrypoints with backward-compatible fallbacks.

---

### Concrete changes

1. **Add `bin/snapshot.sh`**
   - Inputs: `REPO_ID`, `DATE_PATH` (e.g. `2026-04-29`), optional `OUT_JSON`.
   - Uses `huggingface_hub` to call `list_repo_tree(..., recursive=False)` once for the date folder.
   - Emits deterministic JSON manifest:
     - `repo_id`, `date_path`, `generated_at` (ISO8601 UTC), `count`, `files: [{path, size, sha, lfs?}]`.
     - Sorted by path for stability.
   - Prints JSON to stdout; writes to file if requested.
   - Exits non-zero on failure.

2. **Update `bin/dataset-enrich.sh`**
   - Accept optional `SNAPSHOT_FILE` env var.
   - If present and valid, read file list from manifest; otherwise fall back to live `list_repo_tree` (non-recursive).
   - Keep existing per-shard behavior otherwise.

3. **Update training entrypoint / datamodule**
   - Accept `--snapshot` path.
   - Build file list from snapshot.
   - Fetch via CDN (`https://huggingface.co/datasets/<repo_id>/resolve/main/<path>`) or `hf_hub_download`.
   - Do not use recursive listing or `load_dataset(streaming=True)` on heterogeneous repos during training.
   - Validate file existence/size where cheap; skip corrupt files with warning.

4. **Workflow integration (`.github/workflows/ingest.yml`)**
   - Optional pre-step to generate snapshot for the current date and upload as artifact.
   - Pass snapshot to matrix shards via `env.SNAPSHOT_FILE`.
   - Shards download artifact and use snapshot; fallback to live listing if missing.

---

### Code snippets (merged + hardened)

#### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Generate deterministic file manifest for a dataset repo/date folder.
# Usage:
#   HF_TOKEN=<token> ./bin/snapshot.sh <repo_id> <date_path> [output.json]
# Example:
#   HF_TOKEN=$HF_TOKEN ./bin/snapshot.sh datasets/axentx/surrogate-1-training-pairs 2026-04-29 snapshot-2026-04-29.json

set -euo pipefail

REPO_ID="${1:-datasets/axentx/surrogate-1-training-pairs}"
DATE_PATH="${2:-$(date +%Y-%m-%d)}"
OUTPUT="${3:-}"

exec_python() {
  python3 - "$REPO_ID" "$DATE_PATH" "$OUTPUT" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

def main():
    if len(sys.argv) != 4:
        print("Usage: snapshot.py <repo_id> <date_path> <output|->", file=sys.stderr)
        sys.exit(1)
    repo_id = sys.argv[1]
    date_path = sys.argv[2].lstrip("/")
    output = sys.argv[3]

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    entries = api.list_repo_tree(repo_id=repo_id, path=date_path, repo_type="dataset", recursive=False)

    files = []
    for e in sorted(entries, key=lambda x: x.path):
        if e.path.endswith("/"):
            continue
        files.append({
            "path": e.path,
            "size": getattr(e, "size", None),
            "lfs": getattr(e, "lfs", None),
            "sha": getattr(e, "sha", None),
        })

    manifest = {
        "repo_id": repo_id,
        "date_path": date_path,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(files),
        "files": files,
    }

    out = json.dumps(manifest, indent=2, sort_keys=True)
    if output == "-" or not output:
        print(out)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(output)) if os.path.dirname(output) else ".", exist_ok=True)
        with open(output, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"Wrote {len(files)} files to {output}", file=sys.stderr)

if __name__ == "__main__":
    main()
PY
}

exec_python
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

#### Update `bin/dataset-enrich.sh` (minimal diff)
```bash
# Near top, after defaults
SNAPSHOT_FILE="${SNAPSHOT_FILE:-}"

if [ -n "$SNAPSHOT_FILE" ] && [ -f "$SNAPSHOT_FILE" ]; then
  echo "Using snapshot: $SNAPSHOT_FILE"
  # Extract file paths (newline-separated)
  FILES=$(python3 -c "
import json, sys
manifest = json.load(open(sys.argv[1]))
for f in manifest.get('files', []):
    print(f['path'])
" "$SNAPSHOT_FILE")
else
  echo "No snapshot provided; listing repo tree (non-recursive) for $DATE_FOLDER"
  FILES=$(python3 -c "
from huggingface_hub import HfApi
import os
api = HfApi(token=os.environ.get('HF_TOKEN'))
entries = api.list_repo_tree(repo_id='$REPO_ID', path='$DATE_FOLDER', repo_type='dataset', recursive=False)
for e in sorted(entries, key=lambda x: x.path):
    if not e.path.endswith('/'):
        print(e.path)
  ")
fi
```

---

#### Training loader snippet (CDN-first, snapshot-aware)
```python
# train.py or datamodule
import json
import os
from pathlib import Path
from typing import Iterator, Dict

from huggingface_hub import hf_hub_download
import pyarrow.parquet as pq

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo_id}/resolve/main/{path}"

def load_files_from_snapshot(
    snapshot_path: str,
    repo_id: str,
    use_cdn: bool = True,
    columns=("prompt", "response"),
) -> Iterator[Dict[str, str]]:
    with open(snapshot_path) as f:
        manifest = json.load(f)

    for item in manifest.get("files", []):
        rel_path = item["path"]
        if not rel_path.endswith(".parquet"):
            continue

        if use_cdn:
            # CDN fetch (fast, no API calls)
            url = CDN_TEMPLATE.format(repo_id=repo_id, path=rel_path)
            # For simple CDN download you can use requests or wget; here we use hf_hub_download
            # which will cache and can fallback to CDN internally.
            local_path = hf_hub_download(repo_id=repo_id, filename=rel_path, repo_type="dataset")
        else:
            local_path = hf_hub_download(repo_id=repo_id, filename=rel_path, repo_type="dataset")

        try:
            table = pq.read_table(local_path, columns=list(columns))
        except Exception as exc:
            print(f"Skipping {rel_path} due to error: {exc}")
            continue

        for batch in table.to_batches(max_chunksize=1024):
            prompts = batch.column("prompt").to_pylist()
            responses = batch.column("response").to_pylist()
            for p, r in zip(prompts, responses):
                if p is not None and r is not None:
                    yield {"prompt": p, "response": r}
```

---

#### Workflow snippet (`.github/workflows/ingest.yml`) — optional pre-step
```yaml
jobs:

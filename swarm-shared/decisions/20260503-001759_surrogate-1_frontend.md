# surrogate-1 / frontend

## Final Implementation Plan — CDN-first snapshot generator for surrogate-1

**Single highest-value improvement**: Add a deterministic `bin/snapshot.sh` that lists dataset files once per date folder and emits a reproducible manifest. Training and ingestion will use CDN-only fetches (`resolve/main/...`) and never call `list_repo_tree`/`list_repo_files` during data loading, eliminating HF API 429 risk and enabling reproducible runs.

---

### Why this matters (merged rationale)
- **Correctness**: Removes recursive/tree listing during training (prevents 429s and nondeterministic pagination).
- **Reproducibility**: Same manifest → same data order and contents across runs.
- **Actionability**: Fits <2h (single script + small runner/training updates).
- **Performance**: Enables Lightning Studio streaming with zero HF API calls; CDN fetches are public and unthrottled for public datasets.

---

### Concrete changes (merged + resolved)

1. **Create `bin/snapshot.sh`** (new)
   - Accepts `REPO`, `DATE_FOLDER`, `OUT_JSON`, optional `SUBPATH`.
   - Uses `huggingface_hub` to call `list_repo_tree(path=..., recursive=False)` once.
   - Emits deterministic JSON with sorted file entries and metadata.
   - Exits non-zero on API errors; logs clearly to stderr.

2. **Create `bin/lib/snapshot.py`** (new)
   - Shared helper used by `snapshot.sh`.
   - Produces canonical JSON (sorted keys, sorted file list).
   - Captures `path`, `type`, `size`, and `lfs` metadata.

3. **Update `bin/dataset-enrich.sh`** (modify)
   - Add optional `--snapshot FILE` mode to skip live listing and use the manifest.
   - If no snapshot provided, retain existing behavior (backward compatibility).

4. **Update `.github/workflows/ingest.yml`** (modify)
   - Add a snapshot job (or step) that runs before the matrix and uploads the manifest as an artifact.
   - Matrix shards download the artifact and use the manifest to know which files to process (no per-shard API calls).

5. **Update README** (add)
   - Explain snapshot usage and the CDN-only training pattern.
   - Provide example training snippet using CDN fetches.

---

### Resolved contradictions (in favor of correctness + actionability)

- **Metadata fields**: Use `path`, `type`, `size`, `lfs` (from Candidate 1) + optional `sha` if available (Candidate 2). `size` and `lfs` are most actionable for prefetch checks; `sha` is nice-to-have but not required for CDN fetches.
- **Output shape**: Emit a top-level object with `repo`, `folder`, `created_at`, `files[]`, `count` (Candidate 1). This is more self-describing than a bare array and supports future extension without breaking parsers.
- **Determinism**: Sort files by `path` and use `sort_keys=True` in JSON output (both candidates). This guarantees byte-for-byte reproducibility.
- **Training fetch method**: Use CDN-only (`resolve/main/...`) with no Authorization header for public datasets (both candidates). Do not embed API tokens or call `list_repo_files` during training.
- **Runner integration**: Prefer a snapshot job + artifact in CI (Candidate 1) because it cleanly separates snapshot creation from shard processing and avoids per-shard API calls. Candidate 2’s lighter touch is retained as optional local usage via `--snapshot`.

---

### Final code and config

#### `bin/lib/snapshot.py`
```python
#!/usr/bin/env python3
"""
Generate a deterministic snapshot of dataset files for a date folder.
Usage:
  python bin/lib/snapshot.py --repo datasets/axentx/surrogate-1-training-pairs --date 2026-04-29 --out snapshot-2026-04-29.json
"""
import argparse
import json
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi


def main() -> None:
    parser = argparse.ArgumentParser(description="Create HF dataset file snapshot for a date folder.")
    parser.add_argument("--repo", required=True, help="HF repo (e.g., datasets/owner/name)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--subpath", default="", help="Optional subpath within date folder")
    args = parser.parse_args()

    api = HfApi()
    folder_path = args.date
    if args.subpath:
        folder_path = f"{folder_path}/{args.subpath}".rstrip("/")

    try:
        entries = api.list_repo_tree(repo_id=args.repo, path=folder_path, recursive=False)
    except Exception as exc:
        print(f"ERROR: failed to list repo tree {args.repo}@{folder_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    files = []
    for entry in entries:
        files.append({
            "path": entry.path,
            "type": getattr(entry, "type", "file"),
            "size": getattr(entry, "size", None),
            "lfs": getattr(entry, "lfs", None),
        })

    files.sort(key=lambda f: f["path"])

    snapshot = {
        "repo": args.repo,
        "folder": folder_path,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "count": len(files),
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, sort_keys=True)

    print(f"Snapshot written to {args.out} ({len(files)} files)")


if __name__ == "__main__":
    main()
```

#### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# Generate a deterministic snapshot of dataset files for a date folder.
# Usage:
#   bin/snapshot.sh --repo datasets/axentx/surrogate-1-training-pairs --date 2026-04-29 --out snapshot-2026-04-29.json
set -euo pipefail

REPO=""
DATE=""
OUT=""
SUBPATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)     REPO="$2";   shift 2 ;;
    --date)     DATE="$2";   shift 2 ;;
    --out)      OUT="$2";    shift 2 ;;
    --subpath)  SUBPATH="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$REPO" || -z "$DATE" || -z "$OUT" ]]; then
  echo "Usage: $0 --repo <repo> --date <YYYY-MM-DD> --out <out.json> [--subpath <subpath>]" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_PY="${SCRIPT_DIR}/lib/snapshot.py"

if [[ ! -f "$LIB_PY" ]]; then
  echo "ERROR: missing helper $LIB_PY" >&2
  exit 1
fi

python3 "$LIB_PY" --repo "$REPO" --date "$DATE" --out "$OUT" --subpath "$SUBPATH"
```

Make executable:
```bash
chmod +x bin/snapshot.sh bin/lib/snapshot.py
```

#### Example training snippet (CDN-only)
```python
import json
import requests
from pathlib import Path

def load_snapshot(snapshot_path: str):
    with open(snapshot_path) as f:
        return json.load(f)

def cdn_fetch(repo: str, file_path: str):
    # Public CDN fetch; no auth required for public datasets
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content

# Usage in Lightning/DataModule:
# snapshot = load_snapshot("snapshot-2026-04-29.json")
# for f in snapshot["files"]:
#     raw = cdn_fetch(snapshot["repo"], f["path"])
#     # parse parquet/jsonl -> {prompt, response}
```

#### `.github/workflows/ing

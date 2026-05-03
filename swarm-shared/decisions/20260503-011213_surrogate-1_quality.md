# surrogate-1 / quality

## Final Synthesized Plan (≤2h)

**Goal**: Eliminate HF API rate-limit failures and HF Space OOM by replacing recursive `list_repo_files` and per-file API calls with **one per-folder `list_repo_tree` + CDN-only fetches**, and project to `{prompt,response}` at parse time.

**Why this wins**:
- Avoids `list_repo_files` recursive pagination (100× API calls) and per-file metadata requests.
- CDN downloads (`/resolve/main/`) bypass `/api/` auth checks and have much higher rate limits.
- Single manifest JSON lets Lightning training do zero-API data loads.
- Fits within 2h: only `bin/dataset-enrich.sh` + small Python helpers need changes.

---

## Implementation Plan (≤2h)

| Step | Time | Owner | Description |
|------|------|-------|-------------|
| 1 | 15m | Engineer | Add `bin/build-manifest.py` — takes `repo`, optional `path`, does `list_repo_tree(recursive=False)` per top-level date folder, emits `manifest-{date}.json` with `{file, size, etag, cdn_url}`. |
| 2 | 20m | Engineer | Update `bin/dataset-enrich.sh` to: 1) accept manifest file as arg, 2) shard by `slug-hash % 16 == SHARD_ID`, 3) download via `curl -L "$cdn_url"` to temp file, 4) parse with streaming pyarrow/parquet and project `{prompt,response}` only, 5) append to shard output. |
| 3 | 15m | Engineer | Add `.github/workflows/generate-manifest.yml` (manual + cron) that runs `python bin/build-manifest.py` and uploads artifact `manifest-*.json`. Main workflow fetches artifact or regenerates if missing. |
| 4 | 20m | Engineer | Update `lib/dedup.py` to remain unchanged (central md5 store) but accept projected rows; ensure no extra columns (`source`, `ts`) are written. |
| 5 | 20m | Engineer | Update `requirements.txt` if needed (`requests` for manifest fetch; already present likely). |
| 6 | 20m | QA | Smoke test: run one shard locally against a small date folder; verify no HF API calls during download, output schema `{prompt,response}`, dedup works. |
| 7 | 10m | Engineer | Update README snippet with new run pattern and CDN-bypass note. |

Total: ~2h.

---

## Code snippets

### 1) `bin/build-manifest.py`

```python
#!/usr/bin/env python3
"""
Build a CDN-only manifest for surrogate-1 dataset ingestion.
Usage:
  python bin/build-manifest.py axentx/surrogate-1-training-pairs \
    --out manifest-2026-05-03.json \
    --date-folder 2026-05-03
"""
import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", help="HF dataset repo, e.g. axentx/surrogate-1-training-pairs")
    parser.add_argument("--out", required=True, help="Output manifest JSON path")
    parser.add_argument("--date-folder", help="Top-level date folder (e.g. 2026-05-03). If omitted, uses all top-level folders.")
    parser.add_argument("--pattern", default="*.parquet", help="File pattern to include (simple glob-style, applied client-side)")
    args = parser.parse_args()

    api = HfApi()
    # Single API call per folder (non-recursive)
    entries = api.list_repo_tree(
        repo_id=args.repo,
        path=args.date_folder or "",
        recursive=False,
        repo_type="dataset",
    )

    # If no date_folder provided, we only list top-level folders (non-recursive).
    # For simplicity this script expects a date folder; CI can loop over folders.
    files = [e for e in entries if e.rfilename.endswith(".parquet")]
    if not files:
        print("No parquet files found.", file=sys.stderr)
        sys.exit(1)

    manifest = {
        "repo": args.repo,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date_folder": args.date_folder or "all",
        "files": [],
    }

    for f in files:
        cdn_url = CDN_TEMPLATE.format(repo=args.repo, path=f.rfilename)
        manifest["files"].append({
            "path": f.rfilename,
            "size": getattr(f, "size", None),
            "cdn_url": cdn_url,
        })

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) if os.path.dirname(args.out) else ".", exist_ok=True)
    with open(args.out, "w") as fp:
        json.dump(manifest, fp, indent=2)
    print(f"Wrote {len(manifest['files'])} files to {args.out}")

if __name__ == "__main__":
    main()
```

### 2) Updated `bin/dataset-enrich.sh` (core worker)

```bash
#!/usr/bin/env bash
# surrogate-1 dataset enrichment worker (CDN-bypass mode)
# Usage:
#   SHARD_ID=0 SHARD_TOTAL=16 \
#   MANIFEST=manifest-2026-05-03.json \
#   OUTPUT=shard-0-20260503-120000.jsonl \
#   ./bin/dataset-enrich.sh

set -euo pipefail
SHELL=/bin/bash

: "${SHARD_ID:?required}"
: "${SHARD_TOTAL:=16}"
: "${MANIFEST:?required}"
: "${OUTPUT:?required}"
: "${TMPDIR:=/tmp}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEDUP_PY="${SCRIPT_DIR}/lib/dedup.py"

# Python helper to project parquet -> {prompt,response}
PROJECT_PY="${SCRIPT_DIR}/lib/project_parquet.py"

# Ensure helpers exist
if [[ ! -f "${DEDUP_PY}" ]]; then
  echo "Missing dedup helper: ${DEDUP_PY}" >&2
  exit 1
fi

if [[ ! -f "${PROJECT_PY}" ]]; then
  cat > "${PROJECT_PY}" <<'PYEOF'
import pyarrow.parquet as pq
import sys
import json

def project_file(path):
    try:
        pf = pq.ParquetFile(path)
        # Try common column names; keep only prompt/response
        for batch in pf.iter_batches(batch_size=1000):
            df = batch.to_pandas()
            # Normalize column names
            col_map = {}
            for c in df.columns:
                cl = c.strip().lower()
                if "prompt" in cl:
                    col_map[c] = "prompt"
                elif "response" in cl or "completion" in cl or "answer" in cl:
                    col_map[c] = "response"
            if "prompt" not in col_map.values() or "response" not in col_map.values():
                # skip files that don't have required fields
                continue
            df = df.rename(columns={k: v for k, v in col_map.items() if v in ("prompt", "response")})
            # Keep only prompt/response and drop rows with nulls
            df = df[["prompt", "response"]].dropna(subset=["prompt", "response"])
            for _, row in df.iterrows():
                print(json.dumps({"prompt": str(row["prompt"]), "response": str(row["response"])}, ensure_ascii=False))
    except Exception as e:
        # Silently skip malformed files; log to stderr for debugging
        print(f"Skip {path}: {e}", file=sys.stderr)

if __name__ == "__main__":
    for line in sys.stdin:
        project_file(line.strip())
PYEOF
fi

# Parse manifest and select shard files by deterministic hash
# We hash the filename (or path) to assign to shard.
mapfile -t ALL_FILES < <(python3 -c "
import json, sys, hashlib
manifest = json.load(open(sys.argv[1]))
for f in

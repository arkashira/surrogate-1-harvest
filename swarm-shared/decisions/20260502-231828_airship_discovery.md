# airship / discovery

## Highest-Value Incremental Improvement (≤2h)

**Goal**: Harden `airship discover` into a deterministic, CDN-only orchestrator that eliminates HF API rate limits and PyArrow schema errors while producing reproducible file lists and safe ingestion artifacts.

**Why this ships fastest**:
- No new infra — uses existing HF CDN + local orchestration.
- One focused script + one config change.
- Immediately removes two recurring failure modes (429s, CastError).
- Fits the established pattern: Mac orchestrates, remote trains; CDN bypasses API limits.

---

## Implementation Plan

### 1. Create `scripts/discover_cdn_only.sh`
- Shebang: `#!/usr/bin/env bash`
- Make executable: `chmod +x scripts/discover_cdn_only.sh`
- Responsibilities:
  - Accept `REPO`, `DATE`, `OUT_DIR` env vars or args.
  - Run **one** `list_repo_tree` call (non-recursive) for the target date folder.
  - Save file list to `file_list.json` (deterministic, reproducible).
  - Generate `ingest_manifest.json` with CDN URLs and expected slugs.
  - Emit safe `batches/mirror-merged/{date}/{slug}.parquet` filenames (no extra cols).
  - Exit non-zero on any failure; log verbosely to `logs/discover.log`.

### 2. Add `.env.defaults` for discovery
```
HF_REPO=datasets/your-org/your-repo
DISCOVER_DATE=2026-05-02
OUT_DIR=./artifacts/discover
HF_API_WAIT=360
```

### 3. Update `scripts/ingest_safe.py` (lightweight)
- Accept `ingest_manifest.json`.
- Use `hf_hub_download` per file (or direct CDN fetch) — **never** `load_dataset(streaming=True)` on heterogeneous repo.
- Project to `{prompt, response}` only at parse time.
- Write to `batches/mirror-merged/{date}/{slug}.parquet` with no `source`/`ts` columns.
- Deterministic slug → repo shard mapping (hash mod N) to respect HF commit cap.

### 4. Add cron-safe invocation template
- Ensure `SHELL=/bin/bash` in crontab.
- Example crontab line:
  ```
  SHELL=/bin/bash
  0 2 * * * cd /opt/axentx/airship && ./scripts/discover_cdn_only.sh >> logs/discover.log 2>&1
  ```

### 5. Verification checklist (run after implementation)
- `file_list.json` exists and is non-empty.
- All URLs in `ingest_manifest.json` are valid CDN URLs (`resolve/main/...`).
- No `load_dataset(streaming=True)` in ingestion path.
- Ingested parquet has only `prompt`, `response` (and maybe `id`).
- Script exits 0 on success, non-zero on failure.

---

## Code Snippets

### `scripts/discover_cdn_only.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

# Airship Discovery — CDN-only deterministic file listing
# Usage: REPO=datasets/org/repo DATE=2026-05-02 OUT_DIR=./artifacts ./discover_cdn_only.sh

REPO="${REPO:-datasets/axentx/arkship}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
OUT_DIR="${OUT_DIR:-./artifacts/discover}"
HF_TOKEN="${HF_TOKEN:-}"
LOG_FILE="logs/discover.log"

mkdir -p "$OUT_DIR" "$(dirname "$LOG_FILE")"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Discover: repo=$REPO date=$DATE out=$OUT_DIR"

# One API call: list top-level of date folder (non-recursive)
# Uses huggingface_hub via Python to avoid CLI pagination/rate issues
python3 - "$REPO" "$DATE" "$OUT_DIR" <<'PY'
import os, json, sys
from huggingface_hub import HfApi

repo = sys.argv[1]
date = sys.argv[2]
out_dir = sys.argv[3]
api = HfApi(token=os.getenv("HF_TOKEN") or None)

# Non-recursive list for the date folder
items = api.list_repo_tree(repo=repo, path=date, recursive=False)
files = [f.rfilename for f in items if f.rfilename.startswith(date + "/") and not f.rfilename.endswith("/")]

file_list_path = os.path.join(out_dir, "file_list.json")
with open(file_list_path, "w") as f:
    json.dump(files, f, indent=2, sort_keys=True)

# Build manifest with CDN URLs (no auth, bypasses API rate limits)
manifest = []
for fp in sorted(files):
    cdn_url = f"https://huggingface.co/datasets/{repo.removeprefix('datasets/')}/resolve/main/{fp}"
    slug = fp.replace("/", "_").replace(".parquet", "").replace(".jsonl", "")
    manifest.append({
        "path": fp,
        "cdn_url": cdn_url,
        "slug": slug,
        "date": date
    })

manifest_path = os.path.join(out_dir, "ingest_manifest.json")
with open(manifest_path, "w") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)

print(f"Discovered {len(files)} files -> {file_list_path}, {manifest_path}")
PY

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Done."
```

### `scripts/ingest_safe.py` (minimal, safe ingestion)
```python
#!/usr/bin/env python3
"""
Safe ingestion: CDN-only, no streaming dataset, project to {prompt,response} only.
Usage: python ingest_safe.py --manifest artifacts/discover/ingest_manifest.json --out-dir batches/mirror-merged
"""
import argparse, json, os, hashlib, pyarrow.parquet as pq
from pathlib import Path
from huggingface_hub import hf_hub_download
import pandas as pd

def shard_repo(slug: str, n_shards: int = 5) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % n_shards

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out-dir", default="batches/mirror-merged")
    ap.add_argument("--n-shards", type=int, default=5)
    args = ap.parse_args()

    with open(args.manifest) as f:
        manifest = json.load(f)

    os.makedirs(args.out_dir, exist_ok=True)
    date = manifest[0]["date"] if manifest else "unknown"
    date_dir = Path(args.out_dir) / date
    date_dir.mkdir(parents=True, exist_ok=True)

    for item in manifest:
        slug = item["slug"]
        # Determine target sibling repo by shard (HF commit cap mitigation)
        shard = shard_repo(slug, args.n_shards)
        target_repo = f"datasets/axentx/arkship-mirror-{shard}"

        # Download via hf_hub_download (or direct CDN) — avoid load_dataset(streaming=True)
        local_path = hf_hub_download(
            repo_id=target_repo,
            filename=item["path"],
            repo_type="dataset",
            force_download=False,
        )
        # If file not in target repo, fallback to CDN direct (public)
        if not os.path.exists(local_path):
            import urllib.request, tempfile
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".parquet")
            urllib.request.urlretrieve(item["cdn_url"], tmp.name)
            local_path = tmp.name

        # Read and project to {prompt, response} only
        df = pd.read_parquet(local_path)
        # Keep only expected cols; rename if necessary
        keep = [c for c in df.columns if c.lower() in {"prompt", "response", "text", "completion"}]
        if not keep:
            raise ValueError(f"No prompt/response column in {local_path}: {df.columns.tolist()}")
        df = df[keep].rename(columns={c: c.lower() for c in keep})
        # Normalize to prompt/response
        if "text" in df.columns and "prompt" not in df.columns:
            df = df.rename(

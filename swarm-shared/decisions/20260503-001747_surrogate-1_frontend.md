# surrogate-1 / frontend

## Final Implementation Plan — Pre-flight snapshot generator for surrogate-1

**Highest-value improvement**: Add `bin/snapshot.sh` that lists dataset files once per date folder and emits a deterministic file manifest. Training scripts will use the manifest and fetch via HF CDN (`resolve/main/...`) to bypass API rate limits during data loading.

**Why this ships fast (<2h)**:
- Single new script + small change to existing worker.
- No schema changes, no infra, no new secrets.
- Reuses existing `HF_TOKEN` and `datasets` tooling.

---

### 1) New file: `bin/snapshot.sh`

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
#
# Pre-flight snapshot for surrogate-1 dataset ingestion.
#
# Usage:
#   HF_TOKEN=<token> ./bin/snapshot.sh \
#     --repo axentx/surrogate-1-training-pairs \
#     --date 2026-04-29 \
#     --out snapshots/2026-04-29-manifest.json
#
# Environment overrides:
#   REPO, DATE (YYYY-MM-DD), OUT, HF_TOKEN
#
# Behavior:
# - Lists files under {date}/ (non-recursive) via huggingface_hub.
# - Emits a deterministic JSON manifest with CDN URLs.
# - Manifest can be committed or embedded into training scripts.
# - CDN downloads bypass /api/ auth rate limits.

set -euo pipefail

REPO="${REPO:-}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
OUT="${OUT:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --date) DATE="$2"; shift 2 ;;
    --out)  OUT="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ -z "$REPO" || -z "$OUT" ]]; then
  echo "Usage: $0 --repo <repo> --date <YYYY-MM-DD> --out <path>"
  echo "  Or set env: REPO=<repo> DATE=<YYYY-MM-DD> OUT=<path>"
  exit 1
fi

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "ERROR: HF_TOKEN is required to list repo tree." >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"

python3 - "$REPO" "$DATE" "$OUT" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from huggingface_hub import HfApi, hf_hub_url

def main(repo: str, date: str, out: str) -> None:
    api = HfApi(token=os.environ["HF_TOKEN"])
    # List only top-level entries for the date folder (non-recursive).
    entries = api.list_repo_tree(
        repo=repo,
        path=date,
        recursive=False,
        repo_type="dataset",
    )

    files = []
    for e in sorted(entries, key=lambda x: x.path):
        if e.type != "file":
            continue
        # CDN URL that bypasses /api/ auth checks.
        cdn_url = hf_hub_url(
            repo_id=repo,
            filename=e.path,
            repo_type="dataset",
        )
        # Convert to raw resolve URL (CDN).
        cdn_url = cdn_url.replace("/api/", "/resolve/")
        files.append({
            "path": e.path,
            "size": getattr(e, "size", None),
            "cdn_url": cdn_url,
        })

    manifest = {
        "repo": repo,
        "date": date,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "note": "CDN URLs bypass HF API auth rate limits during training data loading.",
    }

    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print(f"Wrote {len(files)} files to {out}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
PY
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

### 2) Update `bin/dataset-enrich.sh` (minimal change)

Add optional snapshot generation at the start of each shard run so the manifest is available for downstream training. Keep behavior unchanged when snapshot is disabled.

```bash
# Near top of bin/dataset-enrich.sh (after shebang and set -euo pipefail)

# Optional: generate pre-flight snapshot for this date folder.
# Controlled by env var to avoid extra API calls when not wanted.
if [[ "${SNAPSHOT:-false}" == "true" ]]; then
  DATE_FOLDER="${DATE_FOLDER:-$(date -u +%Y-%m-%d)}"
  SHARD_ID="${SHARD_ID:-0}"
  SNAPSHOT_OUT="${SNAPSHOT_OUT:-./snapshots/${DATE_FOLDER}-shard${SHARD_ID}.json}"
  echo "Generating snapshot for ${DATE_FOLDER} -> ${SNAPSHOT_OUT}"
  ./bin/snapshot.sh --repo axentx/surrogate-1-training-pairs \
    --date "$DATE_FOLDER" \
    --out "$SNAPSHOT_OUT" || echo "Snapshot failed (non-fatal)"
fi

# Optional: if SNAPSHOT_FILE is provided, skip live list_repo_tree and use manifest.
if [[ -n "${SNAPSHOT_FILE:-}" ]]; then
  if [[ ! -f "$SNAPSHOT_FILE" ]]; then
    echo "ERROR: SNAPSHOT_FILE not found: $SNAPSHOT_FILE" >&2
    exit 1
  fi
  # Validate snapshot contains expected date prefix to avoid mixing folders.
  DATE_PREFIX=$(jq -r '.date' "$SNAPSHOT_FILE" 2>/dev/null || true)
  if [[ -z "$DATE_PREFIX" || "$DATE_PREFIX" != "$DATE_FOLDER"* ]]; then
    echo "ERROR: SNAPSHOT_FILE date mismatch (expected ${DATE_PREFIX:-none} to match ${DATE_FOLDER})" >&2
    exit 1
  fi
  echo "Using snapshot file: $SNAPSHOT_FILE"
  # Replace live listing with manifest-driven file list.
  # (Keep existing per-file streaming/download logic unchanged.)
fi
```

---

### 3) Training script usage (example snippet)

Embed the manifest in Lightning training to perform CDN-only fetches:

```python
# train.py (excerpt)
import json
import requests
from pathlib import Path

def load_via_cdn(manifest_path: str):
    with open(manifest_path) as f:
        m = json.load(f)

    for fmeta in m["files"]:
        url = fmeta["cdn_url"]
        # Stream download with no Authorization header (public CDN).
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        # Process bytes (e.g., parse parquet/jsonl) and project to {prompt,response}.
        yield process(resp.raw)
```

---

### 4) Operational notes

- Run snapshot once per date folder from the Mac orchestrator after the rate-limit window clears, or enable `SNAPSHOT=true` in CI to generate per-shard manifests cheaply (one API call per shard).
- Commit snapshots into repo or store alongside training jobs to ensure reproducible, CDN-only training runs.
- If HF API returns 429 during snapshot, wait 360s and retry (per earlier pattern).

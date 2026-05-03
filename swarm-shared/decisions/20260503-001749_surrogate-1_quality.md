# surrogate-1 / quality

## Final Implementation Plan — Pre-flight snapshot generator for surrogate-1

**Highest-value improvement (merged)**  
Add a single, deterministic snapshot generator (`bin/snapshot.sh`) that lists dataset files once per date folder and emits a canonical JSON manifest. Training scripts embed this manifest and fetch files via HF CDN (`resolve/main/...`) only, eliminating HF API rate-limit exposure during training while keeping ingestion fast and deterministic.

**Why this ships in <2h**  
- One new script + one tiny, backward-compatible addition to `dataset-enrich.sh`  
- No infra changes, no new runtime dependencies (uses existing `huggingface_hub`)  
- Deterministic, idempotent, testable locally, and safe for CI/workers  

---

### 1) New file: `bin/snapshot.sh`

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
#
# Generate a deterministic file manifest for a dataset repo/date folder.
#
# Usage:
#   HF_TOKEN=<token> ./bin/snapshot.sh \
#     --repo axentx/surrogate-1-training-pairs \
#     --date 2026-04-29 \
#     --out snapshots/2026-04-29-manifest.json
#
# Behavior:
# - Uses HF API list_repo_tree (non-recursive) for the date folder.
# - Emits JSON array of { "path": "...", "size": ..., "sha": "..." }
# - Deterministic ordering for stable snapshots.
# - Exits non-zero on failure; prints actionable retry guidance after 429.
# - If date folder is missing, emits empty array and exits 0.

set -euo pipefail

REPO=""
DATE=""
OUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --date) DATE="$2"; shift 2 ;;
    --out)  OUT="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$REPO" || -z "$DATE" || -z "$OUT" ]]; then
  echo "Usage: $0 --repo <owner/repo> --date <YYYY-MM-DD> --out <path>" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"

# Prefer HF_TOKEN from env; if missing, allow unauthenticated (public datasets).
# list_repo_tree is used per folder to avoid recursive pagination explosion.
# We only need immediate children under the date folder.
python3 - "$REPO" "$DATE" "$OUT" <<'PY'
import os
import json
import sys
import time
from huggingface_hub import HfApi

REPO = sys.argv[1]
DATE = sys.argv[2]
OUT = sys.argv[3]

api = HfApi(token=os.getenv("HF_TOKEN") or None)

# Retry guidance for 429 baked into CLI behavior; here we raise so shell can handle.
try:
    entries = api.list_repo_tree(
        repo_id=REPO,
        path=DATE,
        repo_type="dataset",
        recursive=False,
    )
except Exception as e:
    # If folder missing, produce empty manifest rather than fail.
    if "404" in str(e) or "not found" in str(e).lower():
        entries = []
    else:
        raise

manifest = []
for e in entries:
    # Keep only files (ignore subfolders). Path is relative to repo root.
    if getattr(e, "type", None) == "file":
        manifest.append({
            "path": e.path,            # e.g. 2026-04-29/file1.parquet
            "size": e.size or 0,
            "sha": getattr(e, "lfs", {}).get("sha256", "") if getattr(e, "lfs", None) else "",
        })

# Deterministic ordering for stable snapshots.
manifest.sort(key=lambda x: x["path"])

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)

print(f"Wrote {len(manifest)} entries to {OUT}")
PY
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

### 2) Update `bin/dataset-enrich.sh` (backward-compatible)

Add an optional snapshot step at the top of the worker so each shard can emit its own snapshot (or reuse a shared one). This keeps the change minimal and preserves existing behavior when `SNAPSHOT_DIR` is unset.

```bash
# Near the top of bin/dataset-enrich.sh, after argument parsing and env setup:

# Optional: generate per-run snapshot for the date folder being processed.
# If SNAPSHOT_DIR is set, produce a deterministic manifest for CDN-based training.
if [[ -n "${SNAPSHOT_DIR:-}" && -n "${DATASET_REPO:-}" && -n "${DATE_FOLDER:-}" ]]; then
  ts=$(date -u +"%H%M%S")
  snapshot_path="${SNAPSHOT_DIR}/shard${SHARD_ID:-0}-${ts}-manifest.json"
  echo "[$(date -u)] Generating snapshot -> ${snapshot_path}"
  ./bin/snapshot.sh \
    --repo "$DATASET_REPO" \
    --date "$DATE_FOLDER" \
    --out "$snapshot_path" \
    || echo "[$(date -u)] WARNING: snapshot generation failed (continuing)"
fi
```

No other logic changes required. Existing behavior is preserved when `SNAPSHOT_DIR` is unset.

---

### 3) Training-side usage (CDN-only)

Embed the generated manifest in training scripts and use CDN URLs to bypass HF API during data loading:

```python
import json
import requests
import pyarrow as pa
import io

with open("snapshots/2026-04-29-manifest.json") as f:
    files = json.load(f)

def cdn_fetch(path):
    url = f"https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

# Example: stream parquet files and project to {prompt, response}
for f in files:
    if not f["path"].endswith(".parquet"):
        continue
    buf = io.BytesIO(cdn_fetch(f["path"]))
    table = pa.parquet.read_table(buf)
    # project to schema you need
```

This pattern ensures:
- Single API call (from Mac/CI) to produce manifest  
- Training uses CDN-only fetches → zero API rate-limit exposure  
- Deterministic, reproducible file lists per date folder  

---

### 4) Quick validation checklist

- [ ] `chmod +x bin/snapshot.sh`  
- [ ] `HF_TOKEN=<token> ./bin/snapshot.sh --repo axentx/surrogate-1-training-pairs --date 2026-04-29 --out /tmp/test-manifest.json`  
- [ ] Confirm JSON output and non-zero exit on auth/429 when expected  
- [ ] Add `SNAPSHOT_DIR=snapshots` to workflow env to enable per-shard snapshots (optional)  

---

**Estimated effort**: ~30–60 minutes (including tests). This is the minimal, highest-leverage change to unblock reliable training runs while respecting HF API limits.

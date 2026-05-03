# surrogate-1 / quality

### Final Synthesis — CDN-Bypass + Pre-flight Snapshot (Corrected & Actionable)

**Chosen approach**: Combine Candidate 2’s deterministic snapshot/manifest with Candidate 1’s CDN-only download pattern, while fixing critical errors in Candidate 1 (incorrect cron, fragile HTML scraping, wrong repo path). The result is a safe, reproducible pipeline that eliminates HF API rate limits during training.

---

### 1) Add `bin/snapshot.sh` (deterministic manifest)

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
#
# Generate a deterministic file manifest for a date folder in
# axentx/surrogate-1-training-pairs so training can use HF CDN only.
#
# Usage:
#   HF_TOKEN=hf_xxx ./bin/snapshot.sh 2026-05-03
#
# Outputs:
#   snapshots/2026-05-03-manifest.json
#   snapshots/2026-05-03-manifest.json.sha256
#   snapshots/2026-05-03-manifest.files.txt

set -euo pipefail

REPO="datasets/axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="snapshots"
MANIFEST="${OUTDIR}/${DATE}-manifest.json"

mkdir -p "${OUTDIR}"

echo "== Generating snapshot for ${DATE} =="

python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi

api = HfApi(token=os.environ.get("HF_TOKEN"))
repo = "datasets/axentx/surrogate-1-training-pairs"
date = sys.argv[1]

# Non-recursive list to avoid pagination/429 on large repos
entries = api.list_repo_tree(repo=repo, path=date, recursive=False)

files = []
for e in entries:
    if e.type == "file":
        files.append({
            "path": f"{date}/{e.path.split('/')[-1]}",
            "size": getattr(e, "size", None),
            "lfs": getattr(e, "lfs", None) is not None,
        })

# Deterministic ordering
files.sort(key=lambda x: x["path"])

manifest = {
    "date": date,
    "repo": repo,
    "generated_at_utc": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    "files": files,
    "cdn_base": f"https://huggingface.co/datasets/{repo}/resolve/main",
}

out_path = sys.argv[2]
with open(out_path, "w") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)

# Compact line-delimited variant for shell loops
lines_path = out_path.replace(".json", ".files.txt")
with open(lines_path, "w") as f:
    for item in files:
        f.write(f"{item['path']}\n")

print(f"Wrote {len(files)} files -> {out_path}")
PY "${DATE}" "${MANIFEST}"

# Create checksum
sha256sum "${MANIFEST}" > "${MANIFEST}.sha256"
echo "== Snapshot complete: ${MANIFEST} (+ .sha256 + .files.txt) =="
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

### 2) Update `bin/dataset-enrich.sh` to prefer snapshot and use CDN-only fetch

Add near the top (after arg parsing):

```bash
# Prefer pre-flight snapshot to avoid list_repo_tree during runs.
DATE="${DATE:-$(date +%Y-%m-%d)}"
SNAPSHOT="snapshots/${DATE}-manifest.json"
if [[ -f "${SNAPSHOT}" ]]; then
  echo "Using pre-flight snapshot: ${SNAPSHOT}"
  export SURROGATE_SNAPSHOT="${SNAPSHOT}"
else
  echo "No snapshot found at ${SNAPSHOT}; will list via API (rate-limit risk)."
fi
```

Update the Python ingestion portion to prefer the snapshot:

```python
import os, json

def list_files_for_date(date: str):
    snapshot = os.environ.get("SURROGATE_SNAPSHOT")
    if snapshot and os.path.isfile(snapshot):
        with open(snapshot) as f:
            manifest = json.load(f)
        return [f["path"] for f in manifest["files"]]

    # Fallback to API (existing behavior)
    from huggingface_hub import HfApi
    api = HfApi()
    entries = api.list_repo_tree(
        repo="datasets/axentx/surrogate-1-training-pairs",
        path=date,
        recursive=False,
    )
    return [f"{date}/{e.path.split('/')[-1]}" for e in entries if e.type == "file"]
```

Add a CDN-only download helper (replaces API-dependent fetches):

```bash
#!/usr/bin/env bash
# bin/cdn-bypass.sh
# Download listed files via HF CDN (no API/auth required).
# Expects SURROGATE_SNAPSHOT or falls back to repo+date env vars.

set -euo pipefail

REPO="datasets/axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SNAPSHOT="${SURROGATE_SNAPSHOT:-snapshots/${DATE}-manifest.json}"

if [[ -f "${SNAPSHOT}" ]]; then
  echo "Downloading via CDN using snapshot: ${SNAPSHOT}"
  FILES=$(jq -r '.files[].path' < "${SNAPSHOT}")
  CDN_BASE=$(jq -r '.cdn_base' < "${SNAPSHOT}")
else
  echo "No snapshot; listing via API once (rate-limit risk) and downloading via CDN."
  # Lightweight fallback: list once and download via CDN
  FILES=$(python3 -c "
import os
from huggingface_hub import HfApi
api = HfApi()
entries = api.list_repo_tree(
    repo='datasets/axentx/surrogate-1-training-pairs',
    path='${DATE}',
    recursive=False,
)
for e in entries:
    if e.type == 'file':
        print(f'${DATE}/{e.path.split(\"/\")[-1]}')
")
  CDN_BASE="https://huggingface.co/datasets/${REPO}/resolve/main"
fi

mkdir -p "${DATE}"
for FILE in ${FILES}; do
  OUT="${FILE}"
  mkdir -p "$(dirname "${OUT}")"
  curl -sSfL -o "${OUT}" "${CDN_BASE}/${FILE}"
done

echo "CDN download complete for ${DATE}"
```

Make executable:
```bash
chmod +x bin/cdn-bypass.sh
```

Update `bin/dataset-enrich.sh` to call the CDN bypass after snapshot logic:

```bash
# After snapshot/env setup:
bash bin/cdn-bypass.sh
# ... continue with existing enrichment steps
```

---

### 3) Update training launcher to embed manifest and use CDN-only fetches

Replace `load_dataset(streaming=True)` with manifest-driven CDN fetches to avoid API calls and schema heterogeneity issues:

```python
import json
import os
from pathlib import Path
from huggingface_hub import hf_hub_download

MANIFEST = Path(os.environ.get("SURROGATE_MANIFEST", "snapshots/2026-05-03-manifest.json"))
if not MANIFEST.is_file():
    raise FileNotFoundError("Pre-flight manifest required for CDN-only training")

with open(MANIFEST) as f:
    files = [f["path"] for f in json.load(f)["files"]]

# Download each file via CDN (no Authorization header needed)
local_paths = []
for rel in files:
    local = hf_hub_download(
        repo_id="datasets/axentx/surrogate-1-training-pairs",
        filename=rel,
        repo_type="dataset",
    )
    local_paths.append(local)

# Parse local_paths into {prompt, response} pairs per project schema
# This avoids pyarrow CastError from heterogeneous schemas in streaming load.
```

---

### 4) Update CI (`.github/workflows/ingest.yml`) with correct schedule and snapshot step

```yaml
name: Ingest

on:
  schedule:
    # Correct cron: every 30 minutes (not every 

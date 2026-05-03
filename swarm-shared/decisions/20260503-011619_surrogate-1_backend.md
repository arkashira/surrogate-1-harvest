# surrogate-1 / backend

## Final Decision — highest-value <2h backend fix
**Replace recursive HF API ingestion and per-file authenticated fetches with a single non-recursive `list_repo_tree` per date folder + CDN-bypass ingestion + pre-listed manifest.**  
- Eliminates recursive pagination (primary 429 source).  
- Eliminates per-file authenticated API calls during ingestion (rate-limit and commit-cap pressure).  
- Keeps ingestion deterministic, shard-safe, and backwards-compatible.

---

## Implementation plan (≤2h)

1. **Add `bin/list-folder-manifest.sh`**  
   - Accepts `REPO`, `FOLDER`, `OUT_JSON`.  
   - Calls `huggingface_hub.list_repo_tree(path=FOLDER, recursive=False)` once.  
   - Emits compact manifest:  
     ```json
     {
       "repo": "...",
       "folder": "...",
       "ts": "...",
       "files": [
         {"path": "...", "sha": "...", "size": ...},
         ...
       ]
     }
     ```
   - Idempotent: exits 0 with empty `files` if folder missing.

2. **Update `bin/dataset-enrich.sh`**  
   - Accept optional `MANIFEST_FILE`.  
   - If manifest present and valid, skip all listing; iterate only files in manifest.  
   - If manifest absent/invalid, run `list-folder-manifest.sh` once per job (not per file).  
   - Download each file via **CDN URL** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) with curl (no auth header).  
   - Fallback to authenticated `hf_hub_download` on CDN 404 (future-proof for private repos).  
   - Keep existing schema projection, md5 dedup (`lib/dedup.py`), and shard upload.  
   - Upload to deterministic path:  
     `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`

3. **Add `lib/file_io.py` helpers**  
   - `download_via_cdn(repo, path, dest)` with curl + authenticated fallback.  
   - `project_to_pair(file_path)` isolates schema normalization for parquet/jsonl into `{prompt, response}`.

4. **GitHub Actions hygiene (optional but recommended)**  
   - Add a pre-matrix step to generate the manifest for the current date folder and upload as artifact.  
   - Each shard job downloads the same artifact → zero per-shard API list calls → eliminates 429 during ingestion window.

5. **Cron / scheduling hygiene**  
   - Ensure `SHELL=/bin/bash` in crontab entries and `#!/usr/bin/env bash` + `chmod +x` on all bin scripts.

6. **Smoke test**  
   - Run `bin/list-folder-manifest.sh` locally or via GH Actions dry-run.  
   - Run a single shard with `MANIFEST_FILE=manifest-YYYY-MM-DD.json bin/dataset-enrich.sh`.  
   - Verify output file in repo and confirm no 429/403 in logs.

---

## Code snippets

### bin/list-folder-manifest.sh
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
FOLDER="${1:-public-merged/$(date +%Y-%m-%d)}"
OUT="${2:-manifest-$(date +%Y-%m-%d).json}"

python3 - "$REPO" "$FOLDER" "$OUT" <<'PY'
import json
import sys
from datetime import datetime, timezone
from huggingface_hub import HfApi

repo = sys.argv[1]
folder = sys.argv[2]
out = sys.argv[3]

api = HfApi()
files = []
try:
    tree = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    for node in tree:
        files.append({
            "path": node.path,
            "sha": getattr(node, "sha", None),
            "size": getattr(node, "size", None),
        })
except Exception as e:
    # folder may not exist yet — treat as empty
    print(f"Warning: {e}", file=sys.stderr)

manifest = {
    "repo": repo,
    "folder": folder,
    "ts": datetime.now(timezone.utc).isoformat(),
    "files": files,
}

with open(out, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)

print(f"Wrote {len(files)} entries to {out}")
PY
```

### lib/file_io.py
```python
import os
import json
import tempfile
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from huggingface_hub import hf_hub_download

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def download_via_cdn(repo: str, path: str, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    # Use curl for reliable redirects and no-auth CDN access
    result = subprocess.run(
        ["curl", "-L", "--fail", "-s", "-o", str(dest), url],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
        return str(dest)

    # Fallback to authenticated download (for private repos or CDN miss)
    local_path = hf_hub_download(
        repo_id=repo,
        filename=path,
        local_dir=dest.parent,
        local_dir_use_symlinks=False,
    )
    # Ensure dest points to the file
    if Path(local_path) != dest:
        shutil.copy2(local_path, dest)
    return str(dest)


def project_to_pair(file_path: str):
    """
    Normalize heterogeneous file schemas into {prompt, response}.
    Supports parquet and jsonl. Extend per format as needed.
    """
    p = Path(file_path)
    if p.suffix == ".parquet":
        import pyarrow.parquet as pq
        tbl = pq.read_table(file_path, columns=["prompt", "response"])
        df = tbl.to_pandas()
    else:
        import pandas as pd
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                rows.append({
                    "prompt": obj.get("prompt") or obj.get("input") or "",
                    "response": obj.get("response") or obj.get("output") or "",
                })
        df = pd.DataFrame(rows)

    # Basic cleaning
    df["prompt"] = df["prompt"].fillna("").astype(str).str.strip()
    df["response"] = df["response"].fillna("").astype(str).str.strip()
    return df
```

### bin/dataset-enrich.sh (integrated excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
DATE_FOLDER="public-merged/$(date +%Y-%m-%d)"
MANIFEST_FILE="${MANIFEST_FILE:-}"

source "$(dirname "$0")/lib/dedup.py"

WORKDIR=$(mktemp -d)
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

# Resolve manifest
if [[ -z "$MANIFEST_FILE" || ! -f "$MANIFEST_FILE" ]]; then
  MANIFEST_FILE="$WORKDIR/manifest-$(date +%Y-%m-%d).json"
  echo "No valid MANIFEST_FILE provided; generating $MANIFEST_FILE"
  "$(dirname "$0")/list-folder-manifest.sh" "$REPO" "$DATE_FOLDER" "$MANIFEST_FILE"
fi

# Load file list from manifest
mapfile -t FILE_PATHS < <(
  python3 -c "
import json, sys
m = json.load(open(sys.argv[1]))
for f in m.get('files', []):
    print(f['path'])
" "$MANIFEST_FILE"
)

if [[ ${#FILE_PATHS[@

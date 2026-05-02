# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit pressure during ingestion and guarantees deterministic file lists for reproducible sharding.

### Concrete steps (1h 30m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Uses `huggingface_hub` to call `list_repo_tree(recursive=False)` on the dataset repo for today’s date folder (or latest).  
   - Outputs `snapshot-<date>.json` with `{ "date": "...", "files": [...], "sha256": "..." }`.  
   - Shebang `#!/usr/bin/env bash`, `set -euo pipefail`.

2. **Update `bin/dataset-enrich.sh`** (20m)  
   - Accept optional `SNAPSHOT_FILE` env var; if provided, skip `list_repo_files` and read file list from snapshot.  
   - Keep existing streaming/parquet logic unchanged; only the source of file paths changes.

3. **Update GitHub Actions matrix** (20m)  
   - Add a pre-step that runs `bin/snapshot.sh` once and passes the snapshot artifact to all 16 shards.  
   - Each shard downloads the snapshot and sets `SNAPSHOT_FILE` so all workers use identical file lists.

4. **Add CDN-only fetch helper in Python** (30m)  
   - Small utility `lib/cdn_fetch.py` that, given a repo+path, downloads via `https://huggingface.co/datasets/{repo}/resolve/main/{path}` with `requests` (no auth header).  
   - Retries with exponential backoff; falls back to HF API only if CDN 404 (should not happen for public files).

5. **Validation & smoke test** (20m)  
   - Run snapshot locally, verify JSON.  
   - Run one shard with snapshot against a small date folder; confirm zero HF API calls during file enumeration.

---

### Code snippets

#### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Generate a deterministic file-list snapshot for today's dataset folder.
# Usage: HF_TOKEN=<token> ./bin/snapshot.sh [date]
# Output: snapshots/snapshot-<date>.json

set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="snapshots"
OUTFILE="${OUTDIR}/snapshot-${DATE}.json"

mkdir -p "${OUTDIR}"

python3 - <<PY
import os, json, hashlib, datetime, sys
from huggingface_hub import HfApi

api = HfApi(token=os.environ.get("HF_TOKEN"))
repo = os.environ.get("REPO", "$REPO")
date = os.environ.get("DATE", "$DATE")

# List top-level for the date folder (non-recursive)
try:
    tree = api.list_repo_tree(repo=repo, path=date, recursive=False)
    files = [f.rfilename for f in tree if f.type == "file"]
except Exception as e:
    # If folder doesn't exist, produce empty list (safe)
    files = []

payload = {
    "date": date,
    "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    "repo": repo,
    "files": sorted(files),
}
blob = json.dumps(payload, sort_keys=True).encode()
payload["sha256"] = hashlib.sha256(blob).hexdigest()

with open(os.environ.get("OUTFILE", "$OUTFILE"), "w") as f:
    json.dump(payload, f, indent=2)
PY

echo "Snapshot written to ${OUTFILE}"
```

#### `lib/cdn_fetch.py`
```python
# lib/cdn_fetch.py
import requests
import time
import os
from pathlib import Path
from typing import Optional

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
HF_API_TEMPLATE = "https://huggingface.co/api/datasets/{repo}/resolve?path={path}"

def cdn_fetch(repo: str, path: str, out_path: Optional[Path] = None, max_retries: int = 5) -> bytes:
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                data = resp.content
                if out_path:
                    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(out_path).write_bytes(data)
                return data
            # 404 on CDN should not happen for public files; fallback to API once
            if resp.status_code == 404 and attempt == 0:
                api_url = HF_API_TEMPLATE.format(repo=repo, path=path)
                token = os.environ.get("HF_TOKEN")
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                api_resp = requests.get(api_url, headers=headers, timeout=30)
                api_resp.raise_for_status()
                # API returns JSON with redirect or content; try to follow
                if "path" in api_resp.json():
                    # If it's a redirect-like response, retry CDN with resolved path
                    path = api_resp.json()["path"]
                    url = CDN_TEMPLATE.format(repo=repo, path=path)
                    continue
                data = api_resp.content
                if out_path:
                    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(out_path).write_bytes(data)
                return data
            resp.raise_for_status()
        except (requests.RequestException, OSError) as e:
            if attempt == max_retries - 1:
                raise
            sleep = (2 ** attempt) + (os.urandom(1)[0] / 255.0)
            time.sleep(sleep)
    raise RuntimeError("Unreachable")
```

#### Update to `bin/dataset-enrich.sh` (excerpt)
```bash
# If SNAPSHOT_FILE is provided, use it; otherwise fall back to live listing.
if [ -n "${SNAPSHOT_FILE:-}" ] && [ -f "${SNAPSHOT_FILE}" ]; then
  echo "Using snapshot ${SNAPSHOT_FILE}"
  FILES=$(python3 -c "import json,sys;obj=json.load(open(sys.argv[1]));print('\n'.join(obj['files']))" "${SNAPSHOT_FILE}")
else
  echo "Listing files via HF API (no snapshot)"
  FILES=$(python3 - <<PY
from huggingface_hub import HfApi
import os
api = HfApi(token=os.environ.get("HF_TOKEN"))
repo = "axentx/surrogate-1-training-pairs"
date = os.environ.get("DATE", "$(date +%Y-%m-%d)")
try:
    tree = api.list_repo_tree(repo=repo, path=date, recursive=False)
    print("\n".join(f.rfilename for f in tree if f.type == "file"))
except Exception:
    print("")
PY
)
fi
```

#### GitHub Actions excerpt (`.github/workflows/ingest.yml`)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot_file: ${{ steps.set.outputs.snapshot_file }}
    steps:
      - uses: actions/checkout@v4
      - name: Generate snapshot
        run: |
          mkdir -p snapshots
          HF_TOKEN=${{ secrets.HF_TOKEN }} ./bin/snapshot.sh
      - name: Upload snapshot artifact
        uses: actions/upload-artifact@v4
        with:
          name: snapshot-${{ github.run_id }}
          path: snapshots/snapshot-*.json
      - id: set
        run: |
          SNAP=$(ls snapshots/snapshot-*.json | head -1)
          echo "snapshot_file=$SNAP" >> $GITHUB_OUTPUT

  ingest:
    needs: snapshot
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Download snapshot
        uses

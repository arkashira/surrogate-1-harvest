# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing. This eliminates HF API rate-limit risk during ingestion and aligns with the CDN bypass pattern.

### Steps (1h 30m total)

1. **Create `bin/snapshot.sh`** (20m)  
   - Uses `list_repo_tree` (non-recursive) per top-level folder to avoid 429.  
   - Outputs `snapshot/<date>/file-list.json` with CDN-ready `resolve/main/` URLs.  
   - Deterministic sort for reproducible shard assignment.

2. **Update `bin/dataset-enrich.sh`** (30m)  
   - Accept optional `FILE_LIST` env var pointing to snapshot JSON.  
   - If provided, shard workers read only assigned files from the list (no `list_repo_files`).  
   - Downloads via `curl`/`wget` from CDN URLs (no Authorization header).  
   - Fallback to current behavior if no snapshot (for backward compatibility).

3. **Update GitHub Actions matrix** (20m)  
   - Add a one-time job `snapshot` that runs `bin/snapshot.sh` and uploads artifact.  
   - Ingestion matrix downloads the artifact and passes `FILE_LIST` to each shard.  
   - Keep existing 16-shard parallelism unchanged.

4. **Add lightweight validation** (10m)  
   - Verify snapshot contains expected file count and parquet/jsonl extensions.  
   - Fail fast if snapshot is stale (>24h) when running in cron mode.

5. **Update README** (10m)  
   - Document new snapshot workflow and how to trigger manual refresh.  
   - Note CDN bypass benefits and rate-limit avoidance.

---

## Code Snippets

### 1. `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Generate deterministic file-list snapshot for CDN-only ingestion.
# Usage: HF_TOKEN=<token> bin/snapshot.sh <owner>/<dataset> <date-folder>

set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE="${2:-$(date +%Y-%m-%d)}"
OUTDIR="snapshot/${DATE}"
OUTFILE="${OUTDIR}/file-list.json"

mkdir -p "${OUTDIR}"

echo "Listing ${REPO} (non-recursive) for date folder: ${DATE}..."

# Use huggingface_hub Python to list top-level folder (avoids recursive pagination)
python3 - <<PY > "${OUTFILE}.tmp"
import os, json
from huggingface_hub import HfApi

api = HfApi(token=os.environ.get("HF_TOKEN"))
repo = "${REPO}"
date_folder = "${DATE}"

# List only the date folder (non-recursive)
tree = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)

files = []
for item in tree:
    if item.type == "file":
        # CDN URL (no auth required)
        cdn_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}/{item.path}"
        files.append({
            "path": item.path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None)
        })

# Deterministic ordering
files.sort(key=lambda x: x["path"])

output = {
    "repo": repo,
    "date": date_folder,
    "generated_at": os.popen("date -u +%Y-%m-%dT%H:%M:%SZ").read().strip(),
    "files": files
}

print(json.dumps(output, indent=2))
PY

mv "${OUTFILE}.tmp" "${OUTFILE}"
echo "Snapshot written to ${OUTFILE}"
echo "Total files: $(jq '.files | length' "${OUTFILE}")"
```

Make executable:
```bash
chmod +x bin/snapshot.sh
```

---

### 2. Update `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
# ... existing header ...

# If FILE_LIST is provided, use CDN-only mode
if [[ -n "${FILE_LIST:-}" && -f "${FILE_LIST}" ]]; then
  echo "CDN mode: using file list ${FILE_LIST}"
  TOTAL_FILES=$(jq '.files | length' "${FILE_LIST}")
  # Assign shard slice from FILE_LIST instead of repo listing
  SHARD_FILES=$(jq -r --argjson shard "$SHARD_ID" --argjson total "$SHARD_COUNT" \
    '.files | .[$shard:: $total] | .[].cdn_url' "${FILE_LIST}")
else
  echo "Legacy mode: listing repo files (may hit rate limits)"
  # ... existing list_repo_files logic ...
fi

# Download via CDN URL (no auth header)
for url in ${SHARD_FILES}; do
  outfile=$(basename "${url}")
  if curl -fsSL --retry 3 -o "${outfile}" "${url}"; then
    # ... existing processing logic ...
  else
    echo "Download failed: ${url}"
  fi
done
```

---

### 3. `.github/workflows/ingest.yml` (excerpt additions)
```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-path: ${{ steps.set.outputs.path }}
    steps:
      - uses: actions/checkout@v4
      - name: Generate snapshot
        run: |
          bin/snapshot.sh axentx/surrogate-1-training-pairs $(date +%Y-%m-%d)
      - name: Upload snapshot
        uses: actions/upload-artifact@v4
        with:
          name: file-list-snapshot
          path: snapshot/
      - id: set
        run: echo "path=snapshot/$(date +%Y-%m-%d)/file-list.json" >> $GITHUB_OUTPUT

  ingest:
    needs: snapshot
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4
      - name: Download snapshot
        uses: actions/download-artifact@v4
        with:
          name: file-list-snapshot
          path: snapshot/
      - name: Run shard
        env:
          SHARD_ID: ${{ matrix.shard }}
          SHARD_COUNT: 16
          FILE_LIST: snapshot/$(date +%Y-%m-%d)/file-list.json
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: bin/dataset-enrich.sh
```

---

### 4. README update (excerpt)
```markdown
## Snapshot-driven ingestion (recommended)

To avoid HuggingFace API rate limits during parallel ingestion, generate a
deterministic file-list snapshot once and use CDN-only downloads:

```bash
# Generate snapshot (run from repo root)
HF_TOKEN=<token> bin/snapshot.sh axentx/surrogate-1-training-pairs 2026-05-03

# Manual run with snapshot
FILE_LIST=snapshot/2026-05-03/file-list.json bin/dataset-enrich.sh
```

The GitHub Actions workflow now includes a `snapshot` job that produces
`file-list.json`. Each shard worker downloads this artifact and processes
only its assigned files via CDN URLs, eliminating API calls during data fetch.
```

---

**Estimated time**: ~1h 30m (including testing).  
**Impact**: Removes HF API rate-limit bottleneck, enables reliable 16-shard parallel ingestion, and aligns with CDN bypass best practice.

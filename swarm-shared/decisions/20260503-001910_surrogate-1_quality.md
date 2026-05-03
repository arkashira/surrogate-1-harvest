# surrogate-1 / quality

## Final Integrated Implementation  
*(Best parts merged; contradictions resolved for correctness + concrete actionability)*

**Goal (unchanged)**  
Eliminate HF API rate-limit risk during training by implementing a CDN-bypass pre-flight snapshot for `axentx/surrogate-1-training-pairs`.

**Scope (unchanged)**  
Add `bin/snapshot.sh` + modify `bin/dataset-enrich.sh` to embed a deterministic file manifest and fetch training pairs via CDN URLs only.

**Why this ships <2h and is highest-value**  
- Removes `/api/` calls during training data load (prevents 429s).  
- Reuses existing patterns: pre-list once → embed JSON → Lightning training does CDN-only fetches.  
- Small, targeted change; no model/training logic changes.

---

## Resolved Decisions (correctness + actionability)

| Decision | Chosen approach | Rationale |
|---|---|---|
| Snapshot filename | `snapshot/YYYY-MM-DD.json` (not timestamped) | Simpler cron idempotency; one manifest per date is sufficient and easier to cache in Actions. |
| Manifest schema | `{date, repo, files}` (no embedded base_url) | Avoids redundancy; base URL is deterministic and can be constructed safely in consumer code. |
| Fallback behavior | Keep API fallback in `dataset-enrich.sh` only when snapshot missing | Ensures backward compatibility and local dev ergonomics while encouraging snapshot use in CI. |
| Sharding coordination | Single snapshot artifact produced by one job, passed to all matrix shards | Guarantees all shards see identical file list (no race conditions or per-shard API calls). |
| Dedup store location | Explicitly use `/tmp/dedup-cache.sqlite` in CI and allow override via env | Prevents workspace permission issues and makes behavior reproducible locally and in Actions. |
| Validation | Require dry-run + CDN reachability check in snapshot step | Fails fast if CDN URLs are broken or repo structure changes. |
| Python helper for snapshot | Keep inline Python with `huggingface_hub` (single API call) | Minimal dependency surface; already required in CI. |

---

## Implementation Plan

1. **Create `bin/snapshot.sh`**  
   - Single API call: `list_repo_tree(path=date, recursive=False)` for today’s folder (or provided date).  
   - Save `{"date":"YYYY-MM-DD","repo":"axentx/surrogate-1-training-pairs","files":["f1.parquet",...]}` to `snapshot/YYYY-MM-DD.json`.  
   - Dry-run: verify at least one `.parquet` file and that first CDN URL responds with 200 (curl -I).  
   - Exit non-zero on failure (so cron/CI fails fast).  
   - Make executable (`chmod +x`).

2. **Modify `bin/dataset-enrich.sh`**  
   - Accept optional snapshot path arg; if provided and valid, skip `list_repo_tree` and read file list from snapshot.  
   - For each file assigned to this shard, download via CDN URL:  
     `https://huggingface.co/datasets/{repo}/resolve/main/{date}/{file}`  
   - Project to `{prompt,response}` and dedup via `lib/dedup.py` using `/tmp/dedup-cache.sqlite` in CI (or env override).  
   - Upload shard output unchanged: `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.  
   - Keep API fallback if snapshot missing (backward compat).

3. **Update GitHub Actions (`.github/workflows/ingest.yml`)**  
   - Add a lightweight “snapshot” job that runs first, produces `snapshot/YYYY-MM-DD.json` as artifact.  
   - Pass snapshot artifact to the 16 shard runners so all use identical manifest.  
   - Each shard step passes the snapshot path to `dataset-enrich.sh`.  
   - Keep fallback to API list if snapshot missing (defensive).

4. **Validation & Safety**  
   - Dry-run snapshot locally; verify CDN URLs are reachable (curl -I).  
   - Ensure no `load_dataset(streaming=True)` on heterogeneous repo during ingestion.  
   - Confirm dedup SQLite store path is writable in Actions (use `/tmp` by default).  
   - Validate produced JSONL schema `{prompt,response}` and non-empty shards.

---

## Code Snippets

### `bin/snapshot.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="${2:-snapshot}"
OUTFILE="${OUTDIR}/${DATE}.json"

mkdir -p "${OUTDIR}"

echo "[snapshot] Listing ${REPO} tree for date ${DATE}..."
files=$(python3 - "$REPO" "$DATE" <<'PY'
import os, json, sys
from huggingface_hub import HfApi
repo_id = sys.argv[1]
date = sys.argv[2]
api = HfApi()
try:
    tree = api.list_repo_tree(repo_id, path=date, recursive=False)
    filenames = sorted(item.path for item in tree if item.path.endswith(".parquet"))
except Exception as e:
    print(json.dumps({"error": str(e)}), file=sys.stderr)
    sys.exit(1)
print(json.dumps(filenames, separators=(",", ":")))
PY
)

if [ -z "${files}" ] || echo "${files}" | grep -q '"error"'; then
    echo "[snapshot] ERROR: Failed to list files for ${DATE}" >&2
    exit 1
fi

# Basic CDN reachability check (first file only, fail fast)
first_file=$(echo "${files}" | jq -r '.[0]')
cdn_url="https://huggingface.co/datasets/${REPO}/resolve/main/${DATE}/${first_file}"
if ! curl -fsSI --retry 2 --max-time 10 "${cdn_url}" > /dev/null 2>&1; then
    echo "[snapshot] ERROR: CDN URL unreachable: ${cdn_url}" >&2
    exit 1
fi

jq -n \
  --arg date "${DATE}" \
  --arg repo "${REPO}" \
  --argjson files "${files}" \
  '{date:$date, repo:$repo, files:$files}' > "${OUTFILE}"

echo "[snapshot] Written ${OUTFILE} with $(jq '.files | length' "${OUTFILE}") files"
```

### Updated `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
DATE="${1:-$(date +%Y-%m-%d)}"
SHARD_ID="${2:-0}"
TOTAL_SHARDS="${3:-16}"
SNAPSHOT="${4:-}"  # optional snapshot JSON

WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"
DEDUP_PY="${WORKDIR}/lib/dedup.py"
OUTDIR="${WORKDIR}/batches/public-merged/${DATE}"
mkdir -p "${OUTDIR}"

# Dedup store (use /tmp in CI to avoid workspace permission issues)
DEDUP_DB="${DEDUP_DB:-/tmp/dedup-cache.sqlite}"
export DEDUP_DB

# Resolve file list
if [ -n "${SNAPSHOT}" ] && [ -f "${SNAPSHOT}" ]; then
    echo "[enrich] Using snapshot ${SNAPSHOT}"
    mapfile -t FILES < <(jq -r '.files[]' "${SNAPSHOT}")
else
    echo "[enrich] Falling back to API list for ${DATE}"
    mapfile -t FILES < <(python3 - "$REPO" "$DATE" <<'PY'
import sys, json
from huggingface_hub import HfApi
api = HfApi()
tree = api.list_repo_tree(sys.argv[1], path=sys.argv[2], recursive=False)
for item in tree:
    if item.path.endswith(".parquet"):
        print(item.path)
PY
)
fi

# Filter to shard slice
TOTAL_FILES=${#FILES[@]}
if [ "${TOTAL_FILES}" -eq 0 ]; then
    echo "[enrich] No files for ${DATE}"; exit 0
fi

for idx in $(seq 0 $((TOTAL_FILES - 1))); do
    if [ $((idx % TOTAL_SHARDS)) -ne "${SHARD_ID}" ]; then continue; fi
    relpath="${FILES[idx]}"
    filename="${relpath##*/}"

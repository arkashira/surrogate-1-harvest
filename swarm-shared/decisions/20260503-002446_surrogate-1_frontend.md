# surrogate-1 / frontend

**Final Implementation Plan — CDN-first snapshot + zero-HF-API ingestion**

**Goal**: Eliminate HF API rate-limit risk during training/ingest by producing a deterministic file manifest per date folder and switching all downstream fetches to CDN URLs.  
**Scope**: ≤2 hours, minimal diff, zero change to dedup/schema logic.

---

### Why this is highest value
- Removes repeated `list_repo_files`/`load_dataset` API calls during training.
- Enables Lightning training to run with zero HF API calls (CDN-only).
- Fits existing deterministic-shard model (shard ID → filter snapshot rows by hash).
- Low risk, small code surface, immediate payoff for surrogate-1 ingestion/training.

---

### Files to add/modify
```
bin/
  snapshot.sh          # produce deterministic manifest per date
  fetch-cdn.sh         # bulk download via CDN using manifest
  dataset-enrich.sh    # updated to prefer CDN manifest
lib/
  snapshot.py          # validate + project manifest for training
README.md              # usage + HF CDN bypass note
```

---

### 1) `bin/snapshot.sh` (deterministic manifest generator)

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage: HF_TOKEN=... ./bin/snapshot.sh <owner>/<dataset> <date-folder> [out-dir]
#
# Produces:
#   <out-dir>/<date-folder>/files.json   — [{path,cdn_url,size}]
#   <out-dir>/<date-folder>/manifest.json — {repo,date_folder,count,created_at}
#
# Uses a single non-recursive tree call to avoid pagination/rate limits.
# All downloads use CDN URLs (no auth required).

set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE_FOLDER="${2:-}"
OUT_DIR="${3:-snapshot}"

if [ -z "$DATE_FOLDER" ]; then
  echo "Usage: $0 <owner/repo> <date-folder> [out-dir]"
  exit 1
fi

if [ -z "${HF_TOKEN:-}" ]; then
  echo "WARNING: HF_TOKEN not set — unauthenticated calls subject to lower rate limits."
  AUTH_HEADER=""
else
  AUTH_HEADER="Authorization: Bearer ${HF_TOKEN}"
fi

API="https://huggingface.co/api/datasets/${REPO}/tree"
DEST_DIR="${OUT_DIR}/${DATE_FOLDER}"
mkdir -p "$DEST_DIR"
FILES_JSON="${DEST_DIR}/files.json"
MANIFEST_JSON="${DEST_DIR}/manifest.json"

echo "Fetching tree for ${REPO}/${DATE_FOLDER} (non-recursive)..."
TEMP=$(mktemp)
cleanup() { rm -f "$TEMP"; }
trap cleanup EXIT

if ! curl -sSf -H "${AUTH_HEADER}" "${API}/${DATE_FOLDER}?recursive=false" > "$TEMP"; then
  echo "ERROR: Failed to list ${DATE_FOLDER}. If 429, wait 360s and retry."
  exit 1
fi

# Build deterministic files.json with CDN URLs.
jq -r --arg repo "$REPO" '
  def cdn_url($p): "https://huggingface.co/datasets/\($repo)/resolve/main/\($p)";
  map(
    select(.type == "file") |
    {
      path: .path,
      cdn_url: (cdn_url(.path)),
      size: .size
    }
  ) | sort_by(.path)
' "$TEMP" > "$FILES_JSON"

COUNT=$(jq length "$FILES_JSON")
CREATED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

jq -n \
  --arg repo "$REPO" \
  --arg df "$DATE_FOLDER" \
  --argjson count "$COUNT" \
  --arg created "$CREATED_AT" \
  '{
    repo: $repo,
    date_folder: $df,
    count: $count,
    created_at: $created
  }' > "$MANIFEST_JSON"

echo "Snapshot written to ${DEST_DIR}/"
echo "  files.json entries: ${COUNT}"
echo "  manifest.json: ${MANIFEST_JSON}"
```

- Make executable: `chmod +x bin/snapshot.sh`
- Run once per date folder after rate-limit window clears; commit or embed the JSON in training runs.

---

### 2) `bin/fetch-cdn.sh` (CDN bulk downloader)

```bash
#!/usr/bin/env bash
# bin/fetch-cdn.sh
# Usage: ./bin/fetch-cdn.sh <snapshot-dir> <shard-id> <world-size> [out-dir]
#
# Reads snapshot/<date>/files.json, filters by deterministic shard assignment,
# and downloads each file via CDN into out-dir (default: ./cdn-cache).

set -euo pipefail

SNAPSHOT_DIR="${1:-}"
SHARD_ID="${2:-0}"
WORLD_SIZE="${3:-1}"
OUT_DIR="${4:-cdn-cache}"

if [ -z "$SNAPSHOT_DIR" ]; then
  echo "Usage: $0 <snapshot-dir> <shard-id> <world-size> [out-dir]"
  exit 1
fi

FILES_JSON="${SNAPSHOT_DIR}/files.json"
if [ ! -f "$FILES_JSON" ]; then
  echo "ERROR: ${FILES_JSON} not found."
  exit 1
fi

mkdir -p "$OUT_DIR"

# Deterministic shard filter: hash(path) mod world_size == shard_id
mapfile -t URLS < <(
  jq -r --argjson shard "$SHARD_ID" --argjson ws "$WORLD_SIZE" '
    map(
      select((.path | @sh | . | hash) % $ws == $shard) |
      .cdn_url
    ) | .[]
  ' "$FILES_JSON"
)

echo "Shard ${SHARD_ID}/${WORLD_SIZE} — downloading ${#URLS[@]} files to ${OUT_DIR}"

for url in "${URLS[@]}"; do
  out_file="${OUT_DIR}/$(basename "$url")"
  if [ -f "$out_file" ]; then
    echo "Skip (exists): ${out_file}"
    continue
  fi
  echo "Fetching CDN: $url"
  curl -sSfL "$url" -o "$out_file"
done

echo "Done."
```

- No auth required; CDN-only downloads.
- Deterministic shard assignment matches training workers.

---

### 3) Update `bin/dataset-enrich.sh` (prefer CDN manifest)

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated to accept --snapshot <snapshot-dir> and prefer CDN fetch.

set -euo pipefail

SNAPSHOT_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot) SNAPSHOT_DIR="$2"; shift 2 ;;
    *) break ;;
  esac
done

# If snapshot provided, use CDN fetch list; otherwise keep existing HF API path.
if [ -n "$SNAPSHOT_DIR" ]; then
  echo "Using snapshot: ${SNAPSHOT_DIR}"
  SHARD_ID="${SHARD_ID:-0}"
  WORLD_SIZE="${WORLD_SIZE:-1}"
  # Reuse fetch-cdn.sh logic inline for portability (or call it).
  FILES_JSON="${SNAPSHOT_DIR}/files.json"
  if [ ! -f "$FILES_JSON" ]; then
    echo "ERROR: ${FILES_JSON} not found."
    exit 1
  fi

  mapfile -t URLS < <(
    jq -r --argjson shard "$SHARD_ID" --argjson ws "$WORLD_SIZE" '
      map(
        select((.path | @sh | . | hash) % $ws == $shard) |
        .cdn_url
      ) | .[]
    ' "$FILES_JSON"
  )

  for url in "${URLS[@]}"; do
    echo "Fetching CDN: $url"
    curl -sSfL "$url" -o "/tmp/$(basename "$url")"
    # ... existing normalize/dedup logic on /tmp/*.parquet or .jsonl ...
  done
else
  echo "No snapshot — using legacy HF API path (may hit rate limits)."
  # ... existing load_dataset / hf_hub_download logic ...
fi
```

- No change to dedup logic (`lib/dedup.py`) — dedup happens after download.

---

### 4) `lib/snapshot.py` (validation + projection for training)



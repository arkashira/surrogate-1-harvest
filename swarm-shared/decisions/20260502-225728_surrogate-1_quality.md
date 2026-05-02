# surrogate-1 / quality

**Final Synthesis**  
*(Best parts merged, contradictions resolved in favor of correctness + concrete actionability)*

---

## Summary of the chosen approach
- **Deterministic pre-flight file listing** (date-scoped) to eliminate recursive `list_repo_files` calls that cause HF API 429s.
- **CDN-only ingestion** during training/shard work to achieve zero HF API data calls.
- **Backward-compatible**: workers fall back to current behavior when no file list is provided.
- **Actionable in ≤2 hours**: 3 files changed (~110 lines total), clear tests and rollout steps.

---

## 1) `bin/list_files.py` (new, deterministic, date-scoped)

```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a date folder in
axentx/surrogate-1-training-pairs.

Usage:
  python bin/list_files.py --date 2026-05-02 --out file-list.json

Output schema:
{
  "repo": "axentx/surrogate-1-training-pairs",
  "date": "2026-05-02",
  "generated_at": "2026-05-02T22:00:00Z",
  "files": [
    {"path": "batches/public-raw/2026-05-02/foo.parquet", "size": 12345, "sha256": null},
    ...
  ]
}
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

REPO = "axentx/surrogate-1-training-pairs"

def main() -> None:
    parser = argparse.ArgumentParser(description="List repo files for a date folder (non-recursive per folder).")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--token", default=os.getenv("HF_TOKEN"), help="HF token (optional for public repo listing)")
    args = parser.parse_args()

    api = HfApi(token=args.token)
    folder_path = f"batches/public-raw/{args.date}"

    try:
        entries = api.list_repo_tree(
            repo_id=REPO,
            path=folder_path,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as e:
        print(f"ERROR listing {folder_path}: {e}", file=sys.stderr)
        sys.exit(1)

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        files.append({
            "path": entry.path,
            "size": getattr(entry, "size", None),
            "sha256": None,
        })

    # Deterministic ordering for stable sharding
    files.sort(key=lambda x: x["path"])

    payload = {
        "repo": REPO,
        "date": args.date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list_files.py
```

---

## 2) `bin/dataset-enrich.sh` (edit: CDN-only + fallback)

```bash
#!/usr/bin/env bash
#
# dataset-enrich.sh
# Normalize and dedup public dataset shards.
#
# Usage:
#   SHARD_ID=0 SHARD_COUNT=16 FILE_LIST=file-list.json ./bin/dataset-enrich.sh
#
# Environment:
#   SHARD_ID      (required) 0..15
#   SHARD_COUNT   (required) total shards (e.g., 16)
#   FILE_LIST     (optional) path to file-list.json from list_files.py
#
# Behavior:
# - If FILE_LIST is provided and valid: use CDN-only ingestion (zero HF API data calls).
# - Otherwise: fall back to current load_dataset behavior.

set -euo pipefail

: "${SHARD_ID:?required}"
: "${SHARD_COUNT:?required}"
FILE_LIST="${FILE_LIST:-}"

REPO="axentx/surrogate-1-training-pairs"
BASE_CDN="https://huggingface.co/datasets/${REPO}/resolve/main"

log() {
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*"
}

normalize_and_dedup() {
  local input_path="$1"
  local output_path="$2"
  python -m lib.dedup --input "$input_path" --output "$output_path"
}

if [[ -n "$FILE_LIST" && -f "$FILE_LIST" ]]; then
  log "CDN-only mode enabled (FILE_LIST=$FILE_LIST)"

  # Read file list and select shard slice deterministically
  mapfile -t ALL_PATHS < <(jq -r '.files[].path' "$FILE_LIST" | sort)
  TOTAL_FILES="${#ALL_PATHS[@]}"
  if (( TOTAL_FILES == 0 )); then
    log "No files found in FILE_LIST; exiting."
    exit 0
  fi

  # Assign files to shards by stable hash modulo SHARD_COUNT
  shard_files=()
  for path in "${ALL_PATHS[@]}"; do
    # Deterministic shard assignment using path hash
    hash=$(printf '%s' "$path" | sha256sum | awk '{print $1}')
    shard=$(( 0x${hash:0:8} % SHARD_COUNT ))
    if (( shard == SHARD_ID )); then
      shard_files+=("$path")
    fi
  done

  log "Shard ${SHARD_ID}/${SHARD_COUNT} processing ${#shard_files[@]}/${TOTAL_FILES} files"

  for rel_path in "${shard_files[@]}"; do
    cdn_url="${BASE_CDN}/${rel_path}"
    tmpfile=$(mktemp)
    if curl -fsSL --retry 3 --retry-delay 2 -o "$tmpfile" "$cdn_url"; then
      outname=$(basename "$rel_path" .parquet)_enriched.parquet
      normalize_and_dedup "$tmpfile" "$outname"
      rm -f "$tmpfile"
      log "Processed $rel_path -> $outname"
    else
      log "ERROR downloading $cdn_url"
      rm -f "$tmpfile"
    fi
  done

else
  log "FILE_LIST not provided or missing; falling back to load_dataset behavior"
  # Existing behavior preserved (uses load_dataset; may hit HF API listing)
  python -m lib.train_worker --shard-id "$SHARD_ID" --shard-count "$SHARD_COUNT"
fi
```

Make executable:
```bash
chmod +x bin/dataset-enrich.sh
```

---

## 3) `lib/dedup.py` (minor edit: add cdn_url to metadata)

```python
# lib/dedup.py
import argparse
import json
import os
import pyarrow as pa
import pyarrow.parquet as pq
import hashlib

def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cdn-url", default=None)
    args = parser.parse_args()

    table = pq.read_table(args.input)
    # Example dedup logic: drop exact duplicate rows across all columns
    table = table.drop_duplicates()

    metadata = table.schema.metadata or {}
    metadata[b"cdn_url"] = args.cdn_url.encode() if args.cdn_url else b""
    metadata[b"sha256"] = file_hash(args.input).encode()
    table = table.replace_schema_metadata(metadata)

    pq.write_table(table, args.output)
    print(f

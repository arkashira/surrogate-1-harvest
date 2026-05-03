# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Goal**: Eliminate HF API rate-limit risk and OOM in the surrogate-1 ingestion pipeline by replacing recursive authenticated fetches with deterministic shard routing + CDN-only fetches.

**Why this is highest value**  
- Removes 429s from `list_repo_tree` recursive pagination (100-item pages)  
- Avoids `load_dataset(streaming=True)` on heterogeneous schemas (PyArrow CastError)  
- Cuts authenticated API calls during training to **zero** (CDN-only)  
- Fits in <2h: change one script + one training loader, no infra rewrite  

---

### 1) Add helper to produce file manifest (run on Mac/orchestrator)
Save as `bin/build-manifest.py` (used before workflow dispatch).

```python
#!/usr/bin/env python3
"""
Usage:
  python bin/build-manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out manifest-2026-05-03.json

Produces:
  {
    "date": "2026-05-03",
    "repo": "...",
    "files": [
      {"path": "2026-05-03/file1.parquet", "size": 12345},
      ...
    ]
  }
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder in repo")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi()
    # Single non-recursive call per date folder
    entries = api.list_repo_tree(
        repo_id=args.repo,
        path=args.date,
        recursive=False,
        repo_type="dataset",
    )

    files = []
    for e in entries:
        if e.type == "file":
            files.append({"path": e.path, "size": e.size})

    manifest = {
        "date": args.date,
        "repo": args.repo,
        "files": files,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/build-manifest.py
```

---

### 2) Update `bin/dataset-enrich.sh`

Key changes:
- Accept `MANIFEST_JSON` (path to manifest produced above) as input.
- Compute deterministic shard: `shard_id = hash(slug) % 16`.
- Only process files assigned to this `SHARD_ID`.
- Fetch via CDN (`resolve/main/...`) with no auth header.
- Keep existing dedup/upload behavior.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Updated: deterministic shard + CDN-only fetches
set -euo pipefail

# Required env
: "${HF_TOKEN:?HF_TOKEN required}"
: "${SHARD_ID:?SHARD_ID (0-15) required}"
: "${MANIFEST_JSON:?path to manifest JSON required}"
: "${BATCH_DATE:=$(date +%Y-%m-%d)}"

REPO="axentx/surrogate-1-training-pairs"
OUT_DIR="batches/public-merged/${BATCH_DATE}"
TIMESTAMP=$(date +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"
TMP_DIR=$(mktemp -d)
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

mkdir -p "$OUT_DIR"

echo "[$(date)] Shard ${SHARD_ID} starting — manifest: ${MANIFEST_JSON}"

# Python helper to assign shard and emit local file list for this shard
python3 - "$MANIFEST_JSON" "$SHARD_ID" "$TMP_DIR/files.txt" <<'PY'
import json
import hashlib
import sys

manifest_path, shard_id_str, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
shard_id = int(shard_id_str)

with open(manifest_path) as f:
    manifest = json.load(f)

selected = []
for fobj in manifest["files"]:
    path = fobj["path"]
    # Deterministic shard: hash(slug) % 16
    # Use path as slug (or extract basename without extension)
    slug = path
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    if (h % 16) == shard_id:
        selected.append(path)

with open(out_path, "w") as f:
    for p in selected:
        f.write(p + "\n")
PY

echo "[$(date)] Assigned $(wc -l < "$TMP_DIR/files.txt") files to shard ${SHARD_ID}"

# Process assigned files — CDN-only downloads (no auth header)
> "$OUT_FILE"

while IFS= read -r rel_path; do
    [ -z "$rel_path" ] && continue
    # CDN URL (public, no Authorization header)
    url="https://huggingface.co/datasets/${REPO}/resolve/main/${rel_path}"
    local_path="${TMP_DIR}/$(basename "$rel_path")"

    echo "[$(date)] Fetching ${rel_path} -> ${local_path}"
    if ! curl -fsSL --retry 3 --retry-delay 5 -o "$local_path" "$url"; then
        echo "[$(date)] WARN: failed to download ${rel_path}, skipping"
        continue
    fi

    # Project to {prompt,response} and normalize per-schema here.
    # Keep existing per-schema logic (parquet/jsonl/etc.) but operate on local file.
    # For brevity, placeholder:
    python3 lib/project_to_pairs.py "$local_path" "$TMP_DIR/pairs.jsonl" || {
        echo "[$(date)] WARN: projection failed for ${rel_path}, skipping"
        continue
    }

    # Dedup and append
    python3 lib/dedup.py "$TMP_DIR/pairs.jsonl" "$TMP_DIR/dedup.jsonl"
    cat "$TMP_DIR/dedup.jsonl" >> "$OUT_FILE"

    rm -f "$local_path" "$TMP_DIR/pairs.jsonl" "$TMP_DIR/dedup.jsonl"
done < "$TMP_DIR/files.txt"

# Upload (non-recursive, single file per shard+timestamp — no collisions)
echo "[$(date)] Uploading ${OUT_FILE}"
huggingface-cli upload \
    --repo-type dataset \
    "${REPO}" \
    "${OUT_FILE}" \
    "${OUT_FILE}" \
    --token "${HF_TOKEN}"

echo "[$(date)] Shard ${SHARD_ID} completed: ${OUT_FILE}"
```

Make executable:
```bash
chmod +x bin/dataset-enrich.sh
```

---

### 3) Minimal projection stub (keep existing logic)
Ensure `lib/project_to_pairs.py` exists and accepts:
- `input_path` (parquet/jsonl)
- `output_path` (jsonl with `{prompt, response}`)

If not present, create a small stub that handles common schemas and projects only `{prompt, response}` (per past pattern: project at parse time, no extra metadata columns).

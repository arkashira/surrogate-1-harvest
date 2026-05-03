# surrogate-1 / backend

**Final synthesized implementation**  
Combines strongest, actionable parts from both proposals and resolves contradictions in favor of correctness and concrete execution.

---

## Core change (single sentence)
Replace recursive `list_repo_files` + per-file HF API calls with **one `list_repo_tree` per date folder + CDN-only fetches**, and **project to `{prompt,response}` at parse time** to eliminate 429 rate limits and HF Space OOM.

---

## What to modify (concrete)

- `bin/gen-manifest.sh` (new, run once per date folder)  
- `bin/dataset-enrich.sh` (worker script)  
- `ingest.yml` (optional orchestration)  

Keep deterministic 16-shard split and existing dedup flow **unchanged**.

---

## 1) Manifest generator (run once, commit or embed)

```bash
# bin/gen-manifest.sh
#!/usr/bin/env bash
set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE="${2:-$(date +%Y-%m-%d)}"
OUT="${3:-manifest-${DATE}.json}"

python3 - "$REPO" "$DATE" "$OUT" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

repo_id, date_folder, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
api = HfApi()

# One non-recursive tree call per folder (no per-file API calls)
tree = api.list_repo_tree(repo_id, path=date_folder, recursive=False)
files = sorted(
    f.rfilename
    for f in tree
    if f.rfilename.endswith((".jsonl", ".parquet", ".json"))
)

manifest = {"repo": repo_id, "date": date_folder, "files": files}
os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, ensure_ascii=False)
print(f"Wrote {len(files)} files to {out_path}")
PY
```

```bash
chmod +x bin/gen-manifest.sh
bin/gen-manifest.sh axentx/surrogate-1-training-pairs 2026-05-03 manifest-20260503.json
```

**Why this is correct**:  
- Uses `list_repo_tree(..., recursive=False)` (single API call per folder).  
- Produces a small JSON manifest that can be committed or passed to workers.  
- Avoids per-file API calls entirely.

---

## 2) Worker script (CDN-only, no auth, projection at parse time)

```bash
# bin/dataset-enrich.sh
#!/usr/bin/env bash
# CDN-only ingestion; no HF API auth during streaming
set -euo pipefail

REPO="${REPO:-axentx/surrogate-1-training-pairs}"
DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
MANIFEST="${MANIFEST:-manifest-${DATE}.json}"
OUT_DIR="${OUT_DIR:-output}"
TMP_DIR="${TMP_DIR:-/tmp/surrogate-ingest}"

mkdir -p "$OUT_DIR" "$TMP_DIR"

# Validate manifest
if [[ ! -f "$MANIFEST" ]]; then
  echo "ERROR: Manifest $MANIFEST not found. Generate with bin/gen-manifest.sh"
  exit 1
fi

# Load file list from manifest (deterministic)
mapfile -t ALL_FILES < <(
  python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for fn in sorted(data['files']):
    print(fn)
" "$MANIFEST"
)

TOTAL_FILES="${#ALL_FILES[@]}"
if [[ "$TOTAL_FILES" -eq 0 ]]; then
  echo "No files in manifest for $DATE"
  exit 0
fi

# Deterministic shard assignment (stable by filename)
mapfile -t MY_FILES < <(
  for f in "${ALL_FILES[@]}"; do
    HASH=$(echo -n "$f" | md5sum | awk '{print $1}')
    SHARD=$(( 0x${HASH:0:8} % TOTAL_SHARDS ))
    if [[ "$SHARD" -eq "$SHARD_ID" ]]; then
      echo "$f"
    fi
  done
)

echo "Shard $SHARD_ID/$TOTAL_SHARDS processing ${#MY_FILES[@]} files (out of $TOTAL_FILES total)"

# Process via CDN only
for REL_PATH in "${MY_FILES[@]}"; do
  FILENAME=$(basename "$REL_PATH")
  CDN_URL="https://huggingface.co/datasets/${REPO}/resolve/main/${REL_PATH}"
  TMP_FILE="${TMP_DIR}/${FILENAME}.dl"

  echo "Downloading ${REL_PATH} via CDN..."
  curl -fsSL --retry 3 --retry-delay 5 -o "$TMP_FILE" "$CDN_URL"

  # Project to {prompt,response} at parse time
  OUT_TMP="${TMP_DIR}/${FILENAME}.projected.jsonl"
  python3 - "$TMP_FILE" "$OUT_TMP" <<'PY'
import sys, json, pyarrow.parquet as pq

src, dst = sys.argv[1], sys.argv[2]

def normalize(obj):
    prompt = obj.get("prompt") or obj.get("input") or obj.get("text") or ""
    response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
    if not prompt and not response:
        return None
    return {"prompt": str(prompt), "response": str(response)}

rows = []
if src.endswith(".parquet"):
    tbl = pq.read_table(src)
    schema_names = [f.name for f in tbl.schema]
    # Read all rows; normalize will pick fields
    for batch in tbl.to_batches():
        for row in batch.to_pylist():
            rows.append(normalize(row))
elif src.endswith(".jsonl"):
    with open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(normalize(json.loads(line)))
            except Exception:
                continue
elif src.endswith(".json"):
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        rows = [normalize(obj) for obj in data]
    else:
        rows = [normalize(data)]

rows = [r for r in rows if r is not None]
with open(dst, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
PY

  if [[ -s "$OUT_TMP" ]]; then
    cat "$OUT_TMP" >> "${OUT_DIR}/shard${SHARD_ID}-projected.jsonl"
  fi

  rm -f "$TMP_FILE" "$OUT_TMP"
done

echo "Shard $SHARD_ID complete"
```

**Key correctness choices**  
- Uses **CDN URLs only** (`resolve/main/...`) — no Authorization header, bypasses `/api/` rate limits.  
- Projects to `{prompt,response}` **at parse time**, preventing mixed-schema pollution.  
- Keeps deterministic 16-shard split by filename hash (unchanged).  
- Avoids `datasets` library streaming with auth (reduces OOM risk).  

---

## 3) Optional orchestration (ingest.yml)

```yml
# .github/workflows/ingest.yml
name: Ingest

on:
  workflow_dispatch:
  schedule:
    - cron: "0 2 * * *"

jobs:
  ingest:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install deps
        run: pip install huggingface_hub pyarrow



# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value incremental improvement**: Add deterministic pre-flight file listing + CDN-only ingestion path to eliminate HF API rate limits during training and make shard work reproducible.

### What will ship
1. `bin/list-date-files.sh` — Mac-side pre-flight: list one date folder via `list_repo_tree(recursive=False)` once, save to `file-list-YYYY-MM-DD.json`. Embed this list in training/shard scripts so Lightning training does **zero** HF API calls during data loading.
2. `bin/dataset-enrich.sh` update — switch from `load_dataset(streaming=True)` to per-file CDN downloads (`huggingface.co/datasets/.../resolve/main/...`) using the pre-computed file list; project to `{prompt,response}` only at parse time.
3. `lib/dedup.py` unchanged (remains source-of-truth central md5 store).
4. `requirements.txt` — ensure `requests` present for CDN downloads.
5. `train.py` (if present) or stub — consume `file-list-*.json` and do CDN-only fetches during DataLoader.

### Why this wins
- Eliminates `list_repo_files` recursive and `load_dataset(streaming=True)` on mixed-schema repos (past patterns).
- Bypasses HF API rate limits via CDN (THE KEY INSIGHT 2026-04-29).
- Deterministic sharding: `hash(slug) % 16 == SHARD_ID` so reruns are bitwise identical.
- Fits <2h: small scripts + one workflow annotation.

---

## File changes

### 1) bin/list-date-files.sh
```bash
#!/usr/bin/env bash
# Pre-flight: list files for a single date folder (non-recursive) and save JSON.
# Usage: list-date-files.sh YYYY-MM-DD [output.json]
# Requires: huggingface_hub (pip install huggingface_hub)
set -euo pipefail

REPO="datasets/axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="${2:-file-list-${DATE}.json}"

python3 - "$REPO" "$DATE" "$OUT" <<'PY'
import json, os, sys
from huggingface_hub import list_repo_tree

repo_id = sys.argv[1]
date_folder = sys.argv[2]
out_path = sys.argv[3]

# Non-recursive listing for the date folder
tree = list_repo_tree(repo_id=repo_id, path=date_folder, recursive=False)
files = [f.rfilename for f in tree if f.rfilename.endswith(('.jsonl', '.parquet', '.csv'))]

# Stable sort for deterministic ordering
files.sort()
payload = {
    "date": date_folder,
    "repo": repo_id,
    "files": files
}
with open(out_path, "w") as f:
    json.dump(payload, f, indent=2)
print(f"Wrote {len(files)} files to {out_path}")
PY

echo "✅ Saved file list to $OUT"
```

### 2) bin/dataset-enrich.sh (excerpt — core loop)
```bash
#!/usr/bin/env bash
# Worker shard: consume pre-computed file list and fetch via CDN.
# Usage: dataset-enrich.sh YYYY-MM-DD SHARD_ID TOTAL_SHARDS
set -euo pipefail

DATE="${1:-$(date +%Y-%m-%d)}"
SHARD_ID="${2:-0}"
TOTAL_SHARDS="${3:-16}"
REPO="datasets/axentx/surrogate-1-training-pairs"
FILE_LIST="file-list-${DATE}.json"

if [[ ! -f "$FILE_LIST" ]]; then
  echo "❌ File list $FILE_LIST not found. Run bin/list-date-files.sh $DATE first."
  exit 1
fi

mapfile -t FILES < <(python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
for f in data['files']:
    print(f)
" "$FILE_LIST")

# Deterministic shard assignment by filename slug
shard_files=()
for f in "${FILES[@]}"; do
  slug=$(basename "$f" | sed 's/\.[^.]*$//')
  h=$(python3 -c "import hashlib; print(int(hashlib.md5('$slug'.encode()).hexdigest(), 16))")
  if (( h % TOTAL_SHARDS == SHARD_ID )); then
    shard_files+=("$f")
  fi
done

echo "🔧 Shard $SHARD_ID processing ${#shard_files[@]} files"

# CDN download helper
download_cdn() {
  local rel_path="$1"
  local out="$2"
  curl -fsSL "https://huggingface.co/${REPO}/resolve/main/${rel_path}" -o "$out"
}

# Process each assigned file
mkdir -p "work/shard-${SHARD_ID}"
for rel_path in "${shard_files[@]}"; do
  fname=$(basename "$rel_path")
  tmp="work/shard-${SHARD_ID}/${fname}"
  echo "⬇️  CDN fetch $rel_path"
  download_cdn "$rel_path" "$tmp"

  # Project to {prompt,response} only at parse time (schema-agnostic)
  python3 lib/project-to-pairs.py "$tmp" "work/shard-${SHARD_ID}/${fname}.jsonl"
done

# Merge shard outputs
cat "work/shard-${SHARD_ID}"/*.jsonl > "batches/public-merged/${DATE}/shard-${SHARD_ID}-$(date +%H%M%S).jsonl"
echo "✅ Shard $SHARD_ID done"
```

### 3) lib/project-to-pairs.py (minimal schema-agnostic projector)
```python
#!/usr/bin/env python3
import json, sys, os, pyarrow as pa, pyarrow.parquet as pq, pyarrow.csv as pcsv

def extract_pairs(filepath, outpath):
    ext = os.path.splitext(filepath)[1].lower()
    pairs = []

    try:
        if ext == ".parquet":
            tbl = pq.read_table(filepath)
            cols = tbl.column_names
            # Heuristic: find prompt/response-like columns
            prompt_col = next((c for c in cols if "prompt" in c.lower()), cols[0] if cols else None)
            response_col = next((c for c in cols if "response" in c.lower()), cols[1] if len(cols) > 1 else None)
            if prompt_col and response_col:
                for batch in tbl.to_batches():
                    pc = batch.column(prompt_col).to_pylist()
                    rc = batch.column(response_col).to_pylist()
                    for p, r in zip(pc, rc):
                        if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                            pairs.append({"prompt": p.strip(), "response": r.strip()})
        elif ext == ".jsonl":
            with open(filepath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    p = obj.get("prompt") or (obj.get("input") or "")
                    r = obj.get("response") or (obj.get("output") or "")
                    if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                        pairs.append({"prompt": p.strip(), "response": r.strip()})
        elif ext == ".csv":
            tbl = pcsv.read_csv(filepath)
            cols = tbl.column_names
            prompt_col = next((c for c in cols if "prompt" in c.lower()), cols[0] if cols else None)
            response_col = next((c for c in cols if "response" in c.lower()), cols[1] if len(cols) > 1 else None)
            if prompt_col and response_col:
                pc = tbl.column(prompt_col).to_pylist()
                rc = tbl.column(response_col).to_pylist()
                for p, r in zip(pc, rc):
                    if isinstance(p, str) and isinstance(r, str) and p.strip() and r.strip():
                        pairs.append({"prompt": p.strip(), "response": r.strip()})
    except Exception as e:
        print(f"⚠️  Skipping {filepath}: {e}", file=sys.stderr)

    with open(outpath, "w") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    if len(sys.argv) !=

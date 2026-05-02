# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient.

### What to ship (3 files, ~130 lines total)

1. **`bin/list-shards.sh`** — one-time Mac/Linux script that calls `list_repo_tree` for today’s folder, saves `shard-manifest.json` mapping shard→files (deterministic hash-bucket assignment).
2. **`bin/dataset-enrich.sh`** — accept optional manifest path; if provided, use CDN URLs (`resolve/main/...`) instead of `load_dataset`; fall back to current behavior when absent.
3. **`requirements.txt`** — add `requests` and `pyarrow` if not present.

---

### 1) Pre-flight file-listing script (`bin/list-shards.sh`)

Run once per date folder (or after rate-limit clears). Produces `shard-manifest.json` that is embedded in training/ingest scripts.

```bash
#!/usr/bin/env bash
# bin/list-shards.sh
# Usage: HF_TOKEN=... ./bin/list-shards.sh [date] [--out FILE]
# Produces: shard-manifest.json
set -euo pipefail

REPO="datasets/axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="${2:-shard-manifest.json}"
SHARD_TOTAL="${SHARD_TOTAL:-16}"

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "ERROR: HF_TOKEN is required" >&2
  exit 1
fi

# Use Python for reliable JSON + HF API
python3 - "$DATE" "$OUT" "$SHARD_TOTAL" <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone
from huggingface_hub import HfApi

date_folder, out_path, shard_total = sys.argv[1], sys.argv[2], int(sys.argv[3])
api = HfApi()
repo = "datasets/axentx/surrogate-1-training-pairs"
prefix = f"{date_folder}/"

try:
    tree = api.list_repo_tree(
        repo_id=repo,
        path=prefix,
        recursive=False,
        repo_type="dataset",
    )
except Exception as e:
    print(f"ERROR listing repo tree: {e}", file=sys.stderr)
    sys.exit(1)

files = sorted(
    node.path
    for node in tree
    if node.type == "file" and node.path.lower().endswith((".jsonl", ".parquet"))
)

# Deterministic shard assignment by filename slug
def shard_for(path: str) -> int:
    slug = os.path.splitext(os.path.basename(path))[0]
    return hash(slug) % shard_total

shard_map = {}
for f in files:
    s = shard_for(f)
    shard_map.setdefault(str(s), []).append(f)

manifest = {
    "repo": repo,
    "date": date_folder,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "shard_total": shard_total,
    "shards": shard_map,
    "all_files": files,
}

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")

print(f"Wrote manifest for {len(files)} files across {len(shard_map)} shards to {out_path}")
PYEOF
```

Make executable:

```bash
chmod +x bin/list-shards.sh
```

---

### 2) Updated worker script (`bin/dataset-enrich.sh`)

Uses CDN-only fetch when manifest is provided; falls back to `load_dataset` when absent.

```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
#
# Worker script for GitHub Actions matrix shard.
#
# Modes:
#   - CDN mode (preferred): SHARD_MANIFEST=shard-manifest.json -> CDN-only, zero HF API
#   - Fallback mode: no manifest -> uses load_dataset (may hit 429s)
#
# Required env:
#   HF_TOKEN         - write token for axentx/surrogate-1-training-pairs
#   SHARD_ID         - 0..15
#   SHARD_TOTAL      - 16 (must match manifest if provided)
#   SHARD_MANIFEST   - optional path to shard-manifest.json
#   DATE_FOLDER      - date folder to process (default: today UTC)
#
# Produces:
#   batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl

set -euo pipefail

REPO="datasets/axentx/surrogate-1-training-pairs"
BASE_URL="https://huggingface.co/datasets/${REPO}/resolve/main"
WORKDIR=$(mktemp -d)
OUTDIR="batches/public-merged"

HF_TOKEN="${HF_TOKEN:-}"
SHARD_ID="${SHARD_ID:-0}"
SHARD_TOTAL="${SHARD_TOTAL:-16}"
SHARD_MANIFEST="${SHARD_MANIFEST:-}"
DATE_FOLDER="${DATE_FOLDER:-$(date -u +%Y-%m-%d)}"

if [[ -z "$HF_TOKEN" ]]; then
  echo "ERROR: HF_TOKEN is required" >&2
  exit 1
fi

mkdir -p "$OUTDIR/$DATE_FOLDER"
TIMESTAMP=$(date -u +%H%M%S)
OUTFILE="${OUTDIR}/${DATE_FOLDER}/shard${SHARD_ID}-${TIMESTAMP}.jsonl"

echo "[$(date -u)] Shard $SHARD_ID/$SHARD_TOTAL | date=$DATE_FOLDER | outfile=$OUTFILE"

# Lightweight projection to {prompt,response} + attribution in filename only
emit_row() {
  local src="$1" out="$2"
  python3 - "$src" "$out" <<'PYEOF'
import json, sys, os, pyarrow.parquet as pq

src, out = sys.argv[1], sys.argv[2]

def emit(obj, fh):
    prompt = obj.get("prompt") or obj.get("input") or obj.get("text") or ""
    response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
    if not isinstance(prompt, str):
        prompt = json.dumps(prompt, ensure_ascii=False)
    if not isinstance(response, str):
        response = json.dumps(response, ensure_ascii=False)
    row = {"prompt": prompt.strip(), "response": response.strip()}
    fh.write(json.dumps(row, ensure_ascii=False) + "\n")

with open(out, "a", encoding="utf-8") as fh:
    if src.lower().endswith(".jsonl"):
        with open(src, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    emit(json.loads(line), fh)
    elif src.lower().endswith(".parquet"):
        tbl = pq.read_table(src, columns=["prompt","response","input","output","text","completion"])
        df = tbl.to_pandas()
        for _, row in df.iterrows():
            emit(row.to_dict(), fh)
    else:
        print(f"Unsupported file: {src}", file=sys.stderr)
PYEOF
}

# ---- CDN mode (preferred) ----
if [[ -n "$SHARD_MANIFEST" && -f "$SHARD_MANIFEST" ]]; then
  echo "[$(date -u)] CDN mode: using manifest $SHARD_MANIFEST"

  # Validate shard_total matches
  manifest_total=$(python3 -c "import json; print(json.load(open('$SHARD_MANIFEST'))['shard_total'])")
  if [[ "$manifest_total" != "$SHARD_TOTAL" ]]; then
    echo "ERROR: SHARD_TOTAL mismatch: manifest=$manifest_total, env=$SHARD_TOTAL" >&2
    exit 1
  fi

  # Extract files assigned to this shard from manifest
  mapfile -t MY_FILES < <(python3 -c "
import json, sys
m = json.load(open('$SHARD_MANIFEST'))
for f in sorted(m['shards'].get(str($SHARD_ID), [])):
    print(f)
")

  if [[ ${#MY_FILES[@]} -eq 0 ]]; then
    echo "No files assigned to shard $SHARD_ID"
    exit 0
  fi

  processed=0
  for rel_path in "${MY_FILES

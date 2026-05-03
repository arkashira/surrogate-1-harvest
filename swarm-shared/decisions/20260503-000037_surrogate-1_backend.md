# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Add a pre-flight snapshot generator (`bin/snapshot.sh`) that lists all dataset files once per date folder, embeds the file manifest into training, and enables CDN-only downloads during parallel shard processing — eliminates HF API rate-limit pressure during ingestion and training.

### Why this now
- Current runners call `load_dataset`/`list_repo_files` repeatedly → 429s on big repos.
- CDN bypass (`resolve/main/`) has much higher limits and no auth overhead.
- Single deterministic file-list per date folder lets 16 shards stream with zero API calls during data load.
- Fits existing layout: `bin/`, reused by `dataset-enrich.sh` and training scripts.

---

## Concrete changes

### 1) Add `bin/snapshot.sh`
Lists one date folder (or latest if omitted), saves `file-list.json` with CDN-ready paths.

```bash
#!/usr/bin/env bash
# bin/snapshot.sh
# Usage:
#   HF_TOKEN=... ./bin/snapshot.sh <owner>/<dataset> <date-folder> [out.json]
#
# Example:
#   ./bin/snapshot.sh axentx/surrogate-1-training-pairs 2026-05-02 file-list.json

set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE="${2:-}"
OUT="${3:-file-list.json}"

if [ -z "$DATE" ]; then
  # pick latest date folder by name (YYYY-MM-DD)
  DATE=$(gh api "repos/${REPO}/contents/batches/public-merged" --paginate --jq '.[].name' | sort -r | head -1)
  echo "Latest date folder: $DATE"
fi

FOLDER="batches/public-merged/${DATE}"
echo "Listing ${REPO}/${FOLDER} ..."

# Single API call: non-recursive tree for the folder
gh api "repos/${REPO}/git/trees/${FOLDER}?recursive=false" \
  --paginate \
  --jq '{files: [.tree[] | select(.type=="blob") | .path]}' > "$OUT"

echo "Saved $(jq '.files | length' "$OUT") files to $OUT"
```

- Make executable: `chmod +x bin/snapshot.sh`
- Uses `gh` CLI (already present in Actions) with pagination; one API call total.
- Output is stable and CDN-ready: each path is relative to repo root.

---

### 2) Update `bin/dataset-enrich.sh` to accept file-list and use CDN
Modify worker to read `file-list.json` and stream via CDN URLs instead of `load_dataset(..., streaming=True)` on heterogeneous schemas.

Key snippet to add/replace in `dataset-enrich.sh`:

```bash
# If FILE_LIST is provided, use CDN-only mode
if [ -n "${FILE_LIST:-}" ] && [ -f "$FILE_LIST" ]; then
  echo "CDN-only mode: reading file list from $FILE_LIST"
  mapfile -t FILES < <(jq -r '.files[]' "$FILE_LIST")
else
  echo "Falling back to repo file listing (may hit API limits)"
  mapfile -t FILES < <(gh api "repos/${REPO}/contents/batches/public-merged/${DATE}" --paginate --jq '.[].path')
fi

for rel_path in "${FILES[@]}"; do
  # Skip files not in our deterministic shard
  if ! belongs_to_shard "$rel_path" "$SHARD_ID" "$TOTAL_SHARDS"; then
    continue
  fi

  url="https://huggingface.co/datasets/${REPO}/resolve/main/${rel_path}"
  echo "Processing shard ${SHARD_ID}: ${url}"

  # Download via CDN (no auth header needed for public datasets)
  tmp=$(mktemp)
  curl -fsSL --retry 3 --retry-delay 5 -o "$tmp" "$url"

  # Project to {prompt,response} only at parse time
  # (example for jsonl; adapt for parquet/json as needed)
  if [[ "$rel_path" == *.parquet ]]; then
    python -c "
import pyarrow.parquet as pq, json, sys
tbl = pq.read_table('$tmp')
for b in tbl.to_batches(max_chunksize=10000):
    df = b.to_pandas()
    for _, row in df.iterrows():
        print(json.dumps({'prompt': row.get('prompt',''), 'response': row.get('response','')}))
" 2>/dev/null | python "$PROJECTOR"
  elif [[ "$rel_path" == *.jsonl ]]; then
    jq -c '{prompt: .prompt // "", response: .response // ""}' < "$tmp" | python "$PROJECTOR"
  else
    echo "Skipping unsupported $rel_path"
  fi

  rm -f "$tmp"
done
```

- `PROJECTOR`: small inline Python that normalizes schema and emits `{prompt,response}` JSONL (keeps existing behavior).
- `belongs_to_shard`: deterministic hash on `rel_path` → `shard_id` (existing logic).

---

### 3) Update GitHub Actions matrix to pass file-list

In `.github/workflows/ingest.yml`, add a pre-step that generates the file list once and passes it to all shards:

```yaml
jobs:
  ingest:
    strategy:
      matrix:
        shard: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4

      - name: Generate file list (single job, reuse)
        if: matrix.shard == 0
        run: |
          ./bin/snapshot.sh axentx/surrogate-1-training-pairs "${DATE:-}" file-list.json
        env:
          DATE: ${{ vars.DATE_OVERRIDE || '' }}

      - name: Upload file list artifact
        if: matrix.shard == 0
        uses: actions/upload-artifact@v4
        with:
          name: file-list
          path: file-list.json

      - name: Download file list
        uses: actions/download-artifact@v4
        with:
          name: file-list
          path: .

      - name: Run shard
        run: |
          chmod +x bin/dataset-enrich.sh
          FILE_LIST=file-list.json \
            ./bin/dataset-enrich.sh \
              axentx/surrogate-1-training-pairs \
              "${DATE:-}" \
              ${{ matrix.shard }} \
              16
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
```

- Only shard 0 generates the list; others reuse the artifact → single API call per workflow run.
- `DATE` can be overridden via repo variable or workflow dispatch.

---

### 4) Add training-side usage note (README snippet)

Add to README:

```markdown
## Training with CDN-only file list

Generate a snapshot once per date folder:

```bash
./bin/snapshot.sh axentx/surrogate-1-training-pairs 2026-05-02 file-list.json
```

Embed `file-list.json` in your training container and use CDN URLs:

```python
import json, requests

with open("file-list.json") as f:
    files = json.load(f)["files"]

for path in files:
    url = f"https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/{path}"
    # stream and parse (no HF API calls)
```

This avoids HF API rate limits during training data loading.
```

---

## Acceptance criteria (manual test)

1. `./bin/snapshot.sh axentx/surrogate-1-training-pairs` → produces valid `file-list.json`.
2. `FILE_LIST=file-list.json ./bin/dataset-enrich.sh ... 0 16` runs without HF API list calls (verify via logs) and produces expected shard output.
3. Workflow run with matrix 0..15 completes without 429s (observe Actions logs).

---

## Time estimate

- `bin/snapshot.sh`: 15 min
- Update `dataset-enrich.sh`: 30–45 min (test locally)
- Update workflow: 15 min
- README + polish: 15 min

**Total**: ~1h 45m < 2h

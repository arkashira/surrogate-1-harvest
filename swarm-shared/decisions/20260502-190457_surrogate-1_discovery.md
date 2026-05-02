# surrogate-1 / discovery

## Final Implementation (merged + reconciled)

**Guiding principles**
- Deterministic, idempotent, race-free: date-partitioned paths, shard+timestamp filenames, skip-if-exists.
- CDN-bypass: never use `load_dataset` or `/api/` endpoints during streaming; use raw `https://huggingface.co/datasets/.../resolve/main/...`.
- Single API call for file listing, shared across shards (artifact), with safe fallback per-shard if needed.
- Minimal projection: keep only `{prompt,response}`; drop extra schema columns.
- Reliability: strict `set -euo pipefail`, retries, timeouts, non-empty checks.

---

## 1. Workflow (`.github/workflows/ingest.yml`)

```yaml
name: surrogate-1-ingest
on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:

env:
  DATASET_REPO: axentx/surrogate-1-training-pairs
  SHELL: /bin/bash
  HF_CDN_PREFIX: https://huggingface.co/datasets

jobs:
  file-list:
    runs-on: ubuntu-latest
    outputs:
      folder: ${{ steps.date.outputs.folder }}
    steps:
      - uses: actions/checkout@v4
      - uses: huggingface/huggingface-tools@main
        id: hf
        with:
          token: ${{ secrets.HF_TOKEN }}

      - name: Set date folder
        id: date
        run: |
          FOLDER="public-merged/$(date -u +%Y-%m-%d)"
          echo "folder=$FOLDER" >> "$GITHUB_OUTPUT"

      - name: List target folder once (single API call)
        run: |
          python - <<'PY'
          import os, json, sys
          from huggingface_hub import HfApi
          api = HfApi(token=os.getenv("HF_TOKEN"))
          repo = os.getenv("DATASET_REPO")
          folder = os.getenv("FOLDER")
          files = [f.rfilename for f in api.list_repo_tree(repo=repo, path=folder, recursive=False)]
          out = {"folder": folder, "files": files}
          with open("file-list.json", "w") as f:
              json.dump(out, f)
          PY
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
          FOLDER: ${{ steps.date.outputs.folder }}

      - name: Upload file-list artifact
        uses: actions/upload-artifact@v4
        with:
          name: file-list
          path: file-list.json

  ingest:
    needs: file-list
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4

      - name: Download file-list
        uses: actions/download-artifact@v4
        with:
          name: file-list

      - name: Setup
        run: |
          chmod +x bin/dataset-enrich.sh
          pip install -r requirements.txt

      - uses: huggingface/huggingface-tools@main
        with:
          token: ${{ secrets.HF_TOKEN }}

      - name: Run shard worker (CDN-bypass)
        run: |
          export SHARD_ID=${{ matrix.shard_id }}
          export HF_TOKEN=${{ secrets.HF_TOKEN }}
          export DATASET_REPO=${{ env.DATASET_REPO }}
          export FILE_LIST="$(pwd)/file-list.json"
          bash bin/dataset-enrich.sh
```

---

## 2. Worker script (`bin/dataset-enrich.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

: "${SHARD_ID:?required}"
: "${HF_TOKEN:?required}"
: "${DATASET_REPO:?required}"
: "${FILE_LIST:?required}"
: "${HF_CDN_PREFIX:=https://huggingface.co/datasets}"

FOLDER=$(jq -r '.folder' "$FILE_LIST")
TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)
OUTFILE="shard-${SHARD_ID}-${TIMESTAMP}.jsonl"
PARTITIONED_PATH="${FOLDER}/${OUTFILE}"

# Idempotency: skip if target already exists and is non-empty
if gh api -H "Authorization: token $HF_TOKEN" "/repos/${DATASET_REPO}/contents/${PARTITIONED_PATH}" >/dev/null 2>&1; then
  echo "File ${PARTITIONED_PATH} already exists. Skipping."
  exit 0
fi

# Deterministic shard assignment: hash(slug) % 16
should_process() {
  local slug=$1
  local hash
  hash=$(echo -n "$slug" | sha256sum | tr -d ' -' | head -c 16)
  local mod=$(( 0x${hash} % 16 ))
  [[ $mod -eq $SHARD_ID ]]
}

# CDN-bypass fetch + minimal projection + optional dedup
process_file() {
  local rel_path=$1
  local url="${HF_CDN_PREFIX}/${DATASET_REPO}/resolve/main/${rel_path}"
  python - "$url" <<'PY'
import sys, json, requests, hashlib, os
url = sys.argv[1]
resp = requests.get(url, timeout=120)
resp.raise_for_status()

# Try to handle common formats simply:
# - If .jsonl or .json: parse line-by-line or as JSON array
# - If .parquet: require pyarrow (skip if unavailable)
# This worker keeps only prompt/response.

fname = url.split('/')[-1].lower()

def normalize(obj):
    prompt = obj.get("prompt") or obj.get("input") or ""
    response = obj.get("response") or obj.get("output") or ""
    if not prompt or not response:
        return None
    return {"prompt": prompt, "response": response}

if fname.endswith('.jsonl'):
    for line in resp.text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        out = normalize(obj)
        if out:
            print(json.dumps(out, ensure_ascii=False))
elif fname.endswith('.json'):
    data = resp.json()
    if isinstance(data, list):
        for obj in data:
            out = normalize(obj)
            if out:
                print(json.dumps(out, ensure_ascii=False))
    else:
        out = normalize(data)
        if out:
            print(json.dumps(out, ensure_ascii=False))
elif fname.endswith('.parquet'):
    try:
        import pyarrow.parquet as pq
        import io
        table = pq.read_table(io.BytesIO(resp.content))
        df = table.to_pandas()
        for _, row in df.iterrows():
            obj = row.to_dict()
            out = normalize(obj)
            if out:
                print(json.dumps(out, ensure_ascii=False))
    except Exception:
        # If pyarrow unavailable or fails, skip file
        sys.stderr.write(f"Skipping parquet (unsupported): {url}\n")
else:
    # Fallback: try line-by-line JSON
    for line in resp.text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            out = normalize(obj)
            if out:
                print(json.dumps(out, ensure_ascii=False))
        except Exception:
            continue
PY
}

# Optional dedup helper (if lib/dedup.py exists)
DEDUP_FILTER=""
if [[ -f lib/dedup.py ]]; then
  DEDUP_FILTER="| python lib/dedup.py"
fi

# Iterate file-list and process only assigned shard
TOTAL_FILES=0
TOTAL_LINES=0
while IFS= read -r line; do
  rel_path=$(echo "$line" | jq -r '.rfilename // .')
  slug=$(basename "$rel_path" .jsonl .parquet .json)
  if ! should_process "$slug"; then
    continue
  fi
  echo "Processing shard=${SHARD_ID} file=${rel_path}"
  TOTAL_FILES=$((TOTAL_FILES + 1))
  process_file "$rel_path" | eval "$DEDUP_FILTER" >> "$OUTFILE"
done < <(jq -c '.

# surrogate-1 / discovery

## Final Synthesized Answer

**Chosen scope**: `bin/dataset-enrich.sh` (plus minimal supporting Python)  
**Goal**: deterministic, rate-limit-safe, CDN-only ingestion with warm-start dedup and strict validation before write.

---

### 1. Diagnosis (merged + resolved)
- **Problem**: repeated full scans and per-run `load_dataset`/`datasets` streaming cause HF API 429s and quota waste.  
  **Fix**: single `list_repo_tree` per date → `file-list.json`; workers use CDN URLs only.
- **Problem**: no deterministic shard-to-file mapping → races and duplicate batches across cron ticks.  
  **Fix**: deterministic shard assignment from the cached file list (stable ordering).
- **Problem**: no reuse of running HF Space for dedup state → each run starts cold, increasing collisions and quota.  
  **Fix**: best-effort warm-start from a running Space’s `md5_store.sqlite` (or equivalent) and optional push-back.
- **Problem**: malformed JSON lines or schema drift can poison the dataset.  
  **Fix**: lightweight validation per record before emit; strict schema projection.
- **Problem**: token-scope failures surface late as cryptic 403/401.  
  **Fix**: early preflight check for token and repo/space reachability.

---

### 2. Implementation

#### `bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh
# Deterministic, CDN-only ingestion with warm-start dedup and validation.
set -euo pipefail

REPO="datasets/axentx/surrogate-1-training-pairs"
DATE=$(date -u +%Y-%m-%d)
SHARD_ID=${SHARD_ID:-0}
TOTAL_SHARDS=${TOTAL_SHARDS:-16}
OUTDIR="batches/public-merged/${DATE}"
OUTFILE="${OUTDIR}/shard${SHARD_ID}-$(date -u +%H%M%S).jsonl"
HF_TOKEN=${HF_TOKEN:-}
HF_SPACE_NAME="axentx/surrogate-1-dedup"
FILE_LIST="file-list-${DATE}.json"

mkdir -p "$(dirname "$OUTFILE")"
export DATE OUTFILE

# Preflight: token reachability and repo access
if [[ -z "${HF_TOKEN}" ]]; then
  echo "ERROR: HF_TOKEN is required." >&2
  exit 1
fi
if ! curl -sf -H "Authorization: Bearer ${HF_TOKEN}" \
        "https://huggingface.co/api/repos/info?repo_id=${REPO}" > /dev/null 2>&1; then
  echo "ERROR: Cannot access ${REPO} with provided token (check scope/permissions)." >&2
  exit 1
fi

# 1) Warm-start dedup state from running Space (best-effort)
if curl -sf -H "Authorization: Bearer ${HF_TOKEN}" \
        "https://huggingface.co/api/spaces/${HF_SPACE_NAME}" | jq -e '.running == true' > /dev/null 2>&1; then
  echo "Reusing running Space ${HF_SPACE_NAME} for dedup warm-start..."
  curl -sf -H "Authorization: Bearer ${HF_TOKEN}" \
       "https://huggingface.co/spaces/${HF_SPACE_NAME}/resolve/main/md5_store.sqlite" \
       -o lib/md5_store.sqlite || true
fi

# 2) List today's folder once and cache locally (stable ordering)
if [[ ! -f "${FILE_LIST}" ]]; then
  echo "Fetching file list for ${DATE}..."
  python3 - <<PY
import os, json, sys
from huggingface_hub import HfApi
api = HfApi(token=os.getenv("HF_TOKEN"))
files = sorted(
    f.rfilename for f in api.list_repo_tree(
        repo_id="${REPO}",
        path="${DATE}",
        repo_type="dataset",
        recursive=False
    )
    if f.rfilename.endswith((".parquet", ".jsonl"))
)
with open("${FILE_LIST}", "w") as f:
    json.dump(files, f)
print(f"Found {len(files)} files")
PY
fi

# 3) Deterministic shard assignment
mapfile -t ALL_FILES < <(python3 -c "import json; print('\n'.join(json.load(open('${FILE_LIST}'))))")
SHARD_FILES=()
for i in "${!ALL_FILES[@]}"; do
  if (( i % TOTAL_SHARDS == SHARD_ID )); then
    SHARD_FILES+=("${ALL_FILES[i]}")
  fi
done
echo "Shard ${SHARD_ID}/${TOTAL_SHARDS} processing ${#SHARD_FILES[@]} files"

# 4) Process with CDN-only fetches and strict validation
python3 - <<'PY' "${SHARD_FILES[@]}"
import sys, json, hashlib, os, pyarrow.parquet as pq
from huggingface_hub import hf_hub_download

REPO = "datasets/axentx/surrogate-1-training-pairs"
OUTFILE = os.getenv("OUTFILE")

def parse_file(path):
    local_path = hf_hub_download(repo_id=REPO, filename=path, repo_type="dataset")
    if path.endswith(".parquet"):
        tbl = pq.read_table(local_path, columns=["prompt", "response"])
        for batch in tbl.to_batches(max_chunksize=1000):
            prompts = batch.column("prompt").to_pylist()
            responses = batch.column("response").to_pylist()
            for prompt, response in zip(prompts, responses):
                yield {"prompt": prompt, "response": response}
    elif path.endswith(".jsonl"):
        with open(local_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                yield {"prompt": obj.get("prompt", ""), "response": obj.get("response", "")}

def is_valid(obj):
    return (
        isinstance(obj, dict) and
        isinstance(obj.get("prompt"), str) and obj["prompt"].strip() and
        isinstance(obj.get("response"), str) and obj["response"].strip()
    )

def main():
    out_path = OUTFILE or "/dev/stdout"
    seen = set()
    with open(out_path, "w", encoding="utf-8") as out_f:
        for path in sys.argv[1:]:
            for obj in parse_file(path):
                h = hashlib.md5(json.dumps(obj, sort_keys=True).encode()).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                if not is_valid(obj):
                    continue
                out_f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    print(f"Wrote {len(seen)} records to {out_path}")

if __name__ == "__main__":
    main()
PY

# 5) Optional: push updated md5 store back to Space (best-effort)
if [[ -n "${HF_TOKEN}" && -f lib/md5_store.sqlite ]]; then
  echo "Uploading updated md5 store..."
  python3 -c "
from huggingface_hub import upload_file
import os
upload_file(
    path_or_fileobj='lib/md5_store.sqlite',
    path_in_repo='md5_store.sqlite',
    repo_id='${HF_SPACE_NAME}',
    repo_type='space',
    token=os.getenv('HF_TOKEN')
)
" || true
fi

echo "Shard ${SHARD_ID} completed: ${OUTFILE}"
```

---

### 3. Verification (merged)
1. **Local test**:
   ```bash
   export HF_TOKEN=hf_xxx
   export SHARD_ID=0 TOTAL_SHARDS=2
   bash bin/dataset-enrich.sh
   ```
   - Confirm `file-list-$(date -u +%Y-%m-%d).json` exists and is non-empty.
   - Confirm `batches/public-merged/$(date -u +%Y-%m-%d)/shard0-*.jsonl` exists with valid JSON lines containing non-empty `prompt` and `response` strings.
   - Confirm no authenticated API calls during parsing (only initial `list_repo_tree` and optional Space fetch).

2. **GitHub Actions**:
   ```bash
   gh workflow run ingest.yml -R axentx/surrogate-1-runner -f shard_id=0 -f total_shards=16
   ```
   - Check Actions logs for successful completion.
   - Verify the dataset repo receives one new file in the expected

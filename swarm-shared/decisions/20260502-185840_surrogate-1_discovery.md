# surrogate-1 / discovery

**Final synthesized plan (highest correctness + concrete actionability)**

- **Deterministic date-partitioned output**  
  Use UTC run date `YYYY/MM/DD` in the path so re-runs append instead of overwrite and training data history is stable.

- **Single pre-flight file list + CDN-only fetches**  
  Generate `file-list.json` once per workflow (not per shard) via `list_repo_tree` for the target date folder; workers download this artifact and stream files directly from CDN (`resolve/main/...`). This removes repeated HF API calls, avoids auth/rate-limit issues, and guarantees all shards see the same snapshot.

- **Deterministic, disjoint shard assignment**  
  Each worker hashes `relpath` (e.g., MD4/MD5 truncated) modulo 16 and processes only files assigned to its `SHARD_ID`. This is stable across runs and removes redundant work.

- **Schema/traceability + robustness**  
  Emit `source_file`, `ingest_ts` (and keep any existing `prompt`/`response` schema). Add retries with jitter for CDN downloads, strict bash hygiene (`#!/usr/bin/env bash`, `set -euo pipefail`), and fast-fail if `SHARD_ID` is invalid.

- **Dedup**  
  Keep existing `lib/dedup.py` (central md5 store) for cross-run dedup; workers can skip persisting records whose hashes are already known to reduce wasted writes.

---

**Implementation plan (concrete, ≤2h)**

1. **Workflow changes** (`.github/workflows/ingest.yml`)
   - Compute `RUN_DATE` and `PARTITION` (YYYY/MM/DD) once.
   - Add a pre-flight step that lists the target `PARTITION` folder (non-recursive) via `huggingface_hub.HfApi.list_repo_tree`, writes `file-list.json`, and uploads it as an artifact.
   - Matrix job for 16 shards, passing `SHARD_ID`, `RUN_DATE`, `PARTITION`, and making the artifact available to all shards.

2. **Worker script** (`bin/dataset-enrich.sh`)
   - Validate `SHARD_ID` in 0..15 and required env.
   - Download `file-list.json` artifact (or use local if present).
   - Deterministic shard assignment: `hash(relpath) % 16 == SHARD_ID`.
   - For each assigned file:
     - Build CDN URL: `https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<relpath>`.
     - Download with retries (3×, exponential/jitter) and stream-parse to `{prompt, response, source_file, ingest_ts}`.
     - Skip if dedup store already contains hash (optional per-worker fast skip).
     - Append to `batches/public-merged/${PARTITION}/shard${SHARD_ID}-${RUN_DATE}-${HHMMSS}.jsonl`.
   - Use `#!/usr/bin/env bash`, `set -euo pipefail`, `chmod +x`.

3. **Dedup** (`lib/dedup.py`)
   - No functional change; continue using central md5 store. Workers may call it to check/insert hashes.

---

**Code snippets**

`.github/workflows/ingest.yml` (excerpt)
```yaml
env:
  RUN_DATE: ${{ steps.date.outputs.run_date }}
  PARTITION: ${{ steps.date.outputs.partition }}

steps:
  - name: Compute date partition
    id: date
    run: |
      echo "run_date=$(date -u +%Y-%m-%d)" >> $GITHUB_OUTPUT
      echo "partition=$(date -u +%Y/%m/%d)" >> $GITHUB_OUTPUT

  - name: Generate file-list (once per workflow)
    id: filelist
    run: |
      python - <<'PY'
      import os, json
      from huggingface_hub import HfApi
      api = HfApi()
      repo = "axentx/surrogate-1-training-pairs"
      partition = os.getenv("PARTITION")
      items = api.list_repo_tree(repo, path=partition, recursive=False)
      files = [i.rfilename for i in items if i.type == "file"]
      with open("file-list.json", "w") as f:
          json.dump(files, f)
      print(f"Listed {len(files)} files for {partition}")
      PY

  - name: Upload file-list artifact
    uses: actions/upload-artifact@v4
    with:
      name: file-list-${{ env.PARTITION }}
      path: file-list.json

  - name: Run shards
    uses: matrix-job
    with:
      matrix: '{"shard": [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]}'
    env:
      SHARD_ID: ${{ matrix.shard }}
      RUN_DATE: ${{ env.RUN_DATE }}
      PARTITION: ${{ env.PARTITION }}
```

`bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

# Required env
: "${SHARD_ID:?}"
: "${PARTITION:?}"
: "${RUN_DATE:?}"

REPO="axentx/surrogate-1-training-pairs"
OUTDIR="batches/public-merged/${PARTITION}"
TS=$(date -u +%H%M%S)
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${RUN_DATE}-${TS}.jsonl"

mkdir -p "$(dirname "$OUTFILE")"

# Validate shard index
if [[ "${SHARD_ID}" -lt 0 || "${SHARD_ID}" -gt 15 ]]; then
  echo "ERROR: SHARD_ID must be 0..15" >&2
  exit 1
fi

# Expect file-list.json to be present (downloaded artifact)
LIST="file-list.json"
if [[ ! -f "${LIST}" ]]; then
  echo "ERROR: ${LIST} not found" >&2
  exit 1
fi

TOTAL=$(jq 'length' "${LIST}")
echo "Shard ${SHARD_ID} processing ${TOTAL} files from ${LIST}"

# Deterministic assignment: hash(filename) % 16
assign_shard() {
  local relpath="$1"
  local hash
  hash=$(echo -n "$relpath" | md5sum | cut -c1-8)
  echo $(( 0x${hash} % 16 ))
}

# CDN download with retry
cdn_fetch() {
  local url="$1"
  local max=3
  local attempt=0
  local code=0
  while (( attempt < max )); do
    if (( attempt > 0 )); then
      sleep $(( 2 ** attempt + RANDOM % 3 ))
    fi
    if curl -fsSL --retry 2 --retry-delay 1 --max-time 30 "$url"; then
      return 0
    fi
    code=$?
    attempt=$(( attempt + 1 ))
  done
  echo "ERROR: failed to fetch $url (exit $code)" >&2
  return $code
}

# Process one file into prompt/response + metadata
process_relpath() {
  local relpath="$1"
  local url="https://huggingface.co/datasets/${REPO}/resolve/main/${relpath}"
  local ingest_ts
  ingest_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  # Stream-parse file into {prompt,response} (placeholder: adapt to actual format)
  # Example for JSONL files:
  #   cdn_fetch "$url" | jq -c --arg sf "$relpath" --arg ts "$ingest_ts" \
  #     '{prompt: .prompt, response: .response, source_file: $sf, ingest_ts: $ts}'
  #
  # For this PR, keep a minimal placeholder that records metadata and source.
  # Replace the block below with real parsing logic for your dataset format.
  cdn_fetch "$url" >/dev/null # consume bytes; real impl would parse
  echo "{\"prompt\":\"<placeholder>\",\"response\":\"<placeholder>\",\"source_file\":\"${relpath}\",\"ingest_ts\":\"${ingest_ts}\"}"
}

export -f assign_shard cdn_fetch process_relpath
export SHARD_ID

count=0
skipped=0
jq -r '.[]' "${LIST}" | while read -r f; do
  target=$(assign_shard

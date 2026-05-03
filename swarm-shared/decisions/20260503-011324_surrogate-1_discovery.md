# surrogate-1 / discovery

## Final Synthesis (Best Parts + Correctness + Actionability)

Below is the single, production-ready plan that merges the strongest elements of both proposals, resolves contradictions, and prioritizes correctness and concrete actionability.

---

## Goal (unchanged)
Eliminate HF API rate-limit failures and HF Space OOM by replacing recursive `list_repo_files` and per-file API calls with **one per-folder `list_repo_tree` + CDN-only fetches**, and project to `{prompt,response}` at parse time—while preserving 16-shard parallelism.

---

## Core Decisions (Resolved Contradictions)
1. **Manifest strategy**: generate once per date folder and **commit to repo** (not ephemeral CI artifact).  
   - Why: both proposals agree this removes auth-bound API calls during shard runs; committing ensures reproducibility and fallback safety.

2. **Download method**: prefer **CDN direct fetch** (`resolve/main/...`) for files; keep `hf_hub_download` only as fallback for private repos.  
   - Why: CDN bypasses `/api/` rate limits and is faster; both proposals prefer this.

3. **Projection**: strict `{prompt,response}` only; drop all other fields at parse time to bound memory.  
   - Why: prevents OOM and schema drift; both proposals require this.

4. **No `load_dataset(streaming=True)` on full repo**: use per-file streaming decode instead.  
   - Why: avoids pyarrow CastError on heterogeneous repos; both proposals reject full-repo streaming.

5. **Shard assignment**: deterministic slice of manifest file list (not by file size or content hash) to keep logic simple and reproducible.  
   - Why: Candidate 1’s per-shard slicing is simpler and sufficient; Candidate 2’s size-based weighting adds complexity with little gain for this workload.

---

## Implementation Plan (≤2h)

### 1) Manifest generator (run once per date folder)
- Run on dev machine or in a short pre-job.
- Use `list_repo_tree(..., recursive=False)` for `public-merged/YYYY-MM-DD`.
- Save `manifests/YYYY-MM-DD.json` and **commit to repo**.
- Schema:
  ```json
  {
    "repo_id": "axentx/surrogate-1-training-pairs",
    "date": "YYYY-MM-DD",
    "root": "public-merged/YYYY-MM-DD",
    "files": ["rel/path1.parquet", "rel/path2.jsonl", ...]
  }
  ```

### 2) GitHub Actions (`ingest.yml`) changes
- Add a step before the matrix to compute `MANIFEST_URL` (CDN raw URL) and `MANIFEST_PATH` (repo-relative).
- Pass `MANIFEST_URL` and `TODAY` to each shard job.
- Keep the 16-shard matrix unchanged.
- Optional: add a pre-flight check that the manifest exists; fail fast if not.

### 3) Update `bin/dataset-enrich.sh`
- Accept `MANIFEST_URL` (preferred) or fall back to repo tree with warning.
- Download manifest via CDN (`curl`).
- Deterministically assign shard slice:
  - `TOTAL=${#FILES[@]}`
  - `PER_SHARD=$(( (TOTAL + TOTAL_SHARDS - 1) / TOTAL_SHARDS ))`
  - `START=$(( SHARD_ID * PER_SHARD ))`
  - `END=$(( START + PER_SHARD ))` (clamp to `TOTAL`)
- For each assigned file:
  - Fetch via CDN (`curl`) to temp file.
  - Stream-parse with projection helper.
  - Append `{prompt,response}` to shard output.
  - Clean up temp file immediately.
- Stream output to `batches/public-merged/YYYY-MM-DD/shard-<ID>-<TS>.jsonl`.

### 4) Python projection helper (`tools/parse_and_project.py`)
- Support `.parquet` and `.jsonl`.
- For `.parquet`: use `pyarrow.parquet.ParquetFile` with `iter_batches` to bound memory.
- For `.jsonl`: line-by-line streaming.
- Projection:
  - `prompt` = first non-empty of `prompt`, `input`, `text`.
  - `response` = first non-empty of `response`, `output`, `completion`.
  - Drop rows where either is empty.
  - Emit one JSON object per line (no extra fields).
- Robustness:
  - Skip malformed lines/batches with warnings to stderr.
  - Do not crash on single bad file.

### 5) Validation checklist (run locally before merge)
- Generate manifest for one date folder.
- Run one shard locally with `MANIFEST_URL` pointing to local file or CDN.
- Verify:
  - No 429s during listing or downloads.
  - Peak memory per shard < 4 GB.
  - Output lines are valid JSON with exactly `{prompt, response}`.
  - All assigned files are processed exactly once.

---

## Code Snippets (Merged + Corrected)

### Manifest generator (`tools/gen_manifest.py`)
```python
#!/usr/bin/env python3
import json
from datetime import date
from huggingface_hub import list_repo_tree

REPO_ID = "axentx/surrogate-1-training-pairs"
TODAY = str(date.today())
FOLDER = f"public-merged/{TODAY}"

entries = list_repo_tree(REPO_ID, path=FOLDER, recursive=False)
files = [e["path"] for e in entries if e["type"] == "file"]
files.sort()

manifest = {
    "repo_id": REPO_ID,
    "date": TODAY,
    "root": FOLDER,
    "files": files
}

out_path = f"manifests/{TODAY}.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, ensure_ascii=False)

print(f"Wrote {len(files)} files to {out_path}")
```

### `bin/dataset-enrich.sh` (core)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ID="${REPO_ID:-axentx/surrogate-1-training-pairs}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
MANIFEST_URL="${MANIFEST_URL:-}"
TODAY="${TODAY:-$(date +%F)}"
WORKDIR="$(mktemp -d)"
OUTDIR="batches/public-merged/${TODAY}"
TIMESTAMP="$(date +%H%M%S)"
OUTFILE="${OUTDIR}/shard-${SHARD_ID}-${TIMESTAMP}.jsonl"

mkdir -p "$(dirname "$OUTFILE")"

cleanup() {
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

fetch_cdn() {
  local repo="$1" path="$2" out="$3"
  curl -fsSL --retry 3 "https://huggingface.co/datasets/${repo}/resolve/main/${path}" -o "$out"
}

# Load manifest
if [[ -n "$MANIFEST_URL" ]]; then
  MANIFEST_PATH="${WORKDIR}/manifest.json"
  curl -fsSL --retry 3 "$MANIFEST_URL" -o "$MANIFEST_PATH"
else
  echo "WARNING: MANIFEST_URL not set — falling back to repo tree (may hit rate limits)"
  MANIFEST_PATH="${WORKDIR}/manifest.json"
  python -c "
import json, sys
from huggingface_hub import list_repo_tree
repo = '${REPO_ID}'
folder = 'public-merged/${TODAY}'
files = [e['path'] for e in list_repo_tree(repo, path=folder, recursive=False) if e['type'] == 'file']
files.sort()
with open(sys.argv[1], 'w') as f:
    json.dump({'repo_id': repo, 'date': '${TODAY}', 'root': folder, 'files': files}, f)
" "$MANIFEST_PATH"
fi

mapfile -t ALL_FILES < <(jq -r '.files[]' "$MANIFEST_PATH")
TOTAL=${#ALL_FILES[@]}
if (( TOTAL == 0 )); then
  echo "No files found in manifest"
  exit 1
fi

PER_SHARD=$(( (TOTAL + TOTAL_SHARDS - 1) / TOTAL_SHARDS ))
START=$(( SHARD_ID * PER_SHARD ))
END=$(( START + PER_SHARD ))
if (( END > TOTAL )); then END=$TOTAL; fi

echo "Shard $SHARD

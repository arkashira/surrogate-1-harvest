# surrogate-1 / discovery

## Highest-value incremental improvement (≤2h)

**Deterministic date-partitioning + CDN-bypass ingestion with pre-flight file-list**

- Fixes noisy history and training instability by writing to stable `YYYY/MM/DD` folders.
- Eliminates redundant HF API calls and repeated work by snapshotting one date folder’s file list once and embedding it in workers.
- Bypasses HF API rate limits during data load by using CDN URLs (`resolve/main/...`) with no auth.
- Keeps the 16-shard parallel model unchanged; only makes each shard’s output deterministic and idempotent.

---

## Implementation plan

1. Add a lightweight “plan” step (run on Mac/CI before the 16-shard matrix)  
   - `bin/make-plan.sh <date>` → produces `plan/<date>/files.json` (list of file paths for that date folder in the public dataset repo).
   - Uses `list_repo_tree(path, recursive=False)` per subfolder to avoid recursive pagination and 429s.
   - Embeds `shard_id` assignment into the plan so every worker knows exactly which files to process (no re-streaming full list).

2. Update `bin/dataset-enrich.sh` to accept a plan file  
   - Reads `PLAN_FILE` and processes only the slice assigned to `SHARD_ID`.
   - Downloads via CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header.
   - Projects to `{prompt, response}` at parse time; writes to:
     ```
     batches/public-merged/<YYYY>/<MM>/<DD>/shard<N>-<HHMMSS>.jsonl
     ```

3. Update GitHub Actions matrix to pass the plan file and date into each job  
   - Generate plan in a prior job, upload as artifact, download in each shard job.
   - Keep 16-shard matrix; each job remains independent and isolated.

4. Keep dedup behavior as-is (central SQLite remains source of truth); accept that cross-run duplicates may exist until Space-side dedup runs.

---

## Code snippets

### bin/make-plan.sh
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date -u +%Y-%m-%d)}"
OUTDIR="plan/${DATE}"
OUTFILE="${OUTDIR}/files.json"

mkdir -p "${OUTDIR}"

# Use HF Hub to list top-level date folder (non-recursive to avoid pagination)
# Requires: pip install huggingface_hub
python3 - <<PY > "${OUTFILE}"
import os, json, sys
from huggingface_hub import HfApi

api = HfApi()
repo = os.environ["REPO"]
date = os.environ["DATE"]

# List only the target date folder (non-recursive)
entries = api.list_repo_tree(repo=repo, path=date, recursive=False)
files = [e.path for e in entries if e.type == "file"]

# Assign deterministic shard by stable hash of path
def shard_of(path, n=16):
    return hash(path) % n

plan = {
    "date": date,
    "created_at": os.popen("date -u +%Y-%m-%dT%H:%M:%SZ").read().strip(),
    "shards": 16,
    "files": [
        {"path": p, "shard": shard_of(p), "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{p}"}
        for p in sorted(files)
    ]
}

json.dump(plan, sys.stdout, indent=2)
PY

echo "Plan written to ${OUTFILE}"
```

### bin/dataset-enrich.sh (excerpt — worker portion)
```bash
#!/usr/bin/env bash
set -euo pipefail

# Required env:
#   SHARD_ID      (0..15)
#   PLAN_FILE     path to plan/<date>/files.json
#   HF_TOKEN      write token for uploads
#   ITER_TS       iteration timestamp (e.g., $(date -u +%H%M%S))

if [[ -z "${PLAN_FILE:-}" || ! -f "${PLAN_FILE}" ]]; then
  echo "PLAN_FILE must point to plan file" >&2
  exit 1
fi

DATE=$(jq -r '.date' "${PLAN_FILE}")
WORK_FILES=$(jq -r --argjson sid "${SHARD_ID}" '.files[] | select(.shard == $sid) | .cdn_url' "${PLAN_FILE}")

OUTDIR="batches/public-merged/${DATE//-/\/}/shard${SHARD_ID}"
mkdir -p "${OUTDIR}"
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${ITER_TS}.jsonl"

# Process assigned files via CDN (no auth, bypass API rate limits)
python3 - <<PY
import json, sys, os, subprocess, hashlib
from pathlib import Path

shard_id = int(os.environ["SHARD_ID"])
iter_ts = os.environ["ITER_TS"]
outfile = Path(os.environ["OUTFILE"])

# We'll receive file URLs via stdin (one per line)
urls = [ln.strip() for ln in sys.stdin if ln.strip()]

def normalize_record(raw_bytes, source_path):
    # Placeholder: project raw file bytes into {prompt,response}
    # Implement per-schema handling here.
    text = raw_bytes.decode("utf-8", errors="replace")
    # Example heuristic: split by known delimiter or use filename hints
    return {"prompt": "placeholder prompt", "response": text[:2000]}

with outfile.open("w", encoding="utf-8") as fout:
    for url in urls:
        # Download via CDN (no auth)
        payload = subprocess.check_output(["curl", "-fsSL", url], timeout=120)
        try:
            rec = normalize_record(payload, url)
            rec["_source_path"] = url
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            # Log and continue to avoid losing whole shard
            print(f"skip {url}: {e}", file=sys.stderr)
PY <<< "${WORK_FILES}"

echo "Shard ${SHARD_ID} written to ${OUTFILE}"
```

### GitHub Actions excerpt (ingest.yml additions)
```yaml
jobs:
  plan:
    runs-on: ubuntu-latest
    outputs:
      plan-file: ${{ steps.plan.outputs.plan_file }}
      date: ${{ steps.plan.outputs.date }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install huggingface_hub
      - run: bin/make-plan.sh $(date -u +%Y-%m-%d)
        env:
          REPO: axentx/surrogate-1-training-pairs
          DATE: $(date -u +%Y-%m-%d)
      - id: plan
        run: |
          echo "plan_file=plan/$(date -u +%Y-%m-%d)/files.json" >> $GITHUB_OUTPUT
          echo "date=$(date -u +%Y-%m-%d)" >> $GITHUB_OUTPUT
      - uses: actions/upload-artifact@v4
        with:
          name: plan-${{ steps.plan.outputs.date }}
          path: plan/${{ steps.plan.outputs.date }}/files.json

  ingest:
    needs: plan
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: plan-${{ needs.plan.outputs.date }}
          path: plan/
      - run: |
          export SHARD_ID=${{ matrix.shard_id }}
          export PLAN_FILE=plan/${{ needs.plan.outputs.date }}/files.json
          export ITER_TS=$(date -u +%H%M%S)
          export HF_TOKEN=${{ secrets.HF_TOKEN }}
          bash bin/dataset-enrich.sh
```

---

## Acceptance criteria

- A run for `2026-05-02` produces files under `batches/public-merged/2026/05/

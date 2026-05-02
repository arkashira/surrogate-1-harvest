# surrogate-1 / discovery

## Final Consolidated Solution

### 1. Diagnosis (merged)
- **No deterministic date-partitioning**: re-runs overwrite or duplicate outputs instead of appending to stable `YYYY/MM/DD` folders.
- **No pre-flight file-list**: workers call live HF APIs (`list_repo_tree`/`datasets` streaming) inside every shard, risking 429s and wasting quota.
- **No CDN-only data path**: training and ingestion still hit authenticated `/api/` endpoints instead of public CDN URLs.
- **No shard isolation/retry**: partial shard failures can commit incomplete data; no per-shard exponential backoff or idempotent filenames.
- **No compute reuse hand-off**: ingestion produces shards but does not emit a training-ready config so Lightning Studio can reuse running quota/state.

### 2. Single Proposed Change
Add a **pre-scan → CDN-bypass → deterministic shard output → training config** pipeline:
- One pre-scan job produces `file-list.json` for a deterministic `YYYY/MM/DD` folder.
- Shards download that artifact, process only assigned files, write idempotent `shard-<N>-<date>-<HHMMSS>.jsonl`, and use **only CDN URLs**.
- Emit `train-cdn-config.json` so downstream training uses zero HF API calls.

---

### 3. Implementation

#### `.github/workflows/ingest.yml`
```yaml
name: ingest

on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:
    inputs:
      date_override:
        description: 'Optional date (YYYY-MM-DD) to backfill'
        required: false
        default: ''

env:
  HF_REPO: axentx/surrogate-1-training-pairs
  N_SHARDS: 16

jobs:
  prelist:
    runs-on: ubuntu-latest
    outputs:
      run_date: ${{ steps.date.outputs.run_date }}
      folder: ${{ steps.date.outputs.folder }}
    steps:
      - uses: actions/checkout@v4

      - name: Set run date and folder
        id: date
        run: |
          if [ -n "${{ github.event.inputs.date_override }}" ]; then
            RUN_DATE="${{ github.event.inputs.date_override }}"
          else
            RUN_DATE=$(date -u +%Y-%m-%d)
          fi
          FOLDER=$(echo "$RUN_DATE" | sed 's/-/\//g')
          echo "run_date=$RUN_DATE" >> $GITHUB_OUTPUT
          echo "folder=$FOLDER" >> $GITHUB_OUTPUT

      - name: Install huggingface_hub
        run: pip install huggingface_hub

      - name: List target folder (single API call)
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: |
          python -c "
          from huggingface_hub import list_repo_tree
          import json, os
          folder = os.getenv('FOLDER')
          tree = list_repo_tree('${{ env.HF_REPO }}', path=folder, repo_type='dataset')
          files = [f.rfilename for f in tree if f.type == 'file']
          with open('file-list.json', 'w') as f:
              json.dump(files, f)
          print(f'Found {len(files)} files in {folder}')
          "

      - name: Upload file-list artifact
        uses: actions/upload-artifact@v4
        with:
          name: file-list
          path: file-list.json

      - name: Create CDN training config stub
        run: |
          python -c "
          import json, os
          folder = os.getenv('FOLDER')
          cfg = {
            'dataset_base': 'axentx/surrogate-1-training-pairs',
            'partition_folder': folder,
            'use_cdn_only': True,
            'file_list_artifact': 'file-list.json',
            'shard_pattern': 'shard-{shard_id}-{date}-{ts}.jsonl',
            'data_loader': {
              'type': 'cdn_parquet',
              'columns': ['input_ids', 'attention_mask', 'labels'],
              'batch_size': 8
            }
          }
          os.makedirs('batches/public-merged', exist_ok=True)
          with open('batches/public-merged/train-cdn-config.json', 'w') as f:
              json.dump(cfg, f, indent=2)
          "

      - name: Upload training config artifact
        uses: actions/upload-artifact@v4
        with:
          name: train-cdn-config
          path: batches/public-merged/train-cdn-config.json

  shard:
    needs: prelist
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    env:
      SHARD_ID: ${{ matrix.shard_id }}
      RUN_DATE: ${{ needs.prelist.outputs.run_date }}
      FOLDER: ${{ needs.prelist.outputs.folder }}
    steps:
      - uses: actions/checkout@v4

      - name: Download file-list
        uses: actions/download-artifact@v4
        with:
          name: file-list

      - name: Download training config
        uses: actions/download-artifact@v4
        with:
          name: train-cdn-config
          path: batches/public-merged/

      - name: Install deps
        run: pip install -r requirements.txt

      - name: Run shard worker (with retries)
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: |
          set -euo pipefail
          max_retries=3
          for i in $(seq 1 $max_retries); do
            if bash bin/dataset-enrich.sh "$SHARD_ID" "$N_SHARDS" "$RUN_DATE" "$FOLDER"; then
              echo "Shard $SHARD_ID succeeded"
              exit 0
            else
              echo "Attempt $i failed for shard $SHARD_ID"
              if [ $i -lt $max_retries ]; then sleep $(( 10 * i )); fi
            fi
          done
          echo "Shard $SHARD_ID failed after $max_retries attempts"
          exit 1

      - name: Upload shard output as artifact (for inspection)
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: shard-${{ matrix.shard_id }}-output
          path: batches/public-merged/${{ env.FOLDER }}/
```

---

#### `bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
# Usage: dataset-enrich.sh <shard_id> <n_shards> <run_date> <folder>
# Deterministic shard assignment by slug-hash modulo n_shards.
# Outputs to batches/public-merged/<folder>/shard-<shard_id>-<date>-<HHMMSS>.jsonl
set -euo pipefail

SHARD_ID="${1:-0}"
N_SHARDS="${2:-16}"
RUN_DATE="${3:-$(date -u +%Y-%m-%d)}"
FOLDER="${4:-$(echo "$RUN_DATE" | sed 's/-/\//g')}"

REPO="axentx/surrogate-1-training-pairs"
OUTDIR="batches/public-merged/${FOLDER}"
TS=$(date -u +%H%M%S)
OUTFILE="${OUTDIR}/shard-${SHARD_ID}-${RUN_DATE}-${TS}.jsonl"

mkdir -p "$OUTDIR"

# Deterministic assignment helper (stable across runs)
assign_shard() {
  local slug="$1"
  # stable numeric hash across runs (0..2^31-1)
  local h=$(echo -n "$slug" | cksum | awk '{print $1}')
  echo $(( h % N_SHARDS ))
}

# Use file-list.json if present (avoids live API calls inside workers)
if [ -f file-list.json ]; then
  mapfile -t ALL_FILES < <(python -c "import json,sys;print('\n'.join(json.load(open('file-list.json'))))")
else
  echo "file-list.json not found — falling back to live list (may hit API limits)"
  mapfile -t ALL_FILES < <(huggingface-cli repo ls-files "$REPO" --path "$FOLDER" --repo-type dataset || true)
fi

# Filter files assigned to this shard
map

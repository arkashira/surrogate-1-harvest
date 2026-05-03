# surrogate-1 / frontend

## Implementation Plan (≤2h)

**Goal**: Eliminate HF API rate-limit (429) and recursive listing overhead by switching to single non-recursive `list_repo_tree` + CDN-only fetches + deterministic sibling-repo routing.

### Changes

1. **`bin/dataset-enrich.sh`**  
   - Replace recursive `list_repo_files` with `list_repo_tree(path, recursive=False)` per date folder.  
   - Save file list to `file-list.json` once per run; embed in worker payload.  
   - Use CDN URLs (`https://huggingface.co/datasets/.../resolve/main/...`) for all downloads — zero auth/API calls during streaming.  
   - Deterministic shard → sibling repo routing: `repo = f"axentx/surrogate-1-training-pairs-{hash(slug) % 5}"` (5 siblings = 640 commits/hr aggregate).  
   - Project to `{prompt, response}` only before upload; move attribution to filename pattern `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`. No `source`/`ts` columns.

2. **`lib/dedup.py`**  
   - Keep central md5 store interface unchanged; callers pass `md5` and get `is_duplicate`.  
   - Add lightweight retry/backoff for HF API calls (only used for dedup lookups, not data).

3. **`.github/workflows/ingest.yml`**  
   - Pass `FILE_LIST` artifact (generated once) to all 16 matrix shards so each worker uses identical CDN paths.  
   - Set `SHELL=/bin/bash` and ensure all wrapper scripts have `#!/usr/bin/env bash` + `chmod +x`.

---

### Code snippets

#### `bin/dataset-enrich.sh` (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE=$(date +%Y-%m-%d)
OUTDIR="batches/public-merged/${DATE}"
mkdir -p "${OUTDIR}"

# 1) Single non-recursive tree list for the date folder
echo "Listing ${REPO}/${DATE} (non-recursive)..."
python3 -c "
import json, os
from huggingface_hub import HfApi
api = HfApi()
items = api.list_repo_tree(repo_id='${REPO}', path='${DATE}', recursive=False)
files = [i.rfilename for i in items if i.type == 'file']
with open('file-list.json', 'w') as f:
    json.dump({'date': '${DATE}', 'files': files}, f)
"
echo "Saved file-list.json with $(jq length file-list.json) files."

# 2) Build CDN URLs (no auth) and stream per shard
TOTAL_SHARDS=16
for SHARD in $(seq 0 $((TOTAL_SHARDS - 1))); do
  python3 bin/worker.py \
    --file-list file-list.json \
    --shard-id "${SHARD}" \
    --total-shards "${TOTAL_SHARDS}" \
    --out-dir "${OUTDIR}" \
    --cdn-base "https://huggingface.co/datasets/${REPO}/resolve/main" &
done
wait
echo "All shards completed."
```

#### `bin/worker.py` (excerpt)
```python
#!/usr/bin/env python3
import argparse, json, hashlib, sys, os, requests, pyarrow.parquet as pq
from pathlib import Path

HF_TOKEN = os.getenv("HF_TOKEN")
SIBLINGS = [f"axentx/surrogate-1-training-pairs-{i}" for i in range(5)]

def pick_repo(slug: str) -> str:
    h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    return SIBLINGS[h % len(SIBLINGS)]

def download_cdn(url: str, stream=True):
    # CDN fetch: no Authorization header -> bypasses /api/ rate limits
    resp = requests.get(url, stream=stream, timeout=30)
    resp.raise_for_status()
    return resp

def project_to_pair(raw_bytes, ext):
    # Minimal projection: produce {prompt, response} only
    # Implement per-schema logic here (parquet/jsonl/etc.)
    # Return {"prompt": "...", "response": "...", "slug": "..."}
    ...

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file-list", required=True)
    p.add_argument("--shard-id", type=int, required=True)
    p.add_argument("--total-shards", type=int, required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--cdn-base", required=True)
    args = p.parse_args()

    with open(args.file_list) as f:
        manifest = json.load(f)

    files = manifest["files"]
    shard_files = [f for i, f in enumerate(files) if i % args.total_shards == args.shard_id]

    out_path = Path(args.out_dir) / f"shard{args.shard_id}-{manifest['date']}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w") as out_f:
        for rel in shard_files:
            cdn_url = f"{args.cdn_base}/{manifest['date']}/{rel}"
            try:
                data = download_cdn(cdn_url).content
                pair = project_to_pair(data, Path(rel).suffix)
                if not pair:
                    continue
                slug = pair["slug"]
                repo = pick_repo(slug)
                # Upload to sibling repo (deterministic) via HF API (single commit per shard file)
                # Use HF API only for final upload; data path is CDN-only.
                out_f.write(json.dumps(pair) + "\n")
            except Exception as e:
                print(f"Error processing {rel}: {e}", file=sys.stderr)

    print(f"Shard {args.shard_id} wrote {out_path}")

if __name__ == "__main__":
    main()
```

#### `.github/workflows/ingest.yml` (excerpt)
```yaml
name: ingest
on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:

jobs:
  build-file-list:
    runs-on: ubuntu-latest
    outputs:
      file-list: ${{ steps.save.outputs.file-list }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: python -c "
          from huggingface_hub import HfApi; import json, os;
          api = HfApi();
          items = api.list_repo_tree(repo_id='axentx/surrogate-1-training-pairs', path='${{ env.DATE }}', recursive=False);
          files = [i.rfilename for i in items if i.type == 'file'];
          with open('file-list.json', 'w') as f: json.dump({'date': '${{ env.DATE }}', 'files': files}, f);
        "
        env:
          DATE: ${{ github.event.schedule && (github.event.repository.updated_at || github.run_id) || 'latest' }}
      - id: save
        run: echo "file-list=$(cat file-list.json | jq -c .)" >> $GITHUB_OUTPUT

  ingest-shard:
    needs: build-file-list
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    env:
      SHELL: /bin/bash
      HF_TOKEN: ${{ secrets.HF_TOKEN }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: |
          echo '${{ needs.build-file-list.outputs.file-list }}' > file-list.json
          chmod +x bin/dataset-enrich.sh bin/worker.py
          bash bin/worker.py \
            --file-list file-list.json \
            --shard-id ${{ matrix.shard_id }} \
            --total-shards 16 \
            --out-dir "batches

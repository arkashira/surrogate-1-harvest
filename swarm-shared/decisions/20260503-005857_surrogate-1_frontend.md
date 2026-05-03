# surrogate-1 / frontend

## Analysis & Highest-Value Incremental Improvement

**Chosen improvement**: Add **CDN-bypass ingestion mode** to `bin/dataset-enrich.sh` so the GitHub Actions runners avoid HF API rate limits entirely by using `resolve/main/` CDN URLs for file downloads. This is the single highest-leverage change (<2h) because:

- It directly addresses the **HF API 429 rate-limit** pattern (1000 req/5min) that can kill 16 parallel runners
- CDN downloads are **not counted** against API limits and have much higher tier limits
- Requires only small changes to the existing worker script — no infra or workflow changes
- Aligns with the **HF CDN Bypass** key insight already documented for the project

---

## Implementation Plan (≤2h)

### 1. Modify `bin/dataset-enrich.sh` — add CDN download path
Add a function that, given a repo+path, downloads via CDN instead of `datasets` streaming. Keep existing streaming as fallback for private repos.

```bash
#!/usr/bin/env bash
set -euo pipefail

# ... existing header ...

# New: CDN-bypass download for public repos
# Usage: download_via_cdn <repo_id> <file_path> <output_file>
download_via_cdn() {
  local repo_id="$1"
  local file_path="$2"
  local output_file="$3"
  local url="https://huggingface.co/datasets/${repo_id}/resolve/main/${file_path}"
  curl -L --retry 3 --retry-delay 5 -o "${output_file}" "${url}"
}

# Decide mode: if repo is public and file is parquet/jsonl, use CDN
# (caller should pass PUBLIC_REPO_ID or detect via API once then cache)
```

### 2. Update per-shard worker loop to use CDN for public dataset files
Replace `load_dataset(streaming=True)` for the public training-pairs repo with CDN fetches + local pyarrow projection.

```bash
# In the per-shard processing section of dataset-enrich.sh:

process_shard() {
  local shard_id=$1
  local date_dir=$2
  local file_list=$3  # JSON list from Mac: [{repo,path},...]

  # Use CDN for public repo files only
  local public_repo="axentx/surrogate-1-training-pairs"

  # Read file list and download via CDN
  echo "$file_list" | jq -c '.[]' | while read -r entry; do
    local repo path
    repo=$(echo "$entry" | jq -r '.repo')
    path=$(echo "$entry" | jq -r '.path')

    if [[ "$repo" == "$public_repo" ]]; then
      local tmp_file
      tmp_file=$(mktemp /tmp/cdn_dl.XXXXXX)
      download_via_cdn "$repo" "$path" "$tmp_file"

      # Project to {prompt,response} using pyarrow (via python helper)
      python3 lib/project_cdn_parquet.py \
        --input "$tmp_file" \
        --output-stdout \
        --shard-id "$shard_id" \
        >> "batches/public-merged/${date_dir}/shard${shard_id}-$(date +%H%M%S).jsonl"
      rm -f "$tmp_file"
    else
      # Fallback: streaming load for private repos
      python3 lib/stream_private_repo.py \
        --repo "$repo" \
        --path "$path" \
        --shard-id "$shard_id" \
        >> "batches/public-merged/${date_dir}/shard${shard_id}-$(date +%H%M%S).jsonl"
    fi
  done
}
```

### 3. Add Python projection helper (`lib/project_cdn_parquet.py`)
Minimal script to read parquet from CDN download and project only `{prompt,response}`.

```python
#!/usr/bin/env python3
"""
Project CDN-downloaded parquet to {prompt,response} JSONL lines.
Avoids loading full heterogeneous schema into memory.
"""
import argparse
import json
import pyarrow.parquet as pq
import sys

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-stdout", action="store_true")
    parser.add_argument("--shard-id", required=True)
    args = parser.parse_args()

    try:
        table = pq.read_table(args.input, columns=["prompt", "response"])
    except (KeyError, pyarrow.lib.ArrowInvalid):
        # Fallback: try common aliases
        try:
            table = pq.read_table(args.input, columns=["text", "completion"])
            table = table.rename_columns(["prompt", "response"])
        except Exception:
            # Last resort: read first two string columns
            schema = pq.read_schema(args.input)
            str_cols = [n for n, t in zip(schema.names, schema.types) if pa.types.is_string(t)]
            if len(str_cols) >= 2:
                table = pq.read_table(args.input, columns=str_cols[:2])
                table = table.rename_columns(["prompt", "response"])
            else:
                sys.stderr.write(f"shard={args.shard_id} no usable cols in {args.input}\n")
                return

    for batch in table.to_batches():
        df = batch.to_pandas()
        for _, row in df.iterrows():
            out = {"prompt": str(row["prompt"]), "response": str(row["response"])}
            if args.output_stdout:
                print(json.dumps(out, ensure_ascii=False))
            else:
                # could write to file here
                pass

if __name__ == "__main__":
    main()
```

### 4. Update workflow to pre-list file paths once (optional but recommended)
Add a **pre-flight job** that runs on cron start (or manually) to produce `file-list.json` for the date folder, then pass it to all 16 matrix shards via `env` or artifact. This ensures **zero HF API calls during worker execution**.

```yaml
# .github/workflows/ingest.yml — additions
jobs:
  preflight:
    runs-on: ubuntu-latest
    outputs:
      file_list: ${{ steps.list.outputs.file_list }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install huggingface_hub
      - id: list
        run: |
          # Single API call (rate-limited but only once per cron)
          python3 bin/list_public_files.py \
            --repo axentx/surrogate-1-training-pairs \
            --date $(date +%Y-%m-%d) \
            --output file-list.json
          echo "file_list=$(cat file-list.json | jq -c .)" >> $GITHUB_OUTPUT
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}

  ingest:
    needs: preflight
    strategy:
      matrix: { shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15] }
    runs-on: ubuntu-latest
    env:
      FILE_LIST_JSON: ${{ needs.preflight.outputs.file_list }}
      SHARD_ID: ${{ matrix.shard_id }}
    steps:
      - uses: actions/checkout@v4
      - run: bin/dataset-enrich.sh
```

---

## Summary of Changes

| File | Change | Purpose |
|------|--------|---------|
| `bin/dataset-enrich.sh` | Add `download_via_cdn()` + CDN path in worker loop | Bypass HF API rate limits for public files |
| `lib/project_cdn_parquet.py` | New helper | Project CDN-downloaded parquet to `{prompt,response}` only |
| `.github/workflows/ingest.yml` | Add preflight job + pass file list | Single API call, zero API during shard execution |

**Estimated time**: 1–1.5h (test one shard locally, verify CDN URLs resolve, confirm projection works).

**Risk**: CDN URLs may 404 if repo is private or file deleted — fallback to streaming already implemented.

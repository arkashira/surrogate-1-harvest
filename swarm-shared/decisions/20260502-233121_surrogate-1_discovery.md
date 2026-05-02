# surrogate-1 / discovery

## Final Implementation (merged & hardened)

**Core change**: Replace all runtime `datasets.load_dataset(streaming=True)` and recursive `list_repo_files` calls in `bin/dataset-enrich.sh` with a deterministic, pre-flight snapshot + CDN-only ingestion.  
This eliminates HF API rate limits (429), pyarrow `CastError` from mixed schemas, and keeps the 16-shard parallelism intact.

---

### 1) Snapshot generator (single source of truth)

`bin/make-snapshot.py`

- Runs once per date folder (cron or manual) from orchestrator/Mac.
- Uses **one non-recursive** `list_repo_tree` per folder to avoid pagination storms.
- Emits `snapshot/<date>/snapshot.json` containing `{repo, date, files:[{path, cdn_url, size, sha}]}`.
- Commits snapshot to repo **or** passes via workflow artifact so every shard sees identical file list.

```python
#!/usr/bin/env python3
"""
Generate snapshot.json for a date folder in axentx/surrogate-1-training-pairs.

Usage:
  HF_TOKEN=<token> python bin/make-snapshot.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-02 \
    --out snapshot/2026-05-02/snapshot.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-02")
    parser.add_argument("--out", required=True, help="Output snapshot.json path")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    api = HfApi(token=token) if token else HfApi()

    folder = f"batches/public-merged/{args.date}"
    entries = api.list_repo_tree(
        repo_id=args.repo,
        path=folder,
        repo_type="dataset",
        recursive=False,
    )

    snapshot = {
        "repo": args.repo,
        "date": args.date,
        "folder": folder,
        "files": [],
    }

    for entry in entries:
        if getattr(entry, "type", None) != "file":
            continue
        cdn_url = (
            f"https://huggingface.co/datasets/{args.repo}/resolve/main/"
            f"{folder}/{entry.path}"
        )
        snapshot["files"].append(
            {
                "path": entry.path,
                "cdn_url": cdn_url,
                "size": getattr(entry, "size", None),
                "sha": getattr(entry, "sha", None),
            }
        )

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

    print(f"Wrote {len(snapshot['files'])} files to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

```bash
chmod +x bin/make-snapshot.py
```

---

### 2) Lightweight CDN fetcher (retry + idempotent)

`bin/lib/fetch-cdn.sh`

```bash
#!/usr/bin/env bash
# Lightweight CDN fetcher with retries.
# Usage: fetch-cdn.sh <cdn_url> <output_path>
set -euo pipefail

cdn_url="$1"
out="$2"

curl --fail --silent --show-error \
  --retry 5 --retry-delay 5 \
  --max-time 300 \
  --output "${out}" \
  "${cdn_url}"
```

```bash
chmod +x bin/lib/fetch-cdn.sh
```

---

### 3) Updated ingestion script (CDN-only mode)

Key changes to `bin/dataset-enrich.sh`:

- Accept `SNAPSHOT` path (default: `snapshot/latest/snapshot.json`).
- If snapshot provided → **CDN-only mode** (no `datasets` API calls).
- Deterministic shard assignment by file index (stable across runners).
- Per-shard isolation preserved (7 GB each).
- Project to `{prompt, response}` only at parse time (avoids schema mixing).
- Keep legacy fallback for compatibility (avoid in production).

```bash
#!/usr/bin/env bash
# ... existing header ...

SNAPSHOT="${SNAPSHOT:-snapshot/latest/snapshot.json}"   # optional
DATE_DIR="${DATE_DIR:-}"                               # required if no snapshot
WORK_DIR="${WORK_DIR:-$(mktemp -d)}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
SHARD_ID="${SHARD_ID:-0}"

# CDN-only mode (preferred)
if [[ -n "${SNAPSHOT}" && -f "${SNAPSHOT}" ]]; then
  echo "Using snapshot: ${SNAPSHOT}"
  mapfile -t FILES < <(jq -r '.files[].cdn_url' "${SNAPSHOT}")
  TOTAL="${#FILES[@]}"
  echo "Total files: ${TOTAL}, shard ${SHARD_ID}/${TOTAL_SHARDS}"

  for i in "${!FILES[@]}"; do
    url="${FILES[$i]}"
    shard=$(( i % TOTAL_SHARDS ))
    if [[ "${shard}" != "${SHARD_ID}" ]]; then
      continue
    fi
    fname="$(basename "${url}")"
    dl="${WORK_DIR}/${fname}"
    if [[ -f "${dl}" ]]; then
      echo "Skipping existing ${fname}"
    else
      if ! bin/lib/fetch-cdn.sh "${url}" "${dl}"; then
        echo "Failed to fetch ${url}" >&2
        continue
      fi
    fi
    # Project to {prompt,response} only at parse time
    parse_and_normalize "${dl}" >> "${WORK_DIR}/shard-${SHARD_ID}.jsonl"
  done
else
  # Legacy fallback (avoid in production)
  echo "WARNING: running legacy mode without snapshot" >&2
  # ... existing load_dataset logic (avoid if possible) ...
fi

# Dedup and upload (existing logic)
python lib/dedup.py --input "${WORK_DIR}/shard-${SHARD_ID}.jsonl" --output "${WORK_DIR}/dedup-shard-${SHARD_ID}.jsonl"
# ... upload to batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl
```

```bash
chmod +x bin/dataset-enrich.sh
```

---

### 4) GitHub Actions (snapshot → shards)

```yaml
jobs:
  snapshot:
    runs-on: ubuntu-latest
    outputs:
      snapshot-path: ${{ steps.set.outputs.path }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt huggingface_hub
      - run: |
          mkdir -p snapshot/${{ env.DATE }}
          HF_TOKEN=${{ secrets.HF_TOKEN }} \
          python bin/make-snapshot.py \
            --repo axentx/surrogate-1-training-pairs \
            --date ${{ env.DATE }} \
            --out snapshot/${{ env.DATE }}/snapshot.json
      - uses: actions/upload-artifact@v4
        with:
          name: snapshot-${{ env.DATE }}
          path: snapshot/${{ env.DATE }}/snapshot.json
      - id: set
        run: echo "path=snapshot/${{ env.DATE }}/snapshot.json" >> $GITHUB_OUTPUT

  ingest:
    needs: snapshot
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: snapshot-${{ env.DATE }}
      - run: |
          export SHARD_ID=${{ matrix.shard_id }}
          export TOTAL_SHARDS=16
          export SNAPSHOT=snapshot/${{ env.DATE }}/snapshot.json
          bin/dataset-enrich.sh
```

---


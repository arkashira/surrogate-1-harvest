# surrogate-1 / discovery

## Final synthesized implementation (best of both)

**Core idea**: One pre-flight step produces a deterministic, date-partitioned file list; each shard uses CDN-only fetches and a stable hash to write exactly one file per run. Races and API pressure are eliminated.

---

### 1) Workflow (`.github/workflows/ingest.yml`)

- Preflight job lists today’s folder once and uploads `file-list.json`.
- Matrix of 16 shards runs in parallel.
- Date/time are injected once so all shards use the same filenames.

```yaml
name: surrogate-1-ingest
on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:

jobs:
  preflight:
    runs-on: ubuntu-latest
    outputs:
      date: ${{ steps.date.outputs.DATE }}
      time: ${{ steps.time.outputs.TIME }}
    steps:
      - uses: actions/checkout@v4

      - name: Set date/time
        id: date
        run: echo "DATE=$(date -u +%Y-%m-%d)" >> $GITHUB_OUTPUT

      - name: Set time
        id: time
        run: echo "TIME=$(date -u +%H%M%S)" >> $GITHUB_OUTPUT

      - name: List today folder (single API call)
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: |
          python - <<'PY'
          import os, json, datetime
          from huggingface_hub import HfApi
          api = HfApi(token=os.getenv("HF_TOKEN"))
          today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
          repo = "datasets/axentx/surrogate-1-training-pairs"
          items = api.list_repo_tree(repo=repo, path=today, recursive=False)
          files = sorted(it.rfilename for it in items if hasattr(it, "rfilename"))
          with open("file-list.json", "w") as f:
              json.dump({"date": today, "files": files}, f)
          PY

      - uses: actions/upload-artifact@v4
        with:
          name: file-list
          path: file-list.json

  ingest:
    needs: preflight
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4

      - uses: actions/download-artifact@v4
        with:
          name: file-list

      - name: Install deps
        run: pip install -r requirements.txt

      - name: Run shard worker
        env:
          SHARD_ID: ${{ matrix.shard_id }}
          DATE_PART: ${{ needs.preflight.outputs.date }}
          TIME_PART: ${{ needs.preflight.outputs.time }}
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
          REPO: "datasets/axentx/surrogate-1-training-pairs"
        run: |
          bash bin/dataset-enrich.sh
```

---

### 2) `bin/dataset-enrich.sh` (deterministic + CDN + race-safe)

- Uses pre-computed `file-list.json`.
- Deterministic shard assignment by `slug` (`sha256 % 16`).
- Writes to `batches/public-merged/YYYY-MM-DD/shardN-HHMMSS.jsonl`.
- Skips if `_SUCCESS` marker for this shard/date/time already exists (prevents overwrite races).
- Pushes only its own file; commit includes shard/date/time.

```bash
#!/usr/bin/env bash
set -euo pipefail

: "${SHARD_ID:?}"
: "${DATE_PART:?}"
: "${TIME_PART:?}"
: "${REPO:?}"
: "${HF_TOKEN:?}"

BASE_OUT="batches/public-merged/${DATE_PART}"
OUT_FILE="${BASE_OUT}/shard${SHARD_ID}-${TIME_PART}.jsonl"
SUCCESS_MARKER="${BASE_OUT}/shard${SHARD_ID}-${TIME_PART}.success"

mkdir -p "$(dirname "$OUT_FILE")"

# Skip if this exact shard/time already completed (avoid races/repeats)
if [[ -f "$SUCCESS_MARKER" ]]; then
  echo "Shard ${SHARD_ID} for ${DATE_PART} ${TIME_PART} already done (success marker present). Skipping."
  exit 0
fi

# Deterministic shard assignment by slug
shard_for() {
  local slug=$1
  python -c "import hashlib; print(abs(int(hashlib.sha256('$slug'.encode()).hexdigest(), 16)) % 16)"
}

python - <<PY
import json, os, hashlib, sys, requests, tqdm

SHARD_ID = int(os.getenv("SHARD_ID"))
REPO = os.getenv("REPO")
OUT_FILE = os.getenv("OUT_FILE")
FILE_LIST = "file-list.json"

with open(FILE_LIST) as f:
    manifest = json.load(f)

files = manifest["files"]

def shard_for(slug: str) -> int:
    return abs(int(hashlib.sha256(slug.encode()).hexdigest(), 16)) % 16

def download_cdn(path: str) -> bytes:
    url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{path}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.content

def parse_to_pair(raw: bytes, filename: str):
    # Adapt to your schema. Must return dict with at least {prompt,response}.
    text = raw.decode("utf-8", errors="replace")
    return {"prompt": f"from {filename}", "response": text[:2000]}

written = 0
with open(OUT_FILE, "w", encoding="utf-8") as out:
    for fpath in tqdm.tqdm(files, desc="Processing"):
        slug = os.path.splitext(os.path.basename(fpath))[0]
        if shard_for(slug) != SHARD_ID:
            continue
        try:
            raw = download_cdn(fpath)
            pair = parse_to_pair(raw, fpath)
            if pair is None:
                continue
            pair["_md5"] = hashlib.md5(raw).hexdigest()
            pair["_source_file"] = fpath
            out.write(json.dumps(pair, ensure_ascii=False) + "\n")
            written += 1
        except Exception as e:
            print(f"WARN: failed {fpath}: {e}", file=sys.stderr)

print(f"Wrote {written} pairs to {OUT_FILE}")
PY

# Create success marker to prevent re-runs/races for this exact shard/time
touch "$SUCCESS_MARKER"

# Push only this shard's file + marker
git config user.name "github-actions"
git config user.email "github-actions@github.com"
git add "$OUT_FILE" "$SUCCESS_MARKER"
git commit -m "shard${SHARD_ID} ${DATE_PART} ${TIME_PART}" || true
git push "https://${HF_TOKEN}@huggingface.co/datasets/${REPO}.git" HEAD:main
```

---

### 3) `bin/list_files.py` (optional local helper)

Kept for local debugging / ad-hoc runs; not required in workflow because preflight does the same.

```python
#!/usr/bin/env python3
"""
Generate file-list.json for a date folder.
Usage:
  HF_TOKEN=hf_xxx python bin/list_files.py 2024-06-01
"""
import json, os, sys
from huggingface_hub import HfApi

def main(date: str, repo: str = "datasets/axentx/surrogate-1-training-pairs"):
    api = HfApi(token=os.getenv("HF_TOKEN"))
    items = api.list_repo_tree(repo=repo, path=date, recursive=False)
    files = sorted(it.rfilename for it in items if hasattr(it, "rfilename"))
    out = {"date": date, "files": files}
    with open("file-list.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote file-list.json with {len(files)} files for {date}")

if __name__ == "__main__":
    if len(sys.argv) <

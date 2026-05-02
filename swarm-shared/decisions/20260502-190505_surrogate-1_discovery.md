# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Goal**: Make ingestion deterministic, CDN-bypass friendly, and rate-limit safe by:

1. **Date-partitioned output paths**  
   `batches/public-merged/{YYYY-MM-DD}/shard{N}-{HHMMSS}.jsonl`  
   (no more flat directories; prevents overwrite races across days)

2. **Pre-flight file list**  
   - On workflow trigger (or once per day), run a lightweight Mac/CI step that lists today’s folder via `list_repo_tree(..., recursive=False)` and writes `file-list-{date}.json`.  
   - Commit that list into the repo (or pass via artifact) so each shard uses **CDN-only** downloads during ingestion (zero HF API calls while processing).

3. **Shard → CDN downloader**  
   - Replace `load_dataset(..., streaming=True)` with direct `requests.get` to `https://huggingface.co/datasets/.../resolve/main/{path}` (no auth, CDN tier).  
   - Parse only `{prompt,response}` at read time; drop all other fields.

4. **Deterministic shard assignment**  
   - Hash `slug` → `int % 16` to assign files to shards (stable across runs).  
   - Each shard processes only its slice from the pre-computed list.

5. **Idempotent upload**  
   - Filename includes `shard{N}-{HHMMSS}` and date folder; never collides.  
   - Skip upload if target already exists (check via `hf_hub_download` HEAD or repo file list).

6. **Small operational fixes**  
   - Ensure `bin/dataset-enrich.sh` has `#!/usr/bin/env bash` and is executable.  
   - Set `SHELL=/bin/bash` in workflow env to avoid cron-like shell issues.

---

## Code Changes

### 1. Add pre-flight file-list generator (run once per day)

`bin/list-today-files.py`
```python
#!/usr/bin/env python3
"""
Generate a deterministic file list for today's folder on the dataset repo.
Intended to run once per cron tick (or manually) and commit/upload as artifact.
Outputs: file-list-YYYY-MM-DD.json
"""
import json
import os
from datetime import datetime, timezone
from huggingface_hub import HfApi

REPO_ID = "axentx/surrogate-1-training-pairs"
DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")
FOLDER = f"batches/public-raw/{DATE}"  # or whatever upstream folder
OUTFILE = f"file-list-{DATE}.json"

def main() -> None:
    api = HfApi(token=os.environ.get("HF_TOKEN"))  # optional for public read; required for private
    # Non-recursive to avoid pagination explosion; we only need today's top-level files
    entries = api.list_repo_tree(repo_id=REPO_ID, path=FOLDER, recursive=False)
    files = [e.path for e in entries if e.type == "file"]
    payload = {
        "date": DATE,
        "folder": FOLDER,
        "files": sorted(files),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    with open(OUTFILE, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(files)} files to {OUTFILE}")

if __name__ == "__main__":
    main()
```

Make executable:
```bash
chmod +x bin/list-today-files.py
```

---

### 2. Update worker to use CDN + deterministic sharding

`bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
# Deterministic shard worker with CDN-bypass ingestion.
# Usage:
#   SHARD_ID=0 ./bin/dataset-enrich.sh file-list-YYYY-MM-DD.json
set -euo pipefail

# Ensure Bash is used (defensive)
if [ -z "${BASH_VERSION:-}" ]; then
  echo "This script requires Bash." >&2
  exit 1
fi

# Config
REPO_ID="axentx/surrogate-1-training-pairs"
DATE="${DATE:-$(date -u +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
TS="$(date -u +%H%M%S)"
OUT_DIR="batches/public-merged/${DATE}"
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "$(dirname "$OUT_FILE")"

# Expect file list as first arg
FILE_LIST="${1:-}"
if [[ ! -f "$FILE_LIST" ]]; then
  echo "Usage: $0 file-list-YYYY-MM-DD.json" >&2
  exit 1
fi

# Python processor inline to avoid extra files; uses CDN URLs
python3 - "$FILE_LIST" "$SHARD_ID" "$TOTAL_SHARDS" "$OUT_FILE" <<'PY'
import json
import hashlib
import os
import sys
import requests
from typing import List

REPO_ID = "axentx/surrogate-1-training-pairs"
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def assign_shard(slug: str, total: int) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % total

def parse_file_cdn(path: str):
    url = CDN_TEMPLATE.format(repo=REPO_ID, path=path)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    # Very small projection: keep only prompt/response heuristics
    # Replace with your actual schema logic; here we yield minimal dicts.
    # Example assumes JSONL lines in raw files; adapt as needed.
    for line in r.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        prompt = obj.get("prompt") or obj.get("input") or obj.get("text")
        response = obj.get("response") or obj.get("output") or obj.get("completion")
        if prompt is None or response is None:
            continue
        yield {"prompt": str(prompt), "response": str(response)}

def main():
    file_list_path, shard_id_s, total_shards_s, out_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    shard_id = int(shard_id_s)
    total_shards = int(total_shards_s)

    with open(file_list_path) as f:
        meta = json.load(f)
    files: List[str] = meta.get("files", [])

    kept = 0
    with open(out_path, "w") as out:
        for path in files:
            slug = os.path.splitext(os.path.basename(path))[0]
            if assign_shard(slug, total_shards) != shard_id:
                continue
            try:
                for item in parse_file_cdn(path):
                    out.write(json.dumps(item, ensure_ascii=False) + "\n")
                    kept += 1
            except Exception as exc:
                # Log but continue processing other files
                print(f"Skipping {path}: {exc}", file=sys.stderr)

    print(f"Shard {shard_id} wrote {kept} pairs to {out_path}")

if __name__ == "__main__":
    main()
PY

echo "Shard ${SHARD_ID} completed: ${OUT_FILE}"
```

Make executable:
```bash
chmod +x bin/dataset-enrich.sh
```

---

### 3. Update workflow to generate file list and run shards

`.github/workflows/ingest.yml`
```yaml
name: Ingest (16-shard, CDN-bypass)

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:
    inputs:
      date:
        description: "Date (YYYY-MM-DD) to process (defaults to today UTC)"
        required: false
        default: ""

env:
  HF_TOKEN: ${{ secrets.HF_TOKEN }}
  SHELL: /bin/bash

jobs:
  prepare:
    runs-on: ubuntu-latest
    outputs:
      file_list: ${{ steps.filelist.outputs.file_list }}
      date: ${{ steps.date.outputs.date }}
    steps:
      - uses: actions/checkout@v4


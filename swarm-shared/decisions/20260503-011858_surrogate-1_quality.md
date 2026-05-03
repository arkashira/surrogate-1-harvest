# surrogate-1 / quality

## Implementation Plan (≤2h)

Highest-value incremental improvement:  
Replace recursive HF API ingestion and per-file authenticated fetches with **single non-recursive `list_repo_tree` per folder + CDN-only downloads** and **project-to-schema at parse time** (no `streaming=True` on heterogeneous repos). This removes 429 rate-limit risk during data load and keeps Lightning training API-free.

### Steps (Mac orchestrator + GitHub Actions workers)

1. **Mac orchestrator** (run once per date folder after rate-limit window clears)
   - Call `list_repo_tree(path=date_folder, recursive=False)` for the public dataset repo.
   - Save `files.json` (basename + cdn_path) into repo under `manifests/<date>/files.json`.
   - Commit + push (counts toward HF commit cap; one commit per folder).

2. **GitHub Actions worker** (`bin/dataset-enrich.sh`)
   - Accept `DATE` and `SHARD_ID` (0–15).
   - Load `manifests/<DATE>/files.json`.
   - Deterministic shard assignment: `hash(slug) % 16 == SHARD_ID`.
   - For each assigned file:
     - Download via **CDN** (`https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/<path>` — no Authorization header).
     - Parse with schema-specific extractor; project to `{prompt, response}` only.
     - Compute md5 for dedup (call `lib/dedup.py`).
   - Emit `shard<N>-<HHMMSS>.jsonl` to `batches/public-merged/<DATE>/`.
   - Upload via HF API (one commit per shard).

3. **Lightning training script** (`train.py`)
   - Read manifest at startup (embedded or fetched once).
   - Data loader uses **CDN-only URLs** (no `load_dataset` on heterogeneous repo; no `streaming=True`).
   - Map-style dataset downloads via `requests.get(cdn_url, timeout=30)` and parse in `__getitem__`.
   - Zero HF API calls during training.

4. **Lightning Studio reuse guard**
   - Before `.run()`, list running studios; reuse if exists.
   - On idle stop, restart with `target.start(machine=Machine.L40S)`.

5. **Cron / GitHub Actions**
   - Keep 30m schedule.
   - Matrix: 16 shards.
   - Ensure `SHELL=/bin/bash` in any crontab entries.

---

## Code Snippets

### 1. Mac orchestrator script (`bin/build-manifest.py`)
```python
#!/usr/bin/env python3
"""
Run on Mac after rate-limit window clears.
Usage:
  python bin/build-manifest.py --repo axentx/surrogate-1-training-pairs \
                               --date 2026-05-03 \
                               --out manifests/2026-05-03/files.json
"""
import argparse
import json
import os
from huggingface_hub import HfApi

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi(token=os.getenv("HF_TOKEN"))
    # Non-recursive: one folder = one date partition
    tree = api.list_repo_tree(repo_id=args.repo, path=args.date, recursive=False)

    files = []
    for item in tree:
        if item.type != "file":
            continue
        # CDN bypass: no auth header
        cdn_url = f"https://huggingface.co/datasets/{args.repo}/resolve/main/{args.date}/{item.path}"
        files.append({
            "path": item.path,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None)
        })

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(files, f, indent=2)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

---

### 2. Worker script (`bin/dataset-enrich.sh`)
```bash
#!/usr/bin/env bash
# GitHub Actions worker (one shard).
# Required env:
#   DATE=2026-05-03
#   SHARD_ID=0..15
#   HF_TOKEN (write)
set -euo pipefail
SHELL=/bin/bash

REPO="axentx/surrogate-1-training-pairs"
MANIFEST="manifests/${DATE}/files.json"
OUT_DIR="batches/public-merged/${DATE}"
TS=$(date -u +"%H%M%S")
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "$(dirname "${OUT_FILE}")"

python3 - <<PY
import json, hashlib, os, sys, requests, pyarrow as pa, pyarrow.parquet as pq
from pathlib import Path

REPO = os.getenv("REPO")
DATE = os.getenv("DATE")
SHARD_ID = int(os.getenv("SHARD_ID"))
MANIFEST = os.getenv("MANIFEST")
OUT_FILE = os.getenv("OUT_FILE")
HF_TOKEN = os.getenv("HF_TOKEN")

with open(MANIFEST) as f:
    files = json.load(f)

def assign_shard(path: str) -> int:
    return hash(path) % 16

def download_cdn(url: str) -> bytes:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.content

def extract_pair(data: bytes, path: str):
    # Minimal schema projection: try parquet -> {prompt,response}
    try:
        tbl = pq.read_table(pa.BufferReader(data))
        if "prompt" in tbl.column_names and "response" in tbl.column_names:
            for i in range(tbl.num_rows):
                yield {
                    "prompt": tbl["prompt"][i].as_py(),
                    "response": tbl["response"][i].as_py(),
                }
            return
    except Exception:
        pass
    # Fallback: newline jsonl
    for line in data.decode("utf-8", errors="ignore").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if "prompt" in obj and "response" in obj:
                yield {"prompt": obj["prompt"], "response": obj["response"]}
        except Exception:
            continue

def md5_hex(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()

# Import dedup client
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.dedup import is_duplicate, register_hash

written = 0
with open(OUT_FILE, "w") as out:
    for entry in files:
        if assign_shard(entry["path"]) != SHARD_ID:
            continue
        try:
            data = download_cdn(entry["cdn_url"])
        except Exception as e:
            print(f"SKIP {entry['path']} error={e}", file=sys.stderr)
            continue

        h = md5_hex(data)
        if is_duplicate(h):
            continue

        for pair in extract_pair(data, entry["path"]):
            line = json.dumps(pair, ensure_ascii=False)
            out.write(line + "\n")
            written += 1

        register_hash(h)

print(f"Shard {SHARD_ID} wrote {written} pairs to {OUT_FILE}")
PY

# Upload shard output (single commit per shard)
if [[ -s "${OUT_FILE}" ]]; then
  git config user.name "github-actions"
  git config user.email "github-actions@github.com"
  git add "${OUT_FILE}"
  git commit -m "shard${SHARD_ID} ${DATE} ${TS}"
  git push
else
  echo "No output for shard ${SHARD_ID}"
fi
```

---

### 3. Lightning training data loader (CDN-only, zero API calls)
```python
# train.py snippet
import json, requests, pyarrow as pa, pyarrow.parquet as pq
from torch.utils.data import Dataset
from pathlib import Path

class CDNPairDataset(Dataset):
    def

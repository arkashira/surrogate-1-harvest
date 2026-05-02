# surrogate-1 / quality

## Final Synthesized Plan (single, correct, actionable)

**Highest-value improvement**  
Eliminate HF API 429s during training and make shard workers fully resilient by:

1. Deterministic pre-flight file listing (single `list_repo_tree` snapshot).  
2. CDN-only ingestion and training-time fetches (zero `/api/` calls, no auth header, higher CDN limits).  
3. Deterministic shard assignment from the snapshot so workers never diverge.

---

### Concrete implementation (≤2h)

#### 1) Add snapshot utility (Mac orchestration)
Create `bin/list-snapshot.py` (preferred over shell for robust JSON and HF API handling). Run once per date folder after rate-limit window clears.

```python
#!/usr/bin/env python3
"""
Generate deterministic file listing for one date folder.
Usage:
  HF_TOKEN=... python bin/list-snapshot.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-02 \
    --out filelist.json
"""
import argparse
import json
import os
import sys
from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="Folder under datasets/ to snapshot")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi(token=os.getenv("HF_TOKEN"))
    entries = api.list_repo_tree(
        repo_id=args.repo,
        path=args.date,
        repo_type="dataset",
        recursive=False,
    )

    files = [e.path for e in entries if e.type == "file"]
    files.sort()  # deterministic ordering for reproducible sharding

    payload = {
        "repo": args.repo,
        "date": args.date,
        "files": files,
        "snapshot_mode": "cdn_safe",
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(files)} files -> {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

- Make executable: `chmod +x bin/list-snapshot.py`  
- Shebang and permissions follow the wrapper-fix pattern.

---

#### 2) Update `bin/dataset-enrich.sh` to use CDN + pre-list
Accept a filelist and download via CDN. Deterministic shard assignment from the snapshot.

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
SHARD_ID="${SHARD_ID:-0}"
N_SHARDS="${N_SHARDS:-16}"
FILE_LIST="${FILE_LIST:-filelist.json}"   # optional pre-generated list
WORK_DIR="${WORK_DIR:-work}"
mkdir -p "$WORK_DIR"

download_cdn() {
  local repo="$1"
  local path="$2"
  local out="$3"
  curl -L -s -f -o "$out" \
    "https://huggingface.co/datasets/${repo}/resolve/main/${path}"
}

# Load pre-list or fallback to live tree (non-recursive)
if [[ -f "$FILE_LIST" ]]; then
  mapfile -t ALL_FILES < <(jq -r '.files[]' "$FILE_LIST")
else
  # fallback: use gh api tree (non-recursive) — keep for local dev without snapshot
  mapfile -t ALL_FILES < <(gh api "repos/${REPO}/git/trees/main?recursive=false" | \
    jq -r '.tree[] | select(.type=="blob") | .path' | sort)
fi

# Deterministic shard assignment from filename
for path in "${ALL_FILES[@]}"; do
  # stable shard by hash of path
  hash=$(printf '%s' "$path" | sha256sum | awk '{print "0x" substr($1,1,8)}')
  shard=$(( hash % N_SHARDS ))
  if [[ "$shard" -ne "$SHARD_ID" ]]; then
    continue
  fi

  out="$WORK_DIR/$(basename "$path")"
  if [[ -f "$out" ]]; then
    echo "Skip cached: $path"
    continue
  fi

  echo "Downloading $path -> $out"
  download_cdn "$REPO" "$path" "$out"

  # Project to {prompt, response} here or in Python helper
  # python project.py "$out" --output-projection ...
done
```

- Ensure executable: `chmod +x bin/dataset-enrich.sh`

---

#### 3) Add Python CDN helper (for non-shell stages)
Use this in training or projection code instead of `load_dataset(streaming=True)` for heterogeneous repos.

```python
import requests

def download_cdn(repo: str, path: str, out_path: str) -> None:
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    with requests.get(url, timeout=60) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
```

---

#### 4) Update training script to use CDN + embedded file list
`train.py` should read `filelist.json` and fetch via CDN URLs only.

```python
import json
import os
import random
import requests
from typing import Iterator, Dict

def load_cdn_shard(filelist_path: str, repo: str, shard_id: int, n_shards: int) -> Iterator[Dict]:
    with open(filelist_path) as f:
        payload = json.load(f)
    files = payload["files"]

    for path in files:
        if hash(path) % n_shards != shard_id:
            continue
        url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
        with requests.get(url, timeout=60) as r:
            r.raise_for_status()
            # project to {prompt, response} here
            # yield {"prompt": ..., "response": ...}
            yield {"url": url, "path": path, "raw": r.content}
```

- No `load_dataset` or `list_repo_*` during training — zero HF API calls for data loading.

---

#### 5) GitHub Actions integration (optional but recommended)
Add an initial job that runs `list-snapshot.py` and uploads `filelist.json` as an artifact. Each shard job downloads the artifact and uses it as the source of truth.

- In the “list” job, retry on 429 with a 360s sleep (once).  
- Pass `FILE_LIST` to workers via artifact or commit the snapshot per date.

---

### Expected outcomes
- HF API usage during ingestion/training drops to near-zero (only `list_repo_tree` snapshot).  
- CDN limits absorb parallel shard traffic; 429s eliminated.  
- Deterministic sharding via sorted filelist prevents overlap and improves reproducibility.  
- No changes to dedup logic or output format — safe, incremental improvement.

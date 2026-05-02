# surrogate-1 / backend

## Final Implementation Plan (≤2 h)

**Highest-value improvement**  
Add deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers fully resilient. Keep existing schema normalization, dedup, and cron/matrix behavior unchanged.

---

### What we will do (concrete, ordered)

1. Add `bin/list-shards.sh`  
   - One-time Mac/CI call (after rate-limit window).  
   - Uses `HfApi.list_repo_tree` (non-recursive) for today’s folder.  
   - Writes `shards.json` (flat list of relative file paths).  
   - Exits non-zero on failure; idempotent (overwrites).

2. Add `bin/lib/cdn.py` helper  
   - `resolve_cdn_url(repo, path)` → `https://huggingface.co/datasets/.../resolve/main/...`  
   - `stream_cdn_to_file(url, dst, headers=...)` with retries/timeouts.  
   - No `datasets` or `hf_api` usage during worker run.

3. Update `bin/dataset-enrich.sh`  
   - Accept `SHARDS_FILE` (default: today’s `shards.json`).  
   - If `SHARDS_FILE` exists and non-empty: CDN-only mode. Workers read paths, stream via CDN, parse, normalize, dedup.  
   - If absent: keep existing `datasets` streaming fallback (unchanged).  
   - Deterministic sibling-repo write sharding:  
     `repos = [base, base-1, ..., base-5]`  
     `dst_repo = repos[hash(slug) % 6]` → raises aggregate commit cap to ~640/hr.

4. Update `requirements.txt`  
   - Ensure `requests` present (used by CDN helper).

---

### 1) `bin/list-shards.sh` (new)

```bash
#!/usr/bin/env bash
# list-shards.sh
# Usage:
#   HF_TOKEN=hf_xxx ./list-shards.sh axentx/surrogate-1-training-pairs 2025-06-01 > shards.json
#
# Outputs JSON array:
#   ["raw/2025-06-01/file1.parquet", "raw/2025-06-01/file2.parquet", ...]

set -euo pipefail

REPO="${1:-axentx/surrogate-1-training-pairs}"
DATE="${2:-$(date +%Y-%m-%d)}"

python3 - "$REPO" "$DATE" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

repo = sys.argv[1]
date = sys.argv[2]
api = HfApi(token=os.getenv("HF_TOKEN"))

files = []
try:
    # Non-recursive top-level in date folder
    for o in api.list_repo_tree(repo=repo, path=date, recursive=False):
        if o.type == "file":
            files.append(f"{date}/{o.path}")
        elif o.type == "directory":
            try:
                for s in api.list_repo_tree(repo=repo, path=o.path, recursive=False):
                    if s.type == "file":
                        files.append(f"{o.path}/{s.path}")
            except Exception:
                pass
except Exception as e:
    sys.stderr.write(f"Error listing {repo}/{date}: {e}\n")
    sys.exit(1)

json.dump(files, sys.stdout, indent=2)
PY
```

```bash
chmod +x bin/list-shards.sh
```

---

### 2) `bin/lib/cdn.py` (new)

```python
import os
import time
import requests
from typing import Optional

DEFAULT_HEADERS = {"Authorization": f"Bearer {os.getenv('HF_TOKEN', '')}"} \
    if os.getenv("HF_TOKEN") else {}

def resolve_cdn_url(repo: str, path: str) -> str:
    # repo format: owner/dataset
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def stream_cdn_to_file(
    url: str,
    dst: str,
    headers: Optional[dict] = None,
    timeout: int = 60,
    max_retries: int = 3,
    backoff: float = 1.5,
) -> None:
    hdrs = headers or DEFAULT_HEADERS or {}
    attempt = 0
    delay = 1.0
    while attempt <= max_retries:
        try:
            with requests.get(url, headers=hdrs, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                with open(dst, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            return
        except Exception as e:
            attempt += 1
            if attempt > max_retries:
                raise
            time.sleep(delay)
            delay *= backoff
```

Add to `requirements.txt` if missing:

```
requests>=2.28
```

---

### 3) Updated `bin/dataset-enrich.sh`

Key changes:
- Accept `SHARDS_FILE` (env or arg).  
- CDN-only mode when shards file present.  
- Deterministic sibling-repo write sharding.  
- Preserve existing schema normalization and dedup logic.

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Updated: CDN-only mode + deterministic sibling-repo write sharding.

set -euo pipefail
export SHELL=/bin/bash

REPO_DST_BASE="axentx/surrogate-1-training-pairs"
DATE=$(date +%Y-%m-%d)
TS=$(date +%H%M%S)
SHARD_ID=${SHARD_ID:-0}
TOTAL_SHARDS=${TOTAL_SHARDS:-16}
SHARDS_FILE="${SHARDS_FILE:-./shards.json}"

# Deterministic sibling repo by hash(slug) -> 0..5
pick_sibling() {
  local slug=$1
  local h=$(echo -n "$slug" | cksum | awk '{print $1}')
  echo $(( h % 6 ))
}

python3 - "$SHARD_ID" "$TOTAL_SHARDS" "$SHARDS_FILE" <<'PY'
import os, sys, json, hashlib, requests, pyarrow.parquet as pq, io
from pathlib import Path

SHARD_ID = int(sys.argv[1])
TOTAL_SHARDS = int(sys.argv[2])
SHARDS_FILE = sys.argv[3] if len(sys.argv) > 3 else ""

REPO_DST_BASE = "axentx/surrogate-1-training-pairs"
DATE = os.getenv("DATE", "") or __import__("datetime").date.today().isoformat()
TS = os.getenv("TS", "") or __import__("datetime").datetime.now().strftime("%H%M%S")
HF_TOKEN = os.getenv("HF_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
CDN_BASE = f"https://huggingface.co/datasets/{REPO_DST_BASE}/resolve/main"

def deterministic_sibling(slug: str) -> int:
    h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    return h % 6

def pick_dst_repo(slug: str) -> str:
    sfx = deterministic_sibling(slug)
    if sfx == 0:
        return REPO_DST_BASE
    return f"{REPO_DST_BASE}-{sfx}"

def is_my_shard(slug: str) -> bool:
    h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    return (h % TOTAL_SHARDS) == SHARD_ID

def parse_file_to_pairs(local_path: str):
    # Project heterogeneous files to {prompt,response} only.
    # Preserve existing per-schema logic from repo.
    try:
        tbl = pq.read_table(local_path, columns=["prompt", "response"])
        for row in tbl.to_pylist():
            if row.get("prompt") and row.get("response"):
                slug = row.get("slug") or hashlib.md5(
                    f"{row['prompt'][:128]}{row['response'][:128]}".encode()
                ).hexdigest()[:16]
                yield {"prompt": row["prompt"], "response": row["

# surrogate-1 / backend

Candidate 3:
## Implementation Plan (≤2h)

**Goal**: Eliminate HF API rate-limit (429) and recursive listing overhead by switching to single non-recursive `list_repo_tree` + CDN-only fetches + deterministic sibling-repo routing.

### Changes
1. **`bin/dataset-enrich.sh`**  
   - Accept `DATE_FOLDER` and `SHARD_ID`/`SHARD_TOTAL` as env params (CI already provides matrix).  
   - On Mac/orchestrator: run one non-recursive `list_repo_tree` for `{DATE_FOLDER}/` and persist file list to `filelist.json`.  
   - Workers read `filelist.json`, filter by deterministic hash-bucket (`hash(slug) % SHARD_TOTAL == SHARD_ID`).  
   - Fetch each file via **CDN URL** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header.  
   - Project to `{prompt,response}` only at parse time; do not add `source`/`ts` columns.  
   - Upload output to deterministic sibling repo:  
     - Repo = `axentx/surrogate-1-training-pairs-{hash(slug) % 5}` (5 siblings → 640/hr aggregate cap).  
     - Path = `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

2. **`lib/dedup.py`** (no change to semantics)  
   - Keep central md5 store logic; workers will still dedup locally before upload (best-effort). Cross-run duplicates acceptable per trade-offs.

3. **`.github/workflows/ingest.yml`**  
   - Add `DATE_FOLDER` input (default: latest date folder discovered by a lightweight pre-step or override).  
   - Keep 16-shard matrix.  
   - Add step before matrix to produce `filelist.json` artifact (upload) and a small index mapping shard→paths (download in each job).  
   - Each job downloads only its shard’s path list (few KB), then runs CDN-only fetch + projection + upload.  
   - Remove recursive listing and per-file API calls entirely.

---

## Final Implementation Plan (≤2h)

**Goal**: Eliminate HF API rate-limit (429) and commit-cap (128/hr) bottlenecks with deterministic, CDN-only, shard-aware ingestion.

### 1) Orchestrator: one non-recursive tree list → manifest (per date folder)

`scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Run on Mac (or any orchestrator) after rate-limit window clears.
Produces manifest for a single date folder:
  manifest/
    2026-04-29.json   # [{ "path": "...", "cdn": "...", "size": ... }, ...]
"""
import json, os, hashlib
from huggingface_hub import HfApi

API = HfApi()
REPO = "datasets/axentx/surrogate-1-training-pairs"
DATE = os.getenv("DATE", "2026-04-29")          # e.g. 2026-04-29
OUT_DIR = "manifest"
os.makedirs(OUT_DIR, exist_ok=True)

def list_date_files(date: str):
    # Non-recursive per top-level folder (avoids 100x pagination)
    items = API.list_repo_tree(REPO, path=date, recursive=False)
    out = []
    for it in items:
        if it.type != "file":
            continue
        # CDN URL (no auth, bypasses /api/ rate limit)
        cdn = f"https://huggingface.co/datasets/{REPO}/resolve/main/{it.path}"
        out.append({
            "path": it.path,
            "cdn": cdn,
            "size": getattr(it, "size", None)
        })
    return out

files = list_date_files(DATE)
out_path = os.path.join(OUT_DIR, f"{DATE}.json")
with open(out_path, "w") as f:
    json.dump(files, f, indent=2)
print(f"Wrote {len(files)} files -> {out_path}")
```

- Output: `manifest/2026-04-29.json` (CDN-only URLs).  
- CI pre-step: run this once per date, upload `manifest/2026-04-29.json` as artifact.

---

### 2) Deterministic shard → sibling repo routing (spread writes)

`lib/routing.py`
```python
import hashlib

SIBLING_REPOS = [
    "datasets/axentx/surrogate-1-training-pairs",
    "datasets/axentx/surrogate-1-sibling-1",
    "datasets/axentx/surrogate-1-sibling-2",
    "datasets/axentx/surrogate-1-sibling-3",
    "datasets/axentx/surrogate-1-sibling-4",
    "datasets/axentx/surrogate-1-sibling-5",
]

def repo_for_slug(slug: str) -> str:
    """Deterministic routing: hash slug -> sibling repo."""
    h = hashlib.sha256(slug.encode()).hexdigest()
    idx = int(h, 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]
```

- 6 siblings → aggregate write cap ~768/hr (well above 128 per repo).  
- Deterministic by slug (filename without extension) so same content always routes to same repo.

---

### 3) Worker: CDN-only fetch + projection + shard-aware upload

`bin/dataset-enrich.sh`
```bash
#!/usr/bin/env bash
# Same script used by HF Space and GitHub Actions runners.
# Usage:
#   DATE=2026-04-29 SHARD_ID=0 HF_TOKEN=... ./bin/dataset-enrich.sh
set -euo pipefail
export SHELL=/bin/bash

DATE="${DATE:-2026-04-29}"
SHARD_ID="${SHARD_ID:-0}"
N_SHARDS="${N_SHARDS:-16}"
HF_TOKEN="${HF_TOKEN:-}"
WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="${WORKDIR}/manifest/${DATE}.json"

if [[ ! -f "$MANIFEST" ]]; then
  echo "ERROR: manifest not found: $MANIFEST" >&2
  exit 1
fi

python3 - "$@" <<'PY' "$MANIFEST" "$SHARD_ID" "$N_SHARDS" "$HF_TOKEN" "$DATE"
import json, os, sys, hashlib, pyarrow as pa, pyarrow.parquet as pq, io, requests
from lib.routing import repo_for_slug

MANIFEST, SHARD_ID, N_SHARDS, HF_TOKEN, DATE = sys.argv[1:]
SHARD_ID = int(SHARD_ID)
N_SHARDS = int(N_SHARDS)

with open(MANIFEST) as f:
    files = json.load(f)

# Deterministic shard assignment by file path
my_files = [
    f for f in files
    if (int(hashlib.sha256(f["path"].encode()).hexdigest(), 16) % N_SHARDS) == SHARD_ID
]

def cdn_fetch(cdn_url: str) -> bytes:
    # No Authorization header -> CDN-only, bypasses /api/ rate limit
    r = requests.get(cdn_url, timeout=30)
    r.raise_for_status()
    return r.content

def project_to_pair(raw_bytes: bytes, path: str) -> dict:
    # Generic projection: try parquet -> jsonl -> text fallback
    try:
        tbl = pq.read_table(io.BytesIO(raw_bytes))
        df = tbl.to_pandas()
    except Exception:
        # fallback: treat as utf-8 lines
        lines = raw_bytes.decode("utf-8", errors="replace").strip().splitlines()
        # naive heuristic: first non-empty line as prompt, rest as response
        prompt, response = (lines[0], "\n".join(lines[1:])) if lines else ("", "")
        return {"prompt": prompt, "response": response}

    # Heuristic column names (adjust per schema)
    prompt_col = next((c for c in df.columns if "prompt" in c.lower()), df.columns[0])
    resp_col = next((c for c in df.columns if "response" in c

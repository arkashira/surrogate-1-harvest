# surrogate-1 / quality

## Implementation Plan (≤2h)

**Highest-value improvement:** Eliminate Hugging Face API rate-limit exposure during ingestion and training by pre-computing deterministic file lists on the Mac orchestrator and switching all data movement to CDN-only fetches.

### Why this matters
- Avoids `list_repo_files` recursive pagination (100× API calls) and per-file auth checks that trigger 429s.
- CDN downloads (`/resolve/main/`) bypass `/api/` auth and have much higher rate limits.
- Enables Lightning Studio training with zero API calls during data loading (uses embedded file list + CDN fetches).

---

### Changes (concrete)

#### 1. Mac orchestrator: produce deterministic file list (once per date folder)
File: `bin/list-date-folder.sh`
```bash
#!/usr/bin/env bash
# Usage: HF_TOKEN=... ./bin/list-date-folder.sh axentx surrogate-1-training-pairs 2026-05-03 > file-list-2026-05-03.json
set -euo pipefail

REPO_OWNER="${1:-axentx}"
REPO_NAME="${2:-surrogate-1-training-pairs}"
DATE_PATH="${3:-$(date +%Y-%m-%d)}"

python3 - "$REPO_OWNER" "$REPO_NAME" "$DATE_PATH" <<'PY'
import json, os, sys
from huggingface_hub import HfApi

owner, repo, path = sys.argv[1], sys.argv[2], sys.argv[3]
api = HfApi(token=os.environ.get("HF_TOKEN"))

# Non-recursive per-folder listing to avoid 100× pagination
def list_folder(p):
    items = api.list_repo_tree(repo=f"{owner}/{repo}", path=p, recursive=False)
    files, folders = [], []
    for it in items:
        if it.type == "file":
            files.append(it.path)
        else:
            folders.append(it.path)
    return files, folders

# Walk one date folder only (shallow)
all_files = []
try:
    files, folders = list_folder(path)
    all_files.extend(files)
    for f in folders:
        subfiles, _ = list_folder(f)
        all_files.extend(subfiles)
except Exception as e:
    # If folder doesn't exist, return empty list
    sys.stderr.write(f"Warning listing {path}: {e}\n")

print(json.dumps({"date_path": path, "files": sorted(all_files)}, indent=2))
PY
```
Make executable:
```bash
chmod +x bin/list-date-folder.sh
```

#### 2. Worker script: use CDN-only fetches and project to `{prompt,response}` at parse time
File: `bin/dataset-enrich.sh` (minimal diff — replace data-loading section)
```bash
#!/usr/bin/env bash
# ... existing header ...
set -euo pipefail

# Inputs
HF_REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
DATE_PATH="${DATE_PATH:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
N_SHARDS="${N_SHARDS:-16}"
OUTDIR="${OUTDIR:-output}"
FILE_LIST="${FILE_LIST:-file-list.json}"   # produced by list-date-folder.sh

mkdir -p "$OUTDIR"

python3 - "$SHARD_ID" "$N_SHARDS" "$HF_REPO" "$DATE_PATH" "$FILE_LIST" "$OUTDIR" <<'PY'
import json, hashlib, os, sys, pyarrow.parquet as pq, pyarrow as pa
import requests
from io import BytesIO

shard_id = int(sys.argv[1])
n_shards = int(sys.argv[2])
hf_repo = sys.argv[3]
date_path = sys.argv[4]
file_list_path = sys.argv[5]
outdir = sys.argv[6]

with open(file_list_path) as f:
    manifest = json.load(f)

files = [f for f in manifest.get("files", []) if f.startswith(date_path)]
# Deterministic shard assignment by slug-hash (filename)
def shard_for(path):
    slug = os.path.splitext(os.path.basename(path))[0]
    return hash(slug) % n_shards

my_files = [f for f in files if shard_for(f) == shard_id]

def cdn_url(repo, path):
    # Public CDN URL — no Authorization header
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def normalize_to_pair(raw_path):
    url = cdn_url(hf_repo, raw_path)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.content

    try:
        table = pq.read_table(BytesIO(data))
    except Exception:
        # Fallback: try line-delimited JSON if parquet fails
        lines = data.decode().strip().splitlines()
        pairs = []
        for ln in lines:
            if not ln.strip():
                continue
            obj = json.loads(ln)
            prompt = obj.get("prompt") or obj.get("input") or obj.get("text") or ""
            response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
            if prompt and response:
                pairs.append({"prompt": prompt, "response": response})
        return pairs

    # Project to {prompt, response} only at parse time
    cols = set(table.column_names)
    prompt_col = next((c for c in ("prompt", "input", "text") if c in cols), None)
    response_col = next((c for c in ("response", "output", "completion") if c in cols), None)

    if prompt_col and response_col:
        df = table.select([prompt_col, response_col]).to_pandas()
        df.columns = ["prompt", "response"]
        return df.to_dict(orient="records")
    else:
        # Best-effort: if only one column, treat as prompt and leave response empty
        single = next(iter(cols), None)
        if single:
            vals = table.column(single).to_pylist()
            return [{"prompt": v, "response": ""} for v in vals if v is not None]
        return []

rows = []
for f in my_files:
    try:
        pairs = normalize_to_pair(f)
        for p in pairs:
            if p.get("prompt") and p.get("response"):
                rows.append(p)
    except Exception as e:
        sys.stderr.write(f"Skipping {f}: {e}\n")

# Dedup by content hash (lightweight)
def content_hash(r):
    return hashlib.md5(f"{r['prompt']}\n{r['response']}".encode()).hexdigest()

seen = set()
deduped = []
for r in rows:
    h = content_hash(r)
    if h in seen:
        continue
    seen.add(h)
    deduped.append(r)

# Output shard file
ts = os.popen("date -u +%H%M%S").read().strip()
out_name = f"shard{shard_id}-{ts}.jsonl"
out_path = os.path.join(outdir, out_name)
with open(out_path, "w") as f:
    for r in deduped:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

print(json.dumps({"shard": shard_id, "files_processed": len(my_files), "pairs": len(deduped), "out": out_path}))
PY
```

#### 3. Training script: embed file list and use CDN-only fetches (Lightning Studio)
File: `train.py` (minimal diff — data module section)
```python
# ... existing imports ...
from huggingface_hub import HfApi
import requests, json, os, tempfile, pyarrow.parquet as pq
from io import BytesIO

HF_REPO = "axentx/surrogate-1-training-pairs"
DATE_PATH = "2026-05-03"

def cdn_url(path):
    return f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{path}"

def load_file_list():
    # Pre-computed on Mac orchestrator and baked into repo or passed via artifact
    list_path = "file-list.json"
    if os.path.exists(list_path):
        with open(list_path) as f:
            return json.load(f)["files"]
    # Fallback: single non-recursive listing (only if file-list missing)
    api =

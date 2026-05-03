# surrogate-1 / backend

## Implementation Plan: CDN-bypass ingestion + shard reuse (≤2h)

**Highest-value change**: Replace HF API–driven dataset streaming in workers with a **single pre-list + CDN-only download** strategy. This removes 429 rate-limit risk during ingestion, cuts HF API calls to one per workflow run, and keeps the 16-shard parallel design intact.

### Concrete steps
1. Add `bin/list-and-embed.sh`  
   - Runs once per workflow (on the Mac/orchestrator) after rate-limit window is clear.  
   - Calls `list_repo_tree(recursive=False)` for today’s folder (or latest folder in `public-merged/`).  
   - Emits `file-list.json` (array of `{ "path": "...", "sha256": "..." }`) into workspace.

2. Update `bin/dataset-enrich.sh`  
   - Accept `FILE_LIST` (path to JSON) and `SHARD_ID`/`SHARD_TOTAL`.  
   - Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`.  
   - For each assigned file, download via **CDN URL** (`https://huggingface.co/datasets/.../resolve/main/...`) with `curl --retry 3 --retry-delay 5`.  
   - Stream-parse (parquet/jsonl), project to `{prompt, response}`, compute content-md5, call dedup, emit normalized JSONL.  
   - Upload shard output to `batches/public-merged/<YYYYMMDD>/shard<SHARD_ID>-<HHMMSS>.jsonl` via `huggingface_hub` upload_file (single commit per shard).

3. Update GitHub Actions matrix (`.github/workflows/ingest.yml`)  
   - Add an initial non-matrix job `prepare-list` that produces `file-list.json` as artifact.  
   - Pass artifact to the 16-shard matrix jobs.  
   - Keep `HF_TOKEN` permission scope unchanged.

4. Dedup store (`lib/dedup.py`)  
   - No change to interface; ensure it uses centralized SQLite on HF Space if reachable, else local fallback (current behavior).  
   - Add `exists_batch(hashes)` to reduce round-trips.

5. Lightning training script changes (for downstream consumer)  
   - Embed the same `file-list.json` in training repo.  
   - `train.py` does **only CDN fetches** during DataLoader; zero HF API calls while iterating data.

---

## Code snippets

### bin/list-and-embed.sh
```bash
#!/usr/bin/env bash
# list-and-embed.sh
# Usage: ./list-and-embed.sh <dataset_owner> <dataset_name> <folder> <out.json>
set -euo pipefail

OWNER="${1:-axentx}"
REPO="${2:-surrogate-1-training-pairs}"
FOLDER="${3:-batches/public-merged/$(date +%Y%m%d)}"
OUT="${4:-file-list.json}"

# Requires huggingface_hub installed in the runner (already in requirements)
python3 - "$OWNER" "$REPO" "$FOLDER" "$OUT" <<'PY'
import json
import os
import sys
from huggingface_hub import HfApi

owner, repo, folder, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
api = HfApi()

# Non-recursive list for the folder (avoids 100x pagination on deep trees)
entries = api.list_repo_tree(repo=f"{owner}/{repo}", path=folder, repo_type="dataset", recursive=False)

files = []
for e in entries:
    if not e.path.startswith(folder):
        continue
    files.append({"path": e.path, "sha256": getattr(e, "sha256", "")})

os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    json.dump(files, f, indent=2)

print(f"Wrote {len(files)} entries to {out}")
PY
```

### bin/dataset-enrich.sh (excerpt — core loop)
```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Usage: SHARD_ID=0 SHARD_TOTAL=16 FILE_LIST=file-list.json ./dataset-enrich.sh
set -euo pipefail

SHARD_ID="${SHARD_ID:?required}"
SHARD_TOTAL="${SHARD_TOTAL:?required}"
FILE_LIST="${FILE_LIST:?required}"
HF_REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
DATE_TAG="$(date +%Y%m%d)"
OUT_NAME="shard${SHARD_ID}-$(date +%H%M%S).jsonl"
OUT_PATH="batches/public-merged/${DATE_TAG}/${OUT_NAME}"
TMP_OUT="$(mktemp)"

python3 - "$SHARD_ID" "$SHARD_TOTAL" "$FILE_LIST" "$TMP_OUT" <<'PY'
import hashlib
import json
import os
import sys
import pyarrow.parquet as pq
import pyarrow as pa
import requests
from pathlib import Path

SHARD_ID = int(sys.argv[1])
SHARD_TOTAL = int(sys.argv[2])
FILE_LIST = sys.argv[3]
TMP_OUT = sys.argv[4]

HF_DATASETS_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
CDN_PREFIX = f"https://huggingface.co/datasets/{HF_DATASETS_REPO}/resolve/main"

def belongs_to_shard(slug: str) -> bool:
    h = int(hashlib.sha256(slug.encode("utf-8")).hexdigest(), 16)
    return (h % SHARD_TOTAL) == SHARD_ID

def download_cdn(path: str, dest: str) -> None:
    url = f"{CDN_PREFIX}/{path}"
    # Retry on transient CDN/network errors
    for attempt in range(3):
        try:
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep((attempt + 1) * 2)

def content_md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

# Import dedup lazily (assumes lib/ is available)
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore
dedup = DedupStore()

with open(FILE_LIST, "r", encoding="utf-8") as f:
    entries = json.load(f)

written = 0
with open(TMP_OUT, "w", encoding="utf-8") as out_f:
    for ent in entries:
        path = ent["path"]
        slug = path
        if not belongs_to_shard(slug):
            continue

        local_file = f"/tmp/hf_{hashlib.md5(path.encode()).hexdigest()}"
        try:
            download_cdn(path, local_file)
        except Exception as e:
            print(f"Failed to download {path}: {e}", file=sys.stderr)
            continue

        try:
            # Try parquet first
            try:
                table = pq.read_table(local_file, columns=["prompt", "response"])
                prompts = table.column("prompt").to_pylist()
                responses = table.column("response").to_pylist()
            except Exception:
                # Fallback to jsonl
                prompts, responses = [], []
                with open(local_file, "r", encoding="utf-8") as lf:
                    for line in lf:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        prompts.append(obj.get("prompt", ""))
                        responses.append(obj.get("response", ""))

            hashes = [content_md5(p + r) for p, r in zip(prompts, responses)]
            existing = set(dedup.exists_batch(hashes))

            for p, r, h in zip(prompts, responses, hashes):
                if h in existing:
                    continue
                obj = {"prompt": p, "response": r}
                out_f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                written += 1
                dedup.add(h)  # optimistic add; central store will enforce uniqueness
       

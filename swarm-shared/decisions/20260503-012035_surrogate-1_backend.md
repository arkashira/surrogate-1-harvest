# surrogate-1 / backend

## Highest-value incremental improvement
Replace recursive HF API ingestion and per-file authenticated fetches with **single non-recursive `list_repo_tree` per folder + CDN-only downloads** and **project-to-schema at parse time** (no `streaming=True` on heterogeneous repos). This removes 429 rate-limit risk, avoids `pyarrow` schema errors, and keeps GitHub Actions runners fast and isolated.

---

## Implementation plan (≤2h)

1. **Mac orchestrator** (run once per date folder after rate-limit window clears)
   - Use `huggingface_hub.list_repo_tree(repo_id, path="public-raw/{date}", recursive=False)` to list subfolders/files.
   - Save relative paths to `filelist-{date}.json` and commit to repo (or pass via artifact to Actions).
   - No file contents downloaded; only metadata.

2. **Worker script changes** (`bin/dataset-enrich.sh` and Python helpers)
   - Accept `DATE` and `FILELIST` (JSON) as inputs.
   - For each entry in `FILELIST`, download via CDN URL:
     ```
     https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main/public-raw/{date}/{rel_path}
     ```
     (No Authorization header; CDN tier has much higher limits.)
   - Parse each file individually; project only `{prompt, response}` fields at parse time.
   - Do **not** use `load_dataset(..., streaming=True)` on the full repo.

3. **Schema normalization & dedup**
   - Keep existing `lib/dedup.py` behavior (central md5 store).
   - Normalize each record to `{prompt: str, response: str, _source_file: str, _sha256: str}`.
   - Write output as newline JSON: `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`.

4. **GitHub Actions changes** (`ingest.yml`)
   - Add step to fetch `filelist-{date}.json` artifact (or generate if missing and cache).
   - Pass `DATE` and `SHARD_ID` to worker; worker loads filelist and processes only its deterministic shard (`hash(slug) % 16 == SHARD_ID`).
   - Keep matrix `16`; runners remain isolated.

5. **Error handling & retries**
   - On CDN 429/5xx: exponential backoff (max 3 retries) per file.
   - On parse failure: log and skip file (do not abort whole shard).
   - After 429 from API (if list step fails): wait 360s and retry.

6. **Validation & smoke test**
   - Run worker locally with a small filelist (3–5 files) and verify output schema.
   - Confirm zero authenticated API calls during data fetch (only CDN GETs).

---

## Code snippets

### 1) Orchestrator script (Mac) — generate filelist
```bash
#!/usr/bin/env bash
# generate-filelist.sh
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE="${1:-$(date +%Y-%m-%d)}"
OUT="filelist-${DATE}.json"

python3 - <<PY
import json, os
from huggingface_hub import list_repo_tree

repo = os.environ.get("REPO", "$REPO")
date = os.environ.get("DATE", "$DATE")
path = f"public-raw/{date}"

entries = []
for entry in list_repo_tree(repo, path=path, recursive=False):
    # entry.path is relative to repo root
    entries.append(entry.path)

with open(os.environ.get("OUT", "$OUT"), "w") as f:
    json.dump(entries, f, indent=2)

print(f"Wrote {len(entries)} entries to {os.environ.get('OUT', '$OUT')}")
PY
```

### 2) Python helper — parse & project single file (CDN download)
```python
# lib/parse_cdn.py
import json, hashlib, sys, requests, os
from typing import Dict, Any, Iterator

CDN_ROOT = "https://huggingface.co/datasets/axentx/surrogate-1-training-pairs/resolve/main"

def cdn_url(rel_path: str) -> str:
    return f"{CDN_ROOT}/{rel_path.lstrip('/')}"

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def project_record(raw: Dict[str, Any], source_file: str) -> Dict[str, str]:
    # Best-effort projection to {prompt, response}
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or ""
    return {
        "prompt": str(prompt).strip(),
        "response": str(response).strip(),
        "_source_file": source_file,
    }

def stream_cdn_jsonl(rel_path: str) -> Iterator[Dict[str, str]]:
    url = cdn_url(rel_path)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.content  # raw bytes to avoid decode issues
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except Exception:
            # fallback: try whole-file JSON array
            continue
        projected = project_record(raw, rel_path)
        projected["_sha256"] = sha256_bytes(json.dumps(projected, sort_keys=True).encode())
        yield projected

if __name__ == "__main__":
    rel_path = sys.argv[1]
    for rec in stream_cdn_jsonl(rel_path):
        print(json.dumps(rec, ensure_ascii=False))
```

### 3) Worker snippet (used by `dataset-enrich.sh`)
```bash
#!/usr/bin/env bash
# bin/dataset-enrich.sh (updated worker portion)
set -euo pipefail

DATE="${DATE:-$(date +%Y-%m-%d)}"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
FILELIST="${FILELIST:-filelist-${DATE}.json}"
OUT_DIR="batches/public-merged/${DATE}"
TS=$(date +%H%M%S)
OUT_FILE="${OUT_DIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "$OUT_DIR"

python3 - <<PY
import json, hashlib, sys, os
from lib.dedup import DedupStore
from lib.parse_cdn import stream_cdn_jsonl

shard_id = int(os.environ["SHARD_ID"])
total_shards = int(os.environ["TOTAL_SHARDS"])
filelist_path = os.environ["FILELIST"]
out_path = os.environ["OUT_FILE"]

with open(filelist_path) as f:
    files = json.load(f)

# deterministic shard assignment by file path
def assign_shard(path: str) -> int:
    h = int(hashlib.sha256(path.encode()).hexdigest(), 16)
    return h % total_shards

dedup = DedupStore()
written = 0
with open(out_path, "w") as out:
    for rel_path in files:
        if assign_shard(rel_path) != shard_id:
            continue
        try:
            for rec in stream_cdn_jsonl(rel_path):
                if dedup.seen(rec["_sha256"]):
                    continue
                dedup.add(rec["_sha256"])
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written += 1
        except Exception as exc:
            print(f"WARN: failed {rel_path}: {exc}", file=sys.stderr)

print(f"Shard {shard_id}: wrote {written} records to {out_path}")
PY
```

### 4) GitHub Actions matrix step (excerpt)
```yaml
# .github/workflows/ingest.yml (excerpt)
jobs:
  ingest:
    strategy:
      matrix:
        shard_id: [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    steps:
      - uses: actions/checkout@v4
      - name: Fetch filelist artifact
        uses: actions/download-artifact@v4
        with:
          name: filelist-${{ env.D

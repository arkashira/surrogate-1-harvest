# surrogate-1 / backend

## Final Synthesized Implementation (Best Parts + Correctness + Actionability)

**Goal (unified):**  
Eliminate HF API 429s and Space OOM by replacing recursive `list_repo_files` and per-file API calls with **one per-folder `list_repo_tree` + CDN-only fetches**, and project to `{prompt,response}` at parse time.

**Why this wins (unified):**  
- Avoids recursive pagination and per-file metadata API calls (major 429 source).  
- CDN `/resolve/main/` downloads bypass `/api/` auth checks and have much higher rate limits.  
- A single manifest per date folder enables zero HF API calls during Lightning training.  
- Fits within 2 hours: only `bin/dataset-enrich.sh`, a small manifest builder, and `lib/dedup.py` changes; no infra/workflow changes required (but optional CI integration provided).

---

## Minimal, Correct, Actionable Implementation Plan (≤2h)

1. **Add manifest generator** (`bin/build-manifest.sh`) — run once per date folder before shards start (locally or in CI).  
   - Uses `huggingface_hub.HfApi.list_repo_tree(path, recursive=False)` for the target date folder.  
   - Emits `batches/public-merged/<date>/manifest.json` with `{repo, path, sha, size}` for every parquet/jsonl file.  
   - Exits non-zero on failure so CI fails fast.

2. **Update worker script** (`bin/dataset-enrich.sh`)  
   - Accepts `MANIFEST_FILE` env var. If present, reads file list from it (no HF API).  
   - Downloads each file via `curl -L "https://huggingface.co/datasets/$repo/resolve/main/$path"` (public CDN, no auth).  
   - Stream-parses with pyarrow/pandas and projects only `prompt`/`response`; drops all other columns.  
   - Keeps deterministic hash-bucketing (`slug-hash % TOTAL_SHARDS == SHARD_ID`) and existing dedup via `lib/dedup.py`.  
   - Output unchanged: `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.

3. **Update dedup helper** (`lib/dedup.py`)  
   - No functional change; ensure it is import-safe and idempotent for concurrent writers (use single-writer per shard pattern).

4. **Optional CI integration** (`.github/workflows/ingest.yml`)  
   - Add a `build-manifest` job that produces `manifest.json` as an artifact and passes path to shards via `env.MANIFEST_FILE`.  
   - Keep 16-shard matrix unchanged.

5. **Validation checklist**  
   - Run one shard locally on a small date folder; confirm zero `huggingface_hub` API calls during fetch (only during manifest build).  
   - Confirm output schema is exactly `{prompt, response}` and no extra columns leak.  
   - Confirm dedup store is updated correctly and concurrent shard runs do not corrupt it.

---

## Code Snippets (Corrected + Actionable)

### bin/build-manifest.sh
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${HF_REPO:-axentx/surrogate-1-training-pairs}"
DATE="${1:-$(date +%Y-%m-%d)}"
OUTDIR="batches/public-merged/${DATE}"
MANIFEST="${OUTDIR}/manifest.json"

mkdir -p "${OUTDIR}"

python3 - <<PY
import json, os, sys
from huggingface_hub import HfApi

api = HfApi()
repo = os.environ.get("HF_REPO")
if not repo:
    sys.exit("HF_REPO not set")
date = os.environ.get("DATE", "$DATE")
manifest_path = os.environ.get("MANIFEST_PATH", "$MANIFEST")

tree = api.list_repo_tree(repo, path=date, recursive=False)

entries = []
for item in tree:
    rfn = getattr(item, "rfilename", "")
    if rfn.endswith((".parquet", ".jsonl")):
        entries.append({
            "repo": repo,
            "path": rfn,
            "sha": getattr(item, "sha", ""),
            "size": getattr(item, "size", 0),
        })

os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
with open(manifest_path, "w") as f:
    json.dump(entries, f, indent=2)

print(f"Wrote {len(entries)} entries to {manifest_path}")
PY
```

### bin/dataset-enrich.sh (key, corrected)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
SHARD_ID="${SHARD_ID:-0}"
TOTAL_SHARDS="${TOTAL_SHARDS:-16}"
MANIFEST_FILE="${MANIFEST_FILE:-}"
OUTDIR="batches/public-merged/$(date +%Y-%m-%d)"
DEDUP_DB="/opt/axentx/surrogate-1/lib/dedup.db"

mkdir -p "${OUTDIR}"

project_file() {
  local file="$1"
  if [[ "$file" == *.parquet ]]; then
    python3 -c "
import pyarrow.parquet as pq, json, sys
tbl = pq.read_table(sys.argv[1], columns=['prompt','response'])
for b in tbl.to_batches():
    d = b.to_pydict()
    for i in range(len(d['prompt'])):
        print(json.dumps({'prompt': d['prompt'][i], 'response': d['response'][i]}))
" "$file"
  elif [[ "$file" == *.jsonl ]]; then
    python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        print(json.dumps({'prompt': obj['prompt'], 'response': obj['response']}))
" "$file"
  else
    echo "Unsupported file: $file" >&2
    return 1
  fi
}

download_and_project() {
  local repo="$1"
  local path="$2"
  local url="https://huggingface.co/datasets/${repo}/resolve/main/${path}"
  local tmp
  tmp=$(mktemp)
  curl -fsSL --retry 3 --retry-delay 5 -o "$tmp" "$url" || { rm -f "$tmp"; return 1; }
  project_file "$tmp"
  rm -f "$tmp"
}

process_from_manifest() {
  local manifest="$1"
  python3 -c "
import json, hashlib, os, subprocess, sys

with open(sys.argv[1]) as f:
    entries = json.load(f)

shard_id = int(os.environ['SHARD_ID'])
total_shards = int(os.environ['TOTAL_SHARDS'])

for e in entries:
    slug = e['path'].rsplit('/', 1)[-1].rsplit('.', 1)[0]
    h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    if h % total_shards != shard_id:
        continue
    url = f\"https://huggingface.co/datasets/{e['repo']}/resolve/main/{e['path']}\"
    cmd = ['curl', '-fsSL', '--retry', '3', '--retry-delay', '5', '-o', '/dev/stdout', url]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        print(f'Failed to download {url}: {proc.stderr.decode()}', file=sys.stderr)
        continue
    # project via inline python
    proj = subprocess.run(
        [sys.executable, '-c', open(sys.argv[2]).read()],
        input=proc.stdout,
        capture_output=True
    )
    if proj.returncode != 0:
        print(f'Projection failed for {e[\"path\"]}: {proj.stderr.decode()}', file=sys.stderr)
        continue
    sys.stdout.buffer.write(proj.stdout)
" "$manifest" "$(cat <<'PY'
import sys, json
data = sys.stdin.buffer.read()
# We'll handle parquet/jsonl via file-type detection in outer shell; this inline path is for jsonl-like bytes.
# For safety, rely on shell to call project_file; here we just pass through lines for jsonl case.
# This python snippet is only used for json

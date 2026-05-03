# surrogate-1 / quality

## Final Implementation Plan (≤2h)

**Goal**: Eliminate HF API rate-limit pressure and recursive listing by switching to a single non-recursive `list_repo_tree` per date folder + CDN-only fetches + deterministic shard routing.

### Core Design Decisions (resolved)
- **Single file-list call per run**: Use one `list_repo_tree` per `DATE_FOLDER` and share the file list across all shards in the matrix to avoid N× API calls.
- **Deterministic shard routing**: `hash(slug) % SHARD_TOTAL == SHARD_ID` (MD5, first 8 hex chars as int) — consistent across runs and shards.
- **CDN-only fetches**: `https://huggingface.co/datasets/{repo}/resolve/main/{DATE_FOLDER}/{file}` with no auth header.
- **No new columns**: Output strictly `{prompt, response}`; do not add `source`/`ts`.
- **Dedup unchanged**: Central SQLite dedup store remains source of truth; runners may re-upload duplicates until Space dedup catches them (acceptable trade-off).
- **Idempotent outputs**: Filenames include `shard{N}-{HHMMSS}.jsonl`; reruns produce new files (safe).

---

### Steps (≤2h)

1. Add `bin/list_files.py` (non-recursive file lister) and `bin/cdn_ingest.py` (streaming shard worker).
2. Update `bin/dataset-enrich.sh` to:
   - Accept `DATE_FOLDER`, `SHARD_ID`, `SHARD_TOTAL`.
   - Optionally reuse a shared file list if present; otherwise generate once per shard (safe fallback).
   - Deterministic shard selection and CDN streaming.
   - Append normalized, deduped lines to `batches/public-merged/{DATE_FOLDER}/shard{N}-{HHMMSS}.jsonl`.
3. Update GitHub Actions matrix to pass `DATE_FOLDER`, `SHARD_ID`, `SHARD_TOTAL=16`.
4. Ensure `requirements.txt` includes `huggingface_hub`, `pyarrow`, `numpy`.

---

### Code Snippets

#### bin/list_files.py
```python
#!/usr/bin/env python3
"""
List files in a single HF dataset folder (non-recursive).
Usage:
  python list_files.py <repo> <date_folder> > filelist.json
"""
import json
import sys
from huggingface_hub import HfApi

def main():
    if len(sys.argv) != 3:
        print("Usage: list_files.py <repo> <date_folder>", file=sys.stderr)
        sys.exit(1)
    repo, date_folder = sys.argv[1], sys.argv[2]
    api = HfApi()
    entries = api.list_repo_tree(repo=repo, path=date_folder, recursive=False)
    files = [e.path for e in entries if e.type == "file"]
    json.dump({"repo": repo, "folder": date_folder, "files": files}, sys.stdout)

if __name__ == "__main__":
    main()
```

#### bin/cdn_ingest.py
```python
#!/usr/bin/env python3
"""
Deterministic shard worker: list -> shard-select -> CDN stream -> normalize -> dedup -> emit.
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python cdn_ingest.py <repo> <date_folder> [filelist.json]
If filelist.json is provided, use it; otherwise call list_repo_tree once.
"""
import json
import os
import sys
import hashlib
from pathlib import Path

import requests
from huggingface_hub import HfApi

# project root
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import is_duplicate, mark_seen

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{folder}/{file}"


def deterministic_shard(file_path: str, shard_total: int) -> int:
    h = hashlib.md5(file_path.encode()).hexdigest()
    return int(h[:8], 16) % shard_total


def load_filelist(repo: str, folder: str, filelist_path: str | None = None):
    if filelist_path and os.path.exists(filelist_path):
        with open(filelist_path) as f:
            data = json.load(f)
        return data.get("files", [])
    api = HfApi()
    entries = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    return [e.path for e in entries if e.type == "file"]


def normalize_record(rec: dict) -> dict:
    prompt = rec.get("prompt") or rec.get("input") or ""
    response = rec.get("response") or rec.get("output") or ""
    return {"prompt": str(prompt), "response": str(response)}


def hash_record(rec: dict) -> str:
    payload = f"{rec['prompt']}\0{rec['response']}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def stream_cdn_file(url: str):
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if line:
                yield line.strip()


def main():
    if len(sys.argv) < 3:
        print("Usage: cdn_ingest.py <repo> <date_folder> [filelist.json]", file=sys.stderr)
        sys.exit(1)

    repo = sys.argv[1]
    folder = sys.argv[2]
    filelist_path = sys.argv[3] if len(sys.argv) > 3 else None

    shard_id = int(os.environ.get("SHARD_ID", "0"))
    shard_total = int(os.environ.get("SHARD_TOTAL", "16"))

    files = load_filelist(repo, folder, filelist_path)
    selected = [f for f in files if deterministic_shard(f, shard_total) == shard_id]

    for file in selected:
        url = CDN_TEMPLATE.format(repo=repo, folder=folder, file=file)
        try:
            for line in stream_cdn_file(url):
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    # skip non-json lines; parquet handled separately if needed
                    continue
                nr = normalize_record(rec)
                h = hash_record(nr)
                if is_duplicate(h):
                    continue
                mark_seen(h)
                print(json.dumps(nr, ensure_ascii=False))
        except Exception as e:
            print(f"Error processing {url}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
```

#### bin/dataset-enrich.sh
```bash
#!/usr/bin/env bash
set -euo pipefail

# Required env
: "${HF_TOKEN:?Need HF_TOKEN}"
: "${REPO:?Need repo (e.g. axentx/surrogate-1-training-pairs)}"
: "${DATE_FOLDER:?Need DATE_FOLDER (e.g. 2026-05-03)}"
: "${SHARD_ID:?Need SHARD_ID (0..15)}"
: "${SHARD_TOTAL:?Need SHARD_TOTAL (16)}"

WORKDIR=$(mktemp -d)
cd "$WORKDIR"

# Optional shared filelist: if present at repo root, reuse to avoid per-shard API calls.
# Otherwise each shard will call list_repo_tree once (safe fallback).
FILELIST="/opt/axentx/surrogate-1/filelist-${DATE_FOLDER}.json"
if [[ -f "$FILELIST" ]]; then
  echo "Using shared filelist: $FILELIST"
  USE_FILELIST="$FILELIST"
else
  echo "No shared filelist; will generate per shard."
  USE_FILELIST=""
fi

OUTDIR="/opt/axentx/surrogate-1/batches/public-merged/$DATE_FOLDER"
mkdir -p "$OUTDIR"
TS=$(date +%H%M%S)
OUTFILE="$OUTDIR/shard${SHARD_ID}-${TS}.jsonl"

# Stream via cdn_ingest.py
python3 /opt/axentx/surrogate-1/bin/cdn_ingest.py "$REPO" "$DATE_FOLDER" ${USE_FILELIST:+"$USE_FILELIST"} >> "$OUTFILE"

echo "Shard $SHARD_ID output: $OUTFILE"
```

#### .github/workflows/ingest.yml (partial update)
```yaml
jobs:
  ingest:
    strategy:


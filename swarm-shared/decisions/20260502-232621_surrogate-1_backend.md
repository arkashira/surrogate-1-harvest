# surrogate-1 / backend

## Final Implementation Plan (≤2h)

**Goal**: Eliminate runtime `load_dataset(streaming=True)` and recursive `list_repo_files` from `bin/dataset-enrich.sh`. Replace with deterministic pre-flight snapshot + CDN-only fetches to avoid HF API rate limits and schema errors.

### Core Changes
1. **Add `bin/list-snapshot.sh`** — runs once per cron tick (or locally on Mac) to call `list_repo_tree` non-recursively for today’s folder and emit `snapshot.json` with CDN URLs and sizes.
2. **Update `bin/dataset-enrich.sh`** — remove `load_dataset(streaming=True)` and recursive listing. Accept a snapshot file; deterministically assign shards by path hash; download via CDN with `curl`/`wget` or lightweight Python fetcher; project `{prompt,response}`; dedup; write shard output.
3. **Add lightweight Python fetcher** (`bin/fetch_cdn.py`) to stream-download and parse parquet/jsonl safely without `datasets`, with column projection and fallback aliases.
4. **Update workflow** — generate snapshot in a prior job (or before matrix), pass as artifact or file to all runners so all shards use the same deterministic list and zero API calls during ingest.

### Why This Fits Patterns
- Avoids `load_dataset(streaming=True)` on mixed-schema repos (HF `CastError`).
- Avoids recursive `list_repo_files` (rate-limit 429) by using `list_repo_tree(path, recursive=False)` once per folder.
- Uses HF CDN bypass (no auth, higher limits) during training/ingest.
- Keeps shard isolation and upload filename format (`batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`).

---

## Code Snippets

### 1) `bin/list-snapshot.sh`
Deterministic snapshot for today’s folder. Uses `list_repo_tree` non-recursively and emits CDN URLs.

```bash
#!/usr/bin/env bash
# Generate deterministic snapshot for today's folder.
# Usage: HF_TOKEN=... ./bin/list-snapshot.sh > snapshot.json
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE=$(date -u +%Y-%m-%d)
FOLDER="public-raw/${DATE}"  # adjust if your layout differs

# list single-level tree for the date folder (non-recursive)
python3 - "$REPO" "$FOLDER" <<'PY'
import os, json, sys
from huggingface_hub import HfApi

repo = sys.argv[1]
folder = sys.argv[2].rstrip("/")
api = HfApi(token=os.environ.get("HF_TOKEN"))
entries = api.list_repo_tree(repo=repo, path=folder, recursive=False)

out = []
for e in entries:
    if e.type != "file":
        continue
    # CDN URL bypasses API auth/rate limits
    cdn = f"https://huggingface.co/datasets/{repo}/resolve/main/{e.path}"
    out.append({"path": e.path, "cdn": cdn, "size": e.size})

sys.stdout.write(json.dumps({"date": folder, "entries": out}, indent=2))
PY
```

Make executable:
```bash
chmod +x bin/list-snapshot.sh
```

---

### 2) `bin/fetch_cdn.py`
Lightweight fetcher that downloads via CDN and yields `{prompt, response}` rows. Supports `.jsonl` and `.parquet` with column projection and fallback aliases.

```python
#!/usr/bin/env python3
"""
Fetch a single file via CDN and yield {prompt, response} rows.
Supports .jsonl and .parquet (via pyarrow, column projection only).
"""
import sys
import json
from pathlib import Path
from typing import Iterator, Dict, Any

try:
    import pyarrow.parquet as pq
    import pyarrow as pa
    import requests
    from io import BytesIO
except ImportError as e:
    print(f"Missing dep: {e}", file=sys.stderr)
    sys.exit(1)

CDN_PREFIX = "https://huggingface.co/datasets"

def iter_parquet(cdn_url: str, repo: str) -> Iterator[Dict[str, Any]]:
    # Stream download
    resp = requests.get(cdn_url, timeout=60)
    resp.raise_for_status()
    buf = BytesIO(resp.content)
    try:
        table = pq.read_table(buf, columns=["prompt", "response"])
    except (pa.lib.ArrowInvalid, KeyError):
        # Fallback: try common aliases
        try:
            table = pq.read_table(buf, columns=["instruction", "output"])
            # rename for downstream consistency
            table = table.rename_columns(["prompt", "response"])
        except Exception:
            # Last resort: read all and project
            table = pq.read_table(buf)
            if "prompt" not in table.column_names:
                # try to find any text pair
                text_cols = [c for c in table.column_names if table.schema.field(c).type in (pa.string(), pa.large_string())]
                if len(text_cols) >= 2:
                    table = table.select([text_cols[0], text_cols[1]]).rename_columns(["prompt", "response"])
                else:
                    raise ValueError(f"No prompt/response columns found in {cdn_url}")
    df = table.to_pandas()
    for _, row in df.iterrows():
        yield {"prompt": str(row["prompt"]), "response": str(row["response"])}

def iter_jsonl(cdn_url: str) -> Iterator[Dict[str, Any]]:
    resp = requests.get(cdn_url, stream=True, timeout=60)
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        obj = json.loads(line)
        # Normalize keys
        prompt = obj.get("prompt") or obj.get("instruction") or obj.get("input") or ""
        response = obj.get("response") or obj.get("output") or obj.get("completion") or ""
        yield {"prompt": str(prompt), "response": str(response)}

def iter_file(cdn_url: str, repo: str) -> Iterator[Dict[str, Any]]:
    if cdn_url.endswith(".parquet"):
        yield from iter_parquet(cdn_url, repo)
    else:
        # assume jsonl or line-delimited json
        yield from iter_jsonl(cdn_url)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: fetch_cdn.py <repo> <cdn_url>", file=sys.stderr)
        sys.exit(1)
    repo = sys.argv[1]
    url = sys.argv[2]
    for row in iter_file(url, repo):
        print(json.dumps(row, ensure_ascii=False))
```

Make executable:
```bash
chmod +x bin/fetch_cdn.py
```

---

### 3) Updated `bin/dataset-enrich.sh`
Core changes: deterministic shard assignment by path hash, CDN-only fetches, and projection to `{prompt,response}`.

```bash
#!/usr/bin/env bash
# dataset-enrich.sh
# Updated to use CDN-only fetches and deterministic snapshot.
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
WORKDIR=$(mktemp -d)
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

# Accept snapshot file via arg or env; if not present, try to generate (requires HF_TOKEN)
SNAPSHOT="${1:-${SNAPSHOT_FILE:-}}"
if [[ -z "$SNAPSHOT" || ! -f "$SNAPSHOT" ]]; then
  echo "No snapshot provided; attempting to generate snapshot for today..." >&2
  if [[ -z "${HF_TOKEN:-}" ]]; then
    echo "HF_TOKEN required to generate snapshot" >&2
    exit 1
  fi
  SNAPSHOT="$WORKDIR/snapshot.json"
  HF_TOKEN="$HF_TOKEN" ./bin/list-snapshot.sh > "$SNAPSHOT"
fi

SHARD_ID="${SHARD_ID:-0}"
N_SHARDS="${N_SHARDS:-16}"
DATE=$(date -u +%Y-%m-%d)
OUTDIR="batches/public-merged/${DATE}"
TS=$(date -u +%H%M%S)
OUTFILE="${OUTDIR}/shard${SHARD_ID}-${TS}.jsonl"

mkdir -p "$(dirname "$OUTFILE")"

# Deterministic shard assignment by path hash
python3 - "$SNAPSHOT" "$SHARD_ID" "$N_SHARDS" <<'PY'

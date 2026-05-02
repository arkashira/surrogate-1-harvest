# surrogate-1 / backend

## Implementation Plan (≤2h)

**Highest-value improvement**: Deterministic pre-flight file listing + CDN-only ingestion to eliminate HF API 429s during training and make shard workers resilient, with deterministic sibling-repo sharding for write throughput.

### Changes
1. Add `bin/list-files.py` — single Mac-side script that lists one date folder via `list_repo_tree(recursive=False)` and writes `file-list.json` (path + size + etag). Embed this list in training/shard scripts so Lightning training and GitHub Actions workers do **CDN-only** downloads (`https://huggingface.co/datasets/.../resolve/main/...`) with zero API calls during data load.
2. Update `bin/dataset-enrich.sh` to accept an optional `FILE_LIST` path; if provided, iterate the local list instead of calling HF API to enumerate files. Fallback to current behavior if not provided.
3. Add deterministic sibling-repo routing to avoid HF commit cap (128/hr/repo): `repo = f"axentx/surrogate-1-training-pairs-{hash(slug) % 5}"` for writes; keep reads from canonical dataset.
4. Add small Python helper (`lib/cdn.py`) with robust CDN download (retry, range, timeout) and schema projection to `{prompt, response}` only.
5. Update README with usage and the HF CDN bypass note.

### Estimated time
- `bin/list-files.py` + tests: ~25m
- `bin/dataset-enrich.sh` update: ~20m
- `lib/cdn.py` + `lib/dedup.py` tweak: ~25m
- README + example: ~15m
- Buffer/testing: ~35m

---

## Code snippets

### 1) bin/list-files.py
```python
#!/usr/bin/env python3
"""
List files in a single date folder (non-recursive) for surrogate-1 dataset.
Run from Mac (or any dev machine) after rate-limit window clears.

Usage:
  python bin/list-files.py --repo axentx/surrogate-1-training-pairs \
    --folder batches/public-merged/2026-05-02 \
    --out file-list.json
"""

import argparse
import json
import os
import sys
from typing import List, Dict

from huggingface_hub import HfApi

def list_date_folder(repo_id: str, folder: str, token: str | None = None) -> List[Dict]:
    api = HfApi(token=token or os.getenv("HF_TOKEN"))
    # recursive=False => one page, no per-file API cost during training
    entries = api.list_repo_tree(repo_id=repo_id, path=folder, recursive=False)
    out = []
    for e in entries:
        if e.type != "file":
            continue
        out.append({
            "path": e.path,
            "size": getattr(e, "size", None),
            "etag": getattr(e, "etag", None),
        })
    return out

def main() -> None:
    parser = argparse.ArgumentParser(description="List HF dataset folder (non-recursive).")
    parser.add_argument("--repo", required=True, help="Dataset repo id")
    parser.add_argument("--folder", required=True, help="Folder path inside repo")
    parser.add_argument("--out", required=True, help="Output JSON file")
    parser.add_argument("--token", default=None, help="HF token (env HF_TOKEN preferred)")
    args = parser.parse_args()

    try:
        files = list_date_folder(args.repo, args.folder, args.token)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(files, f, indent=2)
        print(f"Wrote {len(files)} files to {args.out}")
    except Exception as exc:
        print(f"Error listing folder: {exc}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

### 2) lib/cdn.py
```python
import os
import time
import hashlib
import json
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from typing import Dict, Any, Optional, Tuple
from pathlib import Path

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def deterministic_repo(slug: str, n_siblings: int = 5) -> str:
    """Choose sibling repo for writes to bypass 128/hr commit cap."""
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    idx = h % n_siblings
    if idx == 0:
        return "axentx/surrogate-1-training-pairs"
    return f"axentx/surrogate-1-training-pairs-{idx}"

def cdn_download(repo: str, path: str, timeout: int = 30, retries: int = 3) -> bytes:
    url = HF_CDN_TEMPLATE.format(repo=repo, path=path)
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            resp.raise_for_status()
            content = b"".join(resp.iter_content(chunk_size=8192))
            return content
        except Exception as exc:
            if attempt == retries:
                raise
            sleep_sec = 2 ** attempt
            time.sleep(sleep_sec)
    raise RuntimeError("unreachable")

def project_to_pair(raw: bytes, source_path: str) -> Optional[Dict[str, str]]:
    """Project heterogeneous file to {prompt, response} only."""
    suffix = Path(source_path).suffix.lower()
    try:
        if suffix == ".jsonl":
            lines = [ln.strip() for ln in raw.decode().splitlines() if ln.strip()]
            # naive: take first object; adjust per your schema knowledge
            for ln in lines:
                obj = json.loads(ln)
                prompt = obj.get("prompt") or obj.get("input") or obj.get("text")
                response = obj.get("response") or obj.get("output") or obj.get("completion")
                if prompt is not None and response is not None:
                    return {"prompt": str(prompt), "response": str(response)}
            return None
        elif suffix == ".parquet":
            tbl = pq.read_table(pa.BufferReader(raw))
            df = tbl.to_pandas()
            # heuristic column names
            prompt_col = next((c for c in df.columns if "prompt" in c.lower()), None)
            response_col = next((c for c in df.columns if "response" in c.lower() or "completion" in c.lower()), None)
            if prompt_col and response_col:
                row = df.iloc[0]
                return {"prompt": str(row[prompt_col]), "response": str(row[response_col])}
            # fallback: first two text cols
            text_cols = [c for c in df.columns if df[c].dtype == "object"]
            if len(text_cols) >= 2:
                row = df.iloc[0]
                return {"prompt": str(row[text_cols[0]]), "response": str(row[text_cols[1]])}
            return None
        else:
            # plain text fallback
            text = raw.decode()
            # crude split; adapt to your corpus
            parts = text.split("\n\n", 1)
            if len(parts) == 2:
                return {"prompt": parts[0].strip(), "response": parts[1].strip()}
            return None
    except Exception:
        return None
```

### 3) bin/dataset-enrich.sh (excerpt)
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="axentx/surrogate-1-training-pairs"
DATE=$(date +%Y-%m-%d)
SHARD="shard${SHARD_ID}"
TS=$(date +%H%M%S)
OUT="batches/public-merged/${DATE}/${SHARD}-${TS}.jsonl"

# Optional pre-computed file list (CDN bypass)
FILE_LIST="${FILE_LIST:-}"

mkdir -p "$(dirname "$OUT")"

if [[ -n "$FILE_LIST" && -f "$FILE_LIST" ]]; then
  echo "Using file list: $FILE_LIST"
  mapfile -t FILES < <(jq -r '.[].path' "$FILE_LIST")
else
  echo "No FILE_LIST provided; falling back to repo listing (may hit API limits)"
  mapfile -t FILES < <(python -c "
import os, json
from huggingface

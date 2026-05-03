# surrogate-1 / discovery

# Final Synthesis — Manifest-Driven Ingestion (Correct + Actionable)

## Core improvement (≤2h)
Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that:
- Eliminates HF API rate limits during training data load by fetching via CDN-only URLs (`resolve/main/...`).
- Prevents mixed-schema `CastError`s by projecting to `{prompt, response}` at parse time.
- Produces deterministic shard outputs compatible with existing `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` layout.
- Keeps the 16-shard matrix workflow intact (only changes worker + adds manifest generator).

---

## Implementation plan (≤2h)

| Step | Owner | Time | Description |
|------|-------|------|-------------|
| 1 | Me | 10m | Add `requirements.txt` updates (`requests`, `tqdm`; keep `pyarrow`, `datasets`, `huggingface_hub`). |
| 2 | Me | 20m | Create `bin/generate_manifest.py` — run once per date folder to list files via `list_repo_tree(recursive=False)` and emit `manifest-<date>.json`. |
| 3 | Me | 40m | Replace `bin/dataset-enrich.sh` with `bin/worker.py` (manifest-driven, CDN-only, schema projection, dedup via `lib/dedup.py`). |
| 4 | Me | 15m | Update `.github/workflows/ingest.yml` to pass `MANIFEST_PATH` and run `python bin/worker.py` with `SHARD_ID`/`SHARD_TOTAL`. |
| 5 | Me | 15m | Add smoke test + local run instructions; ensure executables and shebangs for any remaining shell helpers. |

Total: ~1h 40m.

---

## Code snippets

### 1) requirements.txt (additions)

```text
# existing
datasets
huggingface_hub
pyarrow
numpy

# new
requests>=2.31
tqdm>=4.66
```

---

### 2) bin/generate_manifest.py

Run once per date folder (locally or in workflow) to produce a manifest. Uses HF API sparingly (one tree call per folder) and avoids recursive listing of large repos.

```python
#!/usr/bin/env python3
"""
Generate manifest for a date folder in axentx/surrogate-1-training-pairs.

Usage:
  HF_TOKEN=<token> python bin/generate_manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out manifest-2026-05-03.json

Output manifest format:
{
  "repo": "...",
  "date": "...",
  "files": [
    {"path": "batches/public-merged/2026-05-03/file1.parquet", "size": 12345},
    ...
  ]
}
"""
import argparse
import json
import os
import sys
from typing import Dict, List

from huggingface_hub import HfApi, login

def build_manifest(repo_id: str, date: str, folders: List[str]) -> Dict:
    api = HfApi()
    files = []
    for folder in folders:
        try:
            tree = api.list_repo_tree(repo_id=repo_id, path=folder, repo_type="dataset")
        except Exception as exc:
            print(f"Warning: failed to list {folder}: {exc}", file=sys.stderr)
            continue
        for item in tree:
            if item.rfilename and not item.rfilename.endswith("/"):
                files.append({
                    "path": item.rfilename,
                    "size": item.size or 0,
                })
    manifest = {
        "repo": repo_id,
        "date": date,
        "folders": folders,
        "files": files,
    }
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate manifest for a date folder.")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-05-03")
    parser.add_argument("--folders", nargs="+", default=None,
                        help="Folders to scan (default: batches/public-merged/<date>)")
    parser.add_argument("--out", required=True, help="Output manifest JSON path")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if token:
        login(token=token, add_to_git_credential=False)

    folders = args.folders or [f"batches/public-merged/{args.date}"]
    manifest = build_manifest(args.repo, args.date, folders)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {args.out} ({len(manifest['files'])} files)")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/generate_manifest.py
```

---

### 3) bin/worker.py (replaces dataset-enrich.sh)

Manifest-driven, CDN-only fetches, projects to `{prompt, response}`, dedup via `lib/dedup.py`.

```python
#!/usr/bin/env python3
"""
Shard worker: consumes a manifest and processes a deterministic slice.

Environment:
  SHARD_ID (0-15)        — which shard to process
  SHARD_TOTAL (default 16)
  MANIFEST_PATH          — path to manifest JSON (or URL)
  HF_TOKEN               — optional (only needed for upload)
  OUTPUT_DIR             — where to write shard output (default: ./batches)

Behavior:
- Reads manifest.
- Selects files by hash(path) % SHARD_TOTAL == SHARD_ID.
- Downloads each file via CDN URL (no auth, bypasses API rate limits).
- Projects rows to {prompt, response} (schema-agnostic).
- Deduplicates via lib.dedup (md5 store).
- Writes shard-N-<ts>.jsonl to OUTPUT_DIR.
"""
import json
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from tqdm import tqdm

# local dedup module
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # type: ignore

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def select_shard_files(manifest: Dict[str, Any], shard_id: int, shard_total: int) -> Iterable[Dict[str, Any]]:
    for f in manifest.get("files", []):
        path = f["path"]
        h = int(hashlib.sha256(path.encode()).hexdigest(), 16)
        if h % shard_total == shard_id:
            yield f

def project_row(row: Dict[str, Any]) -> Dict[str, str]:
    """
    Best-effort projection to {prompt, response}.
    Accepts common key variants and falls back to first/second text columns.
    """
    prompt = row.get("prompt") or row.get("input") or row.get("question") or row.get("instruction")
    response = row.get("response") or row.get("output") or row.get("answer") or row.get("completion")

    if prompt is None or response is None:
        # fallback: use first two string-like columns
        keys = [k for k in row.keys() if isinstance(row[k], str)]
        if len(keys) >= 2:
            prompt, response = row[keys[0]], row[keys[1]]
        elif len(keys) == 1:
            prompt, response = row[keys[0]], row[keys[0]]
        else:
            # last resort: empty strings to keep schema
            prompt, response = "", ""
    return {"prompt": str(prompt), "response": str(response)}

def download_parquet(url: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=6

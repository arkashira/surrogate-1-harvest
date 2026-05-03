# surrogate-1 / quality

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

1. Accepts `SHARD_ID` (0-15) and `TOTAL_SHARDS` (16) via env.
2. Reads a pre-generated `manifest.json` (created once per date on the Mac orchestrator) containing the list of files to process for that date.
3. Deterministically hashes each file path → shard assignment (`hash(slug) % TOTAL_SHARDS`) and processes only assigned files.
4. Downloads each assigned file via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (avoids API rate limits during training).
5. Projects heterogeneous schemas to `{prompt, response}` at parse time (avoids `pyarrow.CastError`).
6. Deduplicates via the existing `lib/dedup.py` central md5 store.
7. Emits `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` with only `{prompt, response}` (no extra metadata columns).
8. Commits to `axentx/surrogate-1-training-pairs` using the HF token (write permission required).

### Changes

- Replace `bin/dataset-enrich.sh` with `bin/dataset-enrich.py`.
- Add `bin/gen-manifest.py` (Mac orchestrator) to produce `manifest-YYYY-MM-DD.json` once per date.
- Update `.github/workflows/ingest.yml` to pass `MANIFEST_PATH` and use the new Python worker.
- Keep `lib/dedup.py` unchanged.

---

## Code Snippets

### bin/gen-manifest.py (run once per date on Mac)

```python
#!/usr/bin/env python3
"""
Generate a manifest of files to ingest for a given date.
Usage:
  HF_TOKEN=... python bin/gen-manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out manifest-2026-05-03.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    # list top-level date folder only (avoids recursive pagination on big repos)
    entries = api.list_repo_tree(
        repo_id=args.repo,
        path=args.date,
        recursive=False,
        repo_type="dataset",
    )

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        # expect paths like 2026-05-03/<slug>.parquet
        files.append(entry.path)

    manifest = {
        "repo": args.repo,
        "date": args.date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": sorted(files),
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(files)} files to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/gen-manifest.py
```

---

### bin/dataset-enrich.py (new worker)

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker.

Environment:
  SHARD_ID=0..15
  TOTAL_SHARDS=16 (default 16)
  MANIFEST_PATH=path/to/manifest-YYYY-MM-DD.json
  HF_TOKEN=write token for axentx/surrogate-1-training-pairs
"""
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, hf_hub_download

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa: E402

CDN_BASE = "https://huggingface.co/datasets"

def shard_for_path(path: str, total_shards: int) -> int:
    # deterministic shard assignment by slug (filename without extension)
    slug = Path(path).stem
    digest = hashlib.md5(slug.encode()).hexdigest()
    return int(digest, 16) % total_shards

def download_via_cdn(repo: str, path: str, out_path: Path) -> None:
    """Download via CDN (no auth) to bypass API rate limits."""
    url = f"{CDN_BASE}/{repo}/resolve/main/{path}"
    resp = requests.get(url, timeout=60, stream=True)
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

def project_to_pair(batch: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schema to {prompt, response}.
    Handles common field names; falls back to first/last text-like columns.
    """
    prompt = None
    response = None

    # preferred names
    for pcol in ("prompt", "instruction", "input", "question", "query"):
        if pcol in batch and batch[pcol] is not None:
            prompt = str(batch[pcol])
            break
    for rcol in ("response", "output", "answer", "completion", "text"):
        if rcol in batch and batch[rcol] is not None:
            response = str(batch[rcol])
            break

    # fallback: pick first and last string-like fields
    if prompt is None or response is None:
        keys = [k for k in batch.keys() if isinstance(batch[k], str) and batch[k].strip()]
        if len(keys) >= 2:
            if prompt is None:
                prompt = str(batch[keys[0]])
            if response is None:
                response = str(batch[keys[-1]])

    # last resort
    if prompt is None:
        prompt = ""
    if response is None:
        response = ""
    return {"prompt": prompt, "response": response}

def process_file(
    repo: str,
    path: str,
    dedup: DedupStore,
    seen_md5s: set,
) -> List[Dict[str, str]]:
    """Download, parse, dedup, and project a single file."""
    with tempfile.TemporaryDirectory() as td:
        local_path = Path(td) / Path(path).name
        try:
            download_via_cdn(repo, path, local_path)
        except Exception as e:
            # fallback to hf_hub_download if CDN fails (rare)
            local_path = Path(hf_hub_download(repo_id=repo, filename=path, repo_type="dataset"))

        try:
            table = pq.read_table(local_path)
        except Exception as e:
            print(f"Failed to read parquet {path}: {e}", file=sys.stderr)
            return []

        pairs = []
        for batch in table.to_batches(max_chunksize=1024):
            df = batch.to_pydict()
            rows = len(next(iter(df.values()))) if df else 0
            for i in range(rows):
                row = {k: v[i] for k, v in df.items()}
                # compute md5 across canonical json of row for dedup
                row_json = json.dumps(row, sort_keys=True, separators=(",", ":"))
                md5 = hashlib.md5(row_json.encode()).hexdigest()
                if md5 in seen_md5s:
                    continue
                if dedup.exists(md5):
                    seen_md5s.add(md5)
                    continue

                pair = project_to_pair(row)
                if not pair["prompt"] and not pair["response"]:
                    continue

                dedup.add(md5)
                seen_md5

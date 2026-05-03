# surrogate-1 / backend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- On the orchestrator (Mac/CI), lists the target date folder once via HF API **before rate-limit window closes**, writes `manifest-{DATE_FOLDER}.json` containing only file paths.
- Worker uses **manifest + CDN URLs only** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero HF API calls during training/ingest, bypassing 429 limits.
- Projects every file to `{prompt, response}` at parse time (no schema assumptions), dedups via central md5 store, and writes:
  ```
  batches/public-merged/<DATE_FOLDER>/shard<N>-<HHMMSS>.jsonl
  ```
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`.
- Reuses running Lightning Studio when present; never recreates.
- Uses Lightning `lightning-public-prod` (L40S) for free-tier; falls back gracefully.
- Avoids `load_dataset(streaming=True)` on heterogeneous repos; uses per-file `hf_hub_download` or CDN fetch.

---

## 2. Concrete Steps (≤2h)

1. **Create `bin/dataset-enrich.py`** (replaces `.sh`)
   - Shebang `#!/usr/bin/env python3`
   - CLI: `--shard-id`, `--shard-total`, `--date-folder`, `--manifest`, `--output-dir`
   - Manifest loader/generator:
     - If manifest exists → use it.
     - Else → call HF API **once** to list date folder, save manifest, then proceed.
   - CDN fetch function:
     - Build URL: `f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"`
     - Stream download with `requests.get(..., stream=True)`; no auth header.
   - Schema-agnostic parser:
     - Try parquet → project `{prompt, response}`.
     - Try JSON/JSONL → normalize keys (`prompt`, `response`, `instruction`, `completion`).
     - Skip files that can’t be projected.
   - Dedup:
     - Import `lib/dedup.py` (SQLite md5 store) to check/insert per sample.
   - Output:
     - Write accepted samples to `shard-N-<ts>.jsonl` (one JSON object per line).
     - Commit via HF API using `upload_file` to target path (single commit per shard).

2. **Create `bin/create-manifest.py`** (optional helper for CI)
   - Lists date folder via HF API, writes `manifest-YYYY-MM-DD.json`.
   - Intended to run on Mac/CI after rate-limit clears; workers use resulting manifest.

3. **Update GitHub Actions matrix** (if needed)
   - Keep 16-shard matrix; ensure `SHARD_ID`/`SHARD_TOTAL` env vars passed.
   - Add step to generate/fetch manifest before shard jobs (or embed fallback in worker).

4. **Lightning Studio integration helper** (`bin/lightning_launcher.py`)
   - List running studios; reuse if name matches.
   - If stopped, restart with `Machine.L40S` on `lightning-public-prod`.
   - Launch training via `Studio.run()` pointing to training script that uses the same manifest+CDN strategy.

5. **Validation & smoke test**
   - Run worker locally with a small date folder subset.
   - Verify:
     - CDN-only fetches (no Authorization header).
     - Output file shape and dedup behavior.
     - No HF API calls during data streaming (check logs).

---

## 3. Key Code Snippets

### `bin/dataset-enrich.py` (core worker)

```python
#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import sys
import time
import requests
from pathlib import Path
from typing import List, Dict, Any

import pyarrow.parquet as pq
from huggingface_hub import list_repo_tree, upload_file

REPO = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO}/resolve/main"

def deterministic_shard(slug: str, total: int) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % total

def load_or_create_manifest(date_folder: str, manifest_path: Path) -> List[str]:
    if manifest_path.exists():
        with open(manifest_path) as f:
            return json.load(f)

    # Single API call to list folder (run this sparingly)
    print(f"Listing {date_folder} via HF API...")
    items = list_repo_tree(path=date_folder, repo_id=REPO, recursive=False)
    files = [it["path"] for it in items if it["type"] == "file"]
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w") as f:
        json.dump(files, f)
    return files

def cdn_fetch(url: str, timeout: int = 30) -> bytes:
    # No Authorization header -> CDN bypass
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        chunks = []
        for chunk in r.iter_content(chunk_size=8192):
            chunks.append(chunk)
        return b"".join(chunks)

def project_to_pair(data: Dict[str, Any]) -> Dict[str, str] | None:
    # Normalize keys
    prompt = data.get("prompt") or data.get("instruction") or data.get("input")
    response = data.get("response") or data.get("completion") or data.get("output")
    if prompt is None or response is None:
        return None
    return {"prompt": str(prompt), "response": str(response)}

def parse_file_content(path: str, content: bytes):
    # Try parquet
    if path.endswith(".parquet"):
        try:
            table = pq.read_table(pa.BufferReader(content))
            df = table.to_pandas()
            for _, row in df.iterrows():
                pair = project_to_pair(row.to_dict())
                if pair:
                    yield pair
            return
        except Exception:
            pass

    # Try JSON/JSONL
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return

    # JSONL
    if "\n" in text.strip():
        for line in text.strip().splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                pair = project_to_pair(obj)
                if pair:
                    yield pair
            except Exception:
                continue
        return

    # Single JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            for item in obj:
                pair = project_to_pair(item)
                if pair:
                    yield pair
        else:
            pair = project_to_pair(obj)
            if pair:
                yield pair
    except Exception:
        pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--shard-total", type=int, default=16)
    parser.add_argument("--date-folder", default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--hf-token", default=None)
    args = parser.parse_args()

    if args.hf_token:
        os.environ["HF_TOKEN"] = args.hf_token

    if args.date_folder is None:
        args.date_folder = time.strftime("%Y-%m-%d")

    if args.manifest is None:
        args.manifest = Path(f"manifest-{args.date_folder}.json")

    files = load_or_create_manifest(args.date_folder, args.manifest)
    my_files = [
        f for f in files
        if deterministic_shard(f, args.shard_total) == args.shard_id
    ]

    # Dedup store (central)
    sys.path.insert(0, str(Path(__file__).parent))
    from lib.dedup import DedupStore
    dedup

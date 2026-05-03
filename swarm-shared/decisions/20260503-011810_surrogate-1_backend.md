# surrogate-1 / backend

## Final Decision (highest-value <2h backend fix)
Replace recursive HF API ingestion and per-file authenticated fetches with:

1. **Single non-recursive `list_repo_tree(path, recursive=False)` per date folder** (called once from orchestrator/Mac).
2. **Save file manifest to JSON** and reuse it everywhere (GitHub Actions matrix, training scripts, local runs).
3. **Fetch via CDN URLs only** (`https://huggingface.co/datasets/{repo}/resolve/main/{path}`) — no Authorization header, bypasses `/api/` rate limits and 429s.
4. **Project to `{prompt, response}` only at parse time**; write homogeneous parquet to `batches/mirror-merged/{date}/{slug}.parquet`.
5. **Schema normalization + strict validation** (reject non-text, log dropped rows, enforce UTF-8, length limits, and exact output path).

This removes recursive pagination, cuts HF API calls during data loading, avoids 429s, keeps schema homogeneous for training, and is fully actionable within <2h.

---

## Implementation plan (≤2h, critical path first)

| Step | Owner | Time | Concrete deliverable |
|------|-------|------|----------------------|
| 1. Manifest generator (`bin/build-manifest.py`) | backend | 20m | Produces `manifest-{date}.json` with repo, folder, sorted files[], cdn_prefix, generated_at, file_count. |
| 2. Update `bin/dataset-enrich.sh` to accept manifest | backend | 15m | Reads `MANIFEST_FILE`; streams listed files; removes recursive listing. |
| 3. CDN download helper (`lib/cdn_download.py`) | backend | 20m | `stream_cdn_file(repo, path)` with retries/backoff, no auth header, timeout=30, raises on final failure. |
| 4. Schema projection + parquet writer (`lib/project_parquet.py`) | backend | 25m | Projects only `prompt`/`response`; normalizes column names; validates UTF-8 + text length; writes exact `batches/mirror-merged/{date}/{slug}.parquet`. |
| 5. GitHub Actions matrix update (manifest artifact) | infra | 15m | Pre-step generates + uploads manifest; shard jobs download artifact; avoids per-shard listing. |
| 6. Lightning/Kaggle training loader update | backend | 15m | Uses manifest + CDN fetches; removes `load_dataset(streaming=True)` on heterogeneous repo. |
| 7. Local + one shard smoke test | qa | 20m | Validate dedup, schema, row counts, CDN behavior, no 429s; compare row counts before/after change on sample folder. |

Total: ~145m. Scope to steps 1–4 + 7 for <2h hard cutoff; Actions/training polish can follow immediately after.

---

## Code snippets (merged + hardened)

### 1. Manifest generator (`bin/build-manifest.py`)
```python
#!/usr/bin/env python3
"""
Generate a non-recursive file manifest for a date folder in axentx/surrogate-1-training-pairs.
Usage:
  HF_TOKEN=<token> python bin/build-manifest.py --repo axentx/surrogate-1-training-pairs \
    --folder batches/public-merged/2026-05-03 --out manifest-2026-05-03.json
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from huggingface_hub import HfApi, login

HF_API_RATE_LIMIT_RETRY = 360  # seconds (per pattern)
MAX_RETRIES = 5

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--folder", required=True, help="Folder path in repo (e.g. batches/public-merged/2026-05-03)")
    parser.add_argument("--out", required=True, help="Output JSON file")
    parser.add_argument("--token", default=os.getenv("HF_TOKEN"))
    args = parser.parse_args()

    if args.token:
        login(args.token)

    api = HfApi()
    folder = args.folder.rstrip("/")
    for attempt in range(MAX_RETRIES):
        try:
            entries = api.list_repo_tree(repo_id=args.repo, path=folder, recursive=False)
            break
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                print(f"Failed to list repo tree after {MAX_RETRIES} attempts: {e}", file=sys.stderr)
                sys.exit(1)
            wait = HF_API_RATE_LIMIT_RETRY if "429" in str(e) else (2 ** attempt)
            print(f"Attempt {attempt+1} failed: {e}. Retry in {wait}s", file=sys.stderr)
            time.sleep(wait)

    files = []
    for entry in entries:
        path = getattr(entry, "path", entry.get("path") if isinstance(entry, dict) else None)
        if not path:
            continue
        if path.endswith((".parquet", ".jsonl")):
            files.append(path)

    manifest = {
        "repo": args.repo,
        "folder": folder,
        "files": sorted(files),
        "cdn_prefix": f"https://huggingface.co/datasets/{args.repo}/resolve/main",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(files),
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(files)} files to {args.out}")

if __name__ == "__main__":
    main()
```

### 2. CDN download helper (`lib/cdn_download.py`)
```python
import requests
import time
from typing import BinaryIO

def stream_cdn_file(repo: str, path: str, chunk_size: int = 8192, retries: int = 3, timeout: int = 30) -> BinaryIO:
    """
    Stream file via CDN (no Authorization header).
    Returns raw file-like object. Raises on final failure.
    """
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, stream=True, timeout=timeout)
            resp.raise_for_status()
            return resp.raw
        except Exception as exc:
            if attempt == retries:
                raise RuntimeError(f"CDN fetch failed for {repo}/{path} after {retries} attempts") from exc
            wait = 2 ** attempt
            print(f"CDN fetch failed ({exc}) for {path}, retry {attempt}/{retries} in {wait}s")
            time.sleep(wait)
```

### 3. Schema projection + parquet writer (`lib/project_parquet.py`)
```python
import json
import os
import io
import pyarrow as pa
import pyarrow.parquet as pq
from typing import List, Dict
from lib.cdn_download import stream_cdn_file

COLS_PROMPT = {"prompt", "input", "text", "instruction", "question"}
COLS_RESPONSE = {"response", "output", "completion", "answer"}
MAX_TEXT_LEN = 100_000  # chars; adjust as needed

def normalize_column_name(name: str) -> str:
    return (name or "").strip().lower()

def project_and_write(
    manifest_path: str,
    out_dir: str,
    date: str,
    slug: str,
    repo: str = "axentx/surrogate-1-training-pairs",
    max_rows: int = 10_000_000
) -> Dict:
    """
    Reads manifest, projects prompt/response, writes parquet.
    Returns stats dict.
    """
    os.makedirs(out_dir, exist_ok=True)
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    rows: List[Dict[str, str]] = []
    dropped_non_text = 0
    dropped_bad_jsonl = 0
    dropped_empty = 0
    dropped_too_long = 0
    processed_files = 0

    for rel_path in manifest["

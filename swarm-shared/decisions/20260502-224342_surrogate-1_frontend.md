# surrogate-1 / frontend

## Final Implementation Plan  
**Highest-value improvement (≤2 h): Eliminate HF API 429s and make shard workers deterministic and CDN-only.**

---

### What I’m shipping (single coherent change set)
1. **Pre-flight deterministic file list** (`bin/list-public-files.py`)  
   - One-time run per training snapshot.  
   - Produces `file-list.json` (sorted, stable) containing only files to ingest.  
   - Committed/artifact-passed to all runners so every shard uses the same snapshot.

2. **CDN-only ingestion loader** (`lib/cdn_stream.py`)  
   - Reads `file-list.json`.  
   - Streams each file via `https://huggingface.co/datasets/{repo}/resolve/main/{path}`.  
   - Retries with exponential backoff; treats 404 as permanent skip.  
   - Emits raw JSONL rows; keeps normalization/dedup unchanged downstream.

3. **Shard-aware CDN worker** (`lib/cdn_worker.py`)  
   - Accepts `--file-list`, `--shard-id`, `--total-shards`.  
   - Deterministic sharding by `hash(path) % total_shards`.  
   - Optional deterministic dedup key (e.g., hash of prompt+response) to avoid duplicates across shards.  
   - Writes `shardN-<ts>.jsonl`.

4. **Updated runner entrypoint** (`bin/dataset-enrich.sh`)  
   - When `FILE_LIST` is provided, delegates to `cdn_worker`.  
   - Otherwise preserves legacy `datasets` path unchanged.  
   - All runners share the same snapshot → reproducible, no HF API calls during training ingestion.

5. **GitHub Actions integration**  
   - Add a pre-job step to generate `file-list.json` (or commit it).  
   - Pass `FILE_LIST=file-list.json` through the matrix to all shard jobs.  
   - No schema or dedup logic changes; only transport/ingestion changes.

---

### Why this is highest value
- **Eliminates HF API 429 risk** during training data ingestion.  
- **Deterministic and reproducible**: all shards see the same file snapshot.  
- **Independent shards**: no shared state beyond the file list.  
- **Minimal scope**: transport-layer only; no changes to schema, normalization, or dedup policy.  
- **Fits ≤2 h**: focused scripts and wiring.

---

### Implementation details

#### 1) Pre-flight file lister (`bin/list-public-files.py`)
```python
#!/usr/bin/env python3
"""
Generate deterministic file list for a public HF dataset repo.
Usage:
  HF_TOKEN=<token> python bin/list-public-files.py \
    --repo axentx/surrogate-1-training-pairs \
    --out file-list.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

from huggingface_hub import HfApi

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--out", default="file-list.json")
    parser.add_argument("--path", default="", help="Subfolder to list (empty = root)")
    parser.add_argument("--date-prefix", default="", help="Only include paths starting with this prefix")
    args = parser.parse_args()

    api = HfApi(token=os.environ.get("HF_TOKEN"))
    entries = api.list_repo_tree(repo=args.repo, path=args.path, recursive=False)

    files = []
    for entry in entries:
        if entry.type != "file":
            continue
        if args.date_prefix and not entry.path.startswith(args.date_prefix):
            continue
        files.append(
            {
                "path": entry.path,
                "size": getattr(entry, "size", None),
                "lfs": getattr(entry, "lfs", None),
            }
        )

    files.sort(key=lambda x: x["path"])

    payload = {
        "repo": args.repo,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "root_path": args.path,
        "date_prefix": args.date_prefix or None,
        "count": len(files),
        "files": files,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)

    print(f"Wrote {len(files)} files to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
```
Make executable:
```bash
chmod +x bin/list-public-files.py
```

---

#### 2) CDN-only stream loader (`lib/cdn_stream.py`)
```python
import json
import logging
import time
from typing import Dict, Any, Iterator

import requests

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
logger = logging.getLogger(__name__)

def iter_cdn_files(file_list_path: str, repo: str) -> Iterator[Dict[str, Any]]:
    with open(file_list_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    files = manifest.get("files", [])
    if not files:
        logger.warning("No files in manifest")
        return

    for item in files:
        path = item["path"]
        url = CDN_TEMPLATE.format(repo=repo, path=path)
        logger.info("Streaming CDN: %s", path)

        for attempt in range(3):
            try:
                with requests.get(url, stream=True, timeout=30) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines(decode_unicode=True):
                        if line is None or not line.strip():
                            continue
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            logger.warning("Invalid JSON line in %s", path)
                break
            except requests.HTTPError as e:
                if getattr(e.response, "status_code", None) == 404:
                    logger.error("CDN 404 (file missing): %s", path)
                    break
                logger.warning("Attempt %d failed for %s: %s", attempt + 1, path, e)
                time.sleep(2 ** attempt)
            except requests.RequestException as e:
                logger.warning("Attempt %d network error for %s: %s", attempt + 1, path, e)
                time.sleep(2 ** attempt)
        else:
            logger.error("Failed to fetch %s after retries", path)
```

---

#### 3) Shard-aware CDN worker (`lib/cdn_worker.py`)
```python
#!/usr/bin/env python3
import argparse
import hashlib
import json
import sys
from pathlib import Path

from lib.cdn_stream import iter_cdn_files
from lib.dedup import is_duplicate, store_hash  # existing dedup store

def deterministic_shard(key: str, total_shards: int) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % total_shards

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-list", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--total-shards", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--dedup-key-fields", default="prompt,response")
    args = parser.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    fields = [f.strip() for f in args.dedup_key_fields.split(",") if f.strip()]

    written = 0
    for row in iter_cdn_files(args.file_list, args.repo):
        # Deterministic sharding by file path so each file is processed by exactly one shard
        shard = deterministic_shard(row.get("path", ""), args.total_shards)
        if shard != args.shard_id:
            continue

        # Optional dedup across shards using content hash
        key_parts = []
        for f in fields:
            val = row
            for part in f.split("."):
                if isinstance(val, dict):
                    val = val.get(part)
                else:
                    val = None
                    break
            key_parts.append(json.dumps(val, sort_keys=True))
        dedup_key = "|".join(key_parts)

        if is_duplicate(dedup_key):
            continue
        store

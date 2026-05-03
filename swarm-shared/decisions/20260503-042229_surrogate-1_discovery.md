# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### Changes

1. Add `bin/generate_manifest.py` — single Mac-side API call that lists one date folder via `list_repo_tree(recursive=False)` and writes `manifest.json` (file paths + sizes + etags + deterministic slugs). Embed this manifest in training scripts so Lightning workers do **CDN-only** fetches with zero API calls during data load.
2. Replace `bin/dataset-enrich.sh` with `bin/dataset_enrich.py` — deterministic shard assignment by slug hash, schema projection to `{prompt, response}` only, per-file `hf_hub_download` (bypass `load_dataset` mixed-schema CastError), streaming line-by-line to bound memory, and dedup via `lib/dedup.py`.
3. Add `requirements.txt` update (`requests` for CDN bypass) and lightweight GitHub Action step to generate+upload manifest before the 16-shard matrix runs.
4. Keep filename pattern `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl` and HF commit-cap mitigation (128/hr) by deterministic shard → repo routing if needed.

Estimated time: 90 minutes (45m code, 30m test, 15m GH Action tweak).

---

### 1) Manifest generator (Mac side, run once per date folder)

`bin/generate_manifest.py`

```python
#!/usr/bin/env python3
"""
Generate manifest for a single date folder to enable CDN-only fetches.
Usage:
  HF_TOKEN=... python bin/generate_manifest.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --out manifest.json
"""
import argparse
import hashlib
import json
import os
import sys
from typing import Dict, List

from huggingface_hub import HfApi, login

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def slug_for_path(path: str) -> str:
    """Deterministic slug for dedup/sharding from repo-relative path."""
    return path.rstrip(".jsonl").rstrip(".parquet").replace("/", "_")

def build_manifest(repo: str, date: str) -> Dict:
    api = HfApi()
    folder = f"batches/public-merged/{date}"
    try:
        items = api.list_repo_tree(repo=repo, path=folder, recursive=False)
    except Exception as exc:
        print(f"Failed to list {repo}/{folder}: {exc}", file=sys.stderr)
        sys.exit(1)

    files = []
    for item in items:
        if getattr(item, "type", None) != "file":
            continue
        path = item.path
        files.append(
            {
                "path": path,
                "slug": slug_for_path(path),
                "size": getattr(item, "size", None),
                "etag": getattr(item, "etag", None),
                "cdn_url": CDN_TEMPLATE.format(repo=repo, path=path),
            }
        )

    manifest = {
        "repo": repo,
        "date": date,
        "folder": folder,
        "generated_by": "generate_manifest.py",
        "files": files,
        "total_files": len(files),
    }
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CDN manifest for a date folder.")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD folder under batches/public-merged/")
    parser.add_argument("--out", default="manifest.json")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if token:
        login(token=token, add_to_git_credential=False)

    manifest = build_manifest(args.repo, args.date)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {args.out} with {manifest['total_files']} files")

if __name__ == "__main__":
    main()
```

Make executable:

```bash
chmod +x bin/generate_manifest.py
```

---

### 2) Python worker (replaces dataset-enrich.sh)

`bin/dataset_enrich.py`

```python
#!/usr/bin/env python3
"""
Robust per-shard enrichment worker.
- Projects heterogeneous repo files to {prompt, response} only.
- Uses hf_hub_download per file to avoid mixed-schema CastError.
- Streams line-by-line to bound memory.
- Deduplicates via lib.dedup (central md5 store on HF Space).
- Outputs batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl

Usage (single shard):
  HF_TOKEN=... python bin/dataset_enrich.py \
    --shard-id 0 --shard-total 16 \
    --date 2026-05-03 \
    --manifest manifest.json \
    --out-dir batches/public-merged
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pyarrow as pa
import pyarrow.parquet as pq
from huggingface_hub import HfApi, hf_hub_download, login

# Local dedup module (shared with HF Space)
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

HF_TOKEN = os.environ.get("HF_TOKEN")
if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

login(token=HF_TOKEN, add_to_git_credential=False)
API = HfApi()

# Deterministic shard assignment by slug hash
def shard_for_slug(slug: str, shard_total: int) -> int:
    digest = hashlib.md5(slug.encode()).hexdigest()
    return int(digest, 16) % shard_total

def normalize_record(raw: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous schemas to {prompt, response}.
    Add more branches as new schemas appear.
    """
    # Common patterns seen in public datasets
    prompt = raw.get("prompt") or raw.get("instruction") or raw.get("input") or raw.get("question")
    response = raw.get("response") or raw.get("output") or raw.get("answer") or raw.get("completion")

    if not prompt or not response:
        # Fallback: try to extract from text/conversation fields
        text = raw.get("text")
        if isinstance(text, str) and "\n\n" in text:
            parts = text.split("\n\n", 1)
            prompt, response = parts[0], parts[1]
        else:
            return None

    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def stream_parquet(path: str) -> Iterable[Dict[str, Any]]:
    """Stream rows from local parquet file."""
    try:
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=1024):
            table = pa.Table.from_batches([batch])
            for col in table.column_names:
                if table.schema.field(col).type == pa.string():
                    table = table.replace_schema_metadata({})
            for row in table.to_pylist():
                yield row
    except Exception as exc:
        print(f"Failed to read parquet {path}: {exc}", file=sys.stderr)

def stream_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

def process_file(
    repo: str,
    rel_path: str,
    dedup: DedupStore,
    shard_id: int,
    shard_total: int,
) -> Iterable[Dict[str, str]]:
    """

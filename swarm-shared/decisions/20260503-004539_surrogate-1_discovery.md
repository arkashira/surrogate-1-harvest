# surrogate-1 / discovery

## Implementation Plan (≤2h)

**Highest-value change**: Add `tools/snapshot_manifest.py` that lists one date-partition via a **single** HF API call, emits `file_manifest.json` with CDN URLs, and a training script that uses **CDN-only** fetches (zero HF API calls during training). This implements the CDN bypass pattern and avoids HF rate limits while enabling reproducible training runs.

### Steps (est. 90 minutes)

1. **Create `tools/snapshot_manifest.py`** (45 min)
   - Single HF API call: `list_repo_tree(path=date_partition, recursive=True)`
   - Filter to parquet/jsonl files
   - Emit `file_manifest.json`: `{cdn_url, size, sha256?, relative_path}`
   - Include dataset repo, partition date, timestamp
   - CLI: `python snapshot_manifest.py --repo axentx/surrogate-1-training-pairs --partition 2026-05-03 --out file_manifest.json`

2. **Create `tools/train_cdn.py`** (30 min)
   - Load `file_manifest.json`
   - Use `requests.get(cdn_url, stream=True)` with retry/backoff
   - Parse parquet → project `{prompt, response}` only
   - Yield training examples (streaming, no full load)
   - Optional: integrate with PyTorch `IterableDataset`

3. **Update `requirements.txt`** (5 min)
   - Add `requests>=2.31`
   - Keep existing: `datasets`, `huggingface_hub`, `pyarrow`, `numpy`

4. **Add `tools/README.md`** (10 min)
   - Usage examples
   - Pattern: run snapshot once (after rate-limit window), embed manifest in training

---

## `tools/snapshot_manifest.py`

```python
#!/usr/bin/env python3
"""
snapshot_manifest.py
List one date-partition of a HuggingFace dataset and emit a CDN manifest.

Usage:
    python snapshot_manifest.py \
        --repo axentx/surrogate-1-training-pairs \
        --partition 2026-05-03 \
        --out file_manifest.json

Produces file_manifest.json with CDN URLs that bypass HF API rate limits.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

from huggingface_hub import HfApi, hf_hub_url

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build CDN manifest for a dataset partition")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. axentx/surrogate-1-training-pairs)")
    parser.add_argument("--partition", required=True, help="Date partition path (e.g. 2026-05-03 or batches/public-merged/2026-05-03)")
    parser.add_argument("--out", default="file_manifest.json", help="Output JSON path")
    parser.add_argument("--ext", nargs="+", default=[".parquet", ".jsonl", ".json"], help="File extensions to include")
    parser.add_argument("--token", default=os.getenv("HF_TOKEN"), help="HF token (optional for public repos)")
    return parser

def list_partition_files(repo: str, partition: str, token: str | None) -> List[Dict[str, Any]]:
    """Single HF API call to list files in a partition (non-recursive by default)."""
    api = HfApi(token=token)
    # list_repo_tree recursive=True to capture nested files in one call
    tree = api.list_repo_tree(repo=repo, path=partition, recursive=True, repo_type="dataset")
    files = [entry for entry in tree if entry.type == "file"]
    return files

def build_manifest(repo: str, partition: str, files: List[Any], exts: List[str]) -> Dict[str, Any]:
    entries = []
    for f in files:
        if not any(f.path.endswith(e) for e in exts):
            continue
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=f.path)
        entries.append({
            "relative_path": f.path,
            "cdn_url": cdn_url,
            "size": f.size,
            "lfs": getattr(f, "lfs", None) is not None,
        })

    manifest = {
        "repo": repo,
        "partition": partition,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_files": len(entries),
        "extensions": exts,
        "files": entries,
    }
    return manifest

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        files = list_partition_files(args.repo, args.partition, args.token)
    except Exception as e:
        print(f"Error listing repo tree: {e}", file=sys.stderr)
        sys.exit(1)

    manifest = build_manifest(args.repo, args.partition, files, args.ext)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote manifest with {manifest['total_files']} files -> {out_path}")

if __name__ == "__main__":
    main()
```

---

## `tools/train_cdn.py`

```python
#!/usr/bin/env python3
"""
train_cdn.py
Training data loader that uses CDN-only fetches (zero HF API calls).

Usage:
    python train_cdn.py file_manifest.json

Reads file_manifest.json and streams examples from CDN URLs.
Projects each file to {prompt, response} at parse time.
"""

import json
import sys
from pathlib import Path
from typing import Dict, Any, Iterator

import numpy as np
import pyarrow.parquet as pq
import requests
from requests.adapters import HTTPAdapter, Retry

def requests_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session

def project_to_pair(record: Dict[str, Any]) -> Dict[str, str]:
    """Project raw record to {prompt, response}. Customize per schema."""
    # Common patterns; adapt to your actual schemas
    prompt = record.get("prompt") or record.get("input") or record.get("question") or ""
    response = record.get("response") or record.get("output") or record.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

def stream_parquet_cdn(url: str, session: requests.Session, batch_size: int = 1024) -> Iterator[Dict[str, str]]:
    """Stream parquet from CDN via HTTP range requests (pyarrow supports file-like)."""
    # Simple approach: download to temp file-like object in chunks
    # For very large files, consider pyarrow.NativeFile via streaming
    resp = session.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    content = resp.content
    table = pq.read_table(pq.ParquetFile(pq.BufferReader(content)))
    df = table.to_pylist()
    for rec in df:
        yield project_to_pair(rec)

def stream_jsonl_cdn(url: str, session: requests.Session) -> Iterator[Dict[str, str]]:
    resp = session.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        rec = json.loads(line)
        yield project_to_pair(rec)

def iter_manifest_examples(manifest_path: Path, session: requests.Session) -> Iterator[Dict[str, str]]:
    manifest = json.loads(manifest_path.read_text())
    for f in manifest["files"]:
        url = f["cdn_url"]
        path = f["relative_path"]
        try:
            if path.endswith(".parquet"):
                yield from stream_parquet_cdn(url, session)
            elif path.endswith(".jsonl") or path.endswith(".json"):
                yield from stream_jsonl_cdn

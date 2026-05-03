# vanguard / discovery

## Final consolidated solution

### 1. Diagnosis (merged)
- No content-addressed manifest per date folder → training repeatedly calls `list_repo_tree`/`load_dataset` at runtime, causing HF API 429s and non-reproducible epochs.
- Data loader uses Hugging Face `datasets` API during training (streaming/list calls) instead of CDN-only fetches, violating the HF CDN bypass pattern.
- No deterministic file-list artifact to embed in training scripts → each run re-enumerates the repo and risks rate limits.
- Missing idempotent ingestion step that projects heterogeneous HF repo files to `{prompt, response}` only and writes to `batches/mirror-merged/{date}/{slug}.parquet` without extra metadata columns.
- No studio-reuse guard in orchestration → Lightning Studio recreation burns quota and risks idle-stop training loss.
- No local cache or offline-first capability for dataset iteration in constrained environments (Lightning Studio).

### 2. Proposed change (merged + corrected)
Create two new files and update training:
- `/opt/axentx/vanguard/ingest/manifest.py` — builds a content-addressed file manifest for a date folder and (optionally) produces normalized `{prompt,response}` Parquet shards.
- `/opt/axentx/vanguard/train/train.py` — accepts `--manifest` and uses CDN-only fetches (`hf_hub_download` or direct CDN URLs) with zero `datasets` API calls during training.
- Add a studio-reuse helper and a small runner script (`run_ingest.sh`) for convenience.

Paths:
- Manifest: `batches/mirror-merged/{date}/manifest-{sha256-prefix}.json` (preferred canonical location; ingest writes here).
- Optional normalized shards: `batches/mirror-merged/{date}/{slug}.parquet`.

### 3. Implementation

#### `/opt/axentx/vanguard/ingest/manifest.py`
```python
#!/usr/bin/env python3
"""
Content-addressed manifest builder + optional Parquet projection for HF date folders.

Produces:
- batches/mirror-merged/{date}/manifest-{sha256-prefix}.json
  [{repo, path, size?, etag?, url}]

Optionally (with --project):
- batches/mirror-merged/{date}/{slug}.parquet
  [{prompt, response}]
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from huggingface_hub import list_repo_tree, hf_hub_download

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pandas as pd
    _PARQUET_AVAILABLE = True
except Exception:
    _PARQUET_AVAILABLE = False


def _cdn_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def build_manifest(
    repo: str,
    date_folder: str,
    out_dir: str = "batches/mirror-merged",
    recursive: bool = True,
) -> str:
    """
    Build manifest for repo:date_folder and write to out_dir.
    Returns path to written manifest.
    """
    tree = list_repo_tree(repo=repo, path=date_folder, recursive=recursive)
    entries: List[Dict] = []
    for item in tree:
        if item.type != "file":
            continue
        # item.path is relative to repo root; ensure it's fully qualified
        path = item.path if item.path.startswith(date_folder) else f"{date_folder}/{item.path}"
        entries.append({
            "repo": repo,
            "path": path,
            "size": getattr(item, "size", None),
            "etag": getattr(item, "etag", None),
            "url": _cdn_url(repo, path),
        })

    os.makedirs(out_dir, exist_ok=True)
    date_out = Path(out_dir) / date_folder
    date_out.mkdir(parents=True, exist_ok=True)

    content = json.dumps(entries, separators=(",", ":"), sort_keys=True).encode()
    digest = hashlib.sha256(content).hexdigest()[:12]
    manifest_name = f"manifest-{digest}.json"
    out_path = date_out / manifest_name
    out_path.write_bytes(content)
    return str(out_path)


def _try_read_jsonl(path: Path) -> Optional[List[Dict]]:
    try:
        with path.open() as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception:
        return None


def _try_read_parquet(path: Path) -> Optional[List[Dict]]:
    if not _PARQUET_AVAILABLE:
        return None
    try:
        tbl = pq.read_table(path)
        return tbl.to_pylist()
    except Exception:
        return None


def _extract_prompt_response(record: Dict) -> Optional[Dict]:
    if not isinstance(record, dict):
        return None

    # Common key variants
    prompt_keys = {"prompt", "input", "question", "instruction", "user"}
    response_keys = {"response", "output", "answer", "assistant", "completion"}

    prompt = None
    for k in prompt_keys:
        v = record.get(k)
        if isinstance(v, str) and v.strip():
            prompt = v.strip()
            break
    if prompt is None:
        return None

    response = None
    for k in response_keys:
        v = record.get(k)
        if isinstance(v, str) and v.strip():
            response = v.strip()
            break
    if response is None:
        return None

    return {"prompt": prompt, "response": response}


def project_to_parquet(
    repo: str,
    manifest_path: str,
    date_folder: str,
    out_dir: str = "batches/mirror-merged",
    cache_dir: Optional[str] = None,
) -> Optional[str]:
    """
    Download each file in manifest, normalize to {prompt,response}, and write Parquet shards.
    Returns path to written Parquet or None if projection skipped/failed.
    """
    if not _PARQUET_AVAILABLE:
        print("pyarrow/pandas not available; skipping Parquet projection.", file=sys.stderr)
        return None

    with open(manifest_path) as f:
        files = json.load(f)

    rows: List[Dict] = []
    for fspec in files:
        local = hf_hub_download(
            repo_id=repo,
            filename=fspec["path"],
            repo_type="dataset",
            cache_dir=cache_dir or os.path.expanduser("~/.cache/huggingface"),
        )
        path = Path(local)

        # Try JSONL first
        records = _try_read_jsonl(path)
        if records is None:
            records = _try_read_parquet(path)
        if records is None:
            # Try line-by-line JSON fallback
            try:
                with path.open() as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rows.append(_extract_prompt_response(json.loads(line)))
                        except Exception:
                            continue
            except Exception:
                continue
        else:
            for rec in records:
                pr = _extract_prompt_response(rec)
                if pr:
                    rows.append(pr)

    if not rows:
        print("No prompt/response rows extracted; skipping Parquet write.", file=sys.stderr)
        return None

    df = pd.DataFrame(rows)
    slug = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:12]
    date_out = Path(out_dir) / date_folder
    date_out.mkdir(parents=True, exist_ok=True)
    parquet_path = date_out / f"{slug}.parquet"
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), parquet_path)
    return str(parquet_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build manifest (and optional Parquet) for HF date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo id (e.g. datasets/axentx/vanguard-mirror)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-05-03)")
    parser.add_argument("--out", default="batches/mirror-merged", help="Base output directory")
    parser.add_argument("--project",

# vanguard / discovery

## 1. Diagnosis
- No content-addressed manifest exists → training scripts call HF API (`list_repo_tree`/`load_dataset`) at runtime, triggering 429s and non-reproducible epochs.
- `enriched/` contains mixed-schema parquet files (extra `source`, `ts` columns) → `pyarrow.CastError` in surrogate-1 training.
- No local file list for a date folder → each run re-enumerates repos and risks rate limits instead of using CDN-only fetches.
- No deterministic repo-to-sibling mapping → ingestion hits HF commit cap (128/hr/repo) instead of spreading across siblings.
- No guard to reuse running Lightning Studio → wastes quota on repeated studio creation instead of reusing running instances.

## 2. Proposed change
Create `/opt/axentx/vanguard/discovery/manifest.py` (single file, ~120 lines) that:
- Exposes `build_manifest(repo, date_folder, out_path)` to list files via HF API **once** (Mac-side), save `{repo}/{date_folder}/{sha256_slug}.json` with `{file, cdn_url, rows, sha256}` entries.
- Exposes `sibling_for(slug, n=5)` to deterministically pick sibling repo by hash.
- Exposes `project_and_write_parquet(in_paths, out_path)` to read mixed-schema files and project to `{prompt, response}` only.
- Exposes `reuse_or_create_studio(name, machine)` to reuse a running studio or start one.

Update `/opt/axentx/vanguard/discovery/train.py` to:
- Accept `--manifest` path and perform **CDN-only** data loading (no HF API calls during training).
- Use `manifest.sibling_for` when pushing artifacts.
- Use `manifest.reuse_or_create_studio` before training.

## 3. Implementation

```bash
# /opt/axentx/vanguard/discovery/manifest.py
#!/usr/bin/env python3
"""
Content-addressed manifest + helpers to avoid HF API rate limits
and enforce schema projection before training.
"""
from __future__ import annotations
import json, hashlib, os, sys
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
import pyarrow.parquet as pq
import pyarrow.compute as pc
import pyarrow as pa

HF_CDN = "https://huggingface.co/datasets"

def _sha256_slug(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]

def sibling_for(slug: str, n: int = 5) -> str:
    """Deterministic sibling repo index 0..n-1."""
    idx = int(_sha256_slug(slug), 16) % n
    return f"{slug}-sibling-{idx}" if idx > 0 else slug

def build_manifest(repo: str, date_folder: str, out_path: Path) -> List[Dict[str, Any]]:
    """
    One-time HF API call to list files in repo/date_folder.
    Writes manifest JSON and returns entries.
    Each entry: {file, cdn_url, rows, sha256}
    """
    from huggingface_hub import HfApi  # lazy import; only used on Mac orchestration
    api = HfApi()
    # non-recursive list for the date folder
    files = api.list_repo_tree(repo=repo, path=date_folder, repo_type="dataset", recursive=False)
    entries = []
    for f in files:
        if not f.path.endswith(".parquet"):
            continue
        cdn = f"{HF_CDN}/{repo}/resolve/main/{f.path}"
        # lightweight row count via pq metadata (avoids full download)
        try:
            md = pq.read_metadata(cdn)
            rows = md.num_rows
        except Exception:
            rows = None
        entry = {
            "file": f.path,
            "cdn_url": cdn,
            "rows": rows,
            "sha256": _sha256_slug(f.path),
        }
        entries.append(entry)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(entries, indent=2))
    return entries

def project_and_write_parquet(in_paths: List[str], out_path: Path) -> Path:
    """
    Read mixed-schema parquet files and project to {prompt, response}.
    Accepts local paths or CDN URLs.
    """
    batches = []
    for p in in_paths:
        try:
            tbl = pq.read_table(p, columns=["prompt", "response"])
        except (pa.ArrowInvalid, KeyError, OSError):
            # fallback: read all and select only prompt/response if present
            tbl = pq.read_table(p)
            cols = [c for c in tbl.column_names if c in ("prompt", "response")]
            if len(cols) != 2:
                raise ValueError(f"Missing prompt/response in {p}")
            tbl = tbl.select(cols)
        # rename if necessary
        if "prompt" not in tbl.column_names:
            # try case-insensitive match
            for c in tbl.column_names:
                if c.lower() == "prompt":
                    tbl = tbl.rename_columns([c if c != "prompt" else "prompt" for c in tbl.column_names])
                    break
        if "response" not in tbl.column_names:
            for c in tbl.column_names:
                if c.lower() == "response":
                    tbl = tbl.rename_columns([c if c != "response" else "response" for c in tbl.column_names])
                    break
        batches.append(tbl)
    if not batches:
        raise ValueError("No valid inputs to project")
    combined = pa.concat_tables(batches).unify_dictionaries()
    pq.write_table(combined, out_path)
    return out_path

def reuse_or_create_studio(name: str, machine: str, lightning):
    """
    Reuse running studio or create new one.
    lightning: lightning.ai SDK module (passed in to avoid hard dependency).
    Returns Studio instance.
    """
    from lightning.app import Teamspace
    teamspace = Teamspace()
    for s in teamspace.studios:
        if s.name == name and s.status == "running":
            return s
    # create new
    from lightning.app import LightningRuntime, Studio
    return Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )

def load_manifest(manifest_path: Path) -> List[Dict[str, Any]]:
    return json.loads(manifest_path.read_text())
```

```python
# /opt/axentx/vanguard/discovery/train.py  (partial diff)
# Add near top:
import argparse, json
from pathlib import Path
from manifest import load_manifest, sibling_for, reuse_or_create_studio

def cdn_only_dataset(entries, columns=("prompt", "response")):
    """Yield rows from CDN URLs without HF API calls."""
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    for e in entries:
        tbl = pq.read_table(e["cdn_url"], columns=columns)
        for batch in tbl.to_batches(max_chunksize=1024):
            for row in zip(*[batch.column(c).to_pylist() for c in columns]):
                yield dict(zip(columns, row))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--machine", default="lightning-lambda-prod/L40S")
    parser.add_argument("--sibling-n", type=int, default=5)
    args = parser.parse_args()

    entries = load_manifest(args.manifest)
    # deterministic sibling for artifact push
    repo_slug = entries[0]["file"].split("/")[1] if entries else "vanguard"
    target_repo = sibling_for(repo_slug, n=args.sibling_n)

    # reuse studio
    import lightning as L
    studio = reuse_or_create_studio("surrogate-1-train", args.machine, L)

    # training loop uses cdn_only_dataset (zero HF API calls)
    # ... your training code here ...
    # Example:
    # for item in cdn_only_dataset(entries):
    #     train_step(item)

    print(f"Using repo={target_repo}, manifest={args.manifest}, entries={len(entries)}")

if __name__ == "__main__":
    main()
```

## 4. Verification
1. On Mac (or orchestration host), run once to

# airship / discovery

Below is the **single, consolidated implementation plan** that merges the strongest, most actionable parts of both proposals, removes contradictions, and enforces deterministic, CDN-only, zero-HF-API-runtime behavior with concrete validation and Lightning Studio reuse.

---

## Final Goal (non-negotiable)
Make `airship discover` a **deterministic, CDN-only orchestrator** that:
- Eliminates HF API 429s at runtime.
- Avoids PyArrow schema errors from heterogeneous repos.
- Produces reproducible, versioned file manifests for training.
- Enables zero-HF-API training in Lightning Studio with studio reuse.

---


## Concrete Implementation Plan (≤2h)

### 1) CLI: `airship discover` (single entrypoint)
```
airship discover --repo <repo> --date <YYYY-MM-DD> --out <manifest.json> [--validate]
```
- Deterministic output for the same repo+date.
- Optional `--validate` to fail-fast on CDN reachability and manifest schema.

### 2) CDN-only file enumeration (no runtime API)
- **One-time, out-of-band** (dev machine, after rate-limit window):
  - Use `list_repo_tree(repo, path=date_folder, recursive=False)` **once** to produce folder listing.
  - Persist to `manifest.json` with:
    - `cdn_url`
    - `size`
    - `etag` or `sha256` when available
    - strict schema version
- **Training runtime uses only CDN URLs** (no Authorization header, no `/api/` calls).

### 3) Schema-safe ingestion (strict projection)
- **Never** use `load_dataset(streaming=True)` on heterogeneous repos.
- Download individual files via CDN (`hf_hub_download` or raw CDN URLs).
- Project to strict `{prompt, response}` at parse time:
  - Drop extra fields.
  - Normalize types to `str`.
  - Move attribution to filename patterns (`batches/mirror-merged/{date}/{slug}.parquet`).
- Parquet handling:
  - Read with `pyarrow` (not `datasets`).
  - Best-effort column mapping; missing columns become empty strings.
  - Avoid mixed-schema `CastError` by explicit column selection and casting.

### 4) Lightning Studio reuse (zero waste)
- Before training, list `Teamspace.studios` and **reuse** a running studio instead of creating new ones.
- Embed manifest in `train.py`; workers fetch files via CDN only (zero HF API calls during training).
- Skip already-downloaded files by size/etag check for idempotent retries.

### 5) Determinism + validation
- Manifest includes:
  - repo
  - date folder
  - file list with sizes and checksums (when available)
  - schema_version
- Fail-fast checks:
  - Validate manifest JSON schema.
  - Verify CDN reachability for a sample file before full download.

---

## Final Code Snippets

### 1) CLI entrypoint (`airship/discover/__main__.py`)
```python
#!/usr/bin/env python3
"""
airship discover --repo <repo> --date <YYYY-MM-DD> --out <manifest.json>
Produces CDN-only file manifest for deterministic ingestion.
"""

import argparse
import json
import sys
from pathlib import Path

from huggingface_hub import list_repo_tree

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def build_manifest(repo: str, date_folder: str, out_path: Path):
    """
    Single API call to list top-level folder (non-recursive), then build CDN manifest.
    """
    tree = list_repo_tree(repo=repo, path=date_folder, recursive=False)

    files = []
    total_size = 0
    for item in tree:
        if item.type != "file":
            continue
        path = item.path
        size = getattr(item, "size", None)
        etag = getattr(item, "etag", None)
        oid = getattr(item, "oid", None)

        cdn_url = CDN_TEMPLATE.format(repo=repo, path=path)
        files.append({
            "path": path,
            "size": size,
            "etag": etag,
            "oid": oid,
            "cdn_url": cdn_url,
        })
        if size:
            total_size += size

    manifest = {
        "repo": repo,
        "folder": date_folder,
        "generated_by": "airship-discover",
        "schema_version": "1.0",
        "total_files": len(files),
        "total_size": total_size,
        "files": files,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Airship CDN-only discovery")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g., 'org/repo')")
    parser.add_argument("--date", required=True, help="Date folder (e.g., '2026-04-29')")
    parser.add_argument("--out", default="manifest.json", help="Output manifest path")
    args = parser.parse_args()

    try:
        manifest = build_manifest(args.repo, args.date, Path(args.out))
        print(f"Manifest written to {args.out} ({manifest['total_files']} files, {manifest['total_size']} bytes)")
    except Exception as exc:
        print(f"Discovery failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
```

---

### 2) Schema-safe loader (`airship/discover/ingest.py`)
```python
import json
import pyarrow.parquet as pq
import pyarrow as pa
from pathlib import Path
from typing import Iterator, Dict, Any
from huggingface_hub import hf_hub_download


def iter_cdn_files(manifest_path: Path) -> Iterator[Path]:
    manifest = json.loads(manifest_path.read_text())
    for f in manifest["files"]:
        local_path = hf_hub_download(
            repo_id=manifest["repo"],
            filename=f["path"],
            repo_type="dataset",
        )
        yield Path(local_path)


def project_to_pair(file_path: Path) -> Iterator[Dict[str, str]]:
    suffix = file_path.suffix.lower()

    if suffix == ".jsonl":
        for line in file_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            prompt = doc.get("prompt") or doc.get("input") or doc.get("question") or ""
            response = doc.get("response") or doc.get("output") or doc.get("answer") or doc.get("completion") or ""
            if prompt or response:
                yield {"prompt": str(prompt), "response": str(response)}

    elif suffix == ".json":
        raw = json.loads(file_path.read_text())
        docs = raw if isinstance(raw, list) else [raw]
        for doc in docs:
            prompt = doc.get("prompt") or doc.get("input") or ""
            response = doc.get("response") or doc.get("output") or ""
            if prompt or response:
                yield {"prompt": str(prompt), "response": str(response)}

    elif suffix == ".parquet":
        tbl = pq.read_table(file_path)

        # Best-effort column mapping; avoid mixed-schema CastError
        prompt_col = (
            "prompt" if "prompt" in tbl.column_names
            else "input" if "input" in tbl.column_names
            else None
        )
        response_col = (
            "response" if "response" in tbl.column_names
            else "output" if "output" in tbl.column_names
            else "answer" if "answer" in tbl.column_names
            else "completion" if "completion" in tbl.column_names
            else None
        )

        if prompt_col and prompt_col in tbl.column_names:
            prompts = tbl.column(prompt_col).to_pylist()
        else:
            prompts = [""] * len(tbl)

        if response_col and response_col in tbl.column_names:
            responses = tbl.column(response_col).to_pylist()
        else:
            responses = [""] * len(tbl)

        for p, r in zip(prompts, responses):
            if p or r:
                yield {"prompt":

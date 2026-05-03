# vanguard / discovery

## 1. Diagnosis
- No content-addressed manifest per date folder → training performs runtime `list_repo_tree`/`load_dataset` calls that trigger HF API 429s and non-reproducible epochs.
- Data loader relies on Hugging Face `datasets` API during training instead of CDN-only fetches, wasting rate-limit quota and creating flaky epochs.
- Missing deterministic `{path, sha256}` snapshot per date folder → training runs cannot be reproduced or resumed reliably.
- No local file-list cache for Lightning training jobs → each epoch re-enumerates remote repo instead of using embedded CDN URLs.
- Surrogate-1 ingestion likely writes mixed-schema files to `enriched/` (with extra metadata columns) instead of projecting to `{prompt, response}` only, risking downstream `pyarrow.CastError`.

## 2. Proposed change
Create `/opt/axentx/vanguard/discovery/make_manifest.py` (single script) that:
- Accepts a HuggingFace dataset repo + date folder (e.g. `datasets/username/repo`, `2026-05-03`)
- Uses one HF API call (`list_repo_tree`) to list parquet files in that folder
- Produces `/opt/axentx/vanguard/manifests/{date}/manifest.json` with `{path, sha256, cdn_url}` entries
- Embeds this manifest into training workspace so Lightning jobs fetch via CDN only (zero API calls during training)

Also add `/opt/axentx/vanguard/discovery/project_to_schema.py` to enforce `{prompt, response}` projection before any parquet upload (prevents mixed-schema CastError).

## 3. Implementation

```bash
# /opt/axentx/vanguard/discovery/make_manifest.py
#!/usr/bin/env python3
"""
Generate content-addressed manifest for a date folder in an HF dataset repo.
Usage:
  python make_manifest.py --repo datasets/username/repo --date 2026-05-03 --out ./manifests
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("Install: pip install huggingface_hub")
    sys.exit(1)

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date_folder: str, out_dir: Path):
    # Single API call: non-recursive listing for the date folder
    items = list_repo_tree(repo=repo, path=date_folder, recursive=False)

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "files": []
    }

    for item in items:
        if not item.path.lower().endswith(".parquet"):
            continue
        # item.path is relative to repo root
        cdn_url = CDN_TEMPLATE.format(repo=repo, path=item.path)
        # item.lfs is present for LFS objects; use oid as sha256 when available
        sha256 = getattr(item, "lfs", {}).get("oid", None)
        manifest["files"].append({
            "path": item.path,
            "sha256": sha256,
            "cdn_url": cdn_url,
            "size": getattr(item, "size", None)
        })

    if not manifest["files"]:
        print(f"No parquet files found in {repo}/{date_folder}")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"manifest-{date_folder.replace('/', '_')}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Manifest written: {out_path}")
    print(f"Files: {len(manifest['files'])}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate HF CDN manifest for a date folder.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (e.g. datasets/username/repo)")
    parser.add_argument("--date", required=True, help="Date folder (e.g. 2026-05-03)")
    parser.add_argument("--out", default="./manifests", help="Output directory for manifests")
    args = parser.parse_args()

    build_manifest(args.repo, args.date, Path(args.out))
```

```bash
# /opt/axentx/vanguard/discovery/project_to_schema.py
#!/usr/bin/env python3
"""
Project raw parquet rows to {prompt, response} only to avoid mixed-schema CastError.
Usage:
  python project_to_schema.py input.parquet output.parquet
"""
import pyarrow as pa
import pyarrow.parquet as pq
import sys
from pathlib import Path

REQUIRED_COLS = {"prompt", "response"}

def project_to_schema(in_path: Path, out_path: Path):
    table = pq.read_table(in_path)
    missing = REQUIRED_COLS - set(table.column_names)
    if missing:
        raise ValueError(f"Missing required columns {missing} in {in_path}")

    # Keep only prompt/response; drop extra metadata to avoid schema drift
    projected = table.select(list(REQUIRED_COLS))
    pq.write_table(projected, out_path)
    print(f"Projected {in_path} -> {out_path} ({projected.num_rows} rows)")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: project_to_schema.py input.parquet output.parquet")
        sys.exit(1)
    project_to_schema(Path(sys.argv[1]), Path(sys.argv[2]))
```

```bash
# /opt/axentx/vanguard/discovery/load_cdn_only.py
"""
Example data loader for Lightning training: uses manifest and CDN-only fetches.
Embed manifest JSON in training workspace and use this loader to avoid HF API during epochs.
"""
import json
import pyarrow.parquet as pq
import requests
import io
from typing import List, Dict

def load_from_manifest(manifest_path: str) -> List[Dict]:
    manifest = json.loads(open(manifest_path).read())
    rows = []
    for f in manifest["files"]:
        resp = requests.get(f["cdn_url"], timeout=30)
        resp.raise_for_status()
        table = pq.read_table(io.BytesIO(resp.content))
        # Convert to list of dicts (or yield for streaming)
        rows.extend(table.select(["prompt", "response"]).to_pylist())
    return rows
```

Make scripts executable:
```bash
chmod +x /opt/axentx/vanguard/discovery/make_manifest.py
chmod +x /opt/axentx/vanguard/discovery/project_to_schema.py
```

## 4. Verification
1. Generate manifest (single API call; verify no 429):
   ```bash
   cd /opt/axentx/vanguard/discovery
   python make_manifest.py --repo datasets/username/repo --date 2026-05-03 --out ./manifests
   ```
   Confirm `manifests/manifest-2026-05-03.json` exists and lists parquet files with `cdn_url`.

2. Project schema (prevents CastError):
   ```bash
   python project_to_schema.py raw/enriched/sample.parquet projected/sample.parquet
   ```
   Confirm output contains only `prompt` and `response`.

3. CDN-only load test (zero HF API during data fetch):
   ```python
   from load_cdn_only import load_from_manifest
   rows = load_from_manifest("./manifests/manifest-2026-05-03.json")
   assert len(rows) > 0 and all("prompt" in r and "response" in r for r in rows)
   ```
   Monitor HF API usage (or logs) — no `list_repo_tree`/`load_dataset` calls during row fetch.

4. Lightning integration smoke test:
   - Place manifest in training workspace.
   - Run a short Lightning job that imports `load_cdn_only` and iterates rows.
   - Confirm job completes without HF API rate-limit errors and epochs are reproducible (same manifest → same rows).

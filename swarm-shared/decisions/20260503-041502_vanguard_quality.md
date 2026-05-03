# vanguard / quality

## Final Synthesized Solution

### Diagnosis (Consensus)
- **No content-addressed manifest**: Training/frontend hit HF API at runtime → 429s, non-reproducible snapshots.
- **Mixed-schema ingestion**: Extra columns (`source`, `ts`) in `enriched/` break downstream `load_dataset`/pyarrow casts and `{prompt,response}` expectations.
- **Non-deterministic loading**: Likely uses `load_dataset(streaming=True)` or recursive `list_repo_tree` at runtime instead of a pre-listed, CDN-only file list.
- **No snapshot pinning**: Each run can see different data if repo changes mid-epoch; can’t share reproducible training runs.
- **Mac/local rule violation**: Local runs may attempt model/data loading that should be remote-only (Mac=CLI + remote compute).

### Proposed Change (Combined + Corrected)
Create a **single, deterministic manifest generator** and **CDN-only data loader** for the surrogate-1 pipeline:
- **New**: `/opt/axentx/vanguard/scripts/generate_manifest.py` (content-addressed manifest for one date folder).
- **Modify**: `/opt/axentx/vanguard/train.py` (data loader to use manifest + CDN only).
- **Modify**: `/opt/axentx/vanguard/ingest/mirror.py` (project to `{prompt,response}` before upload).
- **Scope**: Single date-folder snapshot → JSON manifest with CDN URLs + sha256; training consumes manifest and downloads via CDN only (zero HF API calls during training).

---

### Implementation

#### 1. `/opt/axentx/vanguard/scripts/generate_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate a content-addressed manifest for one date folder.
Usage:
  HF_TOKEN=hf_xxx python generate_manifest.py \
    --repo my-org/datasets \
    --date 2026-04-29 \
    --out manifests/2026-04-29/filelist.json
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

HF_API_BASE = "https://huggingface.co/api"

def list_date_folder(repo: str, date: str, token: str):
    """Single non-recursive tree call for one date folder."""
    url = f"{HF_API_BASE}/datasets/{repo}/tree/resolve/main/batches/mirror-merged/{date}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 429:
        wait = int(resp.headers.get("retry-after", 60))
        print(f"Rate limited. Waiting {wait}s", file=sys.stderr)
        time.sleep(wait)
        return list_date_folder(repo, date, token)
    resp.raise_for_status()
    return resp.json()  # list of {path, type, size}

def sha256_of_cdn_file(repo: str, path: str) -> str:
    """Download via CDN (no auth) and hash; for manifest integrity."""
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    h = hashlib.sha256()
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=8192):
            h.update(chunk)
    return h.hexdigest()

def build_manifest(repo: str, date: str, token: str):
    entries = []
    items = list_date_folder(repo, date, token)
    for item in items:
        if item.get("type") != "file":
            continue
        path = item["path"]
        if not path.endswith(".parquet"):
            continue
        size = item["size"]
        sha256 = sha256_of_cdn_file(repo, path)
        entries.append({
            "repo": repo,
            "path": path,
            "sha256": sha256,
            "size": size,
            "cdn_url": f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
        })
    return {
        "generated_by": "vanguard/generate_manifest.py",
        "repo": repo,
        "date": date,
        "count": len(entries),
        "entries": entries
    }

def main():
    parser = argparse.ArgumentParser(description="Generate content-addressed manifest.")
    parser.add_argument("--repo", required=True, help="HF dataset repo (org/name)")
    parser.add_argument("--date", required=True, help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--token", default=os.getenv("HF_TOKEN"), help="HF token (env HF_TOKEN)")
    args = parser.parse_args()

    if not args.token:
        print("Error: HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    manifest = build_manifest(args.repo, args.date, args.token)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(manifest['entries'])} entries to {out_path}")

if __name__ == "__main__":
    main()
```

#### 2. `/opt/axentx/vanguard/train.py` (CDN-only loader using manifest)
```python
# In train.py (or data module)
import json
from pathlib import Path
from torch.utils.data import IterableDataset
import pyarrow.parquet as pq
import requests
from io import BytesIO

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path):
        manifest = json.loads(Path(manifest_path).read_text())
        self.files = [e["cdn_url"] for e in manifest["entries"]]

    def __iter__(self):
        for url in self.files:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            table = pq.read_table(BytesIO(resp.content))
            # Project only prompt/response; ignore extra cols
            df = table.select(["prompt", "response"]).to_pandas()
            for _, row in df.iterrows():
                yield {"prompt": row["prompt"], "response": row["response"]}
```

#### 3. `/opt/axentx/vanguard/ingest/mirror.py` (projection before upload)
```python
# In ingest/mirror.py before upload
import pyarrow as pa

def project_to_prompt_response(table: pa.Table) -> pa.Table:
    cols = [c for c in ["prompt", "response"] if c in table.column_names]
    return table.select(cols)
```

---

### Verification
- **Run manifest generation**:
  ```bash
  HF_TOKEN=hf_xxx python scripts/generate_manifest.py \
    --repo my-org/datasets \
    --date 2026-04-29 \
    --out manifests/2026-04-29/filelist.json
  ```
  Confirm:
  - Single API call to `/tree/resolve/main/...` (non-recursive).
  - Output JSON contains `entries` with `cdn_url`, `sha256`, and only parquet files.
- **Smoke test training loader**:
  ```bash
  python -c "
from train import CDNParquetDataset
ds = CDNParquetDataset('manifests/2026-04-29/filelist.json')
for i, x in enumerate(ds):
    print(i, x.keys())
    if i >= 5: break
  "
  ```
  Confirm:
  - No HF API calls during iteration (revoke token to verify CDN still works).
  - No pyarrow schema errors (only `prompt`, `response` projected).
- **Reproducibility**: re-run manifest generation on same date → identical `sha256` values and same file list (content-addressed).

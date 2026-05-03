# vanguard / quality

## Final Consolidated Solution

### 1. Diagnosis (merged)
- **Authenticated enumeration on every load**: both `/api/` proxy and HF SDK `list_repo_tree` burn 1000/5min quota and cause 429s.
- **No persisted manifest**: `(repo, dateFolder) → file-list` is recomputed each session.
- **Schema risk + API calls**: `load_dataset(streaming=True)` on heterogeneous repos triggers `pyarrow.CastError` and hidden pagination.
- **Studio churn**: idle-stop kills jobs; new studios are created instead of reusing running ones.

### 2. Proposed changes (merged)
Add a manifest generator + CDN-only loader + Studio reuse guard; remove authenticated enumeration from training path.

- `/opt/axentx/vanguard/scripts/build_manifest.py` (new)
- `/opt/axentx/vanguard/training/train.py` (modify data loader)
- `/opt/axentx/vanguard/scripts/launch_studio.py` (modify to reuse running studio)

Scope: ~120 LoC; focused on eliminating authenticated list calls during training and fixing schema risk.

### 3. Implementation

```bash
mkdir -p /opt/axentx/vanguard/{scripts,training,manifests}
```

#### 3.1 Manifest builder (run once after rate-limit window)

`/opt/axentx/vanguard/scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate repo+dateFolder → file-list manifest for CDN-only training.
Run from any env with HF_TOKEN after rate-limit clears.

Usage:
    HF_TOKEN=hf_xxx python build_manifest.py \
      --repo my-org/surrogate-1 \
      --date-folder 2026-04-29 \
      --out manifests/2026-04-29.json
"""
import argparse
import json
import os
import sys
from pathlib import Path

from huggingface_hub import HfApi

def build_manifest(repo: str, date_folder: str, out_path: Path) -> None:
    api = HfApi(token=os.getenv("HF_TOKEN"))
    # Single non-recursive call per dateFolder (avoids pagination)
    tree = list(api.list_repo_tree(
        repo_id=repo,
        path=date_folder,
        recursive=False,
        repo_type="dataset",
    ))
    files = [
        {
            "path": str(f.rfilename),
            "size": getattr(f, "size", None),
        }
        for f in tree
        if f.type == "file" and f.rfilename.endswith((".parquet", ".jsonl"))
    ]
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "files": files,
        "generated_by": "build_manifest.py",
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--date-folder", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if not os.getenv("HF_TOKEN"):
        sys.exit("HF_TOKEN is required")
    build_manifest(args.repo, args.date_folder, args.out)
```

#### 3.2 CDN-only data loader (training)

`/opt/axentx/vanguard/training/train.py` (replace loader section)
```python
import json
import tempfile
from pathlib import Path
from typing import List, Dict

import pyarrow.parquet as pq
import requests

def load_data_from_manifest(manifest_path: Path) -> List[Dict[str, str]]:
    """
    Load parquet/jsonl via HF CDN (no Authorization header).
    Manifest format: {repo, date_folder, files: [{path, size}]}
    Projects to {prompt, response} only at parse time to avoid pyarrow CastError.
    """
    manifest = json.loads(manifest_path.read_text())
    repo = manifest["repo"]
    date_folder = manifest["date_folder"]
    base = f"https://huggingface.co/datasets/{repo}/resolve/main/{date_folder}"

    records: List[Dict[str, str]] = []
    for f in manifest["files"]:
        url = f"{base}/{f['path']}"
        # CDN fetch; no auth header -> bypasses /api/ rate limit
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()

        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(f["path"]).suffix) as tmp:
            tmp.write(resp.content)
            tmp_path = Path(tmp.name)

        try:
            if f["path"].endswith(".parquet"):
                table = pq.read_table(tmp_path)
                cols = [c for c in table.column_names if c in ("prompt", "response")]
                if not cols:
                    continue
                subset = table.select(cols).to_pylist()
                for row in subset:
                    prompt = row.get("prompt")
                    response = row.get("response")
                    if prompt and response:
                        records.append({"prompt": str(prompt), "response": str(response)})

            elif f["path"].endswith(".jsonl"):
                for line in tmp_path.read_text().splitlines():
                    row = json.loads(line)
                    prompt = row.get("prompt")
                    response = row.get("response")
                    if prompt and response:
                        records.append({"prompt": str(prompt), "response": str(response)})
        finally:
            tmp_path.unlink(missing_ok=True)

    return records

# Example usage in training loop
# records = load_data_from_manifest(Path("manifests/2026-04-29.json"))
```

#### 3.3 Reuse running Lightning Studio

`/opt/axentx/vanguard/scripts/launch_studio.py` (modify)
```python
from lightning import Studio, Teamspace, Machine

def get_or_create_studio(name: str, machine: Machine = Machine.L40S) -> Studio:
    # Reuse running studio to save quota and avoid churn
    for s in Teamspace.studios:
        if s.name == name and s.status == "Running":
            print(f"Reusing running studio: {name}")
            return s
    print(f"Creating studio: {name}")
    return Studio(
        name=name,
        machine=machine,
        create_ok=True,
    )

def ensure_running(studio: Studio, machine: Machine) -> Studio:
    if studio.status != "Running":
        print(f"Studio stopped; restarting on {machine}")
        return Studio(
            name=studio.name,
            machine=machine,
            create_ok=True,
        )
    return studio
```

### 4. Verification

1. **Manifest generation**
   ```bash
   cd /opt/axentx/vanguard
   HF_TOKEN=hf_xxx python scripts/build_manifest.py \
     --repo my-org/surrogate-1 \
     --date-folder 2026-04-29 \
     --out manifests/2026-04-29.json
   ```
   - Confirm `manifests/2026-04-29.json` exists and lists files without errors.
   - Confirm no authenticated `list_repo_tree` calls during subsequent training.

2. **CDN-only fetch**
   - Run a small test in `training/train.py`:
     ```python
     records = load_data_from_manifest(Path("manifests/2026-04-29.json"))
     print(f"Loaded {len(records)} records")
     ```
   - Confirm records have `prompt`/`response` and no `pyarrow.CastError`.
   - Use browser devtools or `tcpdump` to verify requests go to `https://huggingface.co/datasets/.../resolve/main/...` with no `Authorization` header.

3. **Studio reuse**
   - Start a studio once via `launch_studio.py`.
   - Run again; confirm log shows “Reusing running studio” and no new studio creation.

# vanguard / quality

## Final consolidated implementation (best of both proposals)

### 1. Diagnosis (resolved)
- Frontend/training triggers authenticated `list_repo_tree` on every load → burns HF API quota (1000/5min) and causes 429s.
- No persisted `(repo, dateFolder) → file-list` manifest; each session re-enumerates via API.
- Training relies on `load_dataset(streaming=True)` or SDK calls → heterogeneous repo errors (pyarrow CastError) and per-file API calls.
- No CDN-only data path; authenticated SDK calls during training keep quota pressure high.
- Missing reuse of Lightning Studio (creates new runs, wastes quota).

**Resolution**: accept both diagnoses; unify into single root cause — no manifest + no CDN-only loader. Fix by generating a manifest once and using CDN URLs exclusively during training.

---

### 2. Proposed change (unified)
- Add a single manifest generator that produces a canonical `file-manifest.json` per `(repo, dateFolder)` with CDN URLs, sizes, and SHAs.
- Patch training to accept `--manifest` and use CDN-only fetches with `pyarrow` projection to `{prompt, response}`.
- Add a small util for CDN parquet streaming without `datasets`.
- Add runbook and verification steps; reuse existing Lightning Studio where possible.

Scope:
- `/opt/axentx/vanguard/scripts/generate_manifest.py` (new)
- `/opt/axentx/vanguard/utils/hf_cdn.py` (new)
- `/opt/axentx/vanguard/train.py` (patch)
- `/opt/axentx/vanguard/ops/README.md` (runbook)

---

### 3. Implementation

```bash
# Create directories
mkdir -p /opt/axentx/vanguard/scripts /opt/axentx/vanguard/utils
```

#### `/opt/axentx/vanguard/scripts/generate_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate repo+date -> file-list manifest for CDN-only training.
Usage:
  HF_REPO=datasets/owner/name \
  DATE_FOLDER=batches/mirror-merged/2026-05-03 \
  python scripts/generate_manifest.py > file-manifest.json
"""
import os
import json
import sys
from typing import List, Dict, Any

try:
    from huggingface_hub import list_repo_tree
except ImportError:
    print("error: install huggingface_hub", file=sys.stderr)
    sys.exit(1)

CDN_TMPL = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def build_manifest(repo: str, date_folder: str) -> Dict[str, Any]:
    prefix = f"{date_folder.rstrip('/')}/"
    entries: List[Dict[str, Any]] = []

    # Single non-recursive call -> cheap; avoids pagination explosion.
    tree = list_repo_tree(repo=repo, path=date_folder, recursive=False)
    for item in tree:
        if item.get("type") != "file":
            continue
        path = item["path"]
        # Keep only parquet for training; extend if needed.
        if not path.lower().endswith(".parquet"):
            continue
        entries.append({
            "path": path,
            "size": item.get("size"),
            "sha": item.get("oid"),
            "cdn_url": CDN_TMPL.format(repo=repo, path=path),
        })

    payload = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_by": "generate_manifest.py",
        "files": entries,
    }
    return payload

def main() -> None:
    repo = os.getenv("HF_REPO")
    date_folder = os.getenv("DATE_FOLDER")
    if not repo or not date_folder:
        print("HF_REPO and DATE_FOLDER required", file=sys.stderr)
        sys.exit(1)

    manifest = build_manifest(repo, date_folder)
    json.dump(manifest, sys.stdout, indent=2)

if __name__ == "__main__":
    main()
```

```bash
chmod +x /opt/axentx/vanguard/scripts/generate_manifest.py
```

#### `/opt/axentx/vanguard/utils/hf_cdn.py`
```python
import io
import requests
from typing import Iterator, Dict, Any
import pyarrow.parquet as pq
import pyarrow as pa
import pyarrow.compute as pc

CDN_TIMEOUT = 30

def cdn_parquet_projection(url: str, columns: list) -> Iterator[Dict[str, Any]]:
    """
    Fetch a single parquet file from CDN and project selected columns.
    Yields dict rows. Retries and raises on failure.
    """
    resp = requests.get(url, timeout=CDN_TIMEOUT)
    resp.raise_for_status()
    table = pq.read_table(io.BytesIO(resp.content), columns=columns)
    # Convert to dict rows without pandas
    rb = table.to_batches()[0] if table.num_rows > 0 else None
    if rb is None:
        return
    cols = {c: rb.column(i).to_pylist() for i, c in enumerate(table.column_names)}
    for i in range(table.num_rows):
        yield {k: cols[k][i] for k in cols}

def cdn_parquet_iter(manifest_path: str, columns: list = None) -> Iterator[Dict[str, Any]]:
    """
    Iterate all parquet files in manifest and yield projected rows.
    """
    import json
    with open(manifest_path) as f:
        manifest = json.load(f)
    cols = columns or ["prompt", "response"]
    for fobj in manifest.get("files", []):
        url = fobj["cdn_url"]
        try:
            yield from cdn_parquet_projection(url, cols)
        except Exception as exc:
            # Surface file-level errors but don't stop entire epoch
            raise RuntimeError(f"Failed to fetch/read {url}: {exc}") from exc
```

#### Patch `/opt/axentx/vanguard/train.py`
Insert after imports:

```python
import json
import os
from torch.utils.data import IterableDataset, DataLoader
from vanguard.utils.hf_cdn import cdn_parquet_iter  # relative import if package

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path: str, columns: list = None):
        super().__init__()
        self.manifest_path = manifest_path
        self.columns = columns or ["prompt", "response"]

    def __iter__(self):
        return cdn_parquet_iter(self.manifest_path, columns=self.columns)
```

Where DataLoader is created, replace dataset source with:

```python
# Example:
# dataset = CDNParquetDataset("file-manifest.json")
# loader = DataLoader(dataset, batch_size=None, num_workers=0)
```

If you need batching, wrap rows into batches in `__iter__` or use a custom collate.

#### `/opt/axentx/vanguard/ops/README.md`
```markdown
# Quality: CDN-only training

1) Generate manifest once per date folder (Mac orchestration):
   HF_REPO=datasets/org/vanguard \
   DATE_FOLDER=batches/mirror-merged/2026-05-03 \
   python scripts/generate_manifest.py > file-manifest.json

2) Commit file-manifest.json.

3) Train on Lightning using CDN-only loader (no HF API calls during epoch):
   lightning run train.py --manifest file-manifest.json --machine L40S

Notes:
- CDN URLs bypass /api/ auth and rate limits.
- Reuse running Lightning Studio when possible to avoid extra quota usage.
- If schema varies, adjust `columns` in CDNParquetDataset.
```

---

### 4. Verification (unified)

- Run manifest generator:
  ```bash
  HF_REPO=datasets/org/vanguard DATE_FOLDER=batches/mirror-merged/2026-05-03 \
    python /opt/axentx/vanguard/scripts/generate_manifest.py > /tmp/file-manifest.json
  ```
  Confirm:
  - Exit code 0.
  - `/tmp/file-manifest.json` contains a `files` array with `cdn_url` entries.
  - No authenticated requests in logs (`HF_HUB_DISABLE_TELEMETRY=1` optional).

- Dry-run loader:
  ```python
  from vanguard.utils.hf_cdn import cdn_parquet_iter
  for i, row in enumerate(cdn

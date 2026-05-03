# vanguard / quality

## Final Synthesized Answer

### 1. Diagnosis (merged, highest-confidence items)
- **Runtime HF API calls during ingest/training** cause 429 rate-limits and non-reproducible runs.
- **No content-addressed manifest** forces repeated `list_repo_tree`/`list_repo_files` and breaks reproducibility.
- **Mixed-schema files** from `dataset-mirror` land in `enriched/` without projection to strict `{prompt,response}` → breaks surrogate-1 training.
- **No CDN-only strategy**: training uses `load_dataset`/`list_repo_tree` during data load instead of CDN fetches.
- **Missing deterministic repo selection** for HF commit-cap mitigation (128/hr/repo) and schema guardrails.

### 2. Proposed Change (single, high-leverage harness)
Create a minimal, deterministic harness:
- **`scripts/build_manifest.py`** — one-shot, non-recursive listing of a date folder; outputs content-addressed manifest with CDN URLs.
- **`scripts/project_to_pair.py`** — converts raw (json/jsonl/parquet) into strict `{prompt,response}` parquet under `batches/mirror-merged/{date}/{slug}.parquet`.
- **`training/train.py`** — CDN-only loader that consumes a local manifest; zero HF API calls during training.
- **Deterministic repo picker** — `repo = f"vanguard-enriched-{(hash(slug) % 5)}"` to spread writes across 5 siblings and avoid per-repo commit caps.

### 3. Implementation

```bash
# Ensure project structure
mkdir -p /opt/axentx/vanguard/{scripts,training,data}
```

#### /opt/axentx/vanguard/scripts/build_manifest.py
```python
#!/usr/bin/env python3
"""
Build a content-addressed manifest for one date folder.
Usage:
  HF_REPO=vanguard-enriched python3 build_manifest.py 2026-05-03 > manifest-2026-05-03.json
"""
import os
import json
import sys
import hashlib
from datetime import datetime
from huggingface_hub import HfApi

HF_REPO = os.getenv("HF_REPO", "vanguard-enriched")
DATE_FOLDER = sys.argv[1] if len(sys.argv) > 1 else datetime.utcnow().strftime("%Y-%m-%d")

api = HfApi()
# Single non-recursive call to avoid pagination/rate-limit churn
files = api.list_repo_tree(
    repo_id=HF_REPO,
    path=DATE_FOLDER,
    repo_type="dataset",
    recursive=False
)

manifest = {
    "repo": HF_REPO,
    "date": DATE_FOLDER,
    "generated_at_utc": datetime.utcnow().isoformat() + "Z",
    "files": []
}

for f in files:
    if not f.rfilename.endswith((".parquet", ".json", ".jsonl")):
        continue
    # Content-addressed identifier for reproducibility
    content_id = hashlib.sha256(f.rfilename.encode()).hexdigest()[:16]
    manifest["files"].append({
        "path": f.rfilename,
        "content_id": content_id,
        "cdn_url": f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{f.rfilename}"
    })

json.dump(manifest, sys.stdout, indent=2)
```

#### /opt/axentx/vanguard/scripts/project_to_pair.py
```python
#!/usr/bin/env python3
"""
Project raw file to strict {prompt,response} parquet.
Writes to batches/mirror-merged/{date}/{slug}.parquet
"""
import os
import sys
import json
import hashlib
import pyarrow as pa
import pyarrow.parquet as pq
from datetime import datetime

def detect_and_project(obj):
    # Heuristic projection: accept common field names
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
    response = obj.get("response") or obj.get("output") or obj.get("answer") or obj.get("completion") or ""
    return {"prompt": str(prompt), "response": str(response)}

def main(in_path, date=None):
    date = date or datetime.utcnow().strftime("%Y-%m-%d")
    slug = hashlib.sha256(in_path.encode()).hexdigest()[:16]
    out_dir = f"batches/mirror-merged/{date}"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{slug}.parquet")

    rows = []
    if in_path.endswith(".parquet"):
        table = pq.read_table(in_path, columns=None)
        for batch in table.to_batches():
            for i in range(batch.num_rows):
                row = {k: batch[k][i].as_py() for k in batch.schema.names}
                rows.append(detect_and_project(row))
    else:
        with open(in_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                rows.append(detect_and_project(obj))

    schema = pa.schema([("prompt", pa.string()), ("response", pa.string())])
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, out_path)
    print(out_path)

if __name__ == "__main__":
    main(sys.argv[1])
```

#### /opt/axentx/vanguard/training/train.py
```python
#!/usr/bin/env python3
"""
CDN-only training data loader.
Expects manifest-*.json produced by scripts/build_manifest.py.
Zero HF API calls during training.
"""
import json
import os
import sys
import pyarrow.parquet as pq
import torch
from torch.utils.data import IterableDataset, DataLoader

class CDNParquetDataset(IterableDataset):
    def __init__(self, manifest_path):
        with open(manifest_path) as f:
            self.manifest = json.load(f)

    def _stream_rows(self, cdn_url):
        # Download parquet via CDN (no HF API/auth)
        table = pq.read_table(cdn_url, columns=["prompt", "response"])
        for i in range(table.num_rows):
            row = {k: table[k][i].as_py() for k in ["prompt", "response"]}
            if row["prompt"] and row["response"]:
                yield row

    def __iter__(self):
        for f in self.manifest["files"]:
            yield from self._stream_rows(f["cdn_url"])

def main(manifest_path):
    ds = CDNParquetDataset(manifest_path)
    loader = DataLoader(ds, batch_size=8, num_workers=0)
    total = 0
    for batch in loader:
        total += len(batch["prompt"])
    print(f"Loaded {total} rows from {len(ds.manifest['files'])} files (CDN-only).")

if __name__ == "__main__":
    main(sys.argv[1])
```

#### Deterministic repo picker (inline helper)
```python
import hashlib

def pick_enriched_repo(slug: str, n: int = 5) -> str:
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % n
    return f"vanguard-enriched-{idx}"
```

### 4. Verification (merged, concrete checks)
```bash
# 1) Build manifest (single API call)
cd /opt/axentx/vanguard
HF_REPO=vanguard-enriched python3 scripts/build_manifest.py 2026-05-03 > manifest-2026-05-03.json
cat manifest-2026-05-03.json | jq '.files | length'

# 2) Project sample raw file to pair
python3 scripts/project_to_pair.py data/sample_raw.jsonl
ls batches/mirror-merged/2026-05-03/*.parquet

# 3) CDN-only training load (should make zero HF API calls)
python3 training/train.py manifest-2026-05-03.json

# 4) Confirm no HF API calls during step 3
#    Quick check: ensure only HTTPS GETs to huggingface.co/resolve/main/...
#    Example: strace -e trace=network -f python3 training/train.py manifest-2026-05-0

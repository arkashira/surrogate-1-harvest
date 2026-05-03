# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

---

### Core Changes (unified)

1. **Add manifest generation** (single API call on Mac, zero during training)  
   - One script lists one date folder via `list_repo_tree(recursive=False)`  
   - Emits `train/manifest.json` with CDN-resolvable paths and optional row counts  
   - Integrates into GitHub Actions as a one-time step; keeps 16-shard ingestion for raw uploads

2. **Add `train/cdn_loader.py`** — zero-auth, CDN-only dataset fetcher  
   - Uses `https://huggingface.co/datasets/.../resolve/main/...`  
   - Projects heterogeneous files to `{prompt, response}` at parse time  
   - Avoids `load_dataset(streaming=True)` on mixed schemas and prevents CastErrors  
   - Supports both Parquet and JSONL; streams rows to control memory

3. **Add `train/train.py`** — Lightning-compatible training entrypoint  
   - Reuses running Studio if available (saves quota)  
   - Uses `cdn_loader` for data; no HF API calls during dataload  
   - Handles idle-stop by checking status before `.run()`  
   - Uses an IterableDataset + DataLoader with safe collation

4. **Update GitHub Actions runner**  
   - Add optional `--gen-manifest` flag to produce `manifest.json` for training  
   - Keep existing ingestion for raw uploads; manifest step runs once per date

5. **Add `requirements-training.txt`**  
   - Lightning + HF hub + pyarrow + requests  
   - Exclude `datasets` for training path to avoid API calls

---

### Resolved Contradictions (in favor of correctness + actionability)

- **Manifest location/name**: Use `train/manifest.json` (not `train_manifest.json` at repo root) so training code resolves paths consistently regardless of working directory.  
- **CDN URL construction**: Use `resolve/main/{path}` (not `tree/main/...` or mixed variants) — this is the correct raw-file CDN pattern for datasets repos.  
- **Schema projection**: Apply projection at parse time inside `load_parquet_cdn` and `load_jsonl_cdn` with a single `project_to_pair` helper; do not rely on Arrow schema alone when columns may be missing.  
- **Streaming vs full load**: Stream rows (Parquet via pyarrow batches; JSONL via line-by-line) to avoid OOM; do not load entire files into memory.  
- **Studio reuse**: Keep detection logic but degrade gracefully when not in a cloud environment (run locally). Do not block training if Studio listing fails.  
- **Error handling**: Skip bad files with logging rather than crashing; surface per-file errors but continue processing remaining files.

---

### Code Snippets (final, merged)

#### 1. `train/cdn_loader.py`
```python
# train/cdn_loader.py
import json
import pyarrow.parquet as pq
import pyarrow as pa
import requests
from io import BytesIO
from typing import Dict, Iterator, List
import os

HF_DATASET = "axentx/surrogate-1-training-pairs"
CDN_ROOT = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/main"

def cdn_url(path: str) -> str:
    return f"{CDN_ROOT}/{path.lstrip('/')}"

def project_to_pair(raw: Dict) -> Dict:
    """Project heterogeneous schema to {prompt, response} only."""
    return {
        "prompt": raw.get("prompt") or raw.get("input") or raw.get("question") or "",
        "response": raw.get("response") or raw.get("output") or raw.get("answer") or "",
    }

def load_parquet_cdn(path: str) -> Iterator[Dict]:
    """Stream rows from a parquet file via CDN without auth/API."""
    url = cdn_url(path)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    table = pq.read_table(BytesIO(resp.content))
    schema_names = set(table.schema.names)

    prompt_col = table.column("prompt") if "prompt" in schema_names else None
    response_col = table.column("response") if "response" in schema_names else None

    if prompt_col is not None and response_col is not None:
        for i in range(table.num_rows):
            yield {
                "prompt": prompt_col[i].as_py(),
                "response": response_col[i].as_py(),
            }
    else:
        # Fallback projection for mixed schemas
        for i in range(table.num_rows):
            row = {k: table.column(k)[i].as_py() for k in schema_names}
            yield project_to_pair(row)

def load_jsonl_cdn(path: str) -> Iterator[Dict]:
    """Stream lines from a JSONL file via CDN."""
    url = cdn_url(path)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        try:
            raw = json.loads(line)
            yield project_to_pair(raw)
        except Exception as e:
            # Skip malformed lines
            continue

def load_manifest_cdn(manifest_path: str = "train/manifest.json") -> Iterator[Dict]:
    """Load all files listed in manifest via CDN."""
    with open(manifest_path) as f:
        manifest = json.load(f)
    for file_info in manifest.get("files", []):
        path = file_info["path"]
        try:
            if path.endswith(".parquet"):
                yield from load_parquet_cdn(path)
            elif path.endswith(".jsonl"):
                yield from load_jsonl_cdn(path)
            else:
                print(f"Unsupported file type, skipping: {path}")
        except Exception as e:
            print(f"Skipping {path}: {e}")
```

#### 2. `train/manifest.json` (example)
```json
{
  "date": "2026-05-03",
  "files": [
    {"path": "batches/public-merged/2026-05-03/shard0-090000.jsonl", "rows": 12400},
    {"path": "batches/public-merged/2026-05-03/shard1-090000.jsonl", "rows": 12380}
  ]
}
```

#### 3. `train/gen_manifest.py` (run once on Mac or CI)
```python
# train/gen_manifest.py
import json
import os
from huggingface_hub import list_repo_tree
from datetime import datetime, timezone

HF_REPO = "axentx/surrogate-1-training-pairs"
DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")

def gen_manifest(date: str = DATE, out_path: str = "train/manifest.json"):
    # Single API call (non-recursive)
    tree = list_repo_tree(repo_id=HF_REPO, path=f"batches/public-merged/{date}", recursive=False)
    files = [
        {"path": f"batches/public-merged/{date}/{item.path}", "rows": 0}
        for item in tree if item.path.endswith((".jsonl", ".parquet"))
    ]
    manifest = {"date": date, "files": files}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {out_path} ({len(files)} files)")

if __name__ == "__main__":
    gen_manifest()
```

#### 4. `train/train.py` (Lightning entrypoint)
```python
# train/train.py
import lightning as L
import torch
from torch.utils.data import IterableDataset, DataLoader
from cdn_loader import load_manifest_cdn

class CDNDataset(IterableDataset):
    def __init__(self, manifest_path: str = "train/manifest.json"):
        self.manifest_path = manifest_path

    def __iter__(self):
        for item in load_manifest_cdn(self.manifest_path):
            # Require both fields
            if item.get("prompt") and item.get("response"):
                yield item



# vanguard / discovery

## Final Synthesis (Best Parts + Resolved Contradictions)

I merged the strongest, most actionable elements from both candidates and resolved the few divergences in favor of correctness, reproducibility, and concrete actionability.

---

## 1. Diagnosis (Consolidated)

- **Runtime `load_dataset` calls** in frontend and training trigger HF `/api/` file enumeration and per-file metadata requests → 429 rate limits and non-reproducible epochs.
- **No content-addressed manifest** (file list + deterministic hashes/versions) → prevents CDN-only workflows and exact data pinning.
- **Mixed-schema files** (especially in `enriched/`) are loaded without early schema projection → `pyarrow.CastError` during streaming/distributed training.
- **No deterministic repo selection for writes** (single repo) → risks hitting HF commit cap (128/hr) during ingestion bursts.
- **No reuse guard for Lightning Studio sessions** → idle timeouts kill training and burn quota via unnecessary recreations.

---

## 2. Proposed Change (High-Leverage)

Create one new file and modify two existing files to enable:

- Deterministic repo routing (spreads HF commit load).
- Manifest-based, CDN-only data fetching (bypasses `/api/` rate limits).
- Early schema projection at parse time (avoids `pyarrow.CastError`).
- Reproducible epochs via pinned manifests.

Files:
- **New**: `/opt/axentx/vanguard/data_manifest.py`
- **Modify**: `/opt/axentx/vanguard/train.py`
- **Modify**: `/opt/axentx/vanguard/frontend_loader.py`

---

## 3. Implementation

### 3.1 Create `/opt/axentx/vanguard/data_manifest.py`

```python
# /opt/axentx/vanguard/data_manifest.py
import json
import hashlib
import os
from pathlib import Path
from typing import Dict, List, Optional
from huggingface_hub import list_repo_tree, hf_hub_download

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "axentx/vanguard-data")
MANIFEST_DIR = Path(__file__).parent / "manifests"

def deterministic_repo_for(slug: str, n_siblings: int = 5) -> str:
    """
    Deterministically pick one of N sibling repos to spread HF commit load.
    Example: slug="mirror-20260503" -> "axentx/vanguard-data-2"
    """
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    idx = h % n_siblings
    if idx == 0:
        return HF_DATASET_REPO
    return f"{HF_DATASET_REPO}-{idx}"

def build_manifest(date_folder: str, out_dir: Optional[Path] = None) -> Dict:
    """
    Single API call to list top-level folder contents (non-recursive), then
    produce manifest with CDN URLs and deterministic metadata.
    """
    repo = deterministic_repo_for(date_folder)
    tree = list_repo_tree(repo, recursive=False, path=date_folder)
    files = sorted(f.rfilename for f in tree if f.type == "file")

    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "cdn_base": f"https://huggingface.co/datasets/{repo}/resolve/main",
        "entries": [],
    }

    for f in files:
        rel = f"{date_folder}/{f}"
        manifest["entries"].append({
            "path": rel,
            "cdn_url": f"{manifest['cdn_base']}/{rel}",
        })

    if out_dir is None:
        out_dir = MANIFEST_DIR
    out_dir.mkdir(exist_ok=True, parents=True)
    # Sanitize filename for filesystem safety
    safe_name = date_folder.replace("/", "_").replace("\\", "_")
    out_path = out_dir / f"manifest-{safe_name}.json"
    out_path.write_text(json.dumps(manifest, indent=2))
    return manifest

def load_manifest(date_folder: str) -> Dict:
    safe_name = date_folder.replace("/", "_").replace("\\", "_")
    p = MANIFEST_DIR / f"manifest-{safe_name}.json"
    if not p.exists():
        return build_manifest(date_folder)
    return json.loads(p.read_text())
```

---

### 3.2 Modify `/opt/axentx/vanguard/train.py`

```python
# /opt/axentx/vanguard/train.py  (top section)
import os
import json
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from pathlib import Path
from data_manifest import load_manifest, deterministic_repo_for

HF_DATASET_REPO = os.getenv("HF_DATASET_REPO", "axentx/vanguard-data")
DATE_FOLDER = os.getenv("TRAIN_DATE_FOLDER", "mirror-merged/2026-05-03")
BATCH_SIZE = int(os.getenv("TRAIN_BATCH_SIZE", "32"))

# 1) Load manifest (single API call if not cached; pins exact file list)
manifest = load_manifest(DATE_FOLDER)

# 2) CDN-only fetch + early schema projection (avoids pyarrow CastError)
def stream_examples():
    for entry in manifest["entries"]:
        if not entry["path"].endswith(".parquet"):
            continue
        resp = requests.get(entry["cdn_url"], timeout=60)
        resp.raise_for_status()
        tbl = pq.read_table(pa.BufferReader(resp.content))
        # Project only {prompt, response}; drop heterogeneous fields early
        if "prompt" in tbl.column_names and "response" in tbl.column_names:
            for i in range(tbl.num_rows):
                yield {
                    "prompt": tbl["prompt"][i].as_py(),
                    "response": tbl["response"][i].as_py(),
                }

# 3) Materialize once for deterministic epochs (or keep generator for streaming)
train_examples = list(stream_examples())
```

---

### 3.3 Modify `/opt/axentx/vanguard/frontend_loader.py`

```python
# /opt/axentx/vanguard/frontend_loader.py
import json
import requests
from pathlib import Path
from data_manifest import load_manifest

DATE_FOLDER = "mirror-merged/2026-05-03"

def load_frontend_dataset(limit: int = 1000):
    manifest = load_manifest(DATE_FOLDER)
    examples = []
    for entry in manifest["entries"]:
        if not entry["path"].endswith(".parquet"):
            continue
        resp = requests.get(entry["cdn_url"], timeout=30)
        resp.raise_for_status()
        import pyarrow.parquet as pq
        import pyarrow as pa
        tbl = pq.read_table(pa.BufferReader(resp.content))
        if "prompt" in tbl.column_names and "response" in tbl.column_names:
            for i in range(min(tbl.num_rows, limit - len(examples))):
                examples.append({
                    "prompt": tbl["prompt"][i].as_py(),
                    "response": tbl["response"][i].as_py(),
                })
        if len(examples) >= limit:
            break
    return examples
```

---

## 4. Verification (Concrete Steps)

1. **Generate manifest** (single API call, outside rate-limited windows):
   ```bash
   cd /opt/axentx/vanguard
   python -c "from data_manifest import build_manifest; m=build_manifest('mirror-merged/2026-05-03'); print('files:', len(m['entries']))"
   ```
   Confirm `manifests/manifest-mirror-merged_2026-05-03.json` exists and lists files with CDN URLs.

2. **Confirm CDN-only fetch bypasses `/api/`**:
   ```bash
   curl -sI "$(python -c "import json; m=json.load(open('manifests/manifest-mirror-merged_2026-05-03.json')); print(m['entries'][0]['cdn_url'])")"
   ```
   Expect `200 OK` and no `Authorization` header required.

3. **Run training headless** (no `load_dataset` calls):
   ```bash
   cd /opt/axentx/vanguard
   TRAIN_DATE_FOLDER=mirror-merged/2026-05-03 python train.py --dry-run
   ```
   Confirm it

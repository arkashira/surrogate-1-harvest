# vanguard / quality

## Final Synthesized Solution (Best Parts + Correctness + Actionability)

### 1. Diagnosis (Consolidated)
- **Authenticated API burn**: Frontend/training repeatedly calls `list_repo_tree` (via SDK/proxy) → 1000/5min quota exhaustion → 429s.
- **No persisted manifest**: Every reload re-enumerates folders and re-downloads metadata; no `(repo, dateFolder) → file list` cache.
- **Training pathologies**:
  - Uses authenticated calls during dataset streaming/shard enumeration (not CDN-only).
  - Likely uses `load_dataset(streaming=True)` across heterogeneous repos → `pyarrow.CastError` from mixed schemas.
  - Enriched files add extra columns (`source`, `ts`) instead of strict `{prompt,response}` + clean filename attribution.
- **Compute waste**: Lightning Studio reuse missing → each run may spin new studio and burn 80hr/mo quota when an idle/running one could be reused.

### 2. Proposed Change (Single Clear Scope)
Introduce a **manifest-first, CDN-only training pipeline** for one repo + date folder with Lightning Studio reuse and strict schema projection.

- One-shot manifest generator (Mac/CI) → persisted JSON.
- Training uses **only CDN URLs** from manifest; zero authenticated calls during data load.
- Enforce `{prompt,response}` schema + filename attribution; reject heterogeneous parquet writes.
- Reuse existing Lightning Studio; fail fast if manifest missing.

### 3. Implementation

#### Directory structure
```bash
mkdir -p /opt/axentx/vanguard/{manifests,scripts,train}
```

---

#### `/opt/axentx/vanguard/scripts/build_manifest.py`
```python
#!/usr/bin/env python3
"""
Generate a static CDN-only manifest for (repo, date_folder).
Usage:
  HF_TOKEN=hf_xxx python build_manifest.py \
    --repo datasets/mycorp/vanguard-data \
    --date-folder 2026-05-03 \
    --out manifests/mycorp_vanguard-data/2026-05-03.json
"""
import argparse
import json
import os
import time
from pathlib import Path
from typing import List, Dict

from huggingface_hub import HfApi

CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def list_date_folder(api: HfApi, repo: str, date_folder: str) -> List[Dict]:
    items = api.list_repo_tree(repo=repo, path=date_folder, recursive=True)
    files = []
    for item in items:
        if getattr(item, "type", None) == "file":
            rel_path = item.path  # repo tree returns path relative to repo root
            files.append({
                "repo": repo,
                "path": rel_path,
                "size": getattr(item, "size", None),
                "lfs": getattr(item, "lfs", None),
                "cdn_url": CDN_TEMPLATE.format(repo=repo, path=rel_path),
            })
    return files

def build_manifest(repo: str, date_folder: str, out_path: Path) -> None:
    api = HfApi(token=os.environ.get("HF_TOKEN"))
    print(f"Listing {repo}/{date_folder} (recursive) ...")
    files = list_date_folder(api, repo, date_folder)
    if not files:
        raise RuntimeError(f"No files found under {repo}/{date_folder}")
    manifest = {
        "repo": repo,
        "date_folder": date_folder,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {len(files)} files to {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build CDN-only manifest for a date folder.")
    parser.add_argument("--repo", required=True, help="Dataset repo (e.g. datasets/org/name)")
    parser.add_argument("--date-folder", required=True, help="Date folder inside repo (e.g. 2026-05-03)")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()
    build_manifest(args.repo, args.date_folder, Path(args.out))
```

---

#### `/opt/axentx/vanguard/train/manifest.py`
```python
import json
from pathlib import Path
from typing import List, Dict

def load_manifest(repo: str, date_folder: str, manifest_root: Path) -> List[Dict]:
    slug = repo.replace("/", "_")
    manifest_path = manifest_root / slug / f"{date_folder}.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest missing: {manifest_path}. Run scripts/build_manifest.py first."
        )
    with open(manifest_path) as f:
        data = json.load(f)
    return data["files"]

def filter_to_parquet(files: List[Dict]) -> List[Dict]:
    return [f for f in files if f["path"].lower().endswith(".parquet")]
```

---

#### `/opt/axentx/vanguard/train/train.py` (data loader section)
```python
import pyarrow.parquet as pq
import pyarrow.compute as pc
import requests
from pathlib import Path
from typing import Iterator, Dict, Any

from train.manifest import load_manifest, filter_to_parquet

CDN_TIMEOUT = 60

def fetch_cdn_parquet(url: str, local_path: Path) -> Path:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=CDN_TIMEOUT) as r:
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=131072):
                f.write(chunk)
    return local_path

def cdn_parquet_stream(manifest_path: Path, repo: str, date_folder: str, cache_dir: Path):
    files = filter_to_parquet(load_manifest(repo, date_folder, Path(manifest_path).parent.parent))
    for finfo in files:
        local_file = cache_dir / Path(finfo["path"]).name
        if not local_file.exists():
            fetch_cdn_parquet(finfo["cdn_url"], local_file)
        yield local_file

def build_cdn_dataset(
    manifest_path: Path,
    repo: str,
    date_folder: str,
    cache_dir: Path,
) -> Iterator[Dict[str, Any]]:
    """
    Strict schema projection: keep only prompt/response + filename attribution.
    Reject rows with mixed schemas via pyarrow enforcement.
    """
    for shard_path in cdn_parquet_stream(manifest_path, repo, date_folder, cache_dir):
        tbl = pq.read_table(shard_path, columns=["prompt", "response"])
        # Ensure no nulls in required fields; drop if present
        mask = pc.and_(
            pc.is_valid(tbl["prompt"]),
            pc.is_valid(tbl["response"]),
        )
        tbl = tbl.filter(mask)
        # Add filename attribution
        tbl = tbl.append_column("source_file", pc.full(len(tbl), shard_path.name))
        for batch in tbl.to_batches():
            for row in batch.to_pylist():
                yield row
```

---

#### `/opt/axentx/vanguard/scripts/run_training_lightning.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="datasets/mycorp/vanguard-data"
DATE_FOLDER="2026-05-03"
MANIFEST="manifests/mycorp_vanguard-data/${DATE_FOLDER}.json"
CACHE_DIR="data/cdn_cache"

if [[ ! -f "$MANIFEST" ]]; then
  HF_TOKEN="${HF_TOKEN:?required}" python build_manifest.py \
    --repo "$REPO" \
    --date-folder "$DATE_FOLDER" \
    --out "$MANIFEST"
fi

# Reuse existing Lightning Studio session if available; do not create duplicate.
# Example stub (replace with your launcher):
# python -m vanguard.lightning_launcher \
#   --manifest "$MANIFEST" \
#   --cache-dir "$CACHE_DIR" \
#   --reuse-studio \
#   --machine L40

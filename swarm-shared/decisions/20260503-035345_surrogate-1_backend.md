# surrogate-1 / backend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- Uses a **single API call** from the runner (within rate-limit window) to list one date folder via `list_repo_tree(recursive=False)` → saves `file-list.json`.
- Deterministically shards files by `hash(slug) % SHARD_TOTAL`; each worker processes only its shard.
- Downloads files via **HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (bypasses `/api/` rate limits).
- Projects heterogeneous schemas to `{prompt, response}` at parse time (avoids pyarrow CastError from `load_dataset(streaming=True)`).
- Deduplicates via central md5 store (`lib/dedup.py`) and writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Commits to the dataset repo using the HF Hub API (respects 128/hr/repo cap; sharding prevents collisions).

---

### Changes

1. Create `bin/dataset-enrich.py` (replaces shell script).
2. Update `.github/workflows/ingest.yml` to run the Python worker with matrix env.
3. Add `requirements.txt` entries if missing (`requests`, `tqdm`).
4. Keep `lib/dedup.py` unchanged (central md5 store).

---

### Code Snippets

#### `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py
"""

import os
import sys
import json
import hashlib
import datetime
import subprocess
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download, list_repo_tree
from datasets import load_dataset
import pyarrow as pa
import pyarrow.parquet as pq
import numpy as np

# ── config --
REPO_ID = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("ERROR: HF_TOKEN is required", file=sys.stderr)
    sys.exit(1)

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.date.today().isoformat())

API = HfApi(token=HF_TOKEN)
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}
CDN_ROOT = f"https://huggingface.co/datasets/{REPO_ID}/resolve/main"

WORKDIR = Path(__file__).parent.parent
MANIFEST = WORKDIR / "file-list.json"
OUTDIR = WORKDIR / "output"
OUTDIR.mkdir(exist_ok=True)

# ── dedup --
sys.path.insert(0, str(WORKDIR / "lib"))
from dedup import DedupStore  # type: ignore

dedup = DedupStore()

# ── helpers --
def slug_hash(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16)

def list_date_files(date_folder: str) -> List[str]:
    """Single API call: non-recursive tree for one date folder."""
    try:
        tree = list_repo_tree(
            repo_id=REPO_ID,
            path=date_folder,
            repo_type="dataset",
            token=HF_TOKEN,
        )
    except Exception as e:
        print(f"Failed to list {date_folder}: {e}", file=sys.stderr)
        return []
    files = [t.path for t in tree if t.type == "file"]
    return sorted(files)

def download_cdn(path: str, dest: Path) -> None:
    url = f"{CDN_ROOT}/{path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)

def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Best-effort projection for heterogeneous schemas.
    Prefer fields: prompt/response, question/answer, input/output, text.
    """
    p = raw.get("prompt") or raw.get("question") or raw.get("input") or raw.get("text") or ""
    r = raw.get("response") or raw.get("answer") or raw.get("output") or ""
    # If both empty, try to extract from raw JSON string fields
    if not p and not r and isinstance(raw.get("text"), str):
        # naive fallback: split by common separators
        parts = raw["text"].split("\n\n")
        if len(parts) >= 2:
            p, r = parts[0], parts[1]
    return {"prompt": str(p).strip(), "response": str(r).strip()}

def normalize_file(path: str) -> List[Dict[str, str]]:
    """Download and normalize one file to list of {prompt, response}."""
    local = OUTDIR / path.replace("/", "_")
    try:
        download_cdn(path, local)
    except Exception as e:
        print(f"Download failed {path}: {e}", file=sys.stderr)
        return []

    pairs = []
    suffix = local.suffix.lower()
    try:
        if suffix == ".jsonl":
            import jsonlines
            with jsonlines.open(local) as reader:
                for obj in reader:
                    pairs.append(project_to_pair(obj))
        elif suffix == ".json":
            with local.open() as f:
                data = json.load(f)
            if isinstance(data, list):
                for obj in data:
                    pairs.append(project_to_pair(obj))
            else:
                pairs.append(project_to_pair(data))
        elif suffix in (".parquet", ".pq"):
            table = pq.read_table(local)
            df = table.to_pandas()
            for _, row in df.iterrows():
                pairs.append(project_to_pair(row.to_dict()))
        else:
            # fallback: try load_dataset on single file (streaming=False)
            ds = load_dataset("json", data_files=str(local), split="train")
            for row in ds:
                pairs.append(project_to_pair(row))
    except Exception as e:
        print(f"Parse failed {path}: {e}", file=sys.stderr)
    finally:
        if local.exists():
            local.unlink()
    return pairs

def upload_shard(pairs: List[Dict[str, str]]) -> None:
    """Write shard JSONL and commit to dataset repo."""
    ts = datetime.datetime.utcnow().strftime("%H%M%S")
    out_name = f"shard{SHARD_ID}-{ts}.jsonl"
    date_out = OUTDIR / "public-merged" / DATE_FOLDER / out_name
    date_out.parent.mkdir(parents=True, exist_ok=True)

    with date_out.open("w") as f:
        for pair in pairs:
            f.write(json.dumps(pair, ensure_ascii=False) + "\n")

    # Upload to dataset repo under batches/public-merged/<date>/
    remote_path = f"batches/public-merged/{DATE_FOLDER}/{out_name}"
    try:
        API.upload_file(
            path_or_fileobj=str(date_out),
            path_in_repo=remote_path,
            repo_id=REPO_ID,
            repo_type="dataset",
            commit_message=f"shard{SHARD_ID} public-merge {DATE_FOLDER}",
        )
        print(f"Uploaded {remote_path}")
    except Exception as e:
        print(f"Upload failed: {e}", file=sys.stderr)
        raise

# ── main --
def main() -> None:
    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} | date={DATE_FOLDER}")

    # 1) list files once (rate-limit friendly)
    if MANIFEST.exists():
        with MANIFEST.open() as f:
            all_files = json.load(f)
        print(f"Loaded manifest with {len(all_files)} files")
    else:
        all_files = list_date_files

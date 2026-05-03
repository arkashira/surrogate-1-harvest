# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` and `SHARD_TOTAL` (16) from GitHub Actions matrix.
- Uses a pre-generated `file-list.json` (Mac-side `list_repo_tree` snapshot) to deterministically shard file paths without any HF API calls during ingestion.
- Downloads assigned files via **HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero Authorization header, bypasses `/api/` rate limits.
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids PyArrow CastError).
- Deduplicates via the existing `lib/dedup.py` central md5 store.
- Writes normalized JSONL to `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` and pushes to `axentx/surrogate-1-training-pairs` using a single commit per shard.

### Steps (1h 30m total)

1. **Create `bin/dataset-enrich.py`** (45m) — manifest sharding, CDN download, schema projection, dedup, upload.
2. **Add `bin/manifest-snapshot.py`** (15m) — one-off helper to generate `file-list.json` from Mac (run when rate-limit window is clear).
3. **Update GitHub Actions matrix** (10m) — ensure `SHARD_ID`/`SHARD_TOTAL` passed and `file-list.json` is present in repo.
4. **Remove/disable old `dataset-enrich.sh`** (5m) — keep as backup or delete.
5. **Smoke test** (15m) — run one shard locally with a small file list.

---

## Code Snippets

### 1. `bin/manifest-snapshot.py` (run from Mac when HF API window is clear)

```python
#!/usr/bin/env python3
"""
Generate file-list.json for surrogate-1-training-pairs.
Run from Mac (or any machine with HF token) when API rate-limit allows.
"""
import json
import os
from huggingface_hub import HfApi

REPO = "axentx/surrogate-1-training-pairs"
OUT = "file-list.json"

def main() -> None:
    api = HfApi(token=os.environ["HF_TOKEN"])
    # Only top-level listing to avoid 100x pagination; recurse by date folders if needed.
    files = api.list_repo_tree(repo_id=REPO, recursive=True)
    paths = [f.rfilename for f in files if f.rfilename.endswith((".parquet", ".jsonl", ".csv"))]
    with open(OUT, "w") as f:
        json.dump({"repo": REPO, "generated_by": "manifest-snapshot", "paths": paths}, f, indent=2)
    print(f"Wrote {len(paths)} paths to {OUT}")

if __name__ == "__main__":
    main()
```

Make executable and commit `file-list.json` (or generate during workflow if preferred).

---

### 2. `bin/dataset-enrich.py` (new worker)

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py

Expects file-list.json in repo root.
"""
import json
import os
import sys
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pyarrow.parquet as pq
import requests

# Local dedup module
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

REPO = "axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/datasets/{REPO}/resolve/main"
MANIFEST = "file-list.json"
DATE_STR = datetime.now(timezone.utc).strftime("%Y-%m-%d")
OUT_DIR = Path("batches/public-merged") / DATE_STR
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Deterministic shard assignment
SHARD_ID = int(os.environ.get("SHARD_ID", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "16"))

def load_manifest() -> List[str]:
    with open(MANIFEST) as f:
        data = json.load(f)
    return data["paths"]

def shard_paths(paths: List[str]) -> List[str]:
    assigned = []
    for i, p in enumerate(paths):
        if hash(p) % SHARD_TOTAL == SHARD_ID:
            assigned.append(p)
    return assigned

def download_via_cdn(path: str, dest: Path) -> None:
    url = f"{BASE_CDN}/{path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)

def project_to_pair(obj: Dict[str, Any]) -> Dict[str, str]:
    """
    Project heterogeneous schema to {prompt, response}.
    Heuristic: look for common field names; fallback to first/second text-like fields.
    """
    prompt_keys = {"prompt", "instruction", "input", "question", "user"}
    response_keys = {"response", "completion", "output", "answer", "assistant"}

    prompt = None
    response = None

    for k, v in obj.items():
        if k in prompt_keys and isinstance(v, str) and v.strip():
            prompt = v.strip()
        if k in response_keys and isinstance(v, str) and v.strip():
            response = v.strip()

    if prompt is None or response is None:
        text_fields = [v for v in obj.values() if isinstance(v, str) and v.strip()]
        if len(text_fields) >= 2:
            prompt, response = text_fields[0].strip(), text_fields[1].strip()
        elif len(text_fields) == 1:
            parts = [p.strip() for p in text_fields[0].split("\n\n") if p.strip()]
            if len(parts) >= 2:
                prompt, response = parts[0], parts[1]
            else:
                prompt, response = parts[0], ""
        else:
            prompt, response = "", ""

    return {"prompt": prompt, "response": response}

def process_parquet(path: str, dedup: DedupStore) -> List[Dict[str, str]]:
    local_path = Path("tmp") / Path(path).name
    local_path.parent.mkdir(exist_ok=True)
    download_via_cdn(path, local_path)

    pairs = []
    try:
        table = pq.read_table(local_path)
        df = table.to_pandas()
    except Exception as e:
        print(f"Parquet read failed for {path}: {e}", file=sys.stderr)
        return pairs
    finally:
        if local_path.exists():
            local_path.unlink()

    for _, row in df.iterrows():
        obj = row.to_dict()
        raw = json.dumps(obj, sort_keys=True).encode()
        md5 = hashlib.md5(raw).hexdigest()
        if dedup.exists(md5):
            continue
        pair = project_to_pair(obj)
        if pair["prompt"] or pair["response"]:
            pair["_md5"] = md5
            pairs.append(pair)
            dedup.add(md5)
    return pairs

def process_jsonl(path: str, dedup: DedupStore) -> List[Dict[str, str]]:
    local_path = Path("tmp") / Path(path).name
    local_path.parent.mkdir(exist_ok=True)
    download_via_cdn(path, local_path)

    pairs = []
    with open(local_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw = json.dumps(obj, sort_keys=True).encode()
            md5 = hashlib.md5(raw).hexdigest()
            if dedup.exists(md5):
                continue
            pair = project_to_pair(obj)
            if pair["prompt"] or pair["response"]:
                pair["_md5"] = md5
                pairs.append(pair)
                dedup.add(md5)
    if

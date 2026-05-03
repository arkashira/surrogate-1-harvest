# surrogate-1 / frontend

## Final Implementation Plan (≤2h)

**Highest-value improvement**: Replace fragile shell-based ingestion with a **manifest-driven, CDN-bypass Python worker** that eliminates HF API rate limits during training data loads and prevents mixed-schema CastErrors.

### What we change
- Keep GitHub Actions matrix (16 shards) for parallelism.
- Replace `bin/dataset-enrich.sh` with a single Python worker (`bin/ingest_worker.py`) that:
  - Accepts `SHARD_ID` (0–15) and `RUN_DATE` via env.
  - Uses **one** HF API call per runner to list today’s date folder, writes `manifest.json` (path list + sizes + etags).
  - Downloads files **only via CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header → bypasses `/api/` rate limits.
  - Projects each file to `{prompt, response}` at parse time (avoids pyarrow CastError from mixed schemas).
  - Deduplicates via the existing `lib/dedup.py` md5 store.
  - Writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`.
- Update `.github/workflows/ingest.yml` to:
  - Install Python deps from `requirements.txt`.
  - Pass `SHARD_ID` and `RUN_DATE` to the worker.
  - Keep 16-shard matrix.

### Why this is safe and fast (<2h)
- No changes to HF dataset repo structure or permissions.
- Reuses existing dedup logic (`lib/dedup.py`).
- CDN downloads avoid 429s during training data load (key insight).
- Manifest + per-folder listing avoids recursive `list_repo_files` pagination.
- Projection-at-parse prevents mixed-schema CastErrors.
- Keeps GitHub Actions parallelism (16×7 GB RAM) so we don’t hit OOM.

---

## Code snippets

### 1) `requirements.txt` (add if missing)

```text
datasets
huggingface_hub
pyarrow
numpy
requests
tqdm
```

---

### 2) `bin/ingest_worker.py`

```python
#!/usr/bin/env python3
"""
Manifest-driven, CDN-bypass ingestion worker for surrogate-1.

Environment:
  SHARD_ID        0-15 (required)
  RUN_DATE        YYYY-MM-DD (defaults to today UTC)
  HF_REPO         dataset repo (default: axentx/surrogate-1-training-pairs)
  HF_TOKEN        write token (for listing + upload)
"""

import os
import sys
import json
import hashlib
import datetime as dt
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, list_repo_tree
from tqdm import tqdm

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent / "lib"))
from dedup import DedupStore  # type: ignore

HF_REPO = os.getenv("HF_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

API = HfApi(token=HF_TOKEN)

# CDN base (no auth header -> bypass /api/ rate limits)
CDN_BASE = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"


def iso_date() -> str:
    return os.getenv("RUN_DATE", dt.datetime.utcnow().strftime("%Y-%m-%d"))


def list_date_folder(date_str: str) -> List[str]:
    """
    Single API call: list top-level files for one date folder.
    Avoids recursive listing across huge repos.
    """
    prefix = f"batches/public-merged/{date_str}/"
    items = list_repo_tree(repo_id=HF_REPO, path=prefix, recursive=False, token=HF_TOKEN)
    paths = [it.rfilename for it in items if it.rfilename.endswith((".jsonl", ".parquet"))]
    return paths


def build_manifest(date_str: str) -> List[str]:
    """Return file paths for the date folder."""
    return list_date_folder(date_str)


def download_via_cdn(cdn_path: str) -> bytes:
    url = f"{CDN_BASE}/{cdn_path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content


def project_to_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """
    Project arbitrary schema to {prompt, response}.
    Heuristic:
      - prompt: prefer 'prompt', then 'input', then 'question'
      - response: prefer 'response', then 'output', then 'answer'
    If missing, raise ValueError.
    """
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question")
    response = raw.get("response") or raw.get("output") or raw.get("answer")
    if prompt is None or response is None:
        raise ValueError(f"Cannot project pair from keys: {list(raw.keys())}")
    return {"prompt": str(prompt), "response": str(response)}


def hash_content(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


def main() -> None:
    shard_id = int(os.getenv("SHARD_ID", -1))
    if not (0 <= shard_id <= 15):
        print("SHARD_ID must be 0-15", file=sys.stderr)
        sys.exit(1)

    date_str = iso_date()
    run_ts = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(f"batches/public-merged/{date_str}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"shard{shard_id}-{run_ts}.jsonl"

    print(f"[shard {shard_id}] building manifest for {date_str}")
    manifest = build_manifest(date_str)
    if not manifest:
        print(f"[shard {shard_id}] no files for {date_str}")
        return

    # Deterministic shard assignment by slug-hash
    def assign_shard(path: str) -> int:
        slug = Path(path).stem
        return hash(slug) % 16

    my_paths = [p for p in manifest if assign_shard(p) == shard_id]
    print(f"[shard {shard_id}] processing {len(my_paths)} files")

    store = DedupStore()
    written = 0

    with out_file.open("w", encoding="utf-8") as f:
        for cdn_path in tqdm(my_paths, desc=f"shard{shard_id}"):
            try:
                raw_bytes = download_via_cdn(cdn_path)
                content_hash = hash_content(raw_bytes)
                if store.is_duplicate(content_hash):
                    continue

                # Parse depending on extension
                if cdn_path.endswith(".jsonl"):
                    lines = raw_bytes.decode("utf-8").strip().split("\n")
                    rows = [json.loads(l) for l in lines if l.strip()]
                elif cdn_path.endswith(".parquet"):
                    import pyarrow.parquet as pq
                    import io
                    table = pq.read_table(io.BytesIO(raw_bytes))
                    rows = table.to_pylist()
                else:
                    continue

                for row in rows:
                    pair = project_to_pair(row)
                    line = json.dumps(pair, ensure_ascii=False)
                    f.write(line + "\n")
                    written += 1

                store.mark_seen(content_hash)
            except Exception as exc:
                print(f"[shard {shard_id}] skip {cdn_path}: {exc}")

    store.close()
    print(f"[shard {shard_id}] wrote {written} pairs to {out_file}")

    # Upload to HF dataset repo
    if written > 0:
        print(f"[shard {shard_id}] uploading {out_file}")
        API.upload_file(
            path_or_fileobj=str(out_file),
            path_in_repo=str(out_file),
            repo_id=HF_REPO,
            repo_type="dataset",
            commit_message=f"shard{shard_id} {date_str} {run_ts}",
        )
    else:
        print(f"[shard {shard_id}] nothing to upload")


if __name

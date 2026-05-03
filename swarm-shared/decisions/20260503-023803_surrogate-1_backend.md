# surrogate-1 / backend

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Uses **one** `list_repo_tree` call (per date folder) to enumerate files, then saves the file list to `manifest-{DATE}.json`
- Each shard deterministically hashes `slug → shard` and processes only its slice
- Downloads via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header during data fetch
- Projects heterogeneous files to `{prompt, response}` only at parse time (avoids pyarrow CastError)
- Deduplicates via existing `lib/dedup.py` (central md5 store)
- Outputs `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Reuses existing patterns: no `source`/`ts` columns, attribution in filename, HF commit-cap spreading handled by shard isolation

---

### 1) Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.
Usage (local/test):
  HF_TOKEN=hf_xxx \
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  python bin/dataset-enrich.py

GitHub Actions will invoke via bash wrapper with same env vars.
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from huggingface_hub import HfApi, hf_hub_download

# Project imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa

# ---- config ----
HF_REPO = "axentx/surrogate-1-training-pairs"
DATASET_ROOT = "public-raw"            # folder in HF repo containing date subfolders
BATCH_ROOT = "batches/public-merged"   # output folder in HF repo
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
DATE = os.getenv("DATE", datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    print("ERROR: HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

API = HfApi(token=HF_TOKEN)
DEDUP = DedupStore()

# ---- helpers ----
def hf_api_safe_list_tree(path: str) -> List[Dict[str, Any]]:
    """
    Single list_repo_tree call (non-recursive) to avoid pagination/rate-limits.
    Returns list of dicts with at least 'path' and 'type'.
    """
    try:
        items = API.list_repo_tree(repo_id=HF_REPO, path=path, recursive=False)
        return [{"path": i.rfilename, "type": "file" if i.type == "file" else "dir"} for i in items]
    except Exception as e:
        print(f"ERROR listing {path}: {e}", file=sys.stderr)
        return []

def slug_for_file(rel_path: str) -> str:
    """Deterministic slug from relative path (used for shard assignment)."""
    return hashlib.sha256(rel_path.encode()).hexdigest()

def shard_for_slug(slug: str) -> int:
    return int(slug, 16) % SHARD_TOTAL

def cdn_download_url(repo: str, path_in_repo: str) -> str:
    """HF CDN bypass URL (no auth header required)."""
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path_in_repo}"

def safe_download(url: str, timeout: int = 30) -> Optional[bytes]:
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"WARNING: failed to download {url}: {e}", file=sys.stderr)
        return None

def parse_file_to_pairs(content: bytes, filename: str) -> List[Dict[str, str]]:
    """
    Project heterogeneous files to {prompt, response} only.
    Implement minimal parsers for known schemas; ignore unknown.
    """
    pairs = []
    fn = filename.lower()

    # Parquet: load via pyarrow only when needed (avoid streaming mixed schemas)
    if fn.endswith(".parquet"):
        import io, pyarrow.parquet as pq
        try:
            table = pq.read_table(io.BytesIO(content))
            df = table.to_pandas()
            # Heuristic column names
            prompt_col = next((c for c in df.columns if "prompt" in c.lower()), None)
            response_col = next((c for c in df.columns if "response" in c.lower() or "completion" in c.lower()), None)
            if prompt_col and response_col:
                for _, row in df.iterrows():
                    pairs.append({"prompt": str(row[prompt_col]), "response": str(row[response_col])})
        except Exception as e:
            print(f"WARNING: parquet parse failed {filename}: {e}", file=sys.stderr)
        return pairs

    # JSONL
    if fn.endswith(".jsonl"):
        import json as jsonlib
        for line in content.decode("utf-8", errors="ignore").strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = jsonlib.loads(line)
                prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
                response = obj.get("response") or obj.get("output") or obj.get("answer")
                if prompt and response:
                    pairs.append({"prompt": str(prompt), "response": str(response)})
            except Exception:
                continue
        return pairs

    # JSON (single object or list)
    if fn.endswith(".json"):
        import json as jsonlib
        try:
            data = jsonlib.loads(content.decode("utf-8", errors="ignore"))
            if isinstance(data, list):
                items = data
            else:
                items = [data]
            for obj in items:
                prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
                response = obj.get("response") or obj.get("output") or obj.get("answer")
                if prompt and response:
                    pairs.append({"prompt": str(prompt), "response": str(response)})
        except Exception as e:
            print(f"WARNING: json parse failed {filename}: {e}", file=sys.stderr)
        return pairs

    # CSV (basic)
    if fn.endswith(".csv"):
        import csv, io as io_
        try:
            reader = csv.DictReader(io_.StringIO(content.decode("utf-8", errors="ignore")))
            for row in reader:
                prompt = row.get("prompt") or row.get("input") or row.get("question")
                response = row.get("response") or row.get("output") or row.get("answer")
                if prompt and response:
                    pairs.append({"prompt": str(prompt), "response": str(response)})
        except Exception as e:
            print(f"WARNING: csv parse failed {filename}: {e}", file=sys.stderr)
        return pairs

    # Unknown — skip
    return pairs

def upload_batch(entries: List[Dict[str, str]], date: str, shard_id: int) -> Path:
    """Write shard output to local temp file; caller will commit to HF repo."""
    out_dir = Path("output") / BATCH_ROOT / date
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%H%M%S")
    out_path = out_dir / f"shard{shard_id}-{ts}.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"Wrote {len(entries)} pairs to {out_path}")
    return out_path

def commit_to_hf(local_file: Path, repo_path: str) -> None:
    """Upload file to HF dataset repo."""
    API.upload_file(
        path_or_fileobj

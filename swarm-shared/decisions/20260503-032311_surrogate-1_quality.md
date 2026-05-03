# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env
- Single `list_repo_tree(path, recursive=False)` per date folder → deterministic file list
- Saves manifest JSON to `batches/manifest/{DATE}/file-list.json` (committed once per date)
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads via CDN (`resolve/main/...`) with **zero HF API calls during data load** (bypasses 429)
- Projects heterogeneous schemas to `{prompt,response}` only at parse time (avoids pyarrow CastError)
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with no extra metadata columns
- Reuses existing `lib/dedup.py` for cross-source md5 dedup (central sqlite store)
- Adds retry/backoff for 429 (wait 360s) and commit-cap spreading across sibling repos
- Returns exit code 0 on success, non-zero on hard failure (GitHub Actions will retry)

---

### Steps (timed)

1. **Create `bin/dataset-enrich.py`** (60 min) — manifest fetch, CDN download, schema projection, shard filtering, dedup, upload
2. **Update `.github/workflows/ingest.yml`** (15 min) — switch from `bash dataset-enrich.sh` to `python bin/dataset-enrich.py`, pass matrix `shard_id`, `date`, `HF_TOKEN`
3. **Remove/backup `bin/dataset-enrich.sh`** (5 min)
4. **Smoke test locally** (20 min) — run one shard against a small date folder, verify output JSONL and no API calls during download
5. **Commit & push** (10 min)

---

## `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.

Usage (via env):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 HF_TOKEN=hf_xxx python bin/dataset-enrich.py
"""

import os
import sys
import json
import hashlib
import datetime
import subprocess
import time
from pathlib import Path
from typing import List, Dict, Any

import requests
import pyarrow.parquet as pq
import pyarrow as pa

# ── config --
HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
BASE_DIR = Path(__file__).parent.parent
LIB_DIR = BASE_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

# ── env --
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE", datetime.datetime.utcnow().strftime("%Y-%m-%d"))
HF_TOKEN = os.getenv("HF_TOKEN", "")
if not HF_TOKEN:
    print("ERROR: HF_TOKEN required", file=sys.stderr)
    sys.exit(1)

# ── paths --
OUT_DIR = BASE_DIR / "batches" / "public-merged" / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)
MANIFEST_DIR = BASE_DIR / "batches" / "manifest" / DATE
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
MANIFEST_FILE = MANIFEST_DIR / "file-list.json"
TIMESTAMP = datetime.datetime.utcnow().strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TIMESTAMP}.jsonl"

# ── helpers --
def hf_api_get(path: str, params: Dict[str, Any] = None) -> Any:
    url = f"https://huggingface.co/api/{path}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    r = requests.get(url, headers=headers, params=params, timeout=30)
    if r.status_code == 429:
        retry_after = int(r.headers.get("Retry-After", "360"))
        print(f"Rate limited. Sleeping {retry_after}s", file=sys.stderr)
        time.sleep(retry_after)
        return hf_api_get(path, params)
    r.raise_for_status()
    return r.json()

def list_date_files(date: str) -> List[str]:
    """Single API call: list files in date folder (non-recursive)."""
    tree = hf_api_get(f"{HF_REPO}/tree/main/batches/public-merged/{date}", {"recursive": "false"})
    files = []
    for entry in tree:
        if entry.get("type") == "blob":
            files.append(entry["path"])
    return files

def save_manifest(files: List[str]) -> None:
    with MANIFEST_FILE.open("w", encoding="utf-8") as f:
        json.dump(files, f, indent=2)

def slug_from_path(path: str) -> str:
    """Deterministic slug for sharding."""
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0]

def shard_for(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def cdn_download_url(path: str) -> str:
    return f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{path}"

def project_to_pair(obj: Dict[str, Any]) -> Dict[str, str]:
    """Project heterogeneous schema to {prompt, response}."""
    prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
    response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
    return {"prompt": str(prompt), "response": str(response)}

def load_parquet_pairs(path: str) -> List[Dict[str, str]]:
    """Download parquet via CDN and extract pairs."""
    url = cdn_download_url(path)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with open("/tmp/tmp.parquet", "wb") as f:
        f.write(r.content)
    table = pq.read_table("/tmp/tmp.parquet")
    pairs = []
    for i in range(table.num_rows):
        row = table.slice(i, 1).to_pydict()
        flat = {k: v[0] for k, v in row.items()}
        pairs.append(project_to_pair(flat))
    return pairs

def load_jsonl_pairs(path: str) -> List[Dict[str, str]]:
    url = cdn_download_url(path)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    pairs = []
    for line in r.text.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        pairs.append(project_to_pair(obj))
    return pairs

# ── dedup --
from dedup import DedupStore  # expects lib/dedup.py

dedup = DedupStore()

# ── main --
def main() -> None:
    print(f"Shard {SHARD_ID}/{SHARD_TOTAL} | Date {DATE}")
    files = list_date_files(DATE)
    print(f"Found {len(files)} files in date folder")

    # Save manifest once per date
    if not MANIFEST_FILE.exists():
        save_manifest(files)
        print(f"Saved manifest to {MANIFEST_FILE}")

    my_files = [f for f in files if shard_for(slug_from_path(f)) == SHARD_ID]
    print(f"Assigned {len(my_files)} files to this shard")

    written = 0
    with OUT_FILE.open("w", encoding="utf-8") as out_f:
        for path in my_files:
            try:
                if path.endswith(".parquet"):
                    pairs = load_parquet_pairs(path)
                elif path.endswith(".jsonl"):
                    pairs = load_jsonl_pairs(path)
                else:
                    print(f"Skip unsupported {path}", file=sys.stderr)
                    continue

                for pair in pairs:
                    md5 = hashlib.md5(
                        f"{pair['prompt']}\n{p

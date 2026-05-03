# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`)
- Uses **manifest-first strategy**: single API call to `list_repo_tree` for the date folder → save `file_list.json`; workers deterministically shard by `hash(slug) % SHARD_TOTAL`
- Downloads via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header to avoid 429 rate limits
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Dedups via central `lib/dedup.py` md5 store (unchanged)
- Outputs `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl`
- Includes retry/backoff for CDN 429/5xx and commit-cap spreading across sibling repos (hash slug → pick repo)

---

### 1) Create `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.
Usage (GH Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py
  SHARD_ID=0 SHARD_TOTAL=16 DATE_FOLDER=2026-05-03 python bin/dataset-enrich.py
"""

import os
import sys
import json
import time
import hashlib
import datetime
import subprocess
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download, list_repo_tree

# ---- config ----
REPO_ID = os.getenv("HF_DATASET_REPO", "axentx/surrogate-1-training-pairs")
HF_TOKEN = os.getenv("HF_TOKEN")
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE_FOLDER = os.getenv("DATE_FOLDER", datetime.date.today().isoformat())
SIBLING_REPOS = [
    f"axentx/surrogate-1-training-pairs",
    f"axentx/surrogate-1-training-pairs-s1",
    f"axentx/surrogate-1-training-pairs-s2",
    f"axentx/surrogate-1-training-pairs-s3",
    f"axentx/surrogate-1-training-pairs-s4",
]  # 5 siblings => 640 commits/hr aggregate

API = HfApi(token=HF_TOKEN)
# ----

def hf_sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def pick_repo(slug: str) -> str:
    """Deterministic repo selection to spread commit cap."""
    h = int(hf_sha256(slug), 16)
    return SIBLING_REPOS[h % len(SIBLING_REPOS)]

def list_date_files(date_folder: str) -> List[str]:
    """Single API call: list top-level files in date folder (non-recursive)."""
    try:
        tree = list_repo_tree(
            repo_id=REPO_ID,
            path=date_folder,
            recursive=False,
            repo_type="dataset",
        )
    except Exception as e:
        print(f"[error] list_repo_tree failed: {e}", file=sys.stderr)
        raise
    files = [item.rfilename for item in tree if item.type == "file"]
    return files

def save_manifest(files: List[str], out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"date": DATE_FOLDER, "files": files}, f, indent=2)

def load_manifest(manifest_path: Path) -> List[str]:
    if not manifest_path.exists():
        return []
    with open(manifest_path) as f:
        return json.load(f)["files"]

def shard_files(files: List[str]) -> List[str]:
    """Deterministic sharding by slug hash."""
    shard_files = []
    for f in files:
        # Expect <slug>.parquet or <slug>.jsonl; fallback to full path hash
        slug = Path(f).stem
        h = int(hf_sha256(slug), 16)
        if h % SHARD_TOTAL == SHARD_ID:
            shard_files.append(f)
    return shard_files

def cdn_download(repo_id: str, file_path: str, dest: Path, max_retries: int = 5) -> bool:
    """Download via HF CDN (no auth header) to bypass API rate limits."""
    url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{file_path}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=30, stream=True)
            if resp.status_code == 429:
                wait = 360
                print(f"[cdn] 429 rate-limited, waiting {wait}s (attempt {attempt})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception as e:
            wait = 2 ** attempt
            print(f"[cdn] download failed {file_path}: {e}, retry in {wait}s (attempt {attempt})")
            time.sleep(wait)
    return False

def project_to_pair(raw_path: Path) -> List[Dict[str, str]]:
    """
    Project file to {prompt, response} only.
    Supports .jsonl and .parquet (via pyarrow).
    """
    suffix = raw_path.suffix.lower()
    pairs = []

    if suffix == ".jsonl":
        import json as jsonlib
        with open(raw_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = jsonlib.loads(line)
                except Exception:
                    continue
                prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
                response = obj.get("response") or obj.get("output") or obj.get("answer")
                if prompt is not None and response is not None:
                    pairs.append({"prompt": str(prompt), "response": str(response)})
        return pairs

    if suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
            table = pq.read_table(raw_path, columns=["prompt", "response"])
            df = table.to_pandas()
        except Exception:
            # fallback: try common alternative column names
            try:
                table = pq.read_table(raw_path)
                df = table.to_pandas()
            except Exception as e:
                print(f"[warn] cannot read parquet {raw_path}: {e}")
                return []
        # normalize column names
        col_map = {}
        for c in df.columns:
            low = c.lower()
            if "prompt" in low or "input" in low or "question" in low:
                col_map[c] = "prompt"
            elif "response" in low or "output" in low or "answer" in low:
                col_map[c] = "response"
        if "prompt" not in col_map.values() or "response" not in col_map.values():
            # try to pick first text-like pair
            text_cols = [c for c in df.columns if df[c].dtype == "object"]
            if len(text_cols) >= 2:
                col_map[text_cols[0]] = "prompt"
                col_map[text_cols[1]] = "response"
            else:
                return []
        df = df.rename(columns=col_map)
        df = df.dropna(subset=["prompt", "response"])
        for _, row in df.iterrows():
            pairs.append({"prompt": str(row["prompt"]), "response": str(row["response"])})
        return pairs

    return []

def upload_chunk(lines: List[Dict[str, str]], repo_id: str, out_path: str)

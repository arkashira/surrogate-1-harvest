# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list
- Deterministic shard assignment via `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (avoids 429 /api/ limits)
- Projects heterogeneous schemas → `{prompt, response}` only at parse time
- Dedup via central `lib/dedup.py` md5 store
- Outputs `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Reuses existing `requirements.txt` (datasets, huggingface_hub, pyarrow, numpy)

### Steps (est. 90 min)
1. Write `bin/dataset-enrich.py` (60 min) — manifest fetch, CDN download, schema projection, dedup, output
2. Make executable, keep Bash wrapper for cron/env compatibility (10 min)
3. Update `.github/workflows/ingest.yml` to use new script + matrix (10 min)
4. Smoke test locally (10 min)

---

## bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=3 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Behavior:
- list_repo_tree(path=DATE, recursive=False) once
- deterministic shard assignment by hash(slug) % SHARD_TOTAL == SHARD_ID
- downloads via HF CDN (no auth) to bypass /api/ rate limits
- projects heterogeneous files -> {prompt, response}
- dedup via lib/dedup.py (central md5 store)
- outputs batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List

import requests
from huggingface_hub import HfApi, hf_hub_download

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import is_duplicate, mark_seen  # type: ignore

HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
BASE_CDN = f"https://huggingface.co/{HF_REPO}/resolve/main"

# Deterministic shard assignment
def shard_for_slug(slug: str, total: int) -> int:
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()
    return int(digest, 16) % total

def list_date_files(date: str, token: str) -> List[str]:
    """Single API call: list top-level files in DATE folder."""
    api = HfApi(token=token)
    try:
        tree = api.list_repo_tree(
            repo_id=HF_REPO,
            path=date,
            recursive=False,
            repo_type="dataset",
        )
    except Exception as e:
        # If rate-limited, rely on caller to retry after window
        raise RuntimeError(f"list_repo_tree failed for {date}: {e}") from e

    files = []
    for entry in tree:
        if entry.type == "file":
            files.append(entry.path)
    return files

def safe_cdn_download(remote_path: str, dest: Path) -> bool:
    """Download via CDN (no auth). Returns True on success."""
    url = f"{BASE_CDN}/{remote_path}"
    try:
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as exc:
        print(f"[WARN] CDN download failed {remote_path}: {exc}", file=sys.stderr)
        return False

def project_to_pair(local_path: Path) -> Optional[Dict[str, str]]:
    """
    Project heterogeneous file -> {prompt, response}.
    Supports:
      - JSON/JSONL with prompt/response fields (case-insensitive)
      - Parquet via pyarrow (project only prompt/response cols)
    """
    suffix = local_path.suffix.lower()
    try:
        if suffix == ".jsonl":
            with open(local_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
                    response = obj.get("response") or obj.get("output") or obj.get("answer")
                    if isinstance(prompt, str) and isinstance(response, str):
                        return {"prompt": prompt.strip(), "response": response.strip()}
            return None

        if suffix == ".json":
            with open(local_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
                if isinstance(obj, list):
                    for item in obj:
                        prompt = item.get("prompt") or item.get("input") or item.get("question")
                        response = item.get("response") or item.get("output") or item.get("answer")
                        if isinstance(prompt, str) and isinstance(response, str):
                            return {"prompt": prompt.strip(), "response": response.strip()}
                else:
                    prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
                    response = obj.get("response") or obj.get("output") or obj.get("answer")
                    if isinstance(prompt, str) and isinstance(response, str):
                        return {"prompt": prompt.strip(), "response": response.strip()}
            return None

        if suffix in (".parquet", ".pq"):
            import pyarrow.parquet as pq
            tbl = pq.read_table(local_path, columns=["prompt", "response"], use_threads=False)
            df = tbl.to_pandas()
            for _, row in df.iterrows():
                prompt, response = str(row.get("prompt", "")), str(row.get("response", ""))
                if prompt and response:
                    return {"prompt": prompt.strip(), "response": response.strip()}
            return None

    except Exception as exc:
        print(f"[WARN] projection failed {local_path}: {exc}", file=sys.stderr)
    return None

def build_md5_for_dedup(pair: Dict[str, str]) -> str:
    content = f"{pair['prompt']}\n{pair['response']}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()

def main() -> None:
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    date = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    hf_token = os.getenv("HF_TOKEN", "")

    if not hf_token:
        print("[ERROR] HF_TOKEN required", file=sys.stderr)
        sys.exit(1)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path("batches/public-merged") / date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shard{shard_id}-{ts}.jsonl"

    print(f"[INFO] Shard {shard_id}/{shard_total} | DATE={date} | out={out_path}")

    # 1) Manifest: list files once
    try:
        files = list_date_files(date, hf_token)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    if not files:
        print("[WARN] No files found for date", file=sys.stderr)
        sys.exit(0)

    # Deterministic shard assignment by slug (filename without extension)
    my_files = []

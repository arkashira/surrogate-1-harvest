# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID` / `SHARD_TOTAL` from the matrix workflow  
- Loads a pre-generated `manifest-YYYYMMDD.json` (created once per run by the workflow) containing the deterministic slice of file paths to process  
- Downloads each file via **HF CDN** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero API/auth calls during ingestion, bypassing 429 limits  
- Projects heterogeneous schemas to `{prompt, response}` only at parse time (avoids pyarrow CastError)  
- Deduplicates via the existing `lib/dedup.py` md5 store  
- Writes `batches/public-merged/<date>/shard<N>-<HHMMSS>.jsonl` with slug-derived deterministic repo assignment (hash-slug → 1 of 5 sibling repos) to respect HF commit caps  
- Exits with success/failure codes for GitHub Actions matrix

### Steps (≤2h)

1. **Create `bin/dataset-enrich.py`** (main worker) — 60 min  
2. **Add `bin/gen-manifest.py`** (workflow helper) — 20 min  
3. **Update `.github/workflows/ingest.yml`** to generate manifest once and pass to matrix — 15 min  
4. **Tidy/remove old `dataset-enrich.sh`** and update `requirements.txt` — 15 min  
5. **Smoke test** locally with a small manifest — 10 min

---

## 1) `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 \
  MANIFEST=manifest-20260503.json \
  python bin/dataset-enrich.py

Environment:
  HF_TOKEN         - write token for axentx/surrogate-1-training-pairs
  DATASET_REPO     - default: axentx/surrogate-1-training-pairs
  SIBLING_REPOS    - comma-separated list of repos for commit-cap spreading
"""
import os
import sys
import json
import hashlib
import datetime as dt
from pathlib import Path
from typing import Dict, Any, List
from collections import defaultdict

import requests
from huggingface_hub import HfApi, hf_hub_download

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # type: ignore

HF_API = HfApi()
DATASET_REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
SIBLING_REPOS = [r.strip() for r in os.getenv("SIBLING_REPOS", DATASET_REPO).split(",") if r.strip()]
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

def deterministic_shard(path: str, total: int) -> int:
    return int(hashlib.sha256(path.encode()).hexdigest(), 16) % total

def pick_sibling_repo(slug: str) -> str:
    """Spread writes across sibling repos using hash-slug."""
    idx = int(hashlib.sha256(slug.encode()).hexdigest(), 16) % len(SIBLING_REPOS)
    return SIBLING_REPOS[idx]

def parse_pair(raw: Dict[str, Any]) -> Dict[str, str]:
    """Project heterogeneous HF dataset files to {prompt, response}."""
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or ""
    response = raw.get("response") or raw.get("output") or raw.get("answer") or raw.get("completion") or ""
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def download_cdn(repo: str, path: str) -> bytes:
    url = CDN_TEMPLATE.format(repo=repo, path=path)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def load_parquet_via_hf(path_in_repo: str) -> List[Dict[str, Any]]:
    """Fallback: use hf_hub_download for parquet when CDN fails or for schema inspection."""
    local_path = hf_hub_download(repo_id=DATASET_REPO, filename=path_in_repo, repo_type="dataset")
    import pyarrow.parquet as pq
    table = pq.read_table(local_path, columns=["prompt", "response"])
    return table.to_pylist()

def process_file(path: str, dedup: DedupStore) -> List[Dict[str, Any]]:
    """Download, parse, dedup; return accepted pairs."""
    out: List[Dict[str, Any]] = []
    try:
        raw_bytes = download_cdn(DATASET_REPO, path)
    except Exception:
        # fallback for non-raw files (parquet)
        try:
            rows = load_parquet_via_hf(path)
        except Exception as e:
            print(f"[WARN] cannot process {path}: {e}", file=sys.stderr)
            return out

        for row in rows:
            pair = parse_pair(row)
            if not pair["prompt"] or not pair["response"]:
                continue
            slug = pair.get("slug") or pair.get("id") or f"{path}:{hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()}"
            md5 = hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()
            if dedup.seen(md5):
                continue
            dedup.add(md5)
            out.append({"prompt": pair["prompt"], "response": pair["response"], "slug": slug})
        return out

    # Assume JSONL lines (common in surrogate-1 public merges)
    for line in raw_bytes.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except Exception:
            continue
        pair = parse_pair(raw)
        if not pair["prompt"] or not pair["response"]:
            continue
        slug = raw.get("slug") or raw.get("id") or f"{path}:{hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()}"
        md5 = hashlib.md5(json.dumps(pair, sort_keys=True).encode()).hexdigest()
        if dedup.seen(md5):
            continue
        dedup.add(md5)
        out.append({"prompt": pair["prompt"], "response": pair["response"], "slug": slug})
    return out

def main() -> None:
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    manifest_path = os.getenv("MANIFEST")
    if not manifest_path or not Path(manifest_path).exists():
        print(f"[ERROR] manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)
    all_files: List[str] = manifest.get("files", [])
    if not all_files:
        print("[INFO] no files in manifest", file=sys.stderr)
        sys.exit(0)

    my_files = [p for p in all_files if deterministic_shard(p, shard_total) == shard_id]
    print(f"[INFO] shard {shard_id}/{shard_total} -> {len(my_files)} files")

    dedup = DedupStore()
    today = dt.datetime.utcnow().strftime("%Y%m%d")
    ts = dt.datetime.utcnow().strftime("%H%M%S")
    batch_dir = f"batches/public-merged/{today}"
    out_lines: List[str] = []

    for path in my_files:
        pairs = process_file(path, dedup)
        for p in pairs:
            out_lines.append(json.dumps(p, ensure_ascii=False))

    if not out_lines:
        print("[INFO] no new pairs", file=sys.stderr)
        sys.exit(0)

    # Group by sibling repo and upload
    by_repo: Dict[str, List[str]] = defaultdict(list)
    for line in out_lines:
        obj = json.loads(line)
       

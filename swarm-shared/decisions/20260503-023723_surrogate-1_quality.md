# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`, `REPO_ID` (default: `axentx/surrogate-1-training-pairs`)
- Pre-lists target date folder once via `list_repo_tree(recursive=False)` → saves `file-list.json`
- Deterministically assigns files to shards by `hash(slug) % SHARD_TOTAL`
- Downloads assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with zero Authorization header during data fetch
- Streams, normalizes per-schema, projects to `{prompt, response}`, dedups via central `lib/dedup.py` md5 store
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with no extra metadata columns
- Exits non-zero on hard failures; logs summary for GitHub Actions matrix

### Changes

1. `bin/dataset-enrich.py` — new worker (replaces shell script)
2. `bin/dataset-enrich.sh` — thin wrapper for backward compat (calls python)
3. `.github/workflows/ingest.yml` — pass `DATE`, `SHARD_ID`, `SHARD_TOTAL`; use CDN-friendly env
4. `requirements.txt` — ensure `requests`, `tqdm`, `python-dotenv` (optional)

---

## Code Snippets

### 1. `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-05-03 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py --repo axentx/surrogate-1-training-pairs

Environment:
  SHARD_ID          (required) 0..15
  SHARD_TOTAL       (default 16)
  DATE              (required) YYYY-MM-DD
  HF_TOKEN          (required for listing; optional for CDN downloads)
  REPO_ID           (default axentx/surrogate-1-training-pairs)
  OUTPUT_DIR        (default batches/public-merged)
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # noqa: E402

API = HfApi()
CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
RETRY_WAIT = 360  # seconds after 429

def _hash_slug(slug: str) -> int:
    return int(hashlib.md5(slug.encode()).hexdigest(), 16)

def list_date_files(repo_id: str, date: str, token: str) -> List[str]:
    """List files under {date}/ (non-recursive). Returns relative paths."""
    try:
        tree = API.list_repo_tree(repo_id=repo_id, path=date, recursive=False, token=token)
        return [item.rfilename for item in tree if item.rfilename]
    except Exception as e:
        print(f"[ERROR] list_repo_tree failed: {e}", file=sys.stderr)
        raise

def assign_to_shard(paths: List[str], shard_id: int, shard_total: int) -> List[str]:
    assigned = []
    for p in paths:
        # slug = filename without extension for sharding
        slug = Path(p).stem
        if _hash_slug(slug) % shard_total == shard_id:
            assigned.append(p)
    return sorted(assigned)

def cdn_get_lines(url: str, max_retries: int = 3) -> List[Dict[str, Any]]:
    """Download JSONL via CDN and yield parsed lines."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 429:
                wait = RETRY_WAIT
                print(f"[WARN] CDN 429, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            out = []
            for line in resp.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return out
        except Exception as e:
            if attempt == max_retries:
                print(f"[ERROR] CDN download failed {url}: {e}", file=sys.stderr)
                raise
            time.sleep(5 * attempt)
    return []

def normalize_record(rec: Dict[str, Any]) -> Dict[str, str]:
    """Project heterogeneous schemas to {prompt, response}."""
    prompt = rec.get("prompt") or rec.get("input") or rec.get("question") or ""
    response = rec.get("response") or rec.get("output") or rec.get("answer") or rec.get("completion") or ""
    # Ensure strings
    return {"prompt": str(prompt).strip(), "response": str(response).strip()}

def build_output_path(date: str, shard_id: int, output_dir: Path) -> Path:
    ts = datetime.utcnow().strftime("%H%M%S")
    out_dir = output_dir / "public-merged" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"shard{shard_id}-{ts}.jsonl"

def main() -> int:
    shard_id = int(os.environ.get("SHARD_ID", -1))
    shard_total = int(os.environ.get("SHARD_TOTAL", "16"))
    date = os.environ.get("DATE", "")
    token = os.environ.get("HF_TOKEN", "")
    repo_id = os.environ.get("REPO_ID", "axentx/surrogate-1-training-pairs")
    output_dir = Path(os.environ.get("OUTPUT_DIR", "batches/public-merged"))

    if shard_id < 0 or not date:
        print("[ERROR] Set SHARD_ID (>=0) and DATE (YYYY-MM-DD)", file=sys.stderr)
        return 1

    print(f"[INFO] Shard {shard_id}/{shard_total} | Date {date} | Repo {repo_id}")

    # 1) List once
    try:
        files = list_date_files(repo_id, date, token)
    except Exception:
        return 1

    assigned = assign_to_shard(files, shard_id, shard_total)
    print(f"[INFO] Assigned {len(assigned)} files out of {len(files)}")

    # Save manifest for reproducibility
    manifest_path = Path("file-list.json")
    manifest_path.write_text(json.dumps({"date": date, "assigned": assigned}, indent=2))
    print(f"[INFO] Manifest saved to {manifest_path}")

    # 2) Dedup store
    dedup = DedupStore()

    # 3) Process assigned files via CDN
    out_path = build_output_path(date, shard_id, output_dir)
    written = 0
    skipped_dup = 0

    for rel_path in assigned:
        cdn_url = CDN_TEMPLATE.format(repo=repo_id, path=rel_path)
        try:
            records = cdn_get_lines(cdn_url)
        except Exception:
            # If CDN fails, fallback to hf_hub_download (authenticated)
            try:
                local_path = hf_hub_download(repo_id=repo_id, filename=rel_path, token=token or None)
                with open(local_path, "r", encoding="utf-8") as f:
                    records = [json.loads(l) for l in f if l.strip()]
            except Exception as e:
                print(f"[WARN] Failed to fetch {rel_path}: {e}", file=sys.stderr)
                continue

        for rec in records:
            norm = normalize_record(rec)
            if not norm["prompt"] or not norm["response"]:
                continue

            # Central dedup by content hash
            payload = f"{norm['prompt']}\n{norm['response']}"
            if

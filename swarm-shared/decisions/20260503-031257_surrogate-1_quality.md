# surrogate-1 / quality

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` via env
- Single `list_repo_tree` call per date folder → deterministic file list saved to `manifest-{DATE}.json`
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads only assigned files via **HF CDN bypass** (`resolve/main/...` — no Authorization header, avoids 429 API limits)
- Projects heterogeneous schemas to `{prompt, response}` at parse time (avoids pyarrow CastError)
- Deduplicates via central `lib/dedup.py` md5 store
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl` with slug-derived attribution in filename only (no `source`/`ts` cols)
- Exits 0 on success, non-zero on fatal error (GitHub Actions will retry)

---

### Code changes

#### 1) `bin/dataset-enrich.py` (new)

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1-training-pairs.

Env:
  SHARD_ID        (int, 0..15)
  SHARD_TOTAL=16  (int)
  DATE            (YYYY-MM-DD)
  HF_TOKEN        (write token for axentx/surrogate-1-training-pairs)
  REPO_ID=axentx/surrogate-1-training-pairs
  OUTPUT_DIR=batches/public-merged
"""
import json
import os
import sys
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

import requests
from huggingface_hub import HfApi, hf_hub_download

REPO_ID = os.getenv("REPO_ID", "axentx/surrogate-1-training-pairs")
SHARD_ID = int(os.getenv("SHARD_ID", 0))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", 16))
DATE = os.getenv("DATE")
HF_TOKEN = os.getenv("HF_TOKEN")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "batches/public-merged"))

if not DATE:
    print("ERROR: DATE (YYYY-MM-DD) is required", file=sys.stderr)
    sys.exit(1)

if not HF_TOKEN:
    print("ERROR: HF_TOKEN is required", file=sys.stderr)
    sys.exit(1)

API = HfApi(token=HF_TOKEN)
SESSION = requests.Session()
# CDN bypass: no Authorization header for resolve/main/ downloads
CDN_SESSION = requests.Session()

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import DedupStore  # noqa: E402

DEDUP = DedupStore()

def list_date_files(date_folder: str) -> List[str]:
    """Single API call to list files in date folder (non-recursive)."""
    try:
        tree = API.list_repo_tree(repo_id=REPO_ID, path=date_folder, recursive=False)
    except Exception as exc:
        print(f"ERROR: list_repo_tree failed for {date_folder}: {exc}", file=sys.stderr)
        raise
    # tree items are dicts with 'path'
    files = [item["path"] for item in tree if item.get("type") == "file"]
    return sorted(files)

def belongs_to_shard(slug: str) -> bool:
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return (h % SHARD_TOTAL) == SHARD_ID

def cdn_download(repo_id: str, file_path: str) -> bytes:
    """Download via HF CDN (resolve/main) — no auth, bypasses API rate limits."""
    url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{file_path}"
    resp = CDN_SESSION.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content

def parse_parquet_to_pairs(content: bytes) -> List[Dict[str, str]]:
    """Project heterogeneous parquet to {prompt, response} pairs."""
    import pyarrow.parquet as pq
    import io
    table = pq.read_table(io.BytesIO(content))
    df = table.to_pandas()
    pairs = []
    for _, row in df.iterrows():
        prompt = row.get("prompt") or row.get("input") or row.get("question") or ""
        response = row.get("response") or row.get("output") or row.get("answer") or ""
        if not prompt or not response:
            continue
        pairs.append({"prompt": str(prompt).strip(), "response": str(response).strip()})
    return pairs

def parse_jsonl_to_pairs(content: bytes) -> List[Dict[str, str]]:
    import io
    pairs = []
    for line in io.BytesIO(content):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question") or ""
        response = obj.get("response") or obj.get("output") or obj.get("answer") or ""
        if not prompt or not response:
            continue
        pairs.append({"prompt": str(prompt).strip(), "response": str(response).strip()})
    return pairs

def process_file(file_path: str) -> List[Dict[str, str]]:
    content = cdn_download(REPO_ID, file_path)
    if file_path.endswith(".parquet"):
        return parse_parquet_to_pairs(content)
    if file_path.endswith(".jsonl"):
        return parse_jsonl_to_pairs(content)
    # fallback: try parquet, then jsonl
    try:
        return parse_parquet_to_pairs(content)
    except Exception:
        pass
    try:
        return parse_jsonl_to_pairs(content)
    except Exception:
        print(f"WARN: unsupported file {file_path}, skipping", file=sys.stderr)
        return []

def main() -> None:
    date_folder = f"raw/{DATE}"
    print(f"INFO: listing files in {date_folder} (shard {SHARD_ID}/{SHARD_TOTAL})")
    try:
        files = list_date_files(date_folder)
    except Exception:
        sys.exit(1)

    if not files:
        print("INFO: no files found")
        return

    # Save manifest for reproducibility / training script embedding
    manifest_path = Path(f"manifest-{DATE}.json")
    manifest_path.write_text(json.dumps({"date": DATE, "files": files}, indent=2))
    print(f"INFO: manifest saved to {manifest_path}")

    assigned = [f for f in files if belongs_to_shard(f)]
    print(f"INFO: processing {len(assigned)} assigned files")

    out_dir = OUTPUT_DIR / DATE
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    out_path = out_dir / f"shard{SHARD_ID}-{ts}.jsonl"

    written = 0
    skipped_dup = 0
    errors = 0

    with out_path.open("w", encoding="utf-8") as fout:
        for file_path in assigned:
            slug = file_path.rsplit(".", 1)[0].replace("/", "-")
            try:
                pairs = process_file(file_path)
            except Exception as exc:
                print(f"ERROR: failed to process {file_path}: {exc}", file=sys.stderr)
                errors += 1
                continue

            for pair in pairs:
                md5 = hashlib.md5(f"{pair['prompt']}\n{pair['response']}".encode()).hexdigest()
                if DEDUP.exists(md5):
                    skipped_dup += 1
                    continue
                DEDUP.add(md5)
                rec = {"prompt": pair["prompt"], "response": pair["response"]}
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written += 1

    print(f"INFO: written={written} skipped_dup={skipped_dup} errors={errors} out={out_path}")
    if written == 0 and errors > 0:
        sys.exit(1)

if __name

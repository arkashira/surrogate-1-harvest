# surrogate-1 / quality

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list saved to `manifest-{DATE}.json`
- Deterministic shard assignment: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads only assigned files via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) — zero API calls during streaming
- Projects each file to `{prompt, response}` at parse time (avoids pyarrow CastError on mixed schemas)
- Deduplicates via central md5 store (`lib/dedup.py`)
- Writes `batches/public-merged/{DATE}/shard{N}-{HHMMSS}.jsonl`
- Exits 0 on success, non-zero on hard failure (GitHub Actions will retry)

### Why this fixes the known patterns
- ✅ **HF API rate-limit 429**: single tree call per shard, then CDN-only fetches (no `/api/` auth checks)
- ✅ **pyarrow CastError**: never `load_dataset(streaming=True)` on heterogeneous repo; project at parse time
- ✅ **Schema hygiene**: output only `{prompt, response}`; no extra cols that break downstream training
- ✅ **Deterministic sharding**: hash-slug → shard prevents cross-run collisions and enables reproducible retries

---

## Code Changes

### 1) New worker: `bin/dataset-enrich.py`
```python
#!/usr/bin/env python3
"""
surrogate-1 CDN-bypass ingestion worker.

Environment:
  SHARD_ID      (int) 0..15
  SHARD_TOTAL   (int) default 16
  DATE          (str) YYYY-MM-DD folder under dataset repo
  HF_TOKEN      (str) write token for axentx/surrogate-1-training-pairs
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Dict, Any

import requests
from huggingface_hub import HfApi, hf_hub_download

REPO_ID = "axentx/surrogate-1-training-pairs"
API = HfApi()

def _hash_slug(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16)

def list_date_files(date_folder: str) -> list[str]:
    """Single tree call; returns filenames (not recursive)."""
    items = API.list_repo_tree(repo_id=REPO_ID, path=date_folder, repo_type="dataset")
    return [item.rfilename for item in items if item.type == "file"]

def cdn_url(repo_id: str, filename: str) -> str:
    return f"https://huggingface.co/datasets/{repo_id}/resolve/main/{filename}"

def stream_file_cdn(filename: str) -> Iterator[Dict[str, Any]]:
    """
    Download via CDN and parse line-by-line JSONL.
    Project to {prompt, response} only.
    """
    url = cdn_url(REPO_ID, filename)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            prompt = obj.get("prompt") or obj.get("input") or obj.get("text")
            response = obj.get("response") or obj.get("output")
            if prompt is None or response is None:
                continue
            yield {"prompt": str(prompt).strip(), "response": str(response).strip()}

def slug_from_filename(filename: str) -> str:
    # e.g. "2026-05-03/some-slug.jsonl" -> "some-slug"
    return Path(filename).stem

def load_dedup_db() -> Any:
    from lib.dedup import DedupStore  # type: ignore
    return DedupStore()

def main() -> int:
    shard_id = int(os.getenv("SHARD_ID", "0"))
    shard_total = int(os.getenv("SHARD_TOTAL", "16"))
    date_folder = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    hf_token = os.getenv("HF_TOKEN")
    if not hf_token:
        print("HF_TOKEN required", file=sys.stderr)
        return 1

    os.environ["HUGGING_FACE_HUB_TOKEN"] = hf_token

    print(f"[{shard_id}] listing {date_folder} ...")
    try:
        files = list_date_files(date_folder)
    except Exception as e:
        print(f"[{shard_id}] list_repo_tree failed: {e}", file=sys.stderr)
        return 1

    # Deterministic shard assignment
    assigned = [f for f in files if _hash_slug(slug_from_filename(f)) % shard_total == shard_id]
    print(f"[{shard_id}] assigned {len(assigned)} files out of {len(files)}")

    dedup = load_dedup_db()
    ts = datetime.now(timezone.utc).strftime("%H%M%S")
    out_dir = Path(f"batches/public-merged/{date_folder}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shard{shard_id}-{ts}.jsonl"

    written = 0
    skipped_dup = 0
    with out_path.open("w", encoding="utf-8") as fout:
        for filename in assigned:
            slug = slug_from_filename(filename)
            try:
                for item in stream_file_cdn(filename):
                    # Create deterministic content hash for dedup
                    content_key = f"{slug}:{item['prompt'][:120]}:{item['response'][:120]}"
                    md5 = hashlib.md5(content_key.encode()).hexdigest()
                    if dedup.exists(md5):
                        skipped_dup += 1
                        continue
                    dedup.add(md5)
                    fout.write(json.dumps(item, ensure_ascii=False) + "\n")
                    written += 1
            except Exception as e:
                print(f"[{shard_id}] error processing {filename}: {e}", file=sys.stderr)
                # continue with next file (don't fail entire shard for one bad file)

    print(f"[{shard_id}] written={written} skipped_dup={skipped_dup} -> {out_path}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

### 2) Make it executable
```bash
chmod +x bin/dataset-enrich.py
```

### 3) Update GitHub Actions to use Python worker
`.github/workflows/ingest.yml` (minimal change — replace script call):
```yaml
# ... existing matrix etc ...
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: |
          python bin/dataset-enrich.py
        env:
          SHARD_ID: ${{ matrix.shard_id }}
          SHARD_TOTAL: 16
          DATE: ${{ env.DATE }}
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
```

### 4) Keep Bash wrapper for HF Space compatibility (optional)
If the HF Space still expects `bin/dataset-enrich.sh`, create a thin wrapper:
```bash
#!/usr/bin/env bash
set -euo pipefail
exec python "$(dirname "$0")/dataset-enrich.py" "$@"
```
Then `chmod +x bin/dataset-enrich.sh`.

---

## Verification (quick)
```bash
DATE=2026-05-03 SHARD_ID=0 SHARD_TOTAL=16 HF_TOKEN=hf_xxx python bin/dataset-enrich.py
# Expect: list files → assign shard → CDN downloads → shard0-*.jsonl with {prompt,response}
```

## Rollback
If anything breaks, revert workflow to previous `bash bin/dataset-enrich.sh` (keep old script if still present). The new Python worker is isolated and idempotent.

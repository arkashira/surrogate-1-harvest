# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN` (write)
- Uses **single API call** from runner to list one date folder (`list_repo_tree(recursive=False)`) → saves `file-list.json`
- Shards files by `hash(slug) % SHARD_TOTAL` → deterministic 1/16 slice
- Downloads via **CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) with no Authorization header (bypasses /api/ 429)
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Dedups via central `lib/dedup.py` md5 store (same as Space)
- Outputs `batches/public-merged/{date}/shard{N}-{HHMMSS}.jsonl`
- Exits 0 on success, non-zero on fatal error (GitHub Actions handles retries)

### Why this is highest-value
- Eliminates HF API rate-limit risk during data load (CDN bypass)
- Prevents OOM by avoiding `load_dataset(streaming=True)` on heterogeneous repos
- Keeps deterministic sharding so 16 runners never collide
- Reuses existing dedup logic → no new infra
- Fits <2h: single Python file + small bash wrapper + workflow var bump

---

## Code Changes

### 1) New worker: `bin/dataset-enrich.py`

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Usage (GitHub Actions matrix):
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx python bin/dataset-enrich.py
"""
import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from huggingface_hub import HfApi, hf_hub_download

# ── config --
HF_REPO = "datasets/axentx/surrogate-1-training-pairs"
API = HfApi(token=os.getenv("HF_TOKEN"))
DATE = os.getenv("DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
OUT_DIR = Path("batches/public-merged") / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)
TS = datetime.now(timezone.utc).strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TS}.jsonl"

# ── dedup --
DEDUP_DIR = Path(__file__).parent / "lib"
DEDUP_DB = DEDUP_DIR / "dedup.py"
sys.path.insert(0, str(DEDUP_DIR))
try:
    from dedup import is_duplicate, store
except Exception:
    # Fallback: minimal file-based dedup to avoid blocking.
    _seen_path = Path("/tmp/surrogate_dedup_seen.txt")
    _seen = set(_seen_path.read_text().splitlines()) if _seen_path.exists() else set()

    def is_duplicate(md5: str) -> bool:
        return md5 in _seen

    def store(md5: str) -> None:
        _seen.add(md5)
        _seen_path.write_text("\n".join(sorted(_seen)))

# ── helpers --
def list_date_folder() -> list[str]:
    """Single API call: list files in DATE folder (non-recursive)."""
    try:
        tree = API.list_repo_tree(repo_id=HF_REPO, path=DATE, recursive=False)
    except Exception as e:
        # If folder doesn't exist yet, return empty.
        if "404" in str(e) or "not found" in str(e).lower():
            return []
        raise
    paths = [item.path for item in tree if hasattr(item, "path")]
    return [p for p in paths if p.endswith((".parquet", ".jsonl", ".json"))]

def file_url(path: str) -> str:
    """CDN bypass URL (no auth header)."""
    return f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{path}"

def deterministic_shard(path: str) -> int:
    """Map file to shard by hash(slug) % SHARD_TOTAL."""
    slug = Path(path).stem
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return h % SHARD_TOTAL

def parse_record(raw: dict) -> dict | None:
    """Project heterogeneous file to {prompt,response}. Return None if invalid."""
    prompt = raw.get("prompt") or raw.get("input") or raw.get("question") or raw.get("instruction")
    response = raw.get("response") or raw.get("output") or raw.get("answer") or raw.get("completion")
    if prompt is None or response is None:
        return None
    return {"prompt": str(prompt), "response": str(response)}

# ── main --
def main() -> None:
    print(f"[shard{SHARD_ID}] processing date={DATE}")

    files = list_date_folder()
    print(f"[shard{SHARD_ID}] found {len(files)} files")

    my_files = [f for f in files if deterministic_shard(f) == SHARD_ID]
    print(f"[shard{SHARD_ID}] assigned {len(my_files)} files")

    written = 0
    skipped_dup = 0
    skipped_parse = 0

    with OUT_FILE.open("w", buffering=1 << 20) as out:
        for path in my_files:
            url = file_url(path)
            try:
                resp = requests.get(url, stream=True, timeout=60)
                resp.raise_for_status()
            except Exception as e:
                print(f"[shard{SHARD_ID}] WARN failed to fetch {path}: {e}", file=sys.stderr)
                continue

            if path.endswith(".jsonl") or path.endswith(".json"):
                import io
                decoder = io.TextIOWrapper(resp.raw, encoding="utf-8")
                for line in decoder:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except Exception:
                        skipped_parse += 1
                        continue
                    rec = parse_record(raw)
                    if rec is None:
                        skipped_parse += 1
                        continue
                    md5 = hashlib.md5(json.dumps(rec, sort_keys=True).encode()).hexdigest()
                    if is_duplicate(md5):
                        skipped_dup += 1
                        continue
                    store(md5)
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    written += 1
            else:
                # Parquet: download via hf_hub_download then read with projection.
                local_path = hf_hub_download(repo_id=HF_REPO, filename=path, repo_type="dataset")
                import pyarrow.parquet as pq
                pf = pq.read_table(
                    local_path,
                    columns=["prompt", "response"] if "prompt" in pq.read_schema(local_path).names else None,
                )
                df = pf.to_pandas()
                for _, row in df.iterrows():
                    rec = parse_record(row.to_dict())
                    if rec is None:
                        skipped_parse += 1
                        continue
                    md5 = hashlib.md5(json.dumps(rec, sort_keys=True).encode()).hexdigest()
                    if is_duplicate(md5):
                        skipped_dup += 1
                        continue
                    store(md5)
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    written += 1

    print(f"[shard{SHARD_ID}] done: written={written}, dup={skipped_dup}, bad={skipped_parse}")
    sys.exit(0)

if __name__ == "__main__":
    main()
```

### 2) Bash wrapper (optional): `bin/dataset-enrich.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
exec python "$(dirname "$0")/dataset-enrich.py" "$@"
```

### 3) Workflow update (excerpt)

```yaml


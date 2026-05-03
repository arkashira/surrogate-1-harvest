# surrogate-1 / discovery

## Final Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16`, `DATE`, `HF_TOKEN`
- Single `list_repo_tree(path=DATE, recursive=False)` → deterministic file list for the date folder
- Shard assignment by deterministic hash: `hash(slug) % SHARD_TOTAL == SHARD_ID`
- Downloads via **HF CDN bypass** (`https://huggingface.co/datasets/.../resolve/main/...`) — no Authorization header, avoids API 429 during training
- Projects each file to `{prompt, response}` only at parse time (avoids pyarrow CastError on mixed schemas)
- Dedup via central md5 store (`lib/dedup.py`)
- Outputs: `batches/public-merged/{DATE}/shard{SHARD_ID}-{HHMMSS}.jsonl`
- Writes a companion manifest: `batches/public-merged/{DATE}/shard{SHARD_ID}-{HHMMSS}.jsonl.manifest` containing the file list used (so Lightning training can do CDN-only fetches with zero API calls)

### Steps (≤2h)

1. Create `bin/dataset-enrich.py` (replaces shell script)
2. Keep `lib/dedup.py` unchanged (central md5 store)
3. Update `.github/workflows/ingest.yml` to invoke via `python bin/dataset-enrich.py` and pass matrix `shard_id`, `date`, `hf_token`
4. Add `requirements.txt` entries if missing (`requests`, `tqdm`)
5. Smoke-test locally with a small date folder

---

## bin/dataset-enrich.py

```python
#!/usr/bin/env python3
"""
Deterministic CDN-bypass ingestion worker for surrogate-1.

Usage:
  SHARD_ID=0 SHARD_TOTAL=16 DATE=2026-04-29 \
  HF_TOKEN=hf_xxx \
  python bin/dataset-enrich.py

Environment:
  SHARD_ID          - integer 0..15
  SHARD_TOTAL       - total shards (default 16)
  DATE              - date folder in dataset (e.g. 2026-04-29)
  HF_TOKEN          - HuggingFace write token
  DATASET_REPO      - default axentx/surrogate-1-training-pairs
  HF_ENDPOINT       - optional custom endpoint
"""

import os
import sys
import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from huggingface_hub import HfApi

# ── config ──────────────────────────────────────────────────────────────

REPO = os.getenv("DATASET_REPO", "axentx/surrogate-1-training-pairs")
API = HfApi(token=os.getenv("HF_TOKEN"))
HF_TOKEN = os.getenv("HF_TOKEN")
CDN_ROOT = f"https://huggingface.co/datasets/{REPO}/resolve/main"

SHARD_ID = int(os.getenv("SHARD_ID", "0"))
SHARD_TOTAL = int(os.getenv("SHARD_TOTAL", "16"))
DATE = os.getenv("DATE")
if not DATE:
    print("ERROR: DATE is required", file=sys.stderr)
    sys.exit(1)

OUT_DIR = Path("batches/public-merged") / DATE
OUT_DIR.mkdir(parents=True, exist_ok=True)

TS = datetime.now(timezone.utc).strftime("%H%M%S")
OUT_FILE = OUT_DIR / f"shard{SHARD_ID}-{TS}.jsonl"
MANIFEST_FILE = OUT_FILE.with_suffix(".jsonl.manifest")

# ── dedup store ─────────────────────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.dedup import is_duplicate, add_hash  # noqa: E402

# ── helpers ─────────────────────────────────────────────────────────────

def deterministic_shard(slug: str) -> int:
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % SHARD_TOTAL

def list_date_files(date_folder: str):
    """Single API call: list top-level files in date folder."""
    try:
        tree = API.list_repo_tree(
            repo_id=REPO,
            path=date_folder,
            repo_type="dataset",
            recursive=False,
        )
    except Exception as exc:
        print(f"ERROR listing repo tree: {exc}", file=sys.stderr)
        sys.exit(1)

    files = [entry.path for entry in tree if entry.type == "file"]
    return sorted(files)

def cdn_url(path: str) -> str:
    return f"{CDN_ROOT}/{path}"

def parse_to_pair(raw_bytes: bytes, filename: str):
    """
    Project heterogeneous file to {prompt, response} only.
    Supports:
      - JSONL lines with 'prompt'/'response' (or 'instruction'/'output')
      - JSON objects
      - Parquet via pyarrow is avoided upstream; here we expect bytes
        from raw files (if parquet, caller should project before calling).
    """
    # Try JSONL lines first
    text = raw_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        return None

    # If single JSON object
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            prompt = obj.get("prompt") or obj.get("instruction") or obj.get("input") or ""
            response = obj.get("response") or obj.get("output") or ""
            if prompt or response:
                return {"prompt": prompt, "response": response}
        except Exception:
            pass

    # Try line-by-line JSONL
    lines = text.splitlines()
    pairs = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            prompt = obj.get("prompt") or obj.get("instruction") or obj.get("input") or ""
            response = obj.get("response") or obj.get("output") or ""
            if prompt or response:
                pairs.append({"prompt": prompt, "response": response})
        except Exception:
            continue

    if pairs:
        # Return first pair for per-file ingestion; caller can extend to multiple.
        return pairs[0]

    # Fallback: treat entire content as response with empty prompt
    return {"prompt": "", "response": text[:4000]}

# ── main ────────────────────────────────────────────────────────────────

def main():
    print(f"Starting shard {SHARD_ID}/{SHARD_TOTAL} for {DATE}")

    files = list_date_files(DATE)
    print(f"Found {len(files)} files in {DATE}")

    my_files = [f for f in files if deterministic_shard(f) == SHARD_ID]
    print(f"Shard owns {len(my_files)} files")

    manifest = {
        "shard_id": SHARD_ID,
        "shard_total": SHARD_TOTAL,
        "date": DATE,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": my_files,
    }

    with MANIFEST_FILE.open("w") as mf:
        json.dump(manifest, mf, indent=2)

    headers = {}
    # CDN public files do not require Authorization; omit to bypass API rate limits.
    # If repo is private, include token:
    if HF_TOKEN:
        headers = {"Authorization": f"Bearer {HF_TOKEN}"}

    written = 0
    skipped_dup = 0
    errors = 0

    with OUT_FILE.open("w") as out:
        for path in my_files:
            try:
                url = cdn_url(path)
                resp = requests.get(url, headers=headers or None, timeout=30)
                resp.raise_for_status()
                raw = resp.content

                pair = parse_to_pair(raw, path)
                if not pair:
                    continue

                content = json.dumps(pair, ensure_ascii=False)
                md5 = hashlib.md5(content.encode()).hexdigest()

                if is_duplicate(md5):
                    skipped_dup += 1
                    continue

                add_hash(md5)
                out.write(content + "\n")
                written +=

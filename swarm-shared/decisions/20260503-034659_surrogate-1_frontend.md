# surrogate-1 / frontend

## Implementation Plan (≤2h)

Replace `bin/dataset-enrich.sh` with a **manifest-driven, CDN-bypass ingestion worker** (`bin/dataset-enrich.py`) that:

- Accepts `SHARD_ID`, `SHARD_TOTAL=16` (matrix) and optional `DATE_FOLDER` (defaults to today `YYYY-MM-DD`).
- On the orchestrator side (Mac/CI), run a **single API call** to list one date folder via `list_repo_tree(recursive=False)` and emit `manifest.json` (path list only).
- Workers load `manifest.json`, take deterministic shard slice by `hash(slug) % SHARD_TOTAL == SHARD_ID`.
- Each worker downloads assigned files **via HF CDN URLs** (`https://huggingface.co/datasets/.../resolve/main/...`) with **no Authorization header** → bypasses `/api/` rate limits.
- Parse each file with schema tolerance:
  - Parquet/JSONL/JSON: attempt to project to `{prompt, response}` fields; if missing, try common aliases (`instruction`, `input`, `output`, `question`, `answer`, `text`).
  - Skip files that can’t be projected (log + continue).
- Normalize to `{prompt, response}` UTF-8 strings; compute `md5 = hashlib.md5((prompt+response).encode()).hexdigest()()`.
- Local dedup within worker using the shared `lib/dedup.py` SQLite store (central md5 store) to avoid duplicates across shards.
- Emit one output file: `batches/public-merged/{date}/shard{SHARD_ID}-{HHMMSS}.jsonl` with one JSON object per line.
- Push via `huggingface_hub` upload (token from `HF_TOKEN`) to `axentx/surrogate-1-training-pairs` using the deterministic filename (no collisions across shards/iterations).

### Why this satisfies past patterns
- **HF CDN bypass**: workers use CDN URLs only during training data fetch → zero API calls while streaming; avoids 429.
- **Single API list**: orchestrator does one `list_repo_tree` per date folder and embeds manifest → respects rate limits.
- **Mixed schema tolerance**: per-file projection + skip instead of failing on schema mismatch (avoids pyarrow CastError).
- **Deterministic sharding**: `hash(slug) % SHARD_TOTAL` ensures stable assignment across reruns.
- **Central dedup**: reuses `lib/dedup.py` SQLite store to dedup across shards (best-effort; cross-run duplicates still possible per trade-offs).
- **No extra columns**: output is `{prompt, response}` only; attribution via filename pattern.

---

## Concrete steps (≤2h)

1. Inspect current `bin/dataset-enrich.sh` and `lib/dedup.py` to confirm interfaces.
2. Create `bin/dataset-enrich.py` implementing the worker logic above.
3. Create small orchestrator helper `bin/build-manifest.py` (used locally/CI before matrix dispatch) that:
   - Calls `list_repo_tree(path=date_folder, recursive=False)`
   - Emits `manifest.json` with `{"date": "...", "files": [...]}`
4. Update `.github/workflows/ingest.yml` to:
   - Generate `manifest.json` in a prior job (or accept it as an artifact) and pass to matrix jobs.
   - Pass `SHARD_ID`/`SHARD_TOTAL` matrix variables.
   - Set `SHELL=/bin/bash` for any script steps (per pattern).
5. Add/confirm `requirements.txt` includes: `datasets`, `huggingface_hub`, `pyarrow`, `numpy`, `requests`.
6. Test locally with a small date folder subset.

---

## Code snippets

### `bin/dataset-enrich.py` (worker)

```python
#!/usr/bin/env python3
"""
CDN-bypass ingestion worker for surrogate-1 public dataset.
Usage:
  SHARD_ID=0 SHARD_TOTAL=16 python bin/dataset-enrich.py \
    --repo axentx/surrogate-1-training-pairs \
    --date 2026-05-03 \
    --manifest manifest.json \
    --out-dir batches
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from huggingface_hub import HfApi, hf_hub_download

# Local dedup store
sys.path.insert(0, str(Path(__file__).parent))
from lib.dedup import DedupStore  # type: ignore

HF_CDN_TEMPLATE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
PROMPT_ALIASES = {"prompt", "instruction", "question", "input", "text"}
RESPONSE_ALIASES = {"response", "answer", "output"}

def deterministic_shard(key: str, total: int) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % total

def normalize_record(rec: Dict) -> Optional[Tuple[str, str]]:
    # Try exact keys first
    prompt = rec.get("prompt")
    response = rec.get("response")
    if prompt is not None and response is not None:
        return str(prompt).strip(), str(response).strip()

    # Try aliases
    prompt_keys = [k for k in rec if k in PROMPT_ALIASES]
    response_keys = [k for k in rec if k in RESPONSE_ALIASES]

    if prompt_keys and response_keys:
        return str(rec[prompt_keys[0]]).strip(), str(rec[response_keys[0]]).strip()

    # Fallback: if only one candidate pair present
    if len(rec) == 2:
        vals = list(rec.values())
        return str(vals[0]).strip(), str(vals[1]).strip()

    return None

def download_via_cdn(repo: str, path: str, timeout: int = 30) -> bytes:
    url = HF_CDN_TEMPLATE.format(repo=repo, path=path)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def parse_file(repo: str, path: str, dedup: DedupStore) -> List[Dict]:
    content = download_via_cdn(repo, path)
    ext = Path(path).suffix.lower()
    pairs = []

    try:
        if ext == ".parquet":
            table = pq.read_table(pa.BufferReader(content))
            df = table.to_pandas()
        elif ext in (".jsonl", ".ndjson"):
            lines = [ln.strip() for ln in content.decode().splitlines() if ln.strip()]
            df = pa.Table.from_pylist([json.loads(ln) for ln in lines]).to_pandas()
        elif ext == ".json":
            df = pa.Table.from_pylist(json.loads(content.decode())).to_pandas()
        else:
            print(f"Unsupported extension {ext} for {path}", file=sys.stderr)
            return pairs
    except Exception as exc:
        print(f"Failed to parse {path}: {exc}", file=sys.stderr)
        return pairs

    for _, row in df.iterrows():
        rec = row.where(pd.notna(row), None).to_dict()
        norm = normalize_record(rec)
        if norm is None:
            continue
        prompt, response = norm
        if not prompt or not response:
            continue
        md5 = hashlib.md5((prompt + response).encode()).hexdigest()
        if dedup.seen(md5):
            continue
        dedup.add(md5)
        pairs.append({"prompt": prompt, "response": response})

    return pairs

def main() -> None:
    parser = argparse.ArgumentParser(description="CDN-bypass surrogate-1 worker")
    parser.add_argument("--repo", default="axentx/surrogate-1-training-pairs")
    parser.add_argument("--date", help="Date folder (YYYY-MM-DD)")
    parser.add_argument("--manifest", required=True, help="Path to manifest.json")
    parser.add_argument("--out-dir", default="batches")
    parser.add_argument("--shard-id", type=int, default=int(os.environ.get("SHARD_ID", 0)))
    parser.add_argument("--shard-total", type=int, default=int(os.environ.get("SHARD_TOTAL", 16)))
    args = parser.parse_args()

    if args.shard_total <= 0 or args
